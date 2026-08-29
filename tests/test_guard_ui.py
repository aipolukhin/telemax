"""What the guardian chat looks like: one message, buttons, no spam.

A status line delivered as a new message every few seconds is worse than none:
the owner scrolls, loses the keyboard, and taps a button that belongs to a list
built ten minutes ago. Both halves of that are tested here — the editing, and
the refusal of the stale tap.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from aiogram import Dispatcher

from bridge.onboarding import screens
from bridge.onboarding.board import StatusBoard
from bridge.onboarding.fsm import MaxOnboarding
from bridge.onboarding.router import RESTART_FAILED, RESTARTED, build_onboarding_router
from bridge.onboarding.state import StateStore
from bridge.onboarding.views import AttentionFacts, home_view
from bridge.provisioning import DialogFlow, GuardianContext, build_guardian_router
from bridge.provisioning import selection as ui
from bridge.provisioning.journal import ProvisioningJournal
from bridge.provisioning.naming_v2 import contact_bot_username_v2
from bridge.provisioning.picker import DialogOption
from bridge.telegram import OwnerOnlyMiddleware
from tests.fake_provisioning import (
    FakeBridgeRepository,
    FakeDialogPicker,
    FakeGateway,
    FakeProvisioner,
)
from tests.fake_telegram import OWNER_ID, FakeBot, make_callback, make_message

#: The owner's Telegram id is half of every V2 username; the contact's
#: MAX id is the other half. No secret is involved any more.
OWNER = 100000001


def options(count: int) -> list[DialogOption]:
    return [
        DialogOption(
            max_chat_id=1000 + index,
            title=f"Контакт {index}",
            last_activity=index,
            max_user_id=2000 + index,
        )
        for index in range(count)
    ]


# ------------------------------------------------------------------ one message


async def test_the_status_is_edited_not_re_sent(tmp_path: Path) -> None:
    store = StateStore.for_data_dir(tmp_path)
    bot = FakeBot("1:aaa")
    board = StatusBoard(bot=bot, chat_id=OWNER_ID, store=store)

    await board.show(screens.welcome())
    await board.show(screens.ask_max_phone())
    await board.show(screens.validating())

    assert len(bot.method_calls("send_message")) == 1, "one message for the whole flow"
    assert len(bot.method_calls("edit_message_text")) == 2
    assert store.load().status_message_id is not None


async def test_redrawing_the_same_screen_costs_nothing(tmp_path: Path) -> None:
    """Telegram answers an unchanged edit with an error; do not ask for one."""
    store = StateStore.for_data_dir(tmp_path)
    bot = FakeBot("1:aaa")
    board = StatusBoard(bot=bot, chat_id=OWNER_ID, store=store)

    await board.show(screens.welcome())
    await board.show(screens.ask_max_phone())
    before = len(bot.calls)
    await board.show(screens.ask_max_phone())

    assert len(bot.calls) == before


async def test_a_message_too_old_to_edit_is_replaced(tmp_path: Path) -> None:
    """A bot may edit its own message for 48 hours, and no longer."""
    store = StateStore.for_data_dir(tmp_path)
    bot = FakeBot("1:aaa")
    board = StatusBoard(bot=bot, chat_id=OWNER_ID, store=store)

    await board.show(screens.welcome())
    bot.edit_fails = True
    await board.show(screens.ask_max_phone())

    assert len(bot.method_calls("send_message")) == 2, "it posts a new one rather than go silent"


async def test_the_status_message_survives_a_restart(tmp_path: Path) -> None:
    store = StateStore.for_data_dir(tmp_path)
    bot = FakeBot("1:aaa")
    await StatusBoard(bot=bot, chat_id=OWNER_ID, store=store).show(screens.welcome())

    fresh_store = StateStore.for_data_dir(tmp_path)
    fresh_bot = FakeBot("1:aaa")
    await StatusBoard(bot=fresh_bot, chat_id=OWNER_ID, store=fresh_store).show(
        screens.ask_max_phone()
    )

    assert fresh_bot.method_calls("edit_message_text"), "it edits, not posts a second keyboard"


# --------------------------------------------------------------------- dialogs


@dataclass
class Wired:
    dispatcher: Dispatcher
    bot: FakeBot
    context: GuardianContext
    flow: DialogFlow
    provisioner: FakeProvisioner
    gateway: FakeGateway
    board: StatusBoard
    store: StateStore


def wire(tmp_path: Path, count: int = 14, **kwargs: Any) -> Wired:
    provisioner = FakeProvisioner(**kwargs)
    gateway = FakeGateway()
    flow = DialogFlow(
        picker=FakeDialogPicker(options(count)),  # type: ignore[arg-type]
        provisioner=provisioner,
        gateway=gateway,
        journal=ProvisioningJournal.for_data_dir(tmp_path),
        bridges=FakeBridgeRepository(),
        telegram_owner_user_id=OWNER,
    )
    bot = FakeBot("1:aaa")
    store = StateStore.for_data_dir(tmp_path)
    board = StatusBoard(bot=bot, chat_id=OWNER_ID, store=store)
    # The picker draws into the guardian's one message, exactly as it does in
    # the runtime: a test that let it post its own would not notice the day it
    # started leaving a second keyboard behind.
    context = GuardianContext(flow=flow, draw=board.draw)
    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(OwnerOnlyMiddleware(OWNER_ID))
    dispatcher.include_router(build_guardian_router(context))
    return Wired(
        dispatcher=dispatcher,
        bot=bot,
        context=context,
        flow=flow,
        provisioner=provisioner,
        gateway=gateway,
        board=board,
        store=store,
    )


@pytest.fixture
def dialogs(tmp_path: Path) -> Wired:
    return wire(tmp_path)


async def feed(wired: Wired, update: Any) -> None:
    await wired.dispatcher.feed_update(wired.bot, update)  # type: ignore[arg-type]


def last_markup(wired: Wired) -> str:
    for call in reversed(wired.bot.calls):
        if "reply_markup" in call.kwargs:
            return str(call.kwargs["reply_markup"])
    raise AssertionError("no keyboard was sent")


def last_text(wired: Wired) -> str:
    for call in reversed(wired.bot.calls):
        if "text" in call.kwargs:
            return str(call.kwargs["text"])
    raise AssertionError("nothing was said")


def alerts(wired: Wired) -> list[str]:
    return [
        str(call.kwargs.get("text") or "")
        for call in wired.bot.calls
        if call.method == "answer_callback_query"
    ]


async def test_dialogs_are_offered_as_buttons(dialogs: Wired) -> None:
    await feed(dialogs, make_message(1, "/dialogs"))

    markup = last_markup(dialogs)
    assert "Контакт 0" in markup
    assert f"{ui.SELECT}:" in markup
    assert "Создать · 0" in markup


async def test_the_capacity_is_quiet_while_there_is_room(dialogs: Wired) -> None:
    """Five lines of accounting used to sit above a list of people's names.

    The numbers are unchanged and still exact — they appear when they change
    what the next tap can do, which is when the room is nearly gone.
    """
    await feed(dialogs, make_message(1, "/dialogs"))

    text = last_text(dialogs)
    assert "Можно выбрать несколько." in text
    assert "Боты Telegram" not in text
    assert "Свободно новых слотов" not in text
    assert ui.NOT_THE_WHOLE_ACCOUNT not in text


async def test_selecting_a_dialog_ticks_it_and_counts_it(dialogs: Wired) -> None:
    await feed(dialogs, make_message(1, "/dialogs"))
    await feed(dialogs, make_callback(2, _first_select(last_markup(dialogs))))

    markup = last_markup(dialogs)
    assert "✅ Контакт 0" in markup
    assert "Создать · 1" in markup
    assert "Выбрано: 1" in last_text(dialogs)


async def test_the_list_is_paginated_and_keeps_the_selection(dialogs: Wired) -> None:
    await feed(dialogs, make_message(1, "/dialogs"))
    first = last_markup(dialogs)
    assert "1/3" in first, "14 dialogs at six a page"
    assert "Контакт 6" not in first

    await feed(dialogs, make_callback(2, _first_select(first)))
    epoch = dialogs.flow.selection.epoch
    await feed(dialogs, make_callback(3, ui.page_callback(1, epoch=epoch)))

    assert "Контакт 6" in last_markup(dialogs)
    assert dialogs.flow.selection.chosen == {1000}, "turning the page keeps the choice"
    assert "Создать · 1" in last_markup(dialogs)


async def test_a_stale_button_is_refused(dialogs: Wired) -> None:
    await feed(dialogs, make_message(1, "/dialogs"))
    stale = _first_select(last_markup(dialogs))

    # The owner reopens the list: everything drawn before now belongs to an
    # older generation.
    await feed(dialogs, make_message(2, "/dialogs"))
    await feed(dialogs, make_callback(3, stale))

    assert ui.STALE_ALERT in alerts(dialogs)
    assert dialogs.flow.selection.chosen == set(), "a stale tap acts on nothing"


async def test_a_replacement_costs_no_free_slot(tmp_path: Path) -> None:
    """The whole point of counting net cost: an existing bot is not a new one."""
    wired = wire(tmp_path, count=3)
    username = contact_bot_username_v2(OWNER, 2000)
    wired.provisioner.owned[username] = 77

    await feed(wired, make_message(1, "/dialogs"))
    await feed(wired, make_callback(2, _select_for(last_markup(wired), 1000)))

    # A replacement costs no free slot, so it does not move the "выбрано" count
    # that guards the limit. The rule is unchanged; only the header is quieter.
    assert wired.flow.selection.priced().selected_new_count == 0
    assert wired.flow.selection.priced().replacement_count == 1


async def test_more_new_bots_than_slots_cannot_be_selected(tmp_path: Path) -> None:
    wired = wire(tmp_path, count=3, limit=2, strangers=1)

    await feed(wired, make_message(1, "/dialogs"))
    await feed(wired, make_callback(2, _select_for(last_markup(wired), 1000)))
    await feed(wired, make_callback(3, _select_for(last_markup(wired), 1001)))

    assert wired.flow.selection.chosen == {1000}, "the refusal changes nothing"
    assert ui.NO_SLOTS_ALERT in alerts(wired)


async def test_at_the_limit_only_replacements_are_offered(tmp_path: Path) -> None:
    wired = wire(tmp_path, count=2, limit=1, strangers=1)
    username = contact_bot_username_v2(OWNER, 2000)
    wired.provisioner.owned[username] = 77
    wired.provisioner.limit = 2  # one stranger + one Telemax bot: full

    await feed(wired, make_message(1, "/dialogs"))
    assert "Достигнут лимит Telegram-ботов." in last_text(wired)

    await feed(wired, make_callback(2, _select_for(last_markup(wired), 1000)))
    assert wired.flow.selection.chosen == {1000}, "a rebuild is always allowed"

    await feed(wired, make_callback(3, _select_for(last_markup(wired), 1001)))
    assert wired.flow.selection.chosen == {1000}
    assert ui.NO_SLOTS_ALERT in alerts(wired)


async def test_a_foreign_username_cannot_be_selected(tmp_path: Path) -> None:
    wired = wire(tmp_path, count=2)
    wired.provisioner.foreign.add(contact_bot_username_v2(OWNER, 2000))

    await feed(wired, make_message(1, "/dialogs"))
    await feed(wired, make_callback(2, _select_for(last_markup(wired), 1000)))

    assert wired.flow.selection.chosen == set()
    # «Детерминированный username занят другим Telegram-аккаунтом» was a
    # developer describing their own naming scheme at somebody who wanted to
    # message their father.
    assert any("уже занято другим аккаунтом Telegram" in alert for alert in alerts(wired))
    assert wired.provisioner.deleted == [], "nothing of somebody else's is touched"


async def test_an_unknown_limit_refuses_new_bots_but_allows_rebuilds(
    tmp_path: Path,
) -> None:
    """Telegram not publishing a number is an answer, not a licence to guess."""
    wired = wire(tmp_path, count=2, limit=None)
    wired.provisioner.owned[contact_bot_username_v2(OWNER, 2001)] = 77

    await feed(wired, make_message(1, "/dialogs"))
    assert "Лимит Telegram неизвестен" in last_text(wired)

    await feed(wired, make_callback(2, _select_for(last_markup(wired), 1000)))
    assert wired.flow.selection.chosen == set()
    assert ui.UNKNOWN_LIMIT_ALERT in alerts(wired)

    await feed(wired, make_callback(3, _select_for(last_markup(wired), 1001)))
    assert wired.flow.selection.chosen == {1001}, "a rebuild needs no free slot"


def test_pagination_arithmetic() -> None:
    assert ui.page_count(0) == 1
    assert ui.page_count(6) == 1
    assert ui.page_count(7) == 2
    assert ui.page_count(14) == 3


def _first_select(markup: str) -> str:
    import re

    found = re.search(rf"{ui.SELECT}:\d+:\d+", markup)
    assert found, markup
    return found.group(0)


def _select_for(markup: str, max_chat_id: int) -> str:
    import re

    found = re.search(rf"{ui.SELECT}:(\d+):{max_chat_id}\b", markup)
    assert found, markup
    return found.group(0)


async def test_a_second_start_edits_the_anchor_rather_than_adding_one(
    control_chat: ControlChat,
) -> None:
    """The owner reopening the chat must not leave two live keyboards behind."""
    await control_chat.feed(make_message(1, "/start"))
    await control_chat.feed(make_message(2, "/start"))

    assert len(control_chat.bot.method_calls("send_message")) == 1


async def test_every_callback_gets_an_answer(control_chat: ControlChat) -> None:
    """An unanswered callback leaves Telegram's spinner turning on the button."""
    taps = [
        screens.MENU,
        screens.STATUS,
        screens.BRIDGES,
        screens.RESTART,
        screens.RESTART_NO,
    ]
    for index, data in enumerate(taps, start=1):
        await control_chat.feed(make_callback(index, data))

    answered = [call for call in control_chat.bot.calls if call.method == "answer_callback_query"]
    assert len(answered) == len(taps)


