"""Reaction ports, implemented over aiogram and the MAX client."""

from __future__ import annotations

import logging

from aiogram.methods import SetMessageReaction
from aiogram.types import ReactionTypeCustomEmoji, ReactionTypeEmoji, ReactionTypePaid

from bridge.max_client import MaxClient, MessageReactions
from bridge.telegram import BotRegistry

logger = logging.getLogger(__name__)


class TelegramReactionAdapter:
    """Draws a reaction in Telegram. Saying it in words is somebody else's job.

    It used to have a `send_note` that called `send_message` directly — a
    message somebody receives, made from inside a poll whose exceptions were
    swallowed at `debug` level. That belongs on the queue like every other
    created message, so it lives there now and this draws reactions only.
    """

    def __init__(self, registry: BotRegistry) -> None:
        self._registry = registry

    async def set_reaction(
        self, bot_id: int, chat_id: int, message_id: int, emoji: str | None
    ) -> bool:
        live = self._registry.by_bot_id(bot_id)
        if live is None:
            return False

        # A bot may set at most one reaction, and an empty list clears it.
        reaction: list[ReactionTypeEmoji | ReactionTypeCustomEmoji | ReactionTypePaid] = (
            [ReactionTypeEmoji(emoji=emoji)] if emoji else []
        )
        try:
            await live.bot(
                SetMessageReaction(chat_id=chat_id, message_id=message_id, reaction=reaction)
            )
        except Exception:
            # A bot may react in a private chat (private-chat reaction support), but
            # only with one emoji at a time — a second is refused with
            # `REACTIONS_TOO_MANY` — and Telegram refuses reactions on some
            # messages outright. The caller falls back to a text note.
            logger.debug("could not set reaction %s on %s", emoji, message_id, exc_info=True)
            return False
        return True


class MaxReactionAdapter:
    """Reactions and emoji replies on the MAX side."""

    def __init__(self, client: MaxClient) -> None:
        self._client = client

    async def add_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        await self._client.add_reaction(chat_id, message_id, emoji)

    async def remove_reaction(self, chat_id: int, message_id: int) -> None:
        await self._client.remove_reaction(chat_id, message_id)

    async def send_text(
        self, chat_id: int, text: str, *, reply_to: int | None = None
    ) -> int | None:
        return await self._client.send_text(chat_id, text, reply_to=reply_to)

    async def reactions_for(
        self, chat_id: int, message_ids: list[int]
    ) -> dict[int, MessageReactions]:
        return await self._client.reactions_for(chat_id, message_ids)
