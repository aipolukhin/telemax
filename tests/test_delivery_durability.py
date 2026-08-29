"""The loss scenarios from the audit, each one written as a test.

Every test here failed — or could not have been written — before the outbox was
plugged into the router. The shape is always the same: make a send fail at a
chosen moment, then ask whether the message still exists anywhere.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from bridge.routing.delivery import (
    KIND_MAX_TO_TG_TEXT,
    KIND_TG_TO_MAX_TEXT,
    DeliveryPipe,
    UnconfirmedDeliveryError,
)
from bridge.storage import (
    Database,
    Direction,
    MessageMapRepository,
    OutboxRepository,
    OutboxState,
)

BRIDGE = "dad"
MAX_CHAT = 777
BOT_ID = 555


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


class FlakySender:
    """A sender that fails a chosen number of times, then works."""

    def __init__(self, *, fail_times: int = 0, error: Exception | None = None) -> None:
        self.fail_times = fail_times
        self.error = error or ConnectionError("network went away")
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self, kind: str, direction: Direction, payload: dict[str, Any], sending: Any = None
    ) -> int | None:
        self.calls.append(payload)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.error
        return 9000 + len(self.calls)


async def _submit_and_attempt(
    pipe: DeliveryPipe, *, kind: str, source_key: str, payload: dict[str, Any] | None = None
) -> Any:
    job_id, _ours = await pipe.submit(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=kind,
        payload=payload or {"text": "привет"},
        source_key=source_key,
    )
    return await pipe.attempt(
        job_id=job_id,
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=kind,
        payload=payload or {"text": "привет"},
    )


# ------------------------------------------------------------ MAX -> Telegram


async def test_a_failed_send_leaves_a_retryable_job(database: Database) -> None:
    """The headline scenario: Telegram is down when the message arrives."""
    outbox = OutboxRepository(database)
    sender = FlakySender(fail_times=1)
    pipe = DeliveryPipe(outbox=outbox, send=sender)

    settled = await _submit_and_attempt(
        pipe, kind=KIND_MAX_TO_TG_TEXT, source_key="max:777:1"
    )

    assert settled.delivered is False
    # Not lost, not failed — waiting.
    assert (await outbox.counts(BRIDGE)).get(OutboxState.PENDING.value) == 1


async def test_the_worker_delivers_it_afterwards(database: Database) -> None:
    outbox = OutboxRepository(database)
    sender = FlakySender(fail_times=1)
    pipe = DeliveryPipe(outbox=outbox, send=sender)

    await _submit_and_attempt(pipe, kind=KIND_MAX_TO_TG_TEXT, source_key="max:777:1")

    # What the retry worker does, minus its loop.
    claimed = await outbox.claim_due(BRIDGE)
    assert len(claimed) == 1
    settled = await pipe.attempt(
        job_id=claimed[0].id,
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=claimed[0].kind,
        payload={"text": "привет"},
    )

    assert settled.delivered is True
    assert (await outbox.counts(BRIDGE)).get(OutboxState.DONE.value) == 1


async def test_it_is_delivered_exactly_once_across_the_retry(database: Database) -> None:
    outbox = OutboxRepository(database)
    sender = FlakySender(fail_times=1)
    pipe = DeliveryPipe(outbox=outbox, send=sender)

    await _submit_and_attempt(pipe, kind=KIND_MAX_TO_TG_TEXT, source_key="max:777:1")
    claimed = await outbox.claim_due(BRIDGE)
    await pipe.attempt(
        job_id=claimed[0].id,
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=claimed[0].kind,
        payload={"text": "привет"},
    )

    # Two attempts were made; only the second one reached the far side.
    assert len(sender.calls) == 2
    assert await outbox.claim_due(BRIDGE) == []


async def test_a_replayed_max_event_does_not_create_a_second_job(database: Database) -> None:
    """MAX repeats events after a reconnect. That must be free."""
    outbox = OutboxRepository(database)
    pipe = DeliveryPipe(outbox=outbox, send=FlakySender(fail_times=2))

    await _submit_and_attempt(pipe, kind=KIND_MAX_TO_TG_TEXT, source_key="max:777:1")
    await _submit_and_attempt(pipe, kind=KIND_MAX_TO_TG_TEXT, source_key="max:777:1")

    counts = await outbox.counts(BRIDGE)
    assert sum(counts.values()) == 1


async def test_an_adapter_that_returns_none_is_not_a_delivery(database: Database) -> None:
    """The false success marker, in the form it actually took."""
    outbox = OutboxRepository(database)
    delivered: list[str] = []

    async def swallows_errors(
        kind: str, direction: Direction, payload: dict[str, Any], sending: Any = None
    ) -> int | None:
        raise UnconfirmedDeliveryError("Telegram returned no message id")

    pipe = DeliveryPipe(
        outbox=outbox,
        send=swallows_errors,
        on_delivered=lambda *args: delivered.append("no"),  # type: ignore[arg-type,return-value]
    )

    settled = await _submit_and_attempt(
        pipe, kind=KIND_MAX_TO_TG_TEXT, source_key="max:777:1"
    )

    assert settled.delivered is False
    assert settled.ambiguous is True
    assert delivered == [], "a send with no confirmation must not be marked delivered"


async def test_an_unconfirmed_send_is_not_retried_by_itself(database: Database) -> None:
    """It might have arrived. Retrying puts a duplicate in a real chat."""
    outbox = OutboxRepository(database)

    async def unconfirmed(
        kind: str, direction: Direction, payload: dict[str, Any], sending: Any = None
    ) -> int | None:
        raise UnconfirmedDeliveryError("connection lost after send")

    pipe = DeliveryPipe(outbox=outbox, send=unconfirmed)
    await _submit_and_attempt(pipe, kind=KIND_MAX_TO_TG_TEXT, source_key="max:777:1")

    assert await outbox.claim_due(BRIDGE) == []
    waiting = await outbox.needing_attention(BRIDGE)
    assert [item.state for item in waiting] == [OutboxState.AMBIGUOUS]


async def test_the_owner_sees_an_ambiguous_job_and_can_act(database: Database) -> None:
    outbox = OutboxRepository(database)

    async def unconfirmed(
        kind: str, direction: Direction, payload: dict[str, Any], sending: Any = None
    ) -> int | None:
        raise UnconfirmedDeliveryError("lost")

    pipe = DeliveryPipe(outbox=outbox, send=unconfirmed)
    await _submit_and_attempt(pipe, kind=KIND_MAX_TO_TG_TEXT, source_key="max:777:1")

    stuck = (await outbox.needing_attention(BRIDGE))[0]
    assert await outbox.retry_now(stuck.id) is True
    assert len(await outbox.claim_due(BRIDGE)) == 1


# ------------------------------------------------------------ Telegram -> MAX


async def test_a_failed_max_send_leaves_a_retryable_job(database: Database) -> None:
    outbox = OutboxRepository(database)
    pipe = DeliveryPipe(outbox=outbox, send=FlakySender(fail_times=1))

    job_id, _ours = await pipe.submit(
        bridge_name=BRIDGE,
        direction=Direction.TG_TO_MAX,
        kind=KIND_TG_TO_MAX_TEXT,
        payload={"max_chat_id": MAX_CHAT, "text": "привет"},
        source_key="tg:555:42",
    )
    settled = await pipe.attempt(
        job_id=job_id,
        bridge_name=BRIDGE,
        direction=Direction.TG_TO_MAX,
        kind=KIND_TG_TO_MAX_TEXT,
        payload={"max_chat_id": MAX_CHAT, "text": "привет"},
    )

    assert settled.delivered is False
    assert settled.exception is not None, "the caller must be able to classify it"
    assert (await outbox.counts(BRIDGE)).get(OutboxState.PENDING.value) == 1


async def test_the_same_telegram_update_enqueues_once(database: Database) -> None:
    outbox = OutboxRepository(database)
    pipe = DeliveryPipe(outbox=outbox, send=FlakySender(fail_times=5))

    for _ in range(3):
        job_id, _ours = await pipe.submit(
            bridge_name=BRIDGE,
            direction=Direction.TG_TO_MAX,
            kind=KIND_TG_TO_MAX_TEXT,
            payload={"text": "привет"},
            source_key="tg:555:42",
        )
        await pipe.attempt(
            job_id=job_id,
            bridge_name=BRIDGE,
            direction=Direction.TG_TO_MAX,
            kind=KIND_TG_TO_MAX_TEXT,
            payload={"text": "привет"},
        )

    assert sum((await outbox.counts(BRIDGE)).values()) == 1


# --------------------------------------------------------------- crash windows


async def test_a_job_created_but_never_attempted_survives(tmp_path: Path) -> None:
    """Crash between creating the job and sending it."""
    path = tmp_path / "bridge.db"
    first = await Database.connect(path)
    await OutboxRepository(first).enqueue(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_TEXT,
        payload={"text": "не потеряй меня"},
        source_key="max:777:1",
    )
    await first.close()  # the process dies here

    second = await Database.connect(path)
    try:
        claimed = await OutboxRepository(second).claim_due(BRIDGE)
        assert len(claimed) == 1
        assert "не потеряй меня" in claimed[0].payload_json
    finally:
        await second.close()


async def test_a_job_leased_when_the_process_died_comes_back(tmp_path: Path) -> None:
    """Crash after claiming, before or during the send."""
    path = tmp_path / "bridge.db"
    first = await Database.connect(path)
    outbox = OutboxRepository(first)
    await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_TEXT,
        payload={"text": "привет"},
        source_key="max:777:1",
    )
    await outbox.claim_due(BRIDGE)  # leased, then the process vanishes
    await first.close()

    second = await Database.connect(path)
    try:
        recovered = OutboxRepository(second)
        assert await recovered.requeue_inflight() == (1, 0)
        assert len(await recovered.claim_due(BRIDGE)) == 1
    finally:
        await second.close()


async def test_a_delivered_job_is_not_sent_again_after_a_restart(tmp_path: Path) -> None:
    """Crash after the remote id was written. Nothing may go out twice."""
    path = tmp_path / "bridge.db"
    first = await Database.connect(path)
    outbox = OutboxRepository(first)
    job_id = await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_TEXT,
        payload={"text": "привет"},
        source_key="max:777:1",
    )
    await outbox.mark_done(job_id, remote_message_id=4242)
    await first.close()

    second = await Database.connect(path)
    try:
        recovered = OutboxRepository(second)
        await recovered.requeue_inflight()
        assert await recovered.claim_due(BRIDGE) == []

        # And the replayed source event still finds the finished job.
        again = await recovered.enqueue(
            bridge_name=BRIDGE,
            direction=Direction.MAX_TO_TG,
            kind=KIND_MAX_TO_TG_TEXT,
            payload={"text": "привет"},
            source_key="max:777:1",
        )
        assert again == job_id
        assert await recovered.claim_due(BRIDGE) == []
    finally:
        await second.close()


async def test_the_mapping_row_alone_never_means_delivered(database: Database) -> None:
    """The invariant the old design got wrong, stated directly.

    A mapping row exists as soon as a message is claimed, because the echo MAX
    sends back has to be recognised. It says an attempt was made. Whether it
    arrived is the job's business, and only the job's.
    """
    messages = MessageMapRepository(database)
    outbox = OutboxRepository(database)

    link = await messages.claim_from_max(
        bridge_name=BRIDGE,
        max_chat_id=MAX_CHAT,
        max_message_id=1,
        telegram_bot_id=BOT_ID,
        telegram_chat_id=-100,
    )
    assert link is not None

    pipe = DeliveryPipe(outbox=outbox, send=FlakySender(fail_times=1))
    await _submit_and_attempt(pipe, kind=KIND_MAX_TO_TG_TEXT, source_key="max:777:1")

    # The row is there and says nothing about delivery...
    stored = await messages.by_max_message(MAX_CHAT, 1, BOT_ID)
    assert stored is not None
    assert stored.telegram_message_id is None

    # ...while the queue knows the truth.
    assert (await outbox.counts(BRIDGE)).get(OutboxState.PENDING.value) == 1


async def test_a_permanent_failure_is_visible_not_silent(database: Database) -> None:
    outbox = OutboxRepository(database)
    job_id = await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_TEXT,
        payload={"text": "привет"},
        source_key="max:777:1",
    )
    await outbox.mark_failed(job_id, error="chat.control")

    waiting = await outbox.needing_attention(BRIDGE)
    assert [item.state for item in waiting] == [OutboxState.FAILED]
    # And it still holds what it needs for the owner to retry it by hand.
    assert "привет" in waiting[0].payload_json
    assert await outbox.retry_now(job_id) is True


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        ("before_job", "nothing enqueued, source replays"),
        ("after_job", "pending, worker delivers"),
        ("during_send", "pending, worker delivers"),
        ("after_send_before_id", "ambiguous, owner decides"),
        ("after_id", "done, never repeated"),
    ],
)
async def test_every_crash_window_has_a_named_outcome(
    database: Database, moment: str, expected: str
) -> None:
    """The table from the audit, executable.

    Each moment is a place the process can die. None of them may end in the
    message being gone with nothing to show for it.
    """
    outbox = OutboxRepository(database)

    if moment == "before_job":
        assert await outbox.claim_due(BRIDGE) == []
        return

    job_id = await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_TEXT,
        payload={"text": "привет"},
        source_key="max:777:1",
    )

    if moment == "after_job":
        assert len(await outbox.claim_due(BRIDGE)) == 1
        return

    if moment == "during_send":
        await outbox.claim_due(BRIDGE)
        assert await outbox.requeue_inflight() == (1, 0)
        assert len(await outbox.claim_due(BRIDGE)) == 1
        return

    if moment == "after_send_before_id":
        await outbox.claim_due(BRIDGE)
        await outbox.mark_ambiguous(job_id, error="process died after send")
        assert await outbox.claim_due(BRIDGE) == []
        assert len(await outbox.needing_attention(BRIDGE)) == 1
        return

    await outbox.mark_done(job_id, remote_message_id=1)
    assert await outbox.claim_due(BRIDGE) == []
    assert await outbox.needing_attention(BRIDGE) == []


async def test_a_media_job_keeps_what_a_retry_needs(database: Database) -> None:
    """Caught by live verification, not by any test that existed before it.

    The inline attempt downloads the files to temp paths and deletes them the
    moment it returns. A job that remembered only those paths could never be
    retried: MAX blinked once, the first attempt failed, and every retry after
    it died on files that were already gone — a transient outage turned into a
    permanently undelivered photo.

    So the job has to carry the Telegram file ids too. They do not expire.
    """
    outbox = OutboxRepository(database)
    job_id = await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.TG_TO_MAX,
        kind="tg_to_max_media",
        payload={
            "max_chat_id": MAX_CHAT,
            "bot_id": BOT_ID,
            "caption": "подпись",
            "items": [["photo", "/nonexistent/gone-with-the-attempt.jpg", "photo.jpg"]],
            "sources": [["photo", "AgACAgIAAx0-file-id", "photo.jpg"]],
        },
        source_key="tg:555:900",
    )

    claimed = await outbox.claim_due(BRIDGE)
    assert [item.id for item in claimed] == [job_id]

    import json as _json

    payload = _json.loads(claimed[0].payload_json)
    assert payload["sources"], "a retry has nothing to fetch from without these"
    assert payload["sources"][0][1] == "AgACAgIAAx0-file-id"
    assert payload["bot_id"] == BOT_ID, "the retry looks the bot up by this"


async def test_the_worker_cannot_take_a_job_the_caller_is_sending(
    database: Database,
) -> None:
    """The duplicate that live verification produced, as a test.

    The job used to be created PENDING and sent immediately after. The worker
    polls that same queue, and in the gap between the insert and the send it
    claimed the job and delivered it too — two copies in the contact's chat,
    and a remote id overwritten by whichever writer finished last.

    Submitting now takes the job in the same transaction that creates it.
    """
    outbox = OutboxRepository(database)
    pipe = DeliveryPipe(outbox=outbox, send=FlakySender())

    _job_id, ours = await pipe.submit(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_TEXT,
        payload={"text": "привет"},
        source_key="max:777:5",
    )
    assert ours is True

    # This is precisely what the worker does on its next tick.
    assert await outbox.claim_due(BRIDGE) == [], "the worker must not get it"


async def test_a_second_caller_is_told_the_job_is_not_theirs(database: Database) -> None:
    outbox = OutboxRepository(database)
    pipe = DeliveryPipe(outbox=outbox, send=FlakySender())
    common = {
        "bridge_name": BRIDGE,
        "direction": Direction.MAX_TO_TG,
        "kind": KIND_MAX_TO_TG_TEXT,
        "payload": {"text": "привет"},
        "source_key": "max:777:6",
    }

    first_id, first_ours = await pipe.submit(**common)  # type: ignore[arg-type]
    second_id, second_ours = await pipe.submit(**common)  # type: ignore[arg-type]

    assert first_id == second_id
    assert first_ours is True
    assert second_ours is False, "only one caller may send"


async def test_a_delivered_job_is_never_handed_out_again(database: Database) -> None:
    outbox = OutboxRepository(database)
    pipe = DeliveryPipe(outbox=outbox, send=FlakySender())
    common = {
        "bridge_name": BRIDGE,
        "direction": Direction.MAX_TO_TG,
        "kind": KIND_MAX_TO_TG_TEXT,
        "payload": {"text": "привет"},
        "source_key": "max:777:7",
    }

    job_id, _ = await pipe.submit(**common)  # type: ignore[arg-type]
    await outbox.mark_done(job_id, remote_message_id=1)

    _, ours = await pipe.submit(**common)  # type: ignore[arg-type]
    assert ours is False, "a replayed event must not re-send a delivered message"


async def test_an_abandoned_lease_can_be_taken_again(database: Database) -> None:
    """The crash path still has to work: a dead sender must not hold it for ever."""
    outbox = OutboxRepository(database)
    pipe = DeliveryPipe(outbox=outbox, send=FlakySender())
    common = {
        "bridge_name": BRIDGE,
        "direction": Direction.MAX_TO_TG,
        "kind": KIND_MAX_TO_TG_TEXT,
        "payload": {"text": "привет"},
        "source_key": "max:777:8",
    }

    await outbox.claim_for_attempt(lease_ms=-1, **common)  # type: ignore[arg-type]
    _, ours = await pipe.submit(**common)  # type: ignore[arg-type]
    assert ours is True, "an expired lease returns the job to whoever asks"


async def test_the_worker_marks_an_unconfirmed_send_ambiguous(database: Database) -> None:
    """Not just the inline path: the worker must not retry it either (ADR 0002)."""
    from bridge.retry import OutboxWorker
    from bridge.storage import BridgeStateRepository

    outbox = OutboxRepository(database)
    job_id = await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_TEXT,
        payload={"text": "привет"},
        source_key="max:777:9",
    )

    async def unconfirmed(
        kind: str, direction: Direction, payload: dict[str, Any], sending: Any = None
    ) -> int | None:
        raise UnconfirmedDeliveryError("no id came back")

    worker = OutboxWorker(
        bridge_name=BRIDGE,
        outbox=outbox,
        state=BridgeStateRepository(database),
        deliver=unconfirmed,
    )
    await worker.drain_once()

    row = await database.query_one("SELECT state FROM outbox WHERE id = ?", (job_id,))
    assert row is not None
    assert row["state"] == OutboxState.AMBIGUOUS.value
    assert await outbox.claim_due(BRIDGE) == []


async def test_the_worker_records_the_remote_id(database: Database) -> None:
    from bridge.retry import OutboxWorker
    from bridge.storage import BridgeStateRepository

    outbox = OutboxRepository(database)
    job_id = await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_TEXT,
        payload={"text": "привет"},
        source_key="max:777:10",
    )

    async def deliver(
        kind: str, direction: Direction, payload: dict[str, Any], sending: Any = None
    ) -> int | None:
        return 7777

    worker = OutboxWorker(
        bridge_name=BRIDGE,
        outbox=outbox,
        state=BridgeStateRepository(database),
        deliver=deliver,
    )
    await worker.drain_once()

    row = await database.query_one(
        "SELECT state, remote_message_id FROM outbox WHERE id = ?", (job_id,)
    )
    assert row is not None
    assert row["state"] == OutboxState.DONE.value
    assert row["remote_message_id"] == 7777, "a delivery the worker made is still a delivery"
