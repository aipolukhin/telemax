"""Taking the bridge's own decorations back off a rendered message.

Every other module here draws: a stamp in front of a backfilled line, a forward
header above somebody else's words, an `(изм. 12:40)` after a body MAX says was
changed. All three are **presentation** — they exist so the owner can read a
Telegram chat that is standing in for a MAX one, and none of them is anybody's
message.

They have to come off again in exactly one place: when an owner edit made in
Telegram is carried back into MAX. The Telegram copy of a MAX message is a
rendering, so the text an `UpdateEditMessage` reports is the rendering too — and
writing it into MAX would put `[29/07 13:47]` inside the original. The bridge
therefore strips only decorations it can prove it rendered itself before an
owner edit is sent back to MAX.

The patterns are matched against **what these renderers produce**, never against
anything looser: `format_stamp`'s two shapes, `format_edit_mark`'s one, and the
forward header's arrow. A body that merely looks similar — somebody writing
`[дома]` — is left alone, and the caller only asks at all for a message the
mapping says the bridge rendered.
"""

from __future__ import annotations

import re

from .forwards import FORWARD_ARROW
from .stamps import EDIT_MARK

#: `[29/07 13:47] ` and `[29/07/2025 13:47] ` — the two shapes `format_stamp`
#: draws, and no third one. The trailing space is part of the stamp.
_STAMP = re.compile(r"^\[\d{2}/\d{2}(?:/\d{4})? \d{2}:\d{2}\] ")

#: `↪ Переслано от Аня\n` and `↪ Пересланное сообщение\n`. Matched by the arrow
#: and the newline that always follows it rather than by the name, which is a
#: person's and is never pattern-matched here.
_FORWARD = re.compile(rf"^{re.escape(FORWARD_ARROW)} [^\n]*\n")

#: ` (изм. 12:40)`, and the same with a date — `format_edit_mark` over
#: `format_clock`, whose three shapes are all covered.
_EDIT_MARK = re.compile(
    rf" \({re.escape(EDIT_MARK)} (?:\d{{2}}/\d{{2}}(?:/\d{{4}})? )?\d{{2}}:\d{{2}}\)$"
)


def strip_presentation(text: str) -> str:
    """The message inside a rendering of it, or the text unchanged.

    Applied in rendering order, outermost first: the stamp sits in front of the
    forward line, which sits in front of the body, which the edit mark follows.
    Each pattern is applied at most once — a body that genuinely begins with a
    second stamp-shaped thing is the owner's own words and stays.
    """
    body = _STAMP.sub("", text, count=1)
    body = _FORWARD.sub("", body, count=1)
    return _EDIT_MARK.sub("", body, count=1)


__all__ = ["strip_presentation"]
