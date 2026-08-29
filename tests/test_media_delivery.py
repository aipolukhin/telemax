"""WP8 — every MAX attachment kind on the right Bot API method.

The mapping is the point. A voice message sent as a document loses its waveform
and its play button, a video note sent as a video stops being a circle, and a
track sent as a document loses its artist — so each kind is pinned here, along
with the two rules that decide the shape of a delivery: photos and videos travel
as one album, and a failure becomes a visible line rather than silence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from bridge.max_client import AttachmentKind, MaxAttachment, normalize_message
from bridge.max_client.events import normalize_attachment
from bridge.media import MaxMediaSources, MediaPipeline, TempFiles
from bridge.media.delivery import (
    CAPTION_LIMIT,
    UNKNOWN_NOTICE,
    AlbumReceipt,
    MaxMediaDelivery,
    OutgoingMedia,
    album_attachments,
    call_text,
    ms_to_seconds,
)
from tests.test_media import JPEG, MP4, OGG, PDF, FakeProtocol, FakeSession, fetcher_for


class MixedSession(FakeSession):
    """Serves bytes that match the URL, so a mixed message can be tested."""

    def get(self, url: str) -> Any:
        from tests.test_media import FakeResponse

        self.requested.append(url)
        if ".ogg" in url or "audio" in url:
            return FakeResponse(OGG, {"Content-Type": "audio/ogg"})
        if ".mp4" in url or "video" in url:
            return FakeResponse(MP4, {"Content-Type": "video/mp4"})
        return FakeResponse(JPEG, {"Content-Type": "image/jpeg"})

BOT = 4242
CHAT = 100000001


@dataclass
class FakeSender:
    calls: list[tuple[str, OutgoingMedia | list[OutgoingMedia]]] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    next_id: int = 500

    def _record(self, method: str, media: Any) -> int:
        self.calls.append((method, media))
        self.next_id += 1
        return self.next_id

    async def send_photo(self, bot_id: int, chat_id: int, media: OutgoingMedia,
                         *, reply_to: int | None) -> int | None:
        return self._record("photo", media)

    async def send_video(self, bot_id: int, chat_id: int, media: OutgoingMedia,
                         *, reply_to: int | None) -> int | None:
        return self._record("video", media)

    async def send_video_note(self, bot_id: int, chat_id: int, media: OutgoingMedia,
                              *, reply_to: int | None) -> int | None:
        return self._record("video_note", media)

    async def send_voice(self, bot_id: int, chat_id: int, media: OutgoingMedia,
                         *, reply_to: int | None) -> int | None:
        return self._record("voice", media)

    async def send_audio(self, bot_id: int, chat_id: int, media: OutgoingMedia,
                         *, reply_to: int | None) -> int | None:
        return self._record("audio", media)

    async def send_document(self, bot_id: int, chat_id: int, media: OutgoingMedia,
                            *, reply_to: int | None) -> int | None:
        return self._record("document", media)

    async def send_sticker(self, bot_id: int, chat_id: int, media: OutgoingMedia,
                           *, reply_to: int | None) -> int | None:
        return self._record("sticker", media)

    async def send_album(self, bot_id: int, chat_id: int, items: list[OutgoingMedia],
                         *, reply_to: int | None) -> AlbumReceipt | None:
        # One id per part, in order — the shape the Bot API answers with, and the
        # only place the id of the second photo ever appears.
        self.calls.append(("album", items))
        first = self.next_id + 1
        self.next_id += len(items)
        return AlbumReceipt(
            message_ids=tuple(range(first, first + len(items))),
            media_group_id="13984172040192",
        )

    async def send_text(self, bot_id: int, chat_id: int, text: str, *,
                        reply_to: int | None = None,
                        entities: list[dict[str, Any]] | None = None) -> int | None:
        self.texts.append(text)
        self.next_id += 1
        return self.next_id

    async def send_contact(self, bot_id: int, chat_id: int, *, phone: str, first_name: str,
                           last_name: str | None = None, vcard: str | None = None,
                           reply_to: int | None = None) -> int | None:
        self.calls.append(("contact", {"phone": phone, "first_name": first_name,
                                        "last_name": last_name, "vcard": vcard}))
        self.next_id += 1
        return self.next_id

    @property
    def methods(self) -> list[str]:
        return [name for name, _ in self.calls]


def message_with(*attachments: MaxAttachment, text: str = "") -> Any:
    payload = {
        "id": 1,
        "chatId": 777,
        "sender": 99,
        "text": text,
        "time": 1_785_000_000_000,
    }
    base = normalize_message(payload, own_user_id=1)
    from dataclasses import replace

    return replace(base, attachments=tuple(attachments))


def delivery_for(tmp_path: Path, protocol: FakeProtocol, body: bytes, sender: FakeSender) -> Any:
    pipeline = MediaPipeline(
        sources=MaxMediaSources(protocol),
        temp_files=TempFiles(tmp_path / "tmp"),
        fetcher=fetcher_for(FakeSession(body=body)),
        max_file_size_mb=1,
    )
    return MaxMediaDelivery(pipeline=pipeline, sender=sender)


def test_durations_are_converted_to_seconds() -> None:
    """MAX counts milliseconds; Telegram wants seconds and rounds badly on zero."""
    assert ms_to_seconds(1579) == 2
    assert ms_to_seconds(2100) == 2
    assert ms_to_seconds(400) == 1, "a short voice message is not a zero-second one"
    assert ms_to_seconds(None) is None


async def test_a_voice_message_goes_to_send_voice(tmp_path: Path) -> None:
    sender = FakeSender()
    protocol = FakeProtocol(audio={"url": "https://a.oneme.ru/v.ogg"})
    attachment = MaxAttachment(
        kind=AttachmentKind.VOICE, duration_ms=1579, raw={"audioId": 5, "token": "t"}
    )

    await delivery_for(tmp_path, protocol, OGG, sender).deliver(
        message_with(attachment), bot_id=BOT, chat_id=CHAT
    )

    assert sender.methods == ["voice"]
    assert sender.calls[0][1].duration_seconds == 2


async def test_a_circle_goes_to_send_video_note(tmp_path: Path) -> None:
    sender = FakeSender()
    protocol = FakeProtocol(video={"MP4_480": "https://v.oneme.ru/videoMsg?x=1"})
    attachment = MaxAttachment(
        kind=AttachmentKind.VIDEO_NOTE,
        duration_ms=2100,
        width=480,
        height=480,
        raw={"videoId": 9},
    )

    await delivery_for(tmp_path, protocol, MP4, sender).deliver(
        message_with(attachment), bot_id=BOT, chat_id=CHAT
    )

    assert sender.methods == ["video_note"]


async def test_music_keeps_its_artist(tmp_path: Path) -> None:
    sender = FakeSender()
    protocol = FakeProtocol(file={"url": "https://fu.oneme.ru/track"})
    attachment = MaxAttachment(
        kind=AttachmentKind.MUSIC,
        duration_ms=275_000,
        title="Song",
        performer="Someone",
        raw={"fileId": 7},
    )

    await delivery_for(tmp_path, protocol, OGG, sender).deliver(
        message_with(attachment), bot_id=BOT, chat_id=CHAT
    )

    assert sender.methods == ["audio"]
    media = sender.calls[0][1]
    assert (media.title, media.performer) == ("Song", "Someone")
    assert media.duration_seconds == 275


async def test_a_document_keeps_its_name(tmp_path: Path) -> None:
    sender = FakeSender()
    protocol = FakeProtocol(file={"url": "https://fu.oneme.ru/d"})
    attachment = MaxAttachment(
        kind=AttachmentKind.FILE, file_name="отчёт.pdf", raw={"fileId": 7}
    )

    await delivery_for(tmp_path, protocol, PDF, sender).deliver(
        message_with(attachment), bot_id=BOT, chat_id=CHAT
    )

    assert sender.methods == ["document"]
    assert sender.calls[0][1].file_name == "отчёт.pdf"


async def test_photos_travel_as_one_album(tmp_path: Path) -> None:
    sender = FakeSender()
    protocol = FakeProtocol()
    photos = [
        MaxAttachment(kind=AttachmentKind.PHOTO, raw={"baseUrl": f"https://cdn/p{index}"})
        for index in range(3)
    ]

    await delivery_for(tmp_path, protocol, JPEG, sender).deliver(
        message_with(*photos), bot_id=BOT, chat_id=CHAT, caption="три фото"
    )

    assert sender.methods == ["album"]
    items = sender.calls[0][1]
    assert len(items) == 3
    assert items[0].caption == "три фото", "Telegram shows an album caption on the first item"
    assert all(item.caption is None for item in items[1:])


async def test_a_single_photo_is_not_an_album_of_one(tmp_path: Path) -> None:
    sender = FakeSender()
    protocol = FakeProtocol()
    photo = MaxAttachment(kind=AttachmentKind.PHOTO, raw={"baseUrl": "https://cdn/p"})

    await delivery_for(tmp_path, protocol, JPEG, sender).deliver(
        message_with(photo), bot_id=BOT, chat_id=CHAT
    )

    assert sender.methods == ["photo"]


async def test_a_voice_next_to_photos_follows_the_album(tmp_path: Path) -> None:
    """Only photos and videos can share a group; the rest keep their order."""
    sender = FakeSender()
    protocol = FakeProtocol(audio={"url": "https://a.oneme.ru/v.ogg"})
    attachments = [
        MaxAttachment(kind=AttachmentKind.PHOTO, raw={"baseUrl": "https://cdn/p1"}),
        MaxAttachment(kind=AttachmentKind.PHOTO, raw={"baseUrl": "https://cdn/p2"}),
        MaxAttachment(kind=AttachmentKind.VOICE, raw={"audioId": 5}),
    ]

    pipeline = MediaPipeline(
        sources=MaxMediaSources(protocol),
        temp_files=TempFiles(tmp_path / "tmp"),
        fetcher=fetcher_for(MixedSession()),
        max_file_size_mb=1,
    )
    await MaxMediaDelivery(pipeline=pipeline, sender=sender).deliver(
        message_with(*attachments), bot_id=BOT, chat_id=CHAT
    )

    assert sender.methods == ["album", "voice"]


async def test_a_long_caption_follows_as_its_own_message(tmp_path: Path) -> None:
    sender = FakeSender()
    protocol = FakeProtocol()
    photo = MaxAttachment(kind=AttachmentKind.PHOTO, raw={"baseUrl": "https://cdn/p"})
    long_text = "я" * (CAPTION_LIMIT + 10)

    await delivery_for(tmp_path, protocol, JPEG, sender).deliver(
        message_with(photo), bot_id=BOT, chat_id=CHAT, caption=long_text
    )

    assert sender.methods == ["photo"]
    assert sender.calls[0][1].caption is None
    assert sender.texts == [long_text], "the text must not be cut in half"


async def test_an_attachment_that_cannot_be_fetched_says_so(tmp_path: Path) -> None:
    """Silence would leave the owner thinking nothing was sent."""
    sender = FakeSender()
    protocol = FakeProtocol(file={})
    attachment = MaxAttachment(kind=AttachmentKind.FILE, raw={"fileId": 7})

    await delivery_for(tmp_path, protocol, PDF, sender).deliver(
        message_with(attachment), bot_id=BOT, chat_id=CHAT
    )

    assert sender.methods == []
    assert sender.texts and "не удалось скачать" in sender.texts[0]


async def test_an_oversized_attachment_names_the_limit(tmp_path: Path) -> None:
    sender = FakeSender()
    protocol = FakeProtocol(file={"url": "https://fu.oneme.ru/d"})
    attachment = MaxAttachment(
        kind=AttachmentKind.FILE, size=50 * 1024 * 1024, raw={"fileId": 7}
    )

    await delivery_for(tmp_path, protocol, PDF, sender).deliver(
        message_with(attachment), bot_id=BOT, chat_id=CHAT
    )

    assert sender.texts and "больше лимита" in sender.texts[0]


async def test_a_sticker_travels_as_a_sticker(tmp_path: Path) -> None:
    """A sticker needs no protocol round trip: `url` is already the picture.

    Sending it as a photo would work and would be wrong — a sticker in Telegram
    has no bubble, no border and its own size, and that is most of what makes it
    read as a sticker rather than as a small square picture.
    """
    sender = FakeSender()
    attachment = MaxAttachment(
        kind=AttachmentKind.STICKER,
        raw={"stickerId": 3, "url": "https://st.oneme.ru/sticker/3.webp"},
    )

    await delivery_for(tmp_path, FakeProtocol(), JPEG, sender).deliver(
        message_with(attachment), bot_id=BOT, chat_id=CHAT
    )

    assert sender.methods == ["sticker"]
    assert sender.texts == []


async def test_a_sticker_without_a_url_is_named_rather_than_dropped(tmp_path: Path) -> None:
    """The catalogue is MAX's, and an entry can arrive with nothing to fetch."""
    sender = FakeSender()
    attachment = MaxAttachment(kind=AttachmentKind.STICKER, raw={"stickerId": 3})

    await delivery_for(tmp_path, FakeProtocol(), JPEG, sender).deliver(
        message_with(attachment), bot_id=BOT, chat_id=CHAT
    )

    assert sender.methods == []
    assert sender.texts and "стикер" in sender.texts[0]


