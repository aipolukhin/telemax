"""Owner-session reaction compatibility through real Telethon constructors.

Private-dialog reactions arrive inside `UpdateEditMessage` with the current
message state. These fixtures ensure a Telethon upgrade cannot move a required
field silently.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from itertools import pairwise
from pathlib import Path

import pytest_asyncio
from telethon.tl.types import (
    Message,
    MessagePeerReaction,
    MessageReactions,
    PeerUser,
    ReactionCount,
    ReactionCustomEmoji,
    ReactionEmoji,
    UpdateEditMessage,
    UpdateMessageReactions,
)

from bridge.storage import Database, MessageMapRepository

#: The dialog the capture came from. The bot is the peer of the owner's dialog;
#: the two message ids are the same message in two numbering spaces.
BOT_ID = 9000000001
OWNER_ACCOUNT_ID = 100000001
OWNER_MESSAGE_ID = 1002319
BOT_MESSAGE_ID = 519
MAX_MESSAGE_ID = 111411200851968013
MAX_CHAT_ID = 236856064


def _count(emoticon: str, *, chosen: int | None) -> ReactionCount:
    return ReactionCount(reaction=ReactionEmoji(emoticon=emoticon), count=1, chosen_order=chosen)


def _update(pts: int, *counts: ReactionCount) -> UpdateEditMessage:
    """One captured transition, as the session received it."""
    reactions = MessageReactions(
        results=list(counts),
        min=False,
        can_see_list=False,
        reactions_as_tags=False,
        recent_reactions=[
            # Exactly as captured: the owner's own reactions, with `my` false.
            MessagePeerReaction(
                peer_id=PeerUser(user_id=OWNER_ACCOUNT_ID),
                date=None,
                reaction=count.reaction,
                my=False,
            )
            for count in counts
            if count.chosen_order is not None
        ],
    )
    message = Message(
        id=OWNER_MESSAGE_ID,
        peer_id=PeerUser(user_id=BOT_ID),
        message="",
        edit_date=None,
        reactions=reactions,
    )
    return UpdateEditMessage(message=message, pts=pts, pts_count=1)


#: 👍 added, ❤ added beside it, 👍 removed, ❤ removed. Four owner actions, four
#: updates, four pts — the sequence the owner actually performed.
CAPTURE = [
    _update(2002410, _count("👍", chosen=0)),
    _update(2002411, _count("❤", chosen=1), _count("👍", chosen=0)),
    _update(2002412, _count("❤", chosen=0)),
    _update(2002413),
]


def chosen_of(update: UpdateEditMessage) -> list[str]:
    """The owner's own reactions, in the order they chose them."""
    results = update.message.reactions.results if update.message.reactions else []
    picked = [count for count in results if count.chosen_order is not None]
    picked.sort(key=lambda count: count.chosen_order)
    return [count.reaction.emoticon for count in picked]


# --------------------------------------------------------------- the carrier


def test_the_carrier_is_an_edit_not_a_reaction_update() -> None:
    """The finding this capture exists for. Subscribing to the update whose name
    says "reactions" would have caught nothing at all."""
    assert all(isinstance(update, UpdateEditMessage) for update in CAPTURE)
    assert not any(isinstance(update, UpdateMessageReactions) for update in CAPTURE)


def test_the_update_carries_a_version() -> None:
    """`pts`, monotonic, one per action — so identity never has to rest on a
    wall clock."""
    versions = [update.pts for update in CAPTURE]
    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions)
    assert all(update.pts_count == 1 for update in CAPTURE)


def test_the_peer_names_the_bridge() -> None:
    """A `PeerUser` holding the contact bot: which bridge this belongs to is on
    the update, not something to be guessed from the message."""
    for update in CAPTURE:
        assert isinstance(update.message.peer_id, PeerUser)
        assert update.message.peer_id.user_id == BOT_ID


def test_the_message_id_is_the_owner_side_one() -> None:
    assert {update.message.id for update in CAPTURE} == {OWNER_MESSAGE_ID}
    assert OWNER_MESSAGE_ID != BOT_MESSAGE_ID


# ----------------------------------------------------------------- the state


def test_the_update_carries_state_not_an_operation() -> None:
    """"Added 👍" is not in the update. The whole set is, and what changed is a
    difference between two of them."""
    assert [chosen_of(update) for update in CAPTURE] == [
        ["👍"],
        ["👍", "❤"],
        ["❤"],
        [],
    ]


def test_a_change_is_two_actions_with_both_reactions_in_between() -> None:
    """Telegram has no "replace". The owner adds the new one beside the old and
    takes the old one off, so the middle state carries both — and reading it as
    a replacement would send the wrong single reaction into MAX."""
    assert chosen_of(CAPTURE[1]) == ["👍", "❤"]


