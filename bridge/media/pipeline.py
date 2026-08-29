"""One attachment in, one local file out — or a clear reason why not.

This is the piece inbound and outbound media delivery both stand on. It resolves
where a MAX attachment
actually lives, downloads it under the size ceiling, checks the bytes are what
they claimed to be, and hands the caller a file that disappears when the block
ends.

The failure modes are deliberately distinct, because the bridge says something
different to the owner for each: too large is a limit they can raise, wrong
content is an expired link, and everything else is a transfer that failed.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from bridge.max_client import AttachmentKind, MaxAttachment

from .http import DownloadFailedError, HttpFetcher, WrongContentError
from .names import display_name, extension_for
from .sniff import ContentKind
from .sources import EXPECTED_CONTENT, MaxMediaSources, sticker_is_animated
from .stickers import to_telegram_sticker
from .store import LocalFile, MediaTooLargeError, TempFiles

logger = logging.getLogger(__name__)

#: Stem used when an attachment carries no name of its own. Neutral: a file
#: name is one more place a contact's name could leak into the owner's disk.
FALLBACK_STEM = {
    AttachmentKind.PHOTO: "photo",
    AttachmentKind.VIDEO: "video",
    AttachmentKind.VIDEO_NOTE: "circle",
    AttachmentKind.VOICE: "voice",
    AttachmentKind.MUSIC: "audio",
    AttachmentKind.FILE: "file",
    AttachmentKind.STICKER: "sticker",
}

#: Extension to assume when neither the URL nor the server says anything.
DEFAULT_EXTENSION = {
    AttachmentKind.PHOTO: ".jpg",
    AttachmentKind.VIDEO: ".mp4",
    AttachmentKind.VIDEO_NOTE: ".mp4",
    AttachmentKind.VOICE: ".ogg",
    AttachmentKind.MUSIC: ".mp3",
    # `sendSticker` reads the extension, not the bytes: a WebP named `.bin` is
    # refused. MAX serves WebP, so name it that when the URL stays silent.
    AttachmentKind.STICKER: ".webp",
}


class UnavailableMediaError(Exception):
    """The attachment could not be turned into a file."""


class MediaPipeline:
    def __init__(
        self,
        *,
        sources: MaxMediaSources,
        temp_files: TempFiles,
        fetcher: HttpFetcher | None = None,
        max_file_size_mb: int = 20,
    ) -> None:
        self._sources = sources
        self._temp = temp_files
        self._fetcher = fetcher or HttpFetcher()
        self._limit_bytes = max_file_size_mb * 1024 * 1024

    @property
    def limit_bytes(self) -> int:
        return self._limit_bytes

    @property
    def temp_files(self) -> TempFiles:
        """The reserved-temp-file store, so another transport can borrow it."""
        return self._temp

    @asynccontextmanager
    async def fetch_from_max(
        self, attachment: MaxAttachment, *, chat_id: int, message_id: int
    ) -> AsyncIterator[LocalFile]:
        """Download one MAX attachment. The file is gone when the block exits.

        Size is checked twice — against the attachment's own metadata, which is
        free, and against the bytes as they arrive, which is the one that counts.
        """
        if attachment.size and attachment.size > self._limit_bytes:
            raise MediaTooLargeError(self._limit_bytes, attachment.size)

        url = await self._sources.resolve(attachment, chat_id=chat_id, message_id=message_id)
        if not url:
            raise UnavailableMediaError(
                f"no download URL for a {attachment.kind.value} attachment"
            )

        expected: ContentKind | None = EXPECTED_CONTENT.get(attachment.kind)
        stem = FALLBACK_STEM.get(attachment.kind, "file")
        default_ext = DEFAULT_EXTENSION.get(attachment.kind, "")

        if attachment.kind is AttachmentKind.STICKER and sticker_is_animated(attachment):
            # An animated sticker is gzipped Lottie, not a picture, so the image
            # check would reject the very bytes we want. The name matters as
            # much: `sendSticker` reads the extension, and `.tgs` is what makes
            # Telegram treat the file as an animation rather than refuse it.
            expected = ContentKind.DOCUMENT
            default_ext = ".tgs"

        suffix = extension_for(url=url, default=default_ext)

        with self._temp.reserve(suffix=suffix) as path:
            try:
                size, content_type = await self._fetcher.fetch(
                    url, path, limit_bytes=self._limit_bytes, expected=expected
                )
            except WrongContentError as error:
                # Almost always an expired token: MAX answers 200 with a page.
                raise UnavailableMediaError(
                    f"the link for this {attachment.kind.value} no longer serves it ({error})"
                ) from error
            except DownloadFailedError as error:
                raise UnavailableMediaError(str(error)) from error

            name = display_name(
                hint=attachment.file_name,
                fallback_stem=stem,
                content_type=content_type,
                url=url,
                default_extension=default_ext,
            )

            if attachment.kind is AttachmentKind.STICKER:
                # What MAX serves is not what Bot API takes, and Telegram says
                # so only by silently demoting the result. See the function.
                name = to_telegram_sticker(path, animated=sticker_is_animated(attachment))
                size = path.stat().st_size

            yield LocalFile(
                path=path,
                display_name=name,
                size=size,
                content_type=content_type,
            )

    @asynccontextmanager
    async def fetch_from_url(
        self,
        url: str,
        *,
        expected: ContentKind | None = None,
        file_name: str | None = None,
        fallback_stem: str = "file",
        default_extension: str = "",
    ) -> AsyncIterator[LocalFile]:
        """The Telegram side of the same job: a `getFile` link to a local file."""
        with self._temp.reserve(suffix=extension_for(url=url, default=default_extension)) as path:
            try:
                size, content_type = await self._fetcher.fetch(
                    url, path, limit_bytes=self._limit_bytes, expected=expected
                )
            except (WrongContentError, DownloadFailedError) as error:
                raise UnavailableMediaError(str(error)) from error

            yield LocalFile(
                path=path,
                display_name=display_name(
                    hint=file_name,
                    fallback_stem=fallback_stem,
                    content_type=content_type,
                    url=url,
                    default_extension=default_extension,
                ),
                size=size,
                content_type=content_type,
            )

    def sweep_orphans(self) -> int:
        """Called at start-up: files a crash left behind belong to nobody."""
        return self._temp.sweep()


def local_path_is_inside(path: Path, directory: Path) -> bool:
    """Guard used by tests and callers that accept a path from elsewhere."""
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError:
        return False
    return True
