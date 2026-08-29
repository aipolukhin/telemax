"""One update, two independent questions — and the defect that came of asking one.

Before this, every `UpdateEditMessage` on the owner's own message was carried
into MAX as an edit, because that is the only thing the handler knew how to read
it as. Reacting to your own message therefore sent four pointless edits: four
updates, four `pts`, one unchanged content fingerprint (represented by this fixture, jobs
367–370, the compatibility contract).

The reading is now a subtraction against durable state, and it yields two
answers rather than one. What is proved here is that the two are independent,
that the reaction half is decided on the *projection* rather than on the set,
and that a crash between the two effects loses neither.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest_asyncio
from telethon.tl.types import (
    MessageReactions,
    ReactionCount,
    ReactionCustomEmoji,
    ReactionEmoji,
)

from bridge.routing.owner_updates import OwnerUpdateDispatch
from bridge.storage import Database, MessageMapRepository, OwnerMessageStateRepository

ACCOUNT = 100000001
BOT = 9000000001
MESSAGE = 1002319
MAX_CHAT = 236856064
MAX_MESSAGE = 111411200851968013

#: The captured sequence, as the owner performed it.
CAPTURE = [
    (2002410, ["👍"]),
    (2002411, ["❤", "👍"]),  # ❤ chosen second; the list order is not the order
    (2002412, ["❤"]),
    (2002413, []),
]


class Message:
    def __init__(self, text: str = "привет", chosen: list[str] | None = None) -> None:
        self.id = MESSAGE
        self.message = text
        self.entities: list[object] | None = None
        self.reactions = _reactions(chosen or [])


def _reactions(chosen: list[str]) -> MessageReactions | None:
    if not chosen:
        return MessageReactions(
            results=[], min=False, can_see_list=False, reactions_as_tags=False,
            recent_reactions=[],
        )
    # `chosen` is given in the order Telegram listed them; the *choice* order is
    # what the captured update carried, so ❤ first in the list means order 1.
    orders = {"❤": 1, "👍": 0} if len(chosen) > 1 else {chosen[0]: 0}
    return MessageReactions(
        results=[
            ReactionCount(reaction=ReactionEmoji(emoticon=one), count=1, chosen_order=orders[one])
            for one in chosen
        ],
        min=False, can_see_list=False, reactions_as_tags=False, recent_reactions=[],
    )


class FakeEdits:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def on_owner_edit(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


class FakeReactions:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    async def apply_owner_reaction(self, **kwargs: Any) -> None:
        if self.fail:
            raise RuntimeError("MAX is away")
        self.calls.append(kwargs)


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


async def _wired(
    database: Database, *, mapped: bool = True, reactions: FakeReactions | None = None
) -> tuple[OwnerUpdateDispatch, FakeEdits, FakeReactions, OwnerMessageStateRepository]:
    messages = MessageMapRepository(database)
    if mapped:
        link = await messages.claim_from_max(
            bridge_name="mom", max_chat_id=MAX_CHAT, max_message_id=MAX_MESSAGE,
            telegram_bot_id=BOT, telegram_chat_id=ACCOUNT,
        )
        assert link is not None
        await messages.attach_owner_message(link, MESSAGE, telegram_owner_account_id=ACCOUNT)
    edits, reacts = FakeEdits(), reactions or FakeReactions()
    state = OwnerMessageStateRepository(database)
    dispatch = OwnerUpdateDispatch(
        state=state, messages=messages, edits=edits, reactions=reacts
    )
    return dispatch, edits, reacts, state


async def _baseline(state: OwnerMessageStateRepository, chosen: str = "[]") -> None:
    from bridge.telegram.owner_snapshot import content_fingerprint_of

    await state.seed(
        account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE,
        content_fingerprint=content_fingerprint_of(Message()), chosen_json=chosen,
    )


async def _feed(
    dispatch: OwnerUpdateDispatch, message: Message, pts: int, *, outgoing: bool = True
) -> None:
    await dispatch.on_owner_update(
        account_id=ACCOUNT, bot_id=BOT, message=message, pts=pts,
        text=message.message if outgoing else None, outgoing=outgoing,
    )


# ------------------------------------------------- the captured four transitions


async def test_the_captured_sequence_is_four_versions_and_three_max_effects(
    database: Database,
) -> None:
    """The whole contract in one test. Four updates the owner really produced;
    three of them change what MAX can show, and the third does not."""
    dispatch, edits, reactions, state = await _wired(database)
    await _baseline(state)

    for pts, chosen in CAPTURE:
        await _feed(dispatch, Message(chosen=chosen), pts)

    assert [call["emoji"] for call in reactions.calls] == ["👍", "❤", None]
    assert edits.calls == [], "a reaction is not an edit"
    row = await state.get(account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE)
    assert row is not None
    assert row.pts == 2002413 and row.chosen_json == "[]"


async def test_taking_off_the_reaction_that_was_not_showing_touches_nothing(
    database: Database,
) -> None:
    """pts 2002412 on its own: the set went from [👍, ❤] to [❤], and MAX was
    already showing ❤. A diff on the set would have made a call here."""
    dispatch, _, reactions, state = await _wired(database)
    await _baseline(state, chosen='[["e","👍"],["e","❤"]]')

    await _feed(dispatch, Message(chosen=["❤"]), 2002412)

    assert reactions.calls == []
    row = await state.get(account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE)
    assert row is not None and row.chosen_json == '[["e","❤"]]'  # ...but state moved


# ----------------------------------------------------- the two halves are apart


async def test_a_reaction_only_update_creates_no_edit(database: Database) -> None:
    """The production defect, named. Same words, new reaction."""
    dispatch, edits, reactions, state = await _wired(database)
    await _baseline(state)

    await _feed(dispatch, Message("привет", chosen=["👍"]), 2002410)

    assert edits.calls == []
    assert len(reactions.calls) == 1


async def test_a_text_edit_with_unchanged_reactions_creates_one_edit(
    database: Database,
) -> None:
    dispatch, edits, reactions, state = await _wired(database)
    await _baseline(state, chosen='[["e","👍"]]')

    await _feed(dispatch, Message("стало", chosen=["👍"]), 2002420)

    assert len(edits.calls) == 1
    assert edits.calls[0]["text"] == "стало" and edits.calls[0]["edit_pts"] == 2002420
    assert reactions.calls == []


async def test_one_update_can_be_both(database: Database) -> None:
    """Nothing forces them to be exclusive, and neither is lost."""
    dispatch, edits, reactions, state = await _wired(database)
    await _baseline(state)

    await _feed(dispatch, Message("стало", chosen=["👍"]), 2002421)

    assert len(edits.calls) == 1 and len(reactions.calls) == 1


async def test_a_contact_message_never_produces_an_edit(database: Database) -> None:
    """A contact bot editing its own message is the bridge's own delivery being
    corrected. Its reactions are still the owner's."""
    dispatch, edits, reactions, state = await _wired(database)
    await _baseline(state)

    await _feed(dispatch, Message("что-то другое", chosen=["👍"]), 2002430, outgoing=False)

    assert edits.calls == []
    assert len(reactions.calls) == 1


