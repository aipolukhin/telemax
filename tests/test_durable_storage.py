"""The durable half of delivery: inbox, leases, ordering, ambiguity.

Everything here answers one question — after a crash, is the message still
somewhere the process can find it?
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from bridge.storage import (
    Database,
    Direction,
    InboxState,
    MediaGroupRepository,
    OutboxRepository,
    OutboxState,
    TelegramInboxRepository,
    now_ms,
)

BOT_ID = 555


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


# ------------------------------------------------------------ Telegram inbox


async def test_update_is_stored_before_anything_else(database: Database) -> None:
    inbox = TelegramInboxRepository(database)
    stored = await inbox.store(bot_id=BOT_ID, update_id=10, payload={"text": "привет"})
    assert stored is not None
    assert await inbox.depth(BOT_ID) == 1


async def test_the_same_update_is_only_stored_once(database: Database) -> None:
    """Telegram replays whatever was never acknowledged. That must be free."""
    inbox = TelegramInboxRepository(database)
    first = await inbox.store(bot_id=BOT_ID, update_id=10, payload={"text": "привет"})
    second = await inbox.store(bot_id=BOT_ID, update_id=10, payload={"text": "привет"})

    assert first is not None
    assert second is None
    assert await inbox.depth(BOT_ID) == 1


async def test_the_same_update_id_from_another_bot_is_separate(database: Database) -> None:
    """Update ids are per bot; two bridges must not collide."""
    inbox = TelegramInboxRepository(database)
    assert await inbox.store(bot_id=BOT_ID, update_id=10, payload={}) is not None
    assert await inbox.store(bot_id=999, update_id=10, payload={}) is not None
    assert await inbox.depth() == 2


async def test_unhandled_updates_survive_a_restart(tmp_path: Path) -> None:
    """The crash-window test: stored, never handled, still there next time."""
    path = tmp_path / "bridge.db"
    first = await Database.connect(path)
    await TelegramInboxRepository(first).store(
        bot_id=BOT_ID, update_id=7, payload={"text": "не потеряй меня"}
    )
    await first.close()  # stands in for the process dying

    second = await Database.connect(path)
    try:
        inbox = TelegramInboxRepository(second)
        pending = await inbox.claim_open()
        assert [update.update_id for update in pending] == [7]
        assert "не потеряй меня" in pending[0].payload_json
    finally:
        await second.close()


async def test_claim_is_in_arrival_order(database: Database) -> None:
    inbox = TelegramInboxRepository(database)
    for update_id in (1, 2, 3):
        await inbox.store(bot_id=BOT_ID, update_id=update_id, payload={"n": update_id})

    claimed = await inbox.claim_open()
    assert [update.update_id for update in claimed] == [1, 2, 3]


async def test_a_claimed_update_is_not_claimed_twice(database: Database) -> None:
    inbox = TelegramInboxRepository(database)
    await inbox.store(bot_id=BOT_ID, update_id=1, payload={})

    assert len(await inbox.claim_open()) == 1
    assert await inbox.claim_open() == []


async def test_an_expired_lease_returns_the_update(database: Database) -> None:
    """A worker that died holding a lease must not keep the message hostage."""
    inbox = TelegramInboxRepository(database)
    await inbox.store(bot_id=BOT_ID, update_id=1, payload={})

    assert len(await inbox.claim_open(lease_ms=-1)) == 1  # already expired
    assert len(await inbox.claim_open()) == 1


async def test_done_clears_the_payload(database: Database) -> None:
    """Private content does not outlive its delivery."""
    inbox = TelegramInboxRepository(database)
    inbox_id = await inbox.store(bot_id=BOT_ID, update_id=1, payload={"text": "личное"})
    assert inbox_id is not None

    await inbox.mark_done(inbox_id)
    row = await database.query_one("SELECT * FROM telegram_inbox WHERE id = ?", (inbox_id,))
    assert row is not None
    assert row["state"] == InboxState.DONE.value
    assert "личное" not in row["payload_json"]
    assert await inbox.depth(BOT_ID) == 0


async def test_offset_is_remembered_and_never_walks_backwards(database: Database) -> None:
    inbox = TelegramInboxRepository(database)
    await inbox.remember_offset(BOT_ID, 100)
    await inbox.remember_offset(BOT_ID, 50)  # a late writer must not rewind us
    assert await inbox.offset(BOT_ID) == 100


# ------------------------------------------------------------------- outbox


async def test_enqueue_is_idempotent_per_source(database: Database) -> None:
    """A replayed MAX event finds its job instead of creating a second."""
    outbox = OutboxRepository(database)
    common = {
        "bridge_name": "dad",
        "direction": Direction.MAX_TO_TG,
        "kind": "text",
        "payload": {"text": "привет"},
        "source_key": "max:777:42",
    }
    first = await outbox.enqueue(**common)  # type: ignore[arg-type]
    second = await outbox.enqueue(**common)  # type: ignore[arg-type]

    assert first == second
    assert (await outbox.counts("dad")).get(OutboxState.PENDING.value) == 1


async def test_order_is_kept_within_a_bridge(database: Database) -> None:
    """A job waiting out its backoff holds the queue behind it.

    Otherwise message 2 overtakes message 1 and the contact reads the
    conversation out of order.
    """
    outbox = OutboxRepository(database)
    first = await outbox.enqueue(
        bridge_name="dad", direction=Direction.MAX_TO_TG, kind="text", payload={"n": 1}
    )
    await outbox.enqueue(
        bridge_name="dad", direction=Direction.MAX_TO_TG, kind="text", payload={"n": 2}
    )

    await outbox.claim_due("dad")
    await outbox.mark_retry(first, delay_ms=60_000, error="network")

    # The head is not due, so nothing behind it may go.
    assert await outbox.claim_due("dad") == []


async def test_a_failed_job_stops_blocking_the_queue(database: Database) -> None:
    """Head-of-line ordering must not become a permanent wedge."""
    outbox = OutboxRepository(database)
    first = await outbox.enqueue(
        bridge_name="dad", direction=Direction.MAX_TO_TG, kind="text", payload={"n": 1}
    )
    await outbox.enqueue(
        bridge_name="dad", direction=Direction.MAX_TO_TG, kind="text", payload={"n": 2}
    )

    await outbox.claim_due("dad")
    await outbox.mark_failed(first, error="permanent")

    claimed = await outbox.claim_due("dad")
    assert len(claimed) == 1


async def test_one_bridge_does_not_block_another(database: Database) -> None:
    outbox = OutboxRepository(database)
    stuck = await outbox.enqueue(
        bridge_name="dad", direction=Direction.MAX_TO_TG, kind="text", payload={}
    )
    await outbox.enqueue(
        bridge_name="mom", direction=Direction.MAX_TO_TG, kind="text", payload={}
    )

    await outbox.claim_due("dad")
    await outbox.mark_retry(stuck, delay_ms=60_000, error="network")

    assert await outbox.claim_due("dad") == []
    assert len(await outbox.claim_due("mom")) == 1


async def test_an_expired_lease_comes_back(database: Database) -> None:
    """The crash path: LEASED with a dead worker returns to PENDING."""
    outbox = OutboxRepository(database)
    await outbox.enqueue(
        bridge_name="dad", direction=Direction.MAX_TO_TG, kind="text", payload={}
    )

    claimed = await outbox.claim_due("dad", lease_ms=-1)
    assert len(claimed) == 1
    assert await outbox.claim_due("dad") == []  # still held, lease not swept yet

    assert await outbox.reclaim_expired_leases() == (1, 0)
    assert len(await outbox.claim_due("dad")) == 1


async def test_a_live_lease_is_not_reclaimed(database: Database) -> None:
    outbox = OutboxRepository(database)
    await outbox.enqueue(
        bridge_name="dad", direction=Direction.MAX_TO_TG, kind="text", payload={}
    )
    await outbox.claim_due("dad", lease_ms=60_000)

    assert await outbox.reclaim_expired_leases() == (0, 0)


async def test_delivered_records_the_remote_id_and_drops_the_payload(
    database: Database,
) -> None:
    outbox = OutboxRepository(database)
    item_id = await outbox.enqueue(
        bridge_name="dad", direction=Direction.MAX_TO_TG, kind="text", payload={"text": "личное"}
    )

    await outbox.mark_done(item_id, remote_message_id=4242)

    row = await database.query_one("SELECT * FROM outbox WHERE id = ?", (item_id,))
    assert row is not None
    assert row["state"] == OutboxState.DONE.value
    assert row["remote_message_id"] == 4242
    assert "личное" not in row["payload_json"]


async def test_ambiguous_is_terminal_and_visible(database: Database) -> None:
    """It must not retry itself, and the owner must be able to see it."""
    outbox = OutboxRepository(database)
    item_id = await outbox.enqueue(
        bridge_name="dad", direction=Direction.MAX_TO_TG, kind="text", payload={"text": "привет"}
    )
    await outbox.claim_due("dad")
    await outbox.mark_ambiguous(item_id, error="connection lost after send")

    assert await outbox.claim_due("dad") == []
    assert await outbox.reclaim_expired_leases() == (0, 0)

    waiting = await outbox.needing_attention("dad")
    assert [item.state for item in waiting] == [OutboxState.AMBIGUOUS]


async def test_owner_can_retry_an_ambiguous_job(database: Database) -> None:
    outbox = OutboxRepository(database)
    item_id = await outbox.enqueue(
        bridge_name="dad", direction=Direction.MAX_TO_TG, kind="text", payload={"text": "привет"}
    )
    await outbox.claim_due("dad")
    await outbox.mark_ambiguous(item_id, error="lost")

    assert await outbox.retry_now(item_id) is True
    claimed = await outbox.claim_due("dad")
    assert [item.id for item in claimed] == [item_id]


async def test_owner_can_mark_an_ambiguous_job_resolved(database: Database) -> None:
    outbox = OutboxRepository(database)
    item_id = await outbox.enqueue(
        bridge_name="dad", direction=Direction.MAX_TO_TG, kind="text", payload={"text": "привет"}
    )
    await outbox.mark_ambiguous(item_id, error="lost")

    assert await outbox.resolve(item_id) is True
    assert await outbox.needing_attention("dad") == []


async def test_a_delivered_job_cannot_be_retried(database: Database) -> None:
    """There is nothing left to send, and re-sending would duplicate."""
    outbox = OutboxRepository(database)
    item_id = await outbox.enqueue(
        bridge_name="dad", direction=Direction.MAX_TO_TG, kind="text", payload={"text": "привет"}
    )
    await outbox.mark_done(item_id, remote_message_id=1)

    assert await outbox.retry_now(item_id) is False


async def test_ttl_retires_a_job_that_never_went(database: Database) -> None:
    outbox = OutboxRepository(database)
    item_id = await outbox.enqueue(
        bridge_name="dad",
        direction=Direction.MAX_TO_TG,
        kind="text",
        payload={"text": "вчерашнее"},
        ttl_ms=-1,
    )

    assert await outbox.expire_overdue() == 1
    row = await database.query_one("SELECT state FROM outbox WHERE id = ?", (item_id,))
    assert row is not None
    assert row["state"] == OutboxState.EXPIRED.value


async def test_status_numbers_are_available(database: Database) -> None:
    outbox = OutboxRepository(database)
    await outbox.enqueue(
        bridge_name="dad", direction=Direction.MAX_TO_TG, kind="text", payload={}
    )
    assert (await outbox.counts("dad")).get(OutboxState.PENDING.value) == 1

    age = await outbox.oldest_pending_ms("dad")
    assert age is not None and age >= 0
    assert await outbox.oldest_pending_ms("mom") is None


# -------------------------------------------------------------- media groups


async def test_album_parts_survive_a_restart(tmp_path: Path) -> None:
    """A crash between two photos must not eat the first one."""
    path = tmp_path / "bridge.db"
    first = await Database.connect(path)
    groups = MediaGroupRepository(first)
    await groups.add_part(
        media_group_id="alb1",
        bridge_name="dad",
        bot_id=BOT_ID,
        telegram_message_id=1,
        payload={"file_id": "aaa"},
    )
    await groups.add_part(
        media_group_id="alb1",
        bridge_name="dad",
        bot_id=BOT_ID,
        telegram_message_id=2,
        payload={"file_id": "bbb"},
    )
    await first.close()

    second = await Database.connect(path)
    try:
        recovered = MediaGroupRepository(second)
        assert await recovered.open_groups() == ["alb1"]
        parts = await recovered.parts("alb1")
        assert [part.telegram_message_id for part in parts] == [1, 2]
        assert "aaa" in parts[0].payload_json
    finally:
        await second.close()


async def test_album_parts_keep_their_order(database: Database) -> None:
    groups = MediaGroupRepository(database)
    for message_id in (11, 12, 13):
        await groups.add_part(
            media_group_id="alb",
            bridge_name="dad",
            bot_id=BOT_ID,
            telegram_message_id=message_id,
            payload={"n": message_id},
        )

    assert [part.telegram_message_id for part in await groups.parts("alb")] == [11, 12, 13]


async def test_a_replayed_album_part_is_not_stored_twice(database: Database) -> None:
    groups = MediaGroupRepository(database)
    common = {
        "media_group_id": "alb",
        "bridge_name": "dad",
        "bot_id": BOT_ID,
        "telegram_message_id": 1,
        "payload": {"file_id": "aaa"},
    }
    assert await groups.add_part(**common) is True  # type: ignore[arg-type]
    assert await groups.add_part(**common) is False  # type: ignore[arg-type]
    assert len(await groups.parts("alb")) == 1


async def test_a_flushed_album_is_cleared(database: Database) -> None:
    groups = MediaGroupRepository(database)
    await groups.add_part(
        media_group_id="alb",
        bridge_name="dad",
        bot_id=BOT_ID,
        telegram_message_id=1,
        payload={},
    )
    await groups.clear("alb")

    assert await groups.parts("alb") == []
    assert await groups.open_groups() == []


async def test_only_settled_albums_are_reassembled(database: Database) -> None:
    """A group whose last part arrived a moment ago is still being filled."""
    groups = MediaGroupRepository(database)
    await groups.add_part(
        media_group_id="fresh",
        bridge_name="dad",
        bot_id=BOT_ID,
        telegram_message_id=1,
        payload={},
    )

    assert await groups.open_groups(older_than_ms=60_000) == []
    assert await groups.open_groups(older_than_ms=0) == ["fresh"]
    assert now_ms() > 0


async def test_leased_updates_are_taken_back_at_startup(tmp_path: Path) -> None:
    """A crash mid-update must not cost the full lease before anyone retries.

    Found live: the process was killed between storing an update and carrying
    it, and the row then sat in `leased` for the remainder of its two-minute
    lease — while the restarted bridge, which existed precisely to deliver it,
    did nothing. The outbox already had this sweep; the inbox did not.
    """
    path = tmp_path / "bridge.db"
    first = await Database.connect(path)
    inbox = TelegramInboxRepository(first)
    await inbox.store(bot_id=BOT_ID, update_id=1, payload={"text": "привет"})
    claimed = await inbox.claim_open(lease_ms=600_000)
    assert len(claimed) == 1
    await first.close()  # the process dies holding the lease

    second = await Database.connect(path)
    try:
        recovered = TelegramInboxRepository(second)
        assert await recovered.requeue_leased() == 1
        # Available immediately, not in ten minutes.
        assert [u.update_id for u in await recovered.claim_open()] == [1]
    finally:
        await second.close()


async def test_startup_reclaim_leaves_finished_updates_alone(database: Database) -> None:
    inbox = TelegramInboxRepository(database)
    inbox_id = await inbox.store(bot_id=BOT_ID, update_id=1, payload={})
    assert inbox_id is not None
    await inbox.mark_done(inbox_id)

    assert await inbox.requeue_leased() == 0
    assert await inbox.claim_open() == []
