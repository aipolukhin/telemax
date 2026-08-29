"""Choosing which MAX dialogs get a bot, from inside the guardian chat.

The alternative was a wizard in the terminal at install time. This is better for
one reason: the answer changes. Contacts get added months later, a dialog that
did not matter starts mattering, and a bootstrap wizard is exactly the wrong
place to be when that happens. The guardian bot is already the place where the
bridge asks the owner questions, so the picker lives there too.

The list is deliberately short and ordered by last activity: an address book has
hundreds of dialogs and the ones worth bridging are almost always the ones that
moved this week.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from bridge.max_client import enum_text

#: How many dialogs to offer. Enough to cover everyone the owner talks to, few
#: enough to fit in a message without scrolling past the point.
DEFAULT_LIMIT = 60

#: The button that reopens the list from another screen. Everything else about
#: the keyboard — selecting, paging, committing — belongs to `selection`, which
#: owns the callbacks and the state they carry.
OPEN = "prov:dialogs"

#: Personal dialogs only. A group is not a bridge — one bot is one person.
_PERSONAL = {"DIALOG", "PRIVATE", "CHAT_TYPE_DIALOG"}


class DialogSource(Protocol):
    async def fetch_dialogs(self) -> list[Any]: ...

    def contact_of(self, chat: Any) -> int | None: ...

    async def display_names(self, user_ids: list[int]) -> dict[int, str | None]: ...


@dataclass(frozen=True, slots=True)
class DialogOption:
    """One line in the picker."""

    max_chat_id: int
    title: str
    last_activity: int
    max_user_id: int | None = None


def is_personal(chat: Any) -> bool:
    """PyMax hands an enum whose `str()` is `ChatType.DIALOG`, not `DIALOG`."""
    return enum_text(getattr(chat, "type", None)) in _PERSONAL


def rank(chats: list[Any], *, exclude: set[int], limit: int = DEFAULT_LIMIT) -> list[Any]:
    """Personal dialogs, newest first, minus the ones already bridged."""
    personal = [
        chat
        for chat in chats
        if is_personal(chat) and int(getattr(chat, "id", 0) or 0) not in exclude
    ]
    personal.sort(key=lambda chat: int(getattr(chat, "last_event_time", 0) or 0), reverse=True)
    return personal[:limit]


class DialogPicker:
    """Turns MAX's chat list into something the owner can tap."""

    def __init__(self, source: DialogSource, *, limit: int = DEFAULT_LIMIT) -> None:
        self._source = source
        self._limit = limit

    async def dialog_with(self, max_user_id: int) -> DialogOption | None:
        """The dialog with one specific person, however old it is.

        Not `options()` filtered: that list is ranked by activity and cut at
        sixty, which is right for a screen to choose from and wrong for a
        question about one person. Somebody the owner has not spoken to in two
        years is exactly who they are adding by number.
        """
        for chat in await self._source.fetch_dialogs():
            if not is_personal(chat):
                continue
            if self._source.contact_of(chat) != max_user_id:
                continue
            chat_id = int(getattr(chat, "id", 0) or 0)
            if not chat_id:
                continue
            names = await self._source.display_names([max_user_id])
            return DialogOption(
                max_chat_id=chat_id,
                title=names.get(max_user_id) or f"чат {chat_id}",
                last_activity=int(getattr(chat, "last_event_time", 0) or 0),
                max_user_id=max_user_id,
            )
        return None

    async def options(self, *, exclude: set[int]) -> list[DialogOption]:
        """The dialogs worth offering, with a name for each.

        A MAX dialog has no title of its own — the other participant is the name.
        Two calls in total, and it used to be two *per dialog*: the contact comes
        off the chat object already in hand, and the sixty names come back in one
        batch. That is the whole of the five seconds «Добавить диалоги» used to
        take, and nothing else about the screen changed.
        """
        chats = await self._source.fetch_dialogs()
        ranked = [
            (chat_id, chat)
            for chat in rank(chats, exclude=exclude, limit=self._limit)
            if (chat_id := int(getattr(chat, "id", 0) or 0))
        ]
        contacts = {chat_id: self._source.contact_of(chat) for chat_id, chat in ranked}
        names = await self._source.display_names(
            [user_id for user_id in contacts.values() if user_id is not None]
        )

        return [
            DialogOption(
                max_chat_id=chat_id,
                title=names.get(contacts[chat_id] or 0) or f"чат {chat_id}",
                last_activity=int(getattr(chat, "last_event_time", 0) or 0),
                max_user_id=contacts[chat_id],
            )
            for chat_id, chat in ranked
        ]