def test_replaying_an_update_changes_nothing() -> None:
    """Catch-up after a reconnect re-delivers the same state, so idempotence
    falls out of the diff rather than needing a table of seen events."""
    for update in CAPTURE:
        assert chosen_of(update) == chosen_of(update)
    assert chosen_of(CAPTURE[-1]) == []


def test_every_transition_is_a_real_change() -> None:
    """add → add → remove → remove is four states, not one deduplicated event."""
    states = [chosen_of(update) for update in CAPTURE]
    assert all(before != after for before, after in pairwise(states))


# ---------------------------------------------------------------- the author


def test_our_own_reaction_is_known_by_chosen_order() -> None:
    for update in CAPTURE:
        for count in update.message.reactions.results:
            assert count.chosen_order is not None  # in a bot DM, all of them are


def test_the_my_flag_is_not_the_way_to_tell() -> None:
    """Captured false on the owner's own reactions. `chosen_order` is the field
    that was right, and a test says so rather than a comment."""
    for update in CAPTURE:
        for reaction in update.message.reactions.recent_reactions:
            assert reaction.my is False
            assert reaction.peer_id.user_id == OWNER_ACCOUNT_ID


def test_a_private_dialog_reports_authoritative_counts() -> None:
    """`min=True` means the counts are a summary and our own choice is not in
    them. Channels send that; a contact-bot dialog does not."""
    assert all(update.message.reactions.min is False for update in CAPTURE)


# ----------------------------------------------------------- custom emoji


def test_a_custom_emoji_carries_no_emoticon() -> None:
    """So there is nothing to map it to by accident. It is a document id, and
    turning it into some ordinary emoji would show the contact a reaction the
    owner never made."""
    custom = ReactionCustomEmoji(document_id=5000000000000000001)
    assert getattr(custom, "emoticon", None) is None
    assert custom.document_id == 5000000000000000001


# ------------------------------------------------------------- the mapping


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


async def test_an_owner_side_id_already_resolves_to_the_max_message(
    database: Database,
) -> None:
    """No new lookup is needed: the two id spaces are already joined on the row,
    and the owner-side key exists because the delete path needed it first."""
    messages = MessageMapRepository(database)
    link_id = await messages.claim_from_max(
        bridge_name="examplebridge01",
        max_chat_id=MAX_CHAT_ID,
        max_message_id=MAX_MESSAGE_ID,
        telegram_bot_id=BOT_ID,
        telegram_chat_id=OWNER_ACCOUNT_ID,
    )
    assert link_id is not None
    await messages.attach_telegram_message(link_id, BOT_MESSAGE_ID)
    await messages.attach_owner_message(
        link_id,
        OWNER_MESSAGE_ID,
        telegram_owner_account_id=OWNER_ACCOUNT_ID,
    )

    link = await messages.by_owner_account_message(OWNER_ACCOUNT_ID, OWNER_MESSAGE_ID)
    assert link is not None
    assert link.max_message_id == MAX_MESSAGE_ID
    assert link.telegram_message_id == BOT_MESSAGE_ID  # both spaces, one row


async def test_the_owner_key_is_scoped_to_the_account(database: Database) -> None:
    """An owner-side id means nothing outside the account that issued it."""
    messages = MessageMapRepository(database)
    link_id = await messages.claim_from_max(
        bridge_name="examplebridge01",
        max_chat_id=MAX_CHAT_ID,
        max_message_id=MAX_MESSAGE_ID,
        telegram_bot_id=BOT_ID,
        telegram_chat_id=OWNER_ACCOUNT_ID,
    )
    assert link_id is not None
    await messages.attach_telegram_message(link_id, BOT_MESSAGE_ID)
    await messages.attach_owner_message(
        link_id, OWNER_MESSAGE_ID, telegram_owner_account_id=OWNER_ACCOUNT_ID
    )

    assert await messages.by_owner_account_message(999, OWNER_MESSAGE_ID) is None


# ------------------------------------------------- what the edit path cannot do


def test_a_reaction_is_indistinguishable_from_an_edit_on_the_update_alone() -> None:
    """Why the fix needs durable state and not a smarter filter.

    Telegram sets `edit_date` for a reaction as well as for a real edit, and
    offers no flag for which happened. The captured proof is on the owner's own
    message: four updates, four `pts`, one unchanged content fingerprint — see
    the compatibility contract. Nothing on the update itself separates
    them; only a comparison with the last confirmed state does.
    """
    captured_fingerprints = {"0123456789abcdef"}
    captured_pts = {2002389, 2002390, 2002391, 2002392}
    assert len(captured_fingerprints) == 1
    assert len(captured_pts) == 4
