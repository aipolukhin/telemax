"""Plan and upload media sent from Telegram into MAX.

The module preserves native voice messages, video notes and albums when the
upstream APIs support them. Telegram's Bot API download limit is checked before
fetching a file, and a media group is collected briefly so one Telegram album
stays one MAX message.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: How long to wait for the rest of an album after its first item. Telegram
#: sends the parts back to back; this is generous enough for a slow connection
#: and short enough not to feel like a delay.
ALBUM_WINDOW_SECONDS = 1.8

#: Bot API refuses to serve a bot any file bigger than this.
BOT_API_DOWNLOAD_LIMIT = 20 * 1024 * 1024

TOO_LARGE_NOTICE = "Telegram не отдаёт боту файлы больше 20 МБ — это не ушло в MAX."


class UploadKind(StrEnum):
    """What MAX will be asked to store it as."""

    PHOTO = "photo"
    VIDEO = "video"
    FILE = "file"
    #: Not a file attach: MAX uploads the image, creates a sticker and then
    #: references it by id. The MAX client owns that chain.
    STICKER = "sticker"
    #: Voice messages and video notes use the native media path owned by
    #: `MaxClient.send_media`.
    VOICE = "voice"
    CIRCLE = "circle"


@dataclass(frozen=True, slots=True)
class PendingUpload:
    """One Telegram attachment on its way to MAX."""

    kind: UploadKind
    file_id: str
    file_name: str
    size: int | None = None
    #: Set when the original was something MAX cannot store natively.
    degraded_notice: str | None = None


@dataclass(frozen=True, slots=True)
class ReadyUpload:
    """A downloaded file, ready for `MaxClient.send_media`."""

    kind: UploadKind
    path: Path
    name: str


def plan_upload(
    *,
    photo: bool = False,
    video: bool = False,
    voice: bool = False,
    video_note: bool = False,
    audio: bool = False,
    document: bool = False,
    sticker: bool = False,
    file_id: str,
    file_name: str,
    size: int | None = None,
) -> PendingUpload:
    """Decide what MAX will be asked to store, and what the owner is told.

    Unsupported inputs degrade to a generic file instead of being dropped.
    """
    if sticker:
        # No degraded notice, even for an animated one. A sticker that came from
        # MAX returns as itself, animation and all (the round trip in
        # `MaxClient`), so a blanket "ушёл кадром" would be false in exactly the
        # case worth caring about — and for a genuinely lossy conversion a still
        # image is what a sticker already looks like.
        return PendingUpload(UploadKind.STICKER, file_id, file_name, size)
    if photo:
        return PendingUpload(UploadKind.PHOTO, file_id, file_name, size)
    if video:
        return PendingUpload(UploadKind.VIDEO, file_id, file_name, size)
    if video_note:
        return PendingUpload(UploadKind.CIRCLE, file_id, file_name, size)
    if voice:
        return PendingUpload(UploadKind.VOICE, file_id, file_name, size)
    if audio or document:
        return PendingUpload(UploadKind.FILE, file_id, file_name, size)
    return PendingUpload(UploadKind.FILE, file_id, file_name, size)


def too_large(item: PendingUpload) -> bool:
    return bool(item.size and item.size > BOT_API_DOWNLOAD_LIMIT)


@dataclass
class Album:
    """One media group, and everything needed to answer in the right chat.

    The context travels with the group rather than living on the collector: an
    album is flushed by a timer, and by then another bridge may well have sent
    something of its own.
    """

    items: list[PendingUpload] = field(default_factory=list)
    caption: str = ""
    #: `↪ Переслано от …`, when the owner forwarded the album rather than
    #: sending it. Held apart from the caption because the two are filled by
    #: different parts: Telegram puts the caption on whichever part the owner
    #: typed it on, while the origin is on every part and is wanted exactly
    #: once. Merging them here would let a header-only first part count as "the
    #: album already has a caption" and swallow the real one.
    forward_line: str = ""
    reply_to: int | None = None
    first_message_id: int = 0
    context: Any = None
    task: asyncio.Task[None] | None = None


class AlbumStore(Protocol):
    """Where album parts live between the first one and the pause that ends it."""

    async def add_part(
        self,
        *,
        media_group_id: str,
        bridge_name: str,
        bot_id: int,
        telegram_message_id: int,
        payload: dict[str, Any],
    ) -> bool: ...

    async def parts(self, media_group_id: str) -> list[Any]: ...

    async def open_groups(self, *, older_than_ms: int = 0) -> list[str]: ...

    async def clear(self, media_group_id: str) -> None: ...


class AlbumCollector:
    """Gathers a Telegram media group into one MAX message.

    Telegram sends an album as separate updates sharing a `media_group_id`, with
    no marker on the last one — so the only way to know the group has ended is
    that nothing else arrived for a moment.

    That pause is why the parts have to be on disk. The updates are acknowledged
    to Telegram as they arrive, and the process can die in the gap between photo
    two and photo three; with the group living only in this dict, the two already
    accepted photos went with it and Telegram would never send them again. The
    timer stays in memory — it is only a guess about when the album ended, and a
    restart can make that guess again — but what it is waiting for does not.
    """

    def __init__(
        self,
        *,
        flush: Callable[[str, Album], Awaitable[None]],
        window_seconds: float = ALBUM_WINDOW_SECONDS,
        store: AlbumStore | None = None,
    ) -> None:
        self._flush = flush
        self._window = window_seconds
        self._groups: dict[str, Album] = {}
        self._store = store

    async def add(
        self,
        group_id: str,
        item: PendingUpload,
        *,
        caption: str,
        reply_to: int | None,
        message_id: int,
        context: Any = None,
        bridge_name: str = "",
        bot_id: int = 0,
        forward_line: str = "",
    ) -> None:
        if self._store is not None:
            fresh = await self._store.add_part(
                media_group_id=group_id,
                bridge_name=bridge_name,
                bot_id=bot_id,
                telegram_message_id=message_id,
                payload={
                    "kind": item.kind.value,
                    "file_id": item.file_id,
                    "file_name": item.file_name,
                    "size": item.size,
                    "degraded_notice": item.degraded_notice,
                    "caption": caption,
                    "forward_line": forward_line,
                    "reply_to": reply_to,
                },
            )
            if not fresh:
                # This exact part is already recorded: a replayed update after a
                # restart. Adding it again would put the photo in twice.
                return

        album = self._groups.get(group_id)
        if album is None:
            album = Album(
                caption=caption,
                forward_line=forward_line,
                reply_to=reply_to,
                first_message_id=message_id,
                context=context,
            )
            self._groups[group_id] = album
        album.items.append(item)
        if caption and not album.caption:
            # Telegram puts the caption on whichever part the user typed it on.
            album.caption = caption
        if forward_line and not album.forward_line:
            album.forward_line = forward_line

        if album.task is not None:
            album.task.cancel()
        album.task = asyncio.create_task(self._wait_and_flush(group_id))

    async def _wait_and_flush(self, group_id: str) -> None:
        try:
            await asyncio.sleep(self._window)
        except asyncio.CancelledError:
            # Another part of the same album arrived; that part re-armed us.
            return

        album = self._groups.pop(group_id, None)
        if album is None or not album.items:
            return
        try:
            await self._flush(group_id, album)
        except Exception:
            logger.exception("could not deliver album %s to MAX", group_id)
            return
        await self._forget(group_id)

    async def _forget(self, group_id: str) -> None:
        """Drop the stored parts once the album is on its way."""
        if self._store is None:
            return
        try:
            await self._store.clear(group_id)
        except Exception:
            logger.exception("could not clear stored album %s", group_id)

    async def restore(self, *, settle_ms: int = 0) -> list[str]:
        """Rebuild albums left on disk by a process that did not finish.

        Returns the group ids that came back. Only groups whose last part is
        older than `settle_ms` are taken, so a restart does not snatch a group
        that is still arriving.
        """
        if self._store is None:
            return []
        restored: list[str] = []
        for group_id in await self._store.open_groups(older_than_ms=settle_ms):
            if group_id in self._groups:
                continue
            parts = await self._store.parts(group_id)
            if not parts:
                continue
            album = Album()
            for part in parts:
                payload = json.loads(part.payload_json)
                album.items.append(
                    PendingUpload(
                        kind=UploadKind(payload["kind"]),
                        file_id=payload["file_id"],
                        file_name=payload["file_name"],
                        size=payload.get("size"),
                        degraded_notice=payload.get("degraded_notice"),
                    )
                )
                if payload.get("caption") and not album.caption:
                    album.caption = payload["caption"]
                if payload.get("forward_line") and not album.forward_line:
                    album.forward_line = payload["forward_line"]
                if album.reply_to is None:
                    album.reply_to = payload.get("reply_to")
            # Every part this collector wrote has a Telegram id; the column is
            # nullable only for the aliases the other two transports keep in the
            # same table, and `open_groups` never hands those over.
            album.first_message_id = parts[0].telegram_message_id or 0
            self._groups[group_id] = album
            restored.append(group_id)
        return restored

    async def rearm(self, group_id: str) -> None:
        """Start the flush timer for a group that came back from disk."""
        album = self._groups.get(group_id)
        if album is None:
            return
        if album.task is not None:
            album.task.cancel()
        album.task = asyncio.create_task(self._wait_and_flush(group_id))

    async def drain(self) -> None:
        """Flush everything still waiting — used on shutdown."""
        for group_id in list(self._groups):
            album = self._groups.pop(group_id, None)
            if album is None:
                continue
            if album.task is not None:
                album.task.cancel()
            if album.items:
                await self._flush(group_id, album)
                await self._forget(group_id)
