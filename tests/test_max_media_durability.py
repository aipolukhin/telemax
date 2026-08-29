"""MAX → Telegram media has to be as durable as MAX → Telegram text.

It was not. Text went through the outbox; attachments went straight out through
`MaxMediaDelivery.deliver()`. The dedup claim is written before either, so a
failed media send left a row that the next MAX replay read as "already
delivered" — the original loss path, still open for one branch of one method.

These tests fail against that shape and pass once both branches share a
pipeline.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from bridge.config import TimestampStyle
from bridge.max_client import AttachmentKind, IncomingMaxMessage, MaxAttachment
from bridge.routing.delivery import (
    KIND_MAX_TO_TG_MEDIA,
    DeliveryPipe,
    UnconfirmedDeliveryError,
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

MOM = BridgeTarget(name="mom", max_chat_id=777, bot_id=555)
OWNER_CHAT = 4242
CONTACT = 99


class FakeLookup:
    def bridge_for_max_chat(self, max_chat_id: int) -> BridgeTarget | None:
        return MOM if max_chat_id == MOM.max_chat_id else None

    def bridge_for_bot(self, bot_id: int) -> BridgeTarget | None:
        return MOM if bot_id == MOM.bot_id else None


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, bot_id: int, chat_id: int, text: str, **kwargs: Any) -> int | None:
        self.sent.append(text)
        return 900 + len(self.sent)

    async def edit_text(self, *args: Any, **kwargs: Any) -> bool:
        return True

    async def delete(self, *args: Any, **kwargs: Any) -> bool:
        return True


class FakeMax:
    async def send_text(self, *args: Any, **kwargs: Any) -> int | None:
        return 1

    async def send_media(self, *args: Any, **kwargs: Any) -> int | None:
        return 1

    async def edit_text(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def delete_messages(self, *args: Any, **kwargs: Any) -> None:
        return None


class RecordingMedia:
    """Stands in for MaxMediaDelivery. Counts how often it actually sends."""

    def __init__(self, *, fail_times: int = 0, returns: int | None = 700) -> None:
        self.calls = 0
        self.fail_times = fail_times
        self.returns = returns

    async def deliver(self, message: Any, **kwargs: Any) -> int | None:
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("Telegram went away mid-upload")
        return self.returns


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


def attachment(kind: AttachmentKind = AttachmentKind.PHOTO, **raw: Any) -> MaxAttachment:
    return MaxAttachment(kind=kind, file_name="shot.jpg", size=1234, raw=raw or {"photoId": 1})


def incoming(message_id: int, *, attachments: tuple[MaxAttachment, ...], text: str = "") -> Any:
    return IncomingMaxMessage(
        message_id=message_id,
        chat_id=MOM.max_chat_id,
        sender_id=CONTACT,
        text=text,
        timestamp=1_700_000_000_000,
        is_outgoing=False,
        attachments=attachments,
    )


def build(
    database: Database, media: RecordingMedia, sender: Any
) -> tuple[BridgeRouter, OutboxRepository, MessageMapRepository]:
    outbox = OutboxRepository(database)
    messages = MessageMapRepository(database)
    router = BridgeRouter(
        lookup=FakeLookup(),
        telegram=FakeTelegram(),
        max_sender=FakeMax(),
        messages=messages,
        state=BridgeStateRepository(database),
        owner_chat_id=OWNER_CHAT,
        timestamp_style=TimestampStyle.OFF,
        media=media,
        pipe=DeliveryPipe(outbox=outbox, send=sender),
    )
    return router, outbox, messages


def media_sender(delivery: RecordingMedia) -> Any:
    """The runtime's send_job, reduced to the media branch."""

    async def send(
        kind: str, direction: Direction, payload: dict[str, Any], sending: Any = None
    ) -> int | None:
        assert kind == KIND_MAX_TO_TG_MEDIA
        sent = await delivery.deliver(payload)
        if sent is None:
            raise UnconfirmedDeliveryError("Telegram returned no message id for the media")
        return sent

    return send


