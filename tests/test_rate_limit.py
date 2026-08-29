"""A rate limit is the server saying "not yet", never "no".

Two things were wrong and they compounded. The inline path put every failure
back with `delay_ms=0`, so a 429 was followed immediately by the same request
walking into the same limit; and every one of those attempts spent one of the
job's twelve, so a busy minute could carry a perfectly deliverable message all
the way to FAILED and put it in front of the owner as something to decide.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from telethon.errors import FloodWaitError

from bridge.retry.backoff import DEFAULT_POLICY
from bridge.retry.worker import OutboxWorker
from bridge.routing.delivery import KIND_MAX_TO_TG_TEXT, KIND_TG_TO_MAX_TEXT, DeliveryPipe
from bridge.routing.settlement import MIN_RATE_LIMIT_MS, Verdict, settle
from bridge.storage import (
    BridgeStateRepository,
    Database,
    Direction,
    OutboxRepository,
    OutboxState,
)

BRIDGE = "mum"


def retry_after(seconds: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(method=None, message="flood", retry_after=seconds)  # type: ignore[arg-type]


def flood_wait(seconds: int) -> FloodWaitError:
    return FloodWaitError(request=None, capture=seconds)


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


# ------------------------------------------------------------------- the policy


@pytest.mark.parametrize("kind", [KIND_MAX_TO_TG_TEXT, KIND_TG_TO_MAX_TEXT])
def test_a_bot_api_rate_limit_costs_no_attempt(kind: str) -> None:
    verdict = settle(kind, retry_after(30), remote_marked=True)
    assert verdict.verdict is Verdict.RETRY
    assert verdict.delay_ms == 30_000
    assert not verdict.costs_attempt


@pytest.mark.parametrize("kind", [KIND_MAX_TO_TG_TEXT, KIND_TG_TO_MAX_TEXT])
def test_an_mtproto_flood_wait_is_the_same_thing(kind: str) -> None:
    """One classifier, so the owner's session and the bot are priced alike."""
    verdict = settle(kind, flood_wait(45), remote_marked=True)
    assert verdict.verdict is Verdict.RETRY
    assert verdict.delay_ms == 45_000
    assert not verdict.costs_attempt


def test_a_long_mtproto_flood_wait_keeps_its_seconds() -> None:
    """An hour rounded down to a guess is how a queue walks into the wall again."""
    assert settle(KIND_MAX_TO_TG_TEXT, flood_wait(3600), remote_marked=True).delay_ms == 3_600_000


def test_a_zero_wait_is_still_a_wait() -> None:
    """`retry_after: 0` must not become a tight loop."""
    verdict = settle(KIND_MAX_TO_TG_TEXT, retry_after(0), remote_marked=True)
    assert verdict.delay_ms == MIN_RATE_LIMIT_MS


def test_an_ordinary_failure_still_costs_an_attempt() -> None:
    error = TelegramNetworkError(method=None, message="Request timeout error")  # type: ignore[arg-type]
    assert settle(KIND_MAX_TO_TG_TEXT, error, remote_marked=False).costs_attempt


# ---------------------------------------------------------------- through the queue


def _flooding(error: BaseException) -> Any:
    async def deliver(
        kind: str, direction: Direction, payload: dict[str, Any], sending: Any
    ) -> int:
        await sending()
        raise error

    return deliver


async def _job(outbox: OutboxRepository, key: str) -> int:
    job_id, ours = await outbox.claim_for_attempt(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_TEXT,
        payload={"bot_id": 1, "chat_id": 2, "text": "hi"},
        source_key=key,
    )
    assert ours
    return job_id


@pytest.mark.asyncio
async def test_the_inline_path_waits_the_time_the_server_named(database: Database) -> None:
    outbox = OutboxRepository(database)
    pipe = DeliveryPipe(outbox=outbox, send=_flooding(retry_after(30)))
    job_id = await _job(outbox, "inline")
    await pipe.attempt(
        job_id=job_id,
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_TEXT,
        payload={},
    )
    row = await database.query_one(
        "SELECT state, attempts, next_attempt_at - updated_at AS wait FROM outbox WHERE id = ?",
        (job_id,),
    )
    assert row["state"] == OutboxState.PENDING.value
    assert row["attempts"] == 0, "a rate limit is not a failed attempt"
    assert row["wait"] >= 30_000, f"inline scheduled the retry {row['wait']}ms out"


