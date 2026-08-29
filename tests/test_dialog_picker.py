"""The dialog list: two calls in total, not two per dialog.

A MAX dialog has no title — the other participant is the name — so the list looked
like it needed a lookup per line. It did, and with sixty lines that was five
seconds of «Добавить диалоги». The contact is on the chat object the picker already
holds, and the names come back in one batch, so the call counts are asserted here
as behaviour: they are the difference between a list that opens and one that hangs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bridge.provisioning.picker import DialogPicker


@dataclass
class FakeChat:
    id: int
    type: str = "DIALOG"
    last_event_time: int = 0
    participants: dict[str, int] = field(default_factory=dict)


@dataclass
class FakeSource:
    chats: list[FakeChat] = field(default_factory=list)
    names: dict[int, str | None] = field(default_factory=dict)
    own: int = 77

    dialog_calls: int = 0
    name_calls: list[list[int]] = field(default_factory=list)

    async def fetch_dialogs(self) -> list[Any]:
        self.dialog_calls += 1
        return list(self.chats)

    def contact_of(self, chat: Any) -> int | None:
        ids = [int(key) for key in chat.participants if int(key) != self.own]
        return min(ids) if ids else None

    async def display_names(self, user_ids: list[int]) -> dict[int, str | None]:
        self.name_calls.append(list(user_ids))
        return {user_id: self.names.get(user_id) for user_id in user_ids}


def dialog(chat_id: int, contact: int | None, *, when: int = 0) -> FakeChat:
    participants = {"77": 1}
    if contact is not None:
        participants[str(contact)] = 1
    return FakeChat(id=chat_id, last_event_time=when, participants=participants)


async def test_the_whole_list_costs_two_calls() -> None:
    source = FakeSource(
        chats=[dialog(1, 11, when=30), dialog(2, 12, when=20), dialog(3, 13, when=10)],
        names={11: "Мама", 12: "Иван", 13: "Аня"},
    )

    options = await DialogPicker(source).options(exclude=set())

    assert [option.title for option in options] == ["Мама", "Иван", "Аня"]
    assert source.dialog_calls == 1
    assert source.name_calls == [[11, 12, 13]], "one batch, whatever the list length"


async def test_a_dialog_whose_contact_is_unknown_still_shows() -> None:
    """A chat with no readable participant is still a chat the owner may pick."""
    source = FakeSource(chats=[dialog(5, None)], names={})

    options = await DialogPicker(source).options(exclude=set())

    assert options[0].title == "чат 5"
    assert options[0].max_user_id is None
    assert source.name_calls == [[]], "nothing to ask about"


async def test_a_contact_without_a_name_falls_back_to_the_chat_id() -> None:
    source = FakeSource(chats=[dialog(7, 21)], names={21: None})

    options = await DialogPicker(source).options(exclude=set())

    assert options[0].title == "чат 7"
    assert options[0].max_user_id == 21


async def test_excluded_dialogs_are_never_asked_about() -> None:
    """The bridges that already exist cost neither a line nor a lookup."""
    source = FakeSource(chats=[dialog(1, 11), dialog(2, 12)], names={11: "Мама", 12: "Иван"})

    options = await DialogPicker(source).options(exclude={2})

    assert [option.max_chat_id for option in options] == [1]
    assert source.name_calls == [[11]]