# ------------------------------------------------------------- durable intake


@pytest.mark.parametrize(
    "kind",
    [
        AttachmentKind.PHOTO,
        AttachmentKind.VIDEO,
        AttachmentKind.VOICE,
        AttachmentKind.MUSIC,
        AttachmentKind.FILE,
        AttachmentKind.VIDEO_NOTE,
    ],
)
async def test_every_media_kind_creates_a_durable_job(
    database: Database, kind: AttachmentKind
) -> None:
    delivery = RecordingMedia()
    router, outbox, _ = build(database, delivery, media_sender(delivery))

    await router.on_max_message(incoming(1, attachments=(attachment(kind),)))

    counts = await outbox.counts("mom")
    assert counts.get(OutboxState.DONE.value) == 1, f"{kind.value} left no delivered job"
    assert delivery.calls == 1
    assert await outbox.needing_attention("mom") == []


async def test_the_job_carries_what_a_retry_needs(database: Database) -> None:
    """No binaries, but enough to resolve a fresh URL after a restart."""
    delivery = RecordingMedia(fail_times=1)
    router, outbox, _ = build(database, delivery, media_sender(delivery))

    await router.on_max_message(
        incoming(1, attachments=(attachment(AttachmentKind.VIDEO, videoId=4242),), text="подпись")
    )

    import json

    claimed = await outbox.claim_due("mom")
    assert len(claimed) == 1
    payload = json.loads(claimed[0].payload_json)

    assert payload["max_chat_id"] == MOM.max_chat_id
    assert payload["max_message_id"] == 1
    assert payload["bot_id"] == MOM.bot_id
    assert payload["caption"] == "подпись"
    assert len(payload["attachments"]) == 1
    stored = payload["attachments"][0]
    assert stored["kind"] == AttachmentKind.VIDEO.value
    # The raw payload is what `MaxMediaSources.resolve` needs to ask MAX for a
    # *fresh* URL — an expiring link is never the only source.
    assert stored["raw"]["videoId"] == 4242
    assert "url" not in payload, "a signed URL must not be the durable source"


async def test_a_failed_media_send_stays_retryable(database: Database) -> None:
    """The loss path, closed: a failure leaves a job, not just a mapping row."""
    delivery = RecordingMedia(fail_times=1)
    router, outbox, _ = build(database, delivery, media_sender(delivery))

    await router.on_max_message(incoming(1, attachments=(attachment(),)))

    assert (await outbox.counts("mom")).get(OutboxState.PENDING.value) == 1


async def test_the_worker_then_delivers_it_once(database: Database) -> None:
    delivery = RecordingMedia(fail_times=1)
    router, outbox, _messages = build(database, delivery, media_sender(delivery))
    await router.on_max_message(incoming(1, attachments=(attachment(),)))

    import json

    claimed = await outbox.claim_due("mom")
    settled = await DeliveryPipe(outbox=outbox, send=media_sender(delivery)).attempt(
        job_id=claimed[0].id,
        bridge_name="mom",
        direction=Direction.MAX_TO_TG,
        kind=claimed[0].kind,
        payload=json.loads(claimed[0].payload_json),
    )

    assert settled.delivered is True
    assert delivery.calls == 2, "one failed attempt, one that worked"
    assert (await outbox.counts("mom")).get(OutboxState.DONE.value) == 1


async def test_a_replayed_max_event_does_not_send_media_twice(database: Database) -> None:
    """MAX repeats events after a reconnect. The photo must not arrive twice."""
    delivery = RecordingMedia()
    router, outbox, _ = build(database, delivery, media_sender(delivery))

    await router.on_max_message(incoming(1, attachments=(attachment(),)))
    await router.on_max_message(incoming(1, attachments=(attachment(),)))

    assert delivery.calls == 1
    assert sum((await outbox.counts("mom")).values()) == 1


