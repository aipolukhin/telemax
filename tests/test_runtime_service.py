"""The process: one instance, one poller per bot, and a service that survives.

The failures these guard against are all silent ones. Two processes polling the
same token lose half the messages and log nothing. A user unit without lingering
dies when the SSH session ends, hours after setup said it was running. A config
truncated mid-write stops the service on the next restart, not on this one.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import bridge
from bridge.bootstrap.systemd import (
    UNIT_NAME,
    Completed,
    ServiceError,
    ServiceManager,
    ServiceSpec,
    render_unit,
    unit_name,
    unit_path,
)
from bridge.config import load_config
from bridge.config.writer import atomic_write_text, write_bootstrap_config
from bridge.onboarding.maxauth import AuthAbandoned, MaxAuthGateway, Prompt
from bridge.onboarding.state import Stage, StateStore, Step
from bridge.service import AlreadyRunning, BridgeService, ProcessLock
from bridge.service.supervisor import Supervisor

TOKEN = "9000000003:AAF-dummy-token-value-for-tests-0000"


# --------------------------------------------------------------- one instance


def test_a_second_instance_is_refused(tmp_path: Path) -> None:
    """Two pollers on one token lose half the conversation and say nothing."""
    first = ProcessLock.for_data_dir(tmp_path).acquire()
    try:
        with pytest.raises(AlreadyRunning, match="уже работает"):
            ProcessLock.for_data_dir(tmp_path).acquire()
    finally:
        first.release()

    # Released is released: a restart must not need a stale lock cleaned up.
    second = ProcessLock.for_data_dir(tmp_path).acquire()
    second.release()


def test_the_lock_file_names_its_holder(tmp_path: Path) -> None:
    lock = ProcessLock.for_data_dir(tmp_path).acquire()
    try:
        assert lock.path.read_text(encoding="utf-8").strip() == str(os.getpid())
        assert (lock.path.stat().st_mode & 0o777) == 0o600
    finally:
        lock.release()


def test_the_worker_does_not_own_the_guardian(tmp_path: Path) -> None:
    """One Bot, one polling loop: the runtime owns it, the worker borrows it."""
    config = tmp_path / "config.yaml"
    write_bootstrap_config(
        config,
        owner_user_id=1,
        timezone="Europe/Moscow",
        guardian_token_env="TG",
        data_dir=str(tmp_path / "data"),
    )
    loaded = load_config(config, load_env_file=False)

    borrowed = BridgeService(loaded, guardian=object())  # type: ignore[arg-type]
    own = BridgeService(loaded)

    assert borrowed._owns_guardian is False, "a borrowed guardian is never restarted"
    assert own._owns_guardian is True, "started alone, it still makes its own"


# ------------------------------------------------------------------ systemd


@dataclass
class FakeSystemd:
    """Records argv and answers `is-active` from a script."""

    active_after: int = 0
    fail_on: str | None = None
    #: How many restarts systemd has counted — a growing number is a crash loop.
    restarts: int = 0
    calls: list[list[str]] = field(default_factory=list)
    _probes: int = 0

    def __call__(self, args: Any, *, timeout: float = 30.0) -> Completed:
        argv = list(args)
        self.calls.append(argv)
        tail = argv[2:] if "--user" in argv else argv[1:]

        if self.fail_on is not None and self.fail_on in tail:
            return Completed(returncode=1, stderr="Job failed. See journalctl.")
        if tail[:1] == ["show"]:
            self._probes += 1
            active = self._probes > self.active_after
            lines = []
            for wanted in tail:
                if wanted == "--property=ActiveState":
                    lines.append(f"ActiveState={'active' if active else 'activating'}")
                elif wanted == "--property=SubState":
                    lines.append(f"SubState={'running' if active else 'start'}")
                elif wanted == "--property=NRestarts":
                    lines.append(f"NRestarts={self.restarts}")
            return Completed(returncode=0, stdout="\n".join(lines))
        return Completed(returncode=0, stdout="")

    def ran(self, *needle: str) -> bool:
        return any(list(needle) == argv[-len(needle):] for argv in self.calls)


@pytest.fixture
def spec(tmp_path: Path) -> ServiceSpec:
    return ServiceSpec.detect(tmp_path / "config.yaml")


def test_the_unit_uses_the_interpreter_that_is_running(spec: ServiceSpec) -> None:
    """Not `python`, not `.venv/bin/python` — the one that got us here."""
    text = render_unit(spec)

    # Not resolved: a venv's bin/python is a symlink to the system interpreter,
    # and following it produces a unit that cannot import a single dependency.
    assert spec.python == Path(sys.executable)
    assert f"ExecStart={sys.executable} -m bridge run" in text
    assert "ExecStart=python " not in text
    assert "ExecStart=python3 " not in text


def test_the_unit_uses_the_real_project_directory(spec: ServiceSpec) -> None:
    """Wherever the checkout happens to be — including a git worktree."""
    expected = Path(bridge.__file__).resolve().parent.parent
    assert f"WorkingDirectory={expected}" in render_unit(spec)


def test_no_path_in_the_unit_is_hard_coded() -> None:
    """Every path is discovered. The module itself names none of them."""
    from bridge.bootstrap import systemd

    source = Path(systemd.__file__).read_text(encoding="utf-8")
    for literal in ("/home/", ".venv/bin/python", "ExecStart=python"):
        assert literal not in source, f"{literal!r} is written into the unit template"


def test_the_unit_names_the_config_that_is_in_use(tmp_path: Path) -> None:
    spec = ServiceSpec.detect(tmp_path / "live.yaml")
    assert f"Environment=BRIDGE_CONFIG={tmp_path / 'live.yaml'}" in render_unit(spec)


def test_the_unit_holds_no_credentials(spec: ServiceSpec) -> None:
    text = render_unit(spec)
    for secret in (TOKEN, "TELEMAX_API_HASH=", "MAX_PHONE=+", "password"):
        assert secret not in text
    assert "Restart=on-failure" in text
    assert "RestartSec=" in text
    # Shutdown has to finish writing down what it already accepted. A SIGKILL
    # arriving first would undo the durable intake at the very last step.
    assert "TimeoutStopSec=" in text
    assert "NoNewPrivileges=true" in text
    assert "PrivateTmp=true" in text


def test_installing_enables_lingering_first(tmp_path: Path, monkeypatch: Any) -> None:
    """Without it, the unit dies the moment the SSH session ends."""
    runner = FakeSystemd()
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    manager = ServiceManager(runner=runner, home=tmp_path, sleep=lambda _: None)

    manager.install(ServiceSpec.detect(tmp_path / "config.yaml"))

    linger = [argv for argv in runner.calls if argv[0].endswith("loginctl")]
    assert linger, "lingering is what makes the service survive a logout"
    assert linger[0][1:2] == ["enable-linger"]
    order = runner.calls.index(linger[0])
    assert order < next(i for i, argv in enumerate(runner.calls) if "enable" in argv[2:])


def test_installing_writes_the_unit_and_waits_for_active(
    tmp_path: Path, monkeypatch: Any
) -> None:
    runner = FakeSystemd(active_after=2)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    manager = ServiceManager(
        runner=runner, home=tmp_path, sleep=lambda _: None, clock=_ticking(step=1.0)
    )

    path = manager.install(ServiceSpec.detect(tmp_path / "config.yaml"))

    assert path == unit_path(home=tmp_path)
    assert path.read_text(encoding="utf-8").startswith("[Unit]")
    assert runner.ran("daemon-reload")
    assert runner.ran("enable", UNIT_NAME)
    assert runner.ran("restart", UNIT_NAME)
    assert runner._probes > 1, "it kept asking until systemd said active"


def test_a_service_that_starts_and_dies_is_not_a_success(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """`Type=simple` calls a unit active the moment it forks.

    Measured on a real machine: `bridge run` refused its config and exited
    immediately, and systemd still answered `active` — so "it started" has to
    mean "it was still up a few seconds later".
    """
    runner = FakeSystemd(active_after=0)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    manager = ServiceManager(
        runner=runner, home=tmp_path, sleep=lambda _: _crash(runner), clock=_ticking(step=1.0)
    )

    with pytest.raises(ServiceError, match="сразу упал"):
        manager.install(ServiceSpec.detect(tmp_path / "config.yaml"))


def _crash(runner: FakeSystemd) -> None:
    """systemd notices the process died and starts counting restarts."""
    runner.restarts += 1


def test_a_service_that_never_becomes_active_is_an_error(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The whole reason `enable --now` returning 0 is not enough."""
    runner = FakeSystemd(active_after=10_000)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    manager = ServiceManager(
        runner=runner, home=tmp_path, sleep=lambda _: None, clock=_ticking()
    )

    with pytest.raises(ServiceError, match="не поднялся"):
        manager.install(ServiceSpec.detect(tmp_path / "config.yaml"))


