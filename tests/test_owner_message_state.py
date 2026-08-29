"""The durable memory of what the puppet session last saw of an owner message.

The table exists because `UpdateEditMessage` says what a message *is*, never
what changed, and the same constructor arrives for an edit, a reaction, or both.
Everything derived from an update is a subtraction against this row, so the
row's rules are the correctness of the whole path: canonical encoding so two
readings of one state compare equal, a compare-and-set on `pts` so a stale
catch-up cannot walk the state backwards, and a seed that never overwrites what
a live update already wrote.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from telethon.tl.types import (
    MessageReactions,
    ReactionCount,
    ReactionCustomEmoji,
    ReactionEmoji,
    ReactionPaid,
)

from bridge.storage import (
    Database,
    MessageMapRepository,
    OwnerMessageStateRepository,
)
from bridge.storage.migrations import LATEST_VERSION
from bridge.telegram.owner_bootstrap import bootstrap_owner_state
from bridge.telegram.owner_snapshot import (
    EMPTY_CHOSEN,
    Chosen,
    chosen_of,
    content_fingerprint_of,
    decode_chosen,
    encode_chosen,
    representative,
)

ACCOUNT = 100000001
BOT = 9000000001
MESSAGE = 1002319


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


class FakeMessage:
    def __init__(self, text: str = "", reactions: MessageReactions | None = None) -> None:
        self.id = MESSAGE
        self.message = text
        self.entities: list[object] | None = None
        self.reactions = reactions


def counted(*items: tuple[object, int | None]) -> MessageReactions:
    return MessageReactions(
        results=[
            ReactionCount(reaction=value, count=1, chosen_order=order) for value, order in items
        ],
        min=False,
        can_see_list=False,
        reactions_as_tags=False,
        recent_reactions=[],
    )


# ------------------------------------------------------------ canonical form


def test_the_same_state_is_always_the_same_bytes() -> None:
    """Comparison is done on the stored string, so two readings of one state
    that differ by a space would read as a change that never happened."""
    once = encode_chosen((Chosen("e", "👍"), Chosen("c", "5000000000000000001")))
    again = encode_chosen((Chosen("e", "👍"), Chosen("c", "5000000000000000001")))
    assert once == again == '[["e","👍"],["c","5000000000000000001"]]'


def test_an_ordinary_emoji_and_a_custom_one_never_compare_equal() -> None:
    """A document id names a sticker in somebody's pack. Untagged, a custom
    reaction whose id happened to read like an emoji would be carried as one."""
    assert Chosen("e", "5000000000000000001") != Chosen("c", "5000000000000000001")
    assert encode_chosen((Chosen("e", "1"),)) != encode_chosen((Chosen("c", "1"),))


def test_the_order_survives_a_round_trip() -> None:
    """It is meaning, not formatting: the last one chosen is what MAX shows."""
    chosen = (Chosen("e", "👍"), Chosen("e", "❤"))
    assert decode_chosen(encode_chosen(chosen)) == chosen
    assert representative(chosen) == Chosen("e", "❤")


def test_an_empty_set_has_one_spelling() -> None:
    assert encode_chosen(()) == EMPTY_CHOSEN
    assert decode_chosen(EMPTY_CHOSEN) == ()
    assert representative(()) is None


def test_a_row_that_cannot_be_parsed_reads_as_unknown() -> None:
    """Rather than raising in a handler that would then never advance."""
    assert decode_chosen("not json") == ()
    assert decode_chosen('{"a": 1}') == ()


# ---------------------------------------------------------- reading a message


def test_only_reactions_the_owner_chose_are_read() -> None:
    """`chosen_order` is the owner's own marker. An aggregate entry with none is
    somebody else's and must not enter the owner's set."""
    message = FakeMessage(
        reactions=counted(
            (ReactionEmoji(emoticon="👍"), 0),
            (ReactionEmoji(emoticon="🔥"), None),
        )
    )
    assert chosen_of(message) == (Chosen("e", "👍"),)


def test_the_set_is_ordered_by_chosen_order_not_by_arrival() -> None:
    """represented by this fixture at pts 2002411: ❤ came back first in `results`, with
    `chosen_order=1`, and 👍 second with 0. Reading the list order would have
    made ❤ the representative one action too early."""
    message = FakeMessage(
        reactions=counted(
            (ReactionEmoji(emoticon="❤"), 1),
            (ReactionEmoji(emoticon="👍"), 0),
        )
    )
    assert chosen_of(message) == (Chosen("e", "👍"), Chosen("e", "❤"))
    assert representative(chosen_of(message)) == Chosen("e", "❤")


def test_a_custom_emoji_is_kept_as_its_document_id() -> None:
    message = FakeMessage(
        reactions=counted((ReactionCustomEmoji(document_id=5000000000000000001), 0))
    )
    assert chosen_of(message) == (Chosen("c", "5000000000000000001"),)
    assert chosen_of(message)[0].emoji is None


def test_a_reaction_kind_we_have_not_met_is_left_out() -> None:
    """A paid reaction is not something this bridge carries. Left out of the set
    rather than encoded as something it is not."""
    message = FakeMessage(reactions=counted((ReactionPaid(), 0), (ReactionEmoji("👍"), 1)))
    assert chosen_of(message) == (Chosen("e", "👍"),)


def test_a_message_with_no_reactions_reads_as_an_empty_set() -> None:
    assert chosen_of(FakeMessage()) == ()


def test_the_fingerprint_matches_the_one_the_edit_key_uses() -> None:
    """Two hashes of the same words in two modules is one hash by accident. A
    test rather than a comment, because they are written apart on purpose."""
    from bridge.routing.owner_mutation import fingerprint

    assert content_fingerprint_of(FakeMessage("привет")) == fingerprint("привет")


def test_the_fingerprint_ignores_reactions() -> None:
    """Otherwise every reaction would read as a content edit — which is the
    production defect this whole table exists to end."""
    plain = content_fingerprint_of(FakeMessage("привет"))
    reacted = content_fingerprint_of(
        FakeMessage("привет", reactions=counted((ReactionEmoji("👍"), 0)))
    )
    assert plain == reacted


# ------------------------------------------------------------- the compare-and-set


async def test_a_newer_update_moves_the_row(database: Database) -> None:
    state = OwnerMessageStateRepository(database)
    assert await state.advance(
        account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE,
        content_fingerprint="aaa", chosen_json=EMPTY_CHOSEN, pts=10,
    )
    assert await state.advance(
        account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE,
        content_fingerprint="bbb", chosen_json='[["e","👍"]]', pts=11,
    )
    row = await state.get(account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE)
    assert row is not None
    assert (row.pts, row.content_fingerprint, row.chosen_json) == (11, "bbb", '[["e","👍"]]')


async def test_a_stale_update_cannot_walk_the_state_backwards(database: Database) -> None:
    """A catch-up replaying an old version after a newer one landed."""
    state = OwnerMessageStateRepository(database)
    await state.advance(
        account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE,
        content_fingerprint="new", chosen_json='[["e","❤"]]', pts=20,
    )
    assert not await state.advance(
        account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE,
        content_fingerprint="old", chosen_json=EMPTY_CHOSEN, pts=19,
    )
    row = await state.get(account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE)
    assert row is not None and row.pts == 20 and row.content_fingerprint == "new"


async def test_the_same_pts_twice_changes_nothing(database: Database) -> None:
    """Replay of one update. Strictly greater, so equal is refused."""
    state = OwnerMessageStateRepository(database)
    await state.advance(
        account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE,
        content_fingerprint="a", chosen_json=EMPTY_CHOSEN, pts=5,
    )
    assert not await state.advance(
        account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE,
        content_fingerprint="a", chosen_json=EMPTY_CHOSEN, pts=5,
    )


async def test_two_handlers_racing_leave_the_highest_pts(database: Database) -> None:
    """One statement decides it, not a value either of them read earlier."""
    state = OwnerMessageStateRepository(database)
    await asyncio.gather(
        *(
            state.advance(
                account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE,
                content_fingerprint=f"f{pts}", chosen_json=EMPTY_CHOSEN, pts=pts,
            )
            for pts in (31, 30, 33, 32)
        )
    )
    row = await state.get(account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE)
    assert row is not None and row.pts == 33 and row.content_fingerprint == "f33"


async def test_the_key_is_the_account_the_peer_and_the_message(database: Database) -> None:
    """The same owner-side number in another dialog, or from another account, is
    a different message — and the key is exactly what the update carries."""
    state = OwnerMessageStateRepository(database)
    for account, bot in ((ACCOUNT, BOT), (ACCOUNT, BOT + 1), (ACCOUNT + 1, BOT)):
        assert await state.advance(
            account_id=account, bot_id=bot, message_id=MESSAGE,
            content_fingerprint="x", chosen_json=EMPTY_CHOSEN, pts=1,
        )
    assert await state.count() == 3


# ------------------------------------------------------------------- the seed


async def test_a_seed_writes_a_baseline_at_version_zero(database: Database) -> None:
    """A picture of the present, not a position in the stream — so any real
    update outranks it."""
    state = OwnerMessageStateRepository(database)
    assert await state.seed(
        account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE,
        content_fingerprint="a", chosen_json='[["e","👍"]]',
    )
    row = await state.get(account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE)
    assert row is not None and row.pts == 0

    assert await state.advance(
        account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE,
        content_fingerprint="a", chosen_json=EMPTY_CHOSEN, pts=1,
    )


async def test_a_seed_never_overwrites_a_live_update(database: Database) -> None:
    """The bootstrap runs beside a live dispatcher. A row already written from
    an update is newer than any fetch, so the fetch steps aside."""
    state = OwnerMessageStateRepository(database)
    await state.advance(
        account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE,
        content_fingerprint="live", chosen_json='[["e","❤"]]', pts=99,
    )
    assert not await state.seed(
        account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE,
        content_fingerprint="fetched", chosen_json=EMPTY_CHOSEN,
    )
    row = await state.get(account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE)
    assert row is not None and row.pts == 99 and row.content_fingerprint == "live"


# -------------------------------------------------------------- the bootstrap


class FakeClient:
    """A session that hands back messages by id, and can lose some."""

    def __init__(self, available: dict[int, FakeMessage]) -> None:
        self._available = available
        self.asked: list[tuple[int, tuple[int, ...]]] = []

    async def get_messages(self, peer: object, *, ids: list[int]) -> list[FakeMessage | None]:
        self.asked.append((getattr(peer, "user_id", 0), tuple(ids)))
        return [self._available.get(one) for one in ids]


class FakeMappings:
    def __init__(self, keys: list[tuple[int, int, int]]) -> None:
        self._keys = keys

    async def owner_bound_keys(self) -> list[tuple[int, int, int]]:
        return self._keys

    async def by_owner_account_message(self, account_id: int, message_id: int) -> None:
        return None


class Seeder:
    """The one writer the bootstrap is allowed: the dispatch, holding the lock.

    Named rather than passed as the repository, because writing a baseline
    outside the per-message lock is exactly what the serialization exists to
    stop.
    """

    def __init__(self, state: OwnerMessageStateRepository) -> None:
        self._state = state

    async def seed_baseline(self, **kwargs: object) -> bool:
        return await self._state.seed(**kwargs)  # type: ignore[arg-type]


def _message(message_id: int, text: str, order: int | None = None) -> FakeMessage:
    reactions = counted((ReactionEmoji("👍"), order)) if order is not None else None
    message = FakeMessage(text, reactions)
    message.id = message_id
    return message


async def test_an_existing_message_with_no_reactions_is_baselined(
    database: Database,
) -> None:
    state = OwnerMessageStateRepository(database)
    report = await bootstrap_owner_state(
        client=FakeClient({1: _message(1, "привет")}),
        mappings=FakeMappings([(ACCOUNT, BOT, 1)]),
        state=Seeder(state),
        account_id=ACCOUNT,
    )
    assert (report.seeded, report.unavailable) == (1, 0)
    row = await state.get(account_id=ACCOUNT, bot_id=BOT, message_id=1)
    assert row is not None and row.chosen_json == EMPTY_CHOSEN


async def test_an_existing_reacted_message_keeps_its_reaction(database: Database) -> None:
    """The case that makes the bootstrap necessary at all: without it, the first
    update after this release — the *removal* — would subtract against an
    invented empty set and do nothing."""
    state = OwnerMessageStateRepository(database)
    await bootstrap_owner_state(
        client=FakeClient({2: _message(2, "привет", order=0)}),
        mappings=FakeMappings([(ACCOUNT, BOT, 2)]),
        state=Seeder(state),
        account_id=ACCOUNT,
    )
    row = await state.get(account_id=ACCOUNT, bot_id=BOT, message_id=2)
    assert row is not None and row.chosen_json == '[["e","👍"]]'


async def test_a_message_that_cannot_be_fetched_gets_no_row(database: Database) -> None:
    """Unknown is a state, and it is not "there were no reactions"."""
    state = OwnerMessageStateRepository(database)
    report = await bootstrap_owner_state(
        client=FakeClient({}),
        mappings=FakeMappings([(ACCOUNT, BOT, 3)]),
        state=Seeder(state),
        account_id=ACCOUNT,
    )
    assert (report.seeded, report.unavailable) == (0, 1)
    assert await state.get(account_id=ACCOUNT, bot_id=BOT, message_id=3) is None


async def test_a_dialog_that_cannot_be_read_does_not_stop_the_rest(
    database: Database,
) -> None:
    class Failing(FakeClient):
        async def get_messages(self, peer: object, *, ids: list[int]) -> list[FakeMessage | None]:
            if getattr(peer, "user_id", 0) == BOT:
                raise RuntimeError("no access")
            return await super().get_messages(peer, ids=ids)

    state = OwnerMessageStateRepository(database)
    report = await bootstrap_owner_state(
        client=Failing({4: _message(4, "ok")}),
        mappings=FakeMappings([(ACCOUNT, BOT, 9), (ACCOUNT, BOT + 1, 4)]),
        state=Seeder(state),
        account_id=ACCOUNT,
    )
    assert (report.seeded, report.unavailable) == (1, 1)


async def test_another_account_is_not_touched(database: Database) -> None:
    state = OwnerMessageStateRepository(database)
    report = await bootstrap_owner_state(
        client=FakeClient({5: _message(5, "чужое")}),
        mappings=FakeMappings([(ACCOUNT + 1, BOT, 5)]),
        state=Seeder(state),
        account_id=ACCOUNT,
    )
    assert report == type(report)()
    assert await state.count() == 0


async def test_the_bootstrap_creates_no_jobs(database: Database) -> None:
    """Read Telegram, write one row, touch nothing else. A baseline that sent a
    reaction into MAX would replay the owner's whole history at their contact."""
    state = OwnerMessageStateRepository(database)
    await bootstrap_owner_state(
        client=FakeClient({6: _message(6, "привет", order=0)}),
        mappings=FakeMappings([(ACCOUNT, BOT, 6)]),
        state=Seeder(state),
        account_id=ACCOUNT,
    )
    assert await database.query("SELECT id FROM outbox") == []
    assert await database.query("SELECT id FROM message_map") == []


