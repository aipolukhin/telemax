"""The MAX→Telegram creating boundary — the half of the rule that was missing.

`reclaim_expired_leases` has always read `send_started_at` and answered
AMBIGUOUS for any kind, so a process that *died* mid-send was judged carefully.
The live failure paths asked `is_creating(kind)`, and no MAX→TG kind was in the
set — so a 60-second aiohttp timeout, which covers the body upload and therefore
arrives *after* Telegram may have accepted a large album, answered RETRY and the
worker sent the message again.

One MAX event, two messages in the owner's chat. These tests are what stops the
set shrinking back.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from bridge.routing.delivery import (
    KIND_MAX_TO_TG_MEDIA,
    KIND_MAX_TO_TG_OWNER,
    KIND_MAX_TO_TG_TEXT,
    KIND_OWNER_ECHO_BIND,
    KIND_TG_ALBUM_SWEEP,
    KIND_TG_TO_MAX_DELETE,
    KIND_TG_TO_MAX_EDIT,
    KIND_TG_TO_MAX_REACTION,
    DeliveryPipe,
    UnconfirmedDeliveryError,
)
from bridge.routing.owner_voice import OwnerTransportUnavailableError, PartialOwnerAlbumError
from bridge.routing.settlement import Verdict, is_creating, settle
from bridge.storage import (
    BridgeStateRepository,
    Database,
    Direction,
    OutboxRepository,
    OutboxState,
)
from bridge.telegram.errors import TelegramTransportUnavailableError

BRIDGE = "mum"

#: The three MAX→Telegram kinds, all of which create a message somebody reads.
MAX_TO_TG = [KIND_MAX_TO_TG_TEXT, KIND_MAX_TO_TG_MEDIA, KIND_MAX_TO_TG_OWNER]

#: Everything the transport can do that does not amount to an answer.
SILENCE: list[BaseException] = [
    TelegramNetworkError(method=None, message="Request timeout error"),  # type: ignore[arg-type]
    TelegramNetworkError(method=None, message="ClientOSError: connection reset"),  # type: ignore[arg-type]
    TelegramServerError(method=None, message="Bad Gateway"),  # type: ignore[arg-type]
    TimeoutError("no answer"),
    ConnectionResetError("reset"),
    OSError("broken pipe"),
    asyncio.CancelledError(),
    RuntimeError("something nobody has seen"),
]
SILENCE_IDS = [
    "timeout",
    "reset",
    "5xx",
    "TimeoutError",
    "ConnectionReset",
    "OSError",
    "cancelled",
    "unknown",
]


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


# --------------------------------------------------------------------- the policy


@pytest.mark.parametrize("kind", MAX_TO_TG)
def test_every_max_to_tg_send_is_a_creating_kind(kind: str) -> None:
    assert is_creating(kind)


@pytest.mark.parametrize("kind", MAX_TO_TG)
@pytest.mark.parametrize("error", SILENCE, ids=SILENCE_IDS)
def test_silence_past_the_mark_is_a_question(kind: str, error: BaseException) -> None:
    assert settle(kind, error, remote_marked=True).verdict is Verdict.AMBIGUOUS


@pytest.mark.parametrize("kind", MAX_TO_TG)
@pytest.mark.parametrize("error", SILENCE, ids=SILENCE_IDS)
def test_the_same_silence_before_the_mark_is_a_retry(kind: str, error: BaseException) -> None:
    assert settle(kind, error, remote_marked=False).verdict is Verdict.RETRY


@pytest.mark.parametrize("kind", MAX_TO_TG)
def test_a_confirmed_refusal_is_never_a_question(kind: str) -> None:
    """Telegram answered, so nothing was created however the mark reads."""
    error = TelegramBadRequest(method=None, message="Bad Request: chat not found")  # type: ignore[arg-type]
    assert settle(kind, error, remote_marked=True).verdict is Verdict.PERMANENT


@pytest.mark.parametrize("kind", MAX_TO_TG)
def test_a_rate_limit_is_a_retry_that_names_its_own_wait(kind: str) -> None:
    error = TelegramRetryAfter(method=None, message="flood", retry_after=30)  # type: ignore[arg-type]
    verdict = settle(kind, error, remote_marked=True)
    assert verdict.verdict is Verdict.RETRY
    assert verdict.delay_ms == 30_000


@pytest.mark.parametrize("kind", MAX_TO_TG)
def test_an_answer_without_an_id_is_a_question(kind: str) -> None:
    error = UnconfirmedDeliveryError("Telegram accepted the message and named no id")
    assert settle(kind, error, remote_marked=True).verdict is Verdict.AMBIGUOUS
    assert settle(kind, error, remote_marked=False).verdict is Verdict.AMBIGUOUS


def test_a_half_placed_owner_album_is_a_question() -> None:
    """Its own docstring promised AMBIGUOUS; the policy answered RETRY."""
    error = PartialOwnerAlbumError("part 3 of 5; 2 part(s) are already in the chat")
    assert settle(KIND_MAX_TO_TG_OWNER, error, remote_marked=True).verdict is Verdict.AMBIGUOUS


@pytest.mark.parametrize("kind", MAX_TO_TG)
@pytest.mark.parametrize(
    "error",
    [
        OwnerTransportUnavailableError("the owner's Telegram session is not connected"),
        TelegramTransportUnavailableError("bot 1 is no longer registered"),
    ],
    ids=["owner-session", "bot"],
)
def test_a_transport_that_was_never_there_is_a_retry(kind: str, error: BaseException) -> None:
    """The media hook fires before the transport is resolved.

    Without this clause an owner session that had simply gone away became a
    question about a message that never existed.
    """
    assert settle(kind, error, remote_marked=True).verdict is Verdict.RETRY


@pytest.mark.parametrize(
    "kind",
    [
        KIND_TG_TO_MAX_EDIT,
        KIND_TG_TO_MAX_DELETE,
        KIND_TG_TO_MAX_REACTION,
        KIND_OWNER_ECHO_BIND,
        KIND_TG_ALBUM_SWEEP,
    ],
)
def test_mutations_are_still_not_creating(kind: str) -> None:
    """Widening the set must not sweep the idempotent kinds in with it."""
    assert not is_creating(kind)
    for error in (asyncio.CancelledError(), TimeoutError("x")):
        assert settle(kind, error, remote_marked=True).verdict is Verdict.RETRY


# ------------------------------------------------------- the policy, through the queue


async def _job(outbox: OutboxRepository, kind: str, key: str) -> int:
    job_id, ours = await outbox.claim_for_attempt(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=kind,
        payload={"bot_id": 1, "chat_id": 2, "text": "hello"},
        source_key=key,
    )
    assert ours
    return job_id


def _sender(error: BaseException, sent: list[str]) -> Any:
    async def deliver(
        kind: str, direction: Direction, payload: dict[str, Any], sending: Any
    ) -> int:
        await sending()
        # The content is in the chat by the time the client gives up on it.
        sent.append(kind)
        raise error

    return deliver


@pytest.mark.parametrize("kind", MAX_TO_TG)
@pytest.mark.asyncio
async def test_the_inline_path_stops_at_one_message(kind: str, database: Database) -> None:
    outbox = OutboxRepository(database)
    sent: list[str] = []
    pipe = DeliveryPipe(
        outbox=outbox,
        send=_sender(TelegramNetworkError(method=None, message="Request timeout error"), sent),  # type: ignore[arg-type]
    )
    job_id = await _job(outbox, kind, f"max:1:{kind}")
    settled = await pipe.attempt(
        job_id=job_id,
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=kind,
        payload={"bot_id": 1, "chat_id": 2, "text": "hello"},
    )
    assert settled.ambiguous
    row = await database.query_one("SELECT state FROM outbox WHERE id = ?", (job_id,))
    assert row["state"] == OutboxState.AMBIGUOUS.value
    assert len(sent) == 1


@pytest.mark.parametrize("kind", MAX_TO_TG)
@pytest.mark.asyncio
async def test_the_worker_never_carries_an_ambiguous_job_further(
    kind: str, database: Database
) -> None:
    """The duplicate this whole change exists to stop, end to end."""
    from bridge.retry.worker import OutboxWorker

    outbox = OutboxRepository(database)
    sent: list[str] = []
    deliver = _sender(TelegramNetworkError(method=None, message="Request timeout error"), sent)  # type: ignore[arg-type]
    pipe = DeliveryPipe(outbox=outbox, send=deliver)
    job_id = await _job(outbox, kind, f"max:2:{kind}")
    await pipe.attempt(
        job_id=job_id,
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=kind,
        payload={"bot_id": 1, "chat_id": 2, "text": "hello"},
    )

    worker = OutboxWorker(
        bridge_name=BRIDGE,
        outbox=outbox,
        state=BridgeStateRepository(database),
        deliver=deliver,
    )
    await database.execute("UPDATE outbox SET next_attempt_at = 0 WHERE id = ?", (job_id,))
    assert not await worker.drain_once(), "an ambiguous job must not be claimable"
    assert len(sent) == 1, "the worker put a second copy in the chat"


@pytest.mark.parametrize("kind", MAX_TO_TG)
@pytest.mark.asyncio
async def test_inline_and_worker_reach_the_same_state(kind: str, database: Database) -> None:
    from bridge.retry.worker import OutboxWorker

    outbox = OutboxRepository(database)
    error = TelegramNetworkError(method=None, message="Request timeout error")  # type: ignore[arg-type]

    inline_id = await _job(outbox, kind, f"inline:{kind}")
    await DeliveryPipe(outbox=outbox, send=_sender(error, [])).attempt(
        job_id=inline_id,
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=kind,
        payload={},
    )

    worker_id = await _job(outbox, kind, f"worker:{kind}")
    await outbox.mark_retry(worker_id, delay_ms=0, error="reset for the worker")
    worker = OutboxWorker(
        bridge_name=BRIDGE,
        outbox=outbox,
        state=BridgeStateRepository(database),
        deliver=_sender(error, []),
    )
    await database.execute("UPDATE outbox SET next_attempt_at = 0 WHERE id = ?", (worker_id,))
    await worker.drain_once()

    rows = await database.query(
        "SELECT id, state FROM outbox WHERE id IN (?, ?)", (inline_id, worker_id)
    )
    states = {row["state"] for row in rows}
    assert states == {OutboxState.AMBIGUOUS.value}, states


@pytest.mark.parametrize("kind", MAX_TO_TG)
@pytest.mark.asyncio
async def test_a_killed_process_is_judged_the_same_way(kind: str, database: Database) -> None:
    """The path that was already right, kept beside the one that was not."""
    outbox = OutboxRepository(database)
    job_id = await _job(outbox, kind, f"crash:{kind}")
    await outbox.mark_sending(job_id)
    requeued, ambiguous = await outbox.requeue_inflight()
    assert (requeued, ambiguous) == (0, 1)
    row = await database.query_one("SELECT state FROM outbox WHERE id = ?", (job_id,))
    assert row["state"] == OutboxState.AMBIGUOUS.value


@pytest.mark.parametrize("kind", MAX_TO_TG)
@pytest.mark.asyncio
async def test_a_crash_before_the_mark_is_still_only_a_retry(
    kind: str, database: Database
) -> None:
    outbox = OutboxRepository(database)
    job_id = await _job(outbox, kind, f"early:{kind}")
    requeued, ambiguous = await outbox.requeue_inflight()
    assert (requeued, ambiguous) == (1, 0)
    row = await database.query_one("SELECT state FROM outbox WHERE id = ?", (job_id,))
    assert row["state"] == OutboxState.PENDING.value
