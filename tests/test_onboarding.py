"""Connecting MAX from a chat: the state machine, end to end.

The MAX login is phone → SMS code → optional 2FA password. There is no
"MAX password" step: the account is identified by its number, which is why the
first question is a phone and not a login. Everything below follows the real
protocol rather than the shape a login form usually has.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from bridge.onboarding import screens
from bridge.onboarding.fsm import MaxOnboarding
from bridge.onboarding.maxauth import MaxAuthGateway
from bridge.onboarding.state import Stage, StateStore, Step

PHONE = "+79001234567"
CODE = "12345"
PASSWORD = "correct horse"


@dataclass
class FakeMax:
    """Answers like PyMax does, including its retry-on-bad-password loop."""

    needs_password: bool = False
    accept_code: str = CODE
    accept_password: str = PASSWORD
    connected: bool = False
    unreachable: bool = False
    codes_seen: list[str] = field(default_factory=list)
    passwords_seen: list[str] = field(default_factory=list)

    async def connect(self, phone: str, gateway: MaxAuthGateway) -> None:
        if self.unreachable:
            raise ConnectionError("could not connect to MAX")

        code = await gateway.get_code(phone)
        self.codes_seen.append(code)
        if code != self.accept_code:
            # What PyMax raises when the server returns no token.
            raise RuntimeError("Authentication failed: no token received")

        if self.needs_password:
            while True:
                password = await gateway.get_password("любимая книга")
                self.passwords_seen.append(password)
                if password == self.accept_password:
                    break
        self.connected = True


@dataclass
class Harness:
    fsm: MaxOnboarding
    store: StateStore
    max: FakeMax
    shown: list[tuple[str, Any]]
    persisted: list[str]
    launched: list[int]
    launch_fails: list[Exception]


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    store = StateStore.for_data_dir(tmp_path)
    fake = FakeMax()
    shown: list[tuple[str, Any]] = []
    persisted: list[str] = []
    launched: list[int] = []
    launch_fails: list[Exception] = []

    async def show(screen: tuple[str, Any]) -> None:
        shown.append(screen)

    async def persist(phone: str) -> None:
        persisted.append(phone)

    async def launch() -> None:
        if launch_fails:
            raise launch_fails.pop(0)
        launched.append(1)

    return Harness(
        fsm=MaxOnboarding(
            store=store,
            show=show,
            connect=fake.connect,
            persist=persist,
            launch=launch,
            timezone="Europe/Moscow",
        ),
        store=store,
        max=fake,
        shown=shown,
        persisted=persisted,
        launched=launched,
        launch_fails=launch_fails,
    )


async def until(condition: Any, *, timeout: float = 1.0) -> None:  # noqa: ASYNC109
    """Let the login task run until it reaches the state a test is waiting for."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("the onboarding task never got there")
        await asyncio.sleep(0.005)


def texts(harness: Harness) -> str:
    return "\n".join(text for text, _ in harness.shown)


# ------------------------------------------------------------------ happy path


async def test_a_phone_a_code_and_the_bridge_starts_itself(harness: Harness) -> None:
    """The whole point: a valid session needs no further confirmation."""
    await harness.fsm.begin()
    assert harness.store.load().step is Step.WAITING_FOR_PHONE

    await harness.fsm.submit(PHONE)
    await until(lambda: harness.fsm.step is Step.WAITING_FOR_CODE)

    await harness.fsm.submit(CODE)
    await until(lambda: harness.store.load().stage is Stage.BRIDGE_RUNNING)

    assert harness.max.connected
    assert harness.persisted == [PHONE], "the config is written without being asked"
    assert harness.launched == [1], "the bridge starts without being asked"
    assert harness.store.load().step is Step.COMPLETED
    assert "MAX подключён" in texts(harness)


async def test_the_owner_is_never_told_to_run_a_command(harness: Harness) -> None:
    await harness.fsm.begin()
    await harness.fsm.submit(PHONE)
    await until(lambda: harness.fsm.step is Step.WAITING_FOR_CODE)
    await harness.fsm.submit(CODE)
    await until(lambda: harness.store.load().stage is Stage.BRIDGE_RUNNING)

    transcript = texts(harness)
    for banned in ("validate-config", "bridge run", "python -m bridge", "терминал"):
        assert banned not in transcript


# --------------------------------------------------------------------- 2FA


