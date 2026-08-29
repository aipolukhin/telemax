"""Making a bot look like the contact it carries.

One bot per dialog only reads as a conversation if the bot wears the contact's
name and face. Both are available: MAX knows the contact's address-book name and
their avatar URL, and Bot API turns out to allow a bot to change *its own*
profile photo — `setMyProfilePhoto`, verified against the live API — which is
easy to miss, because a bot cannot change anybody else's.

What it cannot change is its @username: that is fixed at creation in @BotFather
and must end in `_bot`. `naming_v2.contact_bot_username_v2` derives it from the
owner's Telegram id and the contact's MAX id — nothing about the person's name or
number is in it, which is why renaming them here renames only what is displayed.

Everything here is best-effort. A bridge that carries messages with the wrong
avatar is working; a bridge that refuses to start because Telegram rate-limited
a name change is not.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from typing import Protocol

from bridge.max_client import MaxContact
from bridge.media import ContentKind, MediaPipeline, UnavailableMediaError

from .avatar import AvatarUnusableError, to_profile_jpeg

logger = logging.getLogger(__name__)

#: Telegram's limit for a bot name.
MAX_NAME_LENGTH = 64

#: Appended to every bridge bot, so a chat list of contact-named bots still says
#: which of them are MAX dialogs rather than people on Telegram.
NAME_SUFFIX = " [Max]"

#: What the bot's "about" line says. No contact name in it: the About text is
#: visible to anyone who opens the bot, and the owner-only filter does not apply
#: to a profile page.
SHORT_DESCRIPTION = "Личный мост MAX ⇄ Telegram."

#: With the guardian named, so every bridge chat carries its own way back to the
#: control panel. Telegram allows 120 characters here and linkifies a bare
#: `t.me/…`, which makes the profile card tappable — the owner asked for exactly
#: this after the `/start` hook turned out never to fire in the managed-creation
#: flow, where Telegram opens the chat itself and no `/start` is ever sent.
SHORT_DESCRIPTION_WITH_GUARDIAN = "Личный мост MAX ⇄ Telegram. Управление: t.me/{guardian}"


def short_description(guardian_username: str | None) -> str:
    """The About line, naming the guardian when there is one to name."""
    name = (guardian_username or "").strip().lstrip("@")
    if not name:
        return SHORT_DESCRIPTION
    text = SHORT_DESCRIPTION_WITH_GUARDIAN.format(guardian=name)
    return text if len(text) <= 120 else SHORT_DESCRIPTION


def signature_of(contact: MaxContact, *, about: str = SHORT_DESCRIPTION) -> str:
    """Fingerprint of what a profile would be set from.

    Telegram rate-limits a name change hard, and a bridge restarts often, so the
    sync is skipped entirely while the contact's name and avatar have not moved.

    `about` is in the material for one reason: without it, changing the About
    text would reach only bots whose contact happened to rename themselves
    afterwards. Every existing bridge would have kept the old line for ever, and
    the change would have looked like it worked because the new ones took it.
    """
    material = f"{contact.display_name or ''}\n{contact.avatar_url or ''}\n{about}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def build_bot_name(name: str) -> str:
    """`Иван Петров [Max]`, trimmed to fit Telegram's 64 characters.

    The suffix is never what gets cut: without it the bot is indistinguishable
    from a Telegram account with the same name.
    """
    stripped = name.strip()
    if not stripped:
        return ""
    room = MAX_NAME_LENGTH - len(NAME_SUFFIX)
    return f"{stripped[:room].strip()}{NAME_SUFFIX}"


class BotProfileWriter(Protocol):
    """The Bot API calls this module needs, one bot at a time."""

    async def set_name(self, bot_id: int, name: str) -> bool: ...

    async def set_short_description(self, bot_id: int, text: str) -> bool: ...

    async def set_profile_photo(self, bot_id: int, photo: bytes, file_name: str) -> bool: ...


class ContactSource(Protocol):
    async def contact_profile(self, user_id: int) -> MaxContact | None: ...


class BotProfileSync:
    """Copies a MAX contact's name and avatar onto their bot."""

    def __init__(
        self,
        *,
        writer: BotProfileWriter,
        contacts: ContactSource,
        pipeline: MediaPipeline | None = None,
        guardian_username: Callable[[], str | None] | None = None,
    ) -> None:
        self._writer = writer
        self._contacts = contacts
        self._pipeline = pipeline
        # Asked on each sync, not captured: the guardian's `getMe` answers after
        # this object is built.
        self._guardian_username = guardian_username

    def about(self) -> str:
        """The About line this sync would write. Also what the signature covers."""
        if self._guardian_username is None:
            return SHORT_DESCRIPTION
        try:
            return short_description(self._guardian_username())
        except Exception:
            logger.debug("could not resolve the guardian username", exc_info=True)
            return SHORT_DESCRIPTION

    async def apply(
        self, *, bot_id: int, max_user_id: int, known_signature: str | None = None
    ) -> MaxContact | None:
        """Dress the bot up as the contact. Returns what MAX said about them."""
        contact = await self._contacts.contact_profile(max_user_id)
        if contact is None:
            logger.debug("no MAX profile for user %s; leaving the bot as it is", max_user_id)
            return None

        about = self.about()
        if known_signature is not None and signature_of(contact, about=about) == known_signature:
            logger.debug("profile of bot %s is already up to date", bot_id)
            return contact

        if contact.display_name:
            await self._apply_name(bot_id, contact.display_name)
        await self._writer.set_short_description(bot_id, about)
        if contact.avatar_url:
            await self._apply_photo(bot_id, contact.avatar_url)
        return contact

    async def _apply_name(self, bot_id: int, name: str) -> None:
        trimmed = build_bot_name(name)
        if not trimmed:
            return
        if not await self._writer.set_name(bot_id, trimmed):
            # Telegram rate-limits name changes hard; the next start tries again.
            logger.info("could not set the bot name for bot %s right now", bot_id)

    async def _apply_photo(self, bot_id: int, url: str) -> None:
        if self._pipeline is None:
            return
        try:
            async with self._pipeline.fetch_from_url(
                url,
                expected=ContentKind.IMAGE,
                fallback_stem="avatar",
                default_extension=".jpg",
            ) as local:
                # MAX serves WebP; Telegram takes JPEG only and blames the size
                # when it gets anything else (see `avatar`).
                jpeg = to_profile_jpeg(local.path.read_bytes())
                await self._writer.set_profile_photo(bot_id, jpeg, "avatar.jpg")
        except UnavailableMediaError:
            logger.info("avatar for bot %s could not be fetched", bot_id)
        except AvatarUnusableError as error:
            logger.info("avatar for bot %s is not usable: %s", bot_id, error)
        except Exception:
            logger.debug("setting the avatar for bot %s failed", bot_id, exc_info=True)
