"""WP2 — schema, migrations and repositories."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from bridge.storage import (
    LATEST_VERSION,
    BridgeRecord,
    BridgeRepository,
    BridgeState,
    BridgeStateRepository,
    Database,
    Direction,
    MessageMapRepository,
    OutboxRepository,
    OutboxState,
    PendingContactRepository,
    PendingContactState,
    ReactionSnapshot,
    ReactionStateRepository,
    ReadStateRepository,
    now_ms,
)

BOT_ID = 555
CHAT_ID = -100
MAX_CHAT = 777


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


async def test_migrates_from_scratch(database: Database) -> None:
    assert await database.schema_version() == LATEST_VERSION

    tables = {
        row["name"]
        for row in await database.query("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "message_map",
        "outbox",
        "bridge_state",
        "bridges",
        "read_state",
        "reaction_state",
        "pending_contacts",
        "pending_inbox",
        "telegram_inbox",
        "telegram_offset",
        "media_group_part",
    } <= tables

    # V1 created it and no line of code ever read or wrote it; V8 drops it and
    # stores album parts individually instead.
    assert "pending_media_group" not in tables


async def test_wal_is_enabled(database: Database) -> None:
    row = await database.query_one("PRAGMA journal_mode")
    assert row is not None
    assert row[0].lower() == "wal"


async def test_migration_is_idempotent(tmp_path: Path) -> None:
    """Reopening an existing database must not re-run steps or double-count."""
    path = tmp_path / "bridge.db"
    first = await Database.connect(path)
    await first.close()

    second = await Database.connect(path)
    try:
        assert await second.migrate() == LATEST_VERSION
        rows = await second.query("SELECT version FROM schema_version ORDER BY version")
        assert [row["version"] for row in rows] == list(range(1, LATEST_VERSION + 1))
    finally:
        await second.close()


async def test_schema_holds_no_token_columns(database: Database) -> None:
    """WP2 invariant: the database stores variable *names*, never secrets."""
    rows = await database.query("SELECT name, sql FROM sqlite_master WHERE type = 'table'")
    for row in rows:
        sql = (row["sql"] or "").lower()
        # `token_env` is allowed; a bare `token` column is not.
        assert "token " not in sql.replace("token_env ", "")


async def test_claim_from_max_deduplicates(database: Database) -> None:
    messages = MessageMapRepository(database)

    first = await messages.claim_from_max(
        bridge_name="mom",
        max_chat_id=MAX_CHAT,
        max_message_id=42,
        telegram_bot_id=BOT_ID,
        telegram_chat_id=CHAT_ID,
    )
    second = await messages.claim_from_max(
        bridge_name="mom",
        max_chat_id=MAX_CHAT,
        max_message_id=42,
        telegram_bot_id=BOT_ID,
        telegram_chat_id=CHAT_ID,
    )

    assert first is not None
    assert second is None, "a replayed MAX event must not create a second row"

    await messages.attach_telegram_message(first, 9001)
    link = await messages.by_max_message(MAX_CHAT, 42, BOT_ID)
    assert link is not None
    assert link.telegram_message_id == 9001
    assert link.direction is Direction.MAX_TO_TG


async def test_two_bots_may_hold_the_same_max_message(database: Database) -> None:
    """The dedup key includes the bot: a second bridge is a separate delivery."""
    messages = MessageMapRepository(database)

    assert await messages.claim_from_max(
        bridge_name="mom",
        max_chat_id=MAX_CHAT,
        max_message_id=42,
        telegram_bot_id=BOT_ID,
        telegram_chat_id=CHAT_ID,
    )
    assert await messages.claim_from_max(
        bridge_name="dad",
        max_chat_id=MAX_CHAT,
        max_message_id=42,
        telegram_bot_id=BOT_ID + 1,
        telegram_chat_id=CHAT_ID,
    )


async def test_loop_guard_recognises_our_own_echo(database: Database) -> None:
    messages = MessageMapRepository(database)

    link_id = await messages.record_from_telegram(
        bridge_name="mom",
        max_chat_id=MAX_CHAT,
        telegram_bot_id=BOT_ID,
        telegram_chat_id=CHAT_ID,
        telegram_message_id=17,
    )
    await messages.attach_max_message(link_id, 12345)

    assert await messages.is_echo_of_our_own(MAX_CHAT, 12345) is True
    assert await messages.is_echo_of_our_own(MAX_CHAT, 999) is False


async def test_pending_telegram_rows_do_not_collide(database: Database) -> None:
    """Two messages sent to MAX before it answers must both be storable."""
    messages = MessageMapRepository(database)

    for telegram_message_id in (1, 2):
        await messages.record_from_telegram(
            bridge_name="mom",
            max_chat_id=MAX_CHAT,
            telegram_bot_id=BOT_ID,
            telegram_chat_id=CHAT_ID,
            telegram_message_id=telegram_message_id,
        )

    rows = await database.query("SELECT COUNT(*) AS n FROM message_map")
    assert rows[0]["n"] == 2


async def test_last_outgoing_is_the_newest_delivered(database: Database) -> None:
    messages = MessageMapRepository(database)

    for max_message_id, telegram_message_id in ((1, 11), (2, 12)):
        link_id = await messages.claim_from_max(
            bridge_name="mom",
            max_chat_id=MAX_CHAT,
            max_message_id=max_message_id,
            telegram_bot_id=BOT_ID,
            telegram_chat_id=CHAT_ID,
        )
        assert link_id is not None
        await messages.attach_telegram_message(link_id, telegram_message_id)

    # Claimed but never delivered: must not win, there is nothing to edit yet.
    await messages.claim_from_max(
        bridge_name="mom",
        max_chat_id=MAX_CHAT,
        max_message_id=3,
        telegram_bot_id=BOT_ID,
        telegram_chat_id=CHAT_ID,
    )

    last = await messages.last_outgoing("mom")
    assert last is not None
    assert last.telegram_message_id == 12


async def test_outbox_roundtrip(database: Database) -> None:
    outbox = OutboxRepository(database)

    item_id = await outbox.enqueue(
        bridge_name="mom",
        direction=Direction.TG_TO_MAX,
        kind="text",
        payload={"text": "hi"},
    )
    assert await outbox.queue_size("mom") == 1

    claimed = await outbox.claim_due("mom")
    assert [item.id for item in claimed] == [item_id]
    # Claimed items are in flight and must not be handed out twice.
    assert await outbox.claim_due("mom") == []

    await outbox.mark_done(item_id)
    assert await outbox.queue_size("mom") == 0


async def test_outbox_retry_then_failure(database: Database) -> None:
    outbox = OutboxRepository(database)
    item_id = await outbox.enqueue(
        bridge_name="mom",
        direction=Direction.TG_TO_MAX,
        kind="text",
        payload={},
    )
    await outbox.claim_due("mom")

    await outbox.mark_retry(item_id, delay_ms=60_000, error="network hiccup")
    # Still queued, but not due yet.
    assert await outbox.queue_size("mom") == 1
    assert await outbox.claim_due("mom") == []

    await outbox.mark_failed(item_id, error="gave up")
    failed = await outbox.failed("mom")
    assert [item.state for item in failed] == [OutboxState.FAILED]
    assert failed[0].last_error == "gave up"
    assert await outbox.queue_size("mom") == 0


async def test_inflight_items_are_requeued_after_a_crash(database: Database) -> None:
    outbox = OutboxRepository(database)
    await outbox.enqueue(bridge_name="mom", direction=Direction.TG_TO_MAX, kind="text", payload={})
    await outbox.claim_due("mom")

    assert await outbox.requeue_inflight() == (1, 0)
    assert len(await outbox.claim_due("mom")) == 1


async def test_outbox_queues_are_isolated_per_bridge(database: Database) -> None:
    outbox = OutboxRepository(database)
    await outbox.enqueue(bridge_name="mom", direction=Direction.TG_TO_MAX, kind="text", payload={})
    await outbox.enqueue(bridge_name="dad", direction=Direction.TG_TO_MAX, kind="text", payload={})

    assert len(await outbox.claim_due("mom")) == 1
    assert await outbox.queue_size("dad") == 1


async def test_bridge_registry_upsert_and_state(database: Database) -> None:
    bridges = BridgeRepository(database)

    await bridges.upsert(BridgeRecord(bridge_name="mom", max_chat_id=MAX_CHAT, token_env="TOK_MOM"))
    await bridges.upsert(
        BridgeRecord(
            bridge_name="mom",
            max_chat_id=MAX_CHAT,
            token_env="TOK_MOM",
            telegram_bot_id=BOT_ID,
            title="Mom",
        )
    )

    record = await bridges.get("mom")
    assert record is not None
    assert record.telegram_bot_id == BOT_ID
    assert record.title == "Mom"
    assert [item.bridge_name for item in await bridges.active()] == ["mom"]

    await bridges.set_state("mom", BridgeState.DISABLED)
    assert await bridges.active() == []
    assert await bridges.by_max_chat(MAX_CHAT) is not None


async def test_one_bot_one_dialog_is_enforced_by_the_schema(database: Database) -> None:
    """The project's core invariant, enforced where it cannot be forgotten.

    Raised as something the guardian can read: an `IntegrityError` in a chat
    tells the owner nothing and tells the code less.
    """
    from bridge.storage import BridgeIdentityConflictError

    bridges = BridgeRepository(database)
    await bridges.upsert(BridgeRecord(bridge_name="mom", max_chat_id=MAX_CHAT, token_env="TOK_MOM"))

    with pytest.raises(BridgeIdentityConflictError, match="диалог MAX"):
        await bridges.upsert(
            BridgeRecord(bridge_name="dad", max_chat_id=MAX_CHAT, token_env="TOK_DAD")
        )


async def test_read_marks_only_move_forward(database: Database) -> None:
    read_state = ReadStateRepository(database)

    assert await read_state.note_contact_read("mom", mark=100, ticked_message_id=5) is True
    # Repeats arrive on every reconnect; they must not re-trigger an edit.
    assert await read_state.note_contact_read("mom", mark=100, ticked_message_id=5) is False
    assert await read_state.note_contact_read("mom", mark=90, ticked_message_id=4) is False
    assert await read_state.note_contact_read("mom", mark=101, ticked_message_id=6) is True

    marks = await read_state.get("mom")
    assert marks.contact_read_mark == 101
    assert marks.last_ticked_message_id == 6

    assert await read_state.note_own_read("mom", mark=50) is True
    assert (await read_state.get("mom")).contact_read_mark == 101, "own read must not clobber"


async def test_reaction_snapshot_roundtrip(database: Database) -> None:
    reactions = ReactionStateRepository(database)

    assert await reactions.get(MAX_CHAT, 1) is None

    await reactions.put(
        ReactionSnapshot(
            max_chat_id=MAX_CHAT, max_message_id=1, counters={"👍": 1}, your_reaction=None
        )
    )
    await reactions.put(
        ReactionSnapshot(
            max_chat_id=MAX_CHAT, max_message_id=1, counters={"👍": 1, "🔥": 2}, your_reaction="🔥"
        )
    )

    snapshot = await reactions.get(MAX_CHAT, 1)
    assert snapshot is not None
    assert snapshot.counters == {"👍": 1, "🔥": 2}
    assert snapshot.your_reaction == "🔥"


async def test_pending_contact_buffer_and_drain(database: Database) -> None:
    pending = PendingContactRepository(database)

    contact = await pending.note_seen(max_chat_id=MAX_CHAT, max_user_id=9, display_name="Aunt")
    assert contact.state is PendingContactState.NEW

    for index in range(3):
        assert await pending.buffer(MAX_CHAT, {"n": index}, cap=10) is True

    assert (await pending.get(MAX_CHAT)).buffered == 3  # type: ignore[union-attr]

    await pending.set_state(MAX_CHAT, PendingContactState.ASKED)
    asked = await pending.get(MAX_CHAT)
    assert asked is not None
    assert asked.state is PendingContactState.ASKED
    assert asked.asked_at is not None

    drained = await pending.drain(MAX_CHAT)
    assert [item["n"] for item in drained] == [0, 1, 2], "replay must keep arrival order"
    assert await pending.drain(MAX_CHAT) == []


async def test_pending_buffer_respects_the_cap(database: Database) -> None:
    """An unknown contact must not be able to fill the disk while we wait."""
    pending = PendingContactRepository(database)
    await pending.note_seen(max_chat_id=MAX_CHAT, max_user_id=None, display_name=None)

    assert await pending.buffer(MAX_CHAT, {"n": 0}, cap=1) is True
    assert await pending.buffer(MAX_CHAT, {"n": 1}, cap=1) is False


async def test_pending_buffer_expires(database: Database) -> None:
    pending = PendingContactRepository(database)
    await pending.note_seen(max_chat_id=MAX_CHAT, max_user_id=None, display_name=None)
    await pending.buffer(MAX_CHAT, {"n": 0}, cap=10)

    assert await pending.expire(older_than_ms=60_000) == 0
    await database.execute("UPDATE pending_inbox SET created_at = ?", (now_ms() - 120_000,))
    assert await pending.expire(older_than_ms=60_000) == 1


async def test_bridge_state_snapshot(database: Database) -> None:
    state = BridgeStateRepository(database)

    await state.note_delivery("mom")
    await state.note_error("mom", "boom")

    snapshot = await state.snapshot("mom")
    assert snapshot is not None
    assert snapshot["last_delivery_at"] is not None
    assert snapshot["last_error"] == "boom"


# ------------------------------------------------- deterministic provisioning


async def test_a_bridge_remembers_the_username_it_must_always_have(
    database: Database,
) -> None:
    """Stored so a rebuild is recognised without re-deriving it from the secret."""
    bridges = BridgeRepository(database)
    await bridges.upsert(
        BridgeRecord(
            bridge_name="c1",
            max_chat_id=236856064,
            token_env="TELEMAX_BOT_C1",
            max_user_id=200000002,
            expected_username="abc_max_bot",
        )
    )

    record = await bridges.by_expected_username("abc_max_bot")
    assert record is not None
    assert record.bridge_name == "c1"

    # A later upsert without one must not wipe it.
    await bridges.upsert(
        BridgeRecord(bridge_name="c1", max_chat_id=236856064, token_env="TELEMAX_BOT_C1")
    )
    again = await bridges.get("c1")
    assert again is not None
    assert again.expected_username == "abc_max_bot"


async def test_the_history_cursor_only_moves_forward(database: Database) -> None:
    """An older tail re-read must not make already-imported messages new again."""
    bridges = BridgeRepository(database)
    await bridges.upsert(
        BridgeRecord(bridge_name="c1", max_chat_id=1, token_env="TELEMAX_BOT_C1")
    )

    assert await bridges.history_cursor("c1") is None
    await bridges.set_history_cursor("c1", 500)
    await bridges.set_history_cursor("c1", 300)

    assert await bridges.history_cursor("c1") == 500


async def test_lifecycle_columns_are_written_without_clobbering(
    database: Database,
) -> None:
    bridges = BridgeRepository(database)
    await bridges.upsert(
        BridgeRecord(bridge_name="c1", max_chat_id=1, token_env="TELEMAX_BOT_C1")
    )

    await bridges.set_lifecycle("c1", expected_username="abc_max_bot", health="healthy")
    await bridges.set_lifecycle("c1", lifecycle_state="healthy")

    row = await database.query_one("SELECT * FROM bridges WHERE bridge_name = 'c1'")
    assert row is not None
    assert row["expected_username"] == "abc_max_bot"
    assert row["lifecycle_state"] == "healthy"
    assert row["health"] == "healthy"


# ------------------------------------------------------- wiping for a re-import


async def test_placed_messages_carry_both_sides_of_a_delivery(
    database: Database,
) -> None:
    """A message can exist twice over: the bot's copy and the owner's own."""
    messages = MessageMapRepository(database)
    first = await messages.claim_from_max(
        bridge_name="mom",
        max_chat_id=MAX_CHAT,
        max_message_id=11,
        telegram_bot_id=BOT_ID,
        telegram_chat_id=CHAT_ID,
    )
    assert first is not None
    await messages.attach_telegram_message(first, 900)
    second = await messages.claim_from_max(
        bridge_name="mom",
        max_chat_id=MAX_CHAT,
        max_message_id=12,
        telegram_bot_id=BOT_ID,
        telegram_chat_id=CHAT_ID,
    )
    assert second is not None
    await messages.attach_owner_message(second, 901)

    placed = await messages.placed_by("mom")

    assert [item.link_id for item in placed] == [first, second]
    assert placed[0].ids == (900,)
    assert placed[1].ids == (901,), "the owner's own copy is reachable too"
    assert placed[0].telegram_chat_id == CHAT_ID


async def test_a_claimed_but_undelivered_row_has_nothing_to_remove(
    database: Database,
) -> None:
    """Claimed and never sent: no Telegram message, and the row is only in the way."""
    messages = MessageMapRepository(database)
    await messages.claim_from_max(
        bridge_name="mom",
        max_chat_id=MAX_CHAT,
        max_message_id=13,
        telegram_bot_id=BOT_ID,
        telegram_chat_id=CHAT_ID,
    )

    placed = await messages.placed_by("mom")

    assert len(placed) == 1
    assert placed[0].ids == ()


async def test_forgetting_a_row_reopens_the_message_for_delivery(
    database: Database,
) -> None:
    """The row *is* the dedup key, which is what makes a re-import possible.

    And why the order matters the other way round: a row dropped for a message
    that is still in the chat would let a re-import place a second copy of it.
    """
    messages = MessageMapRepository(database)
    claim = {
        "bridge_name": "mom",
        "max_chat_id": MAX_CHAT,
        "max_message_id": 14,
        "telegram_bot_id": BOT_ID,
        "telegram_chat_id": CHAT_ID,
    }
    first = await messages.claim_from_max(**claim)  # type: ignore[arg-type]
    assert first is not None
    assert await messages.claim_from_max(**claim) is None, "deduplicated"  # type: ignore[arg-type]

    await messages.forget(first)

    assert await messages.claim_from_max(**claim) is not None  # type: ignore[arg-type]


async def test_only_this_bridge_s_messages_are_listed(database: Database) -> None:
    messages = MessageMapRepository(database)
    for name, chat, message_id in (("mom", 777, 21), ("dad", 888, 22)):
        link = await messages.claim_from_max(
            bridge_name=name,
            max_chat_id=chat,
            max_message_id=message_id,
            telegram_bot_id=BOT_ID,
            telegram_chat_id=CHAT_ID,
        )
        assert link is not None
        await messages.attach_telegram_message(link, 500 + message_id)

    assert len(await messages.placed_by("mom")) == 1
    assert len(await messages.placed_by("dad")) == 1
