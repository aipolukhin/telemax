"""Forwarded messages, in both directions.

The MAX half is a protocol fact rather than a rendering choice: a forward
arrives as an empty envelope with a `link` of type `FORWARD` holding the whole
original. Before this was read, the envelope rendered as an empty string and was
dropped *after* the dedup claim — so the message was lost and the replay would
not bring it back. The tests below pin the unwrap first and the wording second.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest_asyncio

from bridge.config import TimestampStyle
from bridge.formatting import (
    FORWARD_ANONYMOUS,
    FORWARD_FROM,
    forward_header,
    forward_prefix,
    utf16_length,
)
from bridge.max_client import normalize_message
from bridge.media.upload import Album, AlbumCollector, plan_upload
from bridge.routing import BridgeRouter, BridgeTarget
from bridge.storage import BridgeStateRepository, Database, MessageMapRepository
from bridge.telegram.forwards import (
    author_of_entity,
    bot_api_forward_date,
    bot_api_forward_origin,
    forward_peer_id,
    is_bot_api_forward,
    is_mtproto_forward,
    mtproto_forward_date,
    mtproto_forward_name,
    name_of_entity,
)

MOM = BridgeTarget(name="mom", max_chat_id=777, bot_id=100)
OWNER_CHAT = 111
CONTACT = 4242
OWNER_MAX_ID = 100000002
AUTHOR = 909090


@dataclass(slots=True)
class FakeLookup:
    def bridge_for_max_chat(self, max_chat_id: int) -> BridgeTarget | None:
        return MOM if max_chat_id == MOM.max_chat_id else None

    def bridge_for_bot(self, bot_id: int) -> BridgeTarget | None:
        return MOM if bot_id == MOM.bot_id else None


@dataclass(slots=True)
class FakeTelegram:
    sent: list[str] = field(default_factory=list)
    entity_sets: list[list[dict[str, Any]] | None] = field(default_factory=list)
    next_id: int = 9000

    async def send_text(
        self,
        bot_id: int,
        chat_id: int,
        text: str,
        *,
        reply_to: int | None = None,
        entities: list[dict[str, Any]] | None = None,
    ) -> int | None:
        self.sent.append(text)
        self.entity_sets.append(entities)
        self.next_id += 1
        return self.next_id

    async def edit_text(self, bot_id: int, chat_id: int, message_id: int, text: str) -> bool:
        return True

    async def delete(self, bot_id: int, chat_id: int, message_id: int) -> bool:
        return True


@dataclass(slots=True)
class FakeMax:
    async def send_text(
        self, chat_id: int, text: str, *, reply_to: int | None = None
    ) -> int | None:
        return 1


@dataclass(slots=True)
class FakeNames:
    """Whatever MAX would answer for a user id, plus how often it was asked.

    The answer is `(own_name, profile_link)` — never the owner's address-book
    label, which is what `MaxContact.display_name` holds and what must not reach
    the far side of a forward.
    """

    answers: dict[int, tuple[str | None, str | None]] = field(default_factory=dict)
    calls: list[int] = field(default_factory=list)

    async def forward_author(self, user_id: int) -> tuple[str | None, str | None]:
        self.calls.append(user_id)
        return self.answers.get(user_id, (None, None))


@dataclass(slots=True)
class Harness:
    router: BridgeRouter
    telegram: FakeTelegram
    names: FakeNames


@pytest_asyncio.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    database = await Database.connect(tmp_path / "bridge.db")
    telegram = FakeTelegram()
    names = FakeNames(answers={AUTHOR: ("Аня", None)})
    router = BridgeRouter(
        lookup=FakeLookup(),
        telegram=telegram,
        max_sender=FakeMax(),
        messages=MessageMapRepository(database),
        state=BridgeStateRepository(database),
        owner_chat_id=OWNER_CHAT,
        timestamp_style=TimestampStyle.OFF,
        display_names=names,
    )
    try:
        yield Harness(router=router, telegram=telegram, names=names)
    finally:
        await database.close()


def forwarded(
    message_id: int,
    *,
    text: str = "",
    sender: int = CONTACT,
    author: int | None = AUTHOR,
    chat_name: str | None = None,
    original_time: int = 1,
    attaches: list[dict[str, Any]] | None = None,
    elements: list[dict[str, Any]] | None = None,
    envelope_time: int = 1,
) -> Any:
    """The shape the live protocol sends, as read off the official web client.

    The envelope carries no text and no attachments on purpose: that is what
    MAX actually puts on the wire, and a fixture that filled them in would test
    a message that never arrives.
    """
    inner: dict[str, Any] = {"id": 5000 + message_id, "text": text, "time": original_time}
    if author is not None:
        inner["sender"] = author
    if attaches is not None:
        inner["attaches"] = attaches
    if elements is not None:
        inner["elements"] = elements

    link: dict[str, Any] = {"type": "FORWARD", "chatId": 31337, "message": inner}
    if chat_name is not None:
        link["chatName"] = chat_name

    return normalize_message(
        {
            "id": message_id,
            "chatId": MOM.max_chat_id,
            "sender": sender,
            "text": "",
            "time": envelope_time,
            "attaches": [],
            "link": link,
        },
        own_user_id=OWNER_MAX_ID,
    )


# --------------------------------------------------------------- the unwrap


def test_forward_unwraps_the_original_body() -> None:
    message = forwarded(1, text="а помнишь мы на даче")

    assert message.text == "а помнишь мы на даче"
    assert message.forward is not None
    assert message.forward.sender_id == AUTHOR
    assert message.forward.source_chat_id == 31337
    # The envelope's identity is what routing and dedup work with.
    assert message.message_id == 1
    assert message.chat_id == MOM.max_chat_id


def test_forward_unwraps_the_original_attachments() -> None:
    message = forwarded(2, attaches=[{"_type": "PHOTO", "baseUrl": "https://cdn/x"}])

    assert [item.kind.value for item in message.attachments] == ["photo"]


def test_forward_carries_the_original_time_not_the_envelope_s() -> None:
    message = forwarded(3, text="старое", original_time=1_600_000_000_000, envelope_time=5)

    assert message.forward is not None
    assert message.forward.original_timestamp == 1_600_000_000_000
    assert message.timestamp == 5, "the envelope keeps its own time for ordering"


def test_a_chain_of_forwards_names_the_first_author() -> None:
    """MAX nests the same shape again; what the owner wants is who wrote it."""
    innermost = {"id": 1, "sender": 1234, "text": "оригинал", "time": 7}
    middle = {
        "id": 2,
        "sender": 5678,
        "text": "",
        "link": {"type": "FORWARD", "chatId": 22, "message": innermost},
    }
    message = normalize_message(
        {
            "id": 3,
            "chatId": MOM.max_chat_id,
            "sender": CONTACT,
            "text": "",
            "time": 9,
            "link": {"type": "FORWARD", "chatId": 33, "message": middle},
        },
        own_user_id=OWNER_MAX_ID,
    )

    assert message.text == "оригинал"
    assert message.forward is not None
    assert message.forward.sender_id == 1234


def test_a_forward_out_of_saved_messages_keeps_its_chat_id() -> None:
    """Chat zero is the saved-messages chat, not a missing id (protocol-notes §3).

    Measured in compatibility fixtures 2026-08-04: account B forwarded a video out of its own
    saved messages and the link carried `chatId: 0`. Read with `or`, that became
    None — and forwarding something out of saved messages is one of the
    commonest forwards there is.
    """
    message = normalize_message(
        {
            "id": 9,
            "chatId": MOM.max_chat_id,
            "sender": CONTACT,
            "text": "",
            "time": 1,
            "link": {
                "type": "FORWARD",
                "chatId": 0,
                "message": {"id": 77, "sender": AUTHOR, "text": "из избранного", "time": 2},
            },
        },
        own_user_id=OWNER_MAX_ID,
    )

    assert message.forward is not None
    assert message.forward.source_chat_id == 0


def test_a_forward_link_with_no_message_leaves_the_envelope_alone() -> None:
    message = normalize_message(
        {
            "id": 4,
            "chatId": MOM.max_chat_id,
            "sender": CONTACT,
            "text": "что-то",
            "time": 1,
            "link": {"type": "FORWARD", "chatId": 33},
        },
        own_user_id=OWNER_MAX_ID,
    )

    assert message.forward is None
    assert message.text == "что-то"


def test_a_reply_is_still_a_reply() -> None:
    message = normalize_message(
        {
            "id": 5,
            "chatId": MOM.max_chat_id,
            "sender": CONTACT,
            "text": "ага",
            "time": 1,
            "link": {"type": "REPLY", "message": {"id": 4}},
        },
        own_user_id=OWNER_MAX_ID,
    )

    assert message.forward is None
    assert message.reply_to_message_id == 4


# ------------------------------------------------------------- MAX -> Telegram


async def test_forwarded_message_reaches_telegram_marked(harness: Harness) -> None:
    await harness.router.on_max_message(forwarded(1, text="а помнишь мы на даче"))

    assert harness.telegram.sent == [f"{FORWARD_FROM} Аня\nа помнишь мы на даче"]


async def test_forward_used_to_be_dropped(harness: Harness) -> None:
    """The regression this exists for: an empty envelope delivered nothing."""
    await harness.router.on_max_message(forwarded(1, text="важное"))

    assert harness.telegram.sent, "a forwarded message must not vanish"


async def test_channel_forward_uses_the_label_max_itself_sent(harness: Harness) -> None:
    await harness.router.on_max_message(
        forwarded(2, text="новость", chat_name="Новости", author=None)
    )

    assert harness.telegram.sent == [f"{FORWARD_FROM} Новости\nновость"]
    assert harness.names.calls == [], "a labelled forward must not cost a lookup"


async def test_forward_from_someone_max_will_not_name(harness: Harness) -> None:
    await harness.router.on_max_message(forwarded(3, text="аноним", author=None))

    assert harness.telegram.sent == [f"{FORWARD_ANONYMOUS}\nаноним"]


async def test_the_author_is_looked_up_once_per_id(harness: Harness) -> None:
    await harness.router.on_max_message(forwarded(4, text="раз"))
    await harness.router.on_max_message(forwarded(5, text="два"))

    assert harness.names.calls == [AUTHOR]


async def test_the_forward_line_is_italic_and_the_body_keeps_its_formatting(
    harness: Harness,
) -> None:
    await harness.router.on_max_message(
        forwarded(6, text="жирно", elements=[{"type": "STRONG", "from": 0, "length": 5}])
    )

    header = forward_header("Аня")
    entities = harness.telegram.entity_sets[0]
    assert entities == [
        {"type": "italic", "offset": 0, "length": utf16_length(header.rstrip("\n"))},
        {"type": "bold", "offset": utf16_length(header), "length": 5},
    ]


async def test_an_emoji_in_the_author_s_name_does_not_shift_the_formatting(
    tmp_path: Path,
) -> None:
    """UTF-16 code units, not code points: an emoji is two of the former."""
    database = await Database.connect(tmp_path / "bridge.db")
    telegram = FakeTelegram()
    router = BridgeRouter(
        lookup=FakeLookup(),
        telegram=telegram,
        max_sender=FakeMax(),
        messages=MessageMapRepository(database),
        state=BridgeStateRepository(database),
        owner_chat_id=OWNER_CHAT,
        timestamp_style=TimestampStyle.OFF,
        display_names=FakeNames(answers={AUTHOR: ("Аня 🌸", None)}),
    )
    try:
        await router.on_max_message(
            forwarded(7, text="жирно", elements=[{"type": "STRONG", "from": 0, "length": 5}])
        )
    finally:
        await database.close()

    text = telegram.sent[0]
    bold = next(item for item in telegram.entity_sets[0] or [] if item["type"] == "bold")
    units = text.encode("utf-16-le")
    start = bold["offset"] * 2
    end = start + bold["length"] * 2
    assert units[start:end].decode("utf-16-le") == "жирно"


async def test_a_forward_with_nothing_renderable_still_arrives(harness: Harness) -> None:
    await harness.router.on_max_message(forwarded(8, text=""))

    assert harness.telegram.sent == [f"{FORWARD_FROM} Аня"]


# ------------------------------------------------------------- Telegram -> MAX


@dataclass
class FakeUser:
    full_name: str


@dataclass
class FakeChat:
    title: str


@dataclass
class FakeOriginUser:
    sender_user: FakeUser


@dataclass
class FakeOriginHidden:
    sender_user_name: str


@dataclass
class FakeOriginChannel:
    chat: FakeChat
    author_signature: str | None = None


@dataclass
class FakeForwardedMessage:
    forward_origin: Any = None


def test_bot_api_origin_names_a_person() -> None:
    message = FakeForwardedMessage(FakeOriginUser(FakeUser("Иван Петров")))

    assert is_bot_api_forward(message)
    assert bot_api_forward_origin(message) == "Иван Петров"


def test_bot_api_origin_names_a_hidden_person() -> None:
    message = FakeForwardedMessage(FakeOriginHidden("Кто-то"))

    assert bot_api_forward_origin(message) == "Кто-то"


def test_bot_api_origin_names_a_channel_and_its_signature() -> None:
    message = FakeForwardedMessage(FakeOriginChannel(FakeChat("Новости"), "Редакция"))

    assert bot_api_forward_origin(message) == "Новости (Редакция)"


def test_a_message_that_is_not_forwarded_has_no_origin() -> None:
    message = FakeForwardedMessage()

    assert not is_bot_api_forward(message)
    assert bot_api_forward_origin(message) is None


async def test_a_forwarded_album_keeps_both_its_origin_and_its_caption() -> None:
    """The origin is on every part; the caption is on one. Merging them loses one.

    Telegram puts the caption on whichever part the owner typed it on, so a
    forward line folded into the caption would make the *first* part look like
    it already carried one — and the real caption, arriving on part two, would
    be dropped as a duplicate.
    """
    flushed: list[Album] = []

    async def flush(group_id: str, album: Album) -> None:
        flushed.append(album)

    collector = AlbumCollector(flush=flush, window_seconds=0.05)
    line = forward_header("Иван")
    for index in range(3):
        await collector.add(
            "group-fwd",
            plan_upload(photo=True, file_id=f"f{index}", file_name="photo.jpg"),
            caption="три фото" if index == 1 else "",
            reply_to=None,
            message_id=100 + index,
            forward_line=line,
        )

    await asyncio.sleep(0.2)

    assert len(flushed) == 1
    assert flushed[0].forward_line == line
    assert flushed[0].caption == "три фото"


@dataclass
class FakeFwdHeader:
    from_name: str | None = None
    from_id: Any = None
    post_author: str | None = None


@dataclass
class FakeMtprotoMessage:
    fwd_from: Any = None


def test_mtproto_forward_names_a_hidden_sender() -> None:
    message = FakeMtprotoMessage(FakeFwdHeader(from_name="Кто-то"))

    assert is_mtproto_forward(message)
    assert mtproto_forward_name(message) == "Кто-то"


def test_mtproto_forward_prefers_the_resolved_sender_over_the_raw_header() -> None:
    """Measured in compatibility fixtures: the raw header names nobody, the wrapper does.

    `fwd_from.from_name` is only set for a *hidden* sender, so reading the raw
    header first made every ordinary forward over this transport anonymous.
    Telethon's `message.forward` carries the sender Telegram shipped inside the
    update, which is where the name actually is.
    """

    @dataclass
    class Sender:
        first_name: str
        last_name: str | None = None

    @dataclass
    class TelethonForward:
        sender: Any = None
        chat: Any = None
        from_name: str | None = None
        from_id: Any = None
        post_author: str | None = None

    @dataclass
    class Message:
        forward: Any
        fwd_from: Any

    message = Message(
        forward=TelethonForward(sender=Sender("Иван", "Петров")),
        fwd_from=FakeFwdHeader(from_id=object()),
    )

    assert mtproto_forward_name(message) == "Иван Петров"


def test_mtproto_forward_names_a_channel() -> None:
    @dataclass
    class Channel:
        title: str

    @dataclass
    class TelethonForward:
        chat: Any = None
        sender: Any = None
        from_name: str | None = None
        from_id: Any = None
        post_author: str | None = None

    @dataclass
    class Message:
        forward: Any

    assert mtproto_forward_name(Message(TelethonForward(chat=Channel("Новости")))) == "Новости"


def test_mtproto_forward_resolves_a_peer_the_caller_already_knows() -> None:
    @dataclass
    class PeerUser:
        user_id: int

    message = FakeMtprotoMessage(FakeFwdHeader(from_id=PeerUser(55)))

    assert mtproto_forward_name(message, names={55: "Иван"}) == "Иван"


def test_the_author_peer_is_read_for_looking_up_rather_than_the_name() -> None:
    """The name in the update is the author as *this* account has them saved.

    An address-book rename would travel to the contact as the owner's private
    label for a stranger. The peer id is the part worth keeping: it is what the
    author's own account can be asked about.
    """

    @dataclass
    class PeerUser:
        user_id: int

    @dataclass
    class TelethonForward:
        from_id: Any = None
        sender: Any = None
        chat: Any = None

    @dataclass
    class Message:
        forward: Any

    assert forward_peer_id(Message(TelethonForward(from_id=PeerUser(4242)))) == 4242


def test_a_hidden_sender_has_no_account_to_ask() -> None:
    """No id at all — only the name they had at the time, which is on the update."""

    @dataclass
    class TelethonForward:
        from_id: Any = None
        from_name: str = "Кто-то"
        sender: Any = None
        chat: Any = None

    @dataclass
    class Message:
        forward: Any

    message = Message(TelethonForward())
    assert forward_peer_id(message) is None
    assert mtproto_forward_name(message) == "Кто-то"


def test_an_entity_becomes_one_name() -> None:
    @dataclass
    class User:
        first_name: str
        last_name: str | None = None
        username: str | None = None

    @dataclass
    class Channel:
        title: str

    assert name_of_entity(User("Иван", "Петров")) == "Иван Петров"
    assert name_of_entity(Channel("Новости")) == "Новости"
    assert name_of_entity(User("", None, "ivan")) == "@ivan"
    assert name_of_entity(None) is None


# ------------------------------------------------- the original's own time


def test_the_forward_line_carries_the_original_s_time() -> None:
    """Symmetry with MAX → TG, where the stamp has always been the original's."""
    written = datetime(2026, 7, 28, 9, 14, tzinfo=UTC)

    line = forward_prefix(
        "Аня",
        at_ms=int(written.timestamp() * 1000),
        style=TimestampStyle.COMPACT,
        tz=UTC,
    )

    assert line == f"[28/07 09:14] {FORWARD_FROM} Аня\n"


