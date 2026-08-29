"""Bot API is not an owner ingress, and there is no configuration that makes it one.

This file used to prove a *gate*: MTProto authoritative when a flag was true,
Bot API forwarding when it was false. The flag is gone, and so is the second
branch. The owner's Telegram is a puppet session and it is the only authority on
what the owner did — every message, edit, delete, album, reaction and shared card
reaches MAX through it.

What is proved here now is the absence: the Bot API handlers still see all of it,
because the contact bot is the chat the owner is typing into, and they carry none
of it. The two things they do keep doing are worth naming, because deleting them
along with the fallback would break real behaviour — recognising a message the
bridge itself placed on the owner's behalf, and counting the hand-off so a
message reaching neither transport shows up as a number rather than as silence.
"""

from __future__ import annotations

from typing import Any

from aiogram import Dispatcher

from bridge.routing.adapters import build_forwarding_router
from bridge.routing.upload_router import build_upload_router
from tests.fake_telegram import FakeBot, make_message


class RecordingRouter:
    """A BridgeRouter stand-in that records any TG→MAX job it is asked for."""

    def __init__(self) -> None:
        self.texts: list[dict[str, Any]] = []
        self.contacts: list[dict[str, Any]] = []
        self.placements: list[dict[str, Any]] = []

    def target_for_bot(self, bot_id: int) -> None:
        return None

    async def on_telegram_text(self, **kwargs: Any) -> int:
        self.texts.append(kwargs)
        return 1

    async def on_telegram_contact(self, **kwargs: Any) -> int:
        self.contacts.append(kwargs)
        return 1

    async def note_own_placement(self, **kwargs: Any) -> None:
        self.placements.append(kwargs)


class CountingHandOff:
    def __init__(self) -> None:
        self.count = 0

    async def __call__(self) -> None:
        self.count += 1


async def _feed(router_obj: RecordingRouter, update: Any, hand_off: Any = None) -> None:
    dispatcher = Dispatcher()
    dispatcher.include_router(
        build_forwarding_router(  # type: ignore[arg-type]
            router_obj, on_owner_intake_suppressed=hand_off
        )
    )
    await dispatcher.feed_update(FakeBot("1:aaa"), update)


# ------------------------------------------------------------------ text


async def test_an_owner_message_over_bot_api_creates_no_job() -> None:
    """There is no flag to set. The Bot API path never carries the owner's text."""
    router_obj = RecordingRouter()
    await _feed(router_obj, make_message(1, "привет"))
    assert router_obj.texts == []


async def test_the_hand_off_is_counted() -> None:
    """So "the session is carrying them" can be told from "they go nowhere"."""
    hand_off = CountingHandOff()
    await _feed(RecordingRouter(), make_message(1, "привет"), hand_off)
    assert hand_off.count == 1


async def test_a_command_is_not_counted_as_a_hand_off() -> None:
    """`/status` is the owner talking to the bridge, not to their contact."""
    hand_off = CountingHandOff()
    await _feed(RecordingRouter(), make_message(1, "/status"), hand_off)
    assert hand_off.count == 0


# ------------------------------------------------------------------ edit


async def test_an_owner_edit_over_bot_api_creates_no_job() -> None:
    """The one this removal was named for. `on_telegram_edit` no longer exists,
    so a router that grew it back would fail here on the attribute alone."""
    from aiogram.types import Update

    router_obj = RecordingRouter()
    hand_off = CountingHandOff()
    edited = make_message(2, "стало")
    await _feed(
        router_obj,
        Update(update_id=99, edited_message=edited.message),
        hand_off,
    )

    assert not hasattr(router_obj, "edits")
    assert hand_off.count == 1  # seen, counted, not carried


def test_the_router_has_no_bot_api_edit_entry() -> None:
    from bridge.routing.router import BridgeRouter

    assert not hasattr(BridgeRouter, "on_telegram_edit")
    assert not hasattr(BridgeRouter, "_bot_side_target")


# ----------------------------------------------------------------- contact


async def test_a_shared_contact_over_bot_api_creates_no_job() -> None:
    from datetime import UTC, datetime

    from aiogram.types import Chat, Contact, Message, Update, User

    router_obj = RecordingRouter()
    hand_off = CountingHandOff()
    update = Update(
        update_id=3,
        message=Message(
            message_id=3,
            date=datetime.now(tz=UTC),
            chat=Chat(id=111, type="private"),
            from_user=User(id=111, is_bot=False, first_name="Someone"),
            contact=Contact(phone_number="+70000000000", first_name="Кто-то"),
        ),
    )
    await _feed(router_obj, update, hand_off)

    assert router_obj.contacts == []
    assert hand_off.count == 1


# ------------------------------------------------------------------- media


class _FakeUploader:
    def __init__(self) -> None:
        self.handled: list[Any] = []
        self.bridge = RecordingRouter()

    async def handle(self, message: Any, item: Any) -> None:
        self.handled.append(item)


async def test_owner_media_over_bot_api_is_not_uploaded() -> None:
    from datetime import UTC, datetime

    from aiogram.types import Chat, Message, PhotoSize, Update, User

    uploader = _FakeUploader()
    hand_off = CountingHandOff()
    dispatcher = Dispatcher()
    dispatcher.include_router(
        build_upload_router(uploader, on_owner_intake_suppressed=hand_off)  # type: ignore[arg-type]
    )
    update = Update(
        update_id=2,
        message=Message(
            message_id=2,
            date=datetime.now(tz=UTC),
            chat=Chat(id=111, type="private"),
            from_user=User(id=111, is_bot=False, first_name="Someone"),
            photo=[PhotoSize(file_id="f", file_unique_id="u", width=1, height=1, file_size=10)],
        ),
    )
    await dispatcher.feed_update(FakeBot("1:aaa"), update)

    assert uploader.handled == []
    assert hand_off.count == 1


# --------------------------------------------------- what must NOT be removed


async def test_an_echo_of_our_own_placement_is_still_recognised() -> None:
    """A message the bridge put in the chat on the owner's behalf comes back
    through this bot. Recognising it is what stops the owner seeing their own
    line twice — and it is what gives a reply to it a bot-side id."""
    from bridge.routing.echo import OwnEchoes, Placed

    echoes = OwnEchoes()
    echoes.note(1, "привет", placed=Placed(max_chat_id=555, max_message_id=9001))

    router_obj = RecordingRouter()
    dispatcher = Dispatcher()
    dispatcher.include_router(
        build_forwarding_router(router_obj, own_echoes=echoes)  # type: ignore[arg-type]
    )
    await dispatcher.feed_update(FakeBot("1:aaa"), make_message(1, "привет"))

    assert router_obj.texts == []  # still not carried
    assert len(router_obj.placements) == 1  # but the bot-side id was recorded


def test_no_configuration_reopens_bot_api_owner_intake() -> None:
    """The signature itself: there is no parameter left to set."""
    import inspect

    for builder in (build_forwarding_router, build_upload_router):
        assert "owner_mtproto_intake" not in inspect.signature(builder).parameters
