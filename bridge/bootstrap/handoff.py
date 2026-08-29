"""Getting the owner from a terminal into a chat, exactly once.

A Telegram bot cannot open a conversation. If the owner has never pressed Start
on the guardian, no method exists that will put a message in front of them — and
there is no method that reports whether they have, either. The send *is* the
test, so this tries in order:

1. **the bot writes.** Works when a chat already exists, which is the case on a
   re-run, and needs no link at all.
2. **the owner's own account writes to Saved Messages.** Always available: the
   user session opened a moment ago is the owner, and everybody has a chat with
   themselves. The message carries the one-time deep link.
3. **the console prints the link.** Last resort, and honest about being one.

The token that link carries is generated here and only its hash is written down.
It never appears in a log line: `logger.info("link sent")` is all any of this
says out loud.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from bridge.onboarding import screens, tokens
from bridge.onboarding.state import Stage, StateStore

logger = logging.getLogger(__name__)


class Delivery(StrEnum):
    BOT = "bot"
    SAVED_MESSAGES = "saved_messages"
    CONSOLE = "console"


@dataclass(frozen=True, slots=True)
class Handoff:
    delivery: Delivery
    link: str
    bot_username: str


def invitation(bot_username: str, link: str) -> str:
    return (
        f"Бот-страж Telemax создан: @{bot_username}\n\n"
        "Нажмите ссылку, чтобы продолжить настройку:\n"
        f"{link}"
    )


async def hand_off(
    *,
    store: StateStore,
    session: Any,
    bot: Any,
    bot_username: str,
    owner_user_id: int,
    ttl_seconds: int = tokens.DEFAULT_TTL_SECONDS,
) -> Handoff:
    """Issue the link, deliver it the best way available, record the hash."""
    issued = tokens.issue(ttl_seconds=ttl_seconds)
    store.update(
        stage=Stage.MAX_ONBOARDING_PENDING,
        owner_user_id=owner_user_id,
        guardian_username=bot_username,
        token_digest=issued.digest,
        token_expires_at=issued.expires_at,
        token_used_at=None,
    )
    link = tokens.deep_link(bot_username, issued.plaintext)

    if bot is not None and await _bot_invites(bot, owner_user_id):
        logger.info("the guardian opened the conversation itself")
        return Handoff(delivery=Delivery.BOT, link=link, bot_username=bot_username)

    if session is not None and await session.send_to_saved(invitation(bot_username, link)):
        logger.info("setup link delivered to Saved Messages")
        return Handoff(
            delivery=Delivery.SAVED_MESSAGES, link=link, bot_username=bot_username
        )

    logger.warning("could not deliver the setup link; falling back to the console")
    return Handoff(delivery=Delivery.CONSOLE, link=link, bot_username=bot_username)


async def _bot_invites(bot: Any, owner_user_id: int) -> bool:
    text, markup = screens.handoff_invite()
    try:
        await bot.send_message(chat_id=owner_user_id, text=text, reply_markup=markup)
    except Exception:  # noqa: BLE001 - "chat not found" is the expected answer
        return False
    return True