async def test_the_same_text_under_a_new_keyboard_is_still_a_new_screen(
    tmp_path: Path,
) -> None:
    """Dedupe has to compare the buttons too, or a screen silently loses them."""
    store = StateStore.for_data_dir(tmp_path)
    bot = FakeBot("1:aaa")
    board = StatusBoard(bot=bot, chat_id=OWNER_ID, store=store)
    view = home_view(AttentionFacts(worker_running=True, max_connected=True))

    await board.show(screens.home(view))
    text, markup = screens.home(view)
    await board.show((text, None))

    assert len(bot.method_calls("edit_message_text")) == 1
    assert markup is not None


async def test_provisioning_walks_one_message_from_list_to_result(
    dialogs: Wired,
) -> None:
    """The picker becomes the progress display becomes the result. One message."""
    await feed(dialogs, make_message(1, "/dialogs"))
    await feed(dialogs, make_callback(2, _first_select(last_markup(dialogs))))
    epoch = dialogs.flow.selection.epoch
    await feed(dialogs, make_callback(3, ui.go_callback(epoch=epoch)))

    assert len(dialogs.bot.method_calls("send_message")) == 1, "one message, edited throughout"
    assert "Готово 🎉" in last_text(dialogs), "the result replaces the progress screen"


# -------------------------------------------------------------------- control