async def test_the_second_factor_is_asked_for_in_the_chat(harness: Harness) -> None:
    harness.max.needs_password = True

    await harness.fsm.begin()
    await harness.fsm.submit(PHONE)
    await until(lambda: harness.fsm.step is Step.WAITING_FOR_CODE)
    await harness.fsm.submit(CODE)
    await until(lambda: harness.fsm.step is Step.WAITING_FOR_2FA)

    assert "второй фактор" in texts(harness)
    assert "любимая книга" in texts(harness), "MAX's own hint is passed through"

    await harness.fsm.submit(PASSWORD)
    await until(lambda: harness.store.load().stage is Stage.BRIDGE_RUNNING)
    assert harness.max.connected


async def test_a_wrong_password_costs_one_step_not_the_whole_login(harness: Harness) -> None:
    harness.max.needs_password = True

    await harness.fsm.begin()
    await harness.fsm.submit(PHONE)
    await until(lambda: harness.fsm.step is Step.WAITING_FOR_CODE)
    await harness.fsm.submit(CODE)
    await until(lambda: harness.fsm.step is Step.WAITING_FOR_2FA)

    await harness.fsm.submit("wrong")
    await until(lambda: len(harness.max.passwords_seen) == 1)
    await until(lambda: harness.fsm.step is Step.WAITING_FOR_2FA)

    assert "не подошёл" in texts(harness)
    assert harness.max.codes_seen == [CODE], "the code that worked is not asked for again"

    await harness.fsm.submit(PASSWORD)
    await until(lambda: harness.store.load().stage is Stage.BRIDGE_RUNNING)


# ------------------------------------------------------------------- failures


async def test_a_wrong_code_can_be_tried_again(harness: Harness) -> None:
    """MAX has no resend: a fresh code needs a fresh request, so retry restarts."""
    await harness.fsm.begin()
    await harness.fsm.submit(PHONE)
    await until(lambda: harness.fsm.step is Step.WAITING_FOR_CODE)

    await harness.fsm.submit("00000")
    await until(lambda: harness.fsm.step is Step.WAITING_FOR_PHONE)
    assert "Не удалось подключить MAX" in texts(harness)

    harness.shown.clear()
    await harness.fsm.retry()
    assert harness.shown[-1][0] == screens.ask_max_phone()[0]

    await harness.fsm.submit(PHONE)
    await until(lambda: harness.fsm.step is Step.WAITING_FOR_CODE)
    await harness.fsm.submit(CODE)
    await until(lambda: harness.store.load().stage is Stage.BRIDGE_RUNNING)


async def test_max_being_unreachable_is_one_sentence(harness: Harness) -> None:
    harness.max.unreachable = True

    await harness.fsm.begin()
    await harness.fsm.submit(PHONE)
    await until(lambda: any("Не удалось подключить" in text for text, _ in harness.shown))

    transcript = texts(harness)
    assert "MAX временно недоступен" in transcript
    assert "Traceback" not in transcript
    assert "ConnectionError" not in transcript


async def test_a_malformed_phone_does_not_reset_anything(harness: Harness) -> None:
    await harness.fsm.begin()
    await harness.fsm.submit("не телефон")

    assert harness.fsm.step is Step.WAITING_FOR_PHONE, "still on the same question"
    assert harness.store.load().stage is Stage.MAX_ONBOARDING_PENDING
    assert harness.max.codes_seen == []


async def test_a_bridge_that_will_not_start_offers_a_retry(harness: Harness) -> None:
    harness.launch_fails.append(RuntimeError("не удалось подключиться к Telegram"))

    await harness.fsm.begin()
    await harness.fsm.submit(PHONE)
    await until(lambda: harness.fsm.step is Step.WAITING_FOR_CODE)
    await harness.fsm.submit(CODE)
    await until(lambda: any("не смог запуститься" in text for text, _ in harness.shown))

    text, markup = harness.shown[-1]
    assert "MAX подключён" in text
    assert screens.LAUNCH in str(markup)
    assert harness.store.load().stage is Stage.MAX_SESSION_VALID

    await harness.fsm.retry_launch()
    await until(lambda: harness.store.load().stage is Stage.BRIDGE_RUNNING)
    assert harness.launched == [1]


# --------------------------------------------------------------- what is kept


async def test_no_credential_is_ever_written_down(harness: Harness) -> None:
    harness.max.needs_password = True

    await harness.fsm.begin()
    await harness.fsm.submit(PHONE)
    await until(lambda: harness.fsm.step is Step.WAITING_FOR_CODE)
    await harness.fsm.submit(CODE)
    await until(lambda: harness.fsm.step is Step.WAITING_FOR_2FA)
    await harness.fsm.submit(PASSWORD)
    await until(lambda: harness.store.load().stage is Stage.BRIDGE_RUNNING)

    on_disk = harness.store.path.read_text(encoding="utf-8")
    assert PASSWORD not in on_disk
    assert CODE not in on_disk
    assert PHONE not in on_disk, "the phone belongs in .env, not in the state file"


