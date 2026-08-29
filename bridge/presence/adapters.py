"""Telegram-side implementations of the presence ports."""

from __future__ import annotations

import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SetMessageReaction
from aiogram.types import ReactionTypeEmoji

from bridge.telegram import BotRegistry

logger = logging.getLogger(__name__)


class TelegramPresenceAdapter:
    """`ChatActionSender` and `ReceiptRenderer` over the live bot registry."""

    def __init__(self, registry: BotRegistry) -> None:
        self._registry = registry

    async def send_chat_action(self, bot_id: int, chat_id: int, action: str) -> None:
        live = self._registry.by_bot_id(bot_id)
        if live is None:
            return
        await live.bot.send_chat_action(chat_id=chat_id, action=action)

    async def append_suffix(self, bot_id: int, chat_id: int, message_id: int, suffix: str) -> bool:
        """Add the tick to a message, once.

        Telegram rejects an edit that changes nothing, which is exactly what a
        second attempt on an already-ticked message looks like — so that error
        counts as success, not failure.
        """
        live = self._registry.by_bot_id(bot_id)
        if live is None:
            return False

        try:
            await live.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=await self._current_text(bot_id, chat_id, message_id, suffix),
            )
        except TelegramBadRequest as error:
            text = str(error).lower()
            if "message is not modified" in text:
                return True
            # No text to edit (a video note, a sticker), or the message is too
            # old. The caller falls back to a reaction.
            logger.debug("cannot append a tick to %s: %s", message_id, error)
            return False
        except Exception:
            logger.debug("tick edit failed for %s", message_id, exc_info=True)
            return False
        return True

    async def _current_text(self, bot_id: int, chat_id: int, message_id: int, suffix: str) -> str:
        """Text for the edited message.

        Bot API cannot read a message back, so the text has to come from
        somewhere else. read and presence state keeps this simple on purpose: the caller edits the
        message it just sent, and the sender records that text.
        """
        cached = _LAST_TEXT.get((bot_id, chat_id, message_id))
        return f"{cached or ''}{suffix}" if cached else suffix.strip()

    # ------------------------------------------------------------- status line

    async def send_status(self, bot_id: int, chat_id: int, text: str) -> int | None:
        """Post the status line. Silent: it must never notify.

        The line changes on every read mark, and a notification per tick would
        make the bridge unbearable.
        """
        live = self._registry.by_bot_id(bot_id)
        if live is None:
            return None
        try:
            message = await live.bot.send_message(
                chat_id=chat_id, text=text, disable_notification=True
            )
        except Exception:
            logger.debug("could not post the status line", exc_info=True)
            return None
        return int(message.message_id)

    async def edit_status(self, bot_id: int, chat_id: int, message_id: int, text: str) -> bool:
        live = self._registry.by_bot_id(bot_id)
        if live is None:
            return False
        try:
            await live.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
        except TelegramBadRequest as error:
            # An identical edit is refused; the caller already avoids that, but
            # a race can still get here and it means the line is correct.
            return "message is not modified" in str(error).lower()
        except Exception:
            logger.debug("could not edit the status line %s", message_id, exc_info=True)
            return False
        return True

    async def delete_status(self, bot_id: int, chat_id: int, message_id: int) -> bool:
        live = self._registry.by_bot_id(bot_id)
        if live is None:
            return False
        try:
            await live.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            # Older than 48 hours, or already gone. Either way the new line is
            # what matters and the stale one is left where it is.
            logger.debug("could not delete the status line %s", message_id, exc_info=True)
            return False
        return True

    async def pin_status(self, bot_id: int, chat_id: int, message_id: int) -> bool:
        """Pin the presence line. Bots may pin their own messages in a private chat."""
        live = self._registry.by_bot_id(bot_id)
        if live is None:
            return False
        try:
            await live.bot.pin_chat_message(
                chat_id=chat_id, message_id=message_id, disable_notification=True
            )
        except Exception:
            logger.debug("could not pin %s", message_id, exc_info=True)
            return False
        return True

    async def set_reaction(self, bot_id: int, chat_id: int, message_id: int, emoji: str) -> bool:
        live = self._registry.by_bot_id(bot_id)
        if live is None:
            return False
        try:
            await live.bot(
                SetMessageReaction(
                    chat_id=chat_id,
                    message_id=message_id,
                    reaction=[ReactionTypeEmoji(emoji=emoji)],
                    # A tick is not an occasion. `is_big` plays the reaction
                    # full-screen over the chat, and the default for an omitted
                    # field is the server's to change — so say it out loud.
                    is_big=False,
                )
            )
        except Exception:
            # A bot may react in a private chat (private-chat reaction support), but the slot
            # holds one reaction and the message may be too old to touch, so a
            # refusal here is ordinary and stays quiet.
            logger.debug("could not set a reaction on %s", message_id, exc_info=True)
            return False
        return True


# What the bridge last sent for a given message, so a tick can be appended
# without asking Telegram to read the message back (Bot API cannot).
_LAST_TEXT: dict[tuple[int, int, int], str] = {}


def remember_text(bot_id: int, chat_id: int, message_id: int, text: str) -> None:
    _LAST_TEXT[(bot_id, chat_id, message_id)] = text
    # Only the newest message per chat can be ticked, so the cache never needs
    # to grow past a handful of entries per bridge.
    if len(_LAST_TEXT) > 512:
        for key in list(_LAST_TEXT)[:256]:
            _LAST_TEXT.pop(key, None)
