"""Telegram handlers for read marks. Reactions no longer enter here.

The owner's reactions used to arrive two ways: `message_reaction`, and a `/react`
picker written when it was still unknown whether Telegram delivers that update in
a private chat at all. Both are gone. A reaction is something the owner does in
Telegram, and everything the owner does in Telegram reaches the bridge through
their puppet session — `UpdateEditMessage`, read against durable state, in
`bridge/routing/owner_updates.py`.

What the picker offered that native reactions cannot — a keyboard of only the
emoji MAX accepts — is covered by the durable emoji-note: a reaction MAX has no
equivalent for becomes a reply carrying it, on the same queue as every message.

What is left here is `/read`, which is not a reaction and never was.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bridge.presence import AutoRead

from .router import BridgeTarget

logger = logging.getLogger(__name__)


def build_presence_router(
    *,
    auto_read: AutoRead,
    lookup: Callable[[int], BridgeTarget | None],
) -> Router:
    """`lookup` resolves a bot id to the bridge it serves."""
    router = Router(name="presence")
    resolve = lookup

    @router.message(Command("read"))
    async def _read(message: Message) -> None:
        bridge_name = _bridge_name(resolve, message.bot.id if message.bot else 0)
        if bridge_name is None:
            return
        marked = await auto_read.mark_now(bridge_name)
        await message.answer("Отмечено прочитанным." if marked else "Нечего отмечать.")

    return router


def _bridge_name(resolve: Callable[[int], BridgeTarget | None], bot_id: int) -> str | None:
    target = resolve(bot_id)
    return target.name if target is not None else None