def test_a_forward_of_something_just_written_needs_no_clock() -> None:
    """The messenger already stamps it with now; a second clock says nothing."""
    line = forward_prefix(
        "Аня",
        at_ms=int(datetime.now(UTC).timestamp() * 1000),
        style=TimestampStyle.COMPACT,
        tz=UTC,
    )

    assert line == f"{FORWARD_FROM} Аня\n"


def test_bot_api_forward_date_is_read_in_milliseconds() -> None:
    @dataclass
    class Origin:
        date: Any
        sender_user: Any = None

    written = datetime(2026, 7, 28, 9, 14, tzinfo=UTC)
    message = FakeForwardedMessage(Origin(date=written))

    assert bot_api_forward_date(message) == int(written.timestamp() * 1000)


def test_mtproto_forward_date_is_read_in_milliseconds() -> None:
    @dataclass
    class TelethonForward:
        date: Any
        sender: Any = None
        chat: Any = None

    @dataclass
    class Message:
        forward: Any

    written = datetime(2026, 7, 28, 9, 14, tzinfo=UTC)

    assert mtproto_forward_date(Message(TelethonForward(date=written))) == int(
        written.timestamp() * 1000
    )


def test_a_message_that_is_not_forwarded_has_no_date() -> None:
    assert bot_api_forward_date(FakeForwardedMessage()) is None