async def test_text_beside_a_sticker_follows_it_instead_of_being_lost(tmp_path: Path) -> None:
    """`sendSticker` has no caption parameter, so the text cannot ride along."""
    sender = FakeSender()
    attachment = MaxAttachment(
        kind=AttachmentKind.STICKER,
        raw={"stickerId": 3, "url": "https://st.oneme.ru/sticker/3.webp"},
    )

    await delivery_for(tmp_path, FakeProtocol(), JPEG, sender).deliver(
        message_with(attachment), bot_id=BOT, chat_id=CHAT, caption="держи"
    )

    assert sender.methods == ["sticker"]
    assert sender.texts == ["держи"]


# ----------------------------------------------------------------------- calls


async def test_a_missed_call_is_a_sentence_not_a_download(tmp_path: Path) -> None:
    """What the owner actually saw: «[вложение: не удалось скачать]», twice.

    A call has no file behind it. Handing it to the media pipeline produced a
    fetch failure, and a fetch failure reads like a network problem rather than
    a call somebody did not pick up.
    """
    sender = FakeSender()
    # The payload as MAX actually sends it, measured on a real dialog.
    attachment = normalize_attachment(
        {
            "_type": "CALL",
            "callType": "AUDIO",
            "hangupType": "CANCELED",
            "duration": 0,
            "conversationId": "abc",
            "contactIds": [1, 2],
        }
    )

    assert attachment.kind is AttachmentKind.CALL
    await delivery_for(tmp_path, FakeProtocol(), JPEG, sender).deliver(
        message_with(attachment), bot_id=BOT, chat_id=CHAT
    )

    assert sender.methods == [], "nothing is fetched and nothing is uploaded"
    assert sender.texts == ["📞 Пропущенный аудиозвонок"]


