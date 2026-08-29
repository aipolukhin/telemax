"""The owner filter.

These bots are private. Anyone can find a bot by username and press Start, and
whatever they get back must not reveal that MAX exists, who the contact is, or
that this bot is part of anything at all. So the check runs as an outer
middleware — before any handler, on every update type, including callbacks and
reactions.

**Being the owner is not enough; the place has to be right too.** A bot can be
added to a group by anyone who can add bots, and until this was fixed the filter
looked only at who was speaking. The owner typing in that group passed the check
and their message went to a relative in MAX — a private conversation leaking out
of a group they never associated with the bridge. So a contact bot accepts
exactly one place: the private chat between the owner and itself.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import Chat, TelegramObject, User

logger = logging.getLogger(__name__)

# Deliberately bland. It says nothing about what the bot is for.
STRANGER_REPLY = "Этот бот недоступен."


def _is_private(data: dict[str, Any]) -> bool:
    """A one-to-one chat, whoever is on the other side of it."""
    chat: Chat | None = data.get("event_chat")
    return chat is not None and chat.type == ChatType.PRIVATE


class OwnerOnlyMiddleware(BaseMiddleware):
    """Drops every update that is not the owner, alone, in their own chat."""

    def __init__(self, owner_user_id: int, *, reply_to_strangers: bool = True) -> None:
        self._owner_user_id = owner_user_id
        self._reply = reply_to_strangers

    def _place_is_allowed(self, data: dict[str, Any]) -> bool:
        """The owner's private chat with this bot, and nowhere else.

        A private chat's id *is* the user's id, so the two checks together say:
        this is a one-to-one conversation, and the person on the other side is
        the owner. A group, supergroup or channel fails on both counts.
        """
        chat: Chat | None = data.get("event_chat")
        if chat is None:
            # Updates with no chat at all (business_connection, managed_bot)
            # are authorised by their sender alone; there is no room for a
            # third party in them. Every update this bot receives now carries
            # one — `deleted_business_messages`, the one that did not, is no
            # longer subscribed to.
            return True
        return chat.type == ChatType.PRIVATE and chat.id == self._owner_user_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")

        if user is not None and user.id == self._owner_user_id:
            if not self._place_is_allowed(data):
                chat: Chat | None = data.get("event_chat")
                logger.warning(
                    "dropped an owner update from a %s chat (%s): a contact bot only"
                    " answers in the owner's private chat",
                    getattr(chat, "type", "unknown"),
                    getattr(chat, "id", "unknown"),
                )
                return None
            return await handler(event, data)

        # No user at all (a service update we did not ask for) is dropped just
        # as firmly as a stranger.
        if user is not None:
            logger.info("dropped an update from user %s", user.id)
            # A bot is never a stranger who wandered in. Telegram attributes
            # some service updates to the bot itself, and answering one put
            # «Этот бот недоступен.» into the owner's own chat with their own
            # working bridge — the bot telling them it does not exist.
            #
            # The brush-off is for a person who found the bot and pressed Start.
            # Nothing else should ever produce it.
            if user.is_bot:
                return None
            if self._reply and _is_private(data):
                # A stranger who pressed Start gets one bland line back — in
                # their own private chat, which is a different question from
                # whether the chat is the owner's. Never answered in a group:
                # that would announce the bot to everyone in it.
                await self._brush_off(event, data)
        return None

    async def _brush_off(self, event: TelegramObject, data: dict[str, Any]) -> None:
        """Answer a stranger once, neutrally, and never mention MAX."""
        message = getattr(event, "message", None)
        bot = data.get("bot")
        if message is None or bot is None:
            return
        try:
            await bot.send_message(chat_id=message.chat.id, text=STRANGER_REPLY)
        except Exception:
            # A stranger's chat is not our problem; never let it affect delivery.
            logger.debug("could not answer a stranger", exc_info=True)
