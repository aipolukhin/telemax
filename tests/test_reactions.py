"""WP18 — the verified emoji set, the mapping, and the two-way sync."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest_asyncio

from bridge.config import ReactionsConfig, ReactionStyle
from bridge.max_client import (
    ChatReaction,
    MessageReactions,
    ReactionUpdate,
    chat_reaction_from,
    reactions_by_message,
)
from bridge.reactions import (
    LEGACY_ACCEPTED,
    REFUSED_BY_SERVER,
    SAFE,
    TELEGRAM_FREE,
    DialogActivity,
    ReactionSync,
    can_send,
    is_in_picker,
    to_max,
    to_telegram,
    unsupported,
)
from bridge.storage import (
    Database,
    MessageMapRepository,
    ReactionStateRepository,
)

BOT = 100
TG_CHAT = 111
MAX_CHAT = 777
OWNER_ACCOUNT = 100000001


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


# ------------------------------------------------------------------- the set


def test_the_three_sets_are_the_measured_ones() -> None:
    """Sizes come from the live probe; a change here means a re-measurement."""
    assert len(SAFE) == 66
    assert len(REFUSED_BY_SERVER) == 8
    assert len(LEGACY_ACCEPTED) == 13

    # The sets are disjoint by construction.
    assert not set(SAFE) & set(REFUSED_BY_SERVER)
    assert not set(SAFE) & set(LEGACY_ACCEPTED)


def test_sending_is_restricted_but_receiving_is_not() -> None:
    """The limit is on what we send; a contact may use anything."""
    assert can_send("👍") is True
    assert can_send("😂") is True, "legacy emoji are still accepted by the server"
    assert can_send("🟩") is False, "in the app picker, refused for our client"

    # ...but a refused emoji still has to be displayable.
    assert to_telegram("⚡") is not None


def test_the_apps_own_picker_is_still_the_narrower_set() -> None:
    """Telemax no longer draws a keyboard, but the distinction the measurement
    found is still real: what MAX *accepts* is wider than what its app offers."""
    assert is_in_picker("👍") is True
    assert is_in_picker("😂") is False, "legacy emoji are not in the app's picker"
    assert unsupported(("👍", "🟩", "❤️")) == ("🟩",)


# ------------------------------------------------------------------ mapping


def test_exact_matches_pass_through() -> None:
    assert to_telegram("👍") == "👍"
    assert to_max("👍") == "👍"


def test_near_misses_map_by_meaning_not_by_shape() -> None:
    assert to_max("😂") == "😂", "accepted as legacy, no substitution needed"
    assert to_telegram("🚀") == "🔥", "no rocket in Telegram's free set"
    assert to_max("⚡") == "🔥", "MAX refuses ⚡ from our client"
    # A cat is not a unicorn: a wrong substitution is worse than a text line.
    assert to_telegram("🐱") is None


def test_unmappable_reactions_become_text() -> None:
    from bridge.reactions import describe

    assert to_telegram("🛑") is None, "Telegram's free set has no stop sign"
    assert to_telegram("🫠") is None
    assert describe("🫠").endswith("🫠")


def test_every_safe_emoji_is_either_mappable_or_describable() -> None:
    """Nothing may vanish: each one either maps or falls back to text."""
    for emoji in SAFE:
        mapped = to_telegram(emoji)
        assert mapped is None or mapped in TELEGRAM_FREE


# --------------------------------------------------------------------- sync


@dataclass(slots=True)
class FakeRenderer:
    reactions: list[tuple[int, str | None]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    accept: bool = True

    async def set_reaction(
        self, bot_id: int, chat_id: int, message_id: int, emoji: str | None
    ) -> bool:
        if not self.accept:
            return False
        self.reactions.append((message_id, emoji))
        return True



@dataclass(slots=True)
class FakeMaxReactions:
    added: list[tuple[int, int, str]] = field(default_factory=list)
    removed: list[tuple[int, int]] = field(default_factory=list)
    replies: list[tuple[int, str, int | None]] = field(default_factory=list)
    #: What op180 answers with, per message id.
    server_counters: dict[int, dict[str, int]] = field(default_factory=dict)
    #: `yourReaction` in that answer — the owner's own, per message id.
    server_yours: dict[int, str] = field(default_factory=dict)
    asked: list[list[int]] = field(default_factory=list)

    async def reactions_for(
        self, chat_id: int, message_ids: list[int]
    ) -> dict[int, MessageReactions]:
        self.asked.append(message_ids)
        return {
            message_id: MessageReactions(
                counters=self.server_counters.get(message_id, {}),
                yours=self.server_yours.get(message_id),
            )
            for message_id in message_ids
        }

    async def add_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        self.added.append((chat_id, message_id, emoji))

    async def remove_reaction(self, chat_id: int, message_id: int) -> None:
        self.removed.append((chat_id, message_id))

    async def send_text(
        self, chat_id: int, text: str, *, reply_to: int | None = None
    ) -> int | None:
        self.replies.append((chat_id, text, reply_to))
        return 12345


async def mapped_message(database: Database) -> None:
    messages = MessageMapRepository(database)
    link_id = await messages.claim_from_max(
        bridge_name="mom",
        max_chat_id=MAX_CHAT,
        max_message_id=5,
        telegram_bot_id=BOT,
        telegram_chat_id=TG_CHAT,
    )
    assert link_id is not None
    await messages.attach_telegram_message(link_id, 55)


async def carry_and_run(sync: Any, database: Database, max_side: Any, **kwargs: Any) -> None:
    """The whole owner-reaction path: enqueue, then run the job the way the
    worker does. Two steps because they really are two — the point of the job is
    that the process may die between them."""
    from bridge.routing.owner_mutation import resolve_reaction
    from bridge.storage import OwnerMessageStateRepository

    notes = CollectingNotes()
    sync._notes = notes
    await sync.apply_owner_reaction(**kwargs)
    for call in notes.reactions:
        await resolve_reaction(
            state=OwnerMessageStateRepository(database),
            snapshots=ReactionStateRepository(database),
            max_sender=max_side,
            payload={
                "max_chat_id": call["max_chat_id"],
                "max_message_id": call["max_message_id"],
                "account_id": call["owner_account_id"],
                "bot_id": call["bot_id"],
                "owner_message_id": call["owner_message_id"],
                "emoji": call["emoji"],
                "pts": call["pts"],
            },
        )


class CollectingNotes:
    """The durable queue an emoji reply takes, as far as `ReactionSync` sees it."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reactions: list[dict[str, Any]] = []
        self.notes: list[dict[str, Any]] = []

    async def carry_emoji_note(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    async def carry_reaction_note(self, **kwargs: Any) -> None:
        self.notes.append(kwargs)

    async def carry_owner_reaction(self, **kwargs: Any) -> None:
        self.reactions.append(kwargs)


def build_sync(
    database: Database,
    renderer: FakeRenderer,
    max_side: FakeMaxReactions,
    style: ReactionStyle = ReactionStyle.NATIVE,
    notes: CollectingNotes | None = None,
) -> ReactionSync:
    return ReactionSync(
        renderer=renderer,
        max_sender=max_side,
        messages=MessageMapRepository(database),
        snapshots=ReactionStateRepository(database),
        config=ReactionsConfig(style=style),
        notes=notes,
    )


async def test_contact_reaction_appears_in_telegram(database: Database) -> None:
    await mapped_message(database)
    renderer, max_side = FakeRenderer(), FakeMaxReactions()
    sync = build_sync(database, renderer, max_side)

    await sync.on_max_reaction(
        ReactionUpdate(chat_id=MAX_CHAT, message_id=5, counters={"🔥": 1}, total=1),
        telegram_bot_id=BOT,
    )

    assert renderer.reactions == [(55, "🔥")]


async def test_only_the_delta_is_mirrored(database: Database) -> None:
    """MAX sends totals; the snapshot is what turns them into an event."""
    await mapped_message(database)
    renderer, max_side = FakeRenderer(), FakeMaxReactions()
    sync = build_sync(database, renderer, max_side)

    first = ReactionUpdate(chat_id=MAX_CHAT, message_id=5, counters={"🔥": 1}, total=1)
    await sync.on_max_reaction(first, telegram_bot_id=BOT)
    # The same totals again: nothing changed, so nothing to draw.
    await sync.on_max_reaction(first, telegram_bot_id=BOT)

    assert renderer.reactions == [(55, "🔥")]


async def test_cleared_reaction_is_cleared_in_telegram(database: Database) -> None:
    await mapped_message(database)
    renderer, max_side = FakeRenderer(), FakeMaxReactions()
    sync = build_sync(database, renderer, max_side)

    await sync.on_max_reaction(
        ReactionUpdate(chat_id=MAX_CHAT, message_id=5, counters={"🔥": 1}, total=1),
        telegram_bot_id=BOT,
    )
    await sync.on_max_reaction(
        ReactionUpdate(chat_id=MAX_CHAT, message_id=5, counters={}, total=0),
        telegram_bot_id=BOT,
    )

    assert renderer.reactions[-1] == (55, None)


async def test_unmappable_reaction_becomes_a_durable_note(database: Database) -> None:
    """It must not disappear just because Telegram has no such reaction.

    And it must not be a bare `send_message` from inside a poller either: a note
    is a message somebody receives, so it goes on the queue keyed by the message
    and the emoji.
    """
    await mapped_message(database)
    renderer, max_side, notes = FakeRenderer(accept=False), FakeMaxReactions(), CollectingNotes()
    sync = build_sync(database, renderer, max_side, notes=notes)

    await sync.on_max_reaction(
        ReactionUpdate(chat_id=MAX_CHAT, message_id=5, counters={"🫠": 1}, total=1),
        telegram_bot_id=BOT,
    )

    assert len(notes.notes) == 1
    assert "🫠" in notes.notes[0]["text"]
    assert notes.notes[0]["source_key"].startswith("max-react-note:")


async def test_a_note_that_cannot_be_written_down_leaves_the_snapshot_alone(
    database: Database,
) -> None:
    """Nothing stands for the reaction, so nothing may say anything does."""
    from bridge.storage import ReactionStateRepository

    await mapped_message(database)
    sync = build_sync(database, FakeRenderer(accept=False), FakeMaxReactions())

    await sync.on_max_reaction(
        ReactionUpdate(chat_id=MAX_CHAT, message_id=5, counters={"🫠": 1}, total=1),
        telegram_bot_id=BOT,
    )

    assert await ReactionStateRepository(database).get(MAX_CHAT, 5) is None


async def test_owner_reaction_becomes_one_durable_job(database: Database) -> None:
    """The projection into MAX, called with a resolved message rather than a
    bot-side id, and put on the queue rather than at MAX: idempotent is not the
    same as accounted."""
    renderer, max_side, notes = FakeRenderer(), FakeMaxReactions(), CollectingNotes()
    sync = build_sync(database, renderer, max_side, notes=notes)

    await sync.apply_owner_reaction(
        telegram_bot_id=BOT, max_chat_id=MAX_CHAT, max_message_id=999,
        owner_account_id=OWNER_ACCOUNT, owner_message_id=1002319,
        emoji="👍", custom_id=None, pts=1,
    )

    assert max_side.added == [] and max_side.removed == []
    assert [call["emoji"] for call in notes.reactions] == ["👍"]


async def test_reaction_max_lacks_becomes_an_emoji_reply(database: Database) -> None:
    """MAX has no 🐳 reaction, so it arrives as a reply consisting of that emoji —
    through the durable queue, which is what `notes` is."""
    max_side = FakeMaxReactions()
    notes = CollectingNotes()
    sync = build_sync(database, FakeRenderer(), max_side, notes=notes)

    await sync.apply_owner_reaction(
        telegram_bot_id=BOT, max_chat_id=MAX_CHAT, max_message_id=999,
        owner_account_id=OWNER_ACCOUNT, owner_message_id=1002319,
        emoji="🐳", custom_id=None, pts=1,
    )

    assert max_side.added == [], "an unsupported emoji must not be sent as a reaction"
    assert [call["emoji"] for call in notes.calls] == ["🐳"]


async def test_a_custom_emoji_clears_rather_than_showing_the_wrong_one(
    database: Database,
) -> None:
    """A document id names a sticker in somebody's pack. There is no ordinary
    emoji that *is* it, so MAX is told nothing — not the reaction that was there
    before, which is no longer the owner's choice."""
    max_side, notes = FakeMaxReactions(), CollectingNotes()
    sync = build_sync(database, FakeRenderer(), max_side, notes=notes)

    await sync.apply_owner_reaction(
        telegram_bot_id=BOT, max_chat_id=MAX_CHAT, max_message_id=999,
        owner_account_id=OWNER_ACCOUNT, owner_message_id=1002319,
        emoji=None, custom_id="5000000000000000001", pts=1,
    )

    assert max_side.added == [] and max_side.replies == [] and notes.calls == []
    assert [call["emoji"] for call in notes.reactions] == [None]


# ------------------------------------------------- reactions as a chat update (135)


def test_a_dialog_reports_its_reaction_as_two_chat_fields() -> None:
    """The shape taken off compatibility fixtures, 2026-07-29."""
    event = chat_reaction_from(
        {
            "chat": {
                "id": MAX_CHAT,
                "type": "DIALOG",
                "lastReactedMessageId": 111411200262144004,
                "lastReaction": "👍",
            }
        }
    )

    assert event == ChatReaction(
        chat_id=MAX_CHAT, message_id=111411200262144004, emoji="👍"
    )


def test_a_chat_update_without_a_reaction_carries_none() -> None:
    """Removal is announced by the two fields no longer being sent."""
    event = chat_reaction_from({"chat": {"id": MAX_CHAT, "type": "DIALOG"}})

    assert event == ChatReaction(chat_id=MAX_CHAT, message_id=None, emoji=None)


async def test_a_dialog_reaction_is_mirrored_from_the_chat_update(database: Database) -> None:
    """The path that actually fires in a private dialog: 155 never arrives."""
    await mapped_message(database)
    renderer, max_side = FakeRenderer(), FakeMaxReactions()
    max_side.server_counters = {5: {"🔥": 1}}
    sync = build_sync(database, renderer, max_side)

    await sync.on_chat_reaction(
        ChatReaction(chat_id=MAX_CHAT, message_id=5, emoji="🔥"), telegram_bot_id=BOT
    )

    assert renderer.reactions == [(55, "🔥")]


async def test_the_same_chat_update_is_not_drawn_twice(database: Database) -> None:
    """135 repeats on every unrelated change; the reaction is already on screen."""
    await mapped_message(database)
    renderer, max_side = FakeRenderer(), FakeMaxReactions()
    max_side.server_counters = {5: {"🔥": 1}}
    sync = build_sync(database, renderer, max_side)

    event = ChatReaction(chat_id=MAX_CHAT, message_id=5, emoji="🔥")
    await sync.on_chat_reaction(event, telegram_bot_id=BOT)
    await sync.on_chat_reaction(event, telegram_bot_id=BOT)

    assert renderer.reactions == [(55, "🔥")]


async def test_a_reaction_the_push_never_named_is_still_found(database: Database) -> None:
    """Three reactions produced two frames, and neither named the third message."""
    await mapped_message(database)
    renderer, max_side = FakeRenderer(), FakeMaxReactions()
    max_side.server_counters = {5: {"😁": 1}}
    sync = build_sync(database, renderer, max_side)

    # A chat update that says nothing about reactions at all.
    await sync.on_chat_reaction(
        ChatReaction(chat_id=MAX_CHAT, message_id=None, emoji=None), telegram_bot_id=BOT
    )

    assert max_side.asked == [[5]], "the window of recent messages is what gets asked about"
    assert renderer.reactions == [(55, "😁")]


async def test_the_owners_own_reaction_is_not_mirrored_back(database: Database) -> None:
    """Setting a reaction for the owner makes MAX report it right back at us."""
    messages = MessageMapRepository(database)
    link_id = await messages.record_from_telegram(
        bridge_name="mom",
        max_chat_id=MAX_CHAT,
        telegram_bot_id=BOT,
        telegram_chat_id=TG_CHAT,
        telegram_message_id=77,
    )
    await messages.attach_max_message(link_id, 999)

    renderer, max_side = FakeRenderer(), FakeMaxReactions()
    # The bridge acts as the owner's own MAX session, so the server attributes
    # the reaction back to us in `yourReaction`.
    max_side.server_counters = {999: {"👍": 1}}
    max_side.server_yours = {999: "👍"}
    sync = build_sync(database, renderer, max_side)

    await carry_and_run(
        sync, database, max_side,
        telegram_bot_id=BOT, max_chat_id=MAX_CHAT, max_message_id=999,
        owner_account_id=OWNER_ACCOUNT, owner_message_id=1002319,
        emoji="👍", custom_id=None, pts=1,
    )
    await sync.on_chat_reaction(
        ChatReaction(chat_id=MAX_CHAT, message_id=999, emoji="👍"), telegram_bot_id=BOT
    )

    assert max_side.added == [(MAX_CHAT, 999, "👍")]
    assert renderer.reactions == [], "the owner already sees their own reaction"


def test_a_dialog_is_warm_only_for_a_while() -> None:
    """The fast polling cadence has to expire, or it is not a cadence at all."""
    activity = DialogActivity(warm_seconds=0.05)

    assert activity.is_warm(MAX_CHAT) is False, "never heard from"
    activity.touch(MAX_CHAT)
    assert activity.is_warm(MAX_CHAT) is True
    time.sleep(0.06)
    assert activity.is_warm(MAX_CHAT) is False


def test_an_op180_answer_is_read_by_message(database: Database) -> None:
    """The exact answer shape, copied from compatibility fixtures on 2026-07-29."""
    parsed = reactions_by_message(
        {
            "messagesReactions": {
                "111411200458752007": {
                    "counters": [{"reaction": "❤️", "count": 1}],
                    "totalCount": 1,
                },
                "111411200393216006": {
                    "counters": [{"reaction": "❤️", "count": 1}],
                    "yourReaction": "❤️",
                    "totalCount": 1,
                },
                "111411200327680005": {},
            }
        }
    )

    assert parsed[111411200458752007] == MessageReactions(counters={"❤️": 1}, yours=None)
    assert parsed[111411200393216006] == MessageReactions(counters={"❤️": 1}, yours="❤️")
    assert parsed[111411200327680005] == MessageReactions(counters={}, yours=None)


async def test_our_own_reaction_reported_by_the_server_is_not_mirrored(
    database: Database,
) -> None:
    """`yourReaction` is how the owner's own reaction is told apart from the contact's."""
    await mapped_message(database)
    renderer, max_side = FakeRenderer(), FakeMaxReactions()
    max_side.server_counters = {5: {"❤️": 1}}
    max_side.server_yours = {5: "❤️"}
    sync = build_sync(database, renderer, max_side)

    await sync.on_chat_reaction(
        ChatReaction(chat_id=MAX_CHAT, message_id=5, emoji="❤️"), telegram_bot_id=BOT
    )

    assert renderer.reactions == []


async def test_the_contact_is_seen_behind_our_own_reaction(database: Database) -> None:
    """Counters are totals: ours has to come off before the rest is the contact's."""
    messages = MessageMapRepository(database)
    link_id = await messages.record_from_telegram(
        bridge_name="mom",
        max_chat_id=MAX_CHAT,
        telegram_bot_id=BOT,
        telegram_chat_id=TG_CHAT,
        telegram_message_id=77,
    )
    await messages.attach_max_message(link_id, 999)

    renderer, max_side = FakeRenderer(), FakeMaxReactions()
    max_side.server_counters = {999: {"👍": 1, "🔥": 1}}
    sync = build_sync(database, renderer, max_side)

    await carry_and_run(
        sync, database, max_side,
        telegram_bot_id=BOT, max_chat_id=MAX_CHAT, max_message_id=999,
        owner_account_id=OWNER_ACCOUNT, owner_message_id=1002319,
        emoji="👍", custom_id=None, pts=1,
    )
    await sync.on_chat_reaction(
        ChatReaction(chat_id=MAX_CHAT, message_id=999, emoji="🔥"), telegram_bot_id=BOT
    )

    assert renderer.reactions == [(77, "🔥")]


async def test_a_dialog_reaction_going_away_is_cleared(database: Database) -> None:
    """Removal is never announced: the server has to be asked and answers empty."""
    await mapped_message(database)
    renderer, max_side = FakeRenderer(), FakeMaxReactions()
    max_side.server_counters = {5: {"🔥": 1}}
    sync = build_sync(database, renderer, max_side)

    event = ChatReaction(chat_id=MAX_CHAT, message_id=5, emoji="🔥")
    await sync.on_chat_reaction(event, telegram_bot_id=BOT)

    max_side.server_counters = {5: {}}
    await sync.on_chat_reaction(
        ChatReaction(chat_id=MAX_CHAT, message_id=None, emoji=None), telegram_bot_id=BOT
    )

    assert renderer.reactions == [(55, "🔥"), (55, None)]


async def test_a_poll_reports_whether_anything_moved(database: Database) -> None:
    """The poller reads this to put a woken dialog on the fast cadence."""
    await mapped_message(database)
    max_side = FakeMaxReactions()
    max_side.server_counters = {5: {"🔥": 1}}
    sync = build_sync(database, FakeRenderer(), max_side)

    assert await sync.poll(MAX_CHAT, telegram_bot_id=BOT) is True
    assert await sync.poll(MAX_CHAT, telegram_bot_id=BOT) is False, "nothing new the second time"


async def test_an_unchanged_dialog_draws_nothing(database: Database) -> None:
    """Most of the traffic on 135 has nothing to do with reactions."""
    await mapped_message(database)
    renderer, max_side = FakeRenderer(), FakeMaxReactions()
    sync = build_sync(database, renderer, max_side)

    await sync.on_chat_reaction(
        ChatReaction(chat_id=MAX_CHAT, message_id=None, emoji=None), telegram_bot_id=BOT
    )

    assert renderer.reactions == []


# ------------------------------------------- the snapshot advances after the effect


async def test_a_refused_drawing_leaves_the_snapshot_where_it_was(
    database: Database,
) -> None:
    """The defect: the snapshot moved first, so the poll saw nothing left to do.

    A reaction Telegram refused, on a message the notes could not be written for
    either, must stay *undone* — the next poll is the retry, and it can only
    retry what the snapshot still says is missing.
    """
    from bridge.storage import ReactionStateRepository

    await mapped_message(database)
    renderer, max_side = FakeRenderer(accept=False), FakeMaxReactions()
    sync = build_sync(database, renderer, max_side)

    await sync.on_max_reaction(
        ReactionUpdate(chat_id=MAX_CHAT, message_id=5, counters={"❤": 1}, total=1),
        telegram_bot_id=BOT,
    )

    assert await ReactionStateRepository(database).get(MAX_CHAT, 5) is None


async def test_a_reaction_on_an_unbound_target_is_not_counted_as_done(
    database: Database,
) -> None:
    """An owner-placed message whose echo has not arrived yet has no bot-side id."""
    from bridge.storage import MessageMapRepository, ReactionStateRepository

    messages = MessageMapRepository(database)
    link_id = await messages.claim_from_max(
        bridge_name="mom",
        max_chat_id=MAX_CHAT,
        max_message_id=77,
        telegram_bot_id=BOT,
        telegram_chat_id=TG_CHAT,
    )
    assert link_id is not None
    renderer = FakeRenderer()
    sync = build_sync(database, renderer, FakeMaxReactions())

    await sync.on_max_reaction(
        ReactionUpdate(chat_id=MAX_CHAT, message_id=77, counters={"👍": 1}, total=1),
        telegram_bot_id=BOT,
    )

    assert renderer.reactions == []
    assert await ReactionStateRepository(database).get(MAX_CHAT, 77) is None


async def test_the_binding_arriving_later_lets_the_poll_draw_it(
    database: Database,
) -> None:
    """The whole point of not advancing: the reaction is still owed, and lands."""
    from bridge.storage import MessageMapRepository

    messages = MessageMapRepository(database)
    link_id = await messages.claim_from_max(
        bridge_name="mom",
        max_chat_id=MAX_CHAT,
        max_message_id=78,
        telegram_bot_id=BOT,
        telegram_chat_id=TG_CHAT,
    )
    assert link_id is not None
    renderer, max_side = FakeRenderer(), FakeMaxReactions()
    sync = build_sync(database, renderer, max_side)

    await sync.on_max_reaction(
        ReactionUpdate(chat_id=MAX_CHAT, message_id=78, counters={"👍": 1}, total=1),
        telegram_bot_id=BOT,
    )
    assert renderer.reactions == []

    # The echo arrives and the message finally has a bot-side id.
    await messages.attach_telegram_message(link_id, 5678)
    max_side.server_counters[78] = {"👍": 1}
    assert await sync.poll(MAX_CHAT, telegram_bot_id=BOT)
    assert renderer.reactions == [(5678, "👍")]


async def test_a_successful_drawing_advances_the_snapshot_once(
    database: Database,
) -> None:
    from bridge.storage import ReactionStateRepository

    await mapped_message(database)
    renderer, max_side = FakeRenderer(), FakeMaxReactions()
    sync = build_sync(database, renderer, max_side)
    event = ReactionUpdate(chat_id=MAX_CHAT, message_id=5, counters={"❤": 1}, total=1)

    await sync.on_max_reaction(event, telegram_bot_id=BOT)
    await sync.on_max_reaction(event, telegram_bot_id=BOT)

    stored = await ReactionStateRepository(database).get(MAX_CHAT, 5)
    assert stored is not None and stored.counters == {"❤": 1}
    assert renderer.reactions == [(55, "❤")], "a duplicate MAX event draws once"


async def test_a_replayed_reaction_makes_one_note(database: Database) -> None:
    """The source key is what stops a second line in the chat."""
    await mapped_message(database)
    notes = CollectingNotes()
    sync = build_sync(database, FakeRenderer(accept=False), FakeMaxReactions(), notes=notes)
    event = ReactionUpdate(chat_id=MAX_CHAT, message_id=5, counters={"🫠": 1}, total=1)

    await sync.on_max_reaction(event, telegram_bot_id=BOT)
    await sync.on_max_reaction(event, telegram_bot_id=BOT)

    assert len({note["source_key"] for note in notes.notes}) == 1