@pytest.mark.asyncio
async def test_the_worker_agrees_with_the_inline_path(database: Database) -> None:
    outbox = OutboxRepository(database)
    worker = OutboxWorker(
        bridge_name=BRIDGE,
        outbox=outbox,
        state=BridgeStateRepository(database),
        deliver=_flooding(retry_after(30)),
    )
    job_id = await _job(outbox, "worker")
    await outbox.mark_retry(job_id, delay_ms=0, error="reset", costs_attempt=False)
    await database.execute("UPDATE outbox SET next_attempt_at = 0 WHERE id = ?", (job_id,))
    await worker.drain_once()
    row = await database.query_one(
        "SELECT state, attempts, next_attempt_at - updated_at AS wait FROM outbox WHERE id = ?",
        (job_id,),
    )
    assert row["state"] == OutboxState.PENDING.value
    assert row["attempts"] == 0
    assert row["wait"] >= 30_000


@pytest.mark.asyncio
async def test_a_wall_of_rate_limits_never_reaches_failed(database: Database) -> None:
    """The budget is for refusals. A limit is not one."""
    outbox = OutboxRepository(database)
    worker = OutboxWorker(
        bridge_name=BRIDGE,
        outbox=outbox,
        state=BridgeStateRepository(database),
        deliver=_flooding(retry_after(5)),
    )
    job_id = await _job(outbox, "wall")
    await outbox.mark_retry(job_id, delay_ms=0, error="reset", costs_attempt=False)
    for _ in range(DEFAULT_POLICY.max_attempts + 3):
        await database.execute("UPDATE outbox SET next_attempt_at = 0 WHERE id = ?", (job_id,))
        await worker.drain_once()
    row = await database.query_one(
        "SELECT state, attempts FROM outbox WHERE id = ?", (job_id,)
    )
    assert row["state"] == OutboxState.PENDING.value
    assert row["attempts"] == 0


@pytest.mark.asyncio
async def test_an_ordinary_failure_still_walks_toward_failed(database: Database) -> None:
    """The other half: nothing here weakened the ordinary budget."""
    outbox = OutboxRepository(database)
    worker = OutboxWorker(
        bridge_name=BRIDGE,
        outbox=outbox,
        state=BridgeStateRepository(database),
        deliver=_flooding(
            TelegramNetworkError(method=None, message="Request timeout error")  # type: ignore[arg-type]
        ),
    )
    job_id, _ = await outbox.claim_for_attempt(
        bridge_name=BRIDGE,
        direction=Direction.TG_TO_MAX,
        # An idempotent mutation, so silence stays a retry and the budget is what
        # eventually stops it.
        kind="tg_to_max_edit",
        payload={},
        source_key="ordinary",
    )
    await outbox.mark_retry(job_id, delay_ms=0, error="reset")
    for _ in range(DEFAULT_POLICY.max_attempts + 2):
        await database.execute("UPDATE outbox SET next_attempt_at = 0 WHERE id = ?", (job_id,))
        await worker.drain_once()
    row = await database.query_one(
        "SELECT state, attempts FROM outbox WHERE id = ?", (job_id,)
    )
    assert row["state"] == OutboxState.FAILED.value
    assert row["attempts"] >= DEFAULT_POLICY.max_attempts


@pytest.mark.asyncio
async def test_a_rate_limited_job_is_not_claimable_before_its_due_time(
    database: Database,
) -> None:
    """No tight loop: the worker finds nothing until the wait is over."""
    outbox = OutboxRepository(database)
    calls: list[int] = []

    async def deliver(
        kind: str, direction: Direction, payload: dict[str, Any], sending: Any
    ) -> int:
        await sending()
        calls.append(1)
        raise retry_after(30)

    worker = OutboxWorker(
        bridge_name=BRIDGE,
        outbox=outbox,
        state=BridgeStateRepository(database),
        deliver=deliver,
    )
    job_id = await _job(outbox, "due")
    await outbox.mark_retry(job_id, delay_ms=0, error="reset", costs_attempt=False)
    await database.execute("UPDATE outbox SET next_attempt_at = 0 WHERE id = ?", (job_id,))
    await worker.drain_once()
    assert len(calls) == 1
    for _ in range(5):
        assert not await worker.drain_once()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cancellation_is_not_priced_as_a_rate_limit(database: Database) -> None:
    """The free pass is for 429 and nothing else."""
    outbox = OutboxRepository(database)

    async def deliver(
        kind: str, direction: Direction, payload: dict[str, Any], sending: Any
    ) -> int:
        raise asyncio.CancelledError

    worker = OutboxWorker(
        bridge_name=BRIDGE,
        outbox=outbox,
        state=BridgeStateRepository(database),
        deliver=deliver,
    )
    job_id = await _job(outbox, "cancelled")
    await outbox.mark_retry(job_id, delay_ms=0, error="reset")
    await database.execute("UPDATE outbox SET next_attempt_at = 0 WHERE id = ?", (job_id,))
    with pytest.raises(asyncio.CancelledError):
        await worker.drain_once()
    row = await database.query_one("SELECT state FROM outbox WHERE id = ?", (job_id,))
    assert row["state"] == OutboxState.PENDING.value