# ------------------------------------------------------------------- versions


async def test_the_same_update_twice_does_nothing_the_second_time(
    database: Database,
) -> None:
    dispatch, _, reactions, state = await _wired(database)
    await _baseline(state)

    await _feed(dispatch, Message(chosen=["👍"]), 2002410)
    await _feed(dispatch, Message(chosen=["👍"]), 2002410)

    assert len(reactions.calls) == 1


async def test_an_older_update_arriving_late_does_nothing(database: Database) -> None:
    """Catch-up after a reconnect can hand back a version already settled."""
    dispatch, edits, reactions, state = await _wired(database)
    await _baseline(state)

    await _feed(dispatch, Message(chosen=["❤"]), 2002412)
    await _feed(dispatch, Message(chosen=["👍"]), 2002410)

    assert [call["emoji"] for call in reactions.calls] == ["❤"]
    assert edits.calls == []
    row = await state.get(account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE)
    assert row is not None and row.pts == 2002412


async def test_a_newer_version_of_an_unchanged_message_only_moves_the_version(
    database: Database,
) -> None:
    """Telegram may version a message for a reason the bridge does not carry.
    The row follows; nothing else happens."""
    dispatch, edits, reactions, state = await _wired(database)
    await _baseline(state, chosen='[["e","👍"]]')

    await _feed(dispatch, Message("привет", chosen=["👍"]), 2002440)

    assert edits.calls == [] and reactions.calls == []
    row = await state.get(account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE)
    assert row is not None and row.pts == 2002440


# -------------------------------------------------------------- no baseline