@dataclass
class FakeControl:
    restarts: int = 0
    fail: bool = False

    async def status_lines(self) -> list[str]:
        return ["Telegram   подключён", "MAX        подключён", "Мост       работает"]

    async def restart_bridge(self) -> bool:
        self.restarts += 1
        return not self.fail


@dataclass
class ControlChat:
    dispatcher: Dispatcher
    bot: FakeBot
    control: FakeControl
    store: StateStore

    async def feed(self, update: Any) -> None:
        await self.dispatcher.feed_update(self.bot, update)  # type: ignore[arg-type]

    def texts(self) -> list[str]:
        return [
            str(call.kwargs["text"])
            for call in self.bot.calls
            if call.method in {"send_message", "edit_message_text"}
        ]

    def said(self) -> str:
        return "\n".join(str(call.kwargs.get("text", "")) for call in self.bot.calls)

    def toasts(self) -> list[str]:
        return [
            str(call.kwargs.get("text") or "")
            for call in self.bot.calls
            if call.method == "answer_callback_query"
        ]

    @property
    def revision(self) -> int:
        return self.store.load().screen_revision


@pytest.fixture
def control_chat(tmp_path: Path) -> ControlChat:
    store = StateStore.for_data_dir(tmp_path)
    control = FakeControl()
    bot = FakeBot("1:aaa")
    board = StatusBoard(bot=bot, chat_id=OWNER_ID, store=store)

    async def nothing(*_: object, **__: object) -> None:
        return None

    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(OwnerOnlyMiddleware(OWNER_ID))
    dispatcher.include_router(
        build_onboarding_router(
            owner_user_id=OWNER_ID,
            store=store,
            onboarding=MaxOnboarding(
                store=store, show=board.show, connect=nothing, persist=nothing, launch=nothing
            ),
            control=control,
            is_guardian=lambda _: True,
            show=board.show,
        )
    )
    return ControlChat(dispatcher=dispatcher, bot=bot, control=control, store=store)


