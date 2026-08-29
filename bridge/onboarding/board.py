"""One message, edited in place, for everything the guardian says.

A status line that arrives as a new message every few seconds is worse than no
status line: the owner scrolls, loses the buttons, and taps a stale one. So the
guardian keeps a single message and edits it, and remembers which one across
restarts — otherwise a service restart would leave two live keyboards in the
chat, both of which look current.

This used to hold only onboarding. It holds the whole control surface now —
menu, status, the dialog picker, provisioning progress — because a second
message with its own keyboard is exactly the thing the anchor exists to prevent.

Two rules worth naming:

* **a redraw that changes nothing is not sent.** Telegram answers an unchanged
  edit with an error, and the comparison has to cover the keyboard as well as
  the text: the same sentence under a different set of buttons is a different
  screen.
* **an edit that fails posts instead of going silent.** A message older than 48
  hours cannot be edited, and the owner is owed an answer either way.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram.types import InlineKeyboardMarkup

from .state import StateStore

logger = logging.getLogger(__name__)

Screen = tuple[str, InlineKeyboardMarkup | None]


class StatusBoard:
    """Owns the guardian's one message."""

    def __init__(self, *, bot: Any, chat_id: int, store: StateStore) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._store = store
        self._last: tuple[str, str] | None = None

    @property
    def chat_id(self) -> int:
        return self._chat_id

    @property
    def message_id(self) -> int | None:
        """Which message is the anchor right now, or None before the first draw."""
        record = self._store.load()
        if record.status_chat_id != self._chat_id:
            return None
        return record.status_message_id

    async def show(self, screen: Screen) -> bool:
        """Draw the screen. False when it could not be put anywhere.

        Never raises. A bot may not write to somebody who has never pressed
        Start, and there is no method that says whether they have — the send is
        the test. A brand-new guardian therefore *always* fails this on its first
        draw, and letting that out killed the service at start-up: the owner
        could not press Start, because pressing Start needs a bot that is
        polling, and the process died before it could poll. A screen that cannot
        be drawn is not a reason to refuse to serve.
        """
        text, markup = screen
        fingerprint = (text, str(markup))
        record = self._store.load()

        if record.status_message_id is not None and record.status_chat_id == self._chat_id:
            if fingerprint == self._last:
                # Telegram answers "message is not modified" with an error, and
                # a redraw that changes nothing is not worth one.
                return True
            try:
                await self._bot.edit_message_text(
                    chat_id=self._chat_id,
                    message_id=record.status_message_id,
                    text=text,
                    reply_markup=markup,
                )
            except Exception:  # noqa: BLE001 - too old to edit, or deleted by hand
                logger.debug("could not edit the status message; posting a new one")
            else:
                self._last = fingerprint
                return True

        return await self._post(text, markup)

    async def draw(self, text: str, markup: InlineKeyboardMarkup | None) -> None:
        """`show` with the pair spread out, for callers that build it that way.

        `DialogFlow` draws through a two-argument callable; giving it the anchor
        rather than the message a tap arrived on is what stopped the picker
        posting a second keyboard next to the one it came from.
        """
        await self.show((text, markup))

    async def _post(self, text: str, markup: InlineKeyboardMarkup | None) -> bool:
        """Put a new message in the chat. False when there is no chat yet.

        The anchor is deliberately *not* remembered on failure: the next draw
        tries again, and by then the owner has usually written something, which
        is what makes the chat exist.
        """
        try:
            sent = await self._bot.send_message(
                chat_id=self._chat_id, text=text, reply_markup=markup
            )
        except Exception as error:  # noqa: BLE001 - any refusal, and each is different
            # It used to assert the cause: "the owner has not started the bot".
            # That is *a* reason and it was the reason once, so the line was
            # written as if it were the only one. Then a keyboard Telegram
            # refused produced the same sentence, and the guardian's menu went
            # blank while the log insisted the owner had never pressed Start.
            #
            # A screen that cannot be drawn is still not a reason to refuse to
            # serve — but it is every reason to say what actually happened.
            reason = str(error)
            if "chat not found" in reason.lower():
                logger.info("the owner has not started the guardian bot yet; screen not drawn")
            else:
                logger.warning("could not draw the guardian screen: %s", reason)
            return False
        message_id = getattr(sent, "message_id", None)
        self._store.update(
            status_chat_id=self._chat_id,
            status_message_id=int(message_id) if message_id is not None else None,
        )
        self._last = (text, str(markup))
        return True

    def forget(self) -> None:
        """Next `show` posts a fresh message — used when the chat has moved on."""
        self._store.update(status_message_id=None)
        self._last = None