async def test_a_message_with_no_baseline_does_not_guess_its_reaction(
    database: Database,
) -> None:
    """The first update on an old message has nothing to subtract from. Reading
    a reaction here would be wrong in the case that matters — a reaction put on
    yesterday whose first update is its removal."""
    dispatch, _, reactions, state = await _wired(database)

    await _feed(dispatch, Message(chosen=["👍"]), 2002410)

    assert reactions.calls == []
    assert dispatch.counts.without_baseline == 1
    row = await state.get(account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE)
    assert row is not None and row.chosen_json == '[["e","👍"]]'


async def test_a_message_with_no_baseline_carries_no_edit(
    database: Database,
) -> None:
    """Unknown is not "changed", and it used to be read as one.

    The old reading carried the edit, on the grounds that "at worst it is an
    edit that changes nothing, which MAX absorbs". Production disproved both
    halves on 2026-08-07: MAX answered `error.edit.timeout` sixteen times over
    because the target was past its edit window, and what the update carried was
    not "nothing" but the bridge's own rendering of the message — stamp
    included. Nothing here has been shown to have changed, so nothing is sent.
    """
    dispatch, edits, _, _ = await _wired(database)

    await _feed(dispatch, Message("стало"), 2002450)

    assert edits.calls == []
    assert dispatch.counts.unproven_edit == 1
    assert dispatch.counts.without_baseline == 1


async def test_the_edit_after_the_first_sight_is_carried_exactly_once(
    database: Database,
) -> None:
    """Declining the first sight must not cost the owner a real edit.

    The first update writes the baseline; the next one that moves off it is an
    ordinary subtraction and carries — once, with the text it actually carried.
    """
    dispatch, edits, _, _ = await _wired(database)

    await _feed(dispatch, Message("было"), 2002450)     # first sight: baseline only
    await _feed(dispatch, Message("стало"), 2002451)    # a real change
    await _feed(dispatch, Message("стало"), 2002452)    # a reaction, say: no change

    assert [call["text"] for call in edits.calls] == ["стало"]


async def test_it_happens_at_most_once_per_message(database: Database) -> None:
    dispatch, _, reactions, _state = await _wired(database)

    await _feed(dispatch, Message(chosen=["👍"]), 2002410)
    await _feed(dispatch, Message(chosen=["❤", "👍"]), 2002411)

    assert dispatch.counts.without_baseline == 1
    assert [call["emoji"] for call in reactions.calls] == ["❤"]


# ---------------------------------------------------------- what cannot be shown


async def test_a_reaction_on_a_message_the_session_cannot_name_is_counted(
    database: Database,
) -> None:
    """Albums and stickers have no owner-side id: the echo binding leaves them
    unbound. Nothing is guessed, nothing is alerted on, and the number is there
    to be read."""
    dispatch, _, reactions, state = await _wired(database, mapped=False)
    await _baseline(state)

    await _feed(dispatch, Message(chosen=["👍"]), 2002410)

    assert reactions.calls == []
    assert dispatch.counts.unresolved_reaction == 1


async def test_a_custom_emoji_clears_max_rather_than_leaving_a_stale_one(
    database: Database,
) -> None:
    """A document id names a sticker in somebody's pack: there is no ordinary
    emoji that *is* it, so nothing can be shown for it. What MAX is told is
    "nothing" rather than the reaction the owner chose *before* — that one is no
    longer their choice, and leaving it would present it as if it were."""
    dispatch, _, reactions, state = await _wired(database)
    await _baseline(state)

    message = Message()
    message.reactions = MessageReactions(
        results=[
            ReactionCount(
                reaction=ReactionCustomEmoji(document_id=5000000000000000001),
                count=1, chosen_order=0,
            )
        ],
        min=False, can_see_list=False, reactions_as_tags=False, recent_reactions=[],
    )
    await _feed(dispatch, message, 2002460)

    assert [call["emoji"] for call in reactions.calls] == [None]
    assert reactions.calls[0]["custom_id"] == "5000000000000000001"
    assert dispatch.counts.custom_emoji == 1


async def test_going_from_a_custom_emoji_back_to_a_supported_one_is_carried(
    database: Database,
) -> None:
    dispatch, _, reactions, state = await _wired(database)
    await _baseline(state, chosen='[["c","5000000000000000001"]]')

    await _feed(dispatch, Message(chosen=["👍"]), 2002461)

    assert [call["emoji"] for call in reactions.calls] == ["👍"]


async def test_removing_a_custom_emoji_clears_the_reaction(database: Database) -> None:
    """The representative goes from something MAX cannot show to nothing at all,
    which MAX can: whatever was on the message comes off."""
    dispatch, _, reactions, state = await _wired(database)
    await _baseline(state, chosen='[["c","5000000000000000001"]]')

    await _feed(dispatch, Message(chosen=[]), 2002462)

    assert [call["emoji"] for call in reactions.calls] == [None]