async def test_a_command_still_reaches_the_guardian_mid_onboarding(tmp_path: Path) -> None:
    """The aiogram trap: a handler that runs stops the routing.

    The onboarding router has a catch-all for text, and `/dialogs` is text. If
    that catch-all is a handler with an early return instead of a filter, the
    guardian's commands stop working the moment onboarding is waiting for an
    answer — silently, with a clean log.
    """
    from bridge.onboarding.state import Stage, Step

    store = StateStore.for_data_dir(tmp_path)
    store.update(stage=Stage.MAX_ONBOARDING_PENDING, step=Step.WAITING_FOR_PHONE)
    wired = wire(tmp_path, count=3)

    async def nothing(*_: object, **__: object) -> None:
        return None

    dispatcher = wired.dispatcher
    dispatcher.include_router(
        build_onboarding_router(
            owner_user_id=OWNER_ID,
            store=store,
            onboarding=MaxOnboarding(
                store=store,
                show=wired.board.show,
                connect=nothing,
                persist=nothing,
                launch=nothing,
            ),
            control=FakeControl(),
            is_guardian=lambda _: True,
            show=wired.board.show,
        )
    )

    bot = wired.bot
    await dispatcher.feed_update(bot, make_message(1, "/dialogs"))  # type: ignore[arg-type]

    markups = [call.kwargs["reply_markup"] for call in bot.calls if "reply_markup" in call.kwargs]
    assert markups, "/dialogs was swallowed by the onboarding catch-all"
    assert f"{ui.SELECT}:" in str(markups[-1])


