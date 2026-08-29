"""AU-3 G3 — a message the worker carried gets its identity too.

`attach_max_message` lived in the router, which is the *inline* sender. Anything
the worker delivered — a retry, a message written while MAX was briefly down —
was marked DONE with a real remote id in the outbox and left `max_message_id`
NULL in `message_map`. Production: 33 of 202 `tg_to_max` rows, at least eight of
them with a DONE job holding the id that never made it across.

Nothing was lost — the message arrived — but half its identity was. A reply to
such a message resolves to nothing and reaches MAX without its quote, and edit
and delete survive only through a defensive fallback to `outbox.remote_message_id`.

So the attach moved into `send_job`, the one function both senders share.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest_asyncio

from bridge.routing.settlement import settle_max_delivery_mapping
from bridge.storage import Database, MessageMapRepository

BRIDGE = "dad"
MAX_CHAT = 555
BOT = 7001


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


async def a_row(messages: MessageMapRepository, telegram_message_id: int = 100) -> int:
    return await messages.record_from_telegram(
        bridge_name=BRIDGE,
        max_chat_id=MAX_CHAT,
        telegram_bot_id=BOT,
        telegram_chat_id=BOT,
        telegram_message_id=telegram_message_id,
    )


# --------------------------------------------------------------- the attach


async def test_the_mapping_learns_the_max_id(database: Database) -> None:
    messages = MessageMapRepository(database)
    link_id = await a_row(messages)

    assert await settle_max_delivery_mapping(messages, {"link_id": link_id}, 9001) == 9001

    link = await messages.by_id(link_id)
    assert link is not None
    assert link.max_message_id == 9001


async def test_settling_the_same_id_twice_is_a_quiet_success(database: Database) -> None:
    """A replayed settlement writes the same number. That is not a conflict."""
    messages = MessageMapRepository(database)
    link_id = await a_row(messages)

    assert await messages.attach_max_message(link_id, 9001)
    assert await messages.attach_max_message(link_id, 9001)

    link = await messages.by_id(link_id)
    assert link is not None and link.max_message_id == 9001


async def test_a_different_id_is_refused_rather_than_overwritten(
    database: Database, caplog: Any
) -> None:
    """Two remote messages claiming one mapping row. Whichever id lost would
    become a message no reply, edit or delete could resolve against again — so
    the first one stands and the second is said out loud."""
    import logging

    messages = MessageMapRepository(database)
    link_id = await a_row(messages)
    assert await messages.attach_max_message(link_id, 9001)

    assert not await messages.attach_max_message(link_id, 9002)
    link = await messages.by_id(link_id)
    assert link is not None and link.max_message_id == 9001

    with caplog.at_level(logging.ERROR, logger="bridge.routing.settlement"):
        await settle_max_delivery_mapping(messages, {"link_id": link_id}, 9002)
    assert any("needs a look" in record.getMessage() for record in caplog.records)


async def test_a_payload_without_a_link_still_hands_the_id_back(database: Database) -> None:
    """Not every creating job carries a mapping row; the id is the return value
    either way, and the caller's `mark_done` depends on it."""
    messages = MessageMapRepository(database)
    assert await settle_max_delivery_mapping(messages, {}, 9001) == 9001
    assert await settle_max_delivery_mapping(None, {"link_id": 1}, 9001) == 9001


async def test_a_missing_row_is_not_silently_a_success(database: Database) -> None:
    messages = MessageMapRepository(database)
    assert not await messages.attach_max_message(999_999, 9001)


# --------------------------------------------------- both senders, same result


async def test_a_reply_resolves_after_a_worker_delivery(database: Database) -> None:
    """The user-visible half of G3. A reply looks the target up by
    `link.max_message_id`, with no fallback to the outbox — so a row the worker
    left NULL used to make the answer arrive with no quote."""
    messages = MessageMapRepository(database)
    link_id = await a_row(messages, telegram_message_id=100)

    # Delivered by the worker: before this commit nothing wrote the id here.
    await settle_max_delivery_mapping(messages, {"link_id": link_id}, 9001)

    target = await messages.by_telegram_message(BOT, 100)
    assert target is not None
    assert target.max_message_id == 9001  # what `_max_reply_target` returns


async def test_an_unsettled_row_is_what_the_old_path_left(database: Database) -> None:
    """The negative control, so the test above is measuring something."""
    messages = MessageMapRepository(database)
    await a_row(messages, telegram_message_id=101)

    target = await messages.by_telegram_message(BOT, 101)
    assert target is not None
    assert target.max_message_id is None


# ------------------------------------------------------------ album settlement


async def test_settling_the_canonical_row_settles_every_part(database: Database) -> None:
    """An album is N Telegram messages and one MAX message; every part aliases
    the one canonical row, so filling it in settles all of them at once."""
    from bridge.storage import MediaGroupRepository

    messages = MessageMapRepository(database)
    albums = MediaGroupRepository(database)
    link_id = await a_row(messages, telegram_message_id=200)
    for part in (200, 201, 202):
        await albums.add_part(
            media_group_id="grp",
            bridge_name=BRIDGE,
            bot_id=BOT,
            telegram_message_id=part,
            payload={},
        )
    await albums.bind_link("grp", link_id)

    await settle_max_delivery_mapping(messages, {"link_id": link_id}, 9001)

    bound = await albums.link_of("grp")
    assert bound == link_id
    link = await messages.by_id(link_id)
    assert link is not None and link.max_message_id == 9001


def test_nothing_attaches_outside_the_shared_helper() -> None:
    """The structural half: one settlement implementation, not one per sender."""
    import ast
    offenders: list[str] = []
    for path in sorted(Path("bridge").rglob("*.py")):
        if path.name == "settlement.py" or "repositories" in path.name:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "attach_max_message"
            ):
                offenders.append(f"{path}:{node.lineno}")
    assert offenders == [], f"attach_max_message called outside the shared helper: {offenders}"