def test_a_host_without_user_units_says_so(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    manager = ServiceManager(runner=FakeSystemd(), home=tmp_path, sleep=lambda _: None)

    with pytest.raises(ServiceError, match="systemctl --user"):
        manager.install(ServiceSpec.detect(tmp_path / "config.yaml"))


def test_uninstall_never_raises(tmp_path: Path, monkeypatch: Any) -> None:
    """It is the rollback path; throwing there would hide the real failure."""
    monkeypatch.setattr("shutil.which", lambda name: None)
    ServiceManager(runner=FakeSystemd(), home=tmp_path).uninstall()


def _ticking(step: float = 5.0) -> Any:
    """A clock that always moves, so no wait loop can spin in a test."""
    state = {"now": 0.0}

    def clock() -> float:
        state["now"] += step
        return state["now"]

    return clock


# ----------------------------------------------------------------- restarting


def test_an_interrupted_onboarding_resumes_but_forgets_the_secret(tmp_path: Path) -> None:
    store = StateStore.for_data_dir(tmp_path)
    store.update(
        stage=Stage.MAX_ONBOARDING_PENDING, step=Step.WAITING_FOR_2FA, status_message_id=7
    )

    after_restart = StateStore.for_data_dir(tmp_path).load()

    assert after_restart.step is Step.WAITING_FOR_PHONE, "the attempt died with the process"
    assert after_restart.stage is Stage.MAX_ONBOARDING_PENDING, "but the progress did not"
    assert after_restart.status_message_id == 7, "and the message to edit is remembered"

    on_disk = store.path.read_text(encoding="utf-8")
    assert "password" not in on_disk.lower()


def test_a_valid_session_means_the_owner_is_never_asked_again(tmp_path: Path) -> None:
    from bridge.service.telemax import TelemaxRuntime

    config = tmp_path / "config.yaml"
    write_bootstrap_config(
        config,
        owner_user_id=1,
        timezone="Europe/Moscow",
        guardian_token_env="TG",
        data_dir=str(tmp_path / "data"),
    )
    loaded = load_config(config, load_env_file=False)
    runtime = TelemaxRuntime(loaded, config_path=config)

    assert runtime._can_serve() is False, "no phone, no session: ask"

    os.environ["MAX_PHONE"] = "+79001234567"
    try:
        assert runtime._can_serve() is False, "a phone without a session is not enough"
        session = loaded.app.max_session_dir / loaded.app.max.session_name
        session.parent.mkdir(parents=True, exist_ok=True)
        session.write_text("pretend", encoding="utf-8")
        assert runtime._can_serve() is True, "session plus phone: serve, do not ask"
    finally:
        os.environ.pop("MAX_PHONE", None)


async def test_restarting_stops_before_it_starts(tmp_path: Path) -> None:
    """A restart that overlapped would be two workers on the same bots."""
    from bridge.service.telemax import TelemaxRuntime

    config = tmp_path / "config.yaml"
    write_bootstrap_config(
        config,
        owner_user_id=1,
        timezone="Europe/Moscow",
        guardian_token_env="TG",
        data_dir=str(tmp_path / "data"),
    )
    runtime = TelemaxRuntime(load_config(config, load_env_file=False), config_path=config)

    order: list[str] = []

    class FakeWorker:
        async def stop(self) -> None:
            order.append("stop")

    async def start_worker() -> None:
        order.append("start")
        runtime._service = FakeWorker()  # type: ignore[assignment]

    runtime._service = FakeWorker()  # type: ignore[assignment]
    runtime._start_worker = start_worker  # type: ignore[method-assign]

    assert await runtime.restart_bridge() is True
    assert order == ["stop", "start"], "never two workers at once"


async def test_a_failed_restart_is_reported_not_raised(tmp_path: Path) -> None:
    from bridge.service.telemax import TelemaxRuntime

    config = tmp_path / "config.yaml"
    write_bootstrap_config(
        config,
        owner_user_id=1,
        timezone="Europe/Moscow",
        guardian_token_env="TG",
        data_dir=str(tmp_path / "data"),
    )
    runtime = TelemaxRuntime(load_config(config, load_env_file=False), config_path=config)

    async def explode() -> None:
        raise RuntimeError("Telegram is unreachable")

    runtime._start_worker = explode  # type: ignore[method-assign]
    assert await runtime.restart_bridge() is False


async def test_a_serving_start_refreshes_the_anchor(tmp_path: Path) -> None:
    """A restart must leave the one message showing what is true now.

    The anchor is edited in place, so whatever it showed when the process
    stopped — a progress display for a finished run, a screen from the previous
    version — stays there looking current. Nobody is going to send `/start` to
    find out, so the resume draws home itself.
    """
    from bridge.service.telemax import TelemaxRuntime

    config = tmp_path / "config.yaml"
    write_bootstrap_config(
        config,
        owner_user_id=1,
        timezone="Europe/Moscow",
        guardian_token_env="TG",
        data_dir=str(tmp_path / "data"),
    )
    runtime = TelemaxRuntime(load_config(config, load_env_file=False), config_path=config)

    drawn: list[str] = []

    async def show(screen: tuple[str, Any]) -> None:
        drawn.append(screen[0])

    async def start_worker() -> None:
        return None

    runtime._show = show  # type: ignore[method-assign]
    runtime._start_worker = start_worker  # type: ignore[method-assign]
    runtime._can_serve = lambda: True  # type: ignore[method-assign]

    await runtime._resume()

    assert len(drawn) == 1, "one screen, not a conversation"
    assert "<b>Telemax</b>" in drawn[0]
    # The verdict, not the settings. The timezone is a preference and lives
    # behind «Настройки»; this screen answers whether anything is working.
    assert "Telegram" in drawn[0] and "MAX" in drawn[0]
    assert "Часовой пояс" not in drawn[0]


async def test_a_start_that_cannot_serve_draws_no_home(tmp_path: Path) -> None:
    """Onboarding owns the screen until MAX is connected."""
    from bridge.service.telemax import TelemaxRuntime

    config = tmp_path / "config.yaml"
    write_bootstrap_config(
        config,
        owner_user_id=1,
        timezone="Europe/Moscow",
        guardian_token_env="TG",
        data_dir=str(tmp_path / "data"),
    )
    runtime = TelemaxRuntime(load_config(config, load_env_file=False), config_path=config)

    drawn: list[str] = []

    async def show(screen: tuple[str, Any]) -> None:
        drawn.append(screen[0])

    runtime._show = show  # type: ignore[method-assign]
    runtime._can_serve = lambda: False  # type: ignore[method-assign]

    await runtime._resume()

    assert drawn == [], "no anchor yet, and nothing to say into it"


# --------------------------------------------------------------- async hygiene


async def test_an_abandoned_login_unwinds_instead_of_hanging() -> None:
    """Cancelling must not leave a task waiting on a future forever."""
    asked: list[Prompt] = []

    async def ask(prompt: Prompt) -> None:
        asked.append(prompt)

    gateway = MaxAuthGateway(ask)
    waiting = asyncio.create_task(gateway.get_code("+79001234567"))
    await asyncio.sleep(0)

    gateway.abandon("cancelled")
    with pytest.raises(AuthAbandoned):
        await waiting
    assert gateway.waiting_for is None


async def test_a_crashing_background_task_comes_back_with_backoff() -> None:
    """A dead loop is silent; the supervisor is what makes it not be."""
    attempts: list[float] = []
    supervisor = Supervisor(initial_backoff=0.001, max_backoff=0.004)

    async def flaky() -> None:
        attempts.append(asyncio.get_running_loop().time())
        if len(attempts) < 3:
            raise RuntimeError("network hiccup")
        await asyncio.Event().wait()

    supervisor.start("flaky", flaky)
    for _ in range(200):
        if len(attempts) >= 3:
            break
        await asyncio.sleep(0.005)

    assert len(attempts) >= 3
    assert supervisor.snapshot()["flaky"].restarts >= 2
    await supervisor.stop()


async def test_the_catch_up_keeps_its_cadence_after_a_failure() -> None:
    """One unreachable history call must not put the whole catch-up in backoff.

    The loop is supervised, and a supervised task that raises is restarted with
    backoff — right for a task that cannot work at all, wrong for a periodic
    pass that failed once. So the failure is logged and the next tick is on time.
    """
    from bridge.service.runtime import _on_a_timer

    passes: list[int] = []

    async def catch_up() -> None:
        passes.append(len(passes))
        if len(passes) == 1:
            raise RuntimeError("history is unreachable right now")

    task = asyncio.create_task(_on_a_timer(0.001, catch_up, label="test")())
    try:
        for _ in range(200):
            if len(passes) >= 3:
                break
            await asyncio.sleep(0.005)
    finally:
        task.cancel()

    assert len(passes) >= 3, "the loop stopped at the first failure"


# ------------------------------------------------------------ atomic on disk


def test_a_torn_write_never_reaches_the_config(tmp_path: Path, monkeypatch: Any) -> None:
    config = tmp_path / "config.yaml"
    write_bootstrap_config(
        config, owner_user_id=1, timezone="Europe/Moscow", guardian_token_env="TG"
    )
    before = config.read_text(encoding="utf-8")

    monkeypatch.setattr(os, "replace", _explode)
    with pytest.raises(OSError):
        atomic_write_text(config, "half a fi")

    assert config.read_text(encoding="utf-8") == before
    assert list(tmp_path.glob(".config.yaml.*")) == []


def _explode(*_: object) -> None:
    raise OSError("no space left on device")


# ------------------------------------------------------ two stands, one host


def test_a_named_instance_gets_its_own_unit() -> None:
    """Two installations is a real case: a second phone number, a staging
    account. Without a distinct unit the second setup overwrites the first."""
    assert unit_name(None) == "telemax.service"
    assert unit_name("beta") == "telemax-beta.service"
    assert unit_path(home=Path("/home/x"), instance="beta").name == "telemax-beta.service"
    assert unit_path(home=Path("/home/x")).name == "telemax.service"


def test_an_instance_name_is_made_safe_for_systemd() -> None:
    assert unit_name("Second Phone") == "telemax-second-phone.service"
    assert unit_name("тест/../etc") == "telemax-etc.service"
    assert unit_name("   ") == "telemax.service", "an empty name is no name"


def test_two_instances_do_not_share_a_unit_or_a_config(tmp_path: Path) -> None:
    first = ServiceSpec.detect(tmp_path / "a.yaml")
    second = ServiceSpec.detect(tmp_path / "b.yaml", instance="beta")

    assert first.unit_name != second.unit_name
    assert f"BRIDGE_CONFIG={tmp_path / 'a.yaml'}" in render_unit(first)
    assert f"BRIDGE_CONFIG={tmp_path / 'b.yaml'}" in render_unit(second)
    assert "(beta)" in render_unit(second), "systemctl list-units has to tell them apart"
    assert "(beta)" not in render_unit(first)


def test_a_manager_only_ever_touches_its_own_unit(tmp_path: Path) -> None:
    """A manager built for one instance must not be able to stop another."""
    seen: list[list[str]] = []

    def runner(args: Any, *, timeout: float = 30.0) -> Any:
        seen.append(list(args))
        return Completed(returncode=0)

    manager = ServiceManager(runner=runner, home=tmp_path, instance="beta")
    manager.stop()
    manager.uninstall()
    manager.restart_count()

    named = [args for args in seen if any(part.endswith(".service") for part in args)]
    assert named, "no unit was ever named"
    for args in named:
        assert "telemax-beta.service" in args
        assert "telemax.service" not in args


# ------------------------------------------------------ wiping before a re-pull


async def test_a_message_that_survives_keeps_its_mapping(tmp_path: Path) -> None:
    """The safety rule of the whole operation, in one assertion.

    Telegram refuses to delete anything older than 48 hours. The mapping *is* the
    dedup key, so dropping it for a message that is still in the chat would let
    the re-import place a second copy beside it. What could not be deleted keeps
    its row, keeps deduplicating, and is counted as kept.
    """
    from bridge.service.runtime import BridgeService
    from bridge.storage import BridgeRecord, BridgeRepository, Database, MessageMapRepository

    config = tmp_path / "config.yaml"
    write_bootstrap_config(
        config,
        owner_user_id=1,
        timezone="Europe/Moscow",
        guardian_token_env="TG",
        data_dir=str(tmp_path / "data"),
    )
    service = BridgeService(load_config(config, load_env_file=False))

    database = await Database.connect(tmp_path / "bridge.db")
    try:
        messages = MessageMapRepository(database)
        bridges = BridgeRepository(database)
        await bridges.upsert(
            BridgeRecord(
                bridge_name="mom",
                max_chat_id=777,
                token_env="TELEMAX_BOT_MOM",
                telegram_bot_id=555,
            )
        )
        fresh = await messages.claim_from_max(
            bridge_name="mom",
            max_chat_id=777,
            max_message_id=1,
            telegram_bot_id=555,
            telegram_chat_id=1,
        )
        old = await messages.claim_from_max(
            bridge_name="mom",
            max_chat_id=777,
            max_message_id=2,
            telegram_bot_id=555,
            telegram_chat_id=1,
        )
        assert fresh is not None and old is not None
        await messages.attach_telegram_message(fresh, 900)
        await messages.attach_telegram_message(old, 901)
        await bridges.set_history_cursor("mom", 42)

        class TooOldToDelete:
            """A bot that refuses the second message, as Telegram does at 48 hours."""

            def __init__(self) -> None:
                self.deleted: list[int] = []

            async def __call__(self, method: Any) -> bool:
                message_id = int(method.message_id)
                if message_id == 901:
                    raise RuntimeError("message can't be deleted")
                self.deleted.append(message_id)
                return True

        bot = TooOldToDelete()

        class Live:
            def __init__(self) -> None:
                self.bot = bot

        class Registry:
            def by_max_chat(self, max_chat_id: int) -> Any:
                return Live() if max_chat_id == 777 else None

        service._registry = Registry()  # type: ignore[assignment]
        service._messages = messages
        service._bridge_rows = bridges

        gone, kept = await service.wipe_delivered(777)

        assert (gone, kept) == (1, 1)
        assert bot.deleted == [900]
        # The one that was removed can be delivered again...
        assert (
            await messages.claim_from_max(
                bridge_name="mom",
                max_chat_id=777,
                max_message_id=1,
                telegram_bot_id=555,
                telegram_chat_id=1,
            )
            is not None
        )
        # ...and the one still in the chat cannot, so no duplicate appears.
        assert (
            await messages.claim_from_max(
                bridge_name="mom",
                max_chat_id=777,
                max_message_id=2,
                telegram_bot_id=555,
                telegram_chat_id=1,
            )
            is None
        )
        assert await bridges.history_cursor("mom") is None, "the import starts over"
    finally:
        await database.close()


async def test_a_chat_the_owner_cleared_by_hand_is_re_importable(tmp_path: Path) -> None:
    """The bug this rule was missing: nothing came back after a manual wipe.

    Deleting the conversation in Telegram leaves every mapping pointing at a
    message that no longer exists. Reading Telegram's «message to delete not
    found» as «still there» kept every row, and the re-import then deduplicated
    the whole conversation away — into a chat the owner had just emptied.
    """
    from bridge.service.runtime import BridgeService
    from bridge.storage import BridgeRecord, BridgeRepository, Database, MessageMapRepository

    config = tmp_path / "config.yaml"
    write_bootstrap_config(
        config,
        owner_user_id=1,
        timezone="Europe/Moscow",
        guardian_token_env="TG",
        data_dir=str(tmp_path / "data"),
    )
    service = BridgeService(load_config(config, load_env_file=False))

    database = await Database.connect(tmp_path / "bridge.db")
    try:
        messages = MessageMapRepository(database)
        bridges = BridgeRepository(database)
        await bridges.upsert(
            BridgeRecord(
                bridge_name="dad",
                max_chat_id=777,
                token_env="TELEMAX_BOT_DAD",
                telegram_bot_id=555,
            )
        )
        for index, message_id in enumerate((900, 901, 902), start=1):
            link = await messages.claim_from_max(
                bridge_name="dad",
                max_chat_id=777,
                max_message_id=index,
                telegram_bot_id=555,
                telegram_chat_id=1,
            )
            assert link is not None
            await messages.attach_telegram_message(link, message_id)

        class EmptyChat:
            """Telegram after the owner deleted the whole conversation."""

            async def __call__(self, method: Any) -> bool:
                raise RuntimeError("Bad Request: message to delete not found")

        class Registry:
            def by_max_chat(self, max_chat_id: int) -> Any:
                return type("Live", (), {"bot": EmptyChat()})()

        service._registry = Registry()  # type: ignore[assignment]
        service._messages = messages
        service._bridge_rows = bridges

        gone, kept = await service.wipe_delivered(777)

        assert (gone, kept) == (3, 0), "already gone counts as cleared, not as kept"
        assert await messages.placed_by("dad") == [], "every mapping is released"
        assert (
            await messages.claim_from_max(
                bridge_name="dad",
                max_chat_id=777,
                max_message_id=1,
                telegram_bot_id=555,
                telegram_chat_id=1,
            )
            is not None
        ), "so the whole conversation can be delivered again"
    finally:
        await database.close()


async def test_an_undeletable_message_is_still_kept(tmp_path: Path) -> None:
    """The other wording means the opposite, and must keep protecting the chat."""
    from bridge.service.runtime import BridgeService
    from bridge.storage import BridgeRecord, BridgeRepository, Database, MessageMapRepository

    config = tmp_path / "config.yaml"
    write_bootstrap_config(
        config,
        owner_user_id=1,
        timezone="Europe/Moscow",
        guardian_token_env="TG",
        data_dir=str(tmp_path / "data"),
    )
    service = BridgeService(load_config(config, load_env_file=False))

    database = await Database.connect(tmp_path / "bridge.db")
    try:
        messages = MessageMapRepository(database)
        bridges = BridgeRepository(database)
        await bridges.upsert(
            BridgeRecord(
                bridge_name="dad",
                max_chat_id=777,
                token_env="TELEMAX_BOT_DAD",
                telegram_bot_id=555,
            )
        )
        link = await messages.claim_from_max(
            bridge_name="dad",
            max_chat_id=777,
            max_message_id=1,
            telegram_bot_id=555,
            telegram_chat_id=1,
        )
        assert link is not None
        await messages.attach_telegram_message(link, 900)

        class TooOld:
            async def __call__(self, method: Any) -> bool:
                raise RuntimeError("Bad Request: message can't be deleted for everyone")

        class Registry:
            def by_max_chat(self, max_chat_id: int) -> Any:
                return type("Live", (), {"bot": TooOld()})()

        service._registry = Registry()  # type: ignore[assignment]
        service._messages = messages
        service._bridge_rows = bridges

        assert await service.wipe_delivered(777) == (0, 1)
        assert len(await messages.placed_by("dad")) == 1, "the mapping still guards it"
    finally:
        await database.close()


def test_the_owner_session_is_supervised_even_when_it_fails_to_start() -> None:
    """The regression that stranded the owner for the life of a process.

    `watch()` used to be registered only after a successful `start()`. So a
    session that did not come up at boot was never retried — while the intake
    gate went on closing the Bot API path against it, and every message the owner
    typed was counted as "left to the MTProto session" and carried by nobody.

    Read off the source because the failing branch is the one no test drives: it
    needs a Telegram that refuses at exactly the wrong moment.
    """
    import ast
    from pathlib import Path

    runtime = Path(__file__).resolve().parent.parent / "bridge" / "service" / "runtime.py"
    tree = ast.parse(runtime.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.AsyncFunctionDef)
            or node.name != "_maybe_start_owner_session"
        ):
            continue
        body = ast.unparse(node)
        watch_at = body.index("supervisor.start('telegram-user-session'")
        start_at = body.index("await session.start()")
        assert watch_at > start_at, "the watchdog is registered after the attempt"
        # And nothing between the attempt and the registration may return early.
        between = body[start_at:watch_at]
        assert "return" not in between, (
            "a `return` between the first start and the watchdog is how the "
            "session stopped being retried:\n" + between
        )
        return
    raise AssertionError("_maybe_start_owner_session is gone; this test needs updating")