# ------------------------------------------------------- the work list itself


async def test_the_work_list_needs_all_three_parts_of_the_key(
    database: Database,
) -> None:
    """An owner-side id without its account is not an identity; without the bot
    there is no dialog to fetch it from."""
    messages = MessageMapRepository(database)
    complete = await messages.claim_from_max(
        bridge_name="b", max_chat_id=1, max_message_id=10,
        telegram_bot_id=BOT, telegram_chat_id=ACCOUNT,
    )
    assert complete is not None
    await messages.attach_owner_message(complete, 111, telegram_owner_account_id=ACCOUNT)

    partial = await messages.claim_from_max(
        bridge_name="b", max_chat_id=1, max_message_id=11,
        telegram_bot_id=BOT, telegram_chat_id=ACCOUNT,
    )
    assert partial is not None
    await messages.attach_owner_message(partial, 112)  # no account

    assert await messages.owner_bound_keys() == [(ACCOUNT, BOT, 111)]


# --------------------------------------------------------------- the migration


async def test_v17_lands_on_a_v16_database_without_touching_it(tmp_path: Path) -> None:
    """An upgrade in place: the table appears, and everything already there is
    exactly as it was. `WITHOUT ROWID` because the primary key *is* the row —
    there is nothing else to index it by."""
    import sqlite3

    from bridge.storage.migrations import MIGRATIONS

    path = tmp_path / "bridge.db"
    with sqlite3.connect(path) as raw:
        raw.execute(
            "CREATE TABLE schema_version (version INTEGER NOT NULL,"
            " applied_at INTEGER NOT NULL)"
        )
        for version, statements in MIGRATIONS:
            if version > 16:
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

    database = await Database.connect(path)
    try:
        assert await database.schema_version() == LATEST_VERSION
        assert len(await database.query("SELECT id FROM message_map")) == 1
        state = OwnerMessageStateRepository(database)
        assert await state.count() == 0
    finally:
        await database.close()