async def test_a_replay_after_a_failure_does_not_create_a_second_job(
    database: Database,
) -> None:
    """The exact scenario the audit described, for the media branch."""
    delivery = RecordingMedia(fail_times=1)
    router, outbox, _ = build(database, delivery, media_sender(delivery))

    await router.on_max_message(incoming(1, attachments=(attachment(),)))
    # MAX replays it; the claim row already exists, and before this change that
    # was read as "already delivered".
    await router.on_max_message(incoming(1, attachments=(attachment(),)))

    counts = await outbox.counts("mom")
    assert sum(counts.values()) == 1
    assert counts.get(OutboxState.PENDING.value) == 1, "still owed, not written off"


async def test_media_is_not_marked_delivered_without_a_remote_id(
    database: Database,
) -> None:
    delivery = RecordingMedia(returns=None)
    router, outbox, messages = build(database, delivery, media_sender(delivery))

    await router.on_max_message(incoming(1, attachments=(attachment(),)))

    assert (await outbox.counts("mom")).get(OutboxState.AMBIGUOUS.value) == 1
    link = await messages.by_max_message(MOM.max_chat_id, 1, MOM.bot_id)
    assert link is not None
    assert link.telegram_message_id is None


async def test_the_mapping_records_the_remote_id_on_success(database: Database) -> None:
    delivery = RecordingMedia(returns=770)
    router, _, messages = build(database, delivery, media_sender(delivery))

    await router.on_max_message(incoming(1, attachments=(attachment(),)))

    link = await messages.by_max_message(MOM.max_chat_id, 1, MOM.bot_id)
    assert link is not None
    assert link.telegram_message_id == 770


async def test_a_media_reply_keeps_its_reply_target(database: Database) -> None:
    delivery = RecordingMedia(fail_times=1)
    router, outbox, messages = build(database, delivery, media_sender(delivery))

    # The message being answered was delivered earlier.
    first = await messages.claim_from_max(
        bridge_name="mom",
        max_chat_id=MOM.max_chat_id,
        max_message_id=1,
        telegram_bot_id=MOM.bot_id,
        telegram_chat_id=OWNER_CHAT,
    )
    assert first is not None
    await messages.attach_telegram_message(first, 601)

    answer = IncomingMaxMessage(
        message_id=2,
        chat_id=MOM.max_chat_id,
        sender_id=CONTACT,
        text="",
        timestamp=1_700_000_000_000,
        is_outgoing=False,
        attachments=(attachment(),),
        reply_to_message_id=1,
    )
    await router.on_max_message(answer)

    import json

    # The send failed, so the job still holds what a retry would use.
    claimed = await outbox.claim_due("mom")
    payload = json.loads(claimed[0].payload_json)
    assert payload["reply_to"] == 601, "the Telegram message being answered"
    assert payload["reply_to_message_id"] == 1, "and the MAX one it came from"


async def test_an_album_is_one_job(database: Database) -> None:
    """Several attachments on one MAX message stay one unit of delivery."""
    delivery = RecordingMedia()
    router, outbox, _ = build(database, delivery, media_sender(delivery))

    await router.on_max_message(
        incoming(
            1,
            attachments=(
                attachment(AttachmentKind.PHOTO, photoId=1),
                attachment(AttachmentKind.PHOTO, photoId=2),
            ),
            text="общая подпись",
        )
    )

    assert delivery.calls == 1, "the album is sent as one call, not one per part"
    assert sum((await outbox.counts("mom")).values()) == 1


async def test_the_album_job_keeps_attachment_order(database: Database) -> None:
    delivery = RecordingMedia(fail_times=1)
    router, outbox, _ = build(database, delivery, media_sender(delivery))

    await router.on_max_message(
        incoming(
            1,
            attachments=(
                attachment(AttachmentKind.PHOTO, photoId=11),
                attachment(AttachmentKind.VIDEO, videoId=22),
            ),
        )
    )

    import json

    claimed = await outbox.claim_due("mom")
    stored = json.loads(claimed[0].payload_json)["attachments"]
    assert [item["raw"].get("photoId") or item["raw"].get("videoId") for item in stored] == [
        11,
        22,
    ]


