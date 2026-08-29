"""An album's identity is written whole, or the database is untouched.

The settlement used to be a loop of conditional updates with the canonical head
written first. Three things followed, and all three are asserted here as their
opposites: a short receipt left a canonical message pointing at a delivery whose
parts were unbound; a conflict on part *k* left 0..k-1 written and k..N not; and
"the ids are ascending" was a contract nothing checked.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from bridge.storage import (
    AlbumSettlementError,
    AlbumSettlementRepository,
    Database,
    Direction,
    MediaGroupRepository,
    MessageMapRepository,
)

BRIDGE = "mum"
BOT = 111
OWNER = 999
MAX_CHAT = 7


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


async def _album(
    database: Database, *, parts: int = 3, max_message_id: int = 42
) -> tuple[int, MediaGroupRepository, MessageMapRepository, AlbumSettlementRepository]:
    messages = MessageMapRepository(database)
    albums = MediaGroupRepository(database)
    link_id = await messages.claim_from_max(
        bridge_name=BRIDGE,
        max_chat_id=MAX_CHAT,
        max_message_id=max_message_id,
        telegram_bot_id=BOT,
        telegram_chat_id=OWNER,
    )
    assert link_id is not None
    for index in range(parts):
        await albums.add_part(
            media_group_id=f"max:{link_id}",
            bridge_name=BRIDGE,
            bot_id=BOT,
            payload={"kind": "photo"},
            link_id=link_id,
            direction=Direction.MAX_TO_TG,
            part_index=index,
            media_kind="photo",
            caption_present=index == 0,
            part_fingerprint=f"a1:{index}",
        )
    return link_id, albums, messages, AlbumSettlementRepository(database)


async def _nothing_written(
    albums: MediaGroupRepository, messages: MessageMapRepository, link_id: int
) -> None:
    """Not one row moved — the whole point of the transaction."""
    parts = await albums.parts_of_link(link_id)
    assert [part.telegram_message_id for part in parts] == [None] * len(parts)
    assert [part.telegram_owner_message_id for part in parts] == [None] * len(parts)
    canonical = await messages.by_id(link_id)
    assert canonical is not None
    assert canonical.telegram_message_id is None
    assert canonical.telegram_owner_message_id is None


# ------------------------------------------------------------------ the happy path


@pytest.mark.asyncio
async def test_a_full_album_binds_the_head_and_every_alias(database: Database) -> None:
    link_id, albums, messages, settlement = await _album(database)
    await settlement.settle_bot_album(link_id=link_id, parts=[(0, 7001), (1, 7002), (2, 7003)])

    parts = await albums.parts_of_link(link_id)
    assert [part.part_index for part in parts] == [0, 1, 2]
    assert [part.telegram_message_id for part in parts] == [7001, 7002, 7003]
    canonical = await messages.by_id(link_id)
    assert canonical is not None and canonical.telegram_message_id == 7001


@pytest.mark.asyncio
async def test_an_owner_album_binds_the_account_with_every_id(database: Database) -> None:
    link_id, albums, messages, settlement = await _album(database)
    await settlement.settle_owner_album(
        link_id=link_id, account_id=OWNER, parts=[(0, 6001), (1, 6002), (2, 6003)]
    )
    parts = await albums.parts_of_link(link_id)
    assert [part.telegram_owner_message_id for part in parts] == [6001, 6002, 6003]
    assert all(part.telegram_owner_account_id == OWNER for part in parts)
    canonical = await messages.by_id(link_id)
    assert canonical is not None and canonical.telegram_owner_message_id == 6001


# ------------------------------------------------------------- nothing partial ever


@pytest.mark.asyncio
async def test_a_short_receipt_writes_nothing_at_all(database: Database) -> None:
    link_id, albums, messages, settlement = await _album(database)
    with pytest.raises(AlbumSettlementError, match="nothing was bound"):
        await settlement.settle_bot_album(link_id=link_id, parts=[(0, 7001), (1, 7002)])
    await _nothing_written(albums, messages, link_id)


@pytest.mark.asyncio
async def test_a_long_receipt_writes_nothing_at_all(database: Database) -> None:
    link_id, albums, messages, settlement = await _album(database)
    with pytest.raises(AlbumSettlementError):
        await settlement.settle_bot_album(
            link_id=link_id, parts=[(0, 7001), (1, 7002), (2, 7003), (3, 7004)]
        )
    await _nothing_written(albums, messages, link_id)


@pytest.mark.asyncio
async def test_duplicate_ids_write_nothing_at_all(database: Database) -> None:
    link_id, albums, messages, settlement = await _album(database)
    with pytest.raises(AlbumSettlementError, match="same message id"):
        await settlement.settle_bot_album(
            link_id=link_id, parts=[(0, 7001), (1, 7001), (2, 7003)]
        )
    await _nothing_written(albums, messages, link_id)


@pytest.mark.asyncio
async def test_reordered_ids_write_nothing_and_are_never_sorted(database: Database) -> None:
    link_id, albums, messages, settlement = await _album(database)
    with pytest.raises(AlbumSettlementError, match="ascending"):
        await settlement.settle_bot_album(
            link_id=link_id, parts=[(0, 7003), (1, 7001), (2, 7002)]
        )
    await _nothing_written(albums, messages, link_id)


@pytest.mark.asyncio
async def test_a_gap_in_the_positions_writes_nothing(database: Database) -> None:
    link_id, albums, messages, settlement = await _album(database)
    with pytest.raises(AlbumSettlementError, match="cover"):
        await settlement.settle_bot_album(
            link_id=link_id, parts=[(0, 7001), (2, 7002), (3, 7003)]
        )
    await _nothing_written(albums, messages, link_id)


@pytest.mark.asyncio
async def test_an_impossible_id_writes_nothing(database: Database) -> None:
    link_id, albums, messages, settlement = await _album(database)
    with pytest.raises(AlbumSettlementError):
        await settlement.settle_bot_album(link_id=link_id, parts=[(0, 0), (1, 7002), (2, 7003)])
    await _nothing_written(albums, messages, link_id)


# ---------------------------------------------------------------------- conflicts


@pytest.mark.parametrize("position", [0, 1, 2], ids=["first", "middle", "last"])
@pytest.mark.asyncio
async def test_a_conflict_on_any_part_rolls_the_whole_album_back(
    position: int, database: Database
) -> None:
    """The defect this transaction exists for: 0..k-1 written, k..N not."""
    link_id, albums, messages, settlement = await _album(database)
    parts = await albums.parts_of_link(link_id)
    # Somebody else already proved a different id for one alias.
    assert await albums.attach_bot_message(parts[position].id, 8888)

    with pytest.raises(AlbumSettlementError):
        await settlement.settle_bot_album(
            link_id=link_id, parts=[(0, 7001), (1, 7002), (2, 7003)]
        )

    after = await albums.parts_of_link(link_id)
    expected = [None, None, None]
    expected[position] = 8888  # type: ignore[call-overload]
    assert [part.telegram_message_id for part in after] == expected
    canonical = await messages.by_id(link_id)
    assert canonical is not None and canonical.telegram_message_id is None


@pytest.mark.asyncio
async def test_a_canonical_conflict_leaves_every_alias_alone(database: Database) -> None:
    link_id, albums, messages, settlement = await _album(database)
    await messages.attach_telegram_message(link_id, 5555)

    with pytest.raises(AlbumSettlementError, match="canonical"):
        await settlement.settle_bot_album(
            link_id=link_id, parts=[(0, 7001), (1, 7002), (2, 7003)]
        )

    parts = await albums.parts_of_link(link_id)
    assert [part.telegram_message_id for part in parts] == [None, None, None]
    canonical = await messages.by_id(link_id)
    assert canonical is not None and canonical.telegram_message_id == 5555


@pytest.mark.asyncio
async def test_a_head_that_belongs_to_another_message_is_refused(database: Database) -> None:
    """The unique index answers, and the answer is a rollback, not a force.

    `message_map` is unique on `(telegram_bot_id, telegram_message_id)`, so one
    Telegram message can be the canonical id of one logical message and no more.
    The alias table's own uniqueness is scoped to a group — two albums naming the
    same part id is not something Telegram can produce, and inventing a
    cross-group check here would be a guard against nothing.
    """
    first, albums, messages, settlement = await _album(database, max_message_id=42)
    await settlement.settle_bot_album(link_id=first, parts=[(0, 7001), (1, 7002), (2, 7003)])
    second, _, _, _ = await _album(database, max_message_id=43)

    with pytest.raises(AlbumSettlementError):
        await settlement.settle_bot_album(
            link_id=second, parts=[(0, 7001), (1, 7900), (2, 7901)]
        )
    await _nothing_written(albums, messages, second)


# ----------------------------------------------------------------------- replays


@pytest.mark.asyncio
async def test_settling_the_same_album_twice_changes_nothing(database: Database) -> None:
    link_id, albums, _messages, settlement = await _album(database)
    parts = [(0, 7001), (1, 7002), (2, 7003)]
    await settlement.settle_bot_album(link_id=link_id, parts=parts)
    await settlement.settle_bot_album(link_id=link_id, parts=parts)
    bound = await albums.parts_of_link(link_id)
    assert [part.telegram_message_id for part in bound] == [7001, 7002, 7003]


@pytest.mark.asyncio
async def test_a_second_settlement_with_other_ids_is_refused(database: Database) -> None:
    link_id, albums, _messages, settlement = await _album(database)
    await settlement.settle_bot_album(link_id=link_id, parts=[(0, 7001), (1, 7002), (2, 7003)])
    with pytest.raises(AlbumSettlementError):
        await settlement.settle_bot_album(
            link_id=link_id, parts=[(0, 7101), (1, 7102), (2, 7103)]
        )
    bound = await albums.parts_of_link(link_id)
    assert [part.telegram_message_id for part in bound] == [7001, 7002, 7003]


@pytest.mark.asyncio
async def test_the_owner_settlement_is_replayable_too(database: Database) -> None:
    link_id, albums, _messages, settlement = await _album(database)
    parts = [(0, 6001), (1, 6002), (2, 6003)]
    await settlement.settle_owner_album(link_id=link_id, account_id=OWNER, parts=parts)
    await settlement.settle_owner_album(link_id=link_id, account_id=OWNER, parts=parts)
    bound = await albums.parts_of_link(link_id)
    assert [part.telegram_owner_message_id for part in bound] == [6001, 6002, 6003]


@pytest.mark.asyncio
async def test_a_delivery_with_no_aliases_still_binds_its_canonical_row(
    database: Database,
) -> None:
    """A router built without the alias table: the mapping is the whole story."""
    messages = MessageMapRepository(database)
    settlement = AlbumSettlementRepository(database)
    link_id = await messages.claim_from_max(
        bridge_name=BRIDGE,
        max_chat_id=MAX_CHAT,
        max_message_id=99,
        telegram_bot_id=BOT,
        telegram_chat_id=OWNER,
    )
    assert link_id is not None
    await settlement.settle_bot_album(link_id=link_id, parts=[(0, 7001), (1, 7002)])
    canonical = await messages.by_id(link_id)
    assert canonical is not None and canonical.telegram_message_id == 7001


@pytest.mark.asyncio
async def test_the_database_is_usable_after_a_rolled_back_settlement(
    database: Database,
) -> None:
    """A rollback must leave the connection working, not poisoned."""
    link_id, albums, _messages, settlement = await _album(database)
    with pytest.raises(AlbumSettlementError):
        await settlement.settle_bot_album(link_id=link_id, parts=[(0, 7001)])
    assert database.poisoned is None
    await settlement.settle_bot_album(link_id=link_id, parts=[(0, 7001), (1, 7002), (2, 7003)])
    bound = await albums.parts_of_link(link_id)
    assert [part.telegram_message_id for part in bound] == [7001, 7002, 7003]
