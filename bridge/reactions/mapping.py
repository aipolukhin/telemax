"""Translating reactions between two sets that only partly overlap.

Telegram's free reaction set and MAX's are different lists, so a reaction can
survive one of three ways, tried in order:

1. **as itself** — the emoji exists on the other side;
2. **as its nearest neighbour in the same meaning group** — 😂 becomes 🤣 rather
   than vanishing. The groups are written out below by hand: matching on
   codepoint similarity produces nonsense like 🐱 for 🐶;
3. **as text** — a short line saying what the contact reacted with. Ugly, but it
   never silently drops a reaction.

A bot may set only one reaction per message, so nothing here ever produces a
list.
"""

from __future__ import annotations

from typing import Final

from .max_set import can_send

# Telegram's free set, from the Bot API docs. Anything outside it cannot be set
# by a bot, whatever MAX sends us.
# fmt: off
TELEGRAM_FREE: Final[frozenset[str]] = frozenset({
    "👍", "👎", "❤", "❤️", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", "🤬",
    "😢", "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊", "🤡", "🥱", "🥴", "😍",
    "🐳", "❤‍🔥", "🌚", "🌭", "💯", "🤣", "⚡", "🍌", "🏆", "💔", "🤨", "😐",
    "🍓", "🍾", "💋", "🖕", "😈", "😴", "😭", "🤓", "👻", "👨‍💻", "👀", "🎃",
    "🙈", "😇", "😨", "🤝", "✍", "🤗", "🫡", "🎅", "🎄", "☃", "💅", "🤪",
    "🗿", "🆒", "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾", "🤷‍♂", "🤷",
    "🤷‍♀", "😡",
})
# fmt: on

# Meaning groups, ordered by preference inside each group. The first member that
# the target side accepts wins.
_GROUPS: Final[tuple[tuple[str, ...], ...]] = (
    ("👍", "👌", "💪", "🤝"),
    ("👎", "😡", "🤬", "🖕"),
    ("❤️", "❤", "🥰", "😍", "😘", "💋", "💘", "🖤", "❤️‍🩹"),
    ("🔥", "💯", "🚀", "⚡"),
    ("🤣", "😂", "😁", "😜", "🤪", "🤡"),
    ("😢", "😭", "😔", "💔", "🥺"),
    ("😱", "😨", "🤯", "😳", "👀"),
    ("🤔", "🧐", "🤨", "😐", "🤷", "🤷‍♀️", "🤷‍♂️"),
    ("🎉", "🥳", "🤩", "🏆"),
    ("🤮", "💩", "😒", "🥴"),
    ("🙏", "🫡", "👏"),
    ("😴", "🥱"),
    ("👻", "💀", "🗿", "😈"),
    # Animals are not interchangeable: a cat is not a unicorn. Kept apart so an
    # unmappable one falls back to text instead of turning into a different pet.
    ("🐱", "🐶"),
    ("🎄", "🎅", "☃️", "🌚", "🌝"),
    ("❓", "❗", "🛑"),
)

_GROUP_OF: Final[dict[str, tuple[str, ...]]] = {
    member: group for group in _GROUPS for member in group
}


def to_telegram(emoji: str) -> str | None:
    """A MAX reaction as a Telegram one, or None when only text will do."""
    if emoji in TELEGRAM_FREE:
        return emoji
    for candidate in _GROUP_OF.get(emoji, ()):
        if candidate in TELEGRAM_FREE:
            return candidate
    return None


def to_max(emoji: str) -> str | None:
    """A Telegram reaction as a MAX one, or None when MAX has nothing like it."""
    if can_send(emoji):
        return emoji
    for candidate in _GROUP_OF.get(emoji, ()):
        if can_send(candidate):
            return candidate
    return None


def describe(emoji: str) -> str:
    """The text fallback, used when no mapping exists."""
    return f"Реакция: {emoji}"
