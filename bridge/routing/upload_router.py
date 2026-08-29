"""Media the owner sends from Telegram on its way to MAX.

aiogram hands each kind of attachment in its own field, so the first job is to
recognise what arrived and decide what MAX will be asked to store it as.

A voice message and a video note go to MAX as native media when supported.
`MaxClient.send_media` owns that decision and its safe fallback.

An album arrives as several updates sharing a `media_group_id`, with nothing
marking the last one, so the parts are collected until a short silence and sent
to MAX as a single message.
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from datetime import tzinfo
from pathlib import Path
from typing import Any

from aiogram import F, Router
from aiogram.types import Message

from bridge.config import TimestampStyle
from bridge.formatting import entities_to_markdown
from bridge.media import MediaPipeline, UnavailableMediaError
from bridge.media.upload import (
    TOO_LARGE_NOTICE,
    Album,
    AlbumCollector,
    PendingUpload,
    plan_upload,
    too_large,
)
from bridge.presence import AutoRead

from .adapters import (
    OwnerIntakeSuppressed,
    OwnerMessageSeen,
    forward_line_of,
    note_owner_intake_suppressed,
)
from .echo import OwnEchoes, media_key
from .router import BridgeRouter

logger = logging.getLogger(__name__)

UPLOAD_FAILED_NOTICE = "Не смог передать вложение в MAX."


def describe(message: Message) -> PendingUpload | None:
    """What this Telegram message carries, in MAX's terms."""
    if message.photo:
        # The last size is the largest Telegram kept.
        largest = message.photo[-1]
        return plan_upload(
            photo=True,
            file_id=largest.file_id,
            file_name="photo.jpg",
            size=largest.file_size,
        )
    if message.video:
        return plan_upload(
            video=True,
            file_id=message.video.file_id,
            file_name=message.video.file_name or "video.mp4",
            size=message.video.file_size,
        )
    if message.video_note:
        return plan_upload(
            video_note=True,
            file_id=message.video_note.file_id,
            file_name="circle.mp4",
            size=message.video_note.file_size,
        )
    if message.voice:
        return plan_upload(
            voice=True,
            file_id=message.voice.file_id,
            file_name="voice.ogg",
            size=message.voice.file_size,
        )
    if message.audio:
        return plan_upload(
            audio=True,
            file_id=message.audio.file_id,
            file_name=message.audio.file_name or "audio.mp3",
            size=message.audio.file_size,
        )
    if message.document:
        return plan_upload(
            document=True,
            file_id=message.document.file_id,
            file_name=message.document.file_name or "file.bin",
            size=message.document.file_size,
        )
    if message.animation:
        return plan_upload(
            video=True,
            file_id=message.animation.file_id,
            file_name=message.animation.file_name or "animation.mp4",
            size=message.animation.file_size,
        )
    if message.sticker:
        # The name carries the real format so the converter's magic-byte check
        # has something sane to fall back on: `.tgs` for animated Lottie,
        # `.webm` for video stickers, `.webp` for the ordinary ones.
        suffix = (
            ".tgs" if message.sticker.is_animated
            else ".webm" if message.sticker.is_video
            else ".webp"
        )
        return plan_upload(
            sticker=True,
            file_id=message.sticker.file_id,
            file_name=f"sticker{suffix}",
            size=message.sticker.file_size,
        )
    return None