def test_mtproto_forward_with_an_unresolvable_peer_is_anonymous() -> None:
    @dataclass
    class PeerUser:
        user_id: int

    message = FakeMtprotoMessage(FakeFwdHeader(from_id=PeerUser(55)))

    assert mtproto_forward_name(message) is None
    assert forward_header(None) == f"{FORWARD_ANONYMOUS}\n"


# --------------------------------------------- the author's own name and link


def test_the_name_is_a_link_to_the_author_s_profile() -> None:
    line = forward_prefix("Anna Example", username="profile_example", at_ms=None)

    assert line == f"{FORWARD_FROM} [Anna Example](https://t.me/profile_example)\n"


def test_with_no_name_the_address_is_the_label() -> None:
    """A saved contact contributes only their handle — see `author_of_entity`."""
    line = forward_prefix(None, username="profile_example", at_ms=None)

    assert line == f"{FORWARD_FROM} [t.me/profile_example](https://t.me/profile_example)\n"


def test_with_no_username_the_name_stays_plain() -> None:
    line = forward_prefix("Иван Петров", at_ms=None)

    assert line == f"{FORWARD_FROM} Иван Петров\n"


def test_brackets_in_a_name_cannot_break_the_link() -> None:
    """PyMax's markdown has no escapes: a `]` would end the link early."""
    line = forward_prefix("Аня [работа]", username="anya", at_ms=None)

    assert line == f"{FORWARD_FROM} [Аня работа](https://t.me/anya)\n"
    assert line.count("]") == 1


