"""Typing indicators, in both directions — and they are not symmetric.

**MAX to Telegram** is a real mirror: PyMax reports the contact typing, and
`sendChatAction` shows it. Telegram's indicator lasts about five seconds, so it
has to be refreshed while the contact keeps typing, and allowed to lapse when
they stop.

**Telegram to MAX cannot be a mirror.** Bot API never tells a bot that the owner
is typing — there is no such update. What the bridge *can* do is show "typing"
in MAX while it is busy carrying something: the twenty seconds spent downloading
a video from Telegram and uploading it to MAX are exactly the seconds the
contact sees nothing at all. That is synthetic, and it is the only honest use of
op65 here.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

from bridge.config import OutgoingTyping, PresenceConfig
from bridge.max_client import TypingKind

logger = logging.getLogger(__name__)


class ChatActionSender(Protocol):
    async def send_chat_action(self, bot_id: int, chat_id: int, action: str) -> None: ...


class MaxTypingSender(Protocol):
    async def send_typing(self, chat_id: int, kind: TypingKind = TypingKind.TEXT) -> None: ...


class TelegramTypingMirror:
    """Shows "typing" in Telegram while the contact is typing in MAX.

    Rate limiting matters here: MAX sends a typing event per keystroke burst, and
    forwarding each one would spend the bridge's Bot API budget on an indicator.
    """

    def __init__(
        self,
        sender: ChatActionSender,
        config: PresenceConfig,
        *,
        clock: object = time,
    ) -> None:
        self._sender = sender
        self._config = config
        self._clock = clock
        self._last_sent: dict[str, float] = {}

    def _now(self) -> float:
        return float(self._clock.monotonic())  # type: ignore[attr-defined]

    async def on_contact_typing(self, bridge_name: str, bot_id: int, chat_id: int) -> None:
        if not self._config.mirror_typing:
            return

        now = self._now()
        last = self._last_sent.get(bridge_name)
        if last is not None and now - last < self._config.typing_refresh_seconds:
            return

        self._last_sent[bridge_name] = now
        try:
            await self._sender.send_chat_action(bot_id, chat_id, "typing")
        except Exception:
            # An indicator is never worth failing anything else over.
            logger.debug("could not mirror typing for %s", bridge_name, exc_info=True)

    def forget(self, bridge_name: str) -> None:
        self._last_sent.pop(bridge_name, None)


class MaxTypingLoop:
    """Keeps "typing" alive in MAX for as long as the bridge is busy.

    op65 is fire-and-forget and its indicator expires on its own, so the loop
    just repeats until the work is done. The period is a safe guess until
    research item typing-state behaviour measures the real lifetime.
    """

    def __init__(self, sender: MaxTypingSender, config: PresenceConfig) -> None:
        self._sender = sender
        self._config = config

    def _enabled(self, *, is_media: bool) -> bool:
        mode = self._config.outgoing_typing
        if mode is OutgoingTyping.OFF:
            return False
        if mode is OutgoingTyping.MEDIA_ONLY:
            return is_media
        return True

    @asynccontextmanager
    async def busy(
        self,
        chat_id: int,
        *,
        kind: TypingKind = TypingKind.TEXT,
        is_media: bool = False,
    ) -> AsyncIterator[None]:
        """Show "typing" in MAX for the duration of the block."""
        if not self._enabled(is_media=is_media):
            yield
            return

        task = asyncio.create_task(self._repeat(chat_id, kind), name=f"max-typing-{chat_id}")
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _repeat(self, chat_id: int, kind: TypingKind) -> None:
        while True:
            await self._sender.send_typing(chat_id, kind)
            await asyncio.sleep(self._config.max_typing_period_seconds)