# ------------------------------------------------------------ the crash contract


async def test_a_failed_reaction_leaves_the_version_where_it_was(
    database: Database,
) -> None:
    """The state moves last, on purpose. A row still on its old version is what
    makes the next reading of that message derive what this one could not."""
    dispatch, _, _, state = await _wired(database, reactions=FakeReactions(fail=True))
    await _baseline(state)

    try:
        await _feed(dispatch, Message(chosen=["👍"]), 2002410)
    except RuntimeError:
        pass

    row = await state.get(account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE)
    assert row is not None and row.pts == 0


async def test_the_missed_effect_is_re_derived_by_the_next_update(
    database: Database,
) -> None:
    """No marker to remember a half-done update by, and none needed: the state
    was never advanced, so the next reading subtracts from the same place."""
    failing = FakeReactions(fail=True)
    dispatch, _edits, _, state = await _wired(database, reactions=failing)
    await _baseline(state)
    try:
        await _feed(dispatch, Message(chosen=["👍"]), 2002410)
    except RuntimeError:
        pass

    failing.fail = False
    await _feed(dispatch, Message(chosen=["👍"]), 2002411)

    assert [call["emoji"] for call in failing.calls] == ["👍"]


async def test_the_edit_is_written_down_before_the_reaction_is_attempted(
    database: Database,
) -> None:
    """If only one of the two survives a crash it should be the durable one."""
    dispatch, edits, _, state = await _wired(database, reactions=FakeReactions(fail=True))
    await _baseline(state)

    try:
        await _feed(dispatch, Message("стало", chosen=["👍"]), 2002470)
    except RuntimeError:
        pass

    assert len(edits.calls) == 1


# ------------------------------------------------------- the projection into MAX


class FakeMax:
    def __init__(self) -> None:
        self.added: list[tuple[int, int, str]] = []
        self.removed: list[tuple[int, int]] = []

    async def add_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        self.added.append((chat_id, message_id, emoji))

    async def remove_reaction(self, chat_id: int, message_id: int) -> None:
        self.removed.append((chat_id, message_id))

    async def send_text(self, chat_id: int, text: str, *, reply_to: int | None = None) -> int:
        raise AssertionError("a note must go through the durable queue, never straight at MAX")

    async def reactions_for(self, chat_id: int, message_ids: list[int]) -> dict[int, Any]:
        return {}