async def test_cancelling_stops_the_attempt(harness: Harness) -> None:
    await harness.fsm.begin()
    await harness.fsm.submit(PHONE)
    await until(lambda: harness.fsm.step is Step.WAITING_FOR_CODE)

    await harness.fsm.cancel()

    assert harness.fsm.step is Step.IDLE
    assert "Настройка отменена" in texts(harness)
    assert not harness.max.connected


async def test_an_interrupted_login_is_rewound_on_restart(tmp_path: Path) -> None:
    """The code belonged to an attempt that died with the process."""
    store = StateStore.for_data_dir(tmp_path)
    store.update(stage=Stage.MAX_ONBOARDING_PENDING, step=Step.WAITING_FOR_CODE)

    fresh = StateStore.for_data_dir(tmp_path)
    assert fresh.load().step is Step.WAITING_FOR_PHONE


async def test_a_finished_setup_stays_finished_on_restart(tmp_path: Path) -> None:
    store = StateStore.for_data_dir(tmp_path)
    store.update(stage=Stage.BRIDGE_RUNNING, step=Step.COMPLETED)

    fresh = StateStore.for_data_dir(tmp_path)
    assert fresh.load().stage is Stage.BRIDGE_RUNNING
    assert fresh.load().max_ready


async def test_a_damaged_state_file_does_not_stop_the_service(tmp_path: Path) -> None:
    store = StateStore.for_data_dir(tmp_path)
    store.update(stage=Stage.BRIDGE_RUNNING)
    store.path.write_text("{not json", encoding="utf-8")

    assert StateStore.for_data_dir(tmp_path).load().stage is Stage.BOOTSTRAP_CONFIGURED


# ------------------------------------- a guardian nobody has started yet


async def test_a_screen_that_cannot_be_drawn_does_not_take_the_service_down(
    tmp_path: Path,
) -> None:
    """The deadlock a fresh guardian used to be in.

    A bot may not write to somebody who has never pressed Start, and there is no
    method that says whether they have — the send is the test. So the *first*
    draw of a brand-new guardian always fails, and letting that out of `start()`
    killed the runtime: the owner could not press Start, because pressing Start
    needs a bot that is polling, and the process died before it could poll.
    Measured in production during the V2 cutover.
    """
    from bridge.onboarding.board import StatusBoard
    from bridge.onboarding.state import StateStore

    class Unstarted:
        async def send_message(self, **kwargs: object) -> object:
            raise RuntimeError("Telegram server says - Bad Request: chat not found")

        async def edit_message_text(self, **kwargs: object) -> object:
            raise RuntimeError("Bad Request: message to edit not found")

    store = StateStore.for_data_dir(tmp_path)
    board = StatusBoard(bot=Unstarted(), chat_id=100000001, store=store)

    assert await board.show(("привет", None)) is False
    assert store.load().status_message_id is None, "no anchor is remembered for a failed post"


async def test_the_anchor_is_taken_the_moment_the_chat_exists(tmp_path: Path) -> None:
    from bridge.onboarding.board import StatusBoard
    from bridge.onboarding.state import StateStore

    class Bot:
        def __init__(self) -> None:
            self.started = False

        async def send_message(self, **kwargs: object) -> object:
            if not self.started:
                raise RuntimeError("Bad Request: chat not found")
            return type("Sent", (), {"message_id": 7})()

        async def edit_message_text(self, **kwargs: object) -> object:
            raise RuntimeError("Bad Request: message to edit not found")

    bot = Bot()
    store = StateStore.for_data_dir(tmp_path)
    board = StatusBoard(bot=bot, chat_id=100000001, store=store)

    assert await board.show(("до старта", None)) is False
    bot.started = True
    assert await board.show(("после старта", None)) is True
    assert store.load().status_message_id == 7


async def test_an_anchor_from_another_bot_is_replaced_rather_than_fatal(
    tmp_path: Path,
) -> None:
    """After a cutover the stored message id belongs to the *old* guardian."""
    from bridge.onboarding.board import StatusBoard
    from bridge.onboarding.state import StateStore

    class Bot:
        async def send_message(self, **kwargs: object) -> object:
            return type("Sent", (), {"message_id": 1})()

        async def edit_message_text(self, **kwargs: object) -> object:
            raise RuntimeError("Bad Request: message to edit not found")

    store = StateStore.for_data_dir(tmp_path)
    store.update(status_chat_id=100000001, status_message_id=234)
    board = StatusBoard(bot=Bot(), chat_id=100000001, store=store)

    assert await board.show(("новый страж", None)) is True
    assert store.load().status_message_id == 1
