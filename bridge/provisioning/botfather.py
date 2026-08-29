"""Parsing @BotFather, kept away from anything that talks to the network.

The dialogue with BotFather is four messages long and entirely made of prose:
it answers "Done! Congratulations on your new bot" and hides the token in the
middle of a paragraph. Prose changes. So every string BotFather can send is
handled here, in pure functions over text, and the transport lives next door in
`mtproto.py` — which means the fragile half is the half that is tested.

The rule for anything unrecognised is to stop rather than guess: a wrong guess
here would leave a half-created bot and a token nobody wrote down.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

#: A bot token: numeric id, colon, 35 characters of base64-ish secret.
TOKEN_PATTERN = re.compile(r"\b(\d{8,12}:[A-Za-z0-9_-]{35})\b")

#: What BotFather says when a username is taken. Several wordings over the years.
_TAKEN_MARKERS = (
    "sorry, this username is already taken",
    "sorry, this username is invalid",
    "username is already taken",
)

_INVALID_NAME_MARKERS = (
    "sorry, the name is invalid",
    "invalid name",
)

#: The account is full. Waiting does not help; deleting a bot does. These two
#: groups used to be one, and conflating them is the exact mistake
#: `provisioner.py` warns about: an owner with thirty-three free slots was told
#: they had run out, because @BotFather had merely asked them to slow down.
_LIMIT_MARKERS = (
    "you have too many bots",
    "can't add more than",
    "cannot add more than",
)

#: Come back later, and @BotFather says how much later — always, and in seconds.
#: Measured on this account: 62 s at the low end, 58000 s (over sixteen hours)
#: after five `/newbot` walks back to back. A blind cooldown cannot be right for
#: both, so the number in the sentence is read rather than guessed.
_TOO_SOON_MARKERS = (
    "too many attempts",
    "sorry, too many",
    "try again in",
    "retry after",
)

#: `…try again in 62 seconds`, `…in 58000 seconds`. Minutes and hours have not
#: been observed; the pattern takes them in case, and normalises to seconds.
#: The plural is load bearing. `seconds` with the `s` outside the group never
#: matched — the word boundary landed mid-word — and the wait read as "not
#: stated" on every real refusal @BotFather has ever sent.
_RETRY_AFTER = re.compile(
    r"(?:try again|retry)\D{0,20}?(\d+)\s*(seconds?|secs?|minutes?|mins?|hours?|[smh])\b",
    re.IGNORECASE,
)
_RETRY_UNITS = {
    "s": 1,
    "sec": 1,
    "second": 1,
    "m": 60,
    "min": 60,
    "minute": 60,
    "h": 3600,
    "hour": 3600,
}


def retry_after_seconds(text: str) -> int | None:
    """How long @BotFather asked us to wait, or None when it did not say."""
    found = _RETRY_AFTER.search(text or "")
    if not found:
        return None
    unit = found.group(2).lower().rstrip("s") or "s"
    return int(found.group(1)) * _RETRY_UNITS[unit]


_ASK_NAME_MARKERS = (
    "alright, a new bot",
    "how are we going to call it",
    "please choose a name",
)

_ASK_USERNAME_MARKERS = (
    "choose a username",
    "must end in `bot`",
    "must end in bot",
)

#: `/token` shows the *existing* token of a bot that already exists — which is
#: the whole of what Managed Bots' `getManagedBotToken` did, and the reason a
#: bot never has to be deleted and remade to recover one.
#:
#: Deliberately not `/revoke`: that mints a new token and invalidates the old,
#: which would stop a bridge that is running perfectly well on it.
_ASK_TOKEN_TARGET_MARKERS = (
    "choose a bot to change token",
    "choose a bot to generate",
    "select a bot to change token",
)

#: `/deletebot` is a three-step dialogue of its own, and every step has to be
#: recognised: sending the confirmation phrase to the *wrong* step would either
#: do nothing or, worse, answer a question about a different bot.
_ASK_DELETE_TARGET_MARKERS = (
    "choose a bot to delete",
    "send me the username of the bot you want to delete",
    "which bot do you want to delete",
)

#: BotFather insists on this exact sentence before it removes anything. It is
#: also the only safety interlock the deletion path has on Telegram's side.
DELETE_CONFIRMATION = "Yes, I am totally sure."

_CONFIRM_DELETE_MARKERS = (
    "are you sure",
    "totally sure",
)

_DELETED_MARKERS = (
    "the bot is gone",
    "bot is deleted",
    "done! the bot",
)

_NO_SUCH_BOT_MARKERS = (
    "invalid bot selected",
    "i don't have a bot with",
    "i don't have such a bot",
    "unknown bot",
)


class Reply(StrEnum):
    """What BotFather's latest message means for the flow."""

    ASK_NAME = "ask_name"
    ASK_USERNAME = "ask_username"
    TOKEN = "token"  # noqa: S105 - a reply kind, not a secret
    USERNAME_TAKEN = "username_taken"
    NAME_INVALID = "name_invalid"
    LIMIT = "limit"
    #: A rate limit with a stated wait. Not `LIMIT`: waiting fixes this one.
    TOO_SOON = "too_soon"
    ASK_TOKEN_TARGET = "ask_token_target"  # noqa: S105 - a reply kind, not a secret
    ASK_DELETE_TARGET = "ask_delete_target"
    CONFIRM_DELETE = "confirm_delete"
    DELETED = "deleted"
    NO_SUCH_BOT = "no_such_bot"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ParsedReply:
    kind: Reply
    token: str | None = None
    #: Only for `TOO_SOON`: the wait @BotFather named, in seconds.
    retry_after: int | None = None


