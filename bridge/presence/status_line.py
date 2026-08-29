"""The status line: one bot message that says what happened to your messages.

A bot may not edit a message somebody else sent, and everything the owner types
into the bot chat belongs to the owner. So there is no way to append `✓✓` to it
— the tick has to live in a message the bot owns.

Hence one line per bridge, kept at the bottom of the chat and edited in place:

    ✓ доставлено 14:10 · ✓✓ прочитано 14:12

Both halves are real. `✓` is the moment MAX accepted `MSG_SEND` — MAX has no
separate delivery signal at all (research item delivery-state behaviour), so that is the strongest
statement anyone can make. `✓✓` is the contact's own read mark, and MAX reports
it as a millisecond timestamp, which is why the line can name the minute.

The line is re-posted rather than edited when the conversation has moved past
it: a status stuck twenty messages up is worse than no status at all.
"""

from __future__ import annotations

import logging
from datetime import tzinfo
from typing import Protocol

from bridge.config import PresenceConfig, ReadReceiptStyle, TimestampStyle
from bridge.formatting import format_clock
from bridge.storage import ReadStateRepository

logger = logging.getLogger(__name__)

DELIVERED_MARK = "✓ доставлено"
READ_MARK = "✓✓ прочитано"
SEPARATOR = " · "


class StatusRenderer(Protocol):
    """What the Telegram side must provide for the line."""

    async def send_status(self, bot_id: int, chat_id: int, text: str) -> int | None: ...

    async def edit_status(self, bot_id: int, chat_id: int, message_id: int, text: str) -> bool: ...

    async def delete_status(self, bot_id: int, chat_id: int, message_id: int) -> bool: ...


def render(delivered_at: int, read_at: int, style: TimestampStyle,
           tz: tzinfo | None = None) -> str:
    """The line's text. Empty when there is nothing to report yet."""
    # `message_timestamp: off` hides stamps on messages; a tick without a time
    # would say nothing at all, so the line keeps its clock either way.
    del style

    def at(moment: int) -> str:
        return format_clock(moment, tz=tz)

    parts: list[str] = []
    if delivered_at:
        parts.append(f"{DELIVERED_MARK} {at(delivered_at)}")
    if read_at:
        parts.append(f"{READ_MARK} {at(read_at)}")
    return SEPARATOR.join(parts)


class StatusLine:
    """Keeps one status message per bridge in step with reality."""

    def __init__(
        self,
        *,
        renderer: StatusRenderer,
        read_state: ReadStateRepository,
        config: PresenceConfig,
        timestamp_style: TimestampStyle = TimestampStyle.COMPACT,
        timezone: tzinfo | None = None,
    ) -> None:
        self._renderer = renderer
        self._read_state = read_state
        self._config = config
        self._timestamp_style = timestamp_style
        self._timezone = timezone
        #: Bridges whose line is no longer the last message in the chat.
        self._stale: set[str] = set()

    @property
    def enabled(self) -> bool:
        return self._config.read_receipt_style is ReadReceiptStyle.LINE

    def note_chat_activity(self, bridge_name: str) -> None:
        """Something new was delivered, so the line is no longer at the bottom."""
        if self.enabled:
            self._stale.add(bridge_name)

    async def note_delivered(
        self, bridge_name: str, *, bot_id: int, chat_id: int, at_ms: int
    ) -> None:
        """MAX accepted a message of ours: draw or refresh the single tick."""
        if not self.enabled:
            return
        await self._read_state.note_delivered(bridge_name, at_ms=at_ms)
        await self._refresh(bridge_name, bot_id=bot_id, chat_id=chat_id)

    async def note_read(self, bridge_name: str, *, bot_id: int, chat_id: int) -> None:
        """The contact's watermark moved; the caller has already stored it."""
        if not self.enabled:
            return
        await self._refresh(bridge_name, bot_id=bot_id, chat_id=chat_id)

    async def _refresh(self, bridge_name: str, *, bot_id: int, chat_id: int) -> None:
        marks = await self._read_state.get(bridge_name)
        text = render(
            marks.delivered_at, marks.contact_read_mark, self._timestamp_style, self._timezone
        )
        if not text:
            return

        stale = bridge_name in self._stale
        message_id = marks.status_message_id

        if message_id is not None and not stale:
            if text == marks.status_text:
                return
            if await self._renderer.edit_status(bot_id, chat_id, message_id, text):
                await self._read_state.set_status_line(
                    bridge_name, message_id=message_id, text=text
                )
                return
            # The line is gone (the owner deleted it) or too old to edit: fall
            # through and post a new one.
            logger.debug("status line for %s could not be edited, reposting", bridge_name)

        if message_id is not None:
            await self._renderer.delete_status(bot_id, chat_id, message_id)

        new_id = await self._renderer.send_status(bot_id, chat_id, text)
        if new_id is None:
            return
        self._stale.discard(bridge_name)
        await self._read_state.set_status_line(bridge_name, message_id=new_id, text=text)
