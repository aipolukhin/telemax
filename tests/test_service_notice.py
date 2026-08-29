"""MAX's own notices are messages, not unrenderable attachments.

Measured 2026-08-06 on a bridge built for a contact with no history: the one
thing in the dialog was MAX's join notice from 20 July, and it arrived as
«[MAX прислал вложение неизвестного типа — показать его пока не умею]».

The type was not unknown. It is `CONTROL`, it carries no file, and the server
has already written the sentence — `message` and `shortMessage` both hold it.
"""

from __future__ import annotations

from typing import Any

import pytest

from bridge.max_client.events import AttachmentKind, normalize_message
from bridge.media.delivery import KIND_NAMES, service_text

pytestmark = pytest.mark.asyncio

#: The shape from the live log: type=CONTROL fields=[_type, event, message,
#: shortMessage].
JOINED = {
    "_type": "CONTROL",
    "event": "USER_JOIN",
    "message": "Теперь в MAX! 👉 Напишите что-нибудь!",
    "shortMessage": "Теперь в MAX!",
}


def _attachment(raw: dict[str, Any]) -> Any:
    message = normalize_message(
        {"id": 1, "sender": 7, "time": 1700000001000, "attaches": [raw]},
        own_user_id=99,
    )
    assert len(message.attachments) == 1
    return message.attachments[0]


async def test_a_control_attachment_is_a_notice_not_an_unknown() -> None:
    attachment = _attachment(JOINED)

    assert attachment.kind is AttachmentKind.SERVICE
    assert attachment.notice == "Теперь в MAX! 👉 Напишите что-нибудь!"
    assert attachment.event == "user_join"


async def test_the_notice_is_what_the_owner_reads() -> None:
    assert service_text(_attachment(JOINED), caption=None) == (
        "Теперь в MAX! 👉 Напишите что-нибудь!"
    )


async def test_a_caption_keeps_its_place_above_the_notice() -> None:
    assert service_text(_attachment(JOINED), caption="привет") == (
        "привет\nТеперь в MAX! 👉 Напишите что-нибудь!"
    )


async def test_the_short_form_answers_when_the_long_one_is_missing() -> None:
    raw = {"_type": "CONTROL", "event": "USER_JOIN", "shortMessage": "Теперь в MAX!"}
    assert service_text(_attachment(raw), caption=None) == "Теперь в MAX!"


async def test_an_event_with_no_sentence_is_named_rather_than_blank() -> None:
    """A blank line in the middle of an import reads as a lost message."""
    raw = {"_type": "CONTROL", "event": "SOMETHING_NEW"}
    assert service_text(_attachment(raw), caption=None) == (
        "MAX: служебное уведомление (something_new)"
    )


async def test_a_control_with_nothing_in_it_still_says_something() -> None:
    assert service_text(_attachment({"_type": "CONTROL"}), caption=None) == (
        "MAX: служебное уведомление"
    )


async def test_the_kind_has_a_russian_name_like_every_other() -> None:
    assert KIND_NAMES[AttachmentKind.SERVICE] == "уведомление"
