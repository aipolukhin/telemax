"""`python -m bridge setup` — from an empty machine to a chat, and then stop.

The console asks for exactly three things it cannot find out: an api_id, an
api_hash and a phone. Everything else it works out or writes itself — the
owner's Telegram id comes from the session it opens, the guardian bot's username
is derived from that id, the config and the service unit are written, and the
last thing it does is hand the owner a link and get out of the way.

Two things are deliberately *not* here.

**MAX.** Connecting it needs a code that arrives minutes later, on a phone, and
asking for that in a terminal is what forced people to keep an SSH session open
through the whole setup. It happens in the bot now.

**The timezone.** A terminal on a server cannot know what the owner's clock
says — the host is usually UTC — and the owner is holding the device that does.
So the config is written with `timezone: null` and the guardian asks first
thing.

Nothing is reported as done before it is observed: the service is polled until
systemd says `active`, and only then is the link sent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from bridge.config.writer import (
    set_config_value,
    write_bootstrap_config,
)
from bridge.onboarding.state import Stage, StateStore
from bridge.provisioning.naming_v2 import NamingVersion

from . import handoff as handoff_module
from .identity import IdentityUnavailableError, owner_identities
from .managed import adopt_guardian
from .plan import Plan, read_plan
from .systemd import ServiceError, ServiceManager, ServiceSpec
from .telegram import (
    TelegramAccount,
    connect_account,
    ensure_guardian,
    offer_owner_session,
)
from .ui import SetupCancelled, Ui

logger = logging.getLogger(__name__)

TOTAL_STEPS = 1


def write_config(plan: Plan, ui: Ui, *, owner_user_id: int) -> bool:
    """Create the config, or edit the one that is already there.

    Returns True when the file was created by this run — which is the only case
    where cancelling later is allowed to delete it.

    The timezone is never written here. An existing file keeps whatever it says;
    a new one gets `null`, which the guardian recognises as "ask first".
    """
    if not plan.exists:
        write_bootstrap_config(
            plan.config_path,
            owner_user_id=owner_user_id,
            timezone=None,
            guardian_token_env=plan.guardian_token_env,
            data_dir=str(plan.data_dir),
        )
        ui.ok(f"Конфигурация создана: {plan.config_path}")
        return True

    edits = (
        ("telegram", "owner_user_id", str(owner_user_id)),
        ("provisioning", "mode", "managed"),
        ("provisioning", "guardian_bot_token_env", plan.guardian_token_env),
    )
    for section, key, value in edits:
        if not set_config_value(plan.config_path, section, key, value):
            ui.warn(f"В конфиге нет секции `{section}:` — допишите `{key}: {value}` руками.")
    ui.ok(f"Конфигурация обновлена: {plan.config_path}")
    return False


def install_service(
    plan: Plan, ui: Ui, *, manager: ServiceManager, instance: str | None = None
) -> Path:
    """Step three, unnumbered: the thing that survives the terminal closing."""
    spec = ServiceSpec.detect(plan.config_path, instance=instance)
    with ui.working("Устанавливаю сервис Telemax…"):
        path = manager.install(spec)
    ui.ok("Сервис Telemax запущен")
    return path


async def _deliver_link(
    plan: Plan, ui: Ui, account: TelegramAccount
) -> handoff_module.Handoff:
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    store = StateStore.for_data_dir(plan.data_dir)
    # HTML, like the guardian this token belongs to: the invite is a `screens`
    # screen, and sending it as plain text would show the owner its own markup.
    bot = Bot(
        token=account.guardian_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        with ui.working("Передаю настройку в Telegram…"):
            result = await handoff_module.hand_off(
                store=store,
                session=account.session,
                bot=bot,
                bot_username=account.guardian_username,
                owner_user_id=account.owner_user_id,
            )
    finally:
        with contextlib.suppress(Exception):
            await bot.session.close()
    return result


def _final_screen(ui: Ui, account: TelegramAccount, result: handoff_module.Handoff) -> None:
    ui.panel("Продолжите в Telegram")
    ui.ok(f"Бот-страж: @{result.bot_username}")
    ui.ok("Сервис Telemax запущен")

    if result.delivery is handoff_module.Delivery.BOT:
        ui.ok("Приглашение отправлено в чат с ботом")
    elif result.delivery is handoff_module.Delivery.SAVED_MESSAGES:
        ui.ok("Ссылка отправлена в «Избранное»")
    else:
        ui.warn("Не смог отправить ссылку — откройте её вручную:")
        ui.plain(result.link)

    ui.plain()
    ui.plain("Откройте Telegram и завершите настройку в боте.")
    ui.plain()
    ui.note("Можно закрыть терминал.")


async def _expected_guardian(plan: Plan, session: Any) -> str | None:
    """The V2 guardian name, when both owner accounts can be established.

    None means "fall back to the V1 name", which only ever recognises a guardian
    that already exists under it. Deliberately quiet: this is the session path,
    kept for debugging, and a missing MAX session there is a reason to use the
    old name rather than to stop.
    """
    telegram: int | None = plan.owner_user_id
    reader = getattr(session, "own_user_id", None)
    if telegram is None and reader is not None:
        with contextlib.suppress(Exception):
            telegram = await reader()
    try:
        return (
            await owner_identities(plan, telegram_user_id=telegram)
        ).guardian_username
    except IdentityUnavailableError:
        return None


async def _guardian_plan(plan: Plan, ui: Ui, *, adopt: bool) -> tuple[str | None, int | None]:
    """The username the guardian must have, and the owner id it was derived from.

    `(None, None)` is the adopt path: the owner already has a guardian and what
    `getMe` says about it is the truth. Anything else is V2 — the name is a
    function of the Telegram and MAX owner accounts, so both have to be known
    *before* the bot exists, and there is no half of that formula worth using.
    """
    if adopt:
        ui.note("Режим приёма: имя стража берётся у самого бота, не вычисляется.")
        return None, None
    identities = await owner_identities(plan)
    ui.ok(f"Telegram владельца: {identities.telegram_user_id}")
    ui.ok(f"MAX владельца: {identities.max_user_id}")
    return identities.guardian_username, identities.telegram_user_id


async def bootstrap_managed(
    plan: Plan,
    ui: Ui,
    *,
    manager: ServiceManager,
    instance: str | None = None,
    adopt: bool = False,
) -> int:
    """The default: a token, a tap, and no Telegram account credential anywhere.

    Same shape as the session flow below and deliberately shorter — everything it
    does not do (log in, create a bot, read the owner's id from a session, deliver
    the link over the account) is what the account credential was for.

    The order changed with V2 naming: the two owner accounts are established
    first, because the guardian's own username is derived from them. When either
    is unknown this stops and says which — a guardian created under a name
    nothing else can recompute is worse than no guardian.
    """
    undo: list[Callable[[], None]] = []
    written: list[str] = []
    try:
        ui.banner()
        ui.step(1, TOTAL_STEPS, "Telegram")

        try:
            expected, known_owner = await _guardian_plan(plan, ui, adopt=adopt)
        except IdentityUnavailableError as missing:
            ui.plain()
            ui.fail(str(missing))
            return 2

        def forget() -> None:
            _forget(plan, written)

        undo.append(forget)
        guardian = await adopt_guardian(
            plan,
            ui,
            journal=written,
            expected_username=expected,
            owner_user_id=known_owner,
        )
        # The token is what this run is *for*: keeping it means a re-run does not
        # ask for it again, and the bot it names is not stranded.
        undo.remove(forget)

        ui.plain()
        created = write_config(plan, ui, owner_user_id=guardian.owner_user_id)
        if created:
            undo.append(lambda: plan.config_path.unlink(missing_ok=True))

        StateStore.for_data_dir(plan.data_dir).update(
            stage=Stage.BOOTSTRAP_CONFIGURED,
            owner_user_id=guardian.owner_user_id,
            guardian_username=guardian.username,
            # Which contract this guardian's name was minted under. Recorded, not
            # recomputed: an adopted guardian keeps whatever it is called, for
            # ever, and reconciliation has to be able to tell that from a name it
            # could work out for itself.
            guardian_naming=(
                NamingVersion.V2.value if expected else NamingVersion.ADOPTED.value
            ),
        )

        # The optional owner-session QR link, in the main flow rather than only in
        # a separate command. It authorises a session and nothing more — intake
        # stays disabled, delivery and runtime are untouched.
        await offer_owner_session(
            plan, ui, owner_user_id=guardian.owner_user_id, journal=written
        )

        install_service(plan, ui, manager=manager, instance=instance)
        undo.append(manager.uninstall)
        undo.clear()

        ui.panel("Продолжите в Telegram")
        ui.ok(f"Бот-страж: @{guardian.username}")
        ui.ok("Сервис Telemax запущен")
        ui.plain()
        ui.plain("Вернитесь в чат с ботом — он продолжит настройку сам.")
        ui.plain()
        ui.note("Можно закрыть терминал.")
        return 0
    except SetupCancelled:
        _roll_back(undo)
        ui.plain()
        ui.warn("Настройка отменена.")
        return 130
    except ServiceError as error:
        _roll_back(undo)
        ui.plain()
        ui.fail(str(error))
        ui.note("Мост не переживёт закрытие SSH — настройка остановлена здесь.")
        return 1
    except Exception as error:
        _roll_back(undo)
        logger.debug("setup failed", exc_info=True)
        ui.plain()
        ui.fail(f"Не довёл до конца: {_short(error)}")
        ui.note("Запустите `python -m bridge setup` ещё раз — сделанное не повторится.")
        return 1


async def bootstrap(
    plan: Plan, ui: Ui, *, manager: ServiceManager, instance: str | None = None
) -> int:
    undo: list[Callable[[], None]] = []
    # What this run typed into `.env`, so a cancellation can take it back out.
    # The Telegram *session* is never in here: it cost a code from the owner's
    # phone, and throwing it away would make a re-run harder, not cleaner.
    written: list[str] = []
    guardian_exists = False
    try:
        ui.banner()

        ui.step(1, TOTAL_STEPS, "Telegram")

        def forget() -> None:
            _forget(plan, written)

        undo.append(forget)
        session = await connect_account(plan, ui, journal=written)
        # The V2 name when both owner accounts can be established, and the V1
        # one otherwise — which only ever recognises a guardian that already
        # exists under it.
        expected = await _expected_guardian(plan, session)
        # The guardian may already exist from a previous bootstrap. Recreating
        # it means deleting it, and deleting it means the process holding its
        # token has to be gone first — hence `stop_runtime`.
        account = await ensure_guardian(
            plan, ui, session, stop_runtime=manager.stop, expected_username=expected
        )
        # From here a re-run must find the bot it already made: forgetting the
        # credentials would strand that bot in @BotFather and burn one of the
        # twenty an account is allowed.
        guardian_exists = True
        undo.remove(forget)

        ui.plain()
        created = write_config(plan, ui, owner_user_id=account.owner_user_id)
        if created:
            undo.append(lambda: plan.config_path.unlink(missing_ok=True))

        StateStore.for_data_dir(plan.data_dir).update(
            stage=Stage.BOOTSTRAP_CONFIGURED,
            owner_user_id=account.owner_user_id,
            guardian_username=account.guardian_username,
        )

        await offer_owner_session(
            plan, ui, owner_user_id=account.owner_user_id, journal=written
        )

        install_service(plan, ui, manager=manager, instance=instance)
        undo.append(manager.uninstall)

        result = await _deliver_link(plan, ui, account)
        undo.clear()

        with contextlib.suppress(Exception):
            await account.session.close()

        _final_screen(ui, account, result)
        return 0
    except SetupCancelled:
        _roll_back(undo)
        ui.plain()
        if guardian_exists:
            # Saying "nothing was saved" here would be a lie: there is a bot.
            ui.warn("Настройка отменена. Бот-страж уже создан.")
            ui.note("Запустите `python -m bridge setup` ещё раз — он продолжит с этого места.")
        else:
            ui.warn("Настройка отменена. Изменения не сохранены.")
        return 130
    except ServiceError as error:
        _roll_back(undo)
        ui.plain()
        ui.fail(str(error))
        ui.note("Мост не переживёт закрытие SSH — настройка остановлена здесь.")
        return 1
    except Exception as error:
        _roll_back(undo)
        logger.debug("setup failed", exc_info=True)
        ui.plain()
        ui.fail(f"Не довёл до конца: {_short(error)}")
        ui.note("Запустите `python -m bridge setup` ещё раз — сделанное не повторится.")
        return 1


def _load_existing_env(plan: Plan) -> None:
    """Read what a previous run already answered, so nothing is asked twice.

    Only the `.env` beside *this* config, never one in the working directory:
    setting up a second deployment from a checkout that already has credentials
    must not silently inherit them.
    """
    if not plan.env_path.is_file():
        return
    from dotenv import load_dotenv

    load_dotenv(plan.env_path, override=False)


def _forget(plan: Plan, keys: list[str]) -> None:
    from bridge.config.writer import unset_env_value

    for key in keys:
        unset_env_value(plan.env_path, key)
    keys.clear()


def _roll_back(undo: list[Callable[[], None]]) -> None:
    """Undo in reverse. Never raises: this is already the failure path."""
    for step in reversed(undo):
        with contextlib.suppress(Exception):
            step()
    undo.clear()


def _short(error: BaseException) -> str:
    from bridge.observability.redaction import redact

    return redact(str(error) or type(error).__name__)[:200]


def run(
    config_path: Path | None = None,
    *,
    ui: Ui | None = None,
    instance: str | None = None,
    use_session: bool = False,
    adopt: bool = False,
) -> int:
    """Install. `use_session` picks the old account-session path, for debugging.

    The default needs no account credential: Managed Bots create the contact bots
    and the owner's own tap identifies them. The session path is kept because it
    can still create the *first* guardian by itself, which is the one thing the
    default asks a person to do by hand.

    `adopt` is the explicit legacy path: take whatever guardian the owner already
    has, at whatever name, instead of deriving one from the two owner accounts.
    """
    plan = read_plan(config_path)
    _load_existing_env(plan)
    console = ui or Ui()
    manager = ServiceManager(instance=instance)
    if use_session:
        return _run(bootstrap(plan, console, manager=manager, instance=instance), console)
    return _run(
        bootstrap_managed(plan, console, manager=manager, instance=instance, adopt=adopt),
        console,
    )


def _run(flow: Coroutine[Any, Any, int], console: Ui) -> int:
    try:
        return asyncio.run(flow)
    except KeyboardInterrupt:
        # Ctrl+C outside a prompt: the same answer, without a traceback.
        console.plain()
        console.warn("Настройка отменена. Изменения не сохранены.")
        return 130