def test_a_saved_contact_contributes_only_their_handle() -> None:
    """Every name this session can get for a contact is the owner's own label.

    Verified against four server paths in compatibility fixtures (2026-08-04): `users.getUsers`,
    `contacts.resolveUsername`, `contacts.search` and `contacts.getContacts` all
    answered «Контакт» for a profile that says `Anna Example`.
    """

    @dataclass
    class User:
        first_name: str
        username: str | None = None
        contact: bool = False

    saved = author_of_entity(User("Контакт", "profile_example", contact=True))
    assert saved.name is None, "the owner's private label must not travel"
    assert saved.username == "profile_example"

    stranger = author_of_entity(User("Anna", "profile_example", contact=False))
    assert stranger.name == "Anna", "not saved, so the name is genuinely theirs"


def test_a_channel_keeps_its_title() -> None:
    @dataclass
    class Channel:
        title: str
        username: str | None = None

    author = author_of_entity(Channel("Новости", "news"))

    assert author.name == "Новости"
    assert author.username == "news"


async def test_a_bot_s_view_of_an_author_is_recorded_and_read_back(tmp_path: Path) -> None:
    """The whole point of the table: carry a name between two transports.

    A bot has no address book, so what it is told is the profile itself — the
    one thing the owner's own session is structurally unable to report.
    """
    from bridge.storage import ForwardAuthorRepository
    from bridge.telegram.forward_authors import LearnForwardAuthors, author_of

    @dataclass
    class User:
        id: int
        first_name: str
        last_name: str | None = None
        username: str | None = None

    @dataclass
    class Origin:
        sender_user: Any

    @dataclass
    class Msg:
        forward_origin: Any

    @dataclass
    class Update:
        message: Any
        edited_message: Any = None

    message = Msg(Origin(User(200000005, "Anna", "Example", "profile_example")))
    assert author_of(message) == (200000005, "Anna Example", "profile_example")

    database = await Database.connect(tmp_path / "bridge.db")
    try:
        authors = ForwardAuthorRepository(database)
        seen: list[str] = []

        async def handler(event: Any, data: dict[str, Any]) -> str:
            seen.append("handled")
            return "ok"

        middleware = LearnForwardAuthors(authors)
        result = await middleware(handler, Update(message), {})

        assert result == "ok" and seen == ["handled"], "it observes, never consumes"
        assert await authors.author_of(200000005) == ("Anna Example", "profile_example")
        assert await authors.author_of(999) is None
    finally:
        await database.close()


