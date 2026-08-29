"""Who may speak to a contact bot, and from where.

The "from where" half is the one that was missing. A contact bot carries a
private conversation with one relative; a message the owner typed in some group
the bot was added to must never reach that person.
"""

from __future__ import annotations

from typing import Any

from aiogram.types import Chat, Message, TelegramObject, Update, User

from bridge.telegram import OwnerOnlyMiddleware

OWNER_ID = 4242
STRANGER_ID = 9999


def _message(*, from_id: int, chat_id: int, chat_type: str) -> Message:
    return Message.model_construct(
        message_id=1,
        date=None,
        chat=Chat.model_construct(id=chat_id, type=chat_type),
        from_user=User.model_construct(id=from_id, is_bot=False, first_name="x"),
        text="привет",
    )


async def _run(message: Message) -> list[TelegramObject]:
    """Feed one message through the filter; return what reached the handler."""
    seen: list[TelegramObject] = []

    async def handler(event: TelegramObject, data: dict[str, Any]) -> Any:
        seen.append(event)
        return None

    middleware = OwnerOnlyMiddleware(OWNER_ID, reply_to_strangers=False)
    update = Update.model_construct(update_id=1, message=message)
    data = {
        "event_update": update,
        "event_from_user": message.from_user,
        "event_chat": message.chat,
    }
    await middleware(handler, update, data)
    return seen


async def test_owner_in_their_private_chat_is_allowed() -> None:
    message = _message(from_id=OWNER_ID, chat_id=OWNER_ID, chat_type="private")
    assert len(await _run(message)) == 1


async def test_owner_in_a_group_is_dropped() -> None:
    """The leak this test exists for: a group message must not reach MAX.

    Someone adds the contact bot to a family group. The owner writes there. Up
    to now the filter saw the owner's id, said yes, and the line went to the
    relative on the other end of that bridge.
    """
    message = _message(from_id=OWNER_ID, chat_id=-1001234, chat_type="group")
    assert await _run(message) == []


async def test_owner_in_a_supergroup_is_dropped() -> None:
    message = _message(from_id=OWNER_ID, chat_id=-1009999, chat_type="supergroup")
    assert await _run(message) == []


async def test_owner_in_a_channel_is_dropped() -> None:
    message = _message(from_id=OWNER_ID, chat_id=-1008888, chat_type="channel")
    assert await _run(message) == []


async def test_owner_in_someone_elses_private_chat_is_dropped() -> None:
    """A private chat whose id is not the owner's is not the owner's chat."""
    message = _message(from_id=OWNER_ID, chat_id=STRANGER_ID, chat_type="private")
    assert await _run(message) == []


async def test_a_stranger_is_dropped() -> None:
    message = _message(from_id=STRANGER_ID, chat_id=STRANGER_ID, chat_type="private")
    assert await _run(message) == []


async def test_a_stranger_in_a_group_is_dropped() -> None:
    message = _message(from_id=STRANGER_ID, chat_id=-1001234, chat_type="group")
    assert await _run(message) == []


async def test_a_stranger_still_gets_one_bland_line() -> None:
    """Dropping the update must not have dropped the brush-off with it."""
    answered: list[tuple[int, str]] = []

    class FakeBot:
        async def send_message(self, *, chat_id: int, text: str) -> None:
            answered.append((chat_id, text))

    async def handler(event: TelegramObject, data: dict[str, Any]) -> Any:
        raise AssertionError("a stranger must never reach a handler")

    message = _message(from_id=STRANGER_ID, chat_id=STRANGER_ID, chat_type="private")
    update = Update.model_construct(update_id=1, message=message)
    await OwnerOnlyMiddleware(OWNER_ID)(
        handler,
        update,
        {
            "event_update": update,
            "event_from_user": message.from_user,
            "event_chat": message.chat,
            "bot": FakeBot(),
        },
    )

    assert len(answered) == 1
    assert "MAX" not in answered[0][1]


async def test_a_stranger_in_a_group_is_not_answered() -> None:
    """Replying there would announce the bot to everyone in the group."""
    answered: list[int] = []

    class FakeBot:
        async def send_message(self, *, chat_id: int, text: str) -> None:
            answered.append(chat_id)

    async def handler(event: TelegramObject, data: dict[str, Any]) -> Any:
        return None

    message = _message(from_id=STRANGER_ID, chat_id=-1001234, chat_type="group")
    update = Update.model_construct(update_id=1, message=message)
    await OwnerOnlyMiddleware(OWNER_ID)(
        handler,
        update,
        {
            "event_update": update,
            "event_from_user": message.from_user,
            "event_chat": message.chat,
            "bot": FakeBot(),
        },
    )

    assert answered == []


async def test_a_business_deletion_no_longer_gets_a_way_in() -> None:
    """The exemption existed for one update, and that update is gone.

    `deleted_business_messages` was the only thing Telegram sent this bot with no
    `from_user` at all, so the middleware carried a rule admitting it. It is no
    longer subscribed to — owner deletions come through the owner's own MTProto
    session, durably — and a rule that admits an update nobody asks for is a
    standing permission with nothing behind it. Every update the bot receives now
    carries a user, and anything that does not is a stranger.
    """

    async def handler(event: TelegramObject, data: dict[str, Any]) -> Any:
        raise AssertionError("a user-less update must not reach a handler")

    update = Update.model_construct(
        update_id=1, deleted_business_messages=object(), message=None
    )
    result = await OwnerOnlyMiddleware(OWNER_ID)(
        handler, update, {"event_update": update, "event_from_user": None, "event_chat": None}
    )

    assert result is None


async def test_an_update_with_no_user_and_no_business_marker_is_dropped() -> None:
    async def handler(event: TelegramObject, data: dict[str, Any]) -> Any:
        raise AssertionError("an anonymous update must not reach a handler")

    update = Update.model_construct(update_id=1, message=None)
    result = await OwnerOnlyMiddleware(OWNER_ID)(
        handler, update, {"event_update": update, "event_from_user": None, "event_chat": None}
    )

    assert result is None


async def test_an_update_with_no_chat_is_judged_by_its_sender() -> None:
    """business_connection and managed_bot have no chat and still must work."""
    seen: list[TelegramObject] = []

    async def handler(event: TelegramObject, data: dict[str, Any]) -> Any:
        seen.append(event)
        return None

    update = Update.model_construct(update_id=1, message=None)
    owner = User.model_construct(id=OWNER_ID, is_bot=False, first_name="x")
    await OwnerOnlyMiddleware(OWNER_ID)(
        handler, update, {"event_update": update, "event_from_user": owner, "event_chat": None}
    )

    assert len(seen) == 1
