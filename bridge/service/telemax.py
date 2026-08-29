"""One process, two phases, one polling loop per bot.

`python -m bridge run` starts *this*, not the bridge worker. The difference
matters: the guardian bot has to answer before MAX exists — that is where the
MAX login happens — and it has to keep answering across a restart of the worker.
So the guardian is owned here, started once, and polled once. The bridge worker
is a component underneath it that can be started, stopped and started again
without the guardian ever going quiet.

    TelemaxRuntime
    ├── process lock          one Telemax per data directory
    ├── Guardian bot          one Bot, one poller, one dispatcher
    │   ├── onboarding router MAX login, /status, /restart
    │   └── guardian router   dialogs, new contacts, pasted tokens
    └── BridgeService         the worker: MAX, bridge bots, routing

Phases are honest rather than decorative. Until MAX has a valid session the
runtime is ONBOARDING and there is no worker at all; afterwards it is RUNNING
and the worker exists. Both are visible in `/status`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
from dataclasses import replace
from datetime import tzinfo
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bridge.config import LoadedConfig, ProvisioningMode, load_config
from bridge.config.writer import set_config_value, set_env_value
from bridge.max_client import MaxClient
from bridge.onboarding import MaxOnboarding, Stage, StateStore, StatusBoard, screens
from bridge.onboarding.maxauth import MaxAuthGateway
from bridge.onboarding.router import build_onboarding_router
from bridge.onboarding.views import (
    AttentionFacts,
    BridgeFacts,
    BridgeUiState,
    BridgeView,
    HomeView,
    UserProblem,
    bridge_view,
    bridge_views,
    home_view,
    problems,
)
from bridge.provisioning import Guardian, GuardianContext, build_guardian_router
from bridge.provisioning.business import use_state_dir as use_business_state_dir
from bridge.service.incidents import build_incidents_router
from bridge.telegram import OwnerOnlyMiddleware

from .lock import ProcessLock
from .runtime import BridgeService

logger = logging.getLogger(__name__)

#: Long enough for somebody to read a code off one phone and type it into
#: another, and short enough that an abandoned attempt does not hold a socket
#: open all day. Must exceed the gateway's own prompt timeout.
MAX_LOGIN_TIMEOUT_SECONDS = 660.0


class Phase(StrEnum):
    ONBOARDING = "onboarding"
    RUNNING = "running"


class StartupError(Exception):
    """The process cannot start. The message is for a person, not a log."""


class TelemaxRuntime:
    """The process. Owns the lock, the guardian, and the worker's lifetime."""

    def __init__(
        self,
        loaded: LoadedConfig,
        *,
        config_path: Path | None = None,
        bot_factory: Any = Bot,
    ) -> None:
        self._loaded = loaded
        self._config_path = config_path or loaded.config_path
        self._store = StateStore.for_data_dir(loaded.app.paths.data_dir)
        self._lock = ProcessLock.for_data_dir(loaded.app.paths.data_dir)
        self._bot_factory = bot_factory

        self._guardian: Guardian | None = None
        self._guardian_bot_id = 0
        self._context = GuardianContext(is_guardian=self._is_guardian)
        self._service: BridgeService | None = None
        self._board: StatusBoard | None = None
        self._onboarding: MaxOnboarding | None = None
        self._phase = Phase.ONBOARDING
        # One at a time: a tap on «Перезапустить» while the first restart is
        # still tearing bots down would produce two workers.
        self._worker_lock = asyncio.Lock()

    # ------------------------------------------------------------------ status

    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def service(self) -> BridgeService | None:
        return self._service

    async def status_lines(self) -> list[str]:
        return [line for _, line in await self._status_pairs()]

    async def diagnostic_sections(self) -> dict[str, list[str]]:
        """The technical block, split for the four drill-downs."""
        sections: dict[str, list[str]] = {}
        for section, line in await self._status_pairs():
            sections.setdefault(section, []).append(line)
        return sections

    async def diagnostic_lights(self) -> dict[str, bool]:
        service = self._service
        if service is None:
            return {"telegram": self._guardian is not None, "max": False,
                    "delivery": False, "database": False}
        lights = await service.diagnostic_lights()
        lights["telegram"] = self._guardian is not None
        return lights

    async def _status_pairs(self) -> list[tuple[str, str]]:
        lines: list[tuple[str, str]] = [
            ("link", f"Telegram   {'подключён' if self._guardian else 'нет связи'}")
        ]
        service = self._service
        if service is None:
            lines.append(("link", "MAX        не подключён"))
            lines.append(("link", "Мост       не запущен"))
        else:
            lines.extend(await service.diagnostic_pairs())
        timezone = self._loaded.app.telegram.timezone
        if timezone:
            lines.append(("link", f"Пояс       {timezone}"))
        return lines

    # --------------------------------------------------------------- lifecycle

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.stop()

    async def start(self) -> None:
        app = self._loaded.app

        # Checked before the lock: refusing to start is not a reason to have
        # claimed the data directory in the meantime.
        token = self._guardian_token()
        if not token:
            raise StartupError(
                "Нет токена бота-стража.\nЗапустите `python -m bridge setup` ещё раз."
            )

        self._lock.acquire()

        # The bot first: the status board and the `is this the guardian?` filter
        # both need its identity, and both are wired before anything is polled.
        # HTML on the guardian only. A bridge bot carries a contact's own words
        # and gets `parse_mode=None`, which is the difference between a message
        # that says `<3` and one Telegram refuses to deliver.
        bot = self._bot_factory(
            token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML)
        )
        self._guardian_bot_id = int(bot.id)
        self._board = StatusBoard(bot=bot, chat_id=app.telegram.owner_user_id, store=self._store)
        # Everything the guardian draws — onboarding, menu, picker, provisioning
        # progress — goes into that one message.
        self._context.draw = self._board.draw
        self._onboarding = self._build_onboarding()

        # Before polling starts: a business connection arrives in a single update
        # and there is no way to ask for it again, so the place to write it down
        # has to be known already. This is the process that owns the guardian —
        # `BridgeService` has its own copy of this line for the path where it does.
        use_business_state_dir(app.paths.data_dir / "state")
        self._guardian = await Guardian.start(
            token=token,
            dispatcher=self._build_dispatcher(self._onboarding),
            owner_chat_id=app.telegram.owner_user_id,
            bot=bot,
        )
        if self._store.load().stage is Stage.BOOTSTRAP_CONFIGURED:
            self._store.update(stage=Stage.BOT_RUNNING)
        logger.info("guardian bot is up (id %s)", self._guardian_bot_id)

        await self._resume()

    async def _resume(self) -> None:
        """Pick up wherever the last run left off.

        A valid MAX session is the deciding fact, not the recorded stage: a
        session file that works means the owner should never be asked for a code
        again, whatever the state file happens to say.
        """
        if self._can_serve():
            try:
                await self._start_worker()
            except Exception as error:
                logger.exception("the bridge worker did not come up at start-up")
                await self._show(screens.launch_failed(_short(error)))
                return
            if self._loaded.app.telegram.timezone is None and self._onboarding is not None:
                # A working bridge with no timezone still owes the owner one
                # question, and nobody is going to send /start to be asked it.
                await self._onboarding.offer()
                return
            # Otherwise the anchor still shows whatever it showed before the
            # process stopped — a screen from another version, or a progress
            # display for a run that ended while nobody was looking. It is
            # edited in place, so there is no way to tell that by reading it.
            await self._show(screens.home(await self.home_view()))
            return

        record = self._store.load()
        if record.status_message_id is not None and self._onboarding is not None:
            # The owner has been in this chat before: continue the conversation
            # instead of waiting for a /start they have no reason to send.
            await self._onboarding.offer()

    async def stop(self) -> None:
        if self._onboarding is not None:
            await self._onboarding.shutdown()
        await self._stop_worker()
        if self._guardian is not None:
            await self._guardian.stop()
            self._guardian = None
        self._lock.release()

    async def run_forever(self) -> None:
        """Serve until SIGTERM or SIGINT, then shut down cleanly."""
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        logger.info("shutting down")

    # ----------------------------------------------------------- worker control

    async def _start_worker(self) -> None:
        async with self._worker_lock:
            if self._service is not None:
                return
            service = BridgeService(
                self._loaded,
                guardian=self._guardian,
                guardian_context=self._context,
                mirror_own_messages=self._own_messages_mirror,
                # The same store the status board, the state machine and the
                # onboarding router write through. Two stores over one file are
                # two caches, and the second writer rolls the first one back.
                state=self._store,
            )
            try:
                await service.start()
            except Exception:
                # Half a worker is worse than none: it would hold bot sessions
                # open that nothing polls.
                with contextlib.suppress(Exception):
                    await service.stop()
                raise
            self._service = service
            self._phase = Phase.RUNNING
            self._store.update(stage=Stage.BRIDGE_RUNNING)

    async def _stop_worker(self) -> None:
        async with self._worker_lock:
            service, self._service = self._service, None
            self._phase = Phase.ONBOARDING
            if service is not None:
                await service.stop()

    async def restart_bridge(self) -> bool:
        """Stop and start the worker. Never spawns a second one."""
        await self._stop_worker()
        try:
            await self._start_worker()
        except Exception:
            logger.exception("restart failed")
            return False
        return True

    # ------------------------------------------------------ own-message mirror

    def _own_messages_mirror(self) -> bool:
        """Whether to carry the owner's own MAX messages, right now.

        The owner's saved choice wins; absent one, the config default answers.
        Read straight from the store each call so the running router — which
        holds this very method as its resolver — sees a flip on the home screen
        without a restart.
        """
        override = self._store.load().mirror_own_messages
        if override is not None:
            return override
        return self._loaded.app.own_messages.mirror

    async def own_messages_mirror(self) -> bool:
        """Control surface: the current answer, for drawing the home toggle."""
        return self._own_messages_mirror()

    async def set_own_messages_mirror(self, value: bool) -> None:
        """Control surface: the owner flipped the home toggle."""
        self._store.update(mirror_own_messages=value)

    # -------------------------------------------------------------- onboarding

    def _build_onboarding(self) -> MaxOnboarding:
        return MaxOnboarding(
            store=self._store,
            show=self._show,
            connect=self._connect_max,
            persist=self._persist,
            launch=self._start_worker,
            timezone=self._loaded.app.telegram.timezone,
            timezone_reader=lambda: self._loaded.app.telegram.timezone,
            timezone_writer=self._write_timezone,
            telegram_phone=self._telegram_phone,
        )

    def _telegram_phone(self) -> str | None:
        """The number the console logged Telegram in with, if it is still there.

        Read from the environment rather than kept in the state file: it is
        already in `.env` at 0600 beside the config, and a personal phone number
        written down twice is a phone number leaked twice.
        """
        variable = self._loaded.app.provisioning.mtproto_phone_env
        return os.environ.get(variable, "").strip() or None

    async def _write_timezone(self, name: str) -> None:
        """Store the zone, prove the config still loads, and apply it.

        A worker that is already running keeps stamping in the old zone until it
        is rebuilt, so it is restarted — normally there is none, because the
        timezone is the first question of onboarding.
        """
        config_path = self._config_path
        if config_path is not None:
            if not set_config_value(config_path, "telegram", "timezone", name):
                raise RuntimeError("в конфиге нет секции telegram")
            os.chmod(config_path, 0o600)
        self._loaded = load_config(config_path)
        logger.info("timezone set to %s", name)
        if self._service is not None:
            await self.restart_bridge()

    async def attention_facts(self) -> AttentionFacts:
        """Everything the home verdict is decided from. Read-only, one pass."""
        service = self._service
        if service is None:
            # No worker at all: nothing else can be true, and «MAX не подключён»
            # would be a smaller claim than the situation deserves.
            return AttentionFacts(
                worker_running=False, telegram_connected=self._guardian is not None
            )
        facts: AttentionFacts = await service.attention_facts()
        return replace(facts, telegram_connected=self._guardian is not None)

    async def home_view(self) -> HomeView:
        """The first screen's model. Three answers and no fourth one."""
        return home_view(await self.attention_facts())

    async def problems(self) -> list[UserProblem]:
        """Every open problem, in the owner's terms, with its jobs expanded.

        The count is the same either way — the home badge counts placeholders
        and this expands them into one entry per contact and moment — which is
        what lets «⚠️ Проблемы · 2» agree with the list behind it.
        """
        service = self._service
        facts = await self.attention_facts()
        if service is None:
            return problems(facts)
        return problems(
            facts,
            jobs=await service.attention_jobs(),
            attempts=await service.unfinished_attempts(),
            now_ms=int(time.time() * 1000),
            tz=self._zone(),
        )

    async def retry_job(self, job_id: int) -> bool:
        service = self._service
        return bool(service is not None and await service.retry_job(job_id))

    async def settle_job(self, job_id: int) -> bool:
        service = self._service
        return bool(service is not None and await service.settle_job(job_id))

    async def archive_job(self, job_id: int) -> bool:
        service = self._service
        return bool(service is not None and await service.archive_job(job_id))

    async def resume_attempt(self, max_chat_id: int) -> str | None:
        """Push one interrupted attempt on — the same walk `/provretry` runs."""
        service = self._service
        if service is None:
            return None
        resume = getattr(service, "_resume_attempt", None)
        if resume is None:
            return None
        outcome: str | None = await resume(max_chat_id)
        return outcome

    async def abandon_attempt(self, max_chat_id: int) -> bool:
        service = self._service
        return bool(service is not None and await service.abandon_attempt(max_chat_id))

    async def timezone_label(self) -> str | None:
        """«Москва · UTC+03:00» — the human form, which is the only form shown."""
        from bridge.bootstrap.timezones import human_label, is_known_timezone

        timezone = self._loaded.app.telegram.timezone
        if not timezone:
            return None
        return human_label(timezone) if is_known_timezone(timezone) else timezone

    async def bridge_views(self) -> list[BridgeView]:
        service = self._service
        if service is None:
            return []
        return bridge_views(
            await service.bridge_facts(), now_ms=int(time.time() * 1000), tz=self._zone()
        )

    async def bridge_view(self, max_chat_id: int) -> BridgeView | None:
        """One bridge, including a disabled one — its bot is still a bot.

        The confirmation screens read the contact's name from here *before* they
        act, so this has to answer for a bridge that is about to stop existing.
        """
        for view in await self.bridge_views():
            if view.max_chat_id == max_chat_id:
                return view
        service = self._service
        if service is None:
            return None
        summary, _ = await service.bridge_card(max_chat_id)
        if summary is None:
            return None
        return bridge_view(
            BridgeFacts(
                title=summary.title,
                username=summary.username,
                bridge_name=summary.bridge_name,
                max_chat_id=summary.max_chat_id,
                bot_id=summary.bot_id,
                state=BridgeUiState.ACTIVE if summary.running else BridgeUiState.DISABLED,
            ),
            now_ms=int(time.time() * 1000),
            tz=self._zone(),
        )

    def _zone(self) -> tzinfo | None:
        """The owner's zone, for «сегодня в 08:18». None means the host's."""
        name = self._loaded.app.telegram.timezone
        if not name:
            return None
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 - a bad zone is not worth a dead screen
            return None

    async def bridges(self) -> list[Any]:
        service = self._service
        return await service.bridges() if service is not None else []

    async def bridge_card(self, max_chat_id: int) -> tuple[Any, list[str]]:
        service = self._service
        if service is None:
            return None, []
        return await service.bridge_card(max_chat_id)

    async def repull_history(self, max_chat_id: int) -> tuple[int, int] | None:
        """Wipe what this bridge delivered and pull the conversation again.

        The import draws its own progress into the anchor, which is why the board
        goes in rather than the router waiting on a silent operation.
        """
        service = self._service
        if service is None or self._board is None:
            return None
        return await service.repull_history(max_chat_id, self._board.draw)

    async def disconnect_bridge(self, max_chat_id: int) -> bool:
        """Stop one bridge. Reversible, and the screen behind it says how."""
        service = self._service
        if service is None:
            return False
        return await service.disconnect_bridge(max_chat_id)

    @property
    def can_delete_bots(self) -> bool:
        """Whether deleting a bot is offered as a button or as a recipe."""
        service = self._service
        return service is not None and bool(service.can_delete_bots)

    @property
    def can_wipe_dialogs(self) -> bool:
        """Whether the chat with a bot can be emptied from here."""
        service = self._service
        return service is not None and bool(service.can_wipe_dialogs)

    async def tear_down_bridge(self, max_chat_id: int) -> Any:
        """Bridge, conversation and bot, in one irreversible move."""
        service = self._service
        if service is None:
            return None
        return await service.tear_down_bridge(max_chat_id)

    async def delete_bot(self, max_chat_id: int) -> str | None:
        """Delete one bridge's bot. Irreversible, and it frees the slot."""
        service = self._service
        if service is None:
            return None
        return await service.delete_bot(max_chat_id)

    async def _show(self, screen: tuple[str, Any]) -> None:
        if self._board is not None:
            await self._board.show(screen)

    async def _connect_max(self, phone: str, gateway: MaxAuthGateway) -> None:
        """Log in to MAX with codes typed into the guardian chat.

        The session is dropped again as soon as it proves itself: the worker
        opens its own from the file on disk, and two live sessions for one
        account is a way to get the account logged out.
        """
        from pymax import Client

        app = self._loaded.app
        client = MaxClient(
            phone=phone,
            session_dir=app.max_session_dir,
            session_name=app.max.session_name,
            client_factory=lambda **kwargs: Client(
                **kwargs, sms_code_provider=gateway, password_provider=gateway
            ),
            timezone=app.telegram.timezone,
        )
        try:
            await client.start(timeout=MAX_LOGIN_TIMEOUT_SECONDS)
        finally:
            with contextlib.suppress(Exception):
                await client.stop()

    async def _persist(self, phone: str) -> None:
        """Write what the worker will need, atomically, and prove it loads."""
        app = self._loaded.app
        config_path = self._config_path
        env_path = (config_path.parent / ".env") if config_path else Path(".env")

        set_env_value(env_path, app.max.phone_env, phone)
        if config_path is not None:
            # Idempotent, and it forces the whole file through the atomic write
            # path — a config that cannot be rewritten is worth finding out
            # about now rather than at the next restart.
            set_config_value(config_path, "max", "phone_env", app.max.phone_env)
            os.chmod(config_path, 0o600)

        # Programmatic validation, not a promise: this is the same loader the
        # worker uses, so a config that passes here starts there.
        self._loaded = load_config(config_path)
        self._store.update(stage=Stage.MAX_SESSION_VALID)

    # ------------------------------------------------------------------ wiring

    def _build_dispatcher(self, onboarding: MaxOnboarding) -> Dispatcher:
        """The guardian's own dispatcher, built once and never added to again.

        Bridge bots get a separate one inside the worker, so a restart replaces
        that dispatcher wholesale instead of stacking a second copy of every
        handler on this one.
        """
        dispatcher = Dispatcher()
        dispatcher.update.outer_middleware(
            OwnerOnlyMiddleware(self._loaded.app.telegram.owner_user_id)
        )
        # Guardian first: it owns `/dialogs` and, more importantly, the handler
        # that recognises a pasted bot token and deletes it. Anything it does
        # not claim falls through to onboarding.
        dispatcher.include_router(build_guardian_router(self._context))
        # /failed, /ambiguous, /retry, /resolved. Registered here rather than in
        # the worker: the queue they act on is replaced on every restart, but
        # these handlers must survive one — a stuck job is exactly what the
        # owner asks about *after* the bridge has bounced.
        dispatcher.include_router(build_incidents_router(self._context))
        dispatcher.include_router(
            build_onboarding_router(
                owner_user_id=self._loaded.app.telegram.owner_user_id,
                store=self._store,
                onboarding=onboarding,
                control=self,
                is_guardian=self._is_guardian,
                show=self._show,
            )
        )
        return dispatcher

    def _is_guardian(self, bot_id: int) -> bool:
        return bot_id == self._guardian_bot_id

    def _guardian_token(self) -> str:
        variable = self._loaded.app.provisioning.guardian_bot_token_env or ""
        return os.environ.get(variable, "").strip()

    def _can_serve(self) -> bool:
        """Everything the worker needs, checked before it is asked to start."""
        app = self._loaded.app
        if app.provisioning.mode is ProvisioningMode.OFF and not self._loaded.bridges:
            return False
        if not os.environ.get(app.max.phone_env, "").strip():
            return False
        return (app.max_session_dir / app.max.session_name).exists()


def _short(error: BaseException) -> str:
    from bridge.observability.redaction import redact

    return redact(str(error) or type(error).__name__)[:160]


async def serve(config_path: Path | None = None) -> int:
    """`python -m bridge run`, minus the argument parsing."""
    loaded = load_config(config_path)
    async with TelemaxRuntime(loaded, config_path=loaded.config_path) as runtime:
        await runtime.run_forever()
    return 0


__all__ = ["Phase", "TelemaxRuntime", "serve"]