def parse(text: str) -> ParsedReply:
    """Classify one BotFather message. Never guesses."""
    lowered = (text or "").lower()

    token = TOKEN_PATTERN.search(text or "")
    if token:
        return ParsedReply(Reply.TOKEN, token.group(1))

    # The deletion dialogue first: "Done! The bot is gone." would otherwise fall
    # through to UNKNOWN, and an unrecognised answer means "stop", which here
    # would leave a bot that *is* deleted looking like one that is not.
    if any(marker in lowered for marker in _DELETED_MARKERS):
        return ParsedReply(Reply.DELETED)
    if any(marker in lowered for marker in _NO_SUCH_BOT_MARKERS):
        return ParsedReply(Reply.NO_SUCH_BOT)
    if any(marker in lowered for marker in _CONFIRM_DELETE_MARKERS):
        return ParsedReply(Reply.CONFIRM_DELETE)
    if any(marker in lowered for marker in _ASK_DELETE_TARGET_MARKERS):
        return ParsedReply(Reply.ASK_DELETE_TARGET)
    if any(marker in lowered for marker in _ASK_TOKEN_TARGET_MARKERS):
        return ParsedReply(Reply.ASK_TOKEN_TARGET)

    if any(marker in lowered for marker in _TAKEN_MARKERS):
        return ParsedReply(Reply.USERNAME_TAKEN)
    if any(marker in lowered for marker in _INVALID_NAME_MARKERS):
        return ParsedReply(Reply.NAME_INVALID)
    if any(marker in lowered for marker in _ASK_USERNAME_MARKERS):
        return ParsedReply(Reply.ASK_USERNAME)
    if any(marker in lowered for marker in _ASK_NAME_MARKERS):
        return ParsedReply(Reply.ASK_NAME)
    if any(marker in lowered for marker in _LIMIT_MARKERS):
        return ParsedReply(Reply.LIMIT)
    if any(marker in lowered for marker in _TOO_SOON_MARKERS):
        return ParsedReply(Reply.TOO_SOON, retry_after=retry_after_seconds(text or ""))
    return ParsedReply(Reply.UNKNOWN)


def redact(text: str) -> str:
    """Anything printed or logged from this dialogue goes through here first."""
    return TOKEN_PATTERN.sub("<token>", text or "")
