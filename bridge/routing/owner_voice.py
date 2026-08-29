"""Writing in the owner's own voice — as the owner, over their own session.

The bridge shows a MAX dialog as a chat with one bot, and that reads correctly in
one direction only. Messages *from* the contact are the bot's to send. Messages
the owner wrote — in the MAX app, or years ago in the history being imported —
are not the bot's, and posting them as `Вы: привет` puts the owner's own words on
the wrong side of the screen.

Secretary Mode solved that first, by lending the guardian a business connection.
It worked and it cost too much: Telegram Premium, Business switched on, a
connection granted to the guardian and revocable at any moment, and a `can_reply`
flag that could quietly turn the whole thing off. This does the same job with the
session the bridge already runs — the owner's own account, sending its own
message — and the placement itself is an ordinary durable job, so a failure is a
retry or a question rather than a second copy of the line.

**The one rule this rests on: the send must go through the *intake* session.** A
session receives no update for a message it sent itself — measured, and nothing
in the API promises it — so sending through the session that also reads owner
updates means the placement is invisible to our own intake and cannot travel back
into MAX as a duplicate. Sending through any *other* session of the same account
would be seen by the intake and would loop. `runtime` therefore hands this the
live `TelegramUserSession` and never a second client.

The contact bot still receives the message, the way it receives anything the
owner types. `OwnEchoes` claims it by its exact text — which carries the MAX
timestamp stamp, so it is far more specific than it looks — and that claim is
also where the bot's own id for the message is picked up, which is what makes a
reply to it resolvable.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol

from bridge.max_client import AttachmentKind
from bridge.media.delivery import (
    CONTACT_CARD,
    CONTACT_CARD_NAMELESS,
    AlbumReceipt,
    OutgoingMedia,
    check_album_receipt,
)
from bridge.routing.delivery import UnconfirmedDeliveryError

logger = logging.getLogger(__name__)


class OwnerTransportUnavailableError(Exception):
    """The owner's session is not there to place a message through.

    A retryable condition and nothing else. It is *not* a reason to send the same
    message some other way: the job is on the queue, the session comes back, and
    the message goes out once. Rerouting it through the contact bot would put a
    second copy of it in the chat the moment the first one turned out to have
    arrived after all.
    """


class PartialOwnerAlbumError(Exception):
    """Some parts of an album reached Telegram and some did not.

    Unconfirmed rather than failed: what is in the chat is in the chat, and a
    retry would place the parts that made it a second time. The queue turns this
    into AMBIGUOUS so the owner sees a half-album and decides, which is the only
    honest answer — nothing here can tell which photo is missing from a chat it
    cannot read back.
    """


class OwnerSession(Protocol):
    """The slice of `TelegramUserSession` placing a message needs."""

    @property
    def is_connected(self) -> bool: ...

    async def send_own_message(
        self, peer_id: int, text: str, *, entities: list[dict[str, Any]] | None = None
    ) -> int | None: ...

    async def send_own_album(
        self,
        peer_id: int,
        paths: list[str],
        *,
        caption: str | None = None,
        entities: list[dict[str, Any]] | None = None,
    ) -> list[int | None]: ...

    async def send_own_file(
        self,
        peer_id: int,
        path: str,
        *,
        caption: str | None = None,
        entities: list[dict[str, Any]] | None = None,
        kind: str = "document",
        file_name: str | None = None,
        duration: int | None = None,
        width: int | None = None,
        height: int | None = None,
        title: str | None = None,
        performer: str | None = None,
        thumbnail: bytes | None = None,
    ) -> int | None: ...


class OwnerVoice:
    """`OwnVoice` over the owner's own MTProto session.

    Available exactly when the session is connected. There is no second condition
    — no Premium, no business connection, no `can_reply` — which is the whole
    point of moving off the guardian: the owner's account can always write in the
    owner's own chat.
    """

    def __init__(self, *, session: Callable[[], OwnerSession | None]) -> None:
        # Resolved on every call, never captured: the router is built before the
        # session is opened, and a session that drops and comes back is a new
        # client. A reference taken once would be stale in both directions.
        self._session = session

    @property
    def available(self) -> bool:
        session = self._session()
        return bool(session is not None and session.is_connected)

    async def send_as_owner(
        self,
        chat_id: int,
        text: str,
        *,
        entities: list[dict[str, Any]] | None = None,
    ) -> int | None:
        """Place one line as the owner. Returns the owner's own id for it.

        `chat_id` is the contact bot: from the owner's account that bot *is* the
        other side of the conversation, so it is both the peer and the chat — the
        same addressing the business path used.

        **Raises rather than reporting failure.** It used to swallow everything
        and answer None, which let the caller fall back to a `Вы: …` bot line —
        and that fallback was reached by a timeout *after* Telegram had already
        accepted the message, so the owner's chat got the same line twice. The
        caller is now a durable job: an exception is what makes the difference
        between "retry, nothing was sent" and "ask the owner, it may have been",
        and swallowing it erases exactly that distinction.
        """
        session = self._require()
        return await session.send_own_message(chat_id, text, entities=entities)

    def _require(self) -> OwnerSession:
        session = self._session()
        if session is None:
            # Not a failure of this message: the transport is away. Raised so the
            # job waits for it rather than being reported as sent or quietly
            # rerouted through a bot that would sign it `Вы: …`.
            raise OwnerTransportUnavailableError(
                "the owner's Telegram session is not connected"
            )
        return session


class MtprotoOwnerSender:
    """`TelegramMediaSender` that uploads as the owner, over the same session.

    Shaped to the existing port so `MaxMediaDelivery` is reused whole: the album
    rule, the caption split, the per-attachment placeholders and the upright
    cover all behave identically to the bot's own path, and only the transport
    underneath is different.
    """

    def __init__(self, *, session: Callable[[], OwnerSession | None]) -> None:
        self._session = session

    def _require(self) -> OwnerSession:
        session = self._session()
        if session is None:
            raise OwnerTransportUnavailableError(
                "the owner's Telegram session is not connected"
            )
        return session

    async def _place(self, peer_id: int, media: OutgoingMedia, kind: str) -> int:
        """One attachment, as the owner. Errors travel; they are not reported as None.

        The queue is what decides what a failure means, and it can only decide
        with the exception in hand: a refusal before the request is a retry, a
        silence after it is a question for the owner. Answering None here made
        every one of them look like the first.

        A session that answers without an id is the same unconfirmed outcome the
        Bot API side names: the message may well be in the chat, and there is
        nothing to point at it with.
        """
        session = self._require()
        placed = await session.send_own_file(
            peer_id,
            str(media.path),
            caption=media.caption,
            entities=media.caption_entities,
            kind=kind,
            file_name=media.file_name,
            duration=media.duration_seconds,
            width=media.width,
            height=media.height,
            title=media.title,
            performer=media.performer,
            # The upright cover, drawn by the delivery. A video placed as the
            # owner had the same sideways-cover problem as one sent by the
            # bot, and the fix travels with the media rather than with the
            # transport.
            thumbnail=media.thumbnail,
        )
        if placed is None:
            raise UnconfirmedDeliveryError(
                "the owner's session placed an attachment and named no id"
            )
        return placed

    async def send_photo(
        self, bot_id: int, chat_id: int, media: OutgoingMedia, *, reply_to: int | None
    ) -> int:
        return await self._place(chat_id, media, "photo")

    async def send_video(
        self, bot_id: int, chat_id: int, media: OutgoingMedia, *, reply_to: int | None
    ) -> int:
        return await self._place(chat_id, media, "video")

    async def send_video_note(
        self, bot_id: int, chat_id: int, media: OutgoingMedia, *, reply_to: int | None
    ) -> int:
        return await self._place(chat_id, media, "video_note")

    async def send_voice(
        self, bot_id: int, chat_id: int, media: OutgoingMedia, *, reply_to: int | None
    ) -> int:
        return await self._place(chat_id, media, "voice")

    async def send_audio(
        self, bot_id: int, chat_id: int, media: OutgoingMedia, *, reply_to: int | None
    ) -> int:
        return await self._place(chat_id, media, "audio")

    async def send_document(
        self, bot_id: int, chat_id: int, media: OutgoingMedia, *, reply_to: int | None
    ) -> int:
        return await self._place(chat_id, media, "document")

    async def send_sticker(
        self, bot_id: int, chat_id: int, media: OutgoingMedia, *, reply_to: int | None
    ) -> int:
        # A MAX sticker reaches Telegram as an image; sending it as a sticker
        # from a user account would need it to be in a set the account owns.
        return await self._place(chat_id, media, "photo")

    async def send_album(
        self, bot_id: int, chat_id: int, items: list[OutgoingMedia], *, reply_to: int | None
    ) -> AlbumReceipt:
        """The owner's own album, placed over their own session.

        **Grouped where the transport can carry it, and only there.** Telethon
        1.44.0's album path uploads each file with `messages.uploadMedia` and then
        makes one `messages.sendMultiMedia`, which is a genuine Telegram album and
        atomic at the server. What that path does *not* accept is per-file
        `attributes` or a `thumb`, so it has to infer a video's duration and
        dimensions — and with `hachoir` absent from this environment, measured,
        the inference yields `DocumentAttributeVideo(0, 1, 1)`. A clip that
        arrives as one pixel is worse than a clip that arrives ungrouped.

        So a group of photos goes as one album, and a group containing a video
        goes file by file with its dimensions and its upright cover intact. The
        split is the measured contract, not a preference; if Telethon grows
        per-file attributes for albums, the second branch disappears.

        Either way the receipt is the owner-side ids in file order, checked
        against the same ascending contract the Bot API album is checked against.
        """
        if all(item.kind is not AttachmentKind.VIDEO for item in items):
            return await self._grouped(chat_id, items)
        return await self._one_at_a_time(chat_id, items)

    async def _grouped(self, chat_id: int, items: list[OutgoingMedia]) -> AlbumReceipt:
        session = self._require()
        head = items[0]
        placed = await session.send_own_album(
            chat_id,
            [str(item.path) for item in items],
            caption=head.caption,
            entities=head.caption_entities,
        )
        named = [value for value in placed if value is not None]
        if len(placed) != len(items) or len(named) != len(items):
            # One `sendMultiMedia` made the group, so what is in the chat is in
            # the chat; what is missing is the *identity* of part of it. Binding
            # the ones that were named would map somebody's fourth photo onto
            # their third, and a retry would place the whole album again.
            raise PartialOwnerAlbumError(
                f"the session placed an album of {len(items)} and named {len(named)} of them"
            )
        message_ids = tuple(int(value) for value in named)
        check_album_receipt(message_ids, expected=len(items))
        return AlbumReceipt(message_ids=message_ids)

    async def _one_at_a_time(self, chat_id: int, items: list[OutgoingMedia]) -> AlbumReceipt:
        """The fallback for a group with a video in it. Not an album; still whole.

        A part that answers without an id fails the group: a receipt naming three
        ids for a four-part album would be settled by position. The count is the
        contract, and a short one is a question rather than a delivery.
        """
        placed: list[int] = []
        for index, item in enumerate(items):
            kind = "video" if item.kind is AttachmentKind.VIDEO else "photo"
            # The caption rides on the first item, the same rule the Bot API path
            # follows, because that is where Telegram shows it.
            try:
                placed.append(
                    await self._place(chat_id, item if index == 0 else _uncaptioned(item), kind)
                )
            except UnconfirmedDeliveryError as error:
                raise PartialOwnerAlbumError(
                    f"part {index + 1} of {len(items)} was placed without an id;"
                    f" {len(placed)} part(s) are already in the chat"
                ) from error
        message_ids = tuple(placed)
        check_album_receipt(message_ids, expected=len(items))
        return AlbumReceipt(message_ids=message_ids)

    async def send_text(
        self,
        bot_id: int,
        chat_id: int,
        text: str,
        *,
        reply_to: int | None = None,
        entities: list[dict[str, Any]] | None = None,
        buttons: list[tuple[str, str]] | None = None,
    ) -> int:
        """The caption tail and the per-attachment notices, in the owner's voice.

        `buttons` are ignored here: this path places a message as the *owner*
        over their own session, and a message the owner sent to themselves needs
        no "поднять мост" button — the contact whose card carries it was theirs
        to begin with. The button lives on the bot-delivered copy.
        """
        session = self._require()
        placed = await session.send_own_message(chat_id, text, entities=entities)
        if placed is None:
            raise UnconfirmedDeliveryError(
                "the owner's session placed a line and named no id"
            )
        return placed

    async def send_contact(
        self,
        bot_id: int,
        chat_id: int,
        *,
        phone: str,
        first_name: str,
        last_name: str | None = None,
        vcard: str | None = None,
        reply_to: int | None = None,
    ) -> int:
        """A contact the owner shared, placed in their own voice as a card.

        Deliberately a line rather than a native Telegram contact: a MTProto
        `InputMediaContact` is a different upload shape from the file placement
        everything else here reuses. The owner already has the contact in MAX;
        seeing it on their own side as `👤 Контакт: Имя` is the small cost, and
        the bot path still sends the real, tappable contact to the actual
        recipient.
        """
        name = " ".join(part for part in (first_name, last_name or "") if part).strip()
        card = CONTACT_CARD.format(name=name) if name else CONTACT_CARD_NAMELESS
        return await self.send_text(bot_id, chat_id, card, reply_to=reply_to)

    async def send_photo_url(
        self,
        bot_id: int,
        chat_id: int,
        url: str,
        *,
        caption: str,
        reply_to: int | None = None,
        buttons: list[tuple[str, str]] | None = None,
    ) -> int | None:
        """A contact card placed as the owner keeps its text, not its avatar.

        None so the delivery falls back to `send_text`: this path is the owner's
        own session, where a card of their own contact needs no avatar and no
        button — the same reasoning as `send_text` above.
        """
        return None


def _uncaptioned(media: OutgoingMedia) -> OutgoingMedia:
    from dataclasses import replace

    return replace(media, caption=None, caption_entities=None)
