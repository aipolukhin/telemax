"""inbound media delivery — a MAX message with attachments, delivered into Telegram.

Every attachment kind lands on a different Bot API method, and the choice is not
cosmetic: a voice message sent with `sendDocument` loses the waveform and the
play button, a video note sent as a video stops being a circle, and a track sent
as a document loses its artist. The mapping is therefore explicit rather than
"send everything as a document and hope".

Order is the other half of the job. A MAX message can carry several
attachments; photos and videos go up as one album so they arrive as one block,
and anything else follows in the order MAX listed it. The caption rides on the
first item, because Telegram shows an album's caption only there.

Failures are per-attachment and never silent: a file that is too large or a link
that has expired becomes a short line of text, so the owner knows something was
sent to them and what happened to it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from bridge.max_client import AttachmentKind, IncomingMaxMessage, MaxAttachment
from bridge.max_client.events import MS_PER_SECOND

from .pipeline import MediaPipeline, UnavailableMediaError
from .store import LocalFile, MediaTooLargeError
from .thumbs import video_thumbnail

logger = logging.getLogger(__name__)


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

#: Telegram's caption limit. A longer text follows as its own message rather
#: than being cut.
CAPTION_LIMIT = 1024

#: Kinds that can share one `sendMediaGroup`.
ALBUM_KINDS = frozenset({AttachmentKind.PHOTO, AttachmentKind.VIDEO})

#: What the owner sees instead of an attachment that could not be delivered.
TOO_LARGE_NOTICE = "[{kind}: файл больше лимита в {limit} МБ]"
UNAVAILABLE_NOTICE = "[{kind}: не удалось скачать]"
UNSUPPORTED_NOTICE = "[{kind}: тип вложения пока не поддерживается]"

#: An attachment MAX sent that this bridge does not recognise at all — a new
#: message type, or an old one wearing a shape we have not measured. Kept
#: distinct from «не удалось скачать» on purpose: that reads as a network
#: problem the owner might retry, and this is not one. The message did arrive;
#: the bridge simply cannot render it yet, and says exactly that.
UNKNOWN_NOTICE = "[MAX прислал вложение неизвестного типа — показать его пока не умею]"

#: A call is not an attachment anybody can open — the message *is* the event. So
#: it gets a sentence rather than a «не поддерживается» notice, which is what the
#: owner used to see: «[вложение: не удалось скачать]», reading like a network
#: failure rather than a missed call.
CALL_MISSED_IN = "{icon} Пропущенный {kind}"
CALL_MISSED_OUT = "{icon} {kind} без ответа"
CALL_DONE = "{icon} {kind} · {duration}"

#: MAX distinguishes the two in `callType`, and so does the line: «пропущенный
#: видеозвонок» is a different thing to have missed.
CALL_KINDS = {False: ("📞", "аудиозвонок"), True: ("📹", "видеозвонок")}

def link_text(attachment: MaxAttachment, *, caption: str | None) -> str:
    """The message as a link Telegram can preview itself.

    MAX ships its own preview inside the attachment — title, description, an
    image. None of it is worth forwarding: Telegram builds a preview from the URL
    on its own, and a copy of MAX's would sit above it saying the same thing
    twice. So the whole rendering is the address.

    The URL is usually already in the message text, which is why this checks
    before appending: MAX sends the text and the preview as one message, and the
    bridge used to add «[вложение: не удалось скачать]» underneath it.
    """
    url = (attachment.url or "").strip()
    body = (caption or "").strip()
    if not url:
        return body
    if not body:
        return url
    return body if url in body else f"{body}\n{url}"


#: Human names for the notices above. The bridge speaks Russian to its owner.
KIND_NAMES = {
    AttachmentKind.PHOTO: "фото",
    AttachmentKind.VIDEO: "видео",
    AttachmentKind.VIDEO_NOTE: "кружок",
    AttachmentKind.VOICE: "голосовое",
    AttachmentKind.MUSIC: "аудио",
    AttachmentKind.FILE: "файл",
    AttachmentKind.STICKER: "стикер",
    AttachmentKind.CONTACT: "контакт",
    AttachmentKind.CALL: "звонок",
    AttachmentKind.LINK: "ссылка",
    AttachmentKind.SERVICE: "уведомление",
    AttachmentKind.UNKNOWN: "вложение",
}


def service_text(attachment: MaxAttachment, *, caption: str | None) -> str:
    """MAX's own notice about the dialog, as one quiet line.

    Not a file and not really a message: MAX writes these itself — «Теперь в
    MAX! 👉 Напишите что-нибудь!» when a contact first appears. The server has
    already rendered the sentence, so there is nothing to compose, only to place.

    Dropping them was the other option and it is the wrong one: the notice is
    part of what happened in that dialog, and an import that silently omits
    messages is worse than one that shows a line the owner can ignore. What was
    shown before was «[MAX прислал вложение неизвестного типа]», which is both
    uglier and false — the type is known now.
    """
    body = (caption or "").strip()
    notice = (attachment.notice or "").strip()
    if not notice:
        # No sentence from the server. Name the event rather than print nothing:
        # a blank line in the middle of an import reads as a lost message.
        event = (attachment.event or "").strip()
        notice = f"MAX: служебное уведомление ({event})" if event else "MAX: служебное уведомление"
    return f"{body}\n{notice}" if body else notice


def _unknown_shape(raw: dict[str, Any]) -> str:
    """A privacy-safe description of an unrecognised attachment, for the log.

    The protocol type and the field *names* — never their values. A value could
    be a caption, a file name or a signed URL; a field name never is. This is
    the same line the redaction discipline draws everywhere else: an alert says
    how many and of what shape, never what was said.

    The point is to turn the next unfamiliar payload into something a developer
    can add support for — `type=POLL fields=[options, question, ...]` is a
    starting point; the message text would only be a leak.
    """
    type_label = str(raw.get("_type") or raw.get("type") or "?")[:40]
    fields = ", ".join(sorted(str(key) for key in raw))
    return f"type={type_label} fields=[{fields}]"


def _duration_text(seconds: int) -> str:
    """`3 мин 20 с`, `45 с`, `1 ч 02 мин`. Short, and never a bare number."""
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} ч {minutes:02d} мин"
    if minutes:
        return f"{minutes} мин {secs:02d} с" if secs else f"{minutes} мин"
    return f"{secs} с"


#: The card shown when a shared contact cannot become a Telegram contact —
#: a MAX user shared by id, whose phone the server does not hand over. There is
#: nothing to dial, so it is a labelled line, not a fake `sendContact`.
CONTACT_CARD = "👤 Контакт [Max]: {name}"
CONTACT_CARD_NAMELESS = "👤 Контакт [Max]"

#: The button under a shared MAX contact that opens the guardian ready to bridge
#: that person. Only shown when the contact is a MAX user (has an id to bridge).
BRIDGE_BUTTON = "Поднять мост"


def contact_card_text(attachment: MaxAttachment, *, caption: str | None) -> str:
    """A contact with no phone, as a plain line. The owner still sees who it was.

    Reached only when `sendContact` cannot be: Telegram requires a phone and a
    MAX user shared by id arrives without one (the server does not leak it). A
    caption the owner typed with it rides along above the card.
    """
    name = (attachment.contact_name or "").strip()
    card = CONTACT_CARD.format(name=name) if name else CONTACT_CARD_NAMELESS
    body = (caption or "").strip()
    return f"{body}\n{card}" if body else card


def contact_first_last(attachment: MaxAttachment) -> tuple[str, str | None]:
    """Split a contact's name into the two halves Telegram's `sendContact` wants.

    Telegram requires a first name and takes an optional last. MAX carries the
    whole name together, so the first word is the first name and the rest is the
    last — good enough for a card, and a name is never empty here because the
    phone path is only taken when there is something to show.
    """
    parts = (attachment.contact_name or "").strip().split(None, 1)
    if not parts:
        return "Контакт", None
    return parts[0], (parts[1] if len(parts) > 1 else None)


def call_text(attachment: MaxAttachment, *, is_outgoing: bool) -> str:
    """What a call message says. Direction matters: only an incoming one is missed.

    An outgoing call nobody answered is not the owner missing anything, and
    calling it «пропущенный» in their own outgoing message reads as a bug.

    The `hangupType` MAX sends is deliberately not used for the wording. It tells
    a cancelled call from a rejected one, and only `CANCELED` has been measured —
    the duration already answers the question the line has to answer.
    """
    icon, kind = CALL_KINDS[attachment.call_video]
    if attachment.missed or not attachment.duration_ms:
        template = CALL_MISSED_OUT if is_outgoing else CALL_MISSED_IN
        return template.format(icon=icon, kind=kind.capitalize() if is_outgoing else kind)
    return CALL_DONE.format(
        icon=icon,
        kind=kind.capitalize(),
        duration=_duration_text(attachment.duration_ms // MS_PER_SECOND),
    )


@dataclass(frozen=True, slots=True)
class AlbumReceipt:
    """What `sendMediaGroup` actually created, in the order it created it.

    The Bot API answers an album with an array of messages, and until now only
    its first element left this module. That first id is enough to say "the
    message arrived" and not nearly enough to say *which Telegram message* the
    second photo is — which is what a reply to it, or a delete of it, has to
    resolve. The array is the only place that appears, so it is carried whole.

    Ordered, and the order is the contract: live verification found the returned
    array, the individual `UpdateNewMessage`s, a Telethon `Album` event and a
    reconnect's catch-up all agree on ascending message id, and nothing else does.
    """

    message_ids: tuple[int, ...]
    #: Telegram's own id for the group, when the answer carries one. Read off the
    #: reply that is already in hand — never worth a second request.
    media_group_id: str | None = None


def check_album_receipt(message_ids: tuple[int, ...], *, expected: int) -> None:
    """The contract every album answer has to keep, asserted rather than assumed.

    `AlbumReceipt` has always documented that Telegram answers an album in
    ascending message id, and the live probe confirmed it on every surface the
    order can be read from. Nothing checked it, and the settlement binds part
    *k* to element *k* — so a single reordered answer would map somebody's third
    photo onto their second for the rest of the conversation.

    Deliberately **not** a sort. Sorting would repair the symptom and hide the
    fact that the transport had stopped behaving as measured, which is exactly
    the kind of silence this project keeps paying for. A violation is a question
    for the owner: the album is in the chat, and which message is which is no
    longer knowable from here.
    """
    # Imported here rather than at module scope: `routing.delivery` owns the
    # exception and `routing` imports this module, so a top-level import would
    # close a cycle. This is the only call that has to look back along it.
    from bridge.routing.delivery import UnconfirmedDeliveryError

    if len(message_ids) != expected:
        raise UnconfirmedDeliveryError(
            f"Telegram answered an album of {expected} with {len(message_ids)} message(s)"
        )
    if len(set(message_ids)) != len(message_ids):
        raise UnconfirmedDeliveryError("Telegram named the same message twice in one album")
    if any(message_ids[index] >= message_ids[index + 1] for index in range(len(message_ids) - 1)):
        raise UnconfirmedDeliveryError(
            "Telegram answered an album out of ascending order; the parts cannot be told apart"
        )
    if any(message_id <= 0 for message_id in message_ids):
        raise UnconfirmedDeliveryError("Telegram named a message id that cannot exist")


@dataclass(frozen=True, slots=True)
class DeliveredPart:
    """One Telegram message an album put in the chat, and what it holds."""

    telegram_message_id: int
    kind: AttachmentKind
    part_index: int


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    """What one MAX→TG delivery put in the chat, structurally.

    `head` is what the mapping has always stored and still stores: a reply, an
    edit or a deletion refers to the message as a whole, and the first id is the
    closest thing Telegram has to that. `album` is non-empty only when the
    delivery took the `sendMediaGroup` branch, so a text message and a single
    attachment describe themselves exactly as they always did.
    """

    head: int | None = None
    album: tuple[DeliveredPart, ...] = ()
    media_group_id: str | None = None


def split_caption(caption: str) -> tuple[str | None, str]:
    """The caption as it will ride along, and whatever will not fit.

    Module-level because two callers need the same answer: the delivery, which
    puts the head part's caption on the wire, and the alias the sender writes
    down before it — a fingerprint built from a caption Telegram was never given
    would describe an album that was never sent.
    """
    text = caption.strip()
    if not text:
        return None, ""
    if len(text) <= CAPTION_LIMIT:
        return text, ""
    return None, text


def album_attachments(message: IncomingMaxMessage) -> tuple[MaxAttachment, ...]:
    """The attachments that will share one `sendMediaGroup`, in order.

    The one place that decision is made, because it is made twice: here, when the
    album is sent, and before it, when the aliases that will be bound to its
    parts are written down. Two copies of this rule would drift, and the drift
    would show up as an album whose parts map to the wrong messages.

    A lone photo or video is deliberately not an album: it reads better as
    itself, so it goes out as a single message and has no parts to alias.
    """
    album = tuple(item for item in message.attachments if item.kind in ALBUM_KINDS)
    return () if len(album) < 2 else album


@dataclass(frozen=True, slots=True)
class OutgoingMedia:
    """One local file plus everything Telegram wants to know about it."""

    kind: AttachmentKind
    path: Path
    file_name: str
    caption: str | None = None
    caption_entities: list[dict[str, Any]] | None = None
    duration_seconds: int | None = None
    width: int | None = None
    height: int | None = None
    title: str | None = None
    performer: str | None = None
    #: A JPEG cover, already upright, for the kinds Telegram would otherwise
    #: draw one for itself. Bytes rather than a path so nothing has to own its
    #: lifetime across an upload.
    thumbnail: bytes | None = None


class TelegramMediaSender(Protocol):
    """The Bot API surface inbound media delivery needs. One method per attachment kind.

    **Every creating method returns `int`, never `None`.** A send either names
    the message it made or raises, because "no id" and "no message" are different
    facts and one return value cannot carry both — which is precisely how a
    placeholder that never went out was settled as part of a delivered message.
    The one exception is `send_photo_url`, where `None` is a documented answer
    from Telegram itself: it could not use the URL, so nothing was created and
    the caller draws the card as text.
    """

    async def send_photo(self, bot_id: int, chat_id: int, media: OutgoingMedia,
                         *, reply_to: int | None) -> int: ...

    async def send_video(self, bot_id: int, chat_id: int, media: OutgoingMedia,
                         *, reply_to: int | None) -> int: ...

    async def send_video_note(self, bot_id: int, chat_id: int, media: OutgoingMedia,
                              *, reply_to: int | None) -> int: ...

    async def send_voice(self, bot_id: int, chat_id: int, media: OutgoingMedia,
                         *, reply_to: int | None) -> int: ...

    async def send_audio(self, bot_id: int, chat_id: int, media: OutgoingMedia,
                         *, reply_to: int | None) -> int: ...

    async def send_document(self, bot_id: int, chat_id: int, media: OutgoingMedia,
                            *, reply_to: int | None) -> int: ...

    async def send_sticker(self, bot_id: int, chat_id: int, media: OutgoingMedia,
                           *, reply_to: int | None) -> int: ...

    async def send_album(self, bot_id: int, chat_id: int, items: list[OutgoingMedia],
                         *, reply_to: int | None) -> AlbumReceipt: ...

    async def send_text(self, bot_id: int, chat_id: int, text: str, *,
                        reply_to: int | None = None,
                        entities: list[dict[str, Any]] | None = None,
                        buttons: list[tuple[str, str]] | None = None) -> int: ...

    async def send_contact(self, bot_id: int, chat_id: int, *, phone: str, first_name: str,
                           last_name: str | None = None, vcard: str | None = None,
                           reply_to: int | None = None) -> int: ...

    async def send_photo_url(self, bot_id: int, chat_id: int, url: str, *, caption: str,
                             reply_to: int | None = None,
                             buttons: list[tuple[str, str]] | None = None) -> int | None: ...


class BridgeLink(Protocol):
    """Builds the deep link that opens the guardian on "bridge this person"."""

    def for_contact(self, max_user_id: int) -> str | None: ...


def ms_to_seconds(duration_ms: int | None) -> int | None:
    """MAX counts in milliseconds, Telegram in seconds — everywhere but MUSIC."""
    if not duration_ms:
        return None
    return max(1, round(duration_ms / 1000))


class _AnnouncesFirstSend:
    """A sender that says so, once, the first time it is actually used.

    Wrapping is what makes the guarantee total. Calling a hook by hand at the
    top of each branch would mean remembering it in `_send_album`,
    `_send_single`, `_dispatch` and three placeholder paths — and a branch added
    later would forget, silently, in the direction that mislabels a message.
    """

    def __init__(
        self, sender: TelegramMediaSender, on_sending: Callable[[], Awaitable[None]]
    ) -> None:
        self._sender = sender
        self._on_sending = on_sending
        self._announced = False

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._sender, name)
        if not callable(attribute):
            return attribute

        async def call(*args: Any, **kwargs: Any) -> Any:
            if not self._announced:
                self._announced = True
                await self._on_sending()
            return await attribute(*args, **kwargs)

        return call


class StickerOrigins(Protocol):
    """Remembers that a file handed to Telegram was a particular MAX sticker."""

    async def put(self, tg_sha256: str, max_sticker_id: int) -> None: ...


class MaxMediaDelivery:
    """Turns the attachments of one MAX message into Telegram messages."""

    def __init__(
        self,
        *,
        pipeline: MediaPipeline,
        sender: TelegramMediaSender,
        sticker_origins: StickerOrigins | None = None,
        bridge_link: BridgeLink | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._sender = sender
        # Optional: without it a MAX sticker sent back from Telegram is rebuilt
        # as a new static copy instead of returning as itself.
        self._sticker_origins = sticker_origins
        # Optional: builds the "поднять мост" deep link for a shared MAX contact.
        # Without it the contact card is still shown, just without the button —
        # a bridge can be added by hand the usual way.
        self._bridge_link = bridge_link

    async def deliver(
        self,
        message: IncomingMaxMessage,
        *,
        bot_id: int,
        chat_id: int,
        caption: str = "",
        caption_entities: list[dict[str, Any]] | None = None,
        reply_to: int | None = None,
        on_sending: Callable[[], Awaitable[None]] | None = None,
    ) -> int | None:
        """Send everything this message carries. Returns the first message id.

        The shape every existing caller expects, kept exactly: the head is what
        the mapping stores, because a reply, an edit or a deletion refers to the
        message as a whole. `deliver_receipt` is the same delivery with the rest
        of the answer attached, for the one caller that binds an album's parts.
        """
        receipt = await self.deliver_receipt(
            message,
            bot_id=bot_id,
            chat_id=chat_id,
            caption=caption,
            caption_entities=caption_entities,
            reply_to=reply_to,
            on_sending=on_sending,
        )
        return receipt.head

    async def deliver_receipt(
        self,
        message: IncomingMaxMessage,
        *,
        bot_id: int,
        chat_id: int,
        caption: str = "",
        caption_entities: list[dict[str, Any]] | None = None,
        reply_to: int | None = None,
        on_sending: Callable[[], Awaitable[None]] | None = None,
    ) -> DeliveryReceipt:
        """As `deliver`, and says which Telegram message each album part became.

        `on_sending` fires once, immediately before the first byte goes to
        Telegram — after every attachment has been resolved, downloaded and
        validated. That ordering is what lets the queue tell "died while
        preparing" (retry: nothing was sent) from "died while sending"
        (ambiguous: it may have arrived). Announcing it any earlier makes a
        download that never reached Telegram look like a delivery nobody can
        vouch for.

        It is attached to the sender rather than sprinkled through the branches
        below because *every* remote call goes through `self._sender`, including
        the placeholder notices — so there is no path that can forget it.
        """
        if on_sending is not None:
            announcing = MaxMediaDelivery(
                pipeline=self._pipeline,
                sender=cast(TelegramMediaSender, _AnnouncesFirstSend(self._sender, on_sending)),
                # Carry the collaborators, not just the pipeline: without the
                # sticker origins an animated sticker came back flat, and without
                # the bridge link a shared contact's card lost its button — both
                # only on the queue path, which is the one every live delivery
                # takes.
                sticker_origins=self._sticker_origins,
                bridge_link=self._bridge_link,
            )
            return await announcing.deliver_receipt(
                message,
                bot_id=bot_id,
                chat_id=chat_id,
                caption=caption,
                caption_entities=caption_entities,
                reply_to=reply_to,
            )

        album_source = list(album_attachments(message))
        singles = [
            item
            for item in message.attachments
            if item.kind not in ALBUM_KINDS or not album_source
        ]

        head_caption, tail_text = split_caption(caption)
        first_id: int | None = None
        caption_used = False
        album: tuple[DeliveredPart, ...] = ()
        media_group_id: str | None = None

        if album_source:
            album, media_group_id = await self._send_album(
                album_source,
                message=message,
                bot_id=bot_id,
                chat_id=chat_id,
                caption=head_caption,
                caption_entities=caption_entities,
                reply_to=reply_to,
            )
            first_id = album[0].telegram_message_id if album else None
            caption_used = True

        for attachment in singles:
            sent = await self._send_one(
                attachment,
                message=message,
                bot_id=bot_id,
                chat_id=chat_id,
                caption=None if caption_used else head_caption,
                caption_entities=None if caption_used else caption_entities,
                reply_to=reply_to if first_id is None else None,
            )
            caption_used = True
            if first_id is None:
                first_id = sent

        if tail_text:
            # A caption Telegram would refuse follows as its own message rather
            # than being silently cut in half.
            sent = await self._sender.send_text(
                bot_id, chat_id, tail_text, reply_to=first_id or reply_to
            )
            if first_id is None:
                first_id = sent

        return DeliveryReceipt(head=first_id, album=album, media_group_id=media_group_id)

    async def _send_album(
        self,
        attachments: list[MaxAttachment],
        *,
        message: IncomingMaxMessage,
        bot_id: int,
        chat_id: int,
        caption: str | None,
        caption_entities: list[dict[str, Any]] | None,
        reply_to: int | None,
    ) -> tuple[tuple[DeliveredPart, ...], str | None]:
        """Download the whole group, then send it as one block.

        The files have to exist at the same moment, so the contexts are entered
        together and unwound together — an album assembled one file at a time
        would be several separate messages.

        Returns a part per Telegram message the group became, paired with the
        item it carried. An attachment that could not be fetched leaves a
        placeholder in the chat and no part here — which is a shorter list than
        the caller expected, and deliberately: the caller compares the two and
        refuses to guess which of its aliases went missing.
        """
        async with AsyncExitStack() as stack:
            items: list[OutgoingMedia] = []
            for attachment in attachments:
                local = await self._enter(stack, attachment, message)
                if local is None:
                    await self._notify_failure(attachment, bot_id, chat_id, reply_to)
                    continue
                items.append(
                    self._describe(
                        attachment,
                        local,
                        caption=caption if not items else None,
                        caption_entities=caption_entities if not items else None,
                        thumbnail=await self._cover(attachment, local),
                    )
                )

            if not items:
                return (), None
            if len(items) == 1:
                # One survivor of a group: Telegram has no album to make of it,
                # so it goes as itself and the answer says so.
                sent = await self._dispatch(items[0], bot_id, chat_id, reply_to)
                return (DeliveredPart(sent, items[0].kind, 0),), None
            receipt = await self._sender.send_album(bot_id, chat_id, items, reply_to=reply_to)
            # `strict=True`: the adapter has already checked the cardinality
            # against the contract, and pairing them loosely here would silently
            # re-introduce the mismatch it exists to catch.
            return (
                tuple(
                    DeliveredPart(message_id, item.kind, index)
                    for index, (message_id, item) in enumerate(
                        zip(receipt.message_ids, items, strict=True)
                    )
                ),
                receipt.media_group_id,
            )
        return (), None

    async def _enter(
        self, stack: AsyncExitStack, attachment: MaxAttachment, message: IncomingMaxMessage
    ) -> LocalFile | None:
        try:
            local: LocalFile = await stack.enter_async_context(
                self._pipeline.fetch_from_max(
                    attachment, chat_id=message.chat_id, message_id=message.message_id
                )
            )
        except (MediaTooLargeError, UnavailableMediaError):
            return None
        return local

    async def _send_one(
        self,
        attachment: MaxAttachment,
        *,
        message: IncomingMaxMessage,
        bot_id: int,
        chat_id: int,
        caption: str | None,
        caption_entities: list[dict[str, Any]] | None,
        reply_to: int | None,
    ) -> int:
        if attachment.kind is AttachmentKind.LINK:
            # Nothing to fetch, and nothing to add: the URL carries its own
            # preview once Telegram sees it. Entities still apply — appending to
            # the end of the text cannot move an offset that precedes it.
            return await self._sender.send_text(
                bot_id,
                chat_id,
                link_text(attachment, caption=caption),
                reply_to=reply_to,
                entities=caption_entities,
            )

        if attachment.kind is AttachmentKind.SERVICE:
            # Nothing to fetch, same as a call: the message *is* the notice.
            return await self._sender.send_text(
                bot_id,
                chat_id,
                service_text(attachment, caption=caption),
                reply_to=reply_to,
            )

        if attachment.kind is AttachmentKind.CALL:
            # Nothing to fetch: a call has no file behind it, and asking the
            # media pipeline for one is how two missed calls turned into two
            # «не удалось скачать».
            return await self._sender.send_text(
                bot_id,
                chat_id,
                call_text(attachment, is_outgoing=message.is_outgoing),
                reply_to=reply_to,
            )

        if attachment.kind is AttachmentKind.CONTACT:
            # Not a file to download (contact sharing). A phone makes it a real Telegram
            # contact — the owner can save it or tap to call; a MAX user shared
            # by id has no phone (the server withholds it), so that one is a
            # labelled card instead of a fake card with a made-up number.
            if attachment.contact_phone:
                first, last = contact_first_last(attachment)
                return await self._sender.send_contact(
                    bot_id,
                    chat_id,
                    phone=attachment.contact_phone,
                    first_name=first,
                    last_name=last,
                    vcard=attachment.contact_vcard,
                    reply_to=reply_to,
                )
            # A MAX user shared by id can be bridged from the card: the id XORs
            # with our own into the dialog's chat id (§9г), and the button hands
            # it to the guardian. A contact with no MAX id — a raw vCard — has no
            # profile to bridge, so it gets no button.
            buttons: list[tuple[str, str]] | None = None
            if attachment.contact_user_id is not None and self._bridge_link is not None:
                link = self._bridge_link.for_contact(attachment.contact_user_id)
                if link:
                    buttons = [(BRIDGE_BUTTON, link)]
            card = contact_card_text(attachment, caption=caption)
            # The avatar as the card's photo, when the contact has one — a face
            # reads faster than a line. Telegram fetches the URL itself; if it
            # cannot (a stale or unreachable avatar), the card still arrives as
            # text rather than nothing.
            if attachment.contact_photo_url:
                shown = await self._sender.send_photo_url(
                    bot_id,
                    chat_id,
                    attachment.contact_photo_url,
                    caption=card,
                    reply_to=reply_to,
                    buttons=buttons,
                )
                if shown is not None:
                    return shown
            return await self._sender.send_text(
                bot_id,
                chat_id,
                card,
                reply_to=reply_to,
                entities=caption_entities,
                buttons=buttons,
            )

        if attachment.kind is AttachmentKind.UNKNOWN:
            # A type we do not recognise. Handing it to the pipeline would ask
            # it to guess a download URL and, failing, cry «не удалось скачать» —
            # a network error that isn't one. Instead the owner gets a straight
            # placeholder and the shape is logged, so an unfamiliar payload
            # becomes a thing to add support for rather than a silent shrug.
            return await self._deliver_unknown(
                attachment,
                bot_id=bot_id,
                chat_id=chat_id,
                caption=caption,
                caption_entities=caption_entities,
                reply_to=reply_to,
            )

        try:
            async with self._pipeline.fetch_from_max(
                attachment, chat_id=message.chat_id, message_id=message.message_id
            ) as local:
                media = self._describe(
                    attachment,
                    local,
                    caption=caption,
                    caption_entities=caption_entities,
                    thumbnail=await self._cover(attachment, local),
                )
                sent = await self._dispatch(media, bot_id, chat_id, reply_to)
                # Recorded inside the block, while the bytes still exist: this is
                # the only moment where both the file Telegram will hold and the
                # MAX sticker it came from are in the same place.
                await self._note_sticker_origin(attachment, local)
                return sent
        except MediaTooLargeError as error:
            return await self._sender.send_text(
                bot_id,
                chat_id,
                TOO_LARGE_NOTICE.format(
                    kind=KIND_NAMES.get(attachment.kind, "вложение"),
                    limit=error.limit_bytes // 1024 // 1024,
                ),
                reply_to=reply_to,
            )
        except UnavailableMediaError:
            logger.info("attachment %s could not be fetched", attachment.kind.value)
            return await self._sender.send_text(
                bot_id,
                chat_id,
                UNAVAILABLE_NOTICE.format(kind=KIND_NAMES.get(attachment.kind, "вложение")),
                reply_to=reply_to,
            )

    async def _deliver_unknown(
        self,
        attachment: MaxAttachment,
        *,
        bot_id: int,
        chat_id: int,
        caption: str | None,
        caption_entities: list[dict[str, Any]] | None,
        reply_to: int | None,
    ) -> int:
        """Placeholder plus diagnostic for an attachment we cannot render.

        The caption still rides along: any text the message carried is real, and
        losing it would compound one gap with another. The notice follows the
        text rather than leading it, so caption entities — which index the text
        from its start — stay valid without being shifted.
        """
        logger.warning(
            "unknown MAX attachment delivered as a placeholder: %s",
            _unknown_shape(attachment.raw),
        )
        body = (caption or "").strip()
        text = f"{body}\n{UNKNOWN_NOTICE}" if body else UNKNOWN_NOTICE
        return await self._sender.send_text(
            bot_id, chat_id, text, reply_to=reply_to, entities=caption_entities
        )

    async def _notify_failure(
        self, attachment: MaxAttachment, bot_id: int, chat_id: int, reply_to: int | None
    ) -> None:
        await self._sender.send_text(
            bot_id,
            chat_id,
            UNAVAILABLE_NOTICE.format(kind=KIND_NAMES.get(attachment.kind, "вложение")),
            reply_to=reply_to,
        )

    async def _note_sticker_origin(self, attachment: MaxAttachment, local: LocalFile) -> None:
        """Remember that these exact bytes are MAX sticker N.

        MSG_SEND accepts a `stickerId` the account does not own, so a sticker
        that came out of MAX can be sent back into it as itself — animation and
        all — rather than rebuilt as a flat copy. Telegram returns the file byte
        for byte, so the bytes are the key and nothing has to be threaded
        through the delivery queue to make the match.
        """
        if self._sticker_origins is None or attachment.kind is not AttachmentKind.STICKER:
            return
        sticker_id = attachment.raw.get("stickerId") or attachment.raw.get("sticker_id")
        if not isinstance(sticker_id, int):
            return
        try:
            digest = await asyncio.to_thread(_sha256_of, local.path)
            await self._sticker_origins.put(digest, sticker_id)
        except Exception:
            # A convenience, not a delivery step: the message is already sent.
            logger.debug("could not record the origin of sticker %s", sticker_id, exc_info=True)

    async def _cover(self, attachment: MaxAttachment, local: LocalFile) -> bytes | None:
        """A cover for the kinds Telegram would otherwise draw one for.

        Only video: a photo is its own thumbnail and a document does not get one.
        Off the event loop, because decoding a keyframe is real work and a
        delivery must not stall the other bridges while it happens.
        """
        if attachment.kind not in (AttachmentKind.VIDEO, AttachmentKind.VIDEO_NOTE):
            return None
        return await asyncio.to_thread(video_thumbnail, local.path)

    def _describe(
        self,
        attachment: MaxAttachment,
        local: LocalFile,
        *,
        caption: str | None,
        caption_entities: list[dict[str, Any]] | None,
        thumbnail: bytes | None = None,
    ) -> OutgoingMedia:
        return OutgoingMedia(
            kind=attachment.kind,
            path=local.path,
            file_name=local.display_name,
            caption=caption,
            caption_entities=caption_entities,
            duration_seconds=ms_to_seconds(attachment.duration_ms),
            width=attachment.width,
            height=attachment.height,
            title=attachment.title,
            performer=attachment.performer,
            thumbnail=thumbnail,
        )

    async def _dispatch(
        self, media: OutgoingMedia, bot_id: int, chat_id: int, reply_to: int | None
    ) -> int:
        if media.kind is AttachmentKind.PHOTO:
            return await self._sender.send_photo(bot_id, chat_id, media, reply_to=reply_to)
        if media.kind is AttachmentKind.VIDEO:
            return await self._sender.send_video(bot_id, chat_id, media, reply_to=reply_to)
        if media.kind is AttachmentKind.VIDEO_NOTE:
            # A circle sent as a video stops being a circle.
            return await self._sender.send_video_note(bot_id, chat_id, media, reply_to=reply_to)
        if media.kind is AttachmentKind.VOICE:
            # MAX serves ogg/opus, which is exactly what sendVoice wants.
            return await self._sender.send_voice(bot_id, chat_id, media, reply_to=reply_to)
        if media.kind is AttachmentKind.MUSIC:
            return await self._sender.send_audio(bot_id, chat_id, media, reply_to=reply_to)
        if media.kind is AttachmentKind.STICKER:
            sent = await self._sender.send_sticker(bot_id, chat_id, media, reply_to=reply_to)
            if media.caption:
                # `sendSticker` takes no caption. Dropping the text would lose
                # half the message, so it follows as its own — hung off the
                # sticker, which keeps the two together in the thread.
                await self._sender.send_text(
                    bot_id,
                    chat_id,
                    media.caption,
                    reply_to=sent or reply_to,
                    entities=media.caption_entities,
                )
            return sent
        return await self._sender.send_document(bot_id, chat_id, media, reply_to=reply_to)
