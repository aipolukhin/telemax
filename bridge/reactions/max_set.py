"""Reaction emoji accepted by the MAX compatibility layer.

The client picker and the server acceptance set may differ across versions, so
the bridge sends only the conservative `SAFE` subset while displaying any
reaction it receives.

Practical consequences:

* the bridge may only *send* `SAFE` — in the picker and accepted;
* any of them may *arrive* from the contact, including `REFUSED_BY_SERVER`, and
  those must display in Telegram normally. The limit is on sending only;
* an unknown emoji is answered with `error.message.like.unknown.like` and the
  connection survives, so a stale list degrades gracefully. A malformed *frame*
  does not — see `bridge/max_client/opcodes.py`.

"""

from __future__ import annotations

from typing import Final

#: In the app's picker and accepted by the server. The only set we send.
SAFE: Final[tuple[str, ...]] = (
    "👍", "❤️", "🤣", "🔥", "😭", "😍", "💩", "💯",
    "😁", "😡", "🎉", "👎", "😱", "🤮", "💔", "🤩",
    "💀", "🤟", "🤡", "😎", "😐", "😮", "🤪", "😜",
    "😋", "😇", "😚", "🥰", "🥳", "🌚", "🌝", "😴",
    "😈", "🤬", "🫠", "🤔", "🫡", "😳", "😔", "😢",
    "🐱", "🐶", "💪", "🤞", "👏", "🤝", "🙏", "💋",
    "👑", "🍷", "🍑", "🤷‍♀️", "🤷‍♂️", "🦄", "👻", "🎄",
    "🎅", "🗿", "👀", "🖤", "❤️‍🩹", "🛑", "❓", "❗",
    "🚀", "🇷🇺",
)  # fmt: skip

#: Offered by the app, refused by the server for our client version. A contact
#: can still send these; we cannot.
REFUSED_BY_SERVER: Final[tuple[str, ...]] = (
    "🤌", "🟩", "🧐", "🖐️", "⚡", "👩‍❤️‍💋‍👨", "☃️", "🛸",
)  # fmt: skip

#: Accepted by the server but gone from the app's picker — an older set.
LEGACY_ACCEPTED: Final[tuple[str, ...]] = (
    "👁️", "👌", "🖕", "😂", "😒", "😘", "🤗", "🤤",
    "🤯", "🤷", "🥱", "🥴", "🥺",
)  # fmt: skip

_SAFE_SET: Final[frozenset[str]] = frozenset(SAFE)
_SENDABLE: Final[frozenset[str]] = frozenset(SAFE) | frozenset(LEGACY_ACCEPTED)


def can_send(emoji: str) -> bool:
    """True when MAX accepted this emoji from our client at verification time."""
    return emoji in _SENDABLE


def is_in_picker(emoji: str) -> bool:
    """True when the emoji is safe to offer the owner in a keyboard."""
    return emoji in _SAFE_SET


def unsupported(emoji_list: tuple[str, ...]) -> tuple[str, ...]:
    """Which of these the bridge must refuse to send. Used to validate config."""
    return tuple(emoji for emoji in emoji_list if not can_send(emoji))
