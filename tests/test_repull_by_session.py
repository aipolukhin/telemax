"""A re-pull empties the chat and rebuilds it from MAX.

Everything goes, the owner's own messages included. The conversation lives in
MAX; the Telegram chat is a rendering of it, and a re-pull is a request to
render it again. Keeping the owner's half while replacing the contact's left a
chat that was neither the old one nor the new one.

Two limits made that impossible for a bot and make it trivial for the owner's
session: a bot may not delete somebody else's messages, and may not delete
anything older than forty-eight hours. The owner's account has neither limit.

The import is raised to `REPULL_LIMIT` for the same reason — bringing back
fifty of the messages that were just removed would be a deletion dressed as a
refresh.
"""

from __future__ import annotations

from typing import Any

import pytest
from telethon.tl import types

from bridge.provisioning.mtproto import BorrowedBotFatherSession, WrongPeerError

pytestmark = pytest.mark.asyncio

BOT_ID = 9000000007


def _bot() -> Any:
    return types.User(id=BOT_ID, bot=True, first_name="x", access_hash=1)


class Client:
    def __init__(self, entity: Any = None) -> None:
        self.entity = entity if entity is not None else _bot()
        self.deleted: list[tuple[Any, list[int], bool]] = []

    async def get_entity(self, peer: Any) -> Any:
        return self.entity

    async def delete_messages(self, entity: Any, ids: list[int], revoke: bool = False) -> None:
        self.deleted.append((entity, ids, revoke))


def _session(client: Client) -> BorrowedBotFatherSession:
    return BorrowedBotFatherSession(lambda: client, secrets_dir=None)  # type: ignore[arg-type]


async def test_the_owner_deletes_for_both_sides_with_no_age_limit() -> None:
    client = Client()

    removed = await _session(client).delete_messages(BOT_ID, [10, 11, 12])

    assert removed == 3
    entity, ids, revoke = client.deleted[0]
    assert ids == [10, 11, 12]
    assert revoke is True, "both sides, or the contact's bot keeps its copy"
    assert int(entity.id) == BOT_ID


async def test_nothing_to_delete_asks_telegram_nothing() -> None:
    client = Client()

    assert await _session(client).delete_messages(BOT_ID, []) == 0
    assert client.deleted == []


async def test_the_peer_is_proved_before_anything_is_deleted() -> None:
    """Same gate as the wipe and the start: an id names one peer, a name does not."""
    client = Client(types.User(id=BOT_ID, bot=False, first_name="человек", access_hash=1))

    with pytest.raises(WrongPeerError):
        await _session(client).delete_messages(BOT_ID, [10])

    assert client.deleted == []


async def test_the_screen_says_the_owner_messages_go_too() -> None:
    """Two different operations behind one button, described differently."""
    from bridge.onboarding import screens

    item = type("Card", (), {"max_chat_id": 1, "title": "Иван", "username": "p_max_bot"})()

    by_session, _ = screens.repull_confirm(item, 1, by_session=True)
    by_bot, _ = screens.repull_confirm(item, 1, by_session=False)

    assert "полностью" in by_session
    assert "ваши собственные сообщения" in by_session
    assert "48 часов" not in by_session, "the bot's limit, not the owner's"
    assert "MAX не тронется" in by_session

    assert "48 часов" in by_bot, "still true when the bot is doing it"


async def test_a_repull_asks_for_the_whole_conversation_not_the_tail() -> None:
    """The chat was emptied first. A slice would be a deletion, not a refresh.

    `None`, not a big number: a number was tried for one deploy and did nothing,
    because MAX's `backward` was never passed and every ask came back as one
    default page of forty.
    """
    from bridge.provisioning.history import DEFAULT_LIMIT, REPULL_LIMIT

    assert REPULL_LIMIT is None
    assert DEFAULT_LIMIT == 50, "the ordinary import is still a tail"


# --------------------------------------- the key that made a re-pull a no-op


async def test_a_repull_frees_the_delivery_keys_of_that_chat(tmp_path: Any) -> None:
    """Without this a re-pull brings back nothing at all, and says nothing.

    The outbox refuses a job whose `source_key` it has already seen — right for
    a retry, since the key is what stops one MAX message becoming two Telegram
    ones, and wrong for a re-pull, which asks for those very messages again.
    Measured live: forty messages fetched, forty claimed, every one answered
    «already in hand», and the chat stayed empty.
    """
    from bridge.storage import Direction
    from bridge.storage.database import Database
    from bridge.storage.repositories import OutboxRepository

    database = await Database.connect(tmp_path / "b.db")
    outbox = OutboxRepository(database)

    async def settled(bridge: str, key: str) -> int:
        item_id = await outbox.enqueue(
            bridge_name=bridge,
            direction=Direction.MAX_TO_TG,
            kind="max_to_tg_text",
            payload={},
            source_key=key,
        )
        await outbox.mark_done(item_id)
        return item_id

    await settled("timur", "max:236856064:1")
    await settled("timur", "max:236856064:2")
    await settled("timur", "max:999:3")            # a different chat
    await settled("mama", "max:300000004:4")       # a different bridge
    in_flight = await outbox.enqueue(
        bridge_name="timur",
        direction=Direction.MAX_TO_TG,
        kind="max_to_tg_text",
        payload={},
        source_key="max:236856064:5",
    )

    freed = await outbox.forget_settled_from_max("timur", 236856064)

    assert freed == 2, "only this bridge, only this chat, only what is settled"
    assert await outbox.by_source_key("max:236856064:1") is None
    assert await outbox.by_source_key("max:999:3") is not None, "another chat is untouched"
    assert await outbox.by_source_key("max:300000004:4") is not None, "another bridge too"
    still = await outbox.by_source_key("max:236856064:5")
    assert still is not None and still.id == in_flight, (
        "a job in flight owns its key — taking it away lets a second copy in"
    )
    await database.close()