async def test_a_max_notice_is_placed_as_text_and_never_fetched(tmp_path: Path) -> None:
    """Same rule as a call: the message *is* the notice, so nothing is downloaded.

    Measured on a bridge built for a contact with no history — the only thing in
    the dialog was MAX's own join notice, and it arrived as «[MAX прислал
    вложение неизвестного типа]». Asking the media pipeline for a file that does
    not exist is how two missed calls became two «не удалось скачать».
    """
    sender = FakeSender()
    attachment = normalize_attachment(
        {
            "_type": "CONTROL",
            "event": "USER_JOIN",
            "message": "Теперь в MAX! 👉 Напишите что-нибудь!",
            "shortMessage": "Теперь в MAX!",
        }
    )

    assert attachment.kind is AttachmentKind.SERVICE
    await delivery_for(tmp_path, FakeProtocol(), JPEG, sender).deliver(
        message_with(attachment), bot_id=BOT, chat_id=CHAT
    )

    assert sender.methods == [], "nothing is fetched and nothing is uploaded"
    assert sender.texts == ["Теперь в MAX! 👉 Напишите что-нибудь!"]


async def test_an_answered_call_says_how_long_it_was(tmp_path: Path) -> None:
    sender = FakeSender()
    attachment = normalize_attachment({"_type": "CALL", "duration": 200})

    await delivery_for(tmp_path, FakeProtocol(), JPEG, sender).deliver(
        message_with(attachment), bot_id=BOT, chat_id=CHAT
    )

    assert sender.texts == ["📞 Аудиозвонок · 3 мин 20 с"]


