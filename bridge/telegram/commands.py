"""What counts as a command to the bot rather than a message to the contact.

There are two ways the owner's typing reaches MAX and they disagreed. The Bot
API router dropped anything starting with `/`; the MTProto intake — the owner's
own user session, reading the same chat — dropped nothing, so `/start` in a
bridge chat was delivered to the contact as the word "/start". The bot answered
*and* the message went through, which is the worst of both.

One rule now, used by both, and it is stricter than "starts with a slash": a
command is a leading `/name`, optionally `@somebot`, made of the characters
Telegram allows. `/etc/passwd` is a path, `/привет` is a word — neither is a
command to Telegram and neither is one here.

Deliberately not entity-based. The user session and the Bot API describe
entities differently, and a rule that reads the same on both paths is worth more
than one that is exactly Telegram's on one path and approximated on the other.
"""

from __future__ import annotations

import re

#: `/name`, `/name@bot`, then end-of-string or whitespace. Telegram's own shape:
#: letters, digits and underscores, at most 32, and it must start with a letter.
_COMMAND = re.compile(r"^/[A-Za-z][A-Za-z0-9_]{0,31}(@[A-Za-z0-9_]{1,32})?(\s|$)")


def is_bot_command(text: str | None) -> bool:
    """Whether this text is addressed to the bot and must not reach the contact."""
    if not text:
        return False
    return _COMMAND.match(text.lstrip()) is not None