async def test_the_caption_travels_once(database: Database) -> None:
    delivery = RecordingMedia(fail_times=1)
    router, outbox, _ = build(database, delivery, media_sender(delivery))

    await router.on_max_message(
        incoming(1, attachments=(attachment(), attachment()), text="одна подпись")
    )

    import json

    claimed = await outbox.claim_due("mom")
    payload = json.loads(claimed[0].payload_json)
    assert payload["caption"].count("одна подпись") == 1


# --------------------------------------------------------------- crash windows


async def test_a_crash_before_the_send_is_a_free_retry(database: Database) -> None:
    """Window 2-3: job exists, nothing left the machine."""
    outbox = OutboxRepository(database)
    await outbox.enqueue(
        bridge_name="mom",
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_MEDIA,
        payload={"max_chat_id": 777, "attachments": []},
        source_key="max:777:1",
    )
    await outbox.claim_due("mom")  # leased, then the process vanishes

    requeued, ambiguous = await outbox.requeue_inflight()
    assert (requeued, ambiguous) == (1, 0)
    assert len(await outbox.claim_due("mom")) == 1


async def test_a_crash_during_the_send_is_ambiguous(database: Database) -> None:
    """Window 5: Telegram may already hold the album. Resending duplicates it.

    This is the one case a lease alone cannot decide, which is why the row
    records the moment the send began.
    """
    outbox = OutboxRepository(database)
    job_id = await outbox.enqueue(
        bridge_name="mom",
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_MEDIA,
        payload={"max_chat_id": 777, "attachments": []},
        source_key="max:777:2",
    )
    await outbox.claim_due("mom")
    await outbox.mark_sending(job_id)  # the upload started, then the process died

    requeued, ambiguous = await outbox.requeue_inflight()
    assert (requeued, ambiguous) == (0, 1)
    assert await outbox.claim_due("mom") == [], "never resent by itself"
    waiting = await outbox.needing_attention("mom")
    assert [item.state for item in waiting] == [OutboxState.AMBIGUOUS]


async def test_an_expired_lease_mid_send_is_ambiguous_too(database: Database) -> None:
    """The same rule while the process is up, not only at startup."""
    outbox = OutboxRepository(database)
    job_id = await outbox.enqueue(
        bridge_name="mom",
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_MEDIA,
        payload={"attachments": []},
        source_key="max:777:3",
    )
    await outbox.claim_due("mom", lease_ms=-1)
    await outbox.mark_sending(job_id)

    assert await outbox.reclaim_expired_leases() == (0, 1)


async def test_a_retry_clears_the_sending_mark(database: Database) -> None:
    """An error came back, so the send demonstrably did not complete.

    Leaving the mark would turn every ordinary network blip into something the
    owner has to resolve by hand.
    """
    outbox = OutboxRepository(database)
    job_id = await outbox.enqueue(
        bridge_name="mom",
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_MEDIA,
        payload={"attachments": []},
        source_key="max:777:4",
    )
    await outbox.claim_due("mom")
    await outbox.mark_sending(job_id)
    await outbox.mark_retry(job_id, delay_ms=0, error="connection reset")

    await outbox.claim_due("mom")
    requeued, ambiguous = await outbox.requeue_inflight()
    assert (requeued, ambiguous) == (1, 0), "a retried job is not mid-send any more"


