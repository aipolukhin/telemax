"""The status line — the only tick that can carry a time.

A bot cannot edit a message the owner sent, so `✓✓` appended to it is not an
option that exists. The line is the bot's own message, and these tests pin the
three things that make it usable: what it says, that it is edited rather than
re-sent, and that it moves back to the bottom once the conversation passes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest_asyncio

from bridge.config import PresenceConfig, ReadReceiptStyle, TimestampStyle
from bridge.presence.status_line import StatusLine, render
from bridge.storage import Database, ReadStateRepository

BRIDGE = "mom"
BOT_ID = 4242
CHAT_ID = 100000001

# Today's, deliberately. A stamp carries a date only when the message is *not*
# from today (that is the whole point of `compact`), so pinning a calendar date
# here made these tests pass until midnight and fail after it.
_TODAY = datetime.now().replace(hour=14, minute=10, second=0, microsecond=0)
DELIVERED = int(_TODAY.timestamp() * 1000)
READ = int((_TODAY + timedelta(minutes=2)).timestamp() * 1000)


@dataclass(slots=True)
class FakeRenderer:
    sent: list[str] = field(default_factory=list)
    edits: list[tuple[int, str]] = field(default_factory=list)
    deleted: list[int] = field(default_factory=list)
    next_id: int = 100
    edit_result: bool = True

    async def send_status(self, bot_id: int, chat_id: int, text: str) -> int | None:
        self.sent.append(text)
        self.next_id += 1
        return self.next_id

    async def edit_status(self, bot_id: int, chat_id: int, message_id: int, text: str) -> bool:
        self.edits.append((message_id, text))
        return self.edit_result

    async def delete_status(self, bot_id: int, chat_id: int, message_id: int) -> bool:
        self.deleted.append(message_id)
        return True


@pytest_asyncio.fixture
async def line(tmp_path: Path) -> Any:
    database = await Database.connect(tmp_path / "bridge.db")
    renderer = FakeRenderer()
    status = StatusLine(
        renderer=renderer,
        read_state=ReadStateRepository(database),
        config=PresenceConfig(read_receipt_style=ReadReceiptStyle.LINE),
        timestamp_style=TimestampStyle.COMPACT,
    )
    try:
        yield status, renderer, ReadStateRepository(database)
    finally:
        await database.close()


def test_render_says_what_actually_happened() -> None:
    assert render(DELIVERED, 0, TimestampStyle.COMPACT) == "✓ доставлено 14:10"
    assert (
        render(DELIVERED, READ, TimestampStyle.COMPACT)
        == "✓ доставлено 14:10 · ✓✓ прочитано 14:12"
    )
    assert render(0, 0, TimestampStyle.COMPACT) == ""


def test_render_keeps_the_time_even_when_stamps_are_off() -> None:
    """`message_timestamp: off` hides stamps on messages, not the tick's point."""
    assert render(DELIVERED, 0, TimestampStyle.OFF) == "✓ доставлено 14:10"


async def test_first_delivery_posts_the_line(line: Any) -> None:
    status, renderer, _ = line
    await status.note_delivered(BRIDGE, bot_id=BOT_ID, chat_id=CHAT_ID, at_ms=DELIVERED)

    assert renderer.sent == ["✓ доставлено 14:10"]
    assert renderer.edits == []


async def test_read_mark_edits_the_same_line(line: Any) -> None:
    """The line must not multiply: one message, edited in place."""
    status, renderer, read_state = line
    await status.note_delivered(BRIDGE, bot_id=BOT_ID, chat_id=CHAT_ID, at_ms=DELIVERED)
    await read_state.note_contact_read(BRIDGE, mark=READ, ticked_message_id=None)
    await status.note_read(BRIDGE, bot_id=BOT_ID, chat_id=CHAT_ID)

    assert len(renderer.sent) == 1
    assert renderer.edits == [(101, "✓ доставлено 14:10 · ✓✓ прочитано 14:12")]


async def test_unchanged_status_is_not_re_edited(line: Any) -> None:
    """Read marks repeat on every reconnect; editing on each would burn limits."""
    status, renderer, _ = line
    await status.note_delivered(BRIDGE, bot_id=BOT_ID, chat_id=CHAT_ID, at_ms=DELIVERED)
    await status.note_delivered(BRIDGE, bot_id=BOT_ID, chat_id=CHAT_ID, at_ms=DELIVERED)

    assert renderer.edits == []
    assert len(renderer.sent) == 1


async def test_new_messages_move_the_line_back_to_the_bottom(line: Any) -> None:
    status, renderer, read_state = line
    await status.note_delivered(BRIDGE, bot_id=BOT_ID, chat_id=CHAT_ID, at_ms=DELIVERED)

    status.note_chat_activity(BRIDGE)  # the contact wrote something
    await read_state.note_contact_read(BRIDGE, mark=READ, ticked_message_id=None)
    await status.note_read(BRIDGE, bot_id=BOT_ID, chat_id=CHAT_ID)

    assert renderer.deleted == [101], "the stale line has to go"
    assert len(renderer.sent) == 2
    assert renderer.sent[-1].endswith("✓✓ прочитано 14:12")


async def test_a_line_that_cannot_be_edited_is_reposted(line: Any) -> None:
    """The owner may delete the line; the tick must survive that."""
    status, renderer, read_state = line
    await status.note_delivered(BRIDGE, bot_id=BOT_ID, chat_id=CHAT_ID, at_ms=DELIVERED)
    renderer.edit_result = False

    await read_state.note_contact_read(BRIDGE, mark=READ, ticked_message_id=None)
    await status.note_read(BRIDGE, bot_id=BOT_ID, chat_id=CHAT_ID)

    assert len(renderer.sent) == 2


async def test_other_styles_leave_the_line_alone(tmp_path: Path) -> None:
    database = await Database.connect(tmp_path / "other.db")
    renderer = FakeRenderer()
    status = StatusLine(
        renderer=renderer,
        read_state=ReadStateRepository(database),
        config=PresenceConfig(read_receipt_style=ReadReceiptStyle.REACTION),
        timestamp_style=TimestampStyle.COMPACT,
    )
    try:
        await status.note_delivered(BRIDGE, bot_id=BOT_ID, chat_id=CHAT_ID, at_ms=DELIVERED)
        assert renderer.sent == []
    finally:
        await database.close()
