"""AU-3 G2/G8 — one failure means one thing, whichever sender hit it.

A job can be attempted twice over: inline by the router, so the owner's message
goes out now, and by the worker for everything the inline path did not finish.
They call the same `send_job` and used to disagree about what its failures meant.

The disagreement had a shape worth stating. Crash recovery reads
`send_started_at` and correctly turns "died after the remote call began" into
AMBIGUOUS. The live paths never read it — every exception became `mark_retry`,
and the worker sent the message again. So `kill -9` was handled *better* than
`systemctl stop`: the hard kill has no chance to overwrite the mark, and the
clean one did exactly that on its way out.

Not a blanket rule. Proof beats the mark: a chat that refused us is an answer,
and there is nothing to ask the owner about.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from pymax.exceptions import ApiError

from bridge.max_client import MaxClientError, MaxUnconfirmedSendError
from bridge.retry.worker import OutboxWorker, PermanentDeliveryError
from bridge.routing.delivery import (
    KIND_TG_TO_MAX_DELETE,
    KIND_TG_TO_MAX_EDIT,
    KIND_TG_TO_MAX_TEXT,
    DeferDelivery,
    DeliveryPipe,
    UnconfirmedDeliveryError,
)
from bridge.routing.settlement import Verdict, is_creating, settle
from bridge.storage import (
    BridgeStateRepository,
    Database,
    Direction,
    OutboxRepository,
    OutboxState,
)

BRIDGE = "dad"


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


# ------------------------------------------------------------------ the policy


def test_creating_kinds_are_named_once() -> None:
    """The question "would a retry duplicate this?" has one answer per kind."""
    assert is_creating(KIND_TG_TO_MAX_TEXT)
    assert not is_creating(KIND_TG_TO_MAX_EDIT)
    assert not is_creating(KIND_TG_TO_MAX_DELETE)


@pytest.mark.parametrize("marked", [True, False])
def test_an_unconfirmed_send_is_ambiguous_either_way(marked: bool) -> None:
    """Already decided by the layer that made the call; the mark adds nothing."""
    for error in (UnconfirmedDeliveryError("x"), MaxUnconfirmedSendError("x")):
        assert settle(KIND_TG_TO_MAX_TEXT, error, remote_marked=marked).verdict is (
            Verdict.AMBIGUOUS
        )


def test_a_permanent_error_beats_the_mark() -> None:
    assert settle(
        KIND_TG_TO_MAX_TEXT, PermanentDeliveryError("gone"), remote_marked=True
    ).verdict is Verdict.PERMANENT


def test_a_deferral_costs_nothing() -> None:
    verdict = settle(KIND_TG_TO_MAX_TEXT, DeferDelivery(200), remote_marked=True)
    assert verdict.verdict is Verdict.DEFER
    assert verdict.delay_ms == 200


def test_a_proven_pre_write_failure_stays_retryable_past_the_mark() -> None:
    """`MaxClientError` comes from the session check that runs before the frame is
    built. The mark says "we were about to"; this says "and then we did not"."""
    assert settle(
        KIND_TG_TO_MAX_TEXT, MaxClientError("not connected"), remote_marked=True
    ).verdict is Verdict.RETRY


def test_a_confirmed_api_error_does_not_become_a_question() -> None:
    """The server answered, so the message was not created — even though the mark
    is set. Turning this into AMBIGUOUS would ask the owner about nothing.

    A blocked chat is terminal rather than retried, which is the classifier's
    call; what matters here is only that it is never a question."""
    blocked = ApiError(opcode=64, error="chat.blocked", message="no")
    assert settle(KIND_TG_TO_MAX_TEXT, blocked, remote_marked=True).verdict is Verdict.PERMANENT

    unknown = ApiError(opcode=64, error="something.new", message="?")
    assert settle(KIND_TG_TO_MAX_TEXT, unknown, remote_marked=True).verdict is Verdict.RETRY


@pytest.mark.parametrize(
    "error", [TimeoutError("x"), ConnectionError("x"), OSError("x"), RuntimeError("x")]
)
def test_an_unknown_failure_past_the_mark_is_ambiguous(error: Exception) -> None:
    assert settle(KIND_TG_TO_MAX_TEXT, error, remote_marked=True).verdict is Verdict.AMBIGUOUS


@pytest.mark.parametrize(
    "error", [TimeoutError("x"), ConnectionError("x"), OSError("x"), RuntimeError("x")]
)
def test_the_same_failure_before_the_mark_is_a_retry(error: Exception) -> None:
    assert settle(KIND_TG_TO_MAX_TEXT, error, remote_marked=False).verdict is Verdict.RETRY


def test_cancellation_past_the_mark_is_ambiguous_for_a_creating_kind() -> None:
    assert settle(
        KIND_TG_TO_MAX_TEXT, asyncio.CancelledError(), remote_marked=True
    ).verdict is Verdict.AMBIGUOUS


def test_cancellation_before_the_mark_is_a_retry() -> None:
    assert settle(
        KIND_TG_TO_MAX_TEXT, asyncio.CancelledError(), remote_marked=False
    ).verdict is Verdict.RETRY


@pytest.mark.parametrize("kind", [KIND_TG_TO_MAX_EDIT, KIND_TG_TO_MAX_DELETE])
def test_an_idempotent_mutation_stays_retryable_whatever_happens(kind: str) -> None:
    """Editing to the same text and deleting a gone message are both no-ops, so a
    retry is free and a question would be noise."""
    for error in (asyncio.CancelledError(), TimeoutError("x"), ConnectionError("x")):
        assert settle(kind, error, remote_marked=True).verdict is Verdict.RETRY


# --------------------------------------------------- inline and worker together


async def _run_both(
    database: Database, kind: str, fault: BaseException, *, mark: bool = True
) -> tuple[OutboxState, OutboxState]:
    """The same failure through both senders. Returns (inline state, worker state)."""
    outbox = OutboxRepository(database)
    state = BridgeStateRepository(database)

    async def send(_kind: str, _direction: Direction, _payload: Any, sending: Any) -> int:
        if mark:
            await sending()
        raise fault

    pipe = DeliveryPipe(outbox=outbox, send=send)
    job_id, ours = await pipe.submit(
        bridge_name=BRIDGE,
        direction=Direction.TG_TO_MAX,
        kind=kind,
        payload={"x": 1},
        source_key=f"inline:{kind}:{id(fault)}",
    )
    assert ours
    with contextlib_suppress_cancelled():
        await pipe.attempt(
            job_id=job_id, bridge_name=BRIDGE, direction=Direction.TG_TO_MAX,
            kind=kind, payload={"x": 1},
        )
    inline = await _state_of(outbox, f"inline:{kind}:{id(fault)}")

    worker = OutboxWorker(bridge_name=BRIDGE, outbox=outbox, state=state, deliver=send)
    worker_job = await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.TG_TO_MAX,
        kind=kind,
        payload={"x": 1},
        source_key=f"worker:{kind}:{id(fault)}",
    )
    with contextlib_suppress_cancelled():
        await worker.drain_once()
    assert worker_job
    return inline, await _state_of(outbox, f"worker:{kind}:{id(fault)}")


async def _state_of(outbox: OutboxRepository, source_key: str) -> OutboxState:
    row = await outbox.by_source_key(source_key)
    assert row is not None
    return row.state


def contextlib_suppress_cancelled() -> Any:
    import contextlib

    return contextlib.suppress(asyncio.CancelledError)


@pytest.mark.parametrize(
    "fault",
    [
        TimeoutError("timeout"),
        ConnectionError("closed"),
        OSError("broken pipe"),
        UnconfirmedDeliveryError("no answer"),
        MaxClientError("not connected"),
        PermanentDeliveryError("never"),
        asyncio.CancelledError(),
    ],
    ids=["timeout", "connection", "oserror", "unconfirmed", "pre-write", "permanent", "cancel"],
)
async def test_inline_and_worker_reach_the_same_state(
    database: Database, fault: BaseException
) -> None:
    """The whole point of the shared policy, asserted directly."""
    inline, worker = await _run_both(database, KIND_TG_TO_MAX_TEXT, fault)
    assert inline == worker, f"{type(fault).__name__} split the two senders"


async def test_a_worker_cancellation_after_sending_is_ambiguous(database: Database) -> None:
    """The window a planned `systemctl stop` used to hide: `mark_retry(…,
    "cancelled")` overwrote the one mark that said the call had begun."""
    inline, worker = await _run_both(database, KIND_TG_TO_MAX_TEXT, asyncio.CancelledError())
    assert worker is OutboxState.AMBIGUOUS
    assert inline is OutboxState.AMBIGUOUS


async def test_a_cancellation_before_sending_still_goes_back_on_the_queue(
    database: Database,
) -> None:
    inline, worker = await _run_both(
        database, KIND_TG_TO_MAX_TEXT, asyncio.CancelledError(), mark=False
    )
    assert inline is OutboxState.PENDING
    assert worker is OutboxState.PENDING


async def test_an_idempotent_mutation_cancellation_is_still_a_retry(
    database: Database,
) -> None:
    inline, worker = await _run_both(database, KIND_TG_TO_MAX_EDIT, asyncio.CancelledError())
    assert inline is OutboxState.PENDING
    assert worker is OutboxState.PENDING


async def test_a_confirmed_refusal_after_sending_is_not_ambiguous(database: Database) -> None:
    error = ApiError(opcode=64, error="chat.blocked", message="no")
    inline, worker = await _run_both(database, KIND_TG_TO_MAX_TEXT, error)
    assert inline is not OutboxState.AMBIGUOUS
    assert worker is not OutboxState.AMBIGUOUS


async def test_a_pre_write_failure_after_the_hook_is_not_ambiguous(database: Database) -> None:
    """`sending()` fires, then the session check proves nothing was written."""
    inline, worker = await _run_both(database, KIND_TG_TO_MAX_TEXT, MaxClientError("down"))
    assert inline is OutboxState.PENDING
    assert worker is OutboxState.PENDING


# -------------------------------------------------------- crash recovery intact


async def test_a_killed_process_is_still_judged_by_the_lease(database: Database) -> None:
    """`KeyboardInterrupt` is the process *being* killed. It must pass straight
    through so the row stays INFLIGHT and lease recovery reads
    `send_started_at` on the next start — catching it here would settle the job
    as a retry and erase the one fact recovery needs."""
    outbox = OutboxRepository(database)

    async def dies(_kind: str, _direction: Direction, _payload: Any, sending: Any) -> int:
        await sending()
        raise KeyboardInterrupt

    pipe = DeliveryPipe(outbox=outbox, send=dies)
    job_id, _ = await pipe.submit(
        bridge_name=BRIDGE,
        direction=Direction.TG_TO_MAX,
        kind=KIND_TG_TO_MAX_TEXT,
        payload={},
        source_key="killed",
    )
    with pytest.raises(KeyboardInterrupt):
        await pipe.attempt(
            job_id=job_id, bridge_name=BRIDGE, direction=Direction.TG_TO_MAX,
            kind=KIND_TG_TO_MAX_TEXT, payload={},
        )

    assert await _state_of(outbox, "killed") is OutboxState.INFLIGHT
    assert (await outbox.requeue_inflight()) == (0, 1)