class FakeNotes:
    """The durable queue, as `ReactionSync` sees it. Both kinds land here now."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reactions: list[dict[str, Any]] = []

    async def carry_emoji_note(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    async def carry_owner_reaction(self, **kwargs: Any) -> None:
        self.reactions.append(kwargs)


def _sync(database: Database, notes: FakeNotes | None = None) -> tuple[Any, FakeMax]:
    from bridge.config import ReactionsConfig
    from bridge.reactions import ReactionSync
    from bridge.storage import ReactionStateRepository

    maxi = FakeMax()
    sync = ReactionSync(
        renderer=None,  # type: ignore[arg-type]
        max_sender=maxi,
        messages=MessageMapRepository(database),
        snapshots=ReactionStateRepository(database),
        config=ReactionsConfig(),
        notes=notes,
    )
    return sync, maxi


async def _apply(sync: Any, emoji: str | None, pts: int = 2002410) -> None:
    await sync.apply_owner_reaction(
        telegram_bot_id=BOT, max_chat_id=MAX_CHAT, max_message_id=MAX_MESSAGE,
        owner_account_id=ACCOUNT, owner_message_id=MESSAGE, emoji=emoji,
        custom_id=None, pts=pts,
    )


async def test_a_supported_emoji_becomes_a_durable_job(database: Database) -> None:
    """Setting a reaction is idempotent, which is why it used to be a direct
    call. Idempotent is not accounted: a process dying between the update and
    the call left nothing behind saying the reaction was meant."""
    notes = FakeNotes()
    sync, maxi = _sync(database, notes)
    await _apply(sync, "👍")
    assert maxi.added == []
    assert [call["emoji"] for call in notes.reactions] == ["👍"]


async def test_changing_the_reaction_is_one_job_not_two(database: Database) -> None:
    """MAX replaces on set, so a change never needs a removal first — one logical
    transition, one job."""
    notes = FakeNotes()
    sync, _ = _sync(database, notes)
    await _apply(sync, "👍", pts=1)
    await _apply(sync, "❤️", pts=2)
    assert [call["emoji"] for call in notes.reactions] == ["👍", "❤️"]


async def test_clearing_is_a_job_carrying_no_emoji(database: Database) -> None:
    notes = FakeNotes()
    sync, maxi = _sync(database, notes)
    await _apply(sync, None)
    assert maxi.removed == []
    assert [call["emoji"] for call in notes.reactions] == [None]


async def test_the_reaction_job_is_keyed_by_the_update_version(
    database: Database,
) -> None:
    from bridge.routing.echo import owner_reaction_source_key

    once = owner_reaction_source_key(ACCOUNT, BOT, MESSAGE, 2002410)
    assert once == owner_reaction_source_key(ACCOUNT, BOT, MESSAGE, 2002410)
    assert once != owner_reaction_source_key(ACCOUNT, BOT, MESSAGE, 2002411)


async def test_an_unsupported_emoji_becomes_one_durable_note(database: Database) -> None:
    notes = FakeNotes()
    sync, maxi = _sync(database, notes)
    await _apply(sync, "🍄")
    assert maxi.added == []
    assert len(notes.calls) == 1
    assert notes.calls[0]["emoji"] == "🍄"


async def test_the_note_is_keyed_by_the_update_so_a_replay_finds_it(
    database: Database,
) -> None:
    """A replayed update produces the same key, which the outbox refuses twice.
    Setting the same unmappable emoji again *later* is a different event with a
    different version, and is a second note on purpose."""
    notes = FakeNotes()
    sync, _ = _sync(database, notes)
    await _apply(sync, "🍄", pts=2002410)
    await _apply(sync, "🍄", pts=2002410)
    await _apply(sync, "🍄", pts=2002499)

    keys = [call["source_key"] for call in notes.calls]
    assert keys[0] == keys[1] != keys[2]
    assert keys[0] == f"tg-owner-note:{ACCOUNT}:{BOT}:{MESSAGE}:2002410:🍄"


async def test_a_custom_emoji_clears_rather_than_leaving_the_old_one(
    database: Database,
) -> None:
    """The policy, named. Leaving the previous ordinary reaction in place would
    show the contact a choice the owner has moved on from, dressed up as the
    current one. Clearing shows less, and nothing false."""
    notes = FakeNotes()
    sync, maxi = _sync(database, notes)
    await sync.apply_owner_reaction(
        telegram_bot_id=BOT, max_chat_id=MAX_CHAT, max_message_id=MAX_MESSAGE,
        owner_account_id=ACCOUNT, owner_message_id=MESSAGE, emoji=None,
        custom_id="5000000000000000001", pts=1,
    )
    assert maxi.added == [] and notes.calls == []
    assert [call["emoji"] for call in notes.reactions] == [None]


# ------------------------------------------------ a baseline at first sight


async def test_a_new_message_is_baselined_where_it_is_seen(database: Database) -> None:
    """Nothing has reacted to a message that has only just been sent, so the
    empty set is a fact here rather than the guess it would be on an old one."""
    dispatch, _, _, state = await _wired(database)

    await dispatch.note_new_message(account_id=ACCOUNT, bot_id=BOT, message=Message())

    row = await state.get(account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE)
    assert row is not None and row.chosen_json == "[]" and row.pts == 0


async def test_the_first_reaction_on_a_new_message_is_carried(
    database: Database,
) -> None:
    """Without the baseline at intake this is the case that silently failed: a
    fresh message, a first reaction, and nothing to subtract from."""
    dispatch, _, reactions, _ = await _wired(database)

    await dispatch.note_new_message(account_id=ACCOUNT, bot_id=BOT, message=Message())
    await _feed(dispatch, Message(chosen=["👍"]), 2002410)

    assert [call["emoji"] for call in reactions.calls] == ["👍"]
    assert dispatch.counts.without_baseline == 0


async def test_noting_a_message_cannot_undo_an_update_that_raced_it(
    database: Database,
) -> None:
    """The echo and the update arrive on the same session and nothing orders
    them. `seed` never overwrites, so the newer reading wins."""
    dispatch, _, _reactions, state = await _wired(database)

    await _feed(dispatch, Message("стало", chosen=["👍"]), 2002410)
    await dispatch.note_new_message(account_id=ACCOUNT, bot_id=BOT, message=Message())

    row = await state.get(account_id=ACCOUNT, bot_id=BOT, message_id=MESSAGE)
    assert row is not None and row.pts == 2002410
