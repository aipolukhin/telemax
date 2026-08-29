"""Contact sharing, both directions (W6).

A shared contact is not a file: its whole content is a name and, when there is
one, a phone. What arrives decides the shape — a MAX user shared by id comes
without a phone (the server does not leak it) and becomes a labelled card, while
a phonebook or vCard contact carries a phone and becomes a real Telegram contact
the owner can tap. Wire shapes confirmed in compatibility fixtures 2026-08-04.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest_asyncio

from bridge.config import TimestampStyle
from bridge.max_client import AttachmentKind, normalize_attachment
from bridge.media.delivery import (
    contact_card_text,
    contact_first_last,
)
from bridge.routing import BridgeRouter, BridgeTarget
from bridge.routing.adapters import vcard_of
from bridge.storage import BridgeStateRepository, Database, MessageMapRepository

MOM = BridgeTarget(name="mom", max_chat_id=777, bot_id=100)
OWNER_CHAT = 111
CONTACT = 4242
OWNER_MAX_ID = 100000002


# --------------------------------------------------------------- normalisation


def test_a_max_user_contact_has_no_phone() -> None:
    """Shared by id: name and MAX id, but no phone — the server withholds it."""
    attach = normalize_attachment(
        {
            "_type": "CONTACT",
            "contactId": 200000002,
            "firstName": "Леон",
            "lastName": "",
            "name": "Леон",
            "photoUrl": "https://i.oneme.ru/i?r=abc",
        }
    )

    assert attach.kind is AttachmentKind.CONTACT
    assert attach.contact_name == "Леон"
    assert attach.contact_user_id == 200000002
    assert attach.contact_phone is None
    assert attach.contact_vcard is None


def test_a_phonebook_contact_carries_phone_and_vcard() -> None:
    attach = normalize_attachment(
        {
            "_type": "CONTACT",
            "firstName": "Иван",
            "lastName": "Петров",
            "name": "Иван",
            "phone": "+79991234567",
            "vcfBody": "BEGIN:VCARD\nVERSION:3.0\nFN:Иван Петров\nEND:VCARD",
        }
    )

    assert attach.contact_name == "Иван Петров"
    assert attach.contact_phone == "+79991234567"
    assert attach.contact_vcard is not None
    assert attach.contact_user_id is None


def test_a_contact_with_only_a_name_field_is_still_named() -> None:
    attach = normalize_attachment({"_type": "CONTACT", "name": "Только имя"})

    assert attach.contact_name == "Только имя"


# ----------------------------------------------------------- MAX -> TG render


def test_the_card_names_a_phoneless_contact() -> None:
    attach = normalize_attachment({"_type": "CONTACT", "contactId": 1, "name": "Аня"})

    assert contact_card_text(attach, caption=None) == "👤 Контакт [Max]: Аня"


def test_the_card_keeps_a_caption_above_it() -> None:
    attach = normalize_attachment({"_type": "CONTACT", "contactId": 1, "name": "Аня"})

    assert contact_card_text(attach, caption="вот") == "вот\n👤 Контакт [Max]: Аня"


def test_a_nameless_contact_is_still_a_card() -> None:
    attach = normalize_attachment({"_type": "CONTACT", "contactId": 1})

    assert contact_card_text(attach, caption=None) == "👤 Контакт [Max]"


def test_the_name_splits_into_first_and_last_for_send_contact() -> None:
    attach = normalize_attachment(
        {"_type": "CONTACT", "phone": "+7999", "firstName": "Иван", "lastName": "Петров"}
    )

    assert contact_first_last(attach) == ("Иван", "Петров")


def test_a_single_word_name_has_no_last() -> None:
    attach = normalize_attachment({"_type": "CONTACT", "phone": "+7999", "name": "Аня"})

    assert contact_first_last(attach) == ("Аня", None)


# --------------------------------------------- MAX -> TG through the delivery


@dataclass(slots=True)
class FakeContactSender:
    sent_contacts: list[dict[str, Any]] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    next_id: int = 900

    async def send_contact(self, bot_id: int, chat_id: int, *, phone: str, first_name: str,
                           last_name: str | None = None, vcard: str | None = None,
                           reply_to: int | None = None) -> int | None:
        self.sent_contacts.append(
            {"phone": phone, "first": first_name, "last": last_name, "vcard": vcard}
        )
        self.next_id += 1
        return self.next_id

    button_sets: list[list[tuple[str, str]] | None] = field(default_factory=list)
    photos: list[dict[str, Any]] = field(default_factory=list)
    #: When set, `send_photo_url` fails (returns None) — a stale avatar.
    photo_fails: bool = False

    async def send_text(self, bot_id: int, chat_id: int, text: str, *,
                        reply_to: int | None = None,
                        entities: list[dict[str, Any]] | None = None,
                        buttons: list[tuple[str, str]] | None = None) -> int | None:
        self.texts.append(text)
        self.button_sets.append(buttons)
        self.next_id += 1
        return self.next_id

    async def send_photo_url(self, bot_id: int, chat_id: int, url: str, *, caption: str,
                             reply_to: int | None = None,
                             buttons: list[tuple[str, str]] | None = None) -> int | None:
        if self.photo_fails:
            return None
        self.photos.append({"url": url, "caption": caption, "buttons": buttons})
        self.next_id += 1
        return self.next_id

    # The rest of the media port is never reached for a contact.
    async def _unused(self, *a: Any, **k: Any) -> int | None:
        raise AssertionError("a contact must not reach a media method")

    send_photo = send_video = send_video_note = _unused
    send_voice = send_audio = send_document = send_sticker = _unused
    send_album = _unused


def _delivery(sender: FakeContactSender, tmp_path: Path) -> Any:
    """A delivery whose media pipeline is never reached — a contact stops before
    any fetch, so the pipeline only has to exist, not to work."""
    from bridge.media.delivery import MaxMediaDelivery
    from bridge.media.pipeline import MediaPipeline
    from bridge.media.sources import MaxMediaSources
    from bridge.media.store import TempFiles

    class _NoProtocol:
        async def video_sources(self, *a: Any, **k: Any) -> Any: ...
        async def audio_sources(self, *a: Any, **k: Any) -> Any: ...
        async def file_source(self, *a: Any, **k: Any) -> Any: ...

    async def _no_fetch(*a: Any, **k: Any) -> Any:
        raise AssertionError("a contact must not fetch bytes")

    pipeline = MediaPipeline(
        sources=MaxMediaSources(_NoProtocol()),
        temp_files=TempFiles(tmp_path / "tmp"),
        fetcher=_no_fetch,
        max_file_size_mb=1,
    )
    return MaxMediaDelivery(pipeline=pipeline, sender=sender)


def _contact_message(**over: Any) -> Any:
    from dataclasses import replace

    from bridge.max_client import normalize_message

    base = normalize_message(
        {"id": 1, "chatId": 777, "sender": 99, "text": "", "time": 1_785_000_000_000},
        own_user_id=1,
    )
    attach = normalize_attachment({"_type": "CONTACT", **over})
    return replace(base, attachments=(attach,))


async def test_a_contact_with_a_phone_becomes_a_telegram_contact(tmp_path: Path) -> None:
    sender = FakeContactSender()
    message = _contact_message(firstName="Иван", lastName="Петров", phone="+79991234567",
                               vcfBody="BEGIN:VCARD\nEND:VCARD")

    await _delivery(sender, tmp_path).deliver(message, bot_id=MOM.bot_id, chat_id=OWNER_CHAT)

    assert sender.texts == []
    assert sender.sent_contacts == [
        {"phone": "+79991234567", "first": "Иван", "last": "Петров",
         "vcard": "BEGIN:VCARD\nEND:VCARD"}
    ]


async def test_a_phoneless_contact_becomes_a_card(tmp_path: Path) -> None:
    sender = FakeContactSender()
    message = _contact_message(contactId=200000002, name="Иван")

    await _delivery(sender, tmp_path).deliver(message, bot_id=MOM.bot_id, chat_id=OWNER_CHAT)

    assert sender.sent_contacts == []
    assert sender.texts == ["👤 Контакт [Max]: Иван"]
    # No bridge link wired: the card is drawn without a button.
    assert sender.button_sets == [None]


async def test_a_contact_with_an_avatar_is_sent_as_a_photo(tmp_path: Path) -> None:
    sender = FakeContactSender()
    message = _contact_message(
        contactId=200000002, name="Анна С", photoUrl="https://i.oneme.ru/i?r=abc"
    )

    await _delivery(sender, tmp_path).deliver(message, bot_id=MOM.bot_id, chat_id=OWNER_CHAT)

    assert sender.texts == [], "with an avatar it is a photo, not a text card"
    assert sender.photos == [
        {"url": "https://i.oneme.ru/i?r=abc", "caption": "👤 Контакт [Max]: Анна С",
         "buttons": None}
    ]


async def test_an_avatar_photo_still_carries_the_bridge_button(tmp_path: Path) -> None:
    from bridge.media.delivery import BRIDGE_BUTTON, MaxMediaDelivery

    sender = FakeContactSender()
    base = _delivery(sender, tmp_path)
    delivery = MaxMediaDelivery(
        pipeline=base._pipeline, sender=sender, bridge_link=FakeBridgeLink()
    )
    message = _contact_message(
        contactId=200000002, name="Анна С", photoUrl="https://i.oneme.ru/i?r=abc"
    )

    await delivery.deliver(message, bot_id=MOM.bot_id, chat_id=OWNER_CHAT)

    assert sender.photos and sender.photos[0]["buttons"] == [
        (BRIDGE_BUTTON, "https://t.me/guard_bot?start=mb200000002")
    ]


async def test_the_router_upgrades_a_contact_thumbnail_to_the_full_avatar(
    tmp_path: Path,
) -> None:
    """The wire carries a 190px `photoUrl`; the profile's `baseUrl` is 1440px.

    The router looks the full one up and swaps it onto the attachment before the
    card is built, so the owner sees a face, not a thumbnail. Only a MAX user is
    enriched — a raw vCard contact has no profile to ask.
    """
    from dataclasses import replace as _replace

    from bridge.max_client import normalize_message

    @dataclass
    class FakeNames:
        avatars: dict[int, str] = field(default_factory=dict)
        asked: list[int] = field(default_factory=list)

        async def forward_author(self, user_id: int) -> tuple[str | None, str | None]:
            return None, None

        async def contact_avatar(self, user_id: int) -> str | None:
            self.asked.append(user_id)
            return self.avatars.get(user_id)

    names = FakeNames(avatars={200000002: "https://i.oneme.ru/i?r=FULL1440"})
    database = await Database.connect(tmp_path / "bridge.db")
    sender = FakeContactSender()

    class _Media:
        async def deliver(self, message: Any, **kw: Any) -> int | None:
            # The delivery the router hands its enriched message to.
            return await _delivery(sender, tmp_path).deliver(message, **kw)

    router = BridgeRouter(
        lookup=FakeLookup(),
        telegram=FakeTelegram(),
        max_sender=FakeMax(),
        messages=MessageMapRepository(database),
        state=BridgeStateRepository(database),
        owner_chat_id=OWNER_CHAT,
        timestamp_style=TimestampStyle.OFF,
        media=_Media(),
        display_names=names,
    )
    base = normalize_message(
        {"id": 1, "chatId": MOM.max_chat_id, "sender": CONTACT, "text": "", "time": 1},
        own_user_id=OWNER_MAX_ID,
    )
    attach = normalize_attachment(
        {"_type": "CONTACT", "contactId": 200000002, "name": "Иван",
         "photoUrl": "https://i.oneme.ru/i?r=thumb190"}
    )
    try:
        await router.on_max_message(_replace(base, attachments=(attach,)))
    finally:
        await database.close()

    assert names.asked == [200000002]
    assert sender.photos, "the card was sent as a photo"
    assert sender.photos[0]["url"] == "https://i.oneme.ru/i?r=FULL1440"


async def test_a_stale_avatar_falls_back_to_a_text_card(tmp_path: Path) -> None:
    sender = FakeContactSender(photo_fails=True)
    message = _contact_message(
        contactId=200000002, name="Анна С", photoUrl="https://i.oneme.ru/i?r=gone"
    )

    await _delivery(sender, tmp_path).deliver(message, bot_id=MOM.bot_id, chat_id=OWNER_CHAT)

    # The photo was attempted and failed; the card still arrives as text.
    assert sender.texts == ["👤 Контакт [Max]: Анна С"]


class FakeBridgeLink:
    def for_contact(self, max_user_id: int) -> str | None:
        return f"https://t.me/guard_bot?start=mb{max_user_id}"


async def test_a_max_contact_card_carries_a_bridge_button(tmp_path: Path) -> None:
    from bridge.media.delivery import BRIDGE_BUTTON, MaxMediaDelivery

    sender = FakeContactSender()
    base = _delivery(sender, tmp_path)
    delivery = MaxMediaDelivery(
        pipeline=base._pipeline, sender=sender, bridge_link=FakeBridgeLink()
    )
    message = _contact_message(contactId=200000002, name="Иван")

    await delivery.deliver(message, bot_id=MOM.bot_id, chat_id=OWNER_CHAT)

    assert sender.texts == ["👤 Контакт [Max]: Иван"]
    assert sender.button_sets == [
        [(BRIDGE_BUTTON, "https://t.me/guard_bot?start=mb200000002")]
    ]


async def test_the_bridge_button_survives_the_queue_path(tmp_path: Path) -> None:
    """`deliver_receipt` with `on_sending` rebuilds the delivery — the queue path
    every live message takes. It used to rebuild without the collaborators, so
    the button (and, before it, an animated sticker's origin) was dropped on the
    exact path that matters. This pins that the rebuild carries the bridge link."""
    from bridge.media.delivery import BRIDGE_BUTTON, MaxMediaDelivery

    sender = FakeContactSender()
    base = _delivery(sender, tmp_path)
    delivery = MaxMediaDelivery(
        pipeline=base._pipeline, sender=sender, bridge_link=FakeBridgeLink()
    )
    message = _contact_message(contactId=200000002, name="Иван")

    async def _announce() -> None:
        return None

    await delivery.deliver_receipt(
        message, bot_id=MOM.bot_id, chat_id=OWNER_CHAT, on_sending=_announce
    )

    assert sender.button_sets == [
        [(BRIDGE_BUTTON, "https://t.me/guard_bot?start=mb200000002")]
    ]


async def test_a_vcard_contact_has_no_bridge_button(tmp_path: Path) -> None:
    """No MAX id, nothing to bridge — a phone contact is a real contact, not a card."""
    from bridge.media.delivery import MaxMediaDelivery

    sender = FakeContactSender()
    base = _delivery(sender, tmp_path)
    delivery = MaxMediaDelivery(
        pipeline=base._pipeline, sender=sender, bridge_link=FakeBridgeLink()
    )
    message = _contact_message(firstName="Иван", phone="+79991234567",
                               vcfBody="BEGIN:VCARD\nEND:VCARD")

    await delivery.deliver(message, bot_id=MOM.bot_id, chat_id=OWNER_CHAT)

    assert sender.texts == []  # it is a native contact, not a card
    assert sender.sent_contacts and sender.button_sets == []


# ----------------------------------------------------------- TG -> MAX vCard


@dataclass
class FakeTgContact:
    phone_number: str
    first_name: str
    last_name: str | None = None
    vcard: str | None = None
    user_id: int | None = None


def test_a_telegram_contact_becomes_a_vcard_with_phone_and_name() -> None:
    card = vcard_of(FakeTgContact("+79991234567", "Иван", "Петров"))

    assert "BEGIN:VCARD" in card and "END:VCARD" in card
    assert "FN:Иван Петров" in card
    assert "TEL;TYPE=CELL:+79991234567" in card


def test_the_vcard_is_built_from_fields_not_telegram_s_own_card() -> None:
    """Telegram's `vcard` is often empty and need not carry a phone — the fields do."""
    card = vcard_of(FakeTgContact("+7999", "Аня", None, vcard="BEGIN:VCARD\nEND:VCARD"))

    assert "TEL;TYPE=CELL:+7999" in card
    assert "FN:Аня" in card


def test_a_special_character_in_a_name_cannot_break_the_vcard() -> None:
    card = vcard_of(FakeTgContact("+7999", "Иван; Петров", "и\nещё"))

    # The separators vCard gives meaning to are escaped, not left to end a field.
    assert "FN:Иван\\; Петров и\\nещё" in card


# ----------------------------------------------------------- TG -> MAX route


@dataclass(slots=True)
class FakeLookup:
    def bridge_for_max_chat(self, max_chat_id: int) -> BridgeTarget | None:
        return MOM if max_chat_id == MOM.max_chat_id else None

    def bridge_for_bot(self, bot_id: int) -> BridgeTarget | None:
        return MOM if bot_id == MOM.bot_id else None


@dataclass(slots=True)
class FakeTelegram:
    async def send_text(self, *a: Any, **k: Any) -> int | None:
        return 1

    async def edit_text(self, *a: Any, **k: Any) -> bool:
        return True

    async def delete(self, *a: Any, **k: Any) -> bool:
        return True


@dataclass(slots=True)
class FakeMax:
    contacts: list[dict[str, Any]] = field(default_factory=list)
    next_id: int = 500

    async def send_text(self, chat_id: int, text: str, *, reply_to: int | None = None) -> int:
        self.next_id += 1
        return self.next_id

    async def send_contact(self, chat_id: int, *, vcard: str,
                           contact_user_id: int | None = None,
                           reply_to: int | None = None) -> int | None:
        self.contacts.append({"chat": chat_id, "vcard": vcard, "user_id": contact_user_id})
        self.next_id += 1
        return self.next_id


@dataclass(slots=True)
class Harness:
    router: BridgeRouter
    max: FakeMax
    messages: MessageMapRepository


@pytest_asyncio.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    database = await Database.connect(tmp_path / "bridge.db")
    messages = MessageMapRepository(database)
    max_side = FakeMax()
    router = BridgeRouter(
        lookup=FakeLookup(),
        telegram=FakeTelegram(),
        max_sender=max_side,
        messages=messages,
        state=BridgeStateRepository(database),
        owner_chat_id=OWNER_CHAT,
        timestamp_style=TimestampStyle.OFF,
    )
    try:
        yield Harness(router=router, max=max_side, messages=messages)
    finally:
        await database.close()


async def test_a_shared_contact_reaches_max_as_a_contact(harness: Harness) -> None:
    vcard = "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Иван\r\nTEL;TYPE=CELL:+7999\r\nEND:VCARD"

    await harness.router.on_telegram_contact(
        bot_id=MOM.bot_id,
        telegram_chat_id=OWNER_CHAT,
        telegram_message_id=42,
        vcard=vcard,
    )

    assert harness.max.contacts == [{"chat": MOM.max_chat_id, "vcard": vcard, "user_id": None}]
    link = await harness.messages.by_telegram_message(MOM.bot_id, 42)
    assert link is not None and link.max_message_id == 501


async def test_a_contact_from_an_unbridged_bot_is_dropped(harness: Harness) -> None:
    await harness.router.on_telegram_contact(
        bot_id=999,
        telegram_chat_id=OWNER_CHAT,
        telegram_message_id=7,
        vcard="BEGIN:VCARD\r\nEND:VCARD",
    )

    assert harness.max.contacts == []


# ----------------------------------------------------- MTProto owner contact


def test_the_mtproto_intake_builds_a_vcard_from_a_contact_media() -> None:
    from bridge.telegram.mtproto_media import owner_contact_vcard

    class MessageMediaContact:
        def __init__(self, **kw: Any) -> None:
            self.__dict__.update(kw)

    # Patch the isinstance check by matching Telethon's type name.
    import bridge.telegram.mtproto_media as mm

    media = MessageMediaContact(phone_number="+79991234567", first_name="Иван",
                                last_name="Петров", vcard="")

    class Msg:
        pass

    msg = Msg()
    msg.media = media  # type: ignore[attr-defined]

    original = mm.types.MessageMediaContact
    mm.types.MessageMediaContact = MessageMediaContact  # type: ignore[misc]
    try:
        card = owner_contact_vcard(msg)
    finally:
        mm.types.MessageMediaContact = original  # type: ignore[misc]

    assert card is not None
    assert "FN:Иван Петров" in card
    assert "TEL;TYPE=CELL:+79991234567" in card


def test_a_non_contact_message_has_no_vcard() -> None:
    from bridge.telegram.mtproto_media import owner_contact_vcard

    class Msg:
        media = None

    assert owner_contact_vcard(Msg()) is None


# ---------------------------------------------------- PyMax model relaxation


def test_a_vcard_contact_without_a_max_id_parses_through_pymax() -> None:
    """PyMax's `ContactAttachment.contactId` is declared required, but a contact
    shared as a raw vCard has no MAX user id. Left strict, one such message in a
    chat's last-message slot fails the whole login — the same class of failure a
    custom sticker caused. Measured against a real frame on 2026-08-04."""
    from pymax.types.domain.message import Message

    import bridge.max_client  # noqa: F401 - importing applies the repair

    frame = {
        "_type": "CONTACT",
        "firstName": "Ivan",
        "lastName": "Petrov",
        "name": "Ivan",
        "vcfBody": "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Ivan Petrov\r\nEND:VCARD",
    }

    message = Message.model_validate(
        {"id": 1, "time": 1, "sender": 1, "type": "USER", "text": "", "attaches": [frame]}
    )
    assert type(message.attaches[0]).__name__ == "ContactAttachment"
    assert message.attaches[0].contact_id is None
