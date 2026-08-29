"""The bridge must notice that it cannot write, and stop pretending otherwise.

`systemd` said `active` throughout the outage. So did every existing health
check, because none of them wrote anything: a connection that cannot close a
transaction answers `SELECT 1` perfectly well. What was missing was a check that
does what the bridge does — begin, write, read back, commit — and a rule that a
process which cannot do that has no business accepting the owner's messages.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from bridge.service.health import DatabaseWrite, HealthService
from bridge.storage import (
    AlertRepository,
    Database,
    HealthStateRepository,
    MessageMapRepository,
    OutboxRepository,
    TelegramInboxRepository,
)
from bridge.storage.database import DatabaseUnavailableError


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


def make_service(database: Database) -> HealthService:
    return HealthService(
        health=HealthStateRepository(database),
        outbox=OutboxRepository(database),
        inbox=TelegramInboxRepository(database),
        alerts=AlertRepository(database),
        db_path=Path(database.path),
        database=database,
        bridges=lambda: [("dad", True)],
        max_is_ready=lambda: True,
    )


def break_writes(database: Database) -> Any:
    original = database._connection.execute

    def refuse(sql: str, *args: Any, **kwargs: Any) -> Any:
        # Not `async def`: the wrapper both awaits this and uses it as an async
        # context manager, and only the original object supports both.
        if sql.startswith("BEGIN"):
            raise sqlite3.OperationalError("disk I/O error")
        return original(sql, *args, **kwargs)

    database._connection.execute = refuse  # type: ignore[method-assign]
    return original


def mend_writes(database: Database, original: Any) -> None:
    database._connection.execute = original  # type: ignore[method-assign]


# ------------------------------------------------------------------ detection


async def test_a_healthy_bridge_says_so(database: Database) -> None:
    service = make_service(database)
    await service.snapshot()  # first canary
    snapshot = await service.snapshot()  # second: settled
    assert snapshot.database_write.healthy
    assert snapshot.database_write.last_ok_ms > 0


async def test_a_broken_write_path_is_seen(database: Database) -> None:
    """`SELECT 1` passed all the way through the outage. This does not."""
    service = make_service(database)
    original = break_writes(database)
    try:
        snapshot = await service.snapshot()
        assert not snapshot.database_write.healthy
        assert snapshot.database_write.failures > 0
    finally:
        mend_writes(database, original)


async def test_it_opens_one_incident_however_long_it_lasts(database: Database) -> None:
    """Recording an incident is itself a write, so the one incident about writes
    being broken is the one that may not be recordable. What must not happen is
    the health loop dying, and what must happen is exactly one incident once
    writing works again."""
    service = make_service(database)
    broken = DatabaseWrite(healthy=False, reason="disk I/O error")

    opened = 0
    for _ in range(20):
        snapshot = replace(await service.snapshot(), database_write=broken)
        if "database-write-unavailable" in await service.evaluate(snapshot):
            opened += 1
    assert opened == 1

    rows = await database.query(
        "SELECT incident_key FROM alert_incidents WHERE resolved_at IS NULL"
    )
    assert [row["incident_key"] for row in rows] == ["database-write-unavailable"]


async def test_the_health_loop_survives_an_outage_it_cannot_record(
    database: Database,
) -> None:
    service = make_service(database)
    snapshot = replace(
        await service.snapshot(), database_write=DatabaseWrite(healthy=False)
    )
    original = break_writes(database)
    try:
        await service.evaluate(snapshot)  # must not raise
    finally:
        mend_writes(database, original)


async def test_recovery_needs_two_canaries_before_it_says_fixed(
    database: Database,
) -> None:
    """One success after a failure is a coincidence as often as a recovery, and
    telling the owner it is fixed twice is worse than telling them once, late."""
    service = make_service(database)
    original = break_writes(database)
    try:
        assert await database.write_canary() is False  # the counter resets on the next read
    finally:
        mend_writes(database, original)
    broken = await service.snapshot()
    await service.evaluate(replace(broken, database_write=DatabaseWrite(healthy=False)))

    first = broken
    assert first.database_write.recovering and not first.database_write.healthy
    assert "database-write-unavailable:resolved" not in await service.evaluate(first)

    second = await service.snapshot()
    assert second.database_write.healthy
    assert "database-write-unavailable:resolved" in await service.evaluate(second)


async def test_the_owner_is_told_what_still_works(database: Database) -> None:
    service = make_service(database)
    await service.evaluate(
        replace(await service.snapshot(), database_write=DatabaseWrite(healthy=False))
    )

    rows = await database.query("SELECT text FROM alert_outbox ORDER BY id DESC LIMIT 1")
    body = rows[0]["text"].lower()
    assert "этот чат работает" in body
    assert "восстановить нельзя" in body  # no promise about what was already lost


async def test_the_incident_carries_no_sql(database: Database) -> None:
    service = make_service(database)
    original = break_writes(database)
    try:
        snapshot = await service.snapshot()
    finally:
        mend_writes(database, original)
    assert "SELECT" not in snapshot.database_write.reason
    assert "INSERT" not in snapshot.database_write.reason


async def test_health_without_a_connection_is_not_broken(tmp_path: Path) -> None:
    """The setup CLI and most tests build health with no connection behind it."""
    db = await Database.connect(tmp_path / "b.db")
    try:
        service = HealthService(
            health=HealthStateRepository(db),
            outbox=OutboxRepository(db),
            inbox=TelegramInboxRepository(db),
            alerts=AlertRepository(db),
            db_path=Path(db.path),
            bridges=lambda: [],
            max_is_ready=lambda: True,
        )
        snapshot = await service.snapshot()
        assert snapshot.database_write.healthy
        assert "database-write-unavailable" not in await service.evaluate(snapshot)
    finally:
        await db.close()


# ---------------------------------------------------------------- fail closed


async def test_an_owner_message_is_refused_rather_than_lost_quietly(
    database: Database,
) -> None:
    """There is no owner inbox before V18, so an update accepted while the first
    durable write cannot happen is an update lost. Reporting it as carried would
    be the worse half of that."""
    database._poison("test")
    messages = MessageMapRepository(database)

    with pytest.raises(DatabaseUnavailableError):
        await messages.record_from_telegram(
            bridge_name="mom",
            max_chat_id=555,
            telegram_bot_id=1,
            telegram_chat_id=2,
            telegram_message_id=None,
            telegram_owner_message_id=3,
            telegram_owner_account_id=4,
        )


async def test_a_worker_cannot_claim_while_the_database_is_unusable(
    database: Database,
) -> None:
    """A claim is a transaction. Refusing it is what stops a job being taken and
    then settled into a transaction nothing will commit."""
    outbox = OutboxRepository(database)
    database._poison("test")

    with pytest.raises(DatabaseUnavailableError):
        await outbox.claim_for_attempt(
            bridge_name="mom",
            direction="tg_to_max",
            kind="tg_to_max_text",
            payload={},
            source_key="k",
        )


async def test_reads_are_refused_too_so_nothing_decides_on_stale_state(
    database: Database,
) -> None:
    database._poison("test")
    with pytest.raises(DatabaseUnavailableError):
        await database.query("SELECT 1")


async def test_a_burst_of_owner_updates_during_an_outage_produces_no_effect(
    database: Database,
) -> None:
    """The seven that were lost. What must not happen is that they look carried."""
    messages = MessageMapRepository(database)
    database._poison("test")

    refused = 0
    for owner_message_id in range(7):
        try:
            await messages.record_from_telegram(
                bridge_name="mom", max_chat_id=555, telegram_bot_id=1,
                telegram_chat_id=2, telegram_message_id=None,
                telegram_owner_message_id=owner_message_id,
                telegram_owner_account_id=4,
            )
        except DatabaseUnavailableError:
            refused += 1

    assert refused == 7
    assert await database.recover()
    assert await database.query("SELECT id FROM message_map") == []
