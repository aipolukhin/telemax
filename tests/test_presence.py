"""WP17 — typing mirrors and read ticks."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest_asyncio

from bridge.config import AutoRead as AutoReadMode
from bridge.config import PresenceConfig, ReadReceiptStyle
from bridge.max_client import TypingKind
from bridge.presence import READ_REACTION, AutoRead, MaxTypingLoop, ReadReceipts
from bridge.presence.typing import TelegramTypingMirror
from bridge.storage import (
    Database,
    Direction,
    MediaGroupRepository,
    MessageMapRepository,
    ReadStateRepository,
)

BOT = 100
TG_CHAT = 111
MAX_CHAT = 777
#: The owner's Telegram account. Their message ids live in a different sequence
#: from the bot's, so every read watermark below is scoped by this as well.
OWNER = 100000001


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value


@dataclass(slots=True)
class FakeActions:
    actions: list[tuple[int, int, str]] = field(default_factory=list)
    fail: bool = False

    async def send_chat_action(self, bot_id: int, chat_id: int, action: str) -> None:
        if self.fail:
            raise RuntimeError("telegram said no")
        self.actions.append((bot_id, chat_id, action))


@dataclass(slots=True)
class FakeRenderer:
    suffixes: list[tuple[int, str]] = field(default_factory=list)
    reactions: list[tuple[int, str]] = field(default_factory=list)
    suffix_ok: bool = True
    reaction_ok: bool = True

    async def append_suffix(self, bot_id: int, chat_id: int, message_id: int, suffix: str) -> bool:
        if not self.suffix_ok:
            return False
        self.suffixes.append((message_id, suffix))
        return True

    async def set_reaction(self, bot_id: int, chat_id: int, message_id: int, emoji: str) -> bool:
        if not self.reaction_ok:
            return False
        self.reactions.append((message_id, emoji))
        return True


@dataclass(slots=True)
class FakeMaxTyping:
    sent: list[tuple[int, TypingKind]] = field(default_factory=list)

    async def send_typing(self, chat_id: int, kind: TypingKind = TypingKind.TEXT) -> None:
        self.sent.append((chat_id, kind))


@dataclass(slots=True)
class FakeMarker:
    marked: list[tuple[int, int]] = field(default_factory=list)
    #: Set to make MAX refuse the mark, the way a dropped connection does.
    fail: bool = False

    async def mark_read(self, chat_id: int, message_id: int) -> None:
        if self.fail:
            raise RuntimeError("MAX refused the read mark")
        self.marked.append((chat_id, message_id))


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


# ------------------------------------------------------------------- typing


async def test_typing_mirror_is_rate_limited() -> None:
    """MAX fires a typing event per keystroke burst; Telegram must not."""
    actions = FakeActions()
    clock = FakeClock()
    mirror = TelegramTypingMirror(actions, PresenceConfig(), clock=clock)

    for _ in range(5):
        await mirror.on_contact_typing("mom", BOT, TG_CHAT)

    assert len(actions.actions) == 1

    clock.value += 5  # past typing_refresh_seconds
    await mirror.on_contact_typing("mom", BOT, TG_CHAT)
    assert len(actions.actions) == 2


async def test_typing_mirror_can_be_switched_off() -> None:
    actions = FakeActions()
    mirror = TelegramTypingMirror(actions, PresenceConfig(mirror_typing=False), clock=FakeClock())

    await mirror.on_contact_typing("mom", BOT, TG_CHAT)

    assert actions.actions == []


async def test_typing_mirror_never_raises() -> None:
    """An indicator must not be able to break delivery."""
    mirror = TelegramTypingMirror(FakeActions(fail=True), PresenceConfig(), clock=FakeClock())
    await mirror.on_contact_typing("mom", BOT, TG_CHAT)


async def test_max_typing_runs_only_for_media_by_default() -> None:
    sender = FakeMaxTyping()
    loop = MaxTypingLoop(sender, PresenceConfig(max_typing_period_seconds=0.01))

    async with loop.busy(MAX_CHAT, is_media=False):
        await asyncio.sleep(0.03)
    assert sender.sent == [], "media_only must not type for plain text"

    async with loop.busy(MAX_CHAT, kind=TypingKind.VIDEO, is_media=True):
        await asyncio.sleep(0.03)

    assert sender.sent, "the contact should see typing while a file uploads"
    assert all(kind is TypingKind.VIDEO for _, kind in sender.sent)


async def test_max_typing_stops_when_the_work_is_done() -> None:
    sender = FakeMaxTyping()
    loop = MaxTypingLoop(sender, PresenceConfig(max_typing_period_seconds=0.01))

    async with loop.busy(MAX_CHAT, is_media=True):
        await asyncio.sleep(0.03)
    sent_at_exit = len(sender.sent)

    await asyncio.sleep(0.05)
    assert len(sender.sent) == sent_at_exit, "typing kept going after the upload finished"


# ------------------------------------------------------------------- receipts


async def deliver(database: Database, *, message_id: int, telegram_message_id: int) -> None:
    messages = MessageMapRepository(database)
    link_id = await messages.claim_from_max(
        bridge_name="mom",
        max_chat_id=MAX_CHAT,
        max_message_id=message_id,
        telegram_bot_id=BOT,
        telegram_chat_id=TG_CHAT,
    )
    assert link_id is not None
    await messages.attach_telegram_message(link_id, telegram_message_id)


async def send_to_max(
    database: Database, *, telegram_message_id: int, max_message_id: int
) -> None:
    """A message the owner sent — the one a read mark is actually about."""
    messages = MessageMapRepository(database)
    link_id = await messages.record_from_telegram(
        bridge_name="mom",
        max_chat_id=MAX_CHAT,
        telegram_bot_id=BOT,
        telegram_chat_id=TG_CHAT,
        telegram_message_id=telegram_message_id,
    )
    await messages.attach_max_message(link_id, max_message_id)


def receipts(database: Database, renderer: FakeRenderer, config: PresenceConfig) -> ReadReceipts:
    return ReadReceipts(
        renderer=renderer,
        messages=MessageMapRepository(database),
        read_state=ReadStateRepository(database),
        config=config,
    )


#: These exercise the suffix style specifically; the default is the reaction.
SUFFIX_STYLE = PresenceConfig(read_receipt_style=ReadReceiptStyle.SUFFIX)

#: A watermark comfortably newer than anything the test just recorded.
FUTURE = 2**42


async def test_tick_lands_on_the_newest_message_only(database: Database) -> None:
    """One edit per read mark — not a redraw of the whole history."""
    await send_to_max(database, telegram_message_id=11, max_message_id=1)
    await send_to_max(database, telegram_message_id=12, max_message_id=2)

    renderer = FakeRenderer()
    assert await receipts(database, renderer, SUFFIX_STYLE).on_contact_read("mom", mark=FUTURE)

    assert renderer.suffixes == [(12, " ✓✓")]


async def test_the_tick_is_about_our_message_not_theirs(database: Database) -> None:
    """A read mark says the contact read *us*; ticking their own message is nonsense."""
    await deliver(database, message_id=1, telegram_message_id=11)  # from the contact
    renderer = FakeRenderer()

    assert await receipts(database, renderer, SUFFIX_STYLE).on_contact_read(
        "mom", mark=FUTURE
    ) is False
    assert renderer.suffixes == []


async def test_a_message_sent_after_the_mark_is_not_ticked(database: Database) -> None:
    """MAX reports a watermark: anything newer than it has not been read."""
    await send_to_max(database, telegram_message_id=11, max_message_id=1)
    renderer = FakeRenderer()

    assert await receipts(database, renderer, SUFFIX_STYLE).on_contact_read(
        "mom", mark=1
    ) is False
    assert renderer.suffixes == []


async def test_repeated_marks_do_not_re_edit(database: Database) -> None:
    """Read marks repeat on every reconnect; the watermark absorbs them."""
    await send_to_max(database, telegram_message_id=11, max_message_id=1)
    renderer = FakeRenderer()
    receipt = receipts(database, renderer, SUFFIX_STYLE)

    assert await receipt.on_contact_read("mom", mark=FUTURE) is True
    assert await receipt.on_contact_read("mom", mark=FUTURE) is False
    assert await receipt.on_contact_read("mom", mark=FUTURE - 10) is False

    assert len(renderer.suffixes) == 1


async def test_tick_falls_back_to_a_reaction(database: Database) -> None:
    """A video note has no caption to edit — react instead of giving up."""
    await send_to_max(database, telegram_message_id=11, max_message_id=1)
    renderer = FakeRenderer(suffix_ok=False)

    assert await receipts(database, renderer, SUFFIX_STYLE).on_contact_read("mom", mark=FUTURE)

    assert renderer.reactions == [(11, READ_REACTION)]


async def test_ticks_can_be_switched_off(database: Database) -> None:
    await deliver(database, message_id=1, telegram_message_id=11)
    renderer = FakeRenderer()
    config = PresenceConfig(read_receipt_style=ReadReceiptStyle.OFF)

    assert await receipts(database, renderer, config).on_contact_read("mom", mark=100) is False
    assert renderer.suffixes == []


async def test_no_tick_before_anything_was_delivered(database: Database) -> None:
    renderer = FakeRenderer()
    assert await receipts(database, renderer, SUFFIX_STYLE).on_contact_read("mom", mark=1) is (
        False
    )


# ------------------------------------------------------------------ auto read


def auto_read(database: Database, marker: FakeMarker, mode: AutoReadMode) -> AutoRead:
    return AutoRead(
        marker=marker,
        read_state=ReadStateRepository(database),
        config=PresenceConfig(auto_read=mode),
        messages=MessageMapRepository(database),
    )


async def carry_into_telegram(
    database: Database,
    *,
    max_message_id: int,
    telegram_message_id: int,
    owner_message_id: int | None = None,
    bridge_name: str = "mom",
    owner_account_id: int = OWNER,
) -> int:
    """One MAX→Telegram message, mapped the way the delivery path maps it.

    The two ids are deliberately far apart, because in a real private chat they
    are: a contact bot numbers its own messages from 1 and the owner's account
    numbers theirs in the millions. Every test below that feeds a read watermark
    feeds an *owner-side* number, and would pass just as well against the bot's
    if the two were allowed to look alike — which is exactly how the id-space
    confusion survived a green suite once.
    """
    messages = MessageMapRepository(database)
    link_id = await messages.claim_from_max(
        bridge_name=bridge_name,
        max_chat_id=MAX_CHAT,
        max_message_id=max_message_id,
        telegram_bot_id=BOT,
        telegram_chat_id=TG_CHAT,
    )
    assert link_id is not None
    await messages.attach_telegram_message(link_id, telegram_message_id)
    if owner_message_id is not None:
        await messages.attach_owner_message(
            link_id, owner_message_id, telegram_owner_account_id=owner_account_id
        )
    return link_id


async def test_reply_marks_the_chat_read(database: Database) -> None:
    marker = FakeMarker()
    reader = auto_read(database, marker, AutoReadMode.ON_REPLY)
    reader.note_incoming("mom", max_chat_id=MAX_CHAT, max_message_id=42)

    assert await reader.on_owner_replied("mom") is True
    assert marker.marked == [(MAX_CHAT, 42)]

    # Nothing new arrived, so a second reply marks nothing again.
    assert await reader.on_owner_replied("mom") is False


async def test_on_delivery_is_not_the_default(database: Database) -> None:
    """It changes what the contact sees, so it must be opted into."""
    marker = FakeMarker()
    reader = auto_read(database, marker, AutoReadMode.ON_REPLY)
    reader.note_incoming("mom", max_chat_id=MAX_CHAT, max_message_id=42)

    assert await reader.on_delivered_to_telegram("mom") is False
    assert marker.marked == []

    eager = auto_read(database, marker, AutoReadMode.ON_DELIVERY)
    eager.note_incoming("mom", max_chat_id=MAX_CHAT, max_message_id=42)
    assert await eager.on_delivered_to_telegram("mom") is True


async def carry_album_into_telegram(
    database: Database,
    *,
    max_message_id: int,
    parts: list[tuple[int, int]],
    bridge_name: str = "mom",
    owner_account_id: int = OWNER,
) -> int:
    """One MAX album: one canonical message, N aliases with their own ids.

    `parts` are `(telegram_message_id, owner_message_id)` in order. Only the
    head's owner-side id reaches `message_map`; the tail lives on the aliases,
    which is why reading the *last* photo of an album has to be resolvable
    through them.
    """
    albums = MediaGroupRepository(database)
    link_id = await carry_into_telegram(
        database,
        max_message_id=max_message_id,
        telegram_message_id=parts[0][0],
        owner_message_id=parts[0][1],
        bridge_name=bridge_name,
        owner_account_id=owner_account_id,
    )
    for index, (telegram_message_id, owner_message_id) in enumerate(parts):
        assert await albums.add_part(
            media_group_id=f"max:{link_id}",
            bridge_name=bridge_name,
            bot_id=BOT,
            telegram_message_id=telegram_message_id,
            payload={"kind": "photo"},
            link_id=link_id,
            direction=Direction.MAX_TO_TG,
            part_index=index,
            media_kind="photo",
            caption_present=index == 0,
            part_fingerprint=f"a1:{index}",
            telegram_owner_account_id=owner_account_id,
            telegram_owner_message_id=owner_message_id,
        )
    return link_id


async def test_opening_the_chat_marks_what_was_actually_read(database: Database) -> None:
    """`on_read`: the watermark is the owner's own id, not the bot's.

    Five messages, bot-side ids in the hundreds and owner-side ids past a
    million — the spread a live install actually shows. The owner reads only the
    first, so only the first may be marked. Against `telegram_message_id` every
    row would have matched and the newest would have been marked, which is the
    defect this test exists to hold shut.
    """
    marker = FakeMarker()
    reader = auto_read(database, marker, AutoReadMode.ON_READ)
    for n in range(1, 6):
        await carry_into_telegram(
            database,
            max_message_id=500 + n,
            telegram_message_id=100 + n,
            owner_message_id=1_121_000 + n,
        )

    assert await reader.on_owner_read("mom", owner_account_id=OWNER, owner_message_id=1_121_001)
    assert marker.marked == [(MAX_CHAT, 501)], "only the message the owner reached"

    # Scrolling on marks the next one, and only up to where they got.
    assert await reader.on_owner_read("mom", owner_account_id=OWNER, owner_message_id=1_121_003)
    assert marker.marked[-1] == (MAX_CHAT, 503)


async def test_reading_the_tail_of_an_album_marks_the_message_it_became(
    database: Database,
) -> None:
    """An album is N Telegram messages and one MAX message.

    The owner scrolls past its last photo, whose owner-side id is on an alias and
    not on the canonical row. Resolving through the aliases is what makes the
    tick land on the message the album became.
    """
    marker = FakeMarker()
    reader = auto_read(database, marker, AutoReadMode.ON_READ)
    await carry_album_into_telegram(
        database,
        max_message_id=700,
        parts=[(201, 1_122_001), (202, 1_122_002), (203, 1_122_003)],
    )

    assert await reader.on_owner_read("mom", owner_account_id=OWNER, owner_message_id=1_122_003)
    assert marker.marked == [(MAX_CHAT, 700)]


async def test_a_row_without_an_owner_side_id_is_not_a_candidate(database: Database) -> None:
    """Its echo has not arrived, so nothing says the owner reached it."""
    marker = FakeMarker()
    reader = auto_read(database, marker, AutoReadMode.ON_READ)
    await carry_into_telegram(database, max_message_id=500, telegram_message_id=101)

    assert (
        await reader.on_owner_read("mom", owner_account_id=OWNER, owner_message_id=1_121_999)
        is False
    )
    assert marker.marked == []


async def test_a_read_never_reaches_another_bridge_or_account(database: Database) -> None:
    """The watermark is scoped by both, and an id means nothing outside its account."""
    marker = FakeMarker()
    reader = auto_read(database, marker, AutoReadMode.ON_READ)
    await carry_into_telegram(
        database,
        max_message_id=500,
        telegram_message_id=101,
        owner_message_id=1_121_001,
        bridge_name="dad",
    )

    assert (
        await reader.on_owner_read("mom", owner_account_id=OWNER, owner_message_id=1_121_001)
        is False
    ), "another bridge's message"
    assert (
        await reader.on_owner_read("dad", owner_account_id=OWNER + 1, owner_message_id=1_121_001)
        is False
    ), "another owner account's numbering"
    assert marker.marked == []
    assert await reader.on_owner_read("dad", owner_account_id=OWNER, owner_message_id=1_121_001)
    assert marker.marked == [(MAX_CHAT, 500)]


async def test_a_read_arriving_before_the_echo_settles_waits_for_it(
    database: Database,
) -> None:
    """Out of order: the read event beats the binding that names the message.

    The mark lands on the newest message that *is* bound, never on the one whose
    owner-side id has not arrived — and once the echo settles, the next read
    event carries it.
    """
    marker = FakeMarker()
    reader = auto_read(database, marker, AutoReadMode.ON_READ)
    messages = MessageMapRepository(database)
    await carry_into_telegram(
        database, max_message_id=500, telegram_message_id=101, owner_message_id=1_121_001
    )
    unsettled = await carry_into_telegram(
        database, max_message_id=600, telegram_message_id=102
    )

    assert await reader.on_owner_read("mom", owner_account_id=OWNER, owner_message_id=1_121_002)
    assert marker.marked == [(MAX_CHAT, 500)], "the bound one, not the newer unbound one"

    await messages.attach_owner_message(
        unsettled, 1_121_002, telegram_owner_account_id=OWNER
    )
    assert await reader.on_owner_read("mom", owner_account_id=OWNER, owner_message_id=1_121_002)
    assert marker.marked[-1] == (MAX_CHAT, 600)


async def test_a_repeated_read_event_does_not_re_mark(database: Database) -> None:
    """Telegram repeats the watermark across devices; MAX must not be told twice."""
    marker = FakeMarker()
    reader = auto_read(database, marker, AutoReadMode.ON_READ)
    await carry_into_telegram(
        database, max_message_id=500, telegram_message_id=101, owner_message_id=1_121_001
    )
    await carry_into_telegram(
        database, max_message_id=600, telegram_message_id=102, owner_message_id=1_121_002
    )

    assert await reader.on_owner_read("mom", owner_account_id=OWNER, owner_message_id=1_121_002)
    assert (
        await reader.on_owner_read("mom", owner_account_id=OWNER, owner_message_id=1_121_002)
        is False
    )
    # And an older event never walks the mark backwards.
    assert (
        await reader.on_owner_read("mom", owner_account_id=OWNER, owner_message_id=1_121_001)
        is False
    )
    assert marker.marked == [(MAX_CHAT, 600)]


async def test_a_failed_mark_leaves_the_watermark_where_it_was(database: Database) -> None:
    """MAX refused, so the tick is owed — and the next read event must still owe it.

    The watermark used to move first: the mark failed, the mark was remembered,
    and every repeat of the same read event was refused as "already done". The
    tick would never be drawn.
    """
    marker = FakeMarker(fail=True)
    reader = auto_read(database, marker, AutoReadMode.ON_READ)
    await carry_into_telegram(
        database, max_message_id=500, telegram_message_id=101, owner_message_id=1_121_001
    )

    assert (
        await reader.on_owner_read("mom", owner_account_id=OWNER, owner_message_id=1_121_001)
        is False
    )
    assert marker.marked == []

    marker.fail = False
    assert await reader.on_owner_read("mom", owner_account_id=OWNER, owner_message_id=1_121_001)
    assert marker.marked == [(MAX_CHAT, 500)]


async def test_a_read_of_a_chat_with_nothing_carried_marks_nothing(database: Database) -> None:
    marker = FakeMarker()
    reader = auto_read(database, marker, AutoReadMode.ON_READ)

    assert (
        await reader.on_owner_read("mom", owner_account_id=OWNER, owner_message_id=1_121_001)
        is False
    )
    assert marker.marked == []


async def test_only_on_read_listens_to_telegram_reads(database: Database) -> None:
    marker = FakeMarker()
    await carry_into_telegram(
        database, max_message_id=500, telegram_message_id=101, owner_message_id=1_121_001
    )

    for mode in (AutoReadMode.ON_REPLY, AutoReadMode.ON_DELIVERY, AutoReadMode.OFF):
        reader = auto_read(database, marker, mode)
        assert (
            await reader.on_owner_read(
                "mom", owner_account_id=OWNER, owner_message_id=1_121_001
            )
            is False
        )
    assert marker.marked == []


async def test_answering_counts_as_reading_under_on_read(database: Database) -> None:
    """Without the owner session that is the only half left, and it must work."""
    marker = FakeMarker()
    reader = auto_read(database, marker, AutoReadMode.ON_READ)
    reader.note_incoming("mom", max_chat_id=MAX_CHAT, max_message_id=42)

    assert await reader.on_owner_replied("mom") is True
    assert marker.marked == [(MAX_CHAT, 42)]


async def test_off_still_allows_the_read_command(database: Database) -> None:
    marker = FakeMarker()
    reader = auto_read(database, marker, AutoReadMode.OFF)
    reader.note_incoming("mom", max_chat_id=MAX_CHAT, max_message_id=42)

    assert await reader.on_owner_replied("mom") is False
    assert await reader.mark_now("mom") is True
    assert marker.marked == [(MAX_CHAT, 42)]
