"""WP6 — backoff, failure classification and per-bridge queue isolation."""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter

from bridge.retry import BackoffPolicy, OutboxWorker, PermanentDeliveryError, WorkerPool, classify
from bridge.storage import (
    BridgeStateRepository,
    Database,
    Direction,
    OutboxRepository,
    OutboxState,
)


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


def test_backoff_grows_and_is_capped() -> None:
    policy = BackoffPolicy(initial_ms=1000, max_ms=8000, factor=2.0, jitter=0.0)
    rng = random.Random(0)

    delays = [policy.delay_ms(attempt, rng=rng) for attempt in range(1, 6)]

    assert delays == [1000, 2000, 4000, 8000, 8000], "the cap must hold"


def test_backoff_jitter_spreads_retries() -> None:
    """Without jitter every bridge retries in lockstep — a self-inflicted flood."""
    policy = BackoffPolicy(initial_ms=1000, jitter=0.5)
    rng = random.Random(1)

    delays = {policy.delay_ms(1, rng=rng) for _ in range(20)}

    assert len(delays) > 1
    assert all(500 <= delay <= 1500 for delay in delays)


def test_attempts_are_bounded() -> None:
    policy = BackoffPolicy(max_attempts=3)
    assert policy.exhausted(2) is False
    assert policy.exhausted(3) is True


def test_classification_of_errors() -> None:
    assert classify(TelegramNetworkError(method=None, message="boom")) == (True, None)  # type: ignore[arg-type]
    assert classify(TimeoutError()) == (True, None)
    assert classify(PermanentDeliveryError("nope")) == (False, None)
    # Bad content fails identically forever; retrying only delays the report.
    assert classify(TelegramBadRequest(method=None, message="text too long")) == (False, None)  # type: ignore[arg-type]
    # Telegram named its own wait, so use it verbatim.
    assert classify(TelegramRetryAfter(method=None, message="flood", retry_after=7)) == (True, 7.0)  # type: ignore[arg-type]
    # Anything unrecognised is retried: losing a message is the worse failure.
    assert classify(ValueError("who knows")) == (True, None)


def make_worker(
    database: Database,
    deliver: Any,
    *,
    bridge_name: str = "mom",
    policy: BackoffPolicy | None = None,
) -> OutboxWorker:
    return OutboxWorker(
        bridge_name=bridge_name,
        outbox=OutboxRepository(database),
        state=BridgeStateRepository(database),
        deliver=deliver,
        policy=policy or BackoffPolicy(initial_ms=10, max_ms=20, max_attempts=3, jitter=0.0),
    )


async def test_successful_delivery_clears_the_queue(database: Database) -> None:
    outbox = OutboxRepository(database)
    delivered: list[dict[str, Any]] = []

    async def deliver(
        kind: str, direction: Direction, payload: dict[str, Any], sending: Any = None
    ) -> None:
        delivered.append(payload)

    await outbox.enqueue(
        bridge_name="mom", direction=Direction.TG_TO_MAX, kind="text", payload={"text": "hi"}
    )

    assert await make_worker(database, deliver).drain_once() is True

    assert delivered == [{"text": "hi"}]
    assert await outbox.queue_size("mom") == 0


async def test_temporary_failure_is_delivered_on_the_second_attempt(
    database: Database,
) -> None:
    outbox = OutboxRepository(database)
    attempts: list[int] = []

    async def deliver(
        kind: str, direction: Direction, payload: dict[str, Any], sending: Any = None
    ) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise TelegramNetworkError(method=None, message="hiccup")  # type: ignore[arg-type]

    await outbox.enqueue(bridge_name="mom", direction=Direction.TG_TO_MAX, kind="text", payload={})
    worker = make_worker(database, deliver)

    await worker.drain_once()
    assert await outbox.queue_size("mom") == 1, "still queued after a retryable failure"

    await asyncio.sleep(0.05)  # let the backoff elapse
    await worker.drain_once()

    assert len(attempts) == 2
    assert await outbox.queue_size("mom") == 0


async def test_permanent_failure_does_not_retry(database: Database) -> None:
    outbox = OutboxRepository(database)
    attempts: list[int] = []

    async def deliver(
        kind: str, direction: Direction, payload: dict[str, Any], sending: Any = None
    ) -> None:
        attempts.append(1)
        raise TelegramBadRequest(method=None, message="message is too long")  # type: ignore[arg-type]

    await outbox.enqueue(bridge_name="mom", direction=Direction.TG_TO_MAX, kind="text", payload={})

    await make_worker(database, deliver).drain_once()

    assert len(attempts) == 1
    failed = await outbox.failed("mom")
    assert [item.state for item in failed] == [OutboxState.FAILED]
    assert "permanent" in (failed[0].last_error or "")


