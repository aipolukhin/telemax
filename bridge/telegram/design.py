"""The one place that decides what a guardian screen looks like.

Two things live here and both exist because they were duplicated before:

* **the status glyphs.** Onboarding drew `✓`, the picker drew `✓` for a chosen
  row and `⏳` for a running step, and neither knew what the other meant. One
  symbol per state, named, so a screen cannot invent a sixth way to say "busy".
* **escaping.** The guardian talks in HTML now, and almost every screen
  interpolates a name that came from MAX. A contact called `<b` is not an attack,
  but it is a message Telegram refuses to send — and the owner sees nothing at
  all rather than a slightly odd name.

Button labels are *not* escaped: Telegram never parses them, so an escaped label
shows the owner a literal `&amp;`.
"""

from __future__ import annotations

import html

#: Live and carrying messages.
RUNNING = "🟢"
#: Working, but something needs the owner.
ATTENTION = "🟡"
#: Broken.
BROKEN = "🔴"
#: Switched off by the owner. Not a fault, and it must not read as one: the
#: three coloured lights say "how is it", and this says "you turned it off".
DISABLED = "⚪"
#: A step in progress right now.
BUSY = "⏳"
#: A step that has not started.
TODO = "○"
#: A step that finished.
DONE = "✅"
#: A step that failed.
FAILED = "❌"

__all__ = [
    "ATTENTION",
    "BROKEN",
    "BUSY",
    "DISABLED",
    "DONE",
    "FAILED",
    "RUNNING",
    "TODO",
    "bold",
    "esc",
    "title",
]


def esc(value: object) -> str:
    """Whatever this is, make it safe to put inside an HTML message.

    `quote=False` on purpose: quotes are legal in message text and escaping them
    turns a normal Russian sentence into `&quot;`-soup.
    """
    return html.escape(str(value), quote=False)


def bold(value: object) -> str:
    return f"<b>{esc(value)}</b>"


def title(heading: str, *lines: str) -> str:
    """A bold heading, a blank line, and the body — the shape of every screen.

    Lines are passed through as-is: the caller escapes what came from outside
    and may deliberately carry markup of its own.
    """
    body = "\n".join(lines).strip("\n")
    return f"{bold(heading)}\n\n{body}" if body else bold(heading)