async def test_a_failing_step_leaves_the_version_where_it_was(tmp_path: Path) -> None:
    """The reason the applier spells out its own transaction: a half-applied
    step that recorded nothing would re-run on the next start, hit "table
    already exists", and the process would never come up again."""
    import bridge.storage.database as database_module
    from bridge.storage.migrations import MIGRATIONS

    upto16 = tuple(step for step in MIGRATIONS if step[0] <= 16)
    broken = (*upto16, (17, ("CREATE TABLE ok_so_far (x INTEGER)", "NOT SQL AT ALL")))
    import sqlite3 as _sqlite3

    import pytest

    original = database_module.MIGRATIONS
    database_module.MIGRATIONS = broken  # type: ignore[misc]
    try:
        with pytest.raises(_sqlite3.OperationalError):
            await Database.connect(tmp_path / "bridge.db")
    finally:
        database_module.MIGRATIONS = original  # type: ignore[misc]

    database = await Database.connect(tmp_path / "bridge.db")
    try:
        assert await database.schema_version() == LATEST_VERSION  # applied cleanly now
        assert await database.query(
            "SELECT name FROM sqlite_master WHERE name = 'ok_so_far'"
        ) == []
    finally:
        await database.close()


# ------------------------------------------------- the baseline is what MAX shows