async def test_attempts_run_out_and_the_failure_is_visible(database: Database) -> None:
    """No infinite retries: the message ends up in /status, not in a loop."""
    outbox = OutboxRepository(database)
    state = BridgeStateRepository(database)
    attempts: list[int] = []

    async def deliver(
        kind: str, direction: Direction, payload: dict[str, Any], sending: Any = None
    ) -> None:
        attempts.append(1)
        raise TelegramNetworkError(method=None, message="still down")  # type: ignore[arg-type]

    await outbox.enqueue(bridge_name="mom", direction=Direction.TG_TO_MAX, kind="text", payload={})
    worker = make_worker(database, deliver)

    for _ in range(5):
        await worker.drain_once()
        await asyncio.sleep(0.03)

    assert len(attempts) == 3, "max_attempts must cap the retries"
    assert len(await outbox.failed("mom")) == 1
    snapshot = await state.snapshot("mom")
    assert snapshot is not None
    assert "still down" in (snapshot["last_error"] or "")


async def test_rate_limit_uses_the_delay_telegram_named(database: Database) -> None:
    outbox = OutboxRepository(database)

    async def deliver(
        kind: str, direction: Direction, payload: dict[str, Any], sending: Any = None
    ) -> None:
        raise TelegramRetryAfter(method=None, message="flood", retry_after=5)  # type: ignore[arg-type]

    item_id = await outbox.enqueue(
        bridge_name="mom", direction=Direction.TG_TO_MAX, kind="text", payload={}
    )
    await make_worker(database, deliver).drain_once()

    row = await database.query_one("SELECT next_attempt_at FROM outbox WHERE id = ?", (item_id,))
    assert row is not None
    from bridge.storage import now_ms

    # Five seconds, not our own guess of ten milliseconds.
    assert row["next_attempt_at"] - now_ms() > 4_000


async def test_queues_are_isolated_between_bridges(database: Database) -> None:
    """A stuck bridge must not delay a healthy one."""
    outbox = OutboxRepository(database)
    delivered: list[str] = []

    async def deliver_mom(
        kind: str, direction: Direction, payload: dict[str, Any], sending: Any = None
    ) -> None:
        raise TelegramNetworkError(method=None, message="mom is down")  # type: ignore[arg-type]

    async def deliver_dad(
        kind: str, direction: Direction, payload: dict[str, Any], sending: Any = None
    ) -> None:
        delivered.append(payload["text"])

    await outbox.enqueue(
        bridge_name="mom", direction=Direction.TG_TO_MAX, kind="text", payload={"text": "a"}
    )
    await outbox.enqueue(
        bridge_name="dad", direction=Direction.TG_TO_MAX, kind="text", payload={"text": "b"}
    )

    await make_worker(database, deliver_mom, bridge_name="mom").drain_once()
    await make_worker(database, deliver_dad, bridge_name="dad").drain_once()

    assert delivered == ["b"]
    assert await outbox.queue_size("mom") == 1


async def test_worker_loop_picks_up_new_work(database: Database) -> None:
    outbox = OutboxRepository(database)
    delivered: asyncio.Queue[str] = asyncio.Queue()

    async def deliver(
        kind: str, direction: Direction, payload: dict[str, Any], sending: Any = None
    ) -> None:
        await delivered.put(payload["text"])

    pool = WorkerPool()
    worker = make_worker(database, deliver)
    pool.add(worker)
    try:
        await outbox.enqueue(
            bridge_name="mom", direction=Direction.TG_TO_MAX, kind="text", payload={"text": "x"}
        )
        pool.wake("mom")

        assert await asyncio.wait_for(delivered.get(), timeout=2) == "x"
    finally:
        await pool.close()

    assert worker.is_running is False


async def test_pool_shares_one_media_budget() -> None:
    """Bandwidth and temp space are finite even though queues are not shared."""
    pool = WorkerPool(media_concurrency=1)

    async with pool.media_slots:
        assert pool.media_slots.locked() is True

    assert pool.media_slots.locked() is False


async def test_cancelled_item_returns_to_the_queue(database: Database) -> None:
    """Shutdown mid-flight must not lose the message."""
    outbox = OutboxRepository(database)

    async def deliver(
        kind: str, direction: Direction, payload: dict[str, Any], sending: Any = None
    ) -> None:
        raise asyncio.CancelledError

    await outbox.enqueue(bridge_name="mom", direction=Direction.TG_TO_MAX, kind="text", payload={})

    with pytest.raises(asyncio.CancelledError):
        await make_worker(database, deliver).drain_once()

    assert await outbox.queue_size("mom") == 1