async def test_an_update_that_is_not_a_forward_teaches_nothing(tmp_path: Path) -> None:
    from bridge.storage import ForwardAuthorRepository
    from bridge.telegram.forward_authors import LearnForwardAuthors

    @dataclass
    class Msg:
        forward_origin: Any = None

    @dataclass
    class Update:
        message: Any
        edited_message: Any = None

    database = await Database.connect(tmp_path / "bridge.db")
    try:
        authors = ForwardAuthorRepository(database)

        async def handler(event: Any, data: dict[str, Any]) -> str:
            return "ok"

        assert await LearnForwardAuthors(authors)(handler, Update(Msg()), {}) == "ok"
    finally:
        await database.close()


# ------------------------------------------ MAX → TG: own name and MAX profile


def test_a_max_contact_carries_both_names_apart() -> None:
    """`CUSTOM` is the owner's label, `ONEME` is the person's own.

    Measured in compatibility fixtures 2026-08-04: `CUSTOM: "Мама"` / `ONEME: "Имя профиля"`,
    `CUSTOM: "Иван"` / `ONEME: "Profile Name"`. `display_name` keeps preferring the
    label — it is the name on the contact's own bot, which the owner chose —
    and only a forward reaches for the other one.
    """
    from bridge.max_client import normalize_contact

    contact = normalize_contact(
        {
            "id": 200000006,
            "names": [
                {"type": "CUSTOM", "name": "Мама", "firstName": "Мама"},
                {"type": "ONEME", "name": "Имя профиля", "firstName": "Имя профиля"},
            ],
        },
        user_id=200000006,
    )

    assert contact.display_name == "Мама"
    assert contact.own_name == "Имя профиля"
    assert contact.profile_link is None