def _chosen(emoji: list[str]) -> MessageReactions | None:
    if not emoji:
        return counted()
    return counted(*((ReactionEmoji(emoticon=one), i) for i, one in enumerate(emoji)))


class Showing:
    """`reaction_state`, as the bootstrap reads it: what MAX was last told."""

    def __init__(self, your_reaction: str | None) -> None:
        self._your = your_reaction

    async def get(self, max_chat_id: int, max_message_id: int) -> object:
        from bridge.storage import ReactionSnapshot

        return ReactionSnapshot(
            max_chat_id=max_chat_id, max_message_id=max_message_id,
            counters={}, your_reaction=self._your,
        )


class MappedTo:
    def __init__(self, keys: list[tuple[int, int, int]]) -> None:
        self._keys = keys

    async def owner_bound_keys(self) -> list[tuple[int, int, int]]:
        return self._keys

    async def by_owner_account_message(self, account_id: int, message_id: int) -> object:
        from bridge.storage import Direction, MessageLink, SourceMarker

        return MessageLink(
            id=1, bridge_name="mom", max_chat_id=555, max_message_id=9001,
            telegram_bot_id=BOT, telegram_chat_id=ACCOUNT, telegram_message_id=None,
            direction=Direction.MAX_TO_TG, source_marker=SourceMarker.FROM_MAX,
            created_at=0, telegram_owner_message_id=message_id,
            telegram_owner_account_id=account_id,
        )


