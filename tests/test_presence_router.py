"""The Telegram handlers left in the presence router: `/read`, and nothing else.

Reactions used to enter here two ways — the `message_reaction` update and a
`/react` picker. Both are gone. The owner's reactions arrive on their puppet
session like everything else they do in Telegram, and a contact bot that saw one
now carries nothing at all.

The absence is asserted, not assumed: the handlers are fed exactly the updates
that used to work and the router is checked for producing no effect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aiogram import Dispatcher
from aiogram.types import (
    Chat,
    Message,
    MessageReactionUpdated,
    ReactionTypeEmoji,
    Update,
    User,
)

from bridge.routing.presence_router import build_presence_router
from bridge.routing.router import BridgeTarget
from bridge.telegram import build_dispatcher
from tests.fake_telegram import FakeBot

OWNER = 111
BOT_ID = 100
MOM = BridgeTarget(name="mom", max_chat_id=777, bot_id=BOT_ID)


@dataclass(slots=True)
class FakeAutoRead:
    marked: list[str] = field(default_factory=list)
    result: bool = True

    async def mark_now(self, bridge_name: str) -> bool:
        self.marked.append(bridge_name)
        return self.result


def build(auto_read: Any) -> Dispatcher:
    dispatcher = build_dispatcher(owner_user_id=OWNER)
    dispatcher.include_router(
        build_presence_router(
            auto_read=auto_read, lookup=lambda bot_id: MOM if bot_id == BOT_ID else None
        )
    )
    return dispatcher


def message(text: str, update_id: int = 1, *, reply_to_id: int | None = None) -> Update:
    user = User(id=OWNER, is_bot=False, first_name="Owner")
    chat = Chat(id=OWNER, type="private")
    reply = (
        Message(
            message_id=reply_to_id, date=datetime.now(tz=UTC), chat=chat, from_user=user, text="x"
        )
        if reply_to_id
        else None
    )
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(tz=UTC),
            chat=chat,
            from_user=user,
            text=text,
            reply_to_message=reply,
        ),
    )


async def test_read_command_marks_the_chat() -> None:
    auto_read = FakeAutoRead()
    dispatcher = build(auto_read)
    bot = FakeBot("1:aaa", bot_id=BOT_ID)

    await dispatcher.feed_update(bot, message("/read"))  # type: ignore[arg-type]

    assert auto_read.marked == ["mom"]
    assert "прочит" in bot.method_calls("send_message")[0]["text"].lower()


async def test_read_in_an_unknown_chat_says_nothing() -> None:
    auto_read = FakeAutoRead()
    dispatcher = build(auto_read)
    bot = FakeBot("1:aaa", bot_id=999)

    await dispatcher.feed_update(bot, message("/read"))  # type: ignore[arg-type]

    assert auto_read.marked == []


# ------------------------------------------------------------ what is gone


async def test_react_is_not_a_command_any_more() -> None:
    """It offered a keyboard of emoji MAX accepts, which was worth having while
    it was unknown whether Telegram delivers a reaction to a bot at all. It does,
    and the puppet session sees every one — including on a contact's message,
    which the picker could never reach."""
    auto_read = FakeAutoRead()
    dispatcher = build(auto_read)
    bot = FakeBot("1:aaa", bot_id=BOT_ID)

    await dispatcher.feed_update(bot, message("/react", reply_to_id=5))  # type: ignore[arg-type]

    assert bot.method_calls("send_message") == []


async def test_a_native_reaction_update_produces_nothing() -> None:
    """The update still reaches the bot — it is the owner's own chat — and the
    router is no longer listening for it."""
    auto_read = FakeAutoRead()
    dispatcher = build(auto_read)
    bot = FakeBot("1:aaa", bot_id=BOT_ID)
    event = MessageReactionUpdated(
        chat=Chat(id=OWNER, type="private"),
        message_id=42,
        user=User(id=OWNER, is_bot=False, first_name="Owner"),
        date=datetime.now(tz=UTC),
        old_reaction=[],
        new_reaction=[ReactionTypeEmoji(emoji="👍")],
    )

    await dispatcher.feed_update(  # type: ignore[arg-type]
        bot, Update(update_id=4, message_reaction=event)
    )

    assert bot.method_calls("send_message") == []


def test_the_router_has_no_reaction_surface_left() -> None:
    import bridge.routing.presence_router as module

    for gone in ("build_picker", "CALLBACK_PREFIX", "PICKER_TITLE", "SENT", "NO_TARGET"):
        assert not hasattr(module, gone), f"{gone} is back"


def test_the_bot_no_longer_asks_telegram_for_reaction_updates() -> None:
    """`allowed_updates` is opt-in: not naming `message_reaction` means Telegram
    stops sending it at all, which is the strongest form of "not an ingress"."""
    from bridge.telegram.registry import DEFAULT_ALLOWED_UPDATES

    assert "message_reaction" not in DEFAULT_ALLOWED_UPDATES