async def test_a_delivered_job_clears_the_sending_mark(database: Database) -> None:
    outbox = OutboxRepository(database)
    job_id = await outbox.enqueue(
        bridge_name="mom",
        direction=Direction.MAX_TO_TG,
        kind=KIND_MAX_TO_TG_MEDIA,
        payload={"attachments": []},
        source_key="max:777:5",
    )
    await outbox.mark_sending(job_id)
    await outbox.mark_done(job_id, remote_message_id=42)

    row = await database.query_one("SELECT send_started_at FROM outbox WHERE id = ?", (job_id,))
    assert row is not None
    assert row["send_started_at"] is None


# ------------------------------------------------------ payloads that fight back


async def test_a_thumbnail_in_bytes_does_not_break_the_job(database: Database) -> None:
    """The live failure, as a test.

    A MAX photo carries its preview as raw bytes. `json.dumps` refuses them, and
    because the dedup row is written first, the message ended up with a mapping,
    no job, and no replay — silent loss reintroduced through the payload.
    """
    delivery = RecordingMedia()
    router, outbox, _ = build(database, delivery, media_sender(delivery))

    photo = MaxAttachment(
        kind=AttachmentKind.PHOTO,
        file_name="shot.jpg",
        raw={"photoId": 7, "preview": b"\x89PNG\r\n\x1a\n binary thumbnail", "url": "https://x/y"},
    )
    await router.on_max_message(incoming(1, attachments=(photo,)))

    assert (await outbox.counts("mom")).get(OutboxState.DONE.value) == 1
    assert delivery.calls == 1


async def test_the_bytes_are_dropped_not_encoded(database: Database) -> None:
    """A preview blob is content, and content does not belong in the queue."""
    delivery = RecordingMedia(fail_times=1)
    router, outbox, _ = build(database, delivery, media_sender(delivery))

    photo = MaxAttachment(
        kind=AttachmentKind.PHOTO,
        raw={"photoId": 7, "preview": b"\x89PNG binary", "token": "keep-me"},
    )
    await router.on_max_message(incoming(1, attachments=(photo,)))

    import json

    claimed = await outbox.claim_due("mom")
    stored = json.loads(claimed[0].payload_json)["attachments"][0]["raw"]
    assert stored["photoId"] == 7
    assert stored["token"] == "keep-me", "what resolve() needs is kept"
    assert "preview" not in stored, "what it does not need is dropped"


async def test_an_unstorable_payload_becomes_a_failed_job_not_a_bypass(
    database: Database,
) -> None:
    """The fallback used to send directly, past the queue. It no longer exists.

    A weaker guarantee is not worth an escape hatch that a later reader would
    take for a supported route — that bypass is exactly what this whole path was
    built to remove. The message is not delivered, and it is not lost: it sits
    on the owner's /failed list with a reason.
    """
    from unittest.mock import patch

    delivery = RecordingMedia()
    router, outbox, messages = build(database, delivery, media_sender(delivery))

    # The sanitiser degrades everything it is given, so the only way to reach
    # the failure branch is for sanitising itself to come back unserialisable.
    with patch("bridge.routing.delivery.json_safe", lambda value, depth=0: {"x": object()}):
        await router.on_max_message(incoming(1, attachments=(attachment(),)))

    assert delivery.calls == 0, "nothing may be sent outside the queue"

    waiting = await outbox.needing_attention("mom")
    assert [item.state for item in waiting] == [OutboxState.FAILED]
    assert "could not be stored" in (waiting[0].last_error or "")

    # The claim row is not left orphaned: it has a job, and the job explains
    # itself.
    link = await messages.by_max_message(MOM.max_chat_id, 1, MOM.bot_id)
    assert link is not None
    assert link.telegram_message_id is None


async def test_a_sanitisable_payload_is_stored_rather_than_failed(
    database: Database,
) -> None:
    """Only what survives nothing at all becomes a failure."""
    delivery = RecordingMedia()
    router, outbox, _ = build(database, delivery, media_sender(delivery))

    awkward = MaxAttachment(
        kind=AttachmentKind.PHOTO,
        raw={"photoId": 3, "thumb": b"binary", "nested": {"blob": bytearray(b"x")}},
    )
    await router.on_max_message(incoming(1, attachments=(awkward,)))

    assert delivery.calls == 1
    assert (await outbox.counts("mom")).get(OutboxState.DONE.value) == 1
    assert await outbox.needing_attention("mom") == []


