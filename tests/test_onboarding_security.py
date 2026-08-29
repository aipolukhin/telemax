"""The guardian is an administrative panel. These are the locks on it.

Two independent ones, deliberately: the dispatcher middleware drops strangers
before any handler runs, and every handler checks again. A single layer is one
wiring mistake away from letting somebody else connect their MAX account to
this bridge.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from aiogram import Dispatcher

from bridge.observability.redaction import redact
from bridge.onboarding import screens
from bridge.onboarding.fsm import MaxOnboarding
from bridge.onboarding.router import DENIED, SECRET_NOT_DELETED, build_onboarding_router
from bridge.onboarding.state import Stage, StateStore, Step
from bridge.provisioning import GuardianContext, build_guardian_router
from bridge.telegram import OwnerOnlyMiddleware
from tests.fake_telegram import OWNER_ID, STRANGER_ID, FakeBot, make_callback, make_message

API_HASH = "0" * 32
BOT_TOKEN = "9000000003:AAF-dummy-token-value-for-tests-0000"
MAX_CODE = "45219"
MAX_2FA = "correct horse battery"
SESSION = "1BVtsOL4Bu0YWqZzY2FhZmM5ZDk4ZDNmYzk4"


@dataclass
class Recorder:
    shown: list[tuple[str, Any]] = field(default_factory=list)
    persisted: list[str] = field(default_factory=list)
    launched: int = 0
    restarts: int = 0
    status_calls: int = 0

    async def show(self, screen: tuple[str, Any]) -> None:
        self.shown.append(screen)

    async def persist(self, phone: str) -> None:
        self.persisted.append(phone)

    async def launch(self) -> None:
        self.launched += 1

    async def status_lines(self) -> list[str]:
        self.status_calls += 1
        return ["MAX подключён"]

    async def restart_bridge(self) -> bool:
        self.restarts += 1
        return True


@dataclass
class Wired:
    dispatcher: Dispatcher
    bot: FakeBot
    store: StateStore
    recorder: Recorder
    fsm: MaxOnboarding


@pytest.fixture
def wired(tmp_path: Path) -> Wired:
    store = StateStore.for_data_dir(tmp_path)
    recorder = Recorder()

    async def connect(phone: str, gateway: Any) -> None:
        await gateway.get_code(phone)
        await gateway.get_password(None)

    fsm = MaxOnboarding(
        store=store,
        show=recorder.show,
        connect=connect,
        persist=recorder.persist,
        launch=recorder.launch,
    )

    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(OwnerOnlyMiddleware(OWNER_ID))
    # The order the runtime uses: guardian first, onboarding second.
    dispatcher.include_router(build_guardian_router(GuardianContext()))
    dispatcher.include_router(
        build_onboarding_router(
            owner_user_id=OWNER_ID,
            store=store,
            onboarding=fsm,
            control=recorder,
            is_guardian=lambda _: True,
            show=recorder.show,
        )
    )
    return Wired(
        dispatcher=dispatcher, bot=FakeBot("1:aaa"), store=store, recorder=recorder, fsm=fsm
    )


async def feed(wired: Wired, update: Any) -> None:
    await wired.dispatcher.feed_update(wired.bot, update)  # type: ignore[arg-type]


def replies(wired: Wired) -> list[str]:
    return [
        str(call.kwargs.get("text", ""))
        for call in wired.bot.calls
        if call.method in {"send_message", "edit_message_text"}
    ]


# ------------------------------------------------------------------ strangers


async def test_a_stranger_cannot_start_onboarding(wired: Wired) -> None:
    await feed(wired, make_message(1, "/start sometoken", user_id=STRANGER_ID))

    assert wired.recorder.shown == [], "the FSM was never touched"
    assert wired.store.load().step is Step.IDLE
    assert wired.store.load().token_used_at is None


async def test_a_stranger_cannot_read_the_status(wired: Wired) -> None:
    await feed(wired, make_message(1, "/status", user_id=STRANGER_ID))
    assert wired.recorder.status_calls == 0


async def test_a_stranger_cannot_list_dialogs(wired: Wired) -> None:
    await feed(wired, make_message(1, "/dialogs", user_id=STRANGER_ID))

    said = " ".join(replies(wired))
    assert "MAX" not in said, "a stranger learns nothing about what this bot is"


async def test_a_stranger_cannot_restart_the_bridge(wired: Wired) -> None:
    await feed(wired, make_message(1, "/restart", user_id=STRANGER_ID))
    await feed(
        wired, make_callback(2, screens.restart_yes_callback(0), user_id=STRANGER_ID)
    )

    assert wired.recorder.restarts == 0


async def test_the_refusal_reveals_nothing(wired: Wired) -> None:
    for word in ("MAX", "Telemax", "мост", "владел"):
        assert word.lower() not in DENIED.lower()


async def test_the_owner_check_holds_without_the_middleware(tmp_path: Path) -> None:
    """Defence in depth: the handler refuses even if the middleware is missing."""
    store = StateStore.for_data_dir(tmp_path)
    recorder = Recorder()
    naked = Dispatcher()
    naked.include_router(
        build_onboarding_router(
            owner_user_id=OWNER_ID,
            store=store,
            onboarding=MaxOnboarding(
                store=store,
                show=recorder.show,
                connect=_never,
                persist=recorder.persist,
                launch=recorder.launch,
            ),
            control=recorder,
            is_guardian=lambda _: True,
            show=recorder.show,
        )
    )
    bot = FakeBot("1:aaa")

    await naked.feed_update(bot, make_message(1, "/status", user_id=STRANGER_ID))  # type: ignore[arg-type]

    assert recorder.status_calls == 0
    assert [call.kwargs.get("text") for call in bot.calls if call.method == "send_message"] == [
        DENIED
    ]


async def _never(phone: str, gateway: Any) -> None:
    raise AssertionError("MAX must not be contacted here")


# ---------------------------------------------------------------- secret hygiene


async def test_a_code_is_deleted_from_the_chat(wired: Wired) -> None:
    wired.store.update(stage=Stage.MAX_ONBOARDING_PENDING, step=Step.WAITING_FOR_CODE)

    await feed(wired, make_message(1, MAX_CODE))

    deleted = [call for call in wired.bot.calls if call.method == "delete_message"]
    assert deleted, "the message carrying the code must go"
    assert MAX_CODE not in " ".join(replies(wired)), "and it is never quoted back"


async def test_a_failed_deletion_still_does_not_repeat_the_secret(wired: Wired) -> None:
    wired.store.update(stage=Stage.MAX_ONBOARDING_PENDING, step=Step.WAITING_FOR_2FA)

    wired.bot.delete_fails = True

    await feed(wired, make_message(1, MAX_2FA))

    said = " ".join(replies(wired))
    assert MAX_2FA not in said, "a refused deletion must not become a quote"
    assert SECRET_NOT_DELETED in said


async def test_no_callback_data_carries_a_secret() -> None:
    """Callback data is echoed back by Telegram and stored in the message."""
    every = [
        screens.BEGIN,
        screens.CANCEL,
        screens.RETRY,
        screens.LAUNCH,
        screens.STATUS,
        screens.DIALOGS,
        screens.ADD_CONTACT,
        screens.RESTART,
        screens.RESTART_YES,
        screens.RESTART_NO,
    ]
    for data in every:
        assert len(data) < 64, "Telegram truncates past 64 bytes"
        assert data.count(":") <= 2
        assert not any(char.isdigit() for char in data.split(":")[-1])


async def test_the_finished_screen_holds_no_credentials() -> None:
    text, markup = screens.ready(timezone="Europe/Moscow")
    for secret in (API_HASH, BOT_TOKEN, MAX_CODE, MAX_2FA, SESSION):
        assert secret not in text
        assert secret not in str(markup)


# ------------------------------------------------------------------- redaction


@pytest.mark.parametrize(
    "line",
    [
        f"token={BOT_TOKEN}",
        f"api_hash={API_HASH}",
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        f"session={SESSION}",
        f"password={MAX_2FA.replace(' ', '')}",
        "cookie=abcdef0123456789",
        f"Authorization: Bearer {SESSION}",
        "код 45219",
        "phone +79001234567",
    ],
)
def test_secrets_do_not_survive_the_log_filter(line: str) -> None:
    cleaned = redact(line)
    for secret in (BOT_TOKEN, API_HASH, SESSION, "45219", "abcdef0123456789"):
        assert secret not in cleaned, f"{secret} survived in {cleaned!r}"


def test_the_filter_runs_on_arguments_too(caplog: Any) -> None:
    """`logger.info("token=%s", token)` is the shape this exists for."""
    from bridge.observability.redaction import RedactingFilter

    logger = logging.getLogger("bridge.test.redaction")
    logger.addFilter(RedactingFilter())
    with caplog.at_level(logging.INFO, logger="bridge.test.redaction"):
        logger.info("guardian token=%s", BOT_TOKEN)

    assert BOT_TOKEN not in caplog.text


def test_the_max_password_never_reaches_the_config(tmp_path: Path) -> None:
    """It is used to make a session and then dropped: nothing writes it down."""
    from bridge.config.writer import write_bootstrap_config

    config = tmp_path / "config.yaml"
    write_bootstrap_config(
        config, owner_user_id=OWNER_ID, timezone="Europe/Moscow", guardian_token_env="TG"
    )

    text = config.read_text(encoding="utf-8")
    assert MAX_2FA not in text
    assert "password" not in text.lower()
    assert BOT_TOKEN not in text
