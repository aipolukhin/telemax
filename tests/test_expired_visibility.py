"""A job that gave up after a day is a thing the owner is told about.

It was not. `expire_overdue` cleared the payload, set the state and said nothing
else: no incident, no `/failed` entry, and the only alert that might have caught
it beforehand fires on queue *depth*, so one stuck job never reached it. The
message was neither delivered nor mentioned anywhere.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from bridge.routing.delivery import KIND_MAX_TO_TG_TEXT
from bridge.storage import Database, Direction, OutboxRepository, OutboxState

BRIDGE = "mum"


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


def _health(database: Database, outbox: OutboxRepository) -> Any:
    from bridge.service.health import HealthService
    from bridge.storage import AlertRepository, HealthStateRepository, TelegramInboxRepository

    return HealthService(
        health=HealthStateRepository(database),
        outbox=outbox,
        inbox=TelegramInboxRepository(database),
        alerts=AlertRepository(database),
        db_path=Path(database.path),
        bridges=lambda: [(BRIDGE, True)],
    )


async def _expired(outbox: OutboxRepository, database: Database, key: str = "max:1:1") -> int:
    job_id = await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_TEXT,
        payload={"bot_id": 1, "chat_id": 2, "text": "hello"},
        source_key=key,
    )
    await database.execute("UPDATE outbox SET expires_at = 1 WHERE id = ?", (job_id,))
    assert await outbox.expire_overdue() == 1
    return job_id


@pytest.mark.asyncio
async def test_an_expired_job_reaches_the_owners_attention(database: Database) -> None:
    outbox = OutboxRepository(database)
    job_id = await _expired(outbox, database)

    waiting = await outbox.needing_attention(BRIDGE)
    assert [item.id for item in waiting] == [job_id]
    assert waiting[0].state is OutboxState.EXPIRED


@pytest.mark.asyncio
async def test_an_expired_job_still_says_what_it_was(database: Database) -> None:
    """The payload goes — it is the private half — but not the identity."""
    outbox = OutboxRepository(database)
    job_id = await _expired(outbox, database, key="max:9:9")

    row = await database.query_one("SELECT * FROM outbox WHERE id = ?", (job_id,))
    assert row is not None
    assert row["kind"] == KIND_MAX_TO_TG_TEXT
    assert row["source_key"] == "max:9:9"
    assert row["direction"] == Direction.MAX_TO_TG.value
    assert row["bridge_name"] == BRIDGE
    assert "ttl expired" in row["last_error"]
    assert row["payload_json"] == "{}", "content at rest goes with the give-up"


@pytest.mark.asyncio
async def test_an_expired_job_can_be_archived_but_not_retried(database: Database) -> None:
    """Archive-only, and explicitly: there is nothing left to send it from."""
    outbox = OutboxRepository(database)
    job_id = await _expired(outbox, database)

    assert not await outbox.retry_now(job_id)
    assert await outbox.archive(job_id, reason="ttl")
    assert await outbox.needing_attention(BRIDGE) == []


@pytest.mark.asyncio
async def test_the_health_snapshot_counts_it(database: Database) -> None:
    outbox = OutboxRepository(database)
    await _expired(outbox, database)

    snapshot = await _health(database, outbox).snapshot()
    assert snapshot.outbox_expired == 1
    assert snapshot.bridges[0].expired == 1


@pytest.mark.asyncio
async def test_the_incident_is_raised_and_cleared(database: Database) -> None:
    outbox = OutboxRepository(database)
    job_id = await _expired(outbox, database)
    health = _health(database, outbox)

    touched = await health.evaluate(await health.snapshot())
    assert "delivery-expired" in touched

    await outbox.archive(job_id, reason="looked at")
    touched = await health.evaluate(await health.snapshot())
    assert "delivery-expired:resolved" in touched
