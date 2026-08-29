"""V19: one bot serves one bridge, one username belongs to one bridge.

Both were true by construction and neither was enforced. Two rows could carry
the same `telegram_bot_id` and the same `expected_username`, and
`by_expected_username` answered with whichever SQLite reached first; the
mismatch surfaced at the next start, as a bridge that silently did not come up.

The preflight exists because a `CREATE UNIQUE INDEX` over rows that already
clash fails *during* the migration. The remedy is never "delete one of them" —
a bridge row names a bot that exists in Telegram — so the answer is a list of
which rows clash, and a decision the owner makes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from bridge.storage import (
    BridgeIdentityConflictError,
    BridgeRecord,
    BridgeRepository,
    Database,
)
from bridge.storage.migrations import LATEST_VERSION, MIGRATIONS
from bridge.storage.preflight import check_identities

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    connection = await Database.connect(tmp_path / "bridge.db")
    try:
        yield connection
    finally:
        await connection.close()


def record(name: str, chat: int, **fields: object) -> BridgeRecord:
    return BridgeRecord(
        bridge_name=name,
        max_chat_id=chat,
        token_env=f"TELEMAX_BOT_{name.upper()}",
        **fields,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------- the index


async def test_the_identity_indexes_are_in_the_schema(database: Database) -> None:
    assert await database.schema_version() >= 19 == 19
    names = {
        str(row["name"])
        for row in await database.query(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_bridges%'"
        )
    }
    assert names == {"idx_bridges_bot_id", "idx_bridges_expected_username"}
    assert LATEST_VERSION >= 19


async def test_two_bridges_cannot_share_one_bot(database: Database) -> None:
    bridges = BridgeRepository(database)
    await bridges.upsert(record("aaaa", 1, telegram_bot_id=111))

    with pytest.raises(BridgeIdentityConflictError, match="bbbb"):
        await bridges.upsert(record("bbbb", 2, telegram_bot_id=111))


async def test_two_bridges_cannot_share_one_username(database: Database) -> None:
    bridges = BridgeRepository(database)
    await bridges.upsert(record("aaaa", 1, expected_username="a_max_bot"))

    with pytest.raises(BridgeIdentityConflictError):
        await bridges.upsert(record("bbbb", 2, expected_username="a_max_bot"))


async def test_the_username_index_folds_case(database: Database) -> None:
    """Telegram is case-insensitive; two rows differing only in case are one bot."""
    bridges = BridgeRepository(database)
    await bridges.upsert(record("aaaa", 1, expected_username="a_max_bot"))

    with pytest.raises(BridgeIdentityConflictError):
        await bridges.upsert(record("bbbb", 2, expected_username="A_MAX_BOT"))


async def test_rows_with_nothing_to_clash_over_are_allowed(database: Database) -> None:
    """Several attempts may be in flight, and none of them names a bot yet."""
    bridges = BridgeRepository(database)
    await bridges.upsert(record("aaaa", 1))
    await bridges.upsert(record("bbbb", 2))
    await bridges.upsert(record("cccc", 3, expected_username=""))

    assert len(await bridges.all()) == 3


async def test_updating_a_bridge_in_place_is_not_a_conflict(database: Database) -> None:
    bridges = BridgeRepository(database)
    await bridges.upsert(record("aaaa", 1, telegram_bot_id=111, expected_username="a_max_bot"))
    await bridges.upsert(record("aaaa", 1, telegram_bot_id=111, expected_username="a_max_bot"))

    assert len(await bridges.all()) == 1


async def test_the_conflict_names_the_bridge_and_no_sql(database: Database) -> None:
    bridges = BridgeRepository(database)
    await bridges.upsert(record("aaaa", 1, telegram_bot_id=111))

    with pytest.raises(BridgeIdentityConflictError) as raised:
        await bridges.upsert(record("bbbb", 2, telegram_bot_id=111))

    text = str(raised.value)
    assert "bbbb" in text
    assert "UNIQUE" not in text and "idx_" not in text


# --------------------------------------------------------------- preflight


def build_v18(path: Path, rows: list[tuple[str, int, int | None, str | None]]) -> None:
    """A database stopped one version short, so V19 can be rehearsed onto it.

    Built with the driver directly: `Database.connect` migrates, and the point
    of this fixture is to be standing at V18 with rows already in place.
    """
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            " version INTEGER NOT NULL, applied_at INTEGER NOT NULL)"
        )
        for version, statements in MIGRATIONS:
            if version >= 19:
                break
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, 0)", (version,)
            )
        for name, chat, bot_id, username in rows:
            connection.execute(
                "INSERT INTO bridges (bridge_name, max_chat_id, token_env, telegram_bot_id,"
                " expected_username, source, state, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'mtproto', 'active', 0, 0)",
                (name, chat, f"E_{name}", bot_id, username),
            )
        connection.commit()
    finally:
        connection.close()


async def test_the_preflight_is_clean_on_an_ordinary_database(tmp_path: Path) -> None:
    path = tmp_path / "v18.db"
    build_v18(path, [("aaaa", 1, 1, "a_max_bot"), ("bbbb", 2, 2, "b_max_bot")])
    database = await Database.connect(path)
    try:
        outcome = await check_identities(database)

        assert outcome.clean
        assert "clean" in outcome.report()
    finally:
        await database.close()


async def test_the_preflight_names_every_clashing_row(tmp_path: Path) -> None:
    path = tmp_path / "v18.db"
    build_v18(path, [("aaaa", 1, 7, "a_max_bot"), ("bbbb", 2, 7, "A_Max_Bot")])
    connection = await _raw(path)
    try:
        outcome = await check_identities(connection)

        assert not outcome.clean
        assert {clash.what for clash in outcome.clashes} == {
            "telegram_bot_id",
            "expected_username",
        }
        report = outcome.report()
        assert "aaaa" in report and "bbbb" in report
        assert "Nothing was changed" in report
        assert outcome.mixed_case == ["bbbb"]
    finally:
        await connection.close()


async def test_a_clashing_database_refuses_the_migration_and_stays_at_v18(
    tmp_path: Path,
) -> None:
    """Failure leaves V18, not half of V19."""
    path = tmp_path / "v18.db"
    build_v18(path, [("aaaa", 1, 7, None), ("bbbb", 2, 7, None)])

    with pytest.raises(sqlite3.IntegrityError):
        await Database.connect(path)

    connection = await _raw(path)
    try:
        assert await connection.schema_version() == 18
    finally:
        await connection.close()


async def test_a_clean_database_migrates_and_keeps_its_rows(tmp_path: Path) -> None:
    path = tmp_path / "v18.db"
    build_v18(path, [("aaaa", 1, 1, "a_max_bot"), ("bbbb", 2, 2, "b_max_bot")])

    database = await Database.connect(path)
    try:
        assert await database.schema_version() == LATEST_VERSION
        assert {row.bridge_name for row in await BridgeRepository(database).all()} == {
            "aaaa",
            "bbbb",
        }
    finally:
        await database.close()


async def _raw(path: Path) -> Database:
    """Open without migrating: the point is to read a database still at V18."""
    from bridge.storage.database import _open

    return Database(await _open(path), path)
