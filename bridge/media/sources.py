"""Turning a MAX attachment into a URL worth downloading.

Only photos carry a usable link in the message itself. Everything else has to be
asked for by opcode, and the answers are not uniform: the same field name means
a player page in one, a direct mp4 in another, and a thumbnail in a third. A
video note's answer has a single `MP4_480`, an ordinary video's has half a dozen
entries including an `EXTERNAL` link to an ok.ru page that is HTML, not video.

So the answer is walked and every URL in it is scored. That is deliberately not
a fixed path into the payload: MAX has already reshaped these answers once, and
a scorer degrades into "picked a slightly worse URL" where a hard-coded path
degrades into "delivered nothing".
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any, Protocol

from bridge.max_client import AttachmentKind, MaxAttachment, enum_text

from .sniff import ContentKind

logger = logging.getLogger(__name__)


class MediaProtocol(Protocol):
    """The MAX calls this module needs. Implemented by `MaxClient`."""

    async def video_sources(self, chat_id: int, message_id: int, video_id: int) -> Any: ...

    async def file_source(self, chat_id: int, message_id: int, file_id: int) -> Any: ...

    async def audio_sources(
        self, chat_id: int, message_id: int, audio_id: int, token: str | None = None
    ) -> Any: ...


#: What we expect the bytes behind each attachment kind to be.
EXPECTED_CONTENT: dict[AttachmentKind, ContentKind] = {
    AttachmentKind.PHOTO: ContentKind.IMAGE,
    AttachmentKind.VIDEO: ContentKind.VIDEO,
    AttachmentKind.VIDEO_NOTE: ContentKind.VIDEO,
    AttachmentKind.VOICE: ContentKind.AUDIO,
    AttachmentKind.MUSIC: ContentKind.AUDIO,
    AttachmentKind.FILE: ContentKind.DOCUMENT,
    AttachmentKind.STICKER: ContentKind.IMAGE,
}

_VIDEO_EXTENSIONS = (".mp4", ".mov", ".m4v", ".webm")
_AUDIO_EXTENSIONS = (".ogg", ".opus", ".mp3", ".m4a", ".aac", ".wav")
_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def _walk(node: Any, key: str | None = None) -> Iterator[tuple[str, str]]:
    """Every (key, url) pair anywhere in a payload."""
    if isinstance(node, str):
        if node.startswith(("http://", "https://")):
            yield (key or ""), node
        return
    if isinstance(node, dict):
        for nested_key, value in node.items():
            yield from _walk(value, str(nested_key))
        return
    if isinstance(node, (list, tuple, set)):
        for value in node:
            yield from _walk(value, key)
        return
    payload = getattr(node, "__dict__", None)
    if payload:
        yield from _walk(payload, key)


def _score_video(key: str, url: str) -> int:
    key_lower, url_lower = key.lower(), url.lower()
    score = 0
    if "mp4" in key_lower:
        score += 6
    if any(res in key_lower for res in ("2160", "1440", "1080", "720", "480", "360", "240")):
        score += 2
    if key_lower in {"url", "src", "source"}:
        score += 4
    # An EXTERNAL entry is a page on ok.ru: HTML, not a file.
    if key_lower == "external" or "ok.ru/video" in url_lower:
        score -= 12
    if any(marker in key_lower for marker in ("thumb", "preview", "poster")):
        score -= 8
    if url_lower.endswith(_VIDEO_EXTENSIONS) or ".mp4?" in url_lower:
        score += 5
    if any(url_lower.endswith(ext) for ext in _IMAGE_EXTENSIONS):
        score -= 8
    # HLS and DASH are playlists: several requests and a remux to be useful.
    if ".m3u8" in url_lower or "dash" in key_lower:
        score -= 3
    return score


def _score_audio(key: str, url: str) -> int:
    key_lower, url_lower = key.lower(), url.lower()
    score = 0
    if any(marker in key_lower for marker in ("audio", "voice", "sound")):
        score += 5
    if key_lower in {"url", "src", "source", "download"}:
        score += 4
    if any(ext in url_lower for ext in _AUDIO_EXTENSIONS):
        score += 6
    if any(marker in key_lower for marker in ("thumb", "preview", "wave", "image")):
        score -= 8
    if any(url_lower.endswith(ext) for ext in _IMAGE_EXTENSIONS):
        score -= 8
    return score


def _score_file(key: str, url: str) -> int:
    key_lower = key.lower()
    score = 0
    if key_lower in {"url", "src", "source", "download", "downloadurl"}:
        score += 5
    if any(marker in key_lower for marker in ("thumb", "preview", "icon")):
        score -= 8
    return score


_SCORERS = {
    ContentKind.VIDEO: _score_video,
    ContentKind.AUDIO: _score_audio,
    ContentKind.DOCUMENT: _score_file,
}


def pick_url(payload: Any, expected: ContentKind) -> str | None:
    """Best URL in a protocol answer for the kind of file we are after."""
    scorer = _SCORERS.get(expected, _score_file)
    best: tuple[int, str] | None = None
    for key, url in _walk(payload):
        score = scorer(key, url)
        if best is None or score > best[0]:
            best = (score, url)
    return best[1] if best else None


def photo_url(attachment: MaxAttachment) -> str | None:
    """Photos are the one kind that needs no protocol round trip."""
    raw = attachment.raw
    for field in ("baseUrl", "base_url", "url", "previewUrl"):
        value = raw.get(field)
        if isinstance(value, str) and value.startswith("http"):
            return value
    return None


def sticker_is_animated(attachment: MaxAttachment) -> bool:
    """Is this one of MAX's Lottie stickers rather than a still?

    MAX's own sticker packs are animated — `stickerType: "LOTTIE"` — and carry
    `lottieUrl` beside the still `url`. Either signal alone is enough.
    """
    raw = attachment.raw
    if enum_text(raw.get("stickerType") or raw.get("sticker_type")) == "LOTTIE":
        return True
    lottie = raw.get("lottieUrl") or raw.get("lottie_url")
    return isinstance(lottie, str) and lottie.startswith("http")


def sticker_url(attachment: MaxAttachment) -> str | None:
    """Where to fetch a sticker from, animation preferred.

    MAX's animated stickers are **already Telegram's format**. `lottieUrl`
    serves gzipped Lottie; compatibility fixtures use the supported 512x512 size,
    60 fps, exactly 3.00 s and 11.5 KB — which is the `.tgs` specification, line
    for line. Telegram accepted such a file unmodified and reported
    `is_animated: true`. So the animation crosses byte for byte, with no
    rendering, no re-encoding and nothing to go wrong in between.

    The still `url` is the fallback, and it is what static stickers use.
    """
    raw = attachment.raw
    for field in ("lottieUrl", "lottie_url"):
        value = raw.get(field)
        if isinstance(value, str) and value.startswith("http"):
            return value
    for field in ("url", "previewUrl", "preview_url"):
        value = raw.get(field)
        if isinstance(value, str) and value.startswith("http"):
            return value
    return None


def _first_int(raw: dict[str, Any], *fields: str) -> int | None:
    for field in fields:
        value = raw.get(field)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.lstrip("-").isdigit():
            return int(value)
    return None


class MaxMediaSources:
    """Resolves the download URL for one attachment."""

    def __init__(self, protocol: MediaProtocol) -> None:
        self._protocol = protocol

    async def resolve(
        self, attachment: MaxAttachment, *, chat_id: int, message_id: int
    ) -> str | None:
        raw = attachment.raw
        expected = EXPECTED_CONTENT.get(attachment.kind, ContentKind.DOCUMENT)

        if attachment.kind is AttachmentKind.PHOTO:
            return photo_url(attachment)

        if attachment.kind is AttachmentKind.STICKER:
            # A sticker carries its own address. Unlike every kind below it
            # there is no id to trade for a link: MAX serves the picture from a
            # public catalogue, and `url` is already that picture. `lottieUrl`
            # sits next to it for the animated ones and is deliberately not used
            # — see `sticker_url` for what it would take.
            return sticker_url(attachment)

        try:
            if attachment.kind in (AttachmentKind.VIDEO, AttachmentKind.VIDEO_NOTE):
                video_id = _first_int(raw, "videoId", "video_id")
                if video_id is None:
                    return None
                answer = await self._protocol.video_sources(chat_id, message_id, video_id)
                return pick_url(answer, ContentKind.VIDEO)

            if attachment.kind is AttachmentKind.VOICE:
                audio_id = _first_int(raw, "audioId", "audio_id")
                if audio_id is None:
                    return None
                token = raw.get("token")
                answer = await self._protocol.audio_sources(
                    chat_id, message_id, audio_id, str(token) if token else None
                )
                url = pick_url(answer, ContentKind.AUDIO)
                if url:
                    return url
                # Voice lives in its own pipeline, but a file id sometimes rides
                # along; trying it costs one call and saves the attachment.
                file_id = _first_int(raw, "fileId", "file_id")
                if file_id is not None:
                    answer = await self._protocol.file_source(chat_id, message_id, file_id)
                    return pick_url(answer, ContentKind.AUDIO)
                return None

            file_id = _first_int(raw, "fileId", "file_id")
            if file_id is None:
                return None
            answer = await self._protocol.file_source(chat_id, message_id, file_id)
            return pick_url(answer, expected)
        except Exception:
            logger.warning(
                "could not resolve a %s attachment in message %s",
                attachment.kind.value,
                message_id,
                exc_info=True,
            )
            return None