async def _baseline_with(database: Database, shown: object) -> str:
    state = OwnerMessageStateRepository(database)
    await bootstrap_owner_state(
        client=FakeClient({7: _message(7, "привет", order=0)}),
        mappings=MappedTo([(ACCOUNT, BOT, 7)]),
        state=Seeder(state),
        shown=shown,  # type: ignore[arg-type]
        account_id=ACCOUNT,
    )
    row = await state.get(account_id=ACCOUNT, bot_id=BOT, message_id=7)
    assert row is not None
    return row.chosen_json


@pytest.mark.parametrize(
    ("applied", "fetched", "expected"),
    [
        # MAX and Telegram agree: the whole fetched set is the baseline, so a
        # later removal of a non-representative reads as no change.
        (None, [], EMPTY_CHOSEN),
        ("👍", ["👍"], '[["e","👍"]]'),
        ("❤️", ["👍", "❤️"], '[["e","👍"],["e","❤️"]]'),
        # They differ: nothing fetched has been applied, so the baseline
        # describes only what MAX is showing and the update carries the rest.
        (None, ["👍"], EMPTY_CHOSEN),
        ("👍", ["❤️"], '[["e","👍"]]'),
        ("👍", ["👍", "❤️"], '[["e","👍"]]'),
    ],
)
async def test_the_baseline_is_the_applied_projection(
    database: Database, applied: str | None, fetched: list[str], expected: str
) -> None:
    """Eight cases, one rule: the baseline must describe a state MAX is already
    in. Recording the fetched Telegram state as though it had been applied is
    what loses an owner action."""
    state = OwnerMessageStateRepository(database)
    message = FakeMessage("привет", _chosen(fetched))
    message.id = 7
    await bootstrap_owner_state(
        client=FakeClient({7: message}),
        mappings=MappedTo([(ACCOUNT, BOT, 7)]),
        state=Seeder(state),
        shown=Showing(applied),  # type: ignore[arg-type]
        account_id=ACCOUNT,
    )
    row = await state.get(account_id=ACCOUNT, bot_id=BOT, message_id=7)
    assert row is not None and row.chosen_json == expected


