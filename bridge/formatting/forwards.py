"""The line that says a message was passed on rather than written.

Neither side of the bridge can carry a forward as a forward. Telegram's own
«Forwarded from» is not something a bot can set: it comes from `forward_origin`,
which only a real `forwardMessage` produces, and the source here lives in MAX
where no bot can reach it. MAX's native forward is a `link` pointing at a message
id *inside MAX*, so a message that came from Telegram has nothing to point at.

So in both directions a forward becomes an ordinary message with a line on top —
which is also what the official MAX web client does when the sender unticks
«показать автора»: it re-sends the original's text and attachments as a message
of its own. The difference here is that the line is kept, because the whole
point is that the owner can see this is not the contact's own words.
"""

from __future__ import annotations

from datetime import tzinfo

from bridge.config import TimestampStyle

from .stamps import format_stamp

#: Telegram puts an arrow in front of a forward; using the same one means the
#: bridge's version reads as the thing it is imitating rather than as a quote.
FORWARD_ARROW = "↪"

FORWARD_FROM = f"{FORWARD_ARROW} Переслано от"

#: MAX names nobody when the original author hid their profile, and Telegram
#: does the same for a user who forbade being linked. The message is still worth
#: marking: "somebody else wrote this" is the part that matters.
FORWARD_ANONYMOUS = f"{FORWARD_ARROW} Пересланное сообщение"


def forward_header(name: str | None) -> str:
    """`↪ Переслано от Аня\\n` — or the anonymous form, always with the newline.

    A trailing newline rather than a separator on purpose: the body below is
    somebody's actual message and may itself start with anything, including
    another arrow.
    """
    cleaned = (name or "").strip()
    return f"{FORWARD_FROM} {cleaned}\n" if cleaned else f"{FORWARD_ANONYMOUS}\n"


#: Where a Telegram profile lives. Short form on purpose: it is what the link
#: shows when there is no name to show instead, and `t.me/someone` reads as an
#: address while the full URL reads as noise.
PROFILE_HOST = "t.me"


def profile_url(username: str) -> str:
    return f"https://{PROFILE_HOST}/{username.lstrip('@').strip()}"


def _linked(name: str | None, username: str | None) -> str:
    """The author, as the markdown PyMax parses back into a link.

    A username is the one identifier that is genuinely the author's — it cannot
    be renamed by whoever saved them — so it is also worth being the link. With
    no name to put on it, the address itself is the label.

    Square brackets are dropped rather than escaped: PyMax's markdown has no
    escape syntax, and a `]` inside the label would end the link early and leave
    the rest of the URL sitting in the message as text.
    """
    handle = (username or "").lstrip("@").strip()
    label = (name or "").strip()
    if not handle:
        return label
    shown = (label or f"{PROFILE_HOST}/{handle}").replace("[", "").replace("]", "")
    return f"[{shown}]({profile_url(handle)})"


def forward_prefix(
    name: str | None,
    *,
    username: str | None = None,
    at_ms: int | None,
    style: TimestampStyle = TimestampStyle.COMPACT,
    tz: tzinfo | None = None,
) -> str:
    """`[28/07 09:14] ↪ Переслано от [Аня](https://t.me/anya)\\n` — the whole line.

    The stamp is the *original's* time, not the moment it was passed on, and it
    goes first for the same reason it does on the way from MAX: a forward is
    routinely days old, and the receiving side stamps it with now. Without this
    the two directions disagreed — a forward reaching Telegram carried when it
    was written, the same forward reaching MAX did not.

    `format_stamp` still decides whether to draw anything: a message forwarded
    moments after it was written needs no second clock next to the one the
    messenger already shows.

    The name is a link to the author's profile when there is a username to point
    at, so the contact reading it can actually reach them; plain text when there
    is not. Markdown, because that is how formatting reaches MAX — this line is
    only ever built for the Telegram → MAX direction, where PyMax parses it. The
    other direction carries entities and stays plain.
    """
    stamp = format_stamp(at_ms, style, tz=tz)
    linked = _linked(name, username)
    if not linked:
        return f"{stamp}{FORWARD_ANONYMOUS}\n"
    return f"{stamp}{FORWARD_FROM} {linked}\n"


def utf16_length(text: str) -> int:
    """Length in UTF-16 code units, which is what both protocols count in.

    `len()` counts code points, and the two differ on exactly the characters a
    display name is most likely to contain — emoji. Every offset that moves
    formatting past a prefix has to be measured this way or an emoji in a
    contact's name shifts the whole message's formatting one character left.
    """
    return len(text.encode("utf-16-le")) // 2