async def test_an_unanswered_outgoing_call_is_not_called_missed(tmp_path: Path) -> None:
    """«Пропущенный» in the owner's own outgoing message reads as a bug."""
    from dataclasses import replace

    sender = FakeSender()
    attachment = normalize_attachment({"_type": "CALL"})
    outgoing = replace(message_with(attachment), is_outgoing=True)

    await delivery_for(tmp_path, FakeProtocol(), JPEG, sender).deliver(
        outgoing, bot_id=BOT, chat_id=CHAT
    )

    assert sender.texts == ["📞 Аудиозвонок без ответа"]


def test_a_call_duration_is_read_in_the_unit_it_arrives_in() -> None:
    """Seconds or milliseconds, and getting it wrong is a factor of a thousand.

    Every other duration in MAX is milliseconds, so a call carrying a small
    number is read as seconds and a large one is divided down. The ceiling is
    what tells them apart: MAX has no six-hour calls.
    """
    assert normalize_attachment({"_type": "CALL", "duration": 200}).duration_ms == 200_000
    # 200_000 cannot be seconds — that is over two days.
    assert normalize_attachment({"_type": "CALL", "duration": 200_000}).duration_ms == 200_000
    assert normalize_attachment({"_type": "CALL", "duration": 0}).missed
    assert normalize_attachment({"_type": "CALL"}).missed
    assert not normalize_attachment({"_type": "CALL", "duration": 5}).missed


