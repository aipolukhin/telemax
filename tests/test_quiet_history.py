"""An import is read in silence, and the owner is shown the way back.

Two small things that only show up in use. Fifty imported messages arrived as
fifty notifications, and creating a bridge left the owner stranded in the new
bot's chat with no route to the guardian but the search box.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from aiogram.methods import GetMe, SendMessage, SendPhoto
from aiogram.types import InlineKeyboardMarkup

from bridge.telegram.app import BACK_TO_GUARDIAN, START_TEXT, build_commands_router
from bridge.telegram.quiet import is_quiet, quietly, silence_history

pytestmark = pytest.mark.asyncio


async def _passthrough(bot: Any, method: Any) -> Any:
    return method


# ------------------------------------------------------------------- silence


async def test_a_send_inside_the_block_goes_out_silently() -> None:
    method = SendMessage(chat_id=1, text="привет")
    with quietly():
        stamped = await silence_history(_passthrough, None, method)
    assert stamped.disable_notification is True


async def test_a_send_outside_the_block_is_left_alone() -> None:
    """compatibility fixtures must ping. Nothing is switched back on afterwards, so the
    only thing keeping live messages loud is that they never enter the block."""
    quiet = SendMessage(chat_id=1, text="в тишине")
    with quietly():
        await silence_history(_passthrough, None, quiet)
    loud = SendMessage(chat_id=1, text="вслух")
    await silence_history(_passthrough, None, loud)

    assert quiet.disable_notification is True
    assert loud.disable_notification is None
    assert is_quiet() is False


async def test_media_is_muted_too_and_getme_is_untouched() -> None:
    """The flag is found by field, not by listing the dozen `send_*` methods —
    which is the point: a send this file has never heard of is still muted."""
    photo = SendPhoto(chat_id=1, photo="file_id")
    me = GetMe()
    with quietly():
        await silence_history(_passthrough, None, photo)
        await silence_history(_passthrough, None, me)

    assert photo.disable_notification is True
    assert not hasattr(me, "disable_notification")


async def test_the_block_ends_even_when_the_import_fails() -> None:
    with pytest.raises(RuntimeError):
        with quietly():
            raise RuntimeError("MAX отказалось отдавать историю")
    assert is_quiet() is False


async def test_a_task_spawned_inside_the_import_inherits_the_silence() -> None:
    """What an argument would not have managed: delivery that hands off to a
    task still sends quietly, because a task copies the context it was made in."""
    seen: list[bool] = []

    async def deep() -> None:
        seen.append(is_quiet())

    with quietly():
        task = asyncio.create_task(deep())
        await task

    assert seen == [True]


async def test_live_traffic_beside_an_import_is_not_silenced() -> None:
    """The variable is per-task, so an import in one task cannot mute the
    delivery of a live message running in another."""
    started = asyncio.Event()
    release = asyncio.Event()
    live: list[bool] = []

    async def importing() -> None:
        with quietly():
            started.set()
            await release.wait()

    async def arriving() -> None:
        await started.wait()
        live.append(is_quiet())
        release.set()

    await asyncio.gather(importing(), arriving())

    assert live == [False]


# --------------------------------------------------------------- the way back


class Answered:
    """One `message.answer`, remembered."""

    def __init__(self) -> None:
        self.text: str | None = None
        self.markup: Any = None

    async def answer(self, text: str, reply_markup: Any = None, **_: Any) -> None:
        self.text = text
        self.markup = reply_markup


async def _start_of(router: Any, message: Any) -> None:
    await router.message.handlers[0].callback(message)


async def test_start_offers_a_link_back_to_the_guardian() -> None:
    router = build_commands_router(None, lambda: "guard_telemax_bot")
    message = Answered()

    await _start_of(router, message)

    assert message.text == START_TEXT
    assert isinstance(message.markup, InlineKeyboardMarkup)
    button = message.markup.inline_keyboard[0][0]
    assert button.text == BACK_TO_GUARDIAN
    assert button.url == "https://t.me/guard_telemax_bot"


async def test_an_at_sign_does_not_reach_the_url() -> None:
    router = build_commands_router(None, lambda: "@guard_telemax_bot")
    message = Answered()

    await _start_of(router, message)

    assert message.markup.inline_keyboard[0][0].url == "https://t.me/guard_telemax_bot"


@pytest.mark.parametrize("username", [None, "", "   "])
async def test_no_guardian_means_no_button_rather_than_a_broken_one(username: Any) -> None:
    router = build_commands_router(None, lambda: username)
    message = Answered()

    await _start_of(router, message)

    assert message.text == START_TEXT
    assert message.markup is None


async def test_start_still_answers_when_the_username_cannot_be_resolved() -> None:
    """The bot the owner just created is the worst place to raise."""

    def broken() -> str:
        raise RuntimeError("guardian is not up yet")

    router = build_commands_router(None, broken)
    message = Answered()

    await _start_of(router, message)

    assert message.text == START_TEXT
    assert message.markup is None


# ------------------------------------------------------- wired to the real path


async def test_the_import_puts_its_deliveries_inside_the_block() -> None:
    """The context is entered by `MaxHistorySource`, not by the caller.

    Pinned here because the block is invisible at the send site: a refactor that
    moved the loop out of `import_chat` would silently un-mute every import and
    nothing else in the suite would notice.
    """
    from bridge.max_client.events import IncomingMaxMessage
    from bridge.service.runtime import MaxHistorySource

    heard: list[bool] = []

    class Client:
        async def fetch_history(self, chat_id: int, limit: int = 0) -> Any:
            return [
                IncomingMaxMessage(
                    message_id=index,
                    chat_id=chat_id,
                    sender_id=7,
                    text=f"старое {index}",
                    timestamp=index,
                    is_outgoing=False,
                    from_history=True,
                )
                for index in (1, 2, 3)
            ]

    async def deliver(message: Any) -> None:
        heard.append(is_quiet())

    source = MaxHistorySource(client=Client(), deliver=deliver)  # type: ignore[arg-type]
    outcome = await source.import_chat(10, limit=50, after=None)

    assert outcome.delivered == 3
    assert heard == [True, True, True]
    assert is_quiet() is False


async def test_every_contact_bot_is_born_with_the_middleware() -> None:
    """Installed in the registry because that is the one place a bridge bot is
    made. A bot that missed it is the single chat that pings through an import."""
    from aiogram import Bot

    from bridge.telegram.registry import BotRegistry

    made: list[Bot] = []

    class Factory(Bot):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            made.append(self)

        async def get_me(self, **_: Any) -> Any:
            return type("Me", (), {"id": 555, "username": "contact_max_bot"})()

    from pydantic import SecretStr

    from bridge.config import ResolvedBridge

    registry = BotRegistry(object(), bot_factory=Factory)  # type: ignore[arg-type]
    bridge = ResolvedBridge(
        name="p",
        max_chat_id=10,
        token=SecretStr("555:TTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTT"),
        token_env="TELEMAX_BOT_P",
    )

    await registry.add(bridge, start=False)

    assert made, "no bot was built"
    assert silence_history in made[0].session.middleware._middlewares
    await registry.close()


async def test_the_result_screen_leads_back_to_the_dialog_list() -> None:
    """Landing on «Готово» with no route onwards is half a way back."""
    from bridge.provisioning.journal import ItemState, JournalEntry
    from bridge.provisioning.picker import OPEN
    from bridge.provisioning.selection import result_markup

    entries = [
        JournalEntry(
            max_chat_id=10,
            expected_username="p_max_bot",
            title="Папа",
            state=ItemState.HEALTHY,
        )
    ]

    markup = result_markup(entries, epoch=1, history_done=True)

    assert any(
        button.callback_data == OPEN for row in markup.inline_keyboard for button in row
    )


async def test_nothing_finished_means_no_way_onwards_offered() -> None:
    from bridge.provisioning.journal import ItemState, JournalEntry
    from bridge.provisioning.picker import OPEN
    from bridge.provisioning.selection import result_markup

    entries = [
        JournalEntry(
            max_chat_id=10,
            expected_username="p_max_bot",
            title="Папа",
            state=ItemState.FAILED_PERMANENT,
        )
    ]

    markup = result_markup(entries, epoch=1)

    assert not any(
        button.callback_data == OPEN for row in markup.inline_keyboard for button in row
    )


# ----------------------------------------------------- the way back, part two


async def _command(router: Any, index: int, message: Any) -> None:
    await router.message.handlers[index].callback(message)


async def test_guard_answers_with_the_button() -> None:
    """`/start` is not a reliable hook.

    Measured across four bridges on 2026-08-06: not one `/start` update was ever
    recorded. Telegram's managed-creation flow opens the chat with the new bot
    itself, and the owner's tap on Start never reaches the bot — so the way back
    cannot hang off it.
    """
    from bridge.telegram.app import GUARD_TEXT

    router = build_commands_router(None, lambda: "guard_telemax_bot")
    message = Answered()

    await _command(router, 1, message)

    assert message.text == GUARD_TEXT
    button = message.markup.inline_keyboard[0][0]
    assert button.text == BACK_TO_GUARDIAN
    assert button.url == "https://t.me/guard_telemax_bot"


async def test_guard_says_so_when_there_is_no_guardian() -> None:
    """A bare sentence with no button reads as the command being broken."""
    from bridge.telegram.app import NO_GUARDIAN

    router = build_commands_router(None, lambda: None)
    message = Answered()

    await _command(router, 1, message)

    assert message.text == NO_GUARDIAN
    assert message.markup is None


async def test_the_bridge_menu_names_guard_and_status() -> None:
    from bridge.telegram.app import BRIDGE_COMMANDS

    assert [name for name, _ in BRIDGE_COMMANDS] == ["guard", "status"]


async def test_every_bridge_bot_gets_its_menu_published() -> None:
    from aiogram.methods import SetChatMenuButton, SetMyCommands

    from bridge.telegram.app import publish_bridge_menu

    calls: list[str] = []

    class Bot:
        async def __call__(self, method: Any) -> Any:
            calls.append(type(method).__name__)
            return True

    await publish_bridge_menu(Bot())

    assert calls == [SetMyCommands.__name__, SetChatMenuButton.__name__]


async def test_a_menu_telegram_refuses_does_not_take_the_bridge_down() -> None:
    from bridge.telegram.app import publish_bridge_menu

    class Bot:
        async def __call__(self, method: Any) -> Any:
            raise RuntimeError("Too Many Requests: retry after 30")

    await publish_bridge_menu(Bot())  # no raise


# ------------------------------------------------------------- profile card


async def test_the_about_line_names_the_guardian() -> None:
    from bridge.provisioning.profile import SHORT_DESCRIPTION, short_description

    assert short_description("guard_telemax_bot") == (
        "Личный мост MAX ⇄ Telegram. Управление: t.me/guard_telemax_bot"
    )
    assert short_description("@guard_telemax_bot").endswith("t.me/guard_telemax_bot")
    assert short_description(None) == SHORT_DESCRIPTION
    assert short_description("  ") == SHORT_DESCRIPTION


async def test_an_about_line_that_would_not_fit_falls_back() -> None:
    """Telegram allows 120 characters. A truncated `t.me/` link is a dead link."""
    from bridge.provisioning.profile import SHORT_DESCRIPTION, short_description

    assert short_description("x" * 100) == SHORT_DESCRIPTION


async def test_the_about_line_is_part_of_the_profile_signature() -> None:
    """Otherwise changing it would reach only bots whose contact happened to
    rename themselves afterwards, and every existing bridge would keep the old
    line for ever."""
    from bridge.max_client import MaxContact
    from bridge.provisioning.profile import signature_of

    contact = MaxContact(user_id=7, display_name="Ваня")

    assert signature_of(contact, about="a") != signature_of(contact, about="b")