async def test_a_custom_representative_never_counts_as_applied(
    database: Database,
) -> None:
    """It has no MAX projection at all, so it cannot be the thing MAX shows."""
    state = OwnerMessageStateRepository(database)
    message = FakeMessage("привет", counted((ReactionCustomEmoji(document_id=5), 0)))
    message.id = 8
    await bootstrap_owner_state(
        client=FakeClient({8: message}),
        mappings=MappedTo([(ACCOUNT, BOT, 8)]),
        state=Seeder(state),
        shown=Showing("👍"),  # type: ignore[arg-type]
        account_id=ACCOUNT,
    )
    row = await state.get(account_id=ACCOUNT, bot_id=BOT, message_id=8)
    assert row is not None and row.chosen_json == '[["e","👍"]]'


async def test_a_reaction_max_has_not_been_told_about_is_not_baselined_away(
    database: Database,
) -> None:
    """The loss this rule exists for. The owner reacts, the fetch already sees
    the reaction, the baseline records it, and the update that follows subtracts
    to nothing — so MAX is never told at all.

    The baseline is therefore what MAX is showing, not what Telegram says: an
    empty set here, so the update reads as a change and carries it through.
    """
    assert await _baseline_with(database, Showing(None)) == EMPTY_CHOSEN


async def test_a_reaction_max_already_shows_is_kept(database: Database) -> None:
    """The two sides are in step, so the fetched set is the truth. Recording an
    empty one would make the *next* update look like an addition and put a
    reaction back that is already there."""
    assert await _baseline_with(database, Showing("👍")) == '[["e","👍"]]'


async def test_without_a_reader_the_fetched_set_is_used(database: Database) -> None:
    """The setup CLI and tests that build no queue. Old behaviour, named."""
    assert await _baseline_with(database, None) == '[["e","👍"]]'