def test_call_durations_read_as_a_person_would_say_them() -> None:
    from bridge.media.delivery import _duration_text

    assert _duration_text(45) == "45 с"
    assert _duration_text(60) == "1 мин"
    assert _duration_text(200) == "3 мин 20 с"
    assert _duration_text(3_720) == "1 ч 02 мин"


async def test_a_video_call_is_named_as_one(tmp_path: Path) -> None:
    """`callType` is the discriminator, and missing a video call is a different thing."""
    sender = FakeSender()
    attachment = normalize_attachment(
        {"_type": "CALL", "callType": "VIDEO", "hangupType": "CANCELED", "duration": 0}
    )

    assert attachment.call_video
    await delivery_for(tmp_path, FakeProtocol(), JPEG, sender).deliver(
        message_with(attachment), bot_id=BOT, chat_id=CHAT
    )

    assert sender.texts == ["📹 Пропущенный видеозвонок"]


def test_an_unfamiliar_hangup_reason_does_not_change_the_wording() -> None:
    """Only `CANCELED` has been measured; the duration decides either way."""
    missed = normalize_attachment(
        {"_type": "CALL", "callType": "AUDIO", "hangupType": "SOMETHING_NEW", "duration": 0}
    )
    talked = normalize_attachment(
        {"_type": "CALL", "callType": "AUDIO", "hangupType": "SOMETHING_NEW", "duration": 90}
    )

    assert missed.missed
    assert not talked.missed
    assert call_text(talked, is_outgoing=False) == "📞 Аудиозвонок · 1 мин 30 с"