def test_a_max_profile_link_is_read_when_there_is_one() -> None:
    """A full URL already, not a handle — `https://max.ru/maxbot`."""
    from bridge.max_client import normalize_contact

    contact = normalize_contact(
        {
            "id": 543835,
            "names": [{"type": "ONEME", "name": "MAX"}],
            "link": "https://max.ru/maxbot",
        },
        user_id=543835,
    )

    assert contact.own_name == "MAX"
    assert contact.profile_link == "https://max.ru/maxbot"


async def test_the_forward_names_the_author_as_they_call_themselves(tmp_path: Path) -> None:
    database = await Database.connect(tmp_path / "bridge.db")
    telegram = FakeTelegram()
    router = BridgeRouter(
        lookup=FakeLookup(),
        telegram=telegram,
        max_sender=FakeMax(),
        messages=MessageMapRepository(database),
        state=BridgeStateRepository(database),
        owner_chat_id=OWNER_CHAT,
        timestamp_style=TimestampStyle.OFF,
        display_names=FakeNames(answers={AUTHOR: ("Имя профиля", "https://max.ru/sveta")}),
    )
    try:
        await router.on_max_message(forwarded(20, text="привет"))
    finally:
        await database.close()

    assert telegram.sent == [f"{FORWARD_FROM} Имя профиля\nпривет"]

    entities = telegram.entity_sets[0] or []
    header = f"{FORWARD_FROM} Имя профиля"
    assert {"type": "italic", "offset": 0, "length": utf16_length(header)} in entities
    # The link covers the name alone: «Переслано от» is our own wording and
    # points at nothing.
    link = next(item for item in entities if item["type"] == "text_link")
    assert link["url"] == "https://max.ru/sveta"
    body = telegram.sent[0].encode("utf-16-le")
    start, end = link["offset"] * 2, (link["offset"] + link["length"]) * 2
    assert body[start:end].decode("utf-16-le") == "Имя профиля"