class MediaUploader:
    """Downloads from Telegram and hands the files to routing."""

    def __init__(
        self,
        *,
        bridge_router: BridgeRouter,
        pipeline: MediaPipeline,
        albums: Any = None,
        timestamp_style: TimestampStyle = TimestampStyle.COMPACT,
        timezone: tzinfo | None = None,
    ) -> None:
        self._router = bridge_router
        self._pipeline = pipeline
        # With a store the parts of an album survive a restart between them.
        self._albums = AlbumCollector(flush=self._flush_album, store=albums)
        # Only the forward line reads these: it carries the original message's
        # own time, exactly as a forward coming the other way does.
        self._timestamp_style = timestamp_style
        self._timezone = timezone

    @property
    def bridge(self) -> BridgeRouter:
        return self._router

    async def restore_pending(self) -> int:
        """Take back albums a previous process accepted and never sent.

        The parts are re-downloaded from Telegram by `file_id` when they go out,
        so nothing depends on temp files that a cleanup may have removed.
        """
        groups = await self._albums.restore()
        for group_id in groups:
            await self._albums.rearm(group_id)
        return len(groups)

    async def flush_pending(self) -> None:
        """Send albums still waiting for their next part.

        A media group is only known to be complete once nothing else has
        arrived for a moment, so a shutdown in that moment would otherwise eat
        photos the owner already watched leave Telegram.
        """
        await self._albums.drain()

    async def handle(self, message: Message, item: PendingUpload) -> None:
        caption = entities_to_markdown(message.caption or "", message.caption_entities)
        # A forwarded photo carries its origin the same way a forwarded line of
        # text does. It is kept out of the caption for an album, because the two
        # arrive on different parts — see `Album.forward_line`.
        forward_line = forward_line_of(
            message, timestamp_style=self._timestamp_style, timezone=self._timezone
        )
        replied = message.reply_to_message

        if message.media_group_id:
            target = self._router.target_for_bot(message.bot.id) if message.bot else None
            await self._albums.add(
                message.media_group_id,
                item,
                caption=caption,
                reply_to=replied.message_id if replied else None,
                message_id=message.message_id,
                context=(message.bot, message.chat.id),
                bridge_name=target.name if target else "",
                bot_id=message.bot.id if message.bot else 0,
                forward_line=forward_line,
            )
            return

        await self._deliver(
            [item],
            bot=message.bot,
            chat_id=message.chat.id,
            message_id=message.message_id,
            caption=f"{forward_line}{caption}",
            reply_to=replied.message_id if replied else None,
            notify=message.answer,
        )

    async def _flush_album(self, group_id: str, album: Album) -> None:
        if not album.context:
            logger.debug("album %s has no chat to answer in", group_id)
            return
        bot, chat_id = album.context
        await self._deliver(
            album.items,
            bot=bot,
            chat_id=chat_id,
            message_id=album.first_message_id,
            caption=f"{album.forward_line}{album.caption}",
            reply_to=album.reply_to,
            notify=None,
        )

    async def _deliver(
        self,
        items: list[PendingUpload],
        *,
        bot: Any,
        chat_id: int,
        message_id: int,
        caption: str,
        reply_to: int | None,
        notify: Any,
    ) -> None:
        async def say(text: str) -> None:
            if notify is not None:
                await notify(text)
            else:
                await bot.send_message(chat_id=chat_id, text=text)

        usable = [item for item in items if not too_large(item)]
        if len(usable) != len(items):
            await say(TOO_LARGE_NOTICE)
        if not usable:
            return

        notices = [item.degraded_notice for item in usable if item.degraded_notice]
        for notice in dict.fromkeys(notices):
            await say(notice)

        async with AsyncExitStack() as stack:
            ready: list[tuple[str, Path, str]] = []
            # What a retry needs. The temp files below are gone the moment this
            # block exits, so a job that remembered only their paths could never
            # be retried — it would fail for ever on files that no longer exist.
            # A Telegram file_id stays valid, so the worker re-fetches from it.
            sources: list[tuple[str, str, str]] = []
            for item in usable:
                local = await self._download(stack, bot, item)
                if local is None:
                    await say(UPLOAD_FAILED_NOTICE)
                    continue
                ready.append((item.kind.value, local, item.file_name))
                sources.append((item.kind.value, item.file_id, item.file_name))

            if not ready:
                return

            try:
                await self._router.on_telegram_media(
                    sources=sources,
                    bot_id=bot.id,
                    telegram_chat_id=chat_id,
                    telegram_message_id=message_id,
                    items=ready,
                    caption=caption,
                    reply_to_telegram_message_id=reply_to,
                )
            except Exception:
                logger.exception("upload to MAX failed")
                await say(UPLOAD_FAILED_NOTICE)

    async def _download(
        self, stack: AsyncExitStack, bot: Any, item: PendingUpload
    ) -> Path | None:
        try:
            file = await bot.get_file(item.file_id)
        except Exception:
            logger.debug("getFile failed for %s", item.file_name, exc_info=True)
            return None

        url = f"https://api.telegram.org/file/bot{bot.token}/{file.file_path}"
        try:
            local = await stack.enter_async_context(
                self._pipeline.fetch_from_url(url, file_name=item.file_name)
            )
        except UnavailableMediaError:
            return None
        return local.path


def unique_ids_of(message: Message) -> list[str]:
    """Every `file_unique_id` an incoming attachment message carries."""
    found: list[str] = []
    for size in message.photo or []:
        if size.file_unique_id:
            found.append(str(size.file_unique_id))
    for item in (
        message.video,
        message.video_note,
        message.voice,
        message.audio,
        message.document,
        message.animation,
        message.sticker,
    ):
        unique = getattr(item, "file_unique_id", None)
        if unique:
            found.append(str(unique))
    return found


def build_upload_router(
    uploader: MediaUploader,
    *,
    on_owner_message: AutoRead | None = None,
    own_echoes: OwnEchoes | None = None,
    on_owner_intake_suppressed: OwnerIntakeSuppressed | None = None,
    on_owner_message_seen: OwnerMessageSeen | None = None,
) -> Router:
    """Handlers for every attachment kind, ahead of the plain-text forwarder.

    Like the forwarding router, this one sees the owner's attachments and carries
    none of them: the puppet session is the only authority on what the owner
    sent. What it still does is recognise an attachment the bridge itself placed
    on the owner's behalf, so their own photo does not come back at them twice.
    """
    router = Router(name="uploads")

    # The filter is not a nicety: aiogram stops after the first handler that
    # runs, so a bare `@router.message()` here would swallow plain text and it
    # would never reach the forwarding router at all.
    @router.message(
        F.photo
        | F.video
        | F.video_note
        | F.voice
        | F.audio
        | F.document
        | F.animation
        | F.sticker
    )
    async def _media(message: Message) -> None:
        item = describe(message)
        if item is None:
            return

        bot_id = message.bot.id if message.bot else 0
        claim = None
        if own_echoes is not None:
            for unique_id in unique_ids_of(message):
                claim = own_echoes.claim(bot_id, media_key(unique_id))
                if claim is not None:
                    break
        if claim is not None:
            # An attachment the bridge itself placed on the owner's behalf. MAX
            # already holds it — the owner sent it there — and uploading it back
            # would put a second copy in the dialog. The copy also carries the
            # bot's own id for it, which is what a reply needs to resolve.
            if claim.placed is not None:
                await uploader.bridge.note_own_placement(
                    bot_id=bot_id,
                    telegram_message_id=message.message_id,
                    placed=claim.placed,
                )
            return

        # Not an echo, so it is the owner sending: their session carries it,
        # by reference, and re-fetches it on every retry. The bot's own id for
        # it is taken, and nothing else — see `routing/owner_binding.py`.
        if on_owner_message_seen is not None and message.media_group_id is None:
            # An album is N Telegram messages for one MAX message, so "the bot's
            # id for it" is not one number. Left alone rather than bound to
            # whichever part arrived first.
            await on_owner_message_seen(bot_id, message.message_id)
        await note_owner_intake_suppressed(on_owner_intake_suppressed, bot_id)

    return router
