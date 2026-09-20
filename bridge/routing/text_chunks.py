"""Lossless text splitting at the Telegram→MAX boundary.

MAX rejects a private message whose body is longer than 4000 characters.  The
owner-side Telegram transport may hand us more than that (and Premium clients
do), so the bridge must make the representation smaller before the first MAX
request rather than retrying a payload the server has already proved invalid.

The limit is counted in UTF-16 code units.  That is the conservative unit for a
protocol whose entity offsets and native clients use UTF-16, and it prevents an
emoji-heavy chunk from looking short in Python while being long on the wire.
"""

from __future__ import annotations

MAX_TEXT_UTF16_LIMIT = 4_000


def utf16_units(text: str) -> int:
    """How many UTF-16 code units ``text`` occupies, without a BOM."""
    return len(text.encode("utf-16-le")) // 2


def split_max_text(text: str, *, limit: int = MAX_TEXT_UTF16_LIMIT) -> list[str]:
    """Split ``text`` losslessly into MAX-sized pieces.

    Prefer paragraph, line and whitespace boundaries in that order, but only in
    the latter half of the available window: a short early paragraph must not
    turn one long message into a shower of tiny bubbles.  If there is no useful
    boundary, split at the last complete Unicode scalar that fits.

    Joining the result always reproduces the input byte-for-byte at the Python
    string level; separators are kept on the preceding part and nothing is
    stripped or invented.
    """
    if limit < 2:
        raise ValueError("the MAX text limit must fit one supplementary character")
    if not text or utf16_units(text) <= limit:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        units = 0
        end = start
        while end < len(text):
            width = 2 if ord(text[end]) > 0xFFFF else 1
            if units + width > limit:
                break
            units += width
            end += 1

        if end == len(text):
            chunks.append(text[start:end])
            break
        if end == start:  # Defensive: ``limit >= 2`` makes this unreachable.
            raise ValueError("the MAX text limit cannot fit the next character")

        window = text[start:end]
        floor = len(window) // 2
        split_at = _preferred_break(window, floor=floor)
        if split_at <= 0:
            split_at = len(window)
        chunks.append(window[:split_at])
        start += split_at

    return chunks


def _preferred_break(window: str, *, floor: int) -> int:
    """Return a separator-inclusive split point, or zero for a hard split."""
    paragraph = window.rfind("\n\n", floor)
    if paragraph >= 0:
        return paragraph + 2
    line = window.rfind("\n", floor)
    if line >= 0:
        return line + 1
    for index in range(len(window) - 1, floor - 1, -1):
        if window[index].isspace():
            return index + 1
    return 0


def text_part_source_key(base: str, *, index: int, total: int) -> str:
    """Stable identity for one piece of one Telegram message."""
    if total == 1:
        return base
    return f"{base}:text-part:{index + 1}:{total}"


__all__ = [
    "MAX_TEXT_UTF16_LIMIT",
    "split_max_text",
    "text_part_source_key",
    "utf16_units",
]
