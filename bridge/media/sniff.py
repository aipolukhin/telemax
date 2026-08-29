"""What the bytes actually are, regardless of what anyone claimed.

Two reasons this exists instead of trusting `Content-Type` or the extension.

**A wrong type is a broken message.** Telegram rejects a `sendVoice` whose body
is a JPEG, and `sendPhoto` with an HTML page in it produces a file the owner
cannot open. Checking the first bytes costs nothing and turns both into a clean
fallback.

**An HTML page is the usual failure mode of a MAX media URL.** An expired token
or a player page answers 200 with HTML, so "downloaded successfully" is not the
same as "downloaded the file". Detecting that is what stops the bridge from
delivering a 4 KB web page as somebody's voice message.
"""

from __future__ import annotations

from enum import StrEnum


class ContentKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"
    HTML = "html"
    TEXT = "text"
    UNKNOWN = "unknown"


#: Enough bytes for every signature below, including the ftyp box at offset 4.
SNIFF_SIZE = 512


def detect_kind(head: bytes) -> ContentKind:
    """Classify by magic bytes. `head` is the start of the file."""
    if not head:
        return ContentKind.UNKNOWN

    if head.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a")):
        return ContentKind.IMAGE
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return ContentKind.IMAGE

    if head[4:8] == b"ftyp":
        # ISO-BMFF covers both mp4 video and m4a audio; the brand tells them apart.
        brand = head[8:12]
        return ContentKind.AUDIO if brand in (b"M4A ", b"M4B ") else ContentKind.VIDEO
    if head.startswith(b"\x1a\x45\xdf\xa3"):  # Matroska / WebM
        return ContentKind.VIDEO

    if head.startswith((b"OggS", b"ID3", b"fLaC")):
        return ContentKind.AUDIO
    if head.startswith(b"\xff\xfb") or head.startswith(b"\xff\xf3"):  # bare MP3 frame
        return ContentKind.AUDIO
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return ContentKind.AUDIO

    if head.startswith((b"%PDF", b"PK\x03\x04", b"\xd0\xcf\x11\xe0", b"Rar!", b"\x1f\x8b")):
        return ContentKind.DOCUMENT

    stripped = head.lstrip().lower()
    if stripped.startswith((b"<!doctype html", b"<html", b"<head", b"<body", b"<?xml")):
        return ContentKind.HTML
    if _looks_like_text(head):
        return ContentKind.TEXT
    return ContentKind.UNKNOWN


def _looks_like_text(head: bytes) -> bool:
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def classify(content_type: str | None, head: bytes) -> ContentKind:
    """Magic bytes first, the server's claim only as a tie-breaker."""
    detected = detect_kind(head)
    if detected is not ContentKind.UNKNOWN:
        return detected

    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    if normalized.startswith("image/"):
        return ContentKind.IMAGE
    if normalized.startswith("video/"):
        return ContentKind.VIDEO
    if normalized.startswith("audio/"):
        return ContentKind.AUDIO
    if normalized.startswith("text/html"):
        return ContentKind.HTML
    if normalized.startswith("text/"):
        return ContentKind.TEXT
    if normalized.startswith("application/"):
        return ContentKind.DOCUMENT
    return ContentKind.UNKNOWN


#: What each expected kind is allowed to turn out to be. A document may be
#: anything — that is what "document" means — but a photo that arrives as a
#: video is a resolution mistake worth catching.
_ALLOWED: dict[ContentKind, frozenset[ContentKind]] = {
    ContentKind.IMAGE: frozenset({ContentKind.IMAGE, ContentKind.UNKNOWN}),
    ContentKind.VIDEO: frozenset({ContentKind.VIDEO, ContentKind.UNKNOWN}),
    ContentKind.AUDIO: frozenset({ContentKind.AUDIO, ContentKind.UNKNOWN}),
    ContentKind.DOCUMENT: frozenset(
        {
            ContentKind.DOCUMENT,
            ContentKind.IMAGE,
            ContentKind.VIDEO,
            ContentKind.AUDIO,
            ContentKind.TEXT,
            ContentKind.UNKNOWN,
        }
    ),
}


def is_acceptable(expected: ContentKind | None, detected: ContentKind) -> bool:
    """Is this download usable as `expected`?

    HTML is never acceptable: it is what MAX serves when a token has expired or
    the URL was a player page rather than a media file.
    """
    if detected is ContentKind.HTML:
        return False
    if expected is None:
        return detected is not ContentKind.TEXT
    return detected in _ALLOWED.get(expected, frozenset({detected}))
