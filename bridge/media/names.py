"""File names: the part of media handling that is a security boundary.

A name arrives from the other side of the world — a MAX attachment or a Telegram
document — and two different things are done with it. It is shown to the user as
the file name of the delivered document, and it is *never* used to build a path.
Everything on disk gets a name this module generates.

That split is the whole point. `../../etc/cron.d/x`, a NUL byte, a 300-character
name, a name that is only dots — none of them can escape a directory if the
directory part is never taken from user input in the first place.
"""

from __future__ import annotations

import mimetypes
import re
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

#: Longest name we hand to Telegram. Long enough for real documents, short
#: enough to stay under filesystem limits once a suffix is added.
MAX_NAME_LENGTH = 120

#: Characters that have meaning to a shell, a path, or a terminal.
_UNSAFE = re.compile(r"[\x00-\x1f\x7f/\\:*?\"<>|]")

#: MAX serves voice as ogg/opus and video as mp4; mimetypes guesses the rest but
#: gets these two wrong often enough to be worth pinning.
_EXTENSION_BY_TYPE = {
    "audio/ogg": ".ogg",
    "audio/opus": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/wav": ".wav",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
}


def sanitize_filename(name: str | None, *, fallback: str = "file") -> str:
    """A display name safe to pass to Telegram — never a path.

    Takes the basename, drops anything that could be a separator or a control
    character, refuses names that are only dots, and trims the length while
    keeping the extension.
    """
    if not name:
        return fallback

    # Windows names arrive with backslashes; treating them as separators too
    # keeps `..\..\windows\cmd.exe` from turning into one long mangled name.
    candidate = PurePosixPath(name.strip().replace("\\", "/")).name
    candidate = _UNSAFE.sub("_", candidate).strip().strip(".")
    if not candidate:
        return fallback

    if len(candidate) <= MAX_NAME_LENGTH:
        return candidate

    suffix = PurePosixPath(candidate).suffix[:16]
    stem = candidate[: MAX_NAME_LENGTH - len(suffix)]
    return f"{stem}{suffix}" if suffix else stem


def extension_for(
    *, content_type: str | None = None, url: str | None = None, default: str = ""
) -> str:
    """Best guess at an extension, from the server's word or the URL."""
    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    if normalized:
        known = _EXTENSION_BY_TYPE.get(normalized)
        if known:
            return known
        guessed = mimetypes.guess_extension(normalized)
        if guessed:
            # mimetypes prefers `.jpe`, which no viewer expects.
            return ".jpg" if guessed == ".jpe" else guessed

    if url:
        suffix = PurePosixPath(unquote(urlparse(url).path)).suffix
        # A "suffix" longer than this is a path fragment, not an extension.
        if 1 < len(suffix) <= 8 and not _UNSAFE.search(suffix):
            return suffix

    return default


def display_name(
    *,
    hint: str | None,
    fallback_stem: str,
    content_type: str | None = None,
    url: str | None = None,
    default_extension: str = "",
) -> str:
    """The name the owner will see, with an extension that matches the bytes."""
    safe = sanitize_filename(hint, fallback=fallback_stem)
    if PurePosixPath(safe).suffix:
        return safe
    suffix = extension_for(content_type=content_type, url=url, default=default_extension)
    return f"{safe}{suffix}" if suffix else safe