async def test_an_author_with_no_max_link_is_named_but_not_linked(tmp_path: Path) -> None:
    """Most people never set one: of five users measured, only MAX itself had."""
    database = await Database.connect(tmp_path / "bridge.db")
    telegram = FakeTelegram()
    router = BridgeRouter(
        lookup=FakeLookup(),
        telegram=telegram,
        max_sender=FakeMax(),
        messages=MessageMapRepository(database),
        state=BridgeStateRepository(database),
        owner_chat_id=OWNER_CHAT,
        timestamp_style=TimestampStyle.OFF,
        display_names=FakeNames(answers={AUTHOR: ("Имя профиля", None)}),
    )
    try:
        await router.on_max_message(forwarded(21, text="привет"))
    finally:
        await database.close()

    entities = telegram.entity_sets[0] or []
    assert [item["type"] for item in entities] == ["italic"]


async def test_an_author_max_will_not_name_stays_anonymous(tmp_path: Path) -> None:
    """Nothing is better than the owner's private label for somebody."""
    database = await Database.connect(tmp_path / "bridge.db")
    telegram = FakeTelegram()
    router = BridgeRouter(
        lookup=FakeLookup(),
        telegram=telegram,
        max_sender=FakeMax(),
        messages=MessageMapRepository(database),
        state=BridgeStateRepository(database),
        owner_chat_id=OWNER_CHAT,
        timestamp_style=TimestampStyle.OFF,
        display_names=FakeNames(),
    )
    try:
        await router.on_max_message(forwarded(22, text="привет"))
    finally:
        await database.close()

    assert telegram.sent == [f"{FORWARD_ANONYMOUS}\nпривет"]