# ----------------------------------------------------------------------- links


PIKABU = "https://pikabu.ru/story/kak_ya_pobedil_1234567"

SHARE_PAYLOAD = {
    "_type": "SHARE",
    "url": PIKABU,
    "title": "Как я победил",
    "description": "Длинный пост",
    "host": "pikabu.ru",
    "image": {"baseUrl": "https://cs.pikabu.ru/p.jpg"},
    "shareId": "abc",
    "contentLevel": 0,
}


async def test_a_shared_link_is_sent_as_a_link(tmp_path: Path) -> None:
    """MAX ships its own preview; Telegram builds one from the URL by itself.

    Forwarding MAX's copy would put a second title and description above
    Telegram's own, saying the same thing twice — and the pipeline could not
    fetch it anyway, which is why it read «не удалось скачать».
    """
    sender = FakeSender()
    attachment = normalize_attachment(SHARE_PAYLOAD)

    assert attachment.kind is AttachmentKind.LINK
    assert attachment.url == PIKABU
    await delivery_for(tmp_path, FakeProtocol(), JPEG, sender).deliver(
        message_with(attachment), bot_id=BOT, chat_id=CHAT
    )

    assert sender.methods == [], "nothing is fetched"
    assert sender.texts == [PIKABU]


async def test_a_link_already_in_the_text_is_not_repeated(tmp_path: Path) -> None:
    """MAX sends the text and the preview as one message. So does the bridge."""
    sender = FakeSender()
    attachment = normalize_attachment(SHARE_PAYLOAD)
    caption = f"смотри что нашёл {PIKABU}"

    await delivery_for(tmp_path, FakeProtocol(), JPEG, sender).deliver(
        message_with(attachment), bot_id=BOT, chat_id=CHAT, caption=caption
    )

    assert sender.texts == [caption]
    assert sender.texts[0].count(PIKABU) == 1


async def test_a_link_missing_from_the_text_is_appended(tmp_path: Path) -> None:
    sender = FakeSender()
    attachment = normalize_attachment(SHARE_PAYLOAD)

    await delivery_for(tmp_path, FakeProtocol(), JPEG, sender).deliver(
        message_with(attachment), bot_id=BOT, chat_id=CHAT, caption="смотри что нашёл"
    )

    assert sender.texts == [f"смотри что нашёл\n{PIKABU}"]


def test_a_share_without_a_url_stays_unknown() -> None:
    """Something else wearing the same type. Better a notice than a link to nowhere."""
    attachment = normalize_attachment({"_type": "SHARE", "title": "нет ссылки"})

    assert attachment.kind is AttachmentKind.UNKNOWN
    assert attachment.url is None


# ------------------------------------------------ unrecognised MAX attachments


async def test_an_unknown_attachment_gets_a_placeholder_not_a_fetch_failure(
    tmp_path: Path,
) -> None:
    """A type the bridge does not know must not read as «не удалось скачать».

    That notice sends the owner looking for a network problem that isn't there:
    the message arrived, MAX just described it in a shape we cannot render. The
    pipeline is never asked to guess a download URL for it — the placeholder is
    sent straight, so nothing is fetched.
    """
    sender = FakeSender()
    attachment = normalize_attachment({"_type": "POLL", "question": "обед?"})
    assert attachment.kind is AttachmentKind.UNKNOWN

    await delivery_for(tmp_path, FakeProtocol(), JPEG, sender).deliver(
        message_with(attachment), bot_id=BOT, chat_id=CHAT
    )

    assert sender.methods == [], "an unknown type is never handed to the fetcher"
    assert sender.texts == [UNKNOWN_NOTICE]
    assert "не удалось скачать" not in sender.texts[0]


