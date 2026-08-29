"""Learning who wrote a forwarded message, from the side that can see it.

Two transports look at the same forwarded message and are told two different
things about its author. The owner's MTProto session gets the author as the
owner filed them — an address-book rename included — and Telegram offers no way
to ask for anything else: a forward header is drawn by each client from the
peer, so the author's own name never travels. A contact bot has no address book,
so the very same peer reaches it as the profile itself.

So the bot's view is recorded as it goes past, and the intake reads it. This is
a middleware rather than a handler on purpose — it must run whatever the intake
gate decides, since the gate closes the Bot API *delivery* path while this is
the one thing only that path can see. It observes and never consumes: the
update carries on to whatever would have handled it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from .forwards import name_of_entity

logger = logging.getLogger(__name__)


class ForwardAuthorStore:
    """What the middleware writes into and the intake reads out of."""

    async def remember(self, peer_id: int, *, name: str, username: str | None = None) -> None: ...

    async def author_of(self, peer_id: int) -> tuple[str, str | None] | None: ...


def author_of(message: Any) -> tuple[int, str, str | None] | None:
    """`(peer_id, name, username)` for a forwarded message's author, if named.

    Only `MessageOriginUser` carries a person. A hidden sender has no id worth
    recording — Telegram sends the name they had at the time and nothing to key
    it by — and a channel origin needs no learning at all, because a channel
    title is the same for everybody who can see it.
    """
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return None
    user = getattr(origin, "sender_user", None)
    if user is None:
        return None
    peer_id = getattr(user, "id", None)
    if peer_id is None:
        return None
    name = name_of_entity(user)
    if not name:
        return None
    username = getattr(user, "username", None)
    return int(peer_id), name, str(username) if username else None


class LearnForwardAuthors(BaseMiddleware):
    """Records the author of every forward the bots are shown."""

    def __init__(self, store: ForwardAuthorStore) -> None:
        self._store = store

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        message = getattr(event, "message", None) or getattr(event, "edited_message", None)
        learned = author_of(message) if message is not None else None
        if learned is not None:
            peer_id, name, username = learned
            try:
                await self._store.remember(peer_id, name=name, username=username)
            except Exception:
                # A name is decoration; the message is not. Learning that fails
                # must never cost the update behind it.
                logger.debug("could not record a forwarded author", exc_info=True)
        return await handler(event, data)