async def test_the_control_surface_never_posts_a_second_message(
    control_chat: ControlChat,
) -> None:
    """Status, menu, restart, back: four screens, one message, no scroll."""
    await control_chat.feed(make_message(1, "/status"))
    await control_chat.feed(make_callback(2, screens.MENU))
    await control_chat.feed(make_callback(3, screens.STATUS))
    await control_chat.feed(make_callback(4, screens.MENU))

    assert len(control_chat.bot.method_calls("send_message")) == 1
    assert control_chat.bot.method_calls("edit_message_text"), "the rest are edits"


async def test_a_command_does_not_stay_in_the_history(control_chat: ControlChat) -> None:
    """`/status` has said what it had to say the moment it is handled."""
    await control_chat.feed(make_message(1, "/status"))

    assert control_chat.bot.method_calls("delete_message"), "the command is cleaned up"


async def test_status_opens_diagnostics_and_the_block_is_one_tap_in(
    control_chat: ControlChat,
) -> None:
    """`/status` was thirty conditional lines of telemetry drawn at a person.

    The block is unchanged and still reachable; what the command hands back
    first is four lights and the way into each of them.
    """
    await control_chat.feed(make_message(1, "/status"))

    landing = control_chat.texts()[0]
    assert "<b>Диагностика</b>" in landing
    assert "Мост       работает" not in landing

    await control_chat.feed(make_callback(2, screens.STATUS))

    text = control_chat.texts()[-1]
    assert "<b>Технические данные</b>" in text
    assert "Мост       работает" in text, "the old block, kept whole"
    for banned in ("|", "╭", "─", "```"):
        assert banned not in text, "box drawing and tables wrap into rubble on a phone"


async def test_restart_asks_first(control_chat: ControlChat) -> None:
    await control_chat.feed(make_message(1, "/restart"))

    assert control_chat.control.restarts == 0, "asking is not doing"
    markup = str(control_chat.bot.method_calls("send_message")[0]["reply_markup"])
    assert screens.RESTART_YES in markup
    assert screens.RESTART_NO in markup


async def test_restart_happens_once_when_confirmed(control_chat: ControlChat) -> None:
    await control_chat.feed(make_message(1, "/restart"))
    await control_chat.feed(
        make_callback(2, screens.restart_yes_callback(control_chat.revision))
    )

    assert control_chat.control.restarts == 1
    assert RESTARTED in control_chat.toasts()
    # The outcome is a toast and the screen underneath is home again: nothing
    # about the restart is left sitting in the chat.
    assert RESTARTED not in control_chat.said().replace(RESTARTED, "", 1)
    assert len(control_chat.bot.method_calls("send_message")) == 1


async def test_the_same_confirmation_cannot_restart_twice(
    control_chat: ControlChat,
) -> None:
    """The anchor is edited in place, so a stale button looks exactly like a live one."""
    await control_chat.feed(make_message(1, "/restart"))
    stale = screens.restart_yes_callback(control_chat.revision)

    await control_chat.feed(make_callback(2, stale))
    await control_chat.feed(make_callback(3, stale))

    assert control_chat.control.restarts == 1
    assert screens.STALE_SCREEN in control_chat.toasts()


