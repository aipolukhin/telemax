"""The two questions that moved out of the terminal: the zone and the number.

The timezone left the console because a server terminal cannot answer it — the
host is usually UTC and the owner is holding the device that knows. The phone
question is the opposite problem: the console *did* answer it, and the bot was
asking again. Both are now in the guardian chat, in that order, before MAX is
touched at all.

The number is only ever drawn masked. This message sits in a chat history for as
long as the owner keeps it, and a full phone number is not a thing to leave
lying there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from aiogram import Dispatcher

from bridge.onboarding import screens
from bridge.onboarding.board import StatusBoard
from bridge.onboarding.fsm import MaxOnboarding
from bridge.onboarding.router import build_onboarding_router
from bridge.onboarding.state import StateStore, Step
from bridge.phone import mask, normalize
from bridge.telegram import OwnerOnlyMiddleware
from tests.fake_telegram import OWNER_ID, FakeBot, make_callback, make_message

PHONE = "+79001232051"


async def until(condition: Any, *, timeout: float = 2.0) -> None:  # noqa: ASYNC109
    """Wait for a background login attempt to get somewhere. Never sleeps long."""
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


@dataclass
class Harness:
    fsm: MaxOnboarding
    store: StateStore
    shown: list[tuple[str, Any]] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    connected: list[str] = field(default_factory=list)
    timezone: str | None = None
    phone: str | None = PHONE

    def texts(self) -> str:
        return "\n".join(text for text, _ in self.shown)


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    store = StateStore.for_data_dir(tmp_path)
    made = Harness(fsm=None, store=store)  # type: ignore[arg-type]

    async def show(screen: tuple[str, Any]) -> None:
        made.shown.append(screen)

    async def write(name: str) -> None:
        made.written.append(name)
        made.timezone = name

    async def connect(phone: str, gateway: Any) -> None:
        made.connected.append(phone)

    async def nothing(*_: object, **__: object) -> None:
        return None

    made.fsm = MaxOnboarding(
        store=store,
        show=show,
        connect=connect,
        persist=nothing,
        launch=nothing,
        timezone_reader=lambda: made.timezone,
        timezone_writer=write,
        telegram_phone=lambda: made.phone,
    )
    return made


# ------------------------------------------------------------------ timezone


async def test_onboarding_begins_with_the_timezone(harness: Harness) -> None:
    await harness.fsm.offer()

    assert "Часовой пояс" in harness.texts()
    assert harness.store.load().step is Step.WAITING_FOR_TIMEZONE
    assert "Отправьте номер" not in harness.texts(), "no number until the zone is set"


async def test_a_valid_iana_zone_is_stored(harness: Harness) -> None:
    await harness.fsm.offer()
    assert await harness.fsm.set_timezone("Europe/Moscow")

    assert harness.written == ["Europe/Moscow"]


async def test_an_unknown_zone_is_refused_and_written_nowhere(harness: Harness) -> None:
    """`+3`, `MSK` and `Moscow` all used to be accepted, and all printed 1970."""
    for bad in ("UTC+3", "MSK", "Moscow", "Mars/Olympus", ""):
        assert not await harness.fsm.set_timezone(bad), bad

    assert harness.written == []


async def test_the_russian_list_is_the_fallback(harness: Harness) -> None:
    await harness.fsm.offer_timezone_list()

    rendered = str(harness.shown[-1][1])
    for city in ("Калининград", "Москва", "Владивосток", "Камчатка"):
        assert city in rendered
    assert "UTC+" in rendered, "each line carries a computed offset"
    assert "Europe/Moscow" in rendered


async def test_automatic_detection_asks_for_confirmation(
    harness: Harness, monkeypatch: Any
) -> None:
    """A detected zone is offered, never silently applied."""
    from bridge.bootstrap import timezones

    monkeypatch.setattr(timezones, "detect_system_timezone", lambda: "Asia/Yekaterinburg")
    await harness.fsm.detect_timezone()

    text, markup = harness.shown[-1]
    assert "Asia/Yekaterinburg" in text
    assert "UTC+05:00" in text
    assert harness.written == [], "nothing is stored until the owner agrees"
    assert screens.TZ_LIST in str(markup), "«Выбрать другой» is always available"


async def test_undetectable_falls_back_to_the_list(
    harness: Harness, monkeypatch: Any
) -> None:
    from bridge.bootstrap import timezones

    monkeypatch.setattr(timezones, "detect_system_timezone", lambda: None)
    await harness.fsm.detect_timezone()

    assert "Определить автоматически не вышло" in harness.shown[-1][0]
    assert screens.TZ_LIST in str(harness.shown[-1][1])


async def test_the_zone_leads_straight_into_the_phone_question(harness: Harness) -> None:
    await harness.fsm.offer()
    await harness.fsm.set_timezone("Europe/Moscow")

    assert "Он же привязан к MAX?" in harness.shown[-1][0]
    assert harness.store.load().step is Step.WAITING_FOR_PHONE_CHOICE


# --------------------------------------------------------------------- phone


async def test_the_telegram_number_is_shown_masked(harness: Harness) -> None:
    harness.timezone = "Europe/Moscow"
    await harness.fsm.begin()

    text = harness.shown[-1][0]
    assert PHONE not in text
    assert mask(PHONE) in text
    assert text.count("•") >= 6


async def test_using_the_same_number_asks_for_nothing(harness: Harness) -> None:
    harness.timezone = "Europe/Moscow"
    await harness.fsm.begin()
    assert await harness.fsm.use_telegram_phone()

    assert harness.store.load().max_phone_mode == "same"
    assert harness.fsm.step is not Step.WAITING_FOR_PHONE
    assert "Запрашиваю код" in harness.texts()


async def test_a_different_number_gets_its_own_prompt(harness: Harness) -> None:
    harness.timezone = "Europe/Moscow"
    await harness.fsm.begin()
    await harness.fsm.ask_other_phone()

    assert harness.store.load().step is Step.WAITING_FOR_PHONE
    assert harness.store.load().max_phone_mode == "other"
    assert "Отправьте номер телефона" in harness.shown[-1][0]


async def test_a_new_number_is_normalised_before_use(harness: Harness) -> None:
    harness.timezone = "Europe/Moscow"
    await harness.fsm.begin()
    await harness.fsm.ask_other_phone()
    await harness.fsm.submit("8 (999) 123-45-67")
    await until(lambda: bool(harness.connected))

    assert harness.connected == ["+79991234567"]


async def test_no_telegram_number_means_asking_outright(harness: Harness) -> None:
    harness.timezone = "Europe/Moscow"
    harness.phone = None
    await harness.fsm.begin()

    assert harness.store.load().step is Step.WAITING_FOR_PHONE
    assert "Использовать этот же номер" not in harness.texts()


async def test_the_full_number_never_reaches_a_screen_or_a_log(
    harness: Harness, caplog: Any
) -> None:
    harness.timezone = "Europe/Moscow"
    with caplog.at_level("DEBUG"):
        await harness.fsm.begin()
        await harness.fsm.use_telegram_phone()

    assert PHONE not in harness.texts()
    assert PHONE not in caplog.text
    assert PHONE.lstrip("+") not in caplog.text


def test_masking_keeps_only_enough_to_recognise() -> None:
    masked = mask(PHONE)
    assert masked.endswith("20-51")
    assert masked.startswith("+7")
    assert normalize(PHONE) not in masked


# -------------------------------------------------------------------- wiring


async def test_the_buttons_are_wired_to_the_state_machine(tmp_path: Path) -> None:
    """The router is thin, but a callback that reaches nothing is invisible."""
    store = StateStore.for_data_dir(tmp_path)
    seen: list[str] = []

    class Recorder(MaxOnboarding):
        async def detect_timezone(self) -> None:
            seen.append("detect")

        async def offer_timezone_list(self) -> None:
            seen.append("list")

        async def set_timezone(self, name: str) -> bool:
            seen.append(f"set:{name}")
            return True

        async def use_telegram_phone(self) -> bool:
            seen.append("same")
            return True

        async def ask_other_phone(self) -> None:
            seen.append("other")

    async def nothing(*_: object, **__: object) -> None:
        return None

    fsm = Recorder(store=store, show=nothing, connect=nothing, persist=nothing, launch=nothing)

    class Control:
        async def status_lines(self) -> list[str]:
            return []

        async def restart_bridge(self) -> bool:
            return True

    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(OwnerOnlyMiddleware(OWNER_ID))
    dispatcher.include_router(
        build_onboarding_router(
            owner_user_id=OWNER_ID,
            store=store,
            onboarding=fsm,
            control=Control(),  # type: ignore[arg-type]
            is_guardian=lambda _: True,
            show=nothing,
        )
    )
    bot = FakeBot("1:aaa")
    for index, data in enumerate(
        [
            screens.TZ_AUTO,
            screens.TZ_LIST,
            screens.timezone_set_callback("Europe/Moscow"),
            screens.PHONE_SAME,
            screens.PHONE_OTHER,
        ],
        start=1,
    ):
        await dispatcher.feed_update(bot, make_callback(index, data))  # type: ignore[arg-type]

    assert seen == ["detect", "list", "set:Europe/Moscow", "same", "other"]


async def test_a_stranger_cannot_set_the_timezone(tmp_path: Path) -> None:
    store = StateStore.for_data_dir(tmp_path)
    written: list[str] = []

    async def write(name: str) -> None:
        written.append(name)

    async def nothing(*_: object, **__: object) -> None:
        return None

    fsm = MaxOnboarding(
        store=store,
        show=nothing,
        connect=nothing,
        persist=nothing,
        launch=nothing,
        timezone_writer=write,
    )

    class Control:
        async def status_lines(self) -> list[str]:
            return []

        async def restart_bridge(self) -> bool:
            return True

    dispatcher = Dispatcher()
    dispatcher.include_router(
        build_onboarding_router(
            owner_user_id=OWNER_ID,
            store=store,
            onboarding=fsm,
            control=Control(),  # type: ignore[arg-type]
            is_guardian=lambda _: True,
            show=nothing,
        )
    )
    bot = FakeBot("1:aaa")
    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, make_callback(1, screens.timezone_set_callback("Europe/Moscow"), user_id=999)
    )

    assert written == []


async def test_the_owner_can_still_reach_a_command_mid_timezone(tmp_path: Path) -> None:
    """The aiogram trap again: a catch-all must be a filter, not a return."""
    store = StateStore.for_data_dir(tmp_path)
    store.update(step=Step.WAITING_FOR_TIMEZONE)

    async def nothing(*_: object, **__: object) -> None:
        return None

    fsm = MaxOnboarding(
        store=store, show=nothing, connect=nothing, persist=nothing, launch=nothing
    )

    class Control:
        async def status_lines(self) -> list[str]:
            return ["MAX  нет связи"]

        async def restart_bridge(self) -> bool:
            return True

    bot = FakeBot("1:aaa")
    board = StatusBoard(bot=bot, chat_id=OWNER_ID, store=store)
    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(OwnerOnlyMiddleware(OWNER_ID))
    dispatcher.include_router(
        build_onboarding_router(
            owner_user_id=OWNER_ID,
            store=store,
            onboarding=fsm,
            control=Control(),  # type: ignore[arg-type]
            is_guardian=lambda _: True,
            show=board.show,
        )
    )
    await dispatcher.feed_update(bot, make_message(1, "/status"))  # type: ignore[arg-type]

    assert any("Диагностика" in str(call.kwargs.get("text", "")) for call in bot.calls)


async def test_the_zone_can_be_changed_after_onboarding(harness: Harness) -> None:
    """Onboarding asks once; a laptop that moves country needs to ask again."""
    from bridge.onboarding.state import Stage

    harness.timezone = "Europe/Moscow"
    harness.store.update(stage=Stage.BRIDGE_RUNNING)

    assert await harness.fsm.set_timezone("Asia/Yekaterinburg")

    assert harness.written == ["Asia/Yekaterinburg"]
    # And it lands back on the ready screen rather than restarting onboarding.
    assert "MAX подключён" in harness.shown[-1][0]
    assert "Использовать этот же номер" not in harness.texts()


def test_settings_offers_a_way_back_to_the_question() -> None:
    """It left the home screen. A timezone is a setting, not a health report."""
    _, markup = screens.settings(mirror_own=False, timezone="Москва · UTC+03:00")
    assert screens.SETTINGS_TZ in str(markup)


def test_the_timezone_list_from_settings_has_a_way_out() -> None:
    """Reached from home it had none: the only exits were picking a city —
    which rewrites the config and restarts the worker — or typing `/menu`."""
    _, markup = screens.timezone_list(
        [("Москва · UTC+03:00", "Europe/Moscow")],
        back=screens.SETTINGS,
        current="Москва · UTC+03:00",
        callback=screens.settings_timezone_callback,
    )

    assert screens.SETTINGS in str(markup)

    _, onboarding = screens.timezone_list([("Москва · UTC+03:00", "Europe/Moscow")])
    assert screens.SETTINGS not in str(onboarding), "the first question has nothing behind it"


async def test_a_running_bridge_with_no_zone_is_still_asked(harness: Harness) -> None:
    """An unset zone is an unanswered question, whatever the stage says."""
    from bridge.onboarding.state import Stage

    harness.timezone = None
    harness.store.update(stage=Stage.BRIDGE_RUNNING)

    await harness.fsm.offer()

    assert "Часовой пояс" in harness.shown[-1][0]
    assert "Telemax запущен" not in harness.texts()


async def test_answering_it_returns_to_the_running_screen(harness: Harness) -> None:
    """Not into the phone question: MAX is already connected."""
    from bridge.onboarding.state import Stage

    harness.timezone = None
    harness.store.update(stage=Stage.BRIDGE_RUNNING)
    await harness.fsm.offer()

    await harness.fsm.set_timezone("Europe/Moscow")

    assert "MAX подключён" in harness.shown[-1][0]
    assert "Использовать этот же номер" not in harness.texts()


# ------------------------------------------------------------- how it reads


def test_the_city_leads_and_the_iana_name_follows() -> None:
    """`Europe/Moscow` is a value the config needs, not an answer anybody asked for."""
    from bridge.bootstrap.timezones import city_of, human_label

    assert city_of("Europe/Moscow") == "Москва"
    assert human_label("Europe/Moscow") == "Москва · UTC+03:00"
    # Outside the table the last segment is still a place, not a mystery.
    assert city_of("Asia/Novosibirsk") == "Novosibirsk"


def test_the_confirmation_screen_shows_the_city_in_bold() -> None:
    text, _ = screens.confirm_timezone("Europe/Moscow", "UTC+03:00", city="Москва")

    assert "<b>Москва · UTC+03:00</b>" in text
    # Still there, and still quieter than the answer.
    assert "<i>Europe/Moscow</i>" in text
    assert text.index("Москва · UTC") < text.index("Europe/Moscow")


def test_the_list_offers_cities_with_their_offsets() -> None:
    from bridge.bootstrap.timezones import RUSSIAN_TIMEZONES, offset_label

    choices = [(f"{city} · {offset_label(name)}", name) for city, name in RUSSIAN_TIMEZONES]
    _, markup = screens.timezone_list(choices)

    rendered = str(markup)
    assert "Москва · UTC+03:00" in rendered
    assert "Владивосток" in rendered