# ----------------------------------------------- where the remote send begins


async def test_preparation_does_not_look_like_sending(database: Database) -> None:
    """Resolve, download and validate all happen before the mark goes down.

    Stamping at the top of the attempt meant a process that died while
    *downloading* a MAX attachment came back AMBIGUOUS — asking the owner to
    adjudicate a message that had never left the machine.
    """
    seen_during_preparation: list[int | None] = []

    async def sender(
        kind: str, direction: Direction, payload: dict[str, Any], sending: Any = None
    ) -> int | None:
        # This stands in for resolve + download + validate.
        row = await database.query_one("SELECT send_started_at FROM outbox WHERE id = 1")
        seen_during_preparation.append(row["send_started_at"] if row else None)
        await sending()
        return 700

    router, _, _ = build(database, RecordingMedia(), sender)
    await router.on_max_message(incoming(1, attachments=(attachment(),)))

    assert seen_during_preparation == [None], "the mark was set before preparation finished"


async def test_a_crash_while_preparing_is_a_retry_not_a_question(
    database: Database,
) -> None:
    """The whole point: nothing left the machine, so nothing needs a decision."""
    outbox = OutboxRepository(database)

    async def dies_while_preparing(
        kind: str, direction: Direction, payload: dict[str, Any], sending: Any = None
    ) -> int | None:
        raise ConnectionError("MAX CDN went away mid-download")

    router, _, _ = build(database, RecordingMedia(), dies_while_preparing)
    await router.on_max_message(incoming(1, attachments=(attachment(),)))

    # Simulate the process dying: the row is left as the attempt left it.
    requeued, ambiguous = await outbox.requeue_inflight()
    assert (requeued, ambiguous) == (0, 0), "already back on the queue, not in flight"
    assert (await outbox.counts("mom")).get(OutboxState.PENDING.value) == 1


async def test_a_crash_after_the_hook_is_ambiguous(database: Database) -> None:
    """Past the hook Telegram may already hold the message."""
    outbox = OutboxRepository(database)

    async def dies_after_announcing(
        kind: str, direction: Direction, payload: dict[str, Any], sending: Any = None
    ) -> int | None:
        await sending()
        # The process is killed here — no exception reaches the pipe.
        raise KeyboardInterrupt

    router, _, _ = build(database, RecordingMedia(), dies_after_announcing)
    with pytest.raises(KeyboardInterrupt):
        await router.on_max_message(incoming(1, attachments=(attachment(),)))

    requeued, ambiguous = await outbox.requeue_inflight()
    assert (requeued, ambiguous) == (0, 1), "the send had begun; the result is unknown"


async def test_the_album_hook_fires_exactly_once(database: Database) -> None:
    """Several attachments, one announcement — not one per part."""
    calls = 0

    async def counting(
        kind: str, direction: Direction, payload: dict[str, Any], sending: Any = None
    ) -> int | None:
        nonlocal calls
        for _ in payload["attachments"]:
            await sending()
            calls += 1
        return 700

    database_marks: list[int] = []
    router, _outbox, _ = build(database, RecordingMedia(), counting)
    await router.on_max_message(
        incoming(1, attachments=(attachment(), attachment(), attachment()))
    )

    # The hook is idempotent at the storage level: three calls, one row, one
    # timestamp. The real latch lives in MaxMediaDelivery and is covered below.
    assert calls == 3
    row = await database.query_one("SELECT send_started_at FROM outbox WHERE id = 1")
    assert row is not None
    database_marks.append(row["send_started_at"])
    assert database_marks[0] is None, "cleared by mark_done after delivery"