async def test_an_unknown_attachment_keeps_the_message_text(tmp_path: Path) -> None:
    """The caption is real even when the attachment cannot be shown."""
    sender = FakeSender()
    attachment = normalize_attachment({"_type": "POLL"})

    await delivery_for(tmp_path, FakeProtocol(), JPEG, sender).deliver(
        message_with(attachment), bot_id=BOT, chat_id=CHAT, caption="глянь"
    )

    assert sender.texts == [f"глянь\n{UNKNOWN_NOTICE}"]


async def test_the_unknown_diagnostic_logs_the_shape_never_the_content(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The log names the type and the fields, so support can be added — and it
    names nothing else, because a value could be a caption or a signed URL."""
    sender = FakeSender()
    attachment = normalize_attachment(
        {"_type": "POLL", "question": "секретный вопрос", "token": "s3cr3t-url-token"}
    )

    with caplog.at_level(logging.WARNING, logger="bridge.media.delivery"):
        await delivery_for(tmp_path, FakeProtocol(), JPEG, sender).deliver(
            message_with(attachment), bot_id=BOT, chat_id=CHAT
        )

    diagnostics = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("type=POLL" in line and "question" in line and "token" in line
               for line in diagnostics), diagnostics
    joined = "\n".join(diagnostics)
    assert "секретный вопрос" not in joined
    assert "s3cr3t-url-token" not in joined


# -------------------------------------------- when the first byte actually goes


async def test_the_hook_fires_once_after_everything_is_downloaded(
    tmp_path: Path,
) -> None:
    """One announcement per message, after preparation, before the first send.

    An album downloads several files before any of them can go. Announcing per
    attachment would mark the job as sending while it was still fetching;
    announcing after the first part would leave the rest unaccounted for. The
    queue reads this mark to decide whether a crash needs the owner, so the
    moment it lands is the whole point.
    """
    fired: list[str] = []
    ready_at_first_send: list[int] = []

    class Counting(FakeSender):
        async def send_album(
            self, bot_id: int, chat_id: int, items: Any, *, reply_to: int | None = None
        ) -> AlbumReceipt | None:
            ready_at_first_send.append(len(items))
            return await FakeSender.send_album(
                self, bot_id, chat_id, items, reply_to=reply_to
            )

    async def on_sending() -> None:
        fired.append("now")

    sender = Counting()
    photos = [
        MaxAttachment(kind=AttachmentKind.PHOTO, raw={"baseUrl": f"https://cdn/p{index}"})
        for index in range(3)
    ]

    await delivery_for(tmp_path, FakeProtocol(), JPEG, sender).deliver(
        message_with(*photos),
        bot_id=BOT,
        chat_id=CHAT,
        caption="три фото",
        on_sending=on_sending,
    )

    assert fired == ["now"], "exactly one announcement for the whole album"
    assert ready_at_first_send == [3], "every file was downloaded before anything was sent"
    assert sender.methods == ["album"]


async def test_the_hook_is_not_fired_when_nothing_can_be_sent(tmp_path: Path) -> None:
    """A message whose only attachment cannot be fetched never reaches Telegram.

    It still produces a placeholder line, which *is* a remote call — so the hook
    fires for that. What must not happen is the hook firing before the download
    was even attempted.
    """
    order: list[str] = []

    class Ordered(FakeSender):
        async def send_text(self, bot_id: int, chat_id: int, text: str, **kwargs: Any) -> int:
            order.append("send")
            return await FakeSender.send_text(self, bot_id, chat_id, text, **kwargs)

    async def on_sending() -> None:
        order.append("hook")

    sender = Ordered()
    # No `baseUrl`: nothing to resolve, so the pipeline gives up before any send.
    photo = MaxAttachment(kind=AttachmentKind.PHOTO, raw={})

    await delivery_for(tmp_path, FakeProtocol(), JPEG, sender).deliver(
        message_with(photo), bot_id=BOT, chat_id=CHAT, on_sending=on_sending
    )

    assert order[:2] == ["hook", "send"], "the hook precedes the call it announces"
    assert order.count("hook") == 1


async def test_without_the_hook_nothing_changes(tmp_path: Path) -> None:
    """The parameter is optional; the old call path must behave identically."""
    sender = FakeSender()
    photo = MaxAttachment(kind=AttachmentKind.PHOTO, raw={"baseUrl": "https://cdn/p"})

    result = await delivery_for(tmp_path, FakeProtocol(), JPEG, sender).deliver(
        message_with(photo), bot_id=BOT, chat_id=CHAT, caption="одно фото"
    )

    assert result is not None
    assert sender.methods == ["photo"]


# ------------------------------------------------------------------- receipts


async def test_an_album_receipt_carries_every_id_in_order(tmp_path: Path) -> None:
    """The head is not enough to say which message the second photo is.

    Telegram answers `sendMediaGroup` with an array and only its first element
    used to leave this module — so a reply to the third photo, or a delete of it,
    had nothing to resolve against. The array is the only place those ids appear.
    """
    sender = FakeSender()
    photos = [
        MaxAttachment(kind=AttachmentKind.PHOTO, raw={"baseUrl": f"https://cdn/p{index}"})
        for index in range(3)
    ]

    receipt = await delivery_for(tmp_path, FakeProtocol(), JPEG, sender).deliver_receipt(
        message_with(*photos), bot_id=BOT, chat_id=CHAT, caption="три фото"
    )

    assert [part.telegram_message_id for part in receipt.album] == [501, 502, 503]
    assert [part.part_index for part in receipt.album] == [0, 1, 2]
    assert {part.kind for part in receipt.album} == {AttachmentKind.PHOTO}
    assert receipt.head == 501, "the head is still what the mapping stores"
    assert receipt.media_group_id == "13984172040192"


async def test_a_single_attachment_reports_no_parts(tmp_path: Path) -> None:
    """A lone photo is not an album of one, and has nothing to alias."""
    sender = FakeSender()
    attachment = MaxAttachment(kind=AttachmentKind.PHOTO, raw={"baseUrl": "https://cdn/p"})

    receipt = await delivery_for(tmp_path, FakeProtocol(), JPEG, sender).deliver_receipt(
        message_with(attachment), bot_id=BOT, chat_id=CHAT
    )

    assert sender.methods == ["photo"]
    assert receipt.album == () and receipt.head == 501


async def test_the_head_is_what_deliver_still_returns(tmp_path: Path) -> None:
    """Every existing caller reads an id, and reads exactly the id it always did."""
    sender = FakeSender()
    photos = [
        MaxAttachment(kind=AttachmentKind.PHOTO, raw={"baseUrl": f"https://cdn/p{index}"})
        for index in range(2)
    ]

    sent = await delivery_for(tmp_path, FakeProtocol(), JPEG, sender).deliver(
        message_with(*photos), bot_id=BOT, chat_id=CHAT
    )

    assert sent == 501


def test_the_album_rule_lives_in_one_place() -> None:
    """The parts that share a `sendMediaGroup` are decided once, not twice.

    The sender needs the answer before it sends, to write the aliases the parts
    will be bound to; the delivery needs it while sending. Two copies would drift,
    and the drift would map an album's parts onto the wrong messages.
    """
    photo = MaxAttachment(kind=AttachmentKind.PHOTO, raw={})
    video = MaxAttachment(kind=AttachmentKind.VIDEO, raw={})
    voice = MaxAttachment(kind=AttachmentKind.VOICE, raw={})

    assert album_attachments(message_with(photo, video)) == (photo, video)
    assert album_attachments(message_with(photo)) == (), "a lone photo reads as itself"
    assert album_attachments(message_with(photo, voice)) == ()
    assert album_attachments(message_with(voice)) == ()