async def test_a_declined_restart_does_nothing(control_chat: ControlChat) -> None:
    await control_chat.feed(make_callback(1, screens.RESTART_NO))

    assert control_chat.control.restarts == 0


async def test_a_failed_restart_is_one_sentence(control_chat: ControlChat) -> None:
    control_chat.control.fail = True
    await control_chat.feed(make_message(1, "/restart"))
    await control_chat.feed(
        make_callback(2, screens.restart_yes_callback(control_chat.revision))
    )

    said = control_chat.said()
    assert RESTART_FAILED in said
    assert "Traceback" not in said
    assert "Exception" not in said


def test_no_screen_ever_shows_a_traceback() -> None:
    """Whatever went wrong, the owner gets a sentence and a button."""
    every = [
        screens.welcome(),
        screens.ask_max_phone(),
        screens.ask_code(),
        screens.ask_password(again=True, hint="подсказка"),
        screens.validating(),
        screens.saving(),
        screens.starting(),
        screens.ready(timezone="Europe/Moscow"),
        screens.login_failed("MAX временно недоступен."),
        screens.launch_failed("не удалось подключиться к Telegram"),
        screens.cancelled(),
        screens.resume_offer(),
        screens.handoff_invite(),
        screens.restart_confirm(1),
        screens.restarting(),
        screens.home(home_view(AttentionFacts(worker_running=True, max_connected=True))),
        screens.technical_details(["MAX подключён"]),
        screens.settings(mirror_own=False, timezone="Москва · UTC+03:00"),
        screens.own_messages(mirror_own=True),
        screens.diagnostics(
            telegram=True, max_connected=True, delivery=True, database=True
        ),
        screens.problems_screen([]),
    ]
    for text, _ in every:
        assert "Traceback" not in text
        assert "  File \"" not in text
        assert len(text) < 900, "a screen has to fit on a phone without scrolling"


async def test_the_history_tick_lives_in_the_picker(dialogs: Wired) -> None:
    """Asked before the run, in the same keyboard as the choice it belongs to."""
    await feed(dialogs, make_message(1, "/dialogs"))
    assert ui.HISTORY_OFF in last_markup(dialogs)

    epoch = dialogs.flow.selection.epoch
    await feed(dialogs, make_callback(2, ui.history_pick_callback(epoch=epoch)))

    assert ui.HISTORY_ON in last_markup(dialogs)
    assert dialogs.flow.selection.history

    await feed(dialogs, make_callback(3, ui.history_pick_callback(epoch=epoch)))
    assert not dialogs.flow.selection.history


async def test_a_stale_history_tick_changes_nothing(dialogs: Wired) -> None:
    await feed(dialogs, make_message(1, "/dialogs"))
    stale = ui.history_pick_callback(epoch=dialogs.flow.selection.epoch)
    await feed(dialogs, make_message(2, "/dialogs"))

    await feed(dialogs, make_callback(3, stale))

    assert not dialogs.flow.selection.history
    assert ui.STALE_ALERT in alerts(dialogs)


async def test_the_result_tells_the_owner_to_press_start(dialogs: Wired) -> None:
    """The one step no bot can take: a bot may not open the conversation.

    `managed` is the shipped mode and it has no user session to press Start
    with, so this is what every real run ends on.
    """
    from bridge.provisioning.owned import OwnerMustOpenChatError, start_link

    async def refuse(username: str, bot_id: int | None = None) -> None:
        raise OwnerMustOpenChatError(username, start_link(username))

    dialogs.provisioner.send_start = refuse  # type: ignore[method-assign]

    await feed(dialogs, make_message(1, "/dialogs"))
    await feed(dialogs, make_callback(2, _first_select(last_markup(dialogs))))
    epoch = dialogs.flow.selection.epoch
    await feed(dialogs, make_callback(3, ui.go_callback(epoch=epoch)))

    text = last_text(dialogs)
    assert "Готово 🎉" in text
    assert "нажмите в нём <b>Старт</b>" in text
    assert "?start=" in last_markup(dialogs), "the link opens on the Start button"


async def test_a_session_that_can_press_start_says_nothing_about_it(
    dialogs: Wired,
) -> None:
    """The instruction is for the case that needs it, not decoration."""
    await feed(dialogs, make_message(1, "/dialogs"))
    await feed(dialogs, make_callback(2, _first_select(last_markup(dialogs))))
    epoch = dialogs.flow.selection.epoch
    await feed(dialogs, make_callback(3, ui.go_callback(epoch=epoch)))

    assert "Старт" not in last_text(dialogs)
