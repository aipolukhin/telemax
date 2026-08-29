"""Inc 2 — owner edits and deletes as durable jobs, with the send-race resolved.

Three layers: the resolution logic against every predecessor state; the outbox
primitives and their two-worker races on a real database; and the router intake
that enqueues the durable jobs. No PyMax, no new pipeline, gate untouched.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from bridge.routing.delivery import (
    KIND_TG_TO_MAX_DELETE,
    KIND_TG_TO_MAX_EDIT,
    KIND_TG_TO_MAX_TEXT,
    DeferDelivery,
    DeliveryPipe,
    UnconfirmedDeliveryError,
)
from bridge.routing.owner_mutation import (
    delete_source_key,
    edit_source_key,
    fingerprint,
    resolve_delete,
    resolve_edit,
    send_source_key,
)
from bridge.routing.router import BridgeRouter, BridgeTarget
from bridge.storage import (
    BridgeStateRepository,
    Database,
    Direction,
    MessageMapRepository,
    OutboxRepository,
    OutboxState,
)

ACCOUNT = 100000001
OWNER_MSG = 1001879
BRIDGE = "mom"
MAX_CHAT = 555
MAX_MSG = 111411200524288008
BOT = 9000000001
#: One edit update's `pts` — the account's update sequence number, which is what
#: makes an edit *event* identifiable rather than the words it happened to carry.
PTS = 4471


class FakeMax:
    def __init__(self) -> None:
        self.edits: list[tuple[int, int, str]] = []
        self.deletes: list[tuple[int, list[int], bool]] = []

    async def edit_text(self, chat_id: int, message_id: int, text: str) -> None:
        self.edits.append((chat_id, message_id, text))

    async def delete_messages(
        self, chat_id: int, message_ids: list[int], *, for_everyone: bool = True
    ) -> None:
        self.deletes.append((chat_id, message_ids, for_everyone))


SEND_KEY = send_source_key(ACCOUNT, OWNER_MSG)


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = await Database.connect(tmp_path / "bridge.db")
    try:
        yield database
    finally:
        await database.close()


async def _map(messages: MessageMapRepository, *, max_message_id: int | None) -> int:
    link_id = await messages.record_from_telegram(
        bridge_name=BRIDGE,
        max_chat_id=MAX_CHAT,
        telegram_bot_id=BOT,
        telegram_chat_id=BOT,
        telegram_message_id=None,
        telegram_owner_message_id=OWNER_MSG,
        telegram_owner_account_id=ACCOUNT,
    )
    if max_message_id is not None:
        await messages.attach_max_message(link_id, max_message_id)
    return link_id


async def _send(outbox: OutboxRepository, *, state: OutboxState) -> int:
    job_id = await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.TG_TO_MAX,
        kind=KIND_TG_TO_MAX_TEXT,
        payload={"max_chat_id": MAX_CHAT, "text": "original"},
        source_key=SEND_KEY,
    )
    if state is OutboxState.PENDING:
        return job_id
    if state is OutboxState.INFLIGHT:
        await outbox.claim_for_attempt(
            bridge_name=BRIDGE,
            direction=Direction.TG_TO_MAX,
            kind=KIND_TG_TO_MAX_TEXT,
            payload={"max_chat_id": MAX_CHAT, "text": "original"},
            source_key=SEND_KEY,
        )
    elif state is OutboxState.DONE:
        await outbox.mark_done(job_id, remote_message_id=MAX_MSG)
    elif state is OutboxState.FAILED:
        await outbox.mark_failed(job_id, error="boom")
    elif state is OutboxState.AMBIGUOUS:
        await outbox.mark_ambiguous(job_id, error="unconfirmed")
    return job_id


def _delete_payload() -> dict[str, Any]:
    return {
        "max_chat_id": MAX_CHAT,
        "account_id": ACCOUNT,
        "owner_message_id": OWNER_MSG,
        "send_source_key": SEND_KEY,
    }


def _edit_payload(text: str = "edited") -> dict[str, Any]:
    return {**_delete_payload(), "text": text}


# ----------------------------------------------------------- delete resolution


async def test_delete_after_done_removes_in_max_for_everyone(db: Database) -> None:
    messages, outbox, mx = MessageMapRepository(db), OutboxRepository(db), FakeMax()
    await _map(messages, max_message_id=MAX_MSG)
    await _send(outbox, state=OutboxState.DONE)

    await resolve_delete(outbox=outbox, messages=messages, max_sender=mx, payload=_delete_payload())

    assert mx.deletes == [(MAX_CHAT, [MAX_MSG], True)]


async def test_delete_is_not_branched_on_origin(db: Database) -> None:
    """The same operation regardless of who authored the MAX message — MAX scopes
    it. There is no direction check in the delete path to test *around*; this
    pins that a mapped, delivered message deletes with `for_everyone=True`."""
    messages, outbox, mx = MessageMapRepository(db), OutboxRepository(db), FakeMax()
    await _map(messages, max_message_id=MAX_MSG)
    await _send(outbox, state=OutboxState.DONE)
    await resolve_delete(outbox=outbox, messages=messages, max_sender=mx, payload=_delete_payload())
    assert mx.deletes[0][2] is True


async def test_delete_before_send_cancels_the_pending_send(db: Database) -> None:
    messages, outbox, mx = MessageMapRepository(db), OutboxRepository(db), FakeMax()
    await _map(messages, max_message_id=None)
    job = await _send(outbox, state=OutboxState.PENDING)

    await resolve_delete(outbox=outbox, messages=messages, max_sender=mx, payload=_delete_payload())

    assert mx.deletes == [], "nothing was sent, so nothing is deleted in MAX"
    send = await outbox.by_source_key(SEND_KEY)
    assert send is not None and send.state is OutboxState.ARCHIVED and send.id == job


async def test_delete_while_inflight_defers(db: Database) -> None:
    messages, outbox, mx = MessageMapRepository(db), OutboxRepository(db), FakeMax()
    await _map(messages, max_message_id=None)
    await _send(outbox, state=OutboxState.INFLIGHT)
    with pytest.raises(DeferDelivery):
        await resolve_delete(
            outbox=outbox, messages=messages, max_sender=mx, payload=_delete_payload()
        )
    assert mx.deletes == []


async def test_delete_after_failed_removes_nothing(db: Database) -> None:
    messages, outbox, mx = MessageMapRepository(db), OutboxRepository(db), FakeMax()
    await _map(messages, max_message_id=None)
    await _send(outbox, state=OutboxState.FAILED)

    await resolve_delete(outbox=outbox, messages=messages, max_sender=mx, payload=_delete_payload())

    assert mx.deletes == [], "no remote message existed to delete"
    send = await outbox.by_source_key(SEND_KEY)
    assert send is not None and send.state is OutboxState.ARCHIVED
    assert await outbox.retry_now(send.id) is False, "the owner cannot hand-retry it either"


async def test_delete_at_ambiguous_needs_attention(db: Database) -> None:
    messages, outbox, mx = MessageMapRepository(db), OutboxRepository(db), FakeMax()
    await _map(messages, max_message_id=None)
    await _send(outbox, state=OutboxState.AMBIGUOUS)
    with pytest.raises(UnconfirmedDeliveryError):
        await resolve_delete(
            outbox=outbox, messages=messages, max_sender=mx, payload=_delete_payload()
        )
    assert mx.deletes == []


async def test_delete_without_mapping_is_ignored(db: Database) -> None:
    messages, outbox, mx = MessageMapRepository(db), OutboxRepository(db), FakeMax()
    await resolve_delete(outbox=outbox, messages=messages, max_sender=mx, payload=_delete_payload())
    assert mx.deletes == []


# ------------------------------------------------------------- edit resolution


async def test_edit_after_done_edits_in_max(db: Database) -> None:
    messages, outbox, mx = MessageMapRepository(db), OutboxRepository(db), FakeMax()
    await _map(messages, max_message_id=MAX_MSG)
    await _send(outbox, state=OutboxState.DONE)

    await resolve_edit(
        outbox=outbox, messages=messages, max_sender=mx, payload=_edit_payload("v2")
    )

    assert mx.edits == [(MAX_CHAT, MAX_MSG, "v2")]


async def test_edit_before_send_coalesces_into_the_pending_payload(db: Database) -> None:
    messages, outbox, mx = MessageMapRepository(db), OutboxRepository(db), FakeMax()
    await _map(messages, max_message_id=None)
    await _send(outbox, state=OutboxState.PENDING)

    await resolve_edit(
        outbox=outbox, messages=messages, max_sender=mx, payload=_edit_payload("latest")
    )

    assert mx.edits == [], "no remote edit — the send has not gone out"
    send = await outbox.by_source_key(SEND_KEY)
    assert send is not None and json.loads(send.payload_json)["text"] == "latest"


async def test_edit_while_inflight_defers(db: Database) -> None:
    messages, outbox, mx = MessageMapRepository(db), OutboxRepository(db), FakeMax()
    await _map(messages, max_message_id=None)
    await _send(outbox, state=OutboxState.INFLIGHT)
    with pytest.raises(DeferDelivery):
        await resolve_edit(
            outbox=outbox, messages=messages, max_sender=mx, payload=_edit_payload()
        )


async def test_edit_after_failed_updates_the_failed_payload(db: Database) -> None:
    messages, outbox, mx = MessageMapRepository(db), OutboxRepository(db), FakeMax()
    await _map(messages, max_message_id=None)
    await _send(outbox, state=OutboxState.FAILED)

    await resolve_edit(
        outbox=outbox, messages=messages, max_sender=mx, payload=_edit_payload("fixed")
    )

    assert mx.edits == []
    send = await outbox.by_source_key(SEND_KEY)
    assert send is not None and json.loads(send.payload_json)["text"] == "fixed"


async def test_edit_at_ambiguous_needs_attention(db: Database) -> None:
    messages, outbox, mx = MessageMapRepository(db), OutboxRepository(db), FakeMax()
    await _map(messages, max_message_id=None)
    await _send(outbox, state=OutboxState.AMBIGUOUS)
    with pytest.raises(UnconfirmedDeliveryError):
        await resolve_edit(
            outbox=outbox, messages=messages, max_sender=mx, payload=_edit_payload()
        )


async def test_a_delete_makes_a_late_edit_a_no_op(db: Database) -> None:
    """Delete is terminal: a durable delete job for the message means a later
    edit — even a replayed older one — does not resurrect it."""
    messages, outbox, mx = MessageMapRepository(db), OutboxRepository(db), FakeMax()
    await _map(messages, max_message_id=MAX_MSG)
    await _send(outbox, state=OutboxState.DONE)
    # A delete job for this message exists (accepted delete).
    await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.TG_TO_MAX,
        kind=KIND_TG_TO_MAX_DELETE,
        payload=_delete_payload(),
        source_key=delete_source_key(ACCOUNT, OWNER_MSG),
    )

    await resolve_edit(
        outbox=outbox, messages=messages, max_sender=mx, payload=_edit_payload("zombie")
    )

    assert mx.edits == [], "the message is terminally deleted; no edit is applied"


# --------------------------------------------------------- concurrency (two workers)


async def test_cancel_wins_before_claim(db: Database) -> None:
    outbox = OutboxRepository(db)
    await _send(outbox, state=OutboxState.PENDING)

    assert await outbox.cancel_pending(SEND_KEY, reason="deleted") is True
    # A worker now tries to claim it — and cannot, it is archived.
    _job, ours = await outbox.claim_for_attempt(
        bridge_name=BRIDGE,
        direction=Direction.TG_TO_MAX,
        kind=KIND_TG_TO_MAX_TEXT,
        payload={"max_chat_id": MAX_CHAT, "text": "original"},
        source_key=SEND_KEY,
    )
    assert ours is False, "the send never runs after a cancel"


async def test_claim_wins_before_cancel(db: Database) -> None:
    outbox = OutboxRepository(db)
    await _send(outbox, state=OutboxState.PENDING)

    _job, ours = await outbox.claim_for_attempt(
        bridge_name=BRIDGE,
        direction=Direction.TG_TO_MAX,
        kind=KIND_TG_TO_MAX_TEXT,
        payload={"max_chat_id": MAX_CHAT, "text": "original"},
        source_key=SEND_KEY,
    )
    assert ours is True
    # The delete's cancel now touches zero rows — it must fall back to waiting.
    assert await outbox.cancel_pending(SEND_KEY, reason="deleted") is False


async def test_replace_wins_before_claim(db: Database) -> None:
    outbox = OutboxRepository(db)
    await _send(outbox, state=OutboxState.PENDING)

    assert await outbox.replace_pending_payload(SEND_KEY, {"max_chat_id": MAX_CHAT, "text": "v2"})
    _job, ours = await outbox.claim_for_attempt(
        bridge_name=BRIDGE,
        direction=Direction.TG_TO_MAX,
        kind=KIND_TG_TO_MAX_TEXT,
        payload={"max_chat_id": MAX_CHAT, "text": "ignored"},
        source_key=SEND_KEY,
    )
    assert ours is True
    send = await outbox.by_source_key(SEND_KEY)
    assert send is not None and json.loads(send.payload_json)["text"] == "v2"


async def test_claim_wins_before_replace(db: Database) -> None:
    outbox = OutboxRepository(db)
    await _send(outbox, state=OutboxState.PENDING)
    await outbox.claim_for_attempt(
        bridge_name=BRIDGE,
        direction=Direction.TG_TO_MAX,
        kind=KIND_TG_TO_MAX_TEXT,
        payload={"max_chat_id": MAX_CHAT, "text": "original"},
        source_key=SEND_KEY,
    )
    assert await outbox.replace_pending_payload(SEND_KEY, {"text": "late"}) is False


async def test_deferring_a_mutation_spends_no_retry_budget(db: Database) -> None:
    """Waiting on a predecessor is not failing: attempts stay put, state PENDING."""
    outbox, messages, mx = OutboxRepository(db), MessageMapRepository(db), FakeMax()
    await _map(messages, max_message_id=None)
    await _send(outbox, state=OutboxState.INFLIGHT)

    edit_job = await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.TG_TO_MAX,
        kind=KIND_TG_TO_MAX_EDIT,
        payload=_edit_payload(),
        source_key=edit_source_key(ACCOUNT, OWNER_MSG, PTS, fingerprint("edited")),
    )

    async def send(kind: str, direction: Direction, payload: dict[str, Any], sending: Any) -> Any:
        return await resolve_edit(outbox=outbox, messages=messages, max_sender=mx, payload=payload)

    pipe = DeliveryPipe(outbox=outbox, send=send)
    settled = await pipe.attempt(
        job_id=edit_job,
        bridge_name=BRIDGE,
        direction=Direction.TG_TO_MAX,
        kind=KIND_TG_TO_MAX_EDIT,
        payload=_edit_payload(),
    )
    assert settled.deferred is True
    key = edit_source_key(ACCOUNT, OWNER_MSG, PTS, fingerprint("edited"))
    row = await outbox.by_source_key(key)
    assert row is not None
    assert row.state is OutboxState.PENDING and row.attempts == 0


async def test_a_replayed_mutation_dedups_to_one_effect(db: Database) -> None:
    outbox, messages, mx = OutboxRepository(db), MessageMapRepository(db), FakeMax()
    await _map(messages, max_message_id=MAX_MSG)
    await _send(outbox, state=OutboxState.DONE)
    key = delete_source_key(ACCOUNT, OWNER_MSG)

    async def send(kind: str, direction: Direction, payload: dict[str, Any], sending: Any) -> Any:
        return await resolve_delete(
            outbox=outbox, messages=messages, max_sender=mx, payload=payload
        )

    pipe = DeliveryPipe(outbox=outbox, send=send)
    for _ in range(2):  # a catch-up replay of the same delete
        job_id, ours = await pipe.submit(
            bridge_name=BRIDGE,
            direction=Direction.TG_TO_MAX,
            kind=KIND_TG_TO_MAX_DELETE,
            payload=_delete_payload(),
            source_key=key,
        )
        if ours:
            await pipe.attempt(
                job_id=job_id,
                bridge_name=BRIDGE,
                direction=Direction.TG_TO_MAX,
                kind=KIND_TG_TO_MAX_DELETE,
                payload=_delete_payload(),
            )
    assert len(mx.deletes) == 1, "the replay found the existing job and did nothing"


async def test_sequential_edits_apply_the_latest_after_done(db: Database) -> None:
    messages, outbox, mx = MessageMapRepository(db), OutboxRepository(db), FakeMax()
    await _map(messages, max_message_id=MAX_MSG)
    await _send(outbox, state=OutboxState.DONE)

    await resolve_edit(outbox=outbox, messages=messages, max_sender=mx, payload=_edit_payload("v1"))
    await resolve_edit(outbox=outbox, messages=messages, max_sender=mx, payload=_edit_payload("v2"))

    assert mx.edits == [(MAX_CHAT, MAX_MSG, "v1"), (MAX_CHAT, MAX_MSG, "v2")]


async def test_delete_is_terminal_across_edit_delete_edit(db: Database) -> None:
    """edit v1 → delete → edit v2: ends deleted, and v2 does not resurrect it."""
    messages, outbox, mx = MessageMapRepository(db), OutboxRepository(db), FakeMax()
    await _map(messages, max_message_id=MAX_MSG)
    await _send(outbox, state=OutboxState.DONE)

    await resolve_edit(outbox=outbox, messages=messages, max_sender=mx, payload=_edit_payload("v1"))
    # The delete arrives and is recorded as a durable job (terminal marker).
    await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.TG_TO_MAX,
        kind=KIND_TG_TO_MAX_DELETE,
        payload=_delete_payload(),
        source_key=delete_source_key(ACCOUNT, OWNER_MSG),
    )
    await resolve_delete(outbox=outbox, messages=messages, max_sender=mx, payload=_delete_payload())
    await resolve_edit(outbox=outbox, messages=messages, max_sender=mx, payload=_edit_payload("v2"))

    assert mx.edits == [(MAX_CHAT, MAX_MSG, "v1")], "v2 does not resurrect the deleted message"
    assert mx.deletes == [(MAX_CHAT, [MAX_MSG], True)]


async def test_an_absorbed_edit_is_durable_and_replays_to_nothing(db: Database) -> None:
    """An edit coalesced into a pending send still owns a durable job with a
    terminal outcome, so a catch-up replay after a restart finds it and stops
    there — no second absorption, and never a remote edit."""
    outbox, messages, mx = OutboxRepository(db), MessageMapRepository(db), FakeMax()
    await _map(messages, max_message_id=None)
    await _send(outbox, state=OutboxState.PENDING)
    key = edit_source_key(ACCOUNT, OWNER_MSG, PTS, fingerprint("latest"))

    async def send(kind: str, direction: Direction, payload: dict[str, Any], sending: Any) -> Any:
        return await resolve_edit(outbox=outbox, messages=messages, max_sender=mx, payload=payload)

    pipe = DeliveryPipe(outbox=outbox, send=send)
    for _ in range(2):  # the live edit, then the same one replayed after a restart
        job_id, ours = await pipe.submit(
            bridge_name=BRIDGE,
            direction=Direction.TG_TO_MAX,
            kind=KIND_TG_TO_MAX_EDIT,
            payload=_edit_payload("latest"),
            source_key=key,
        )
        if ours:
            await pipe.attempt(
                job_id=job_id,
                bridge_name=BRIDGE,
                direction=Direction.TG_TO_MAX,
                kind=KIND_TG_TO_MAX_EDIT,
                payload=_edit_payload("latest"),
            )

    assert mx.edits == [], "absorbed into the send, so nothing was edited in MAX"
    absorbed = await outbox.by_source_key(key)
    assert absorbed is not None and absorbed.state is OutboxState.DONE, "durable and terminal"
    original = await outbox.by_source_key(SEND_KEY)
    assert original is not None and original.state is OutboxState.PENDING
    assert json.loads(original.payload_json)["text"] == "latest", "one send, latest version"


async def test_two_edits_then_a_delete_before_the_send_leave_no_trace(db: Database) -> None:
    """v1 → v2 → delete, all while the send is still pending: the latest version
    coalesces, the delete then cancels the send outright, and a later edit does
    not resurrect it. Nothing at all reaches MAX."""
    messages, outbox, mx = MessageMapRepository(db), OutboxRepository(db), FakeMax()
    await _map(messages, max_message_id=None)
    await _send(outbox, state=OutboxState.PENDING)

    await resolve_edit(outbox=outbox, messages=messages, max_sender=mx, payload=_edit_payload("v1"))
    await resolve_edit(outbox=outbox, messages=messages, max_sender=mx, payload=_edit_payload("v2"))
    coalesced = await outbox.by_source_key(SEND_KEY)
    assert coalesced is not None and json.loads(coalesced.payload_json)["text"] == "v2"

    await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.TG_TO_MAX,
        kind=KIND_TG_TO_MAX_DELETE,
        payload=_delete_payload(),
        source_key=delete_source_key(ACCOUNT, OWNER_MSG),
    )
    await resolve_delete(outbox=outbox, messages=messages, max_sender=mx, payload=_delete_payload())
    await resolve_edit(outbox=outbox, messages=messages, max_sender=mx, payload=_edit_payload("v3"))

    assert mx.edits == [] and mx.deletes == [], "the message never existed in MAX"
    cancelled = await outbox.by_source_key(SEND_KEY)
    assert cancelled is not None and cancelled.state is OutboxState.ARCHIVED


async def test_a_cancelled_send_cannot_be_retried_by_hand(db: Database) -> None:
    """Terminality has to survive Guardian: once a delete cancelled the send,
    the owner cannot put it back on the queue and resurrect the message."""
    messages, outbox, mx = MessageMapRepository(db), OutboxRepository(db), FakeMax()
    await _map(messages, max_message_id=None)
    job = await _send(outbox, state=OutboxState.PENDING)

    await resolve_delete(outbox=outbox, messages=messages, max_sender=mx, payload=_delete_payload())

    assert await outbox.retry_now(job) is False, "a cancelled send is not retryable"


async def test_an_expired_lease_lets_the_send_be_reclaimed(db: Database) -> None:
    """A worker that crashed after claiming does not strand the send: its lease
    lapses and the next claim takes it, so a waiting mutation resolves later."""
    outbox = OutboxRepository(db)
    job = await _send(outbox, state=OutboxState.INFLIGHT)
    # Force the lease into the past, as a crash would leave it.
    async with db.transaction() as connection:
        await connection.execute(
            "UPDATE outbox SET lease_expires_at = 1 WHERE id = ?", (job,)
        )
    _id, ours = await outbox.claim_for_attempt(
        bridge_name=BRIDGE,
        direction=Direction.TG_TO_MAX,
        kind=KIND_TG_TO_MAX_TEXT,
        payload={"max_chat_id": MAX_CHAT, "text": "original"},
        source_key=SEND_KEY,
    )
    assert ours is True, "the lapsed lease is reclaimable — the send is not lost"


# ------------------------------------------------------------- router intake


class _Lookup:
    def bridge_for_bot(self, bot_id: int) -> BridgeTarget | None:
        return BridgeTarget(name=BRIDGE, max_chat_id=MAX_CHAT, bot_id=BOT)

    def bridge_for_max_chat(self, max_chat_id: int) -> BridgeTarget | None:
        return None


class _Dummy:
    async def send_text(self, *a: Any, **k: Any) -> int | None:
        return 1


async def _router(db: Database, mx: FakeMax) -> BridgeRouter:
    messages, outbox = MessageMapRepository(db), OutboxRepository(db)

    async def send(kind: str, direction: Direction, payload: dict[str, Any], sending: Any) -> Any:
        if kind == KIND_TG_TO_MAX_EDIT:
            return await resolve_edit(
                outbox=outbox, messages=messages, max_sender=mx, payload=payload
            )
        if kind == KIND_TG_TO_MAX_DELETE:
            return await resolve_delete(
                outbox=outbox, messages=messages, max_sender=mx, payload=payload
            )
        return 1

    return BridgeRouter(
        lookup=_Lookup(),
        telegram=_Dummy(),
        max_sender=mx,
        messages=messages,
        state=BridgeStateRepository(db),
        owner_chat_id=ACCOUNT,
        pipe=DeliveryPipe(outbox=outbox, send=send),
    )


async def test_owner_edit_resolves_by_account_and_owner_id(db: Database) -> None:
    messages, outbox, mx = MessageMapRepository(db), OutboxRepository(db), FakeMax()
    await _map(messages, max_message_id=MAX_MSG)
    await _send(outbox, state=OutboxState.DONE)
    router = await _router(db, mx)

    await router.on_owner_edit(
        owner_account_id=ACCOUNT, owner_message_id=OWNER_MSG, text="v2", edit_pts=PTS
    )

    assert mx.edits == [(MAX_CHAT, MAX_MSG, "v2")]


async def test_owner_edit_for_an_unmapped_message_is_ignored(db: Database) -> None:
    mx = FakeMax()
    router = await _router(db, mx)
    await router.on_owner_edit(
        owner_account_id=ACCOUNT, owner_message_id=999, text="x", edit_pts=PTS
    )
    assert mx.edits == []


async def test_a_batch_delete_is_n_independent_deduped_effects(db: Database) -> None:
    messages, outbox, mx = MessageMapRepository(db), OutboxRepository(db), FakeMax()
    # Two mapped, delivered owner messages.
    for owner_id, max_id in ((201, 900001), (202, 900002)):
        link = await messages.record_from_telegram(
            bridge_name=BRIDGE,
            max_chat_id=MAX_CHAT,
            telegram_bot_id=BOT,
            telegram_chat_id=BOT,
            telegram_message_id=None,
            telegram_owner_message_id=owner_id,
            telegram_owner_account_id=ACCOUNT,
        )
        await messages.attach_max_message(link, max_id)
        await outbox.mark_done(
            await outbox.enqueue(
                bridge_name=BRIDGE,
                direction=Direction.TG_TO_MAX,
                kind=KIND_TG_TO_MAX_TEXT,
                payload={"max_chat_id": MAX_CHAT, "text": "x"},
                source_key=send_source_key(ACCOUNT, owner_id),
            ),
            remote_message_id=max_id,
        )
    router = await _router(db, mx)

    await router.on_owner_delete(owner_account_id=ACCOUNT, owner_message_ids=[201, 202, 777])

    assert sorted(m[1][0] for m in mx.deletes) == [900001, 900002], "unmapped 777 ignored"
    assert await outbox.by_source_key(delete_source_key(ACCOUNT, 201)) is not None
    assert await outbox.by_source_key(delete_source_key(ACCOUNT, 202)) is not None


# ------------------------------------------------ edit identity (update pts)


async def _edit(router: BridgeRouter, *, pts: int, text: str) -> None:
    """One owner edit event: a version and the words it carried."""
    await router.on_owner_edit(
        owner_account_id=ACCOUNT, owner_message_id=OWNER_MSG, text=text, edit_pts=pts
    )


async def _delivered(db: Database, mx: FakeMax) -> tuple[BridgeRouter, OutboxRepository]:
    """A mapped, delivered owner message, ready to be edited in MAX."""
    messages, outbox = MessageMapRepository(db), OutboxRepository(db)
    await _map(messages, max_message_id=MAX_MSG)
    await _send(outbox, state=OutboxState.DONE)
    return await _router(db, mx), outbox


async def test_a_b_a_b_applies_every_version_and_ends_on_b(db: Database) -> None:
    """The defect this keying exists for. Four edit events are four versions;
    keyed by text alone the last step deduped against the first `B`, and MAX was
    left showing `A` — permanently out of step with what Telegram displayed."""
    mx = FakeMax()
    router, _ = await _delivered(db, mx)

    for pts, text in ((PTS, "A"), (PTS + 1, "B"), (PTS + 2, "A"), (PTS + 3, "B")):
        await _edit(router, pts=pts, text=text)

    assert [edit[2] for edit in mx.edits] == ["A", "B", "A", "B"], "four accepted versions"
    assert mx.edits[-1][2] == "B", "MAX ends on what Telegram shows"


async def test_replaying_every_edit_update_adds_no_duplicate(db: Database) -> None:
    """A catch-up re-delivers each update with the pts it already had, so every
    one finds the job it already finished and stops there."""
    mx = FakeMax()
    router, _ = await _delivered(db, mx)
    updates = ((PTS, "A"), (PTS + 1, "B"), (PTS + 2, "A"), (PTS + 3, "B"))

    for pts, text in updates:
        await _edit(router, pts=pts, text=text)
    for pts, text in updates:  # the same run again, as a reconnect catch-up
        await _edit(router, pts=pts, text=text)

    assert [edit[2] for edit in mx.edits] == ["A", "B", "A", "B"], "replay adds nothing"


async def test_two_updates_with_the_same_text_do_not_dedup(db: Database) -> None:
    """Same words, two events: two versions, two keys, both applied. This is the
    case a content-only key could not tell from a replay."""
    mx = FakeMax()
    router, _ = await _delivered(db, mx)

    await _edit(router, pts=PTS, text="same")
    await _edit(router, pts=PTS + 1, text="same")

    assert len(mx.edits) == 2, "two distinct edit events, both carried"


async def test_one_update_dedups_on_account_message_pts_and_text(db: Database) -> None:
    """Identical account, message, pts and text is one event however many times
    it arrives: one durable job, one effect."""
    mx = FakeMax()
    router, outbox = await _delivered(db, mx)

    await _edit(router, pts=PTS, text="once")
    await _edit(router, pts=PTS, text="once")

    assert len(mx.edits) == 1
    key = edit_source_key(ACCOUNT, OWNER_MSG, PTS, fingerprint("once"))
    assert await outbox.by_source_key(key) is not None


async def test_a_reconnect_replay_keeps_the_same_source_key(db: Database) -> None:
    """The key is a property of the update, not of when it was seen: the replay
    resolves to the very same durable row, not a second one that looks alike."""
    mx = FakeMax()
    router, outbox = await _delivered(db, mx)
    key = edit_source_key(ACCOUNT, OWNER_MSG, PTS, fingerprint("v2"))

    await _edit(router, pts=PTS, text="v2")
    first = await outbox.by_source_key(key)
    assert first is not None

    await _edit(router, pts=PTS, text="v2")  # the same update, after a reconnect

    again = await outbox.by_source_key(key)
    assert again is not None and again.id == first.id, "one row, not a lookalike"
    assert len(mx.edits) == 1


async def test_an_edit_after_an_accepted_delete_stays_a_no_op(db: Database) -> None:
    """A never-seen pts is a new key and still not a way back: terminality is
    decided by the delete's own job, not by the edit's identity."""
    mx = FakeMax()
    router, outbox = await _delivered(db, mx)
    await outbox.enqueue(
        bridge_name=BRIDGE,
        direction=Direction.TG_TO_MAX,
        kind=KIND_TG_TO_MAX_DELETE,
        payload=_delete_payload(),
        source_key=delete_source_key(ACCOUNT, OWNER_MSG),
    )

    await _edit(router, pts=PTS + 99, text="zombie")

    assert mx.edits == [], "the message is terminally deleted"
