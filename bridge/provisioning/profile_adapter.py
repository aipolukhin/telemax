"""`BotProfileWriter` over the live bot registry."""

from __future__ import annotations

import logging

from aiogram.methods import SetMyProfilePhoto
from aiogram.types import BufferedInputFile, InputProfilePhotoStatic

from bridge.telegram import BotRegistry

logger = logging.getLogger(__name__)


class TelegramProfileAdapter:
    def __init__(self, registry: BotRegistry) -> None:
        self._registry = registry

    async def set_name(self, bot_id: int, name: str) -> bool:
        live = self._registry.by_bot_id(bot_id)
        if live is None:
            return False
        try:
            await live.bot.set_my_name(name=name)
        except Exception:
            # Telegram refuses a name change that is too frequent, and an
            # unchanged name is an error too. Neither is worth a stack trace.
            logger.debug("set_my_name failed for bot %s", bot_id, exc_info=True)
            return False
        return True

    async def set_short_description(self, bot_id: int, text: str) -> bool:
        live = self._registry.by_bot_id(bot_id)
        if live is None:
            return False
        try:
            await live.bot.set_my_short_description(short_description=text)
        except Exception:
            logger.debug("set_my_short_description failed for bot %s", bot_id, exc_info=True)
            return False
        return True

    async def set_profile_photo(self, bot_id: int, photo: bytes, file_name: str) -> bool:
        """A bot may change its own avatar — and only its own.

        `setMyProfilePhoto` is easy to overlook because every other photo method
        in Bot API is about somebody else's chat. Static photos must be JPEG and
        cannot be reused by file_id, so the bytes are uploaded every time.
        """
        live = self._registry.by_bot_id(bot_id)
        if live is None:
            return False
        try:
            await live.bot(
                SetMyProfilePhoto(
                    photo=InputProfilePhotoStatic(
                        photo=BufferedInputFile(photo, filename=file_name)
                    )
                )
            )
        except Exception:
            logger.debug("setMyProfilePhoto failed for bot %s", bot_id, exc_info=True)
            return False
        return True
