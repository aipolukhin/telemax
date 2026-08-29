"""A migration step lands whole or not at all.

This file exists because the opposite was true and nobody noticed. SQLite's
Python driver, left at its default, runs DDL outside any transaction: a step
that died halfway left its first tables created, `schema_version` unmoved, and
the next start re-ran the step straight into "table already exists" — a database
that could not be opened again without hand surgery.

The tests below break a step on purpose and check the three things that matter:
nothing from the failed step survives, the version does not move, and the next
attempt still works.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import aiosqlite
import pytest

from bridge.storage import LATEST_VERSION, Database

#: A step whose second statement cannot parse. The first one is perfectly valid,
#: which is the point: it is the statement that must be undone.
_BROKEN_STEP: tuple[Any, ...] = (
    (
        99,
        (
            "CREATE TABLE half_applied (x INTEGER)",
            "CREATE TABLE this is not valid sql at all",
        ),
    ),
)


async def _tables(database: Database) -> set[str]:
    rows = await database.query("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {row["name"] for row in rows}


async def test_failed_step_rolls_back_its_earlier_statements(tmp_path: Path) -> None:
    path = tmp_path / "bridge.db"
    database = await Database.connect(path)
    try:
        assert await database.schema_version() == LATEST_VERSION

        with (
            patch("bridge.storage.database.MIGRATIONS", _BROKEN_STEP),
            patch("bridge.storage.database.LATEST_VERSION", 99),
            pytest.raises(Exception, match=r"syntax|SQL"),
        ):
            await database.migrate()

        # The first statement of the broken step created this table. If DDL is
        # not inside the transaction, it is still here — and that is the bug.
        assert "half_applied" not in await _tables(database)
        assert await database.schema_version() == LATEST_VERSION
    finally:
        await database.close()


async def test_version_does_not_move_when_a_step_fails(tmp_path: Path) -> None:
    path = tmp_path / "bridge.db"
    database = await Database.connect(path)
    try:
        before = await database.query("SELECT version FROM schema_version ORDER BY version")

        with (
            patch("bridge.storage.database.MIGRATIONS", _BROKEN_STEP),
            patch("bridge.storage.database.LATEST_VERSION", 99),
            pytest.raises(Exception, match=r"syntax|SQL"),
        ):
            await database.migrate()

        after = await database.query("SELECT version FROM schema_version ORDER BY version")
        assert [row["version"] for row in after] == [row["version"] for row in before]
        assert 99 not in [row["version"] for row in after]
    finally:
        await database.close()


async def test_database_still_opens_after_a_failed_migration(tmp_path: Path) -> None:
    """The regression that mattered: a half-applied step used to brick the file.

    Re-running the *real* migrations after a failed experimental step has to be
    a no-op, not a crash on a table that a rolled-back step left behind.
    """
    path = tmp_path / "bridge.db"
    database = await Database.connect(path)
    try:
        with (
            patch("bridge.storage.database.MIGRATIONS", _BROKEN_STEP),
            patch("bridge.storage.database.LATEST_VERSION", 99),
            pytest.raises(Exception, match=r"syntax|SQL"),
        ):
            await database.migrate()
    finally:
        await database.close()

    reopened = await Database.connect(path)
    try:
        assert await reopened.schema_version() == LATEST_VERSION
        assert await reopened.migrate() == LATEST_VERSION
    finally:
        await reopened.close()


async def test_a_step_that_succeeds_is_committed(tmp_path: Path) -> None:
    """The other half of the contract: a good step must actually persist."""
    good_step: tuple[Any, ...] = ((99, ("CREATE TABLE fully_applied (x INTEGER)",)),)
    path = tmp_path / "bridge.db"
    database = await Database.connect(path)
    try:
        with (
            patch("bridge.storage.database.MIGRATIONS", good_step),
            patch("bridge.storage.database.LATEST_VERSION", 99),
        ):
            assert await database.migrate() == 99
        assert "fully_applied" in await _tables(database)
    finally:
        await database.close()

    reopened = await Database.connect(path)
    try:
        assert "fully_applied" in await _tables(reopened)
    finally:
        await reopened.close()


async def test_existing_database_upgrades_without_losing_mappings(tmp_path: Path) -> None:
    """A real V7 database gains V8 without losing what it already held.

    The old version is built by running only the first seven steps — the same
    ones someone's live database ran — rather than by mutilating a current
    schema, which would prove nothing about the upgrade people will actually do.
    """
    from bridge.storage import MessageMapRepository
    from bridge.storage.migrations import MIGRATIONS

    v7 = tuple(step for step in MIGRATIONS if step[0] <= 7)
    path = tmp_path / "bridge.db"

    with (
        patch("bridge.storage.database.MIGRATIONS", v7),
        patch("bridge.storage.database.LATEST_VERSION", 7),
    ):
        old = await Database.connect(path)
        try:
            assert await old.schema_version() == 7
            assert "telegram_inbox" not in await _tables(old)

            # Written the way V7 itself wrote it — the columns that existed then,
            # by hand. Claiming through today's repository would insert columns
            # this schema has never heard of, which tests the wrong thing: what is
            # under examination is whether an *old* row survives the upgrade.
            link = await old.execute(
                "INSERT INTO message_map ("
                " bridge_name, max_chat_id, max_message_id, telegram_bot_id,"
                " telegram_chat_id, telegram_message_id, direction, source_marker,"
                " created_at)"
                " VALUES ('dad', 777, 42, 555, -100, NULL, 'max_to_tg', 'from_max', 1)",
                (),
            )
            assert link is not None
            await MessageMapRepository(old).attach_telegram_message(link, 9001)
        finally:
            await old.close()

    upgraded = await Database.connect(path)
    try:
        assert await upgraded.schema_version() == LATEST_VERSION
        assert {"telegram_inbox", "telegram_offset", "media_group_part"} <= await _tables(upgraded)

        found = await MessageMapRepository(upgraded).by_max_message(777, 42, 555)
        assert found is not None
        assert found.telegram_message_id == 9001
    finally:
        await upgraded.close()


async def test_v9_adds_the_sending_marker_to_a_real_database(tmp_path: Path) -> None:
    """A V8 database gains the column without losing its jobs.

    Built by running only the first eight steps, the way a live database ran
    them, rather than by mutilating a current schema.
    """
    from bridge.storage import Direction, OutboxRepository
    from bridge.storage.migrations import MIGRATIONS

    v8 = tuple(step for step in MIGRATIONS if step[0] <= 8)
    path = tmp_path / "bridge.db"

    with (
        patch("bridge.storage.database.MIGRATIONS", v8),
        patch("bridge.storage.database.LATEST_VERSION", 8),
    ):
        old = await Database.connect(path)
        try:
            assert await old.schema_version() == 8
            await OutboxRepository(old).enqueue(
                bridge_name="dad",
                direction=Direction.MAX_TO_TG,
                kind="max_to_tg_text",
                payload={"text": "уже в очереди"},
                source_key="max:1:1",
            )
        finally:
            await old.close()

    upgraded = await Database.connect(path)
    try:
        assert await upgraded.schema_version() == LATEST_VERSION
        columns = {r["name"] for r in await upgraded.query("PRAGMA table_info(outbox)")}
        assert "send_started_at" in columns

        # The job that was already queued is still queued, and still sendable.
        claimed = await OutboxRepository(upgraded).claim_due("dad")
        assert len(claimed) == 1
        assert "уже в очереди" in claimed[0].payload_json
        assert claimed[0].send_started_at is None
    finally:
        await upgraded.close()


async def _open_without_migrating(path: Path) -> Database:
    """A `Database` over an existing file, with `migrate()` left for the caller.

    `connect()` migrates on the way in, which is exactly what a test about a
    *failing* migration cannot have: the failure would happen inside the
    constructor and leave nothing to inspect.
    """
    connection = await aiosqlite.connect(path, isolation_level=None)
    connection.row_factory = aiosqlite.Row
    return Database(connection, path)


async def _v14_with_an_album(path: Path) -> None:
    """A V14 database holding an album the Bot API collector had accepted.

    Built by running only the first fourteen steps, the way a live database ran
    them: what is under examination is whether a *real* old row survives V15's
    rebuild, and a row inserted through today's repository would prove nothing.
    """
    from bridge.storage.migrations import MIGRATIONS

    v14 = tuple(step for step in MIGRATIONS if step[0] <= 14)
    with (
        patch("bridge.storage.database.MIGRATIONS", v14),
        patch("bridge.storage.database.LATEST_VERSION", 14),
    ):
        old = await Database.connect(path)
        try:
            assert await old.schema_version() == 14
            for message_id, file_id in ((11, "AgACAgIAAx"), (12, "AgACAgIAAy")):
                await old.execute(
                    "INSERT INTO media_group_part ("
                    " media_group_id, bridge_name, bot_id, telegram_message_id,"
                    " payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        "17983",
                        "dad",
                        555,
                        message_id,
                        json.dumps({"file_id": file_id, "file_name": "фото.jpg"}),
                        1_700_000_000_000 + message_id,
                    ),
                )
        finally:
            await old.close()


async def test_v15_rebuilds_media_group_part_without_losing_a_row(tmp_path: Path) -> None:
    """The rebuild is a copy, not a re-import: every column survives it.

    `id` most of all. It is the arrival order an unfinished album is reassembled
    in, so a copy that renumbered would reorder somebody's photos — silently, and
    only for the album that was in flight when the upgrade ran.
    """
    from bridge.storage import MediaGroupRepository

    path = tmp_path / "bridge.db"
    await _v14_with_an_album(path)

    upgraded = await Database.connect(path)
    try:
        assert await upgraded.schema_version() == LATEST_VERSION
        parts = await MediaGroupRepository(upgraded).parts("17983")
        assert [part.id for part in parts] == [1, 2]
        assert [part.telegram_message_id for part in parts] == [11, 12]
        assert [part.created_at for part in parts] == [
            1_700_000_000_011,
            1_700_000_000_012,
        ]
        # Byte for byte, non-ASCII included: the payload is the caller's and the
        # migration is not entitled to normalise it.
        assert parts[0].payload_json == json.dumps(
            {"file_id": "AgACAgIAAx", "file_name": "фото.jpg"}
        )
        assert json.loads(parts[1].payload_json)["file_id"] == "AgACAgIAAy"

        # Nothing about a legacy row claims to be an alias of anything.
        for part in parts:
            assert part.link_id is None
            assert part.direction is None
            assert part.part_index is None
            assert part.media_kind is None
            assert part.caption_present is None
            assert part.part_fingerprint is None
            assert part.telegram_owner_account_id is None
            assert part.telegram_owner_message_id is None
            assert part.updated_at is None

        # And the group is still the collector's to reassemble.
        assert await MediaGroupRepository(upgraded).open_groups() == ["17983"]
    finally:
        await upgraded.close()


async def test_v15_keeps_the_id_sequence_across_the_rebuild(tmp_path: Path) -> None:
    """A rebuilt table must not start handing out ids that already exist.

    `AUTOINCREMENT` keeps its high-water mark in `sqlite_sequence`, keyed by
    table name — so a rebuild that dropped the old table and forgot to carry the
    counter would give the next part id 1, and the unique index would be the
    only thing standing between that and a mangled album.
    """
    from bridge.storage import MediaGroupRepository

    path = tmp_path / "bridge.db"
    await _v14_with_an_album(path)

    upgraded = await Database.connect(path)
    try:
        groups = MediaGroupRepository(upgraded)
        assert await groups.add_part(
            media_group_id="17983",
            bridge_name="dad",
            bot_id=555,
            telegram_message_id=13,
            payload={"file_id": "AgACAgIAAz"},
        )
        assert [part.id for part in await groups.parts("17983")] == [1, 2, 3]
    finally:
        await upgraded.close()


async def test_a_failed_v15_leaves_the_old_table_exactly_as_it_was(
    tmp_path: Path,
) -> None:
    """The rebuild is four statements and a handful of indexes; any of them may
    fail. If the drop had already run, a database that came up before would not
    come up again — which is the failure mode the whole transaction rule exists
    for, made concrete on the one step that destroys a table on its way."""
    from bridge.storage.migrations import MIGRATIONS

    path = tmp_path / "bridge.db"
    await _v14_with_an_album(path)

    broken = (
        *(step for step in MIGRATIONS if step[0] <= 14),
        (15, (*MIGRATIONS[14][1], "CREATE TABLE this is not valid sql at all")),
    )
    with (
        patch("bridge.storage.database.MIGRATIONS", broken),
        patch("bridge.storage.database.LATEST_VERSION", 15),
    ):
        database = await _open_without_migrating(path)
        try:
            with pytest.raises(Exception, match=r"syntax|SQL"):
                await database.migrate()

            assert await database.schema_version() == 14
            assert "media_group_part_rebuilt" not in await _tables(database)
            columns = {
                r["name"]
                for r in await database.query("PRAGMA table_info(media_group_part)")
            }
            assert "link_id" not in columns
            rows = await database.query(
                "SELECT telegram_message_id FROM media_group_part ORDER BY id"
            )
            assert [row["telegram_message_id"] for row in rows] == [11, 12]
        finally:
            await database.close()

    # And the upgrade still works once the step is whole again.
    upgraded = await Database.connect(path)
    try:
        assert await upgraded.schema_version() == LATEST_VERSION
    finally:
        await upgraded.close()
