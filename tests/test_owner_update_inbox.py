"""Every owner update is written down before anything is derived from it.

Telethon does not replay: `pts` advances before the handler runs, a raising
handler is logged rather than retried, and `catch_up` is off. On 2026-08-05 a
database outage swallowed seven owner updates on exactly that path.

The guarantee this table buys is narrow and stays narrow. **After the insert
commits, the event survives a crash and will be finished.** Before it commits
Telegram has delivered an update and nothing has written it down, and nothing
here closes that window.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from bridge.storage import (
    Database,
    InboxFamily,
    InboxKey,
    OwnerMessageState,
    OwnerUpdateInboxRepository,
    OwnerUpdateState,
)

ACCOUNT, BOT, MESSAGE = 100000001, 9000000001, 1002400


def _baseline(fingerprint: str = "before") -> OwnerMessageState:
    """What the message was last known to be.

    Present in every dispatch test that expects an edit to be carried: an update
    on a message with **no** baseline proves nothing changed and is deliberately
    not turned into a MAX edit. A body that has moved off a known baseline is
    the case where an edit is real.
    """
    return OwnerMessageState(
        telegram_owner_account_id=ACCOUNT,
        telegram_bot_id=BOT,
        telegram_owner_message_id=MESSAGE,
        content_fingerprint=fingerprint,
        chosen_json="[]",
        pts=1,
        updated_at=0,
    )


def snapshot_key(pts: int = 10, message_id: int = MESSAGE) -> InboxKey:
    return InboxKey(ACCOUNT, BOT, message_id, InboxFamily.SNAPSHOT, pts)


def delete_key(pts: int = 10, message_id: int = MESSAGE) -> InboxKey:
    return InboxKey(ACCOUNT, BOT, message_id, InboxFamily.DELETE, pts)


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


@pytest_asyncio.fixture
async def inbox(database: Database) -> OwnerUpdateInboxRepository:
    return OwnerUpdateInboxRepository(database)


async def remember_snapshot(
    inbox: OwnerUpdateInboxRepository, key: InboxKey, text: str = "привет"
) -> bool:
    return await inbox.remember(
        key, content_text=text, content_fingerprint="f" + str(key.pts), chosen_json="[]"
    )


# ------------------------------------------------------------------ the insert


async def test_a_snapshot_is_written_down(inbox: OwnerUpdateInboxRepository) -> None:
    assert await remember_snapshot(inbox, snapshot_key())
    assert await inbox.counts() == {"open": 1}


async def test_the_same_update_twice_is_one_row(inbox: OwnerUpdateInboxRepository) -> None:
    assert await remember_snapshot(inbox, snapshot_key())
    assert await remember_snapshot(inbox, snapshot_key()) is False
    assert await inbox.counts() == {"open": 1}


async def test_a_replay_cannot_revive_a_dead_row(inbox: OwnerUpdateInboxRepository) -> None:
    """`DO NOTHING`, not an upsert. A catch-up must not reset attempts, take a
    row back from a claimant or un-bury one the owner has been asked about."""
    key = snapshot_key()
    await remember_snapshot(inbox, key)
    await inbox.bury(key, error="malformed")

    assert await remember_snapshot(inbox, key) is False
    assert await inbox.counts() == {"dead": 1}


async def test_a_replay_cannot_reset_attempts(inbox: OwnerUpdateInboxRepository) -> None:
    key = snapshot_key()
    await remember_snapshot(inbox, key)
    await inbox.reopen(key, delay_ms=0, error="MAX away")
    await inbox.reopen(key, delay_ms=0, error="MAX away")

    await remember_snapshot(inbox, key)
    claimed = await inbox.claim_due()
    assert claimed[0].attempts == 2


async def test_two_versions_of_one_message_are_two_rows(
    inbox: OwnerUpdateInboxRepository,
) -> None:
    await remember_snapshot(inbox, snapshot_key(10))
    await remember_snapshot(inbox, snapshot_key(11))
    assert await inbox.counts() == {"open": 2}


async def test_a_batch_delete_is_one_row_per_target(
    inbox: OwnerUpdateInboxRepository,
) -> None:
    """`UpdateDeleteMessages` names several messages and carries one `pts`. The
    message id is in the key, so they do not collide."""
    for message_id in (1, 2, 3):
        assert await inbox.remember(delete_key(50, message_id))
    assert await inbox.counts() == {"open": 3}


async def test_a_delete_stores_no_content(database: Database) -> None:
    inbox = OwnerUpdateInboxRepository(database)
    await inbox.remember(delete_key())
    row = await database.query_one(
        "SELECT content_text, chosen_json FROM owner_update_inbox"
    )
    assert row is not None
    assert row["content_text"] is None and row["chosen_json"] is None


async def test_a_snapshot_without_what_it_is_compared_against_is_refused(
    inbox: OwnerUpdateInboxRepository,
) -> None:
    """A snapshot is read by subtracting it from the last one. Without the
    fingerprint and the reaction set there is nothing to subtract, and the row
    is one that cannot be finished written as though it could."""
    with pytest.raises(Exception, match=r"CHECK constraint"):
        await inbox.remember(snapshot_key())


async def test_a_contact_message_snapshot_keeps_no_text(
    inbox: OwnerUpdateInboxRepository, database: Database
) -> None:
    """Its reactions are the owner's; its words are not. No edit is ever carried
    for one, so there is nothing an exact text would be needed for."""
    assert await inbox.remember(
        snapshot_key(), content_fingerprint="f", chosen_json="[]"
    )
    row = await database.query_one("SELECT content_text FROM owner_update_inbox")
    assert row is not None and row["content_text"] is None


async def test_a_delete_carrying_content_is_refused(
    inbox: OwnerUpdateInboxRepository,
) -> None:
    with pytest.raises(Exception, match=r"CHECK constraint"):
        await inbox.remember(delete_key(), content_text="привет")


# ------------------------------------------------------------- claim and lease


async def test_a_claim_takes_the_row(inbox: OwnerUpdateInboxRepository) -> None:
    await remember_snapshot(inbox, snapshot_key())
    claimed = await inbox.claim_due()
    assert [update.key.pts for update in claimed] == [10]
    assert await inbox.counts() == {"claimed": 1}


async def test_a_claimed_row_is_not_claimed_again(
    inbox: OwnerUpdateInboxRepository,
) -> None:
    await remember_snapshot(inbox, snapshot_key())
    await inbox.claim_due()
    assert await inbox.claim_due() == []


async def test_two_drainers_do_not_both_take_one_row(
    inbox: OwnerUpdateInboxRepository,
) -> None:
    for pts in range(10, 20):
        await remember_snapshot(inbox, snapshot_key(pts))

    first, second = await asyncio.gather(inbox.claim_due(), inbox.claim_due())
    taken = [update.key.pts for update in (*first, *second)]
    assert len(taken) == len(set(taken)) == 10


async def test_an_expired_lease_is_reclaimed(inbox: OwnerUpdateInboxRepository) -> None:
    """The row whose owner died holding it. Nothing else brings it back."""
    await remember_snapshot(inbox, snapshot_key())
    await inbox.claim_due(lease_ms=0)
    reclaimed = await inbox.claim_due()
    assert [update.key.pts for update in reclaimed] == [10]


async def test_a_live_lease_is_left_alone(inbox: OwnerUpdateInboxRepository) -> None:
    await remember_snapshot(inbox, snapshot_key())
    await inbox.claim_due(lease_ms=60_000)
    assert await inbox.claim_due() == []


async def test_a_reopened_row_waits_for_its_backoff(
    inbox: OwnerUpdateInboxRepository,
) -> None:
    key = snapshot_key()
    await remember_snapshot(inbox, key)
    await inbox.claim_due()
    await inbox.reopen(key, delay_ms=60_000, error="MAX away")

    assert await inbox.claim_due() == []
    await inbox.reopen(key, delay_ms=0, error="MAX away")
    assert len(await inbox.claim_due()) == 1


async def test_attempts_count_failures_not_claims(
    inbox: OwnerUpdateInboxRepository,
) -> None:
    """The same convention as the outbox. Two words for one count is how a retry
    limit ends up meaning two things."""
    key = snapshot_key()
    await remember_snapshot(inbox, key)
    for _ in range(3):
        await inbox.claim_due()
        await inbox.reopen(key, delay_ms=0, error="still away")

    claimed = await inbox.claim_due()
    assert claimed[0].attempts == 3


async def test_the_exact_text_survives_for_the_replay(
    inbox: OwnerUpdateInboxRepository,
) -> None:
    """Refetching the message would give whatever it says *now*; a second edit
    will have overwritten the version this row stands for."""
    key = snapshot_key()
    await remember_snapshot(inbox, key, text="стало")
    claimed = await inbox.claim_due()
    assert claimed[0].content_text == "стало"


# ---------------------------------------------------------------- accounting


async def test_an_accounted_row_is_gone(inbox: OwnerUpdateInboxRepository) -> None:
    key = snapshot_key()
    await remember_snapshot(inbox, key)
    await inbox.claim_due()
    await inbox.account(key)
    assert await inbox.counts() == {}


async def test_an_accounted_row_does_not_run_again(
    inbox: OwnerUpdateInboxRepository,
) -> None:
    key = snapshot_key()
    await remember_snapshot(inbox, key)
    await inbox.account(key)
    assert await inbox.claim_due() == []


async def test_the_text_is_gone_once_the_work_is_done(database: Database) -> None:
    """It is the one piece of message content kept at rest, and only while the
    work is unfinished."""
    inbox = OwnerUpdateInboxRepository(database)
    key = snapshot_key()
    await remember_snapshot(inbox, key, text="секрет")
    await inbox.account(key)

    rows = await database.query("SELECT content_text FROM owner_update_inbox")
    assert rows == []


async def test_a_crash_between_accounted_and_delete_is_harmless(
    database: Database,
) -> None:
    """The two statements are separate on purpose: an accounted row that was not
    deleted is skipped and swept, while a row deleted before its effects were
    durable would be an event that quietly never happened."""
    inbox = OwnerUpdateInboxRepository(database)
    key = snapshot_key()
    await remember_snapshot(inbox, key)
    await database.execute(
        "UPDATE owner_update_inbox SET state = 'accounted', accounted_at = 1,"
        " content_text = NULL, chosen_json = NULL"
    )

    assert await inbox.claim_due() == []
    assert await inbox.sweep_accounted() == 1
    assert await inbox.counts() == {}


async def test_the_sweep_cannot_touch_unfinished_work(
    inbox: OwnerUpdateInboxRepository,
) -> None:
    """An old row that is not accounted is the problem, not the litter. Deleting
    it by age is how an unfinished event disappears without anybody deciding."""
    await remember_snapshot(inbox, snapshot_key(10))
    await remember_snapshot(inbox, snapshot_key(11))
    await inbox.claim_due(limit=1)
    await inbox.bury(snapshot_key(11), error="malformed")

    assert await inbox.sweep_accounted() == 0
    assert await inbox.counts() == {"claimed": 1, "dead": 1}


# ---------------------------------------------------------------- dead rows


async def test_a_dead_row_stays_and_is_visible(inbox: OwnerUpdateInboxRepository) -> None:
    key = snapshot_key()
    await remember_snapshot(inbox, key)
    await inbox.bury(key, error="mapping conflict")

    stuck = await inbox.stuck()
    assert [update.key.pts for update in stuck] == [10]
    assert stuck[0].state is OwnerUpdateState.DEAD
    assert await inbox.claim_due() == []


async def test_a_dead_row_can_be_retried_on_the_owners_word(
    inbox: OwnerUpdateInboxRepository,
) -> None:
    key = snapshot_key()
    await remember_snapshot(inbox, key)
    await inbox.bury(key, error="mapping conflict")

    assert await inbox.retry(key)
    assert len(await inbox.claim_due()) == 1


async def test_archiving_removes_the_row_without_running_it(
    inbox: OwnerUpdateInboxRepository,
) -> None:
    key = snapshot_key()
    await remember_snapshot(inbox, key)
    await inbox.bury(key, error="mapping conflict")

    assert await inbox.archive(key)
    assert await inbox.counts() == {}


async def test_archive_and_retry_only_touch_dead_rows(
    inbox: OwnerUpdateInboxRepository,
) -> None:
    key = snapshot_key()
    await remember_snapshot(inbox, key)
    assert await inbox.archive(key) is False
    assert await inbox.retry(key) is False
    assert await inbox.counts() == {"open": 1}


async def test_what_is_shown_about_a_dead_row_carries_no_content(
    inbox: OwnerUpdateInboxRepository,
) -> None:
    key = snapshot_key()
    await remember_snapshot(inbox, key, text="секретный текст")
    await inbox.bury(key, error="mapping conflict")

    stuck = await inbox.stuck()
    assert stuck[0].last_error == "mapping conflict"
    assert "секретный" not in str(stuck[0].last_error)


async def test_the_oldest_unfinished_age_is_reported(
    inbox: OwnerUpdateInboxRepository,
) -> None:
    assert await inbox.oldest_open_ms() is None
    await remember_snapshot(inbox, snapshot_key())
    assert await inbox.oldest_open_ms() is not None


# ---------------------------------------------------------------- the migration


async def test_v18_lands_on_a_v17_database_without_touching_it(tmp_path: Path) -> None:
    import sqlite3

    from bridge.storage.migrations import MIGRATIONS

    path = tmp_path / "bridge.db"
    with sqlite3.connect(path) as raw:
        raw.execute(
            "CREATE TABLE schema_version (version INTEGER NOT NULL,"
            " applied_at INTEGER NOT NULL)"
        )
        for version, statements in MIGRATIONS:
            if version > 17:
                continue
            for statement in statements:
                raw.execute(statement)
            raw.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, 0)", (version,)
            )
        raw.execute(
            "INSERT INTO message_map (bridge_name, max_chat_id, telegram_bot_id,"
            " telegram_chat_id, direction, source_marker, created_at)"
            " VALUES ('mom', 1, 2, 3, 'max_to_tg', 'from_max', 0)"
        )
        raw.execute(
            "INSERT INTO owner_message_state (telegram_owner_account_id, telegram_bot_id,"
            " telegram_owner_message_id, content_fingerprint, chosen_json, pts, updated_at)"
            " VALUES (1, 2, 3, 'f', '[]', 5, 0)"
        )

    database = await Database.connect(path)
    try:
        assert await database.schema_version() == 20
        assert len(await database.query("SELECT id FROM message_map")) == 1
        assert len(await database.query("SELECT pts FROM owner_message_state")) == 1
        assert await OwnerUpdateInboxRepository(database).counts() == {}
        index = await database.query(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?",
            ("owner_update_inbox_due",),
        )
        assert len(index) == 1
        check = await database.query_one("PRAGMA integrity_check")
        assert check is not None and check[0] == "ok"
    finally:
        await database.close()


async def test_a_failing_v18_leaves_the_schema_at_v17(tmp_path: Path) -> None:
    """A half-applied step that recorded nothing would re-run on the next start,
    hit "table already exists", and the process would never come up."""
    import sqlite3

    import bridge.storage.database as database_module
    from bridge.storage.migrations import MIGRATIONS

    broken = (
        *(step for step in MIGRATIONS if step[0] < 18),
        (18, ("CREATE TABLE half_applied (x INTEGER)", "THIS IS NOT SQL")),
    )
    original = database_module.MIGRATIONS
    database_module.MIGRATIONS = broken  # type: ignore[misc]
    try:
        with pytest.raises(sqlite3.OperationalError):
            await Database.connect(tmp_path / "bridge.db")
    finally:
        database_module.MIGRATIONS = original  # type: ignore[misc]

    with sqlite3.connect(tmp_path / "bridge.db") as raw:
        recorded = raw.execute("SELECT max(version) FROM schema_version").fetchone()[0]
        left = raw.execute(
            "SELECT name FROM sqlite_master WHERE name = 'half_applied'"
        ).fetchall()
    assert recorded == 17
    assert left == []

    database = await Database.connect(tmp_path / "bridge.db")
    try:
        assert await database.schema_version() == 20  # applies cleanly now
    finally:
        await database.close()


# ------------------------------------------- the dispatch works from the row


async def test_the_update_is_written_down_before_any_effect(
    database: Database,
) -> None:
    """The insert comes first. Everything after it reads the row, not the
    Telethon message — which is the whole point, because Telethon will not hand
    the update back a second time."""
    from bridge.routing.owner_updates import OwnerUpdateDispatch

    seen: list[str] = []

    class Recording:
        async def get(self, **_: object) -> OwnerMessageState:
            seen.append("state")
            return _baseline()

        async def seed(self, **_: object) -> bool:
            return True

        async def advance(self, **_: object) -> bool:
            seen.append("advance")
            return True

    class Watched(OwnerUpdateInboxRepository):
        async def remember(self, key: InboxKey, **kwargs: object) -> bool:  # type: ignore[override]
            seen.append("inbox")
            return await super().remember(key, **kwargs)  # type: ignore[arg-type]

    class NoMappings:
        async def by_owner_account_message(self, *_: object) -> None:
            return None

    class NoEdits:
        async def on_owner_edit(self, **_: object) -> None:
            seen.append("edit")

    dispatch = OwnerUpdateDispatch(
        state=Recording(),  # type: ignore[arg-type]
        messages=NoMappings(),  # type: ignore[arg-type]
        edits=NoEdits(),  # type: ignore[arg-type]
        reactions=None,  # type: ignore[arg-type]
        inbox=Watched(database),
    )

    class Message:
        id = MESSAGE
        message = "привет"
        entities = None
        reactions = None

    await dispatch.on_owner_update(
        account_id=ACCOUNT, bot_id=BOT, message=Message(), pts=10,
        text="привет", outgoing=True,
    )

    assert seen[0] == "inbox", f"something ran before the update was written down: {seen}"
    assert "edit" in seen  # a body that moved off its baseline is a real edit


async def test_a_finished_update_leaves_nothing_behind(database: Database) -> None:
    from bridge.routing.owner_updates import OwnerUpdateDispatch

    class State:
        async def get(self, **_: object) -> None:
            return None

        async def seed(self, **_: object) -> bool:
            return True

        async def advance(self, **_: object) -> bool:
            return True

    class NoMappings:
        async def by_owner_account_message(self, *_: object) -> None:
            return None

    class NoEdits:
        async def on_owner_edit(self, **_: object) -> None:
            return None

    inbox = OwnerUpdateInboxRepository(database)
    dispatch = OwnerUpdateDispatch(
        state=State(),  # type: ignore[arg-type]
        messages=NoMappings(),  # type: ignore[arg-type]
        edits=NoEdits(),  # type: ignore[arg-type]
        reactions=None,  # type: ignore[arg-type]
        inbox=inbox,
    )

    class Message:
        id = MESSAGE
        message = "привет"
        entities = None
        reactions = None

    await dispatch.on_owner_update(
        account_id=ACCOUNT, bot_id=BOT, message=Message(), pts=10,
        text="привет", outgoing=True,
    )
    assert await inbox.counts() == {}


async def test_a_failed_effect_leaves_the_row_for_the_next_drain(
    database: Database,
) -> None:
    """Accounting before the effects were durable would be an event that quietly
    never happened."""
    from bridge.routing.owner_updates import OwnerUpdateDispatch

    class State:
        async def get(self, **_: object) -> OwnerMessageState:
            return _baseline()

        async def seed(self, **_: object) -> bool:
            return True

        async def advance(self, **_: object) -> bool:
            return True

    class NoMappings:
        async def by_owner_account_message(self, *_: object) -> None:
            return None

    class FailingEdits:
        def __init__(self) -> None:
            self.fail = True

        async def on_owner_edit(self, **_: object) -> None:
            if self.fail:
                raise RuntimeError("MAX away")

    inbox = OwnerUpdateInboxRepository(database)
    edits = FailingEdits()
    dispatch = OwnerUpdateDispatch(
        state=State(),  # type: ignore[arg-type]
        messages=NoMappings(),  # type: ignore[arg-type]
        edits=edits,  # type: ignore[arg-type]
        reactions=None,  # type: ignore[arg-type]
        inbox=inbox,
    )

    class Message:
        id = MESSAGE
        message = "привет"
        entities = None
        reactions = None

    with pytest.raises(RuntimeError):
        await dispatch.on_owner_update(
            account_id=ACCOUNT, bot_id=BOT, message=Message(), pts=10,
            text="привет", outgoing=True,
        )

    assert await inbox.counts() == {"open": 1}

    # The next drain finishes it, from the row rather than from Telegram.
    edits.fail = False
    await database.execute("UPDATE owner_update_inbox SET next_attempt_at = 0")
    assert await dispatch.drain() == 1
    assert await inbox.counts() == {}


async def test_the_drain_uses_the_stored_text_not_the_current_message(
    database: Database,
) -> None:
    """A second edit will have overwritten whatever the message says now. The
    row is what the update carried."""
    from bridge.routing.owner_updates import OwnerUpdateDispatch

    carried: list[str] = []

    class State:
        async def get(self, **_: object) -> OwnerMessageState:
            return _baseline()

        async def seed(self, **_: object) -> bool:
            return True

        async def advance(self, **_: object) -> bool:
            return True

    class NoMappings:
        async def by_owner_account_message(self, *_: object) -> None:
            return None

    class Edits:
        async def on_owner_edit(self, **kwargs: object) -> None:
            carried.append(str(kwargs["text"]))

    inbox = OwnerUpdateInboxRepository(database)
    await inbox.remember(
        snapshot_key(10),
        content_text="то, что было тогда",
        content_fingerprint="f10",
        chosen_json="[]",
    )
    dispatch = OwnerUpdateDispatch(
        state=State(),  # type: ignore[arg-type]
        messages=NoMappings(),  # type: ignore[arg-type]
        edits=Edits(),  # type: ignore[arg-type]
        reactions=None,  # type: ignore[arg-type]
        inbox=inbox,
    )

    assert await dispatch.drain() == 1
    assert carried == ["то, что было тогда"]


# ------------------------------------------------------------------ deletes


async def _delete_dispatch(database: Database, *, mapped: bool = True) -> tuple:
    from bridge.routing.owner_updates import OwnerUpdateDispatch

    carried: list[list[int]] = []

    class Mappings:
        async def by_owner_account_message(self, account_id: int, message_id: int) -> None:
            return None

    class Router:
        def __init__(self) -> None:
            self.fail = False

        async def owner_bot_for(self, account_id: int, message_id: int) -> int | None:
            return BOT if mapped else None

        async def on_owner_edit(self, **_: object) -> None:
            return None

        async def on_owner_delete(self, **kwargs: object) -> None:
            if self.fail:
                raise RuntimeError("MAX away")
            carried.append(list(kwargs["owner_message_ids"]))  # type: ignore[arg-type]

    router = Router()
    inbox = OwnerUpdateInboxRepository(database)
    dispatch = OwnerUpdateDispatch(
        state=None,  # type: ignore[arg-type]
        messages=Mappings(),  # type: ignore[arg-type]
        edits=router,  # type: ignore[arg-type]
        reactions=None,  # type: ignore[arg-type]
        inbox=inbox,
    )
    return dispatch, inbox, router, carried


async def test_a_delete_is_written_down_before_it_is_carried(
    database: Database,
) -> None:
    dispatch, inbox, router, carried = await _delete_dispatch(database)
    router.fail = True

    with pytest.raises(RuntimeError):
        await dispatch.on_owner_delete(account_id=ACCOUNT, message_ids=[MESSAGE], pts=77)

    assert carried == []
    assert await inbox.counts() == {"open": 1}  # written down, not carried


async def test_a_delete_is_accounted_once_it_is_carried(database: Database) -> None:
    dispatch, inbox, _router, carried = await _delete_dispatch(database)

    await dispatch.on_owner_delete(account_id=ACCOUNT, message_ids=[MESSAGE], pts=77)

    assert carried == [[MESSAGE]]
    assert await inbox.counts() == {}


async def test_a_batch_becomes_one_row_per_target(database: Database) -> None:
    """One `pts` for the whole update, and the message id in the key."""
    dispatch, inbox, _router, carried = await _delete_dispatch(database)

    await dispatch.on_owner_delete(account_id=ACCOUNT, message_ids=[1, 2, 3], pts=77)

    assert sorted(one for batch in carried for one in batch) == [1, 2, 3]
    assert await inbox.counts() == {}


async def test_the_same_delete_update_twice_carries_once_more_at_most(
    database: Database,
) -> None:
    """The row is gone once accounted, so a replay writes it again and carries
    it again — which is safe, because deleting in MAX is idempotent. What must
    not happen is two rows for one target at one version."""
    dispatch, inbox, _router, _carried = await _delete_dispatch(database)
    await dispatch.on_owner_delete(account_id=ACCOUNT, message_ids=[MESSAGE], pts=77)
    await dispatch.on_owner_delete(account_id=ACCOUNT, message_ids=[MESSAGE], pts=77)
    assert await inbox.counts() == {}


async def test_a_delete_the_bridge_does_not_know_is_not_written_down(
    database: Database,
) -> None:
    """An owner-side id with no mapping is a deletion in some other chat. It
    cannot be a mapping not written yet: the row is written before the send, and
    a message never sent cannot be deleted."""
    dispatch, inbox, _router, carried = await _delete_dispatch(database, mapped=False)

    await dispatch.on_owner_delete(account_id=ACCOUNT, message_ids=[MESSAGE], pts=77)

    assert carried == []
    assert await inbox.counts() == {}


async def test_a_delete_row_holds_no_content(database: Database) -> None:
    dispatch, _inbox, router, _carried = await _delete_dispatch(database)
    router.fail = True
    with pytest.raises(RuntimeError):
        await dispatch.on_owner_delete(account_id=ACCOUNT, message_ids=[MESSAGE], pts=77)

    row = await database.query_one(
        "SELECT content_text, chosen_json, content_fingerprint FROM owner_update_inbox"
    )
    assert row is not None
    assert row["content_text"] is None and row["chosen_json"] is None


async def test_a_failed_delete_is_finished_by_the_next_drain(
    database: Database,
) -> None:
    dispatch, inbox, router, carried = await _delete_dispatch(database)
    router.fail = True
    with pytest.raises(RuntimeError):
        await dispatch.on_owner_delete(account_id=ACCOUNT, message_ids=[MESSAGE], pts=77)

    router.fail = False
    await database.execute("UPDATE owner_update_inbox SET next_attempt_at = 0")
    assert await dispatch.drain() == 1
    assert carried == [[MESSAGE]]
    assert await inbox.counts() == {}


# ---------------------------------------------------------------- scheduling


async def test_the_next_due_time_is_reported(inbox: OwnerUpdateInboxRepository) -> None:
    assert await inbox.next_due_ms() is None
    await remember_snapshot(inbox, snapshot_key())
    first = await inbox.next_due_ms()
    assert first is not None


async def test_a_backoff_moves_the_next_due_time_out(
    inbox: OwnerUpdateInboxRepository,
) -> None:
    key = snapshot_key()
    await remember_snapshot(inbox, key)
    before = await inbox.next_due_ms()
    await inbox.reopen(key, delay_ms=60_000, error="MAX away")
    after = await inbox.next_due_ms()
    assert before is not None and after is not None and after > before


async def test_an_earlier_row_pulls_the_wake_forward(
    inbox: OwnerUpdateInboxRepository,
) -> None:
    late = snapshot_key(10)
    await remember_snapshot(inbox, late)
    await inbox.reopen(late, delay_ms=60_000, error="MAX away")
    far = await inbox.next_due_ms()

    await remember_snapshot(inbox, snapshot_key(11))
    near = await inbox.next_due_ms()
    assert far is not None and near is not None and near < far


async def test_finished_and_dead_rows_do_not_wake_anything(
    inbox: OwnerUpdateInboxRepository,
) -> None:
    """Waking for a row that is finished, or one waiting on the owner, is a loop
    with nothing to do at the end of it."""
    await remember_snapshot(inbox, snapshot_key(10))
    await inbox.bury(snapshot_key(10), error="conflict")
    await remember_snapshot(inbox, snapshot_key(11))
    await inbox.account(snapshot_key(11))

    assert await inbox.next_due_ms() is None


async def test_an_album_part_resolves_through_the_alias_table(
    database: Database,
) -> None:
    """The first album smoke failed here. One part of an album has no
    `message_map` row of its own — its owner-side id lives in the alias table —
    so a lookup that only read the mapping dropped the deletion in silence."""
    from bridge.routing.delivery import DeliveryPipe
    from bridge.routing.router import BridgeRouter, BridgeTarget
    from bridge.storage import (
        BridgeStateRepository,
        MediaGroupRepository,
        MessageMapRepository,
        OutboxRepository,
    )

    messages = MessageMapRepository(database)
    albums = MediaGroupRepository(database)

    class Lookup:
        def bridge_for_bot(self, bot_id: int) -> BridgeTarget | None:
            return BridgeTarget(name="mom", max_chat_id=555, bot_id=BOT)

        def bridge_for_max_chat(self, chat_id: int) -> BridgeTarget | None:
            return None

    router = BridgeRouter(
        lookup=Lookup(),  # type: ignore[arg-type]
        telegram=None,  # type: ignore[arg-type]
        max_sender=None,  # type: ignore[arg-type]
        messages=messages,
        state=BridgeStateRepository(database),
        owner_chat_id=ACCOUNT,
        pipe=DeliveryPipe(outbox=OutboxRepository(database), send=None),  # type: ignore[arg-type]
        albums=albums,
    )

    link_id = await messages.record_from_telegram(
        bridge_name="mom", max_chat_id=555, telegram_bot_id=BOT,
        telegram_chat_id=ACCOUNT, telegram_message_id=None,
        telegram_owner_message_id=9001, telegram_owner_account_id=ACCOUNT,
    )
    await albums.add_part(
        media_group_id="own:1:2:3", bridge_name="mom", bot_id=BOT,
        telegram_message_id=None, payload={},
        telegram_owner_account_id=ACCOUNT, telegram_owner_message_id=9002,
    )
    await albums.bind_link("own:1:2:3", link_id)

    # The head has a mapping row; the second part exists only as an alias.
    assert await router.owner_bot_for(ACCOUNT, 9001) == BOT
    assert await router.owner_bot_for(ACCOUNT, 9002) == BOT
    assert await router.owner_bot_for(ACCOUNT, 9999) is None
