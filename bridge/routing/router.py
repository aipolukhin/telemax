"""Routing: which message goes where, and which message goes nowhere.

This module owns the two rules that keep the bridge honest:

* **Dedup.** A MAX message is claimed in the database *before* it is sent to
  Telegram. A reconnect replays events, and the claim is what makes the replay
  a no-op instead of a second copy.
* **The loop guard.** Everything the bridge sends to MAX comes back as an event.
  Without a marker the bridge would forward its own message to Telegram, the
  owner would see a duplicate, and a badly ordered handler could bounce it back
  into MAX forever.

It knows nothing about aiogram or PyMax: it takes normalised events and calls a
sender port. That is what makes the whole thing testable without a network.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import tzinfo
from pathlib import Path
from typing import Any, Protocol

from bridge.config import TimestampStyle
from bridge.formatting import (
    FORWARD_FROM,
    elements_to_entities,
    format_edit_mark,
    format_stamp,
    forward_header,
    shift_entities,
    strip_presentation,
    utf16_length,
)
from bridge.max_client import (
    AttachmentKind,
    IncomingMaxMessage,
    MaxAttachment,
    MessageDeleted,
)
from bridge.media.delivery import album_attachments, split_caption
from bridge.routing.delivery import (
    KIND_MAX_TO_TG_DELETE,
    KIND_MAX_TO_TG_EDIT,
    KIND_MAX_TO_TG_MEDIA,
    KIND_MAX_TO_TG_OWNER,
    KIND_MAX_TO_TG_TEXT,
    KIND_OWNER_ECHO_BIND,
    KIND_TG_ALBUM_SWEEP,
    KIND_TG_TO_MAX_CONTACT,
    KIND_TG_TO_MAX_DELETE,
    KIND_TG_TO_MAX_EDIT,
    KIND_TG_TO_MAX_MEDIA,
    KIND_TG_TO_MAX_REACTION,
    KIND_TG_TO_MAX_TEXT,
    DeliveryPipe,
    UnstorablePayloadError,
    attachment_to_payload,
)
from bridge.routing.echo import (
    OwnEchoes,
    Placed,
    album_part_fingerprint,
    echo_kind_of,
    expected_album_namespace,
    media_echo_fingerprint,
    owner_album_source_key,
    owner_reaction_source_key,
    text_echo_fingerprint,
)
from bridge.routing.max_mutation import (
    delete_source_key as max_delete_source_key,
)
from bridge.routing.max_mutation import (
    edit_source_key as max_edit_source_key,
)
from bridge.routing.owner_echo import album_echo_source_key, echo_source_key
from bridge.routing.owner_mutation import (
    album_sweep_source_key,
    delete_source_key,
    edit_source_key,
    fingerprint,
    send_source_key,
)
from bridge.routing.refusals import classify as classify_refusal
from bridge.routing.settlement import settle_max_delivery_mapping
from bridge.routing.text_chunks import split_max_text, text_part_source_key
from bridge.storage import (
    BridgeStateRepository,
    Direction,
    MediaGroupRepository,
    MessageLink,
    MessageMapRepository,
    SourceMarker,
)

logger = logging.getLogger(__name__)

# Prefix for messages the owner sent from the official MAX app (owner-side events). Neutral
# on purpose: the bridge never labels a chat with a contact's name.
OWN_MESSAGE_PREFIX = "Вы: "


def _source_key(bot_id: int, telegram_message_id: int, owner_account_id: int | None) -> str:
    """The dedup key for one TG→MAX message, per intake transport.

    Bot API keys by the bot's own message id (`tg:{bot}:{id}`). The owner's
    MTProto session has no bot-side id, so it keys by the account and the
    owner-side id (`tg-owner-msg:{account}:{id}`) — a different namespace, so a
    replayed MTProto update dedups against itself and never collides with the
    Bot API row for the same message. One message → one key → one job.
    """
    if owner_account_id is not None:
        return f"tg-owner-msg:{owner_account_id}:{telegram_message_id}"
    return f"tg:{bot_id}:{telegram_message_id}"

# What the owner gets when they send something the bridge cannot carry yet.
UNSUPPORTED_NOTICE = "Пока умею только текст — вложения появятся позже."

# When an edit cannot reach MAX, say so instead of leaving the two sides
# quietly out of step.


class TelegramSender(Protocol):
    """The only thing routing needs from the Telegram side: creating a message.

    Editing and deleting used to be here too, answering `bool`. They have moved
    to `BotMutations`, which raises — the router no longer performs either, and a
    port that reports failure as `False` has nowhere to put the difference
    between "unreachable" and "refused".
    """

    async def send_text(
        self,
        bot_id: int,
        chat_id: int,
        text: str,
        *,
        reply_to: int | None = None,
        entities: list[dict[str, Any]] | None = None,
    ) -> int | None: ...


class MaxSender(Protocol):
    """The only thing routing needs from the MAX side.

    The three creating calls return `int`, never `None`. That is the shared
    boundary talking: a send either names the message it made or raises
    `MaxUnconfirmedSendError`, because an answer that names nothing is exactly as
    unknown as no answer at all. `None` used to mean both "unknown" and "fine,
    no id" depending on which of five call sites you read.

    `edit_text` and `delete_messages` return nothing on purpose: they replace
    state rather than adding to it, so there is no new identity to report and a
    retry is free.
    """

    async def send_text(self, chat_id: int, text: str, *, reply_to: int | None = None) -> int: ...

    async def send_media(
        self,
        chat_id: int,
        items: list[tuple[str, Path, str]],
        *,
        text: str = "",
        reply_to: int | None = None,
    ) -> int: ...

    async def edit_text(self, chat_id: int, message_id: int, text: str) -> None: ...

    async def delete_messages(
        self, chat_id: int, message_ids: list[int], *, for_everyone: bool = True
    ) -> None: ...

    async def send_contact(
        self,
        chat_id: int,
        *,
        vcard: str,
        contact_user_id: int | None = None,
        reply_to: int | None = None,
    ) -> int: ...


class BridgeLookup(Protocol):
    """Resolves the one-to-one mapping in both directions."""

    def bridge_for_max_chat(self, max_chat_id: int) -> BridgeTarget | None: ...

    def bridge_for_bot(self, bot_id: int) -> BridgeTarget | None: ...


@dataclass(frozen=True, slots=True)
class BridgeTarget:
    name: str
    max_chat_id: int
    bot_id: int


@dataclass(frozen=True, slots=True)
class _OwnerTarget:
    """What an owner-side message id resolves to, for a mutation to act on.

    Four answers to one question, because an album makes them four different
    things: the canonical mapping, the owner-side id that mapping is written
    under, the send job the mutation may have to coalesce into or cancel, and the
    key its own job is filed under — which for an album is the group, so that
    three deleted parts are one delete.
    """

    link: MessageLink
    canonical_id: int
    send_key: str
    key: str


#: Where a message from a contact with no bridge goes (dynamic provisioning).
UnbridgedHandler = Callable[[IncomingMaxMessage], Awaitable[None]]


class MediaDelivery(Protocol):
    """Sends the attachments of a MAX message (inbound media delivery)."""

    async def deliver(
        self,
        message: IncomingMaxMessage,
        *,
        bot_id: int,
        chat_id: int,
        caption: str = "",
        caption_entities: list[dict[str, Any]] | None = None,
        reply_to: int | None = None,
    ) -> int | None: ...


class OwnVoice(Protocol):
    """Places a message in the owner's chat as the owner, not as the bot.

    The owner's own MTProto session does it: their account writing in their own
    chat, which is what turns a backfilled "Вы: привет" bot line into the owner's
    own message on the right-hand side. Secretary Mode did this first, over a
    business connection lent to the guardian; that path is gone, and with it the
    four conditions it needed.

    Returns False rather than raising when it cannot: the caller then delivers the
    ordinary way, which is worse-looking and still correct.
    """

    @property
    def available(self) -> bool: ...

    async def send_as_owner(
        self,
        chat_id: int,
        text: str,
        *,
        entities: list[dict[str, Any]] | None = None,
    ) -> int | None: ...


class DeliveryObserver(Protocol):
    """Told when something moved, so the status line can keep up (read and presence state).

    Routing does not know what a tick looks like; it only knows that MAX
    accepted a message, and that the Telegram chat has new content below
    whatever the status line was pinned to.
    """

    def note_chat_activity(self, bridge_name: str) -> None: ...

    async def note_delivered(
        self, bridge_name: str, *, bot_id: int, chat_id: int, at_ms: int
    ) -> None: ...


class DisplayNames(Protocol):
    """Who a MAX user id belongs to, for naming the author of a forward.

    Two values because a MAX user has two names and only one of them may leave
    the owner's screen: `CUSTOM` is the owner's own address-book label and
    `ONEME` is what the person calls themselves (confirmed in compatibility tests —
    `CUSTOM: "Мама"`, `ONEME: "Имя профиля"`). A forward names its author *to
    somebody else*, so the private label is exactly the wrong one.

    The link is `https://max.ru/<handle>` when the author set one, so the name
    can be the address the owner would follow to reach them.
    """

    async def forward_author(self, user_id: int) -> tuple[str | None, str | None]: ...

    async def contact_avatar(self, user_id: int) -> str | None: ...


#: How many forwarded authors to remember. A dialog forwards from a handful of
#: places, so this is generous; the point of the bound is that a long-lived
#: process cannot grow a name cache without limit.
FORWARD_NAME_CACHE_LIMIT = 256


class BridgeRouter:
    def __init__(
        self,
        *,
        lookup: BridgeLookup,
        telegram: TelegramSender,
        max_sender: MaxSender,
        messages: MessageMapRepository,
        state: BridgeStateRepository,
        owner_chat_id: int,
        timestamp_style: TimestampStyle = TimestampStyle.COMPACT,
        timezone: tzinfo | None = None,
        on_unbridged: UnbridgedHandler | None = None,
        delivery_observer: DeliveryObserver | None = None,
        media: MediaDelivery | None = None,
        own_voice: OwnVoice | None = None,
        own_media: MediaDelivery | None = None,
        own_echoes: OwnEchoes | None = None,
        mirror_own_messages: Callable[[], bool] = lambda: True,
        pipe: DeliveryPipe | None = None,
        albums: MediaGroupRepository | None = None,
        display_names: DisplayNames | None = None,
    ) -> None:
        self._lookup = lookup
        self._telegram = telegram
        self._max = max_sender
        self._messages = messages
        self._state = state
        self._owner_chat_id = owner_chat_id
        self._timestamp_style = timestamp_style
        self._timezone = timezone
        self._on_unbridged = on_unbridged
        self._observer = delivery_observer
        self._media = media
        self._own_voice = own_voice
        # The same pipeline as `media`, sending through the guardian on the
        # owner's behalf. Separate object rather than a flag: it is a different
        # bot and a different chat id.
        self._own_media = own_media
        # What was written as the owner, so the contact bot's copy of it does not
        # travel back into MAX as a duplicate.
        self._own_echoes = own_echoes
        # Read fresh each time, not captured: the owner can flip it from the
        # guardian while the bridge runs, and a message they send from MAX a
        # second later must see the new answer. Defaults to mirroring so a router
        # built without the resolver keeps the old behaviour.
        self._mirror_own_messages = mirror_own_messages
        # The durable queue. Optional so unit tests can drive routing without a
        # database; in the running service it is always present, and without it
        # a failed send has nowhere to be remembered.
        self._pipe = pipe
        # Per-part aliases of an album. Optional for the same reason as the
        # queue; without it an album is still one message, it simply cannot be
        # resolved from any part but the one the mapping was written under.
        self._albums = albums
        # Names for the authors of forwarded messages, and a cache for them.
        # Optional: without it a forward still arrives, marked but unattributed,
        # which is far better than not arriving.
        self._display_names = display_names
        self._forward_names: dict[int, tuple[str | None, str | None]] = {}

    def target_for_bot(self, bot_id: int) -> BridgeTarget | None:
        return self._lookup.bridge_for_bot(bot_id)

    def bridge_name_for_bot(self, bot_id: int) -> str | None:
        """Which bridge a contact bot belongs to, by name. Nothing else needed.

        The MTProto transport has to label an album's parts with their bridge
        before any of them is carried anywhere, and this is the whole of what it
        asks the router for.
        """
        target = self._lookup.bridge_for_bot(bot_id)
        return target.name if target is not None else None

    # ------------------------------------------------------------- MAX -> Telegram

    async def on_max_message(self, message: IncomingMaxMessage) -> None:
        target = self._lookup.bridge_for_max_chat(message.chat_id)
        if target is None:
            # No bridge for this dialog: hand it to provisioning, which decides
            # whether to ask the owner about this contact (dynamic provisioning).
            if self._on_unbridged is not None and not message.is_outgoing:
                await self._on_unbridged(message)
            else:
                logger.debug("ignoring a message from unbridged MAX chat %s", message.chat_id)
            return

        if await self._messages.is_echo_of_our_own(message.chat_id, message.message_id):
            # Our own delivery coming back. Forwarding it would duplicate the
            # message the owner just sent.
            logger.debug("suppressed the echo of message %s", message.message_id)
            return

        if message.is_outgoing and not self._mirror_own_messages():
            # A message the owner wrote in the MAX app, and they have asked not to
            # carry those. Dropped before the claim so no map row is left behind:
            # if it is not going to be delivered, it should leave nothing to
            # reconcile. Turning the setting on again affects later messages, not
            # this one — MAX will not resend it.
            logger.debug(
                "own MAX message %s not carried: own-message mirroring is off",
                message.message_id,
            )
            return

        # Before the fingerprint: a forward renders with a line naming its
        # author, and an echo can only be matched against what was actually put
        # in the chat.
        message = await self._name_forward(message)
        # Before the job is built from the attachments: a shared contact's card
        # gets the full-resolution avatar, not the 190px thumbnail on the wire.
        message = await self._enrich_contact_avatar(message)

        link_id = await self._messages.claim_from_max(
            bridge_name=target.name,
            max_chat_id=message.chat_id,
            max_message_id=message.message_id,
            telegram_bot_id=target.bot_id,
            telegram_chat_id=self._owner_chat_id,
            # Written with the claim, before anything is sent: the owner's session
            # will see this message with an id of its own, and this is the only
            # thing the two sides can be joined by.
            echo_fingerprint=self._echo_fingerprint(message),
        )
        if link_id is None:
            logger.debug("message %s was already delivered", message.message_id)
            return

        prefix = self._prefix(message)
        entities = elements_to_entities(message.elements)
        if entities:
            # `_render` prepends the stamp, the "you" marker and the forward
            # line, so every range moves by however much text went in front of
            # the body — counted in UTF-16 units, because a contact's name can
            # hold an emoji and `len()` would then be one short of the truth.
            entities = shift_entities(entities, utf16_length(prefix))
        entities = self._forward_entities(prefix, message) + entities
        reply_target = await self._telegram_reply_target(message, target)

        if await self._deliver_as_owner(message, target, link_id):
            # The owner's own message, placed as theirs. There is no Telegram id
            # to map: it belongs to the owner's account, and the bot can neither
            # edit it nor react to it. Dedup still holds — the claim above is what
            # stops a second copy, not the mapping.
            if self._observer is not None:
                self._observer.note_chat_activity(target.name)
            return

        if message.attachments and self._media is not None:
            # The text becomes the caption of the first attachment (inbound media delivery).
            caption = self._caption(message)
            telegram_message_id = await self._send_media_durably(
                target=target,
                link_id=link_id,
                message=message,
                caption=caption,
                entities=entities + self._mark_entities(message, caption),
                reply_to=reply_target,
            )
        else:
            text = self._render(message)
            if not text:
                # Nothing to send: no text, no attachment, nothing passed on.
                # The claim is already on disk, so returning here is what left
                # thirty rows in the live database that no MAX replay can get
                # past — the row says "known", the queue has never heard of the
                # message, and nothing anywhere says it was dropped. Accounted
                # instead, terminally, with a reason. `_render` is deterministic
                # for a given message, so a replay would decide the same thing;
                # releasing the claim would only loop.
                await self._account_nothing_to_deliver(target=target, message=message)
                return
            telegram_message_id = await self._send_text_durably(
                target=target,
                link_id=link_id,
                message=message,
                text=text,
                entities=entities + self._mark_entities(message, text),
                reply_to=reply_target,
            )

        if telegram_message_id is not None:
            await self._messages.attach_telegram_message(link_id, telegram_message_id)
            if self._observer is not None:
                # The status line is no longer the last thing in the chat.
                self._observer.note_chat_activity(target.name)
            # Only now. This used to run unconditionally, so a send that
            # returned nothing still moved "last delivery" forward and /status
            # cheerfully reported a message that is not in the chat.
            await self._state.note_delivery(target.name)

    async def _account_nothing_to_deliver(
        self, *, target: BridgeTarget, message: IncomingMaxMessage
    ) -> None:
        """One MAX event that renders to nothing, written down as such.

        The same `source_key` a delivery would have used, so a replay finds this
        instead of claiming again — and so the reconciler at startup can tell a
        message that was deliberately not sent from one whose job never got
        written.
        """
        if self._pipe is None:
            return
        await self._pipe.submit_noop(
            bridge_name=target.name,
            direction=Direction.MAX_TO_TG,
            kind=KIND_MAX_TO_TG_TEXT,
            source_key=f"max:{message.chat_id}:{message.message_id}",
            reason="the MAX message renders to no text and carries nothing to send",
            reference={
                "max_chat_id": message.chat_id,
                "max_message_id": message.message_id,
                "bot_id": target.bot_id,
            },
        )

    async def _send_media_durably(
        self,
        *,
        target: BridgeTarget,
        link_id: int,
        message: IncomingMaxMessage,
        caption: str,
        entities: list[dict[str, Any]] | None,
        reply_to: int | None,
    ) -> int | None:
        """Attachments, through the same queue text goes through.

        They used to go straight out through `MaxMediaDelivery.deliver()`. The
        dedup claim is written before either branch, so a media send that failed
        left a row behind and the MAX replay that followed read it as "already
        delivered" — the loss path the outbox exists to close, still open for
        one branch of one method.

        The whole message is **one job**, however many attachments it carries:
        an album is one thing the owner sent and one thing the contact reads,
        and splitting it would let half of it arrive.
        """
        payload = {
            "max_chat_id": message.chat_id,
            "max_message_id": message.message_id,
            "bot_id": target.bot_id,
            "chat_id": self._owner_chat_id,
            "link_id": link_id,
            "caption": caption,
            "caption_entities": entities or None,
            "reply_to": reply_to,
            "text": message.text,
            "timestamp": message.timestamp,
            "is_outgoing": message.is_outgoing,
            "reply_to_message_id": message.reply_to_message_id,
            "attachments": [attachment_to_payload(item) for item in message.attachments],
        }

        # Before the job, therefore before the first Telegram call: the parts
        # this album *will* have, in the order it will have them. Written now
        # because after the send there is nothing left to say what was expected —
        # and an echo of the album can already exist by then.
        await self._expect_album(target=target, link_id=link_id, message=message, caption=caption)

        if self._pipe is None:
            # No queue wired (unit tests, and the guardian's own paths).
            assert self._media is not None
            return await self._media.deliver(
                message,
                bot_id=target.bot_id,
                chat_id=self._owner_chat_id,
                caption=caption,
                caption_entities=entities or None,
                reply_to=reply_to,
            )

        try:
            job_id, ours = await self._pipe.submit_in_order(
                bridge_name=target.name,
                direction=Direction.MAX_TO_TG,
                kind=KIND_MAX_TO_TG_MEDIA,
                payload=payload,
                # The same key text uses: a MAX message takes one branch or the
                # other, never both, so one event is always one job.
                source_key=f"max:{message.chat_id}:{message.message_id}",
            )
        except UnstorablePayloadError as error:
            # This used to fall back to `self._media.deliver(...)` — a direct
            # send past the queue, reintroducing the exact bypass this path was
            # built to close. A weaker guarantee is not worth an escape hatch
            # that a future reader would take for a supported route.
            #
            # Instead the job exists and is failed with a reason. The message is
            # not delivered, and it is not lost either: it is on the owner's
            # `/failed` list with an explanation.
            logger.error(
                "bridge %s: attachments of message %s could not be described for the"
                " queue; recorded as failed rather than sent past it",
                target.name,
                message.message_id,
            )
            await self._pipe.submit_unstorable(
                bridge_name=target.name,
                direction=Direction.MAX_TO_TG,
                kind=KIND_MAX_TO_TG_MEDIA,
                source_key=f"max:{message.chat_id}:{message.message_id}",
                reason=str(error),
                reference={
                    "max_chat_id": message.chat_id,
                    "max_message_id": message.message_id,
                    "bot_id": target.bot_id,
                    "link_id": link_id,
                },
            )
            return None
        if not ours:
            logger.debug("media for message %s is already in hand", message.message_id)
            return None

        settled = await self._pipe.attempt(
            job_id=job_id,
            bridge_name=target.name,
            direction=Direction.MAX_TO_TG,
            kind=KIND_MAX_TO_TG_MEDIA,
            payload=payload,
        )
        return settled.remote_message_id

    async def _expect_album(
        self,
        *,
        target: BridgeTarget,
        link_id: int,
        message: IncomingMaxMessage,
        caption: str,
    ) -> None:
        """Write down the parts this album will have, before it has any.

        The same ordering argument the claim itself rests on. An album's parts
        each get an id of their own from Telegram and a *second* id of their own
        in the owner's client, and neither can be joined to the other after the
        fact — so the row that will hold both is created first, with the position
        and the structure it is expected to match, and the send fills it in.

        Deliberately keyed by position and not by content: the parts of an album
        can be byte-identical, and the probe confirmed nothing separates them but
        order. The fingerprint says "a photo, third, carrying no caption"; which
        third photo it is, is the index.

        Idempotent by the logical unique index: a re-entry after a crash writes
        nothing and refuses nothing, it simply finds its rows already there.
        """
        if self._albums is None:
            return
        attachments = album_attachments(message)
        if not attachments:
            # A lone photo, or nothing that shares a `sendMediaGroup`. One
            # message, one mapping row, no parts to alias.
            return
        head_caption, _ = split_caption(caption)
        namespace = expected_album_namespace(link_id)
        for index, attachment in enumerate(attachments):
            # Telegram shows an album's caption on its first item, and that is
            # where the sender puts it — so on this side the position is known,
            # unlike an incoming album where it is wherever it was typed.
            part_caption = head_caption if index == 0 else None
            kind = echo_kind_of(attachment.kind.value)
            await self._albums.add_part(
                media_group_id=namespace,
                bridge_name=target.name,
                bot_id=target.bot_id,
                payload={"kind": attachment.kind.value},
                link_id=link_id,
                direction=Direction.MAX_TO_TG,
                part_index=index,
                media_kind=kind,
                caption_present=part_caption is not None,
                part_fingerprint=album_part_fingerprint(
                    kind, part_index=index, caption=part_caption
                ),
            )

    async def _send_text_durably(
        self,
        *,
        target: BridgeTarget,
        link_id: int,
        message: IncomingMaxMessage,
        text: str,
        entities: list[dict[str, Any]] | None,
        reply_to: int | None,
    ) -> int | None:
        """Send to Telegram with the job on disk first.

        Without a queue this is where messages died: the dedup claim was already
        written, so a failed send left a row that the next MAX replay read as
        "already delivered". Now the job exists before the attempt, and a failure
        leaves it for the worker instead of leaving nothing at all.
        """
        payload = {
            "bot_id": target.bot_id,
            "chat_id": self._owner_chat_id,
            "text": text,
            "entities": entities or None,
            "reply_to": reply_to,
            "link_id": link_id,
        }

        if self._pipe is None:
            # No queue wired (unit tests, and the guardian's own paths). Behave
            # as before rather than refusing to work.
            return await self._telegram.send_text(
                target.bot_id,
                self._owner_chat_id,
                text,
                reply_to=reply_to,
                entities=entities or None,
            )

        job_id, ours = await self._pipe.submit_in_order(
            bridge_name=target.name,
            direction=Direction.MAX_TO_TG,
            kind=KIND_MAX_TO_TG_TEXT,
            payload=payload,
            source_key=f"max:{message.chat_id}:{message.message_id}",
        )
        if not ours:
            # Already delivered, or a worker is carrying it. Sending here would
            # be the second copy.
            logger.debug("message %s is already in hand", message.message_id)
            return None
        settled = await self._pipe.attempt(
            job_id=job_id,
            bridge_name=target.name,
            direction=Direction.MAX_TO_TG,
            kind=KIND_MAX_TO_TG_TEXT,
            payload=payload,
        )
        return settled.remote_message_id

    def _echo_fingerprint(self, message: IncomingMaxMessage) -> str | None:
        """The canonical form of what this message will put in the Telegram chat.

        It has to be decided here, before the claim, because after the send it is
        too late — the job payload that held the text is cleared the moment the
        delivery is confirmed. So this mirrors the branch `on_max_message` is
        about to take, and says None wherever the outgoing shape is not something
        an echo can be matched against by structure alone: an album (its own
        increment), a sticker or a call, which do not arrive as what they claim.
        """
        if message.attachments:
            if self._media is None or len(message.attachments) != 1:
                return None
            kind = echo_kind_of(message.attachments[0].kind.value)
            if kind is None:
                return None
            return media_echo_fingerprint(kind, caption=self._caption(message))
        text = self._render(message)
        return text_echo_fingerprint(text) if text else None

    async def _telegram_reply_target(
        self, message: IncomingMaxMessage, target: BridgeTarget
    ) -> int | None:
        """Which Telegram message this one is answering, if we delivered it.

        A reply to something older than the bridge has no counterpart. It is
        delivered as a plain message rather than dropped: losing the answer
        would be worse than losing the visual thread.
        """
        if message.reply_to_message_id is None:
            return None
        link = await self._messages.by_max_message(
            message.chat_id, message.reply_to_message_id, target.bot_id
        )
        return link.telegram_message_id if link else None

    async def _deliver_as_owner(
        self, message: IncomingMaxMessage, target: BridgeTarget, link_id: int
    ) -> bool:
        """Decide that a message the owner wrote in MAX goes out as theirs.

        **A decision, not a send.** It writes a job and returns; the placement
        itself happens in the queue's sender like every other delivery. That
        boundary is the whole point of this method now. It used to call the
        session here, with the dedup claim already on disk, so a crash or a
        refusal left a `message_map` row with nothing behind it and the next MAX
        replay read it as "already delivered" — 30 such rows were still in the
        live database when this was found. Worse, a timeout *after* Telegram had
        accepted the message dropped through to the Bot API path and put the same
        line in the chat a second time, signed `Вы: …`.

        One condition, checked here and only here: **the owner's session is
        connected.** That is the last moment a fallback is honest. Before the job
        exists, choosing the contact bot instead is a routing decision; after it,
        the message is owed by one transport and sending it through another is
        how a chat gets two of everything. So a session that goes away *after*
        this point does not reroute anything — the job waits for it.

        Attachments go the same way. They were left out at first on the grounds
        that a photo "already reads as the owner's", which is simply wrong: a
        photo the bot sends sits on the bot's side of the screen under a `Вы:`
        caption, exactly like a line of text.

        The chat is addressed by the contact bot's own id: from the owner's
        account the bot *is* the other side of that conversation.
        """
        if not message.is_outgoing:
            return False
        if self._own_voice is None or not self._own_voice.available:
            return False
        if self._pipe is None:
            # No queue wired (unit tests build routers without a database).
            # Production always has one — `test_production_always_wires_the_queue`
            # is what keeps that true — so this is not a fallback, it is the
            # absence of the thing that would make the decision durable.
            return False
        if message.attachments and self._own_media is None:
            return False

        prefix = self._prefix_as_owner(message)
        entities = elements_to_entities(message.elements)
        if entities:
            entities = shift_entities(entities, utf16_length(prefix))
        entities = self._forward_entities(prefix, message) + entities

        payload: dict[str, Any] = {
            "max_chat_id": message.chat_id,
            "max_message_id": message.message_id,
            "bot_id": target.bot_id,
            # From the owner's account the contact bot is the peer *and* the
            # chat, which is why one id appears twice rather than two ids once.
            "peer_id": target.bot_id,
            "link_id": link_id,
            "owner_account_id": self._owner_chat_id,
            "timestamp": message.timestamp,
            "is_outgoing": True,
            "reply_to_message_id": message.reply_to_message_id,
        }
        if message.attachments:
            own_caption = self._caption_as_owner(message)
            payload.update(
                {
                    "text": message.text,
                    "caption": own_caption,
                    "caption_entities": (
                        entities + self._mark_entities(message, own_caption)
                    ) or None,
                    "attachments": [
                        attachment_to_payload(item) for item in message.attachments
                    ],
                }
            )
            # The aliases this album will have, written before the job the same
            # way the bot path writes them — the parts are what the owner-side
            # ids settle onto, and there is nothing to settle onto afterwards.
            await self._expect_album(
                target=target,
                link_id=link_id,
                message=message,
                caption=payload["caption"],
            )
        else:
            outgoing = self._render_as_owner(message)
            if not outgoing:
                return False
            payload.update(
                {
                    "outgoing_text": outgoing,
                    "entities": (entities + self._mark_entities(message, outgoing)) or None,
                }
            )

        return await self._submit_owner_delivery(target=target, message=message, payload=payload)

    async def _submit_owner_delivery(
        self, *, target: BridgeTarget, message: IncomingMaxMessage, payload: dict[str, Any]
    ) -> bool:
        """Put the owner placement on the queue and take the first attempt.

        Returns True once the job exists, whatever the attempt makes of it. The
        message is accounted for from that moment: delivered, retrying, or on the
        owner's `/failed` list with a reason — and in none of those cases may it
        also go out as a bot line.
        """
        assert self._pipe is not None
        try:
            job_id, ours = await self._pipe.submit_in_order(
                bridge_name=target.name,
                direction=Direction.MAX_TO_TG,
                kind=KIND_MAX_TO_TG_OWNER,
                payload=payload,
                # The same key the other two MAX→TG kinds use: a message takes
                # one branch or another, never both, so one event is one job.
                source_key=f"max:{message.chat_id}:{message.message_id}",
            )
        except UnstorablePayloadError as error:
            await self._pipe.submit_unstorable(
                bridge_name=target.name,
                direction=Direction.MAX_TO_TG,
                kind=KIND_MAX_TO_TG_OWNER,
                source_key=f"max:{message.chat_id}:{message.message_id}",
                reason=str(error),
                reference={
                    "max_chat_id": message.chat_id,
                    "max_message_id": message.message_id,
                    "link_id": payload.get("link_id"),
                },
            )
            return True
        if not ours:
            # Already placed, or a worker is carrying it. Sending here would be
            # the second copy.
            logger.debug("owner message %s is already in hand", message.message_id)
            return True
        await self._pipe.attempt(
            job_id=job_id,
            bridge_name=target.name,
            direction=Direction.MAX_TO_TG,
            kind=KIND_MAX_TO_TG_OWNER,
            payload=payload,
        )
        return True

    async def note_own_placement(
        self, *, bot_id: int, telegram_message_id: int, placed: Placed
    ) -> None:
        """Map a message we placed as the owner, using the bot's own id for it.

        The id that came back from the *send* is in the owner's numbering and is
        useless to the bot. The id the bot sees is in the copy it receives — the
        one suppressed as an echo — so that is where the mapping comes from. Until
        it is written, a reply in Telegram to the owner's own line resolves to
        nothing and reaches MAX as a bare message, which is exactly what happened.
        """
        link = await self._messages.by_max_message(
            placed.max_chat_id, placed.max_message_id, bot_id
        )
        if link is None or link.telegram_message_id is not None:
            return
        await self._messages.attach_telegram_message(link.id, telegram_message_id)

    def _caption_as_owner(self, message: IncomingMaxMessage, *, mark: bool = True) -> str:
        """As `_caption`, without the "you" marker."""
        body = message.text.strip()
        if not body:
            return self._prefix_as_owner(message).strip()
        return f"{self._prefix_as_owner(message)}{body}{self._edit_mark(message, show=mark)}"

    def _render_as_owner(self, message: IncomingMaxMessage, *, mark: bool = True) -> str:
        """As `_render`, without the "you" marker: it is the owner's own message."""
        body = message.text.strip()
        if not body:
            # A forward with no words of its own still says something — who
            # wrote the thing — so it is worth a message. Anything else with an
            # empty body is nothing, and a bare stamp is not worth placing.
            return self._prefix_as_owner(message).strip() if message.forward else ""
        return f"{self._prefix_as_owner(message)}{body}{self._edit_mark(message, show=mark)}"

    def _stamp(self, message: IncomingMaxMessage) -> str:
        """When the message was written — the *original*, if it was forwarded.

        A forward's envelope carries the moment it was passed on, which on a
        live bridge is always the current minute and therefore tells the owner
        nothing. The interesting time is the one on the message inside, and it
        is often days old, which is exactly when a stamp earns its place.
        """
        at = message.timestamp
        if message.forward is not None and message.forward.original_timestamp:
            at = message.forward.original_timestamp
        return format_stamp(at, self._timestamp_style, tz=self._timezone)

    def _forward_line(self, message: IncomingMaxMessage) -> str:
        """`↪ Переслано от Аня\\n`, or nothing at all for an ordinary message."""
        if message.forward is None:
            return ""
        forward = message.forward
        return forward_header(forward.display_name or forward.chat_name)

    def _forward_entities(self, prefix: str, message: IncomingMaxMessage) -> list[dict[str, Any]]:
        """Italicise the forward line, and link the name to its MAX profile.

        Measured against `prefix` rather than rebuilt from the parts: the line
        sits after the stamp, and the two have to agree on where that is or the
        italics land on the clock.

        Entities rather than markdown, unlike the line built for the other
        direction: this side already carries formatting as Telegram ranges, so
        a name holding a bracket or an asterisk cannot break anything here.
        """
        line = self._forward_line(message)
        if not line:
            return []
        start = utf16_length(prefix) - utf16_length(line)
        # The newline is a separator, not part of the label; including it makes
        # Telegram italicise the first character of somebody's message.
        length = utf16_length(line.rstrip("\n"))
        if start < 0 or length <= 0:
            return []

        entities: list[dict[str, Any]] = [
            {"type": "italic", "offset": start, "length": length}
        ]
        forward = message.forward
        link = forward.profile_link if forward is not None else None
        if link:
            # Over the name alone, not the whole line: «Переслано от» is our
            # wording and links to nothing. The name is what the line ends with,
            # so its start is the end minus its own length.
            name = line.rstrip("\n").removeprefix(FORWARD_FROM).strip()
            name_length = utf16_length(name)
            if name_length:
                entities.append(
                    {
                        "type": "text_link",
                        "offset": start + length - name_length,
                        "length": name_length,
                        "url": link,
                    }
                )
        return entities

    def _prefix(self, message: IncomingMaxMessage) -> str:
        """Everything that goes in front of the body, stamp first.

        Kept in one place because the entity offsets have to move by exactly
        this many characters.
        """
        # A message the owner sent from the MAX app themselves (owner-side events). Only used
        # when it cannot be placed as the owner's own — see `_deliver_as_owner`.
        marker = OWN_MESSAGE_PREFIX if message.is_outgoing else ""
        return f"{self._stamp(message)}{marker}{self._forward_line(message)}"

    def _prefix_as_owner(self, message: IncomingMaxMessage) -> str:
        """As `_prefix`, without the "you" marker: it is the owner's own message."""
        return f"{self._stamp(message)}{self._forward_line(message)}"

    async def _enrich_contact_avatar(
        self, message: IncomingMaxMessage
    ) -> IncomingMaxMessage:
        """Swap a shared contact's thumbnail for its full-resolution avatar.

        A CONTACT attach can carry a small `photoUrl`; the
        profile's `baseUrl` is the 1440px original, the same one the guardian's
        confirmation card shows. So when the contact is a MAX user, the avatar is
        looked up from the profile and the attachment's photo is replaced with
        it — a face on the card at the resolution a face deserves.

        Only a contact with a MAX id is enriched: a raw vCard contact has no
        profile to ask, and keeps whatever `photoUrl` it arrived with. A lookup
        that fails leaves the thumbnail in place rather than costing the message.
        """
        if self._display_names is None:
            return message
        attachments = message.attachments
        if not any(item.kind is AttachmentKind.CONTACT for item in attachments):
            return message

        enriched: list[MaxAttachment] = []
        changed = False
        for item in attachments:
            if item.kind is AttachmentKind.CONTACT and item.contact_user_id is not None:
                avatar: str | None = None
                with contextlib.suppress(Exception):
                    avatar = await self._display_names.contact_avatar(item.contact_user_id)
                if avatar:
                    enriched.append(replace(item, contact_photo_url=avatar))
                    changed = True
                    continue
            enriched.append(item)
        if not changed:
            return message
        return replace(message, attachments=tuple(enriched))

    async def _name_forward(self, message: IncomingMaxMessage) -> IncomingMaxMessage:
        """Put a name on a forward's author, once, before anything is rendered.

        Resolved here rather than inside the renderers because those are called
        four times per message and are — deliberately — synchronous. Asking MAX
        who a user id is costs a round trip, so the answer is cached, including
        the answer "MAX does not say", which would otherwise be re-asked for
        every message forwarded from the same silent account.
        """
        forward = message.forward
        if forward is None or forward.display_name or forward.chat_name:
            # A channel forward is already labelled by MAX itself, and the
            # label the contact saw beats anything a lookup could invent.
            return message
        if forward.sender_id is None or self._display_names is None:
            return message

        user_id = forward.sender_id
        if user_id in self._forward_names:
            name, link = self._forward_names[user_id]
        else:
            name, link = None, None
            with contextlib.suppress(Exception):
                # A name is decoration; a message is not. A lookup that fails
                # must not cost the delivery.
                name, link = await self._display_names.forward_author(user_id)
            if len(self._forward_names) >= FORWARD_NAME_CACHE_LIMIT:
                self._forward_names.clear()
            self._forward_names[user_id] = (name, link)

        if not name and not link:
            return message
        return replace(
            message, forward=replace(forward, display_name=name, profile_link=link)
        )

    def _caption(self, message: IncomingMaxMessage, *, mark: bool = True) -> str:
        """Text that rides along with the attachments, without the placeholder."""
        body = message.text.strip()
        if not body:
            # A stamp alone is worth keeping: it is the only thing that says when
            # a backfilled photo was actually sent.
            return self._prefix(message).strip()
        return f"{self._prefix(message)}{body}{self._edit_mark(message, show=mark)}"

    def _render(self, message: IncomingMaxMessage, *, mark: bool = True) -> str:
        """Text of a MAX message as it should appear in Telegram."""
        body = message.text.strip()

        if message.attachments and not body:
            kinds = ", ".join(sorted({item.kind.value for item in message.attachments}))
            body = f"[{kinds}]"
        elif message.attachments:
            kinds = ", ".join(sorted({item.kind.value for item in message.attachments}))
            body = f"{body}\n[{kinds}]"

        if not body:
            # The forward line is content of its own: a message passed on with
            # nothing in it that we can render is still a message that arrived,
            # and returning "" here is what used to lose it silently.
            return self._prefix(message).strip() if message.forward else ""
        return f"{self._prefix(message)}{body}{self._edit_mark(message, show=mark)}"

    def _edit_mark(self, message: IncomingMaxMessage, *, show: bool = True) -> str:
        """When MAX says the text changed, in every rendering and every case.

        Telegram's own label cannot be made to appear consistently: measured
        seven ways and confirmed by the schema, `messages.editMessage` carries no
        field for it and the datacenter decides — a bot's `editMessageText` comes
        back with `edit_hide` set and no client draws anything, while a caption
        edit and the owner's own edits are labelled. Media makes no difference: a
        text message carrying a web page preview is hidden just the same.

        So the one mark that *can* be consistent is this one, and it is drawn
        everywhere rather than only where Telegram is silent. On a caption or an
        owner edit that means two marks — the price of every edited message
        looking alike, and `show=False` is one call away if that trade changes.
        """
        if not show:
            return ""
        return format_edit_mark(message.edited_at, self._timestamp_style, tz=self._timezone)

    def _mark_entities(self, message: IncomingMaxMessage, rendered: str) -> list[dict[str, Any]]:
        """Italicise the mark, so it reads as a note about the message.

        Measured against the *rendered* body rather than rebuilt from the parts:
        the mark is its suffix, so its offset is the whole minus its own length,
        in UTF-16 units — a body ending in an emoji would otherwise put the
        italics one unit off.
        """
        mark = self._edit_mark(message)
        if not mark or not rendered.endswith(mark):
            return []
        length = utf16_length(mark)
        return [{"type": "italic", "offset": utf16_length(rendered) - length, "length": length}]

    async def on_max_edit(self, message: IncomingMaxMessage) -> None:
        """An edit in MAX becomes a durable job, not a Telegram call from here.

        It used to be the call: `edit_message_text`, straight out of this
        handler, through an adapter that answered `False` for everything. So a
        caption edit — which Telegram refuses on a media message, because the
        body lives in the caption — was dropped in silence, and so was every
        edit that arrived while Telegram was briefly unreachable.

        The job knows nothing about *how* to apply it. Which id, which id space
        and which of the two edit methods are decided when it runs, from state
        that is on disk by then — see `max_mutation.resolve_max_edit`.
        """
        target = self._lookup.bridge_for_max_chat(message.chat_id)
        if target is None:
            return

        link = await self._messages.by_max_message(
            message.chat_id, message.message_id, target.bot_id
        )
        if link is None:
            # Edited before the bridge existed. Sending the new text as a fresh
            # message would be worse than staying quiet: the owner would see the
            # same message twice with no explanation.
            logger.debug("edit for an unmapped message %s", message.message_id)
            return

        message = await self._name_forward(message)
        # **Both renderings travel, and the job picks.** The body a message wears
        # depends on who put it in the chat: a bot line carries the `Вы: ` marker
        # and, when it is text, the list of attachment kinds; a message the owner
        # placed as themselves carries neither, because it is already on their
        # side of the screen. Authorship is only known where the job runs, and
        # re-rendering there is impossible — the MAX message is long gone. So the
        # ingress renders both and the resolver chooses.
        #
        # A media message is captioned rather than written: `_caption` is the
        # renderer its body came from, and `_render` would append `[photo]` to a
        # caption that never had it. Both mistakes used to be invisible, because
        # the edit was refused by Telegram and the refusal was swallowed.
        # A MAX edit event arrives carrying no attachments at all — measured — so
        # an album's edit is indistinguishable from a text message's here. It no
        # longer matters: the mark is drawn on every rendering, so neither the
        # form nor the author changes what this produces.
        captioned = bool(message.attachments)
        bot_text, bot_prefix = (
            (self._caption(message), self._prefix(message))
            if captioned
            else (self._render(message), self._prefix(message))
        )
        owner_text, owner_prefix = (
            (self._caption_as_owner(message), self._prefix_as_owner(message))
            if captioned
            else (self._render_as_owner(message), self._prefix_as_owner(message))
        )
        if not bot_text and not owner_text:
            return

        await self._enqueue_max_mutation(
            bridge_name=target.name,
            kind=KIND_MAX_TO_TG_EDIT,
            source_key=max_edit_source_key(
                message.chat_id,
                message.message_id,
                str(message.edited_at or fingerprint(bot_text or owner_text)),
            ),
            payload={
                "link_id": link.id,
                "bridge_name": target.name,
                "max_chat_id": message.chat_id,
                "max_message_id": message.message_id,
                "text": bot_text,
                "entities": (
                    self._edit_entities(message, bot_prefix)
                    + self._mark_entities(message, bot_text)
                ) or None,
                "owner_text": owner_text,
                "owner_entities": (
                    self._edit_entities(message, owner_prefix)
                    + self._mark_entities(message, owner_text)
                ) or None,
            },
        )

    def _edit_entities(
        self, message: IncomingMaxMessage, prefix: str
    ) -> list[dict[str, Any]]:
        """Formatting ranges, moved by whatever went in front of the body.

        The two renderings put different prefixes there — the `Вы: ` marker is
        four UTF-16 units the owner's own copy does not have — so the offsets
        differ and each rendering needs its own.
        """
        entities = elements_to_entities(message.elements)
        if entities:
            entities = shift_entities(entities, utf16_length(prefix))
        return self._forward_entities(prefix, message) + entities

    async def on_max_delete(self, event: MessageDeleted) -> None:
        """A deletion in MAX becomes one durable job per logical message.

        Which Telegram messages that is — the canonical one, every part of an
        album, in the bot's numbering or the owner's — is decided when the job
        runs. This used to delete `link.telegram_message_id` and nothing else, so
        an album lost its head and kept the rest.
        """
        target = self._lookup.bridge_for_max_chat(event.chat_id)
        if target is None:
            return

        for message_id in event.message_ids:
            link = await self._messages.by_max_message(event.chat_id, message_id, target.bot_id)
            if link is None:
                continue
            await self._enqueue_max_mutation(
                bridge_name=target.name,
                kind=KIND_MAX_TO_TG_DELETE,
                source_key=max_delete_source_key(event.chat_id, message_id),
                payload={
                    "link_id": link.id,
                    "bridge_name": target.name,
                    "max_chat_id": event.chat_id,
                    "max_message_id": message_id,
                },
            )

    async def _enqueue_max_mutation(
        self, *, bridge_name: str, kind: str, source_key: str, payload: dict[str, Any]
    ) -> None:
        """Put one MAX→Telegram mutation on the queue and take a first attempt.

        `submit_in_order` rather than `submit`, so the mutation cannot overtake
        the send it is about: the send job is older and in the same direction, so
        the ordering rule that already exists holds the edit or the delete behind
        it without any new dependency machinery.

        No queue means no effect. There is deliberately no direct-call branch
        here — that branch is what this whole change removes, and a router built
        without a pipe is a test affordance rather than a supported route.
        """
        if self._pipe is None:
            logger.debug("no queue wired: %s for %s not carried", kind, source_key)
            return
        job_id, ours = await self._pipe.submit_in_order(
            bridge_name=bridge_name,
            direction=Direction.MAX_TO_TG,
            kind=kind,
            payload=payload,
            source_key=source_key,
        )
        if not ours:
            logger.debug("MAX mutation %s is already in hand", source_key)
            return
        await self._pipe.attempt(
            job_id=job_id,
            bridge_name=bridge_name,
            direction=Direction.MAX_TO_TG,
            kind=kind,
            payload=payload,
        )

    # ------------------------------------------------------------- Telegram -> MAX

    async def _record_intake(
        self,
        target: BridgeTarget,
        bot_id: int,
        telegram_chat_id: int,
        telegram_message_id: int,
        owner_account_id: int | None,
    ) -> int:
        """Write the pre-send mapping for either intake, with owner identity.

        `telegram_chat_id` is the Bot API chat the message lives in, and that is
        the *owner's* chat with the bot — the number a bot passes to
        `setMessageReaction` or `editMessageText`. The MTProto intake works in
        peers and hands over the bot as the peer, which is the same dialog seen
        from the other end and the wrong number to give a bot. Storing it was
        why a contact's reaction on an owner-authored message was drawn nowhere
        even once the row had a bot-side id: the renderer was pointed at a chat
        the bot cannot post in.
        """
        if owner_account_id is not None:
            return await self._messages.record_from_telegram(
                bridge_name=target.name,
                max_chat_id=target.max_chat_id,
                telegram_bot_id=bot_id,
                telegram_chat_id=self._owner_chat_id,
                telegram_message_id=None,
                telegram_owner_message_id=telegram_message_id,
                telegram_owner_account_id=owner_account_id,
            )
        return await self._messages.record_from_telegram(
            bridge_name=target.name,
            max_chat_id=target.max_chat_id,
            telegram_bot_id=bot_id,
            telegram_chat_id=telegram_chat_id,
            telegram_message_id=telegram_message_id,
        )

    async def carry_owner_reaction(
        self,
        *,
        bot_id: int,
        max_chat_id: int,
        max_message_id: int,
        owner_account_id: int,
        owner_message_id: int,
        emoji: str | None,
        pts: int,
    ) -> None:
        """The owner's reaction, on the durable queue rather than straight at MAX.

        Setting a reaction is idempotent and that is exactly why it used to be
        a direct call. Idempotent says a repeat is free; it says nothing about a
        process that dies between the update and the call, and an MTProto update
        has no durable inbox to be replayed from. The job is what remembers the
        reaction was meant.

        Keyed by the update's version, so a replay finds this one, and carrying
        that version in the payload is what lets the worker refuse to apply a
        reaction the owner has already moved past.
        """
        target = self._lookup.bridge_for_bot(bot_id)
        if target is None:
            return
        await self._enqueue_mutation(
            bridge_name=target.name,
            kind=KIND_TG_TO_MAX_REACTION,
            source_key=owner_reaction_source_key(
                owner_account_id, bot_id, owner_message_id, pts
            ),
            payload={
                "max_chat_id": max_chat_id,
                "max_message_id": max_message_id,
                "account_id": owner_account_id,
                "bot_id": bot_id,
                "owner_message_id": owner_message_id,
                "emoji": emoji,
                "pts": pts,
            },
        )

    async def carry_reaction_note(
        self,
        *,
        bot_id: int,
        chat_id: int,
        reply_to: int,
        text: str,
        source_key: str,
    ) -> None:
        """A MAX reaction Telegram cannot draw, said in words — durably.

        A note is a message somebody receives, so it takes the same path every
        created message takes: one job, keyed by the message and the emoji, so a
        replayed MAX event and a second poll over the same window both find the
        job rather than put a second line in the chat. It used to be a bare
        `send_message` from inside the reaction poller, whose exceptions were
        swallowed at `debug` level.

        No `link_id` in the payload on purpose. The note is *about* a message; it
        is not that message, and giving it the mapping row's identity would let
        it take the canonical id a reply resolves through.
        """
        target = self._lookup.bridge_for_bot(bot_id)
        if target is None or self._pipe is None:
            logger.debug("no durable route for a reaction note on bot %s", bot_id)
            return
        payload = {
            "bot_id": bot_id,
            "chat_id": chat_id,
            "text": text,
            "reply_to": reply_to,
        }
        job_id, ours = await self._pipe.submit_in_order(
            bridge_name=target.name,
            direction=Direction.MAX_TO_TG,
            kind=KIND_MAX_TO_TG_TEXT,
            payload=payload,
            source_key=source_key,
        )
        if not ours:
            logger.debug("reaction note %s is already in hand", source_key)
            return
        await self._pipe.attempt(
            job_id=job_id,
            bridge_name=target.name,
            direction=Direction.MAX_TO_TG,
            kind=KIND_MAX_TO_TG_TEXT,
            payload=payload,
        )

    async def carry_emoji_note(
        self,
        *,
        bot_id: int,
        max_chat_id: int,
        reply_to: int,
        emoji: str,
        source_key: str,
    ) -> None:
        """The owner reacted with an emoji MAX has no reaction for, so say it.

        A message, and therefore the same durable path every message takes: a
        claim row so MAX's echo of it is recognised as ours, a job keyed by the
        *update* so a replay finds it rather than sending a second one, and the
        shared creating boundary underneath.

        The claim carries no Telegram message id because there is no Telegram
        message — a reaction is not one. The row exists for the echo, and the
        settlement fills in the MAX id that makes the echo recognisable.
        """
        target = self._lookup.bridge_for_bot(bot_id)
        if target is None:
            return

        link_id = await self._messages.record_from_telegram(
            bridge_name=target.name,
            max_chat_id=max_chat_id,
            telegram_bot_id=bot_id,
            telegram_chat_id=self._owner_chat_id,
            telegram_message_id=None,
        )
        await self._deliver_to_max(
            target=target,
            kind=KIND_TG_TO_MAX_TEXT,
            payload={
                "max_chat_id": max_chat_id,
                "text": emoji,
                "reply_to": reply_to,
                "link_id": link_id,
            },
            source_key=source_key,
            send=lambda: self._max.send_text(max_chat_id, emoji, reply_to=reply_to),
        )

    async def on_telegram_text(
        self,
        *,
        bot_id: int,
        telegram_chat_id: int,
        telegram_message_id: int,
        text: str,
        reply_to_telegram_message_id: int | None = None,
        owner_account_id: int | None = None,
    ) -> None:
        target = self._lookup.bridge_for_bot(bot_id)
        if target is None:
            logger.warning("no bridge for bot %s; dropping the message", bot_id)
            return

        reply_to = await self._max_reply_target(
            bot_id, reply_to_telegram_message_id, owner_account_id
        )

        # Recorded before sending, and marked as ours: when MAX echoes this
        # message back, `is_echo_of_our_own` has something to find. When the intake
        # is the owner's MTProto session, `telegram_message_id` is the owner-side
        # id and there is no bot-side id — the owner columns carry the identity a
        # later edit or delete resolves against, and it is durable now, not after
        # the send.
        link_id = await self._record_intake(
            target, bot_id, telegram_chat_id, telegram_message_id, owner_account_id
        )

        chunks = split_max_text(text)
        source_key = _source_key(bot_id, telegram_message_id, owner_account_id)
        parts: list[tuple[dict[str, Any], str]] = []
        for index, chunk in enumerate(chunks):
            payload = {
                "max_chat_id": target.max_chat_id,
                "text": chunk,
                # MAX should quote the original only once.  Repeating the reply
                # on every bubble renders one logical answer as N answers.
                "reply_to": reply_to if index == 0 else None,
            }
            # The canonical Telegram message resolves to the first MAX bubble.
            # Later bubbles are still independently durable, but must not race
            # to overwrite that one-to-one mapping with a different remote id.
            if index == 0:
                payload["link_id"] = link_id
            parts.append(
                (
                    payload,
                    text_part_source_key(source_key, index=index, total=len(chunks)),
                )
            )

        if self._pipe is not None and len(parts) > 1:
            await self._pipe.enqueue_batch_in_order(
                bridge_name=target.name,
                direction=Direction.TG_TO_MAX,
                items=[(KIND_TG_TO_MAX_TEXT, payload, key) for payload, key in parts],
            )

        try:
            max_message_id: int | None = None
            for payload, part_key in parts:
                async def send_part(part: dict[str, Any] = payload) -> int | None:
                    return await self._max.send_text(
                        target.max_chat_id,
                        str(part["text"]),
                        reply_to=part.get("reply_to"),
                    )

                max_message_id = await self._deliver_to_max(
                    target=target,
                    kind=KIND_TG_TO_MAX_TEXT,
                    payload=payload,
                    source_key=part_key,
                    send=send_part,
                    same_kind_order=True,
                )
                if max_message_id is None:
                    # The batch is already durable.  An older job or another
                    # worker owns the head, so the shared queue will continue it
                    # in order; sending a later part inline would overtake it.
                    break
        except Exception as error:
            await self._state.note_error(target.name, str(error))
            refusal = classify_refusal(error)
            if not refusal.permanent:
                raise
            # A dialog that may not be written into refuses identically for
            # ever. Retrying builds a queue that never drains, and raising here
            # only put a traceback in the journal — the owner watched their
            # message sit there looking sent.
            logger.info("MAX refused a message for %s permanently", target.name)
            await self._say(bot_id, telegram_chat_id, telegram_message_id, refusal.message)
            return

        if max_message_id is None:
            # MAX did not give back an id, so nothing here is confirmed. The job
            # is on the queue; saying "delivered" now would be a guess shown to
            # the owner as a tick.
            return

        await self._state.note_delivery(target.name)
        if self._observer is not None:
            # MAX took the message. That is all `✓` has ever meant here: the
            # protocol has no separate delivery signal (research item delivery-state behaviour).
            await self._observer.note_delivered(
                target.name,
                bot_id=bot_id,
                chat_id=telegram_chat_id,
                at_ms=int(time.time() * 1000),
            )

    async def on_telegram_contact(
        self,
        *,
        bot_id: int,
        telegram_chat_id: int,
        telegram_message_id: int,
        vcard: str,
        contact_user_id: int | None = None,
        reply_to_telegram_message_id: int | None = None,
        owner_account_id: int | None = None,
    ) -> None:
        """A contact the owner shared in Telegram, carried into MAX (contact sharing).

        The whole contact travels as its vCard, which MAX parses into name and
        phone — so a phonebook contact with no MAX account shares fine.
        `contact_user_id` is a MAX user id only, never a Telegram one: a Telegram
        `user_id` means nothing to MAX, so the caller passes it only when it has
        resolved the contact to a real MAX account.

        Everything else is the text path exactly: the mapping is recorded before
        the send so the echo is recognised as ours, and the durable job is what
        survives a refusal or a restart.
        """
        target = self._lookup.bridge_for_bot(bot_id)
        if target is None:
            logger.warning("no bridge for bot %s; dropping the contact", bot_id)
            return

        reply_to = await self._max_reply_target(
            bot_id, reply_to_telegram_message_id, owner_account_id
        )
        link_id = await self._record_intake(
            target, bot_id, telegram_chat_id, telegram_message_id, owner_account_id
        )
        payload = {
            "max_chat_id": target.max_chat_id,
            "vcard": vcard,
            "contact_user_id": contact_user_id,
            "reply_to": reply_to,
            "link_id": link_id,
        }
        source_key = _source_key(bot_id, telegram_message_id, owner_account_id)

        try:
            max_message_id = await self._deliver_to_max(
                target=target,
                kind=KIND_TG_TO_MAX_CONTACT,
                payload=payload,
                source_key=source_key,
                send=lambda: self._max.send_contact(
                    target.max_chat_id,
                    vcard=vcard,
                    contact_user_id=contact_user_id,
                    reply_to=reply_to,
                ),
            )
        except Exception as error:
            await self._state.note_error(target.name, str(error))
            refusal = classify_refusal(error)
            if not refusal.permanent:
                raise
            logger.info("MAX refused a contact for %s permanently", target.name)
            await self._say(bot_id, telegram_chat_id, telegram_message_id, refusal.message)
            return

        if max_message_id is None:
            return

        await self._state.note_delivery(target.name)
        if self._observer is not None:
            await self._observer.note_delivered(
                target.name,
                bot_id=bot_id,
                chat_id=telegram_chat_id,
                at_ms=int(time.time() * 1000),
            )

    async def _deliver_to_max(
        self,
        *,
        target: BridgeTarget,
        kind: str,
        payload: dict[str, Any],
        source_key: str,
        send: Callable[[], Awaitable[int | None]],
        same_kind_order: bool = False,
    ) -> int | None:
        """Carry one thing into MAX with a durable job behind it.

        The mapping row was already written by the caller — it has to be, so the
        echo MAX sends back is recognised as ours rather than forwarded to the
        owner as a message from their contact. That row is exactly what used to
        make a failure invisible, which is why the job now goes down with it.
        """
        if self._pipe is None:
            # No durable queue behind us — the setup CLI and a good deal of the
            # test suite. Still settles through the shared helper rather than
            # inline, so "the mapping learns the MAX id" has one implementation
            # and not one per path; that split is what left worker deliveries
            # without their id in the first place.
            sent = await send()
            if sent is None:
                return None
            return await settle_max_delivery_mapping(self._messages, payload, sent)

        submit = (
            self._pipe.submit_in_kind_order if same_kind_order else self._pipe.submit
        )
        job_id, ours = await submit(
            bridge_name=target.name,
            direction=Direction.TG_TO_MAX,
            kind=kind,
            payload=payload,
            source_key=source_key,
        )
        if not ours:
            # Already carried, or being carried. A second send would duplicate
            # it in the contact's MAX dialog.
            logger.debug("update %s is already in hand", source_key)
            return None
        settled = await self._pipe.attempt(
            job_id=job_id,
            bridge_name=target.name,
            direction=Direction.TG_TO_MAX,
            kind=kind,
            payload=payload,
        )
        if settled.exception is not None:
            # Re-raised so the caller can classify it: a permanent MAX refusal
            # is answered to the owner in words, a timeout is worth retrying.
            # The job is already on the queue either way.
            raise settled.exception
        return settled.remote_message_id

    async def _say(
        self, bot_id: int, chat_id: int, reply_to: int | None, text: str
    ) -> None:
        """Answer the owner in the chat they typed in. Never raises."""
        try:
            await self._telegram.send_text(bot_id, chat_id, text, reply_to=reply_to)
        except Exception:
            logger.debug("could not tell the owner about a refusal", exc_info=True)

    async def _max_reply_target(
        self, bot_id: int, telegram_message_id: int | None, owner_account_id: int | None = None
    ) -> int | None:
        """The MAX message the owner is answering, if the bridge knows it.

        Two id spaces, one lookup each. A reply arriving over the Bot API quotes
        the *bot's* id; one arriving over the owner's MTProto session quotes the
        owner's own, which is a different number for the same message and is why
        replies from that transport used to resolve to nothing at all — including
        replies to what the owner themselves had just sent. Owner-side ids are
        keyed by account, since they are only unique within one.

        Still None when the message is not known: the answer is delivered plain
        rather than dropped, which is the existing fallback and remains the right
        one — a lost thread is better than a lost message.

        An album adds a third way to miss it. Its parts are separate Telegram
        messages and only the head is in the mapping, so a reply to the second
        photo quotes an id `message_map` has never heard of — on either side. The
        aliases are what close that: any part resolves to the one canonical row,
        and the reply lands on the one MAX message the album became.
        """
        if telegram_message_id is None:
            return None
        if owner_account_id is not None:
            owner_link = await self._messages.by_owner_account_message(
                owner_account_id, telegram_message_id
            )
            if owner_link is not None:
                return owner_link.max_message_id
            return await self._max_message_of_part(
                await self._album_part(
                    owner_account_id=owner_account_id, owner_message_id=telegram_message_id
                )
            )
        link = await self._messages.by_telegram_message(bot_id, telegram_message_id)
        if link is not None:
            return link.max_message_id
        return await self._max_message_of_part(
            await self._album_part(bot_id=bot_id, telegram_message_id=telegram_message_id)
        )

    async def _album_part(
        self,
        *,
        bot_id: int | None = None,
        telegram_message_id: int | None = None,
        owner_account_id: int | None = None,
        owner_message_id: int | None = None,
    ) -> Any | None:
        """One album part, by whichever of its two Telegram identities is known."""
        if self._albums is None:
            return None
        if owner_account_id is not None and owner_message_id is not None:
            return await self._albums.by_owner_message(owner_account_id, owner_message_id)
        if bot_id is not None and telegram_message_id is not None:
            return await self._albums.by_bot_message(bot_id, telegram_message_id)
        return None

    async def _max_message_of_part(self, part: Any | None) -> int | None:
        if part is None or part.link_id is None:
            return None
        link = await self._messages.by_id(int(part.link_id))
        return link.max_message_id if link is not None else None

    async def on_telegram_media(
        self,
        *,
        bot_id: int,
        telegram_chat_id: int,
        telegram_message_id: int,
        items: list[tuple[str, Path, str]],
        caption: str = "",
        reply_to_telegram_message_id: int | None = None,
        sources: list[tuple[str, str, str]] | None = None,
        owner_account_id: int | None = None,
        mtproto: list[dict[str, Any]] | None = None,
        album_group_id: str | None = None,
        source_key: str | None = None,
    ) -> int | None:
        """Carry the owner's downloaded files into MAX.

        Recorded before the upload for the same reason text is: MAX echoes back
        whatever we send, and without a claim in the map that echo returns as a
        message from the contact. Returns the mapping row's id, so the caller —
        an album, which needs its parts to point at it — can bind to what was
        written rather than looking it up again and hoping.

        Two retry references, one per intake. `sources` are Bot API file_ids;
        `mtproto` are `{account, peer, owner_message_id, part_index, grouped_id,
        kind, name}` for the owner-session transport, where no file_id exists and
        a retry re-fetches the message over MTProto instead. Only one is ever
        populated for a given job.

        `album_group_id` names a group whose parts are already on disk. It makes
        this call idempotent, which an album needs and a single message does not:
        the parts are accepted long before the group is carried, so a restart in
        between re-runs this — and without the reuse below it would write a
        second mapping row for the same album. `source_key` is the album's, keyed
        by namespace rather than by a message id, for the same reason.
        """
        target = self._lookup.bridge_for_bot(bot_id)
        if target is None:
            logger.warning("no bridge for bot %s; dropping the upload", bot_id)
            return None

        reply_to = await self._max_reply_target(
            bot_id, reply_to_telegram_message_id, owner_account_id
        )
        link_id = await self._album_mapping(
            album_group_id, owner_account_id, telegram_message_id
        )
        if link_id is None:
            link_id = await self._record_intake(
                target, bot_id, telegram_chat_id, telegram_message_id, owner_account_id
            )
        if album_group_id is not None and self._albums is not None:
            # Before the send, and before anything can fail: from here every part
            # of this album resolves to this one row, and the group is finished
            # as far as a restart is concerned.
            await self._albums.bind_link(album_group_id, link_id)

        try:
            max_message_id = await self._deliver_to_max(
                target=target,
                kind=KIND_TG_TO_MAX_MEDIA,
                payload={
                    "max_chat_id": target.max_chat_id,
                    "bot_id": bot_id,
                    "caption": caption,
                    "reply_to": reply_to,
                    "link_id": link_id,
                    "items": [[kind, str(path), name] for kind, path, name in items],
                    "sources": [list(source) for source in (sources or [])],
                    "mtproto": [dict(ref) for ref in (mtproto or [])],
                },
                source_key=source_key
                or _source_key(bot_id, telegram_message_id, owner_account_id),
                send=lambda: self._max.send_media(
                    target.max_chat_id, items, text=caption, reply_to=reply_to
                ),
            )
        except Exception as error:
            await self._state.note_error(target.name, str(error))
            raise

        if max_message_id is None:
            return link_id

        await self._state.note_delivery(target.name)
        if self._observer is not None:
            await self._observer.note_delivered(
                target.name,
                bot_id=bot_id,
                chat_id=telegram_chat_id,
                at_ms=int(time.time() * 1000),
            )
        return link_id

    async def _album_mapping(
        self,
        album_group_id: str | None,
        owner_account_id: int | None,
        head_message_id: int,
    ) -> int | None:
        """The canonical row this album already has, if it has one.

        Two questions in order, because there are two moments a crash can land
        in. The parts themselves answer the first: once they carry a `link_id`
        the group has already become a message, whatever happened next. The
        mapping answers the second — the window between writing that row and
        binding the parts to it, where the parts still look unbound and the row
        already exists. Missing either one writes a second mapping for one album.
        """
        if album_group_id is None or self._albums is None:
            return None
        bound = await self._albums.link_of(album_group_id)
        if bound is not None:
            return bound
        if owner_account_id is None:
            return None
        existing = await self._messages.by_owner_account_message(
            owner_account_id, head_message_id
        )
        return existing.id if existing is not None else None

    async def on_owner_edit(
        self, *, owner_account_id: int, owner_message_id: int, text: str, edit_pts: int
    ) -> None:
        """The owner edited a message in their own client; carry it durably.

        A durable job on the existing queue, keyed by the originating update's
        `pts` and the text it carried, that depends on the original send
        (`send_source_key`). The resolution — coalesce into a still-pending send,
        wait for one in flight, or edit the delivered MAX message — happens when
        the job runs (`resolve_edit`), not here. The bridge is taken from the
        mapping, so nothing here needs the Telegram id.
        """
        target = await self._owner_target(owner_account_id, owner_message_id)
        if target is None:
            logger.debug("owner edit for an unmapped message; ignored")
            return
        body = self._owner_edit_body(target, text)
        if not body:
            # Everything the update carried was the bridge's own decoration.
            # There is no message in it to write into MAX.
            logger.info("owner edit carried no body of its own; nothing sent to MAX")
            return
        await self._enqueue_mutation(
            bridge_name=target.link.bridge_name,
            kind=KIND_TG_TO_MAX_EDIT,
            source_key=edit_source_key(
                owner_account_id, target.key, edit_pts, fingerprint(body)
            ),
            payload={
                "max_chat_id": target.link.max_chat_id,
                "account_id": owner_account_id,
                "owner_message_id": target.canonical_id,
                "link_id": target.link.id,
                "mutation_key": target.key,
                "send_source_key": target.send_key,
                "text": body,
            },
        )

    @staticmethod
    def _owner_edit_body(target: _OwnerTarget, text: str) -> str:
        """The owner's words, with the bridge's own rendering taken back off.

        A Telegram message the bridge produced from a MAX one is a *rendering*:
        a stamp in front of it when the message is older than the chat suggests,
        a forward header above somebody else's words, an `(изм. 12:40)` after a
        body MAX reports as changed. An `UpdateEditMessage` reports the message
        as it is, so an edit of one of these carries all of that — and writing it
        into MAX would put the bridge's own furniture inside the original.

        Only for a message the bridge rendered (`FROM_MAX`). A message the owner
        wrote in Telegram is their own text end to end, and a line of theirs that
        happens to start `[29/07 13:47] ` is theirs to keep.
        """
        if target.link.source_marker is not SourceMarker.FROM_MAX:
            return text
        return strip_presentation(text).strip()

    async def on_owner_delete(
        self, *, owner_account_id: int, owner_message_ids: list[int]
    ) -> None:
        """The owner deleted messages; carry each durably. Delete is terminal.

        `UpdateDeleteMessages` carries no peer, so every id is resolved only
        through the owner-side identity. An id with no mapping is ignored — a
        deletion in some other chat, never guessed onto a bridge. Each id becomes
        its own durable delete job with its own source_key, so a batch is N
        independent, independently-deduped effects.

        An album is the exception that proves it. Telegram will delete one part
        of a group, and MAX holds the whole group as one message with no way to
        remove one attachment from it — so every part resolves to the same
        canonical id, produces the same source_key, and the batch collapses into
        exactly one MAX delete. Deleting the other parts afterwards, or the same
        batch replayed by a catch-up, finds that job and does nothing.
        """
        for owner_message_id in owner_message_ids:
            target = await self._owner_target(owner_account_id, owner_message_id)
            if target is None:
                continue
            await self._enqueue_mutation(
                bridge_name=target.link.bridge_name,
                kind=KIND_TG_TO_MAX_DELETE,
                source_key=delete_source_key(owner_account_id, target.key),
                payload={
                    "max_chat_id": target.link.max_chat_id,
                    "account_id": owner_account_id,
                    "owner_message_id": target.canonical_id,
                    "link_id": target.link.id,
                    "mutation_key": target.key,
                    "send_source_key": target.send_key,
                },
            )
            await self._sweep_album_remains(
                owner_account_id=owner_account_id, target=target
            )

    async def _sweep_album_remains(
        self, *, owner_account_id: int, target: _OwnerTarget
    ) -> None:
        """Take the rest of the album out of Telegram too, once one part is gone.

        MAX has no way to remove one attachment from a message: the group goes as
        a whole or not at all, so deleting one part in Telegram takes the whole
        MAX message with it. What that leaves behind is the ugly half — parts
        still sitting in the Telegram chat as a group whose counterpart no longer
        exists anywhere, and which the owner did not choose to keep. So the
        remaining parts follow.

        Every part of the group is listed, including the one already deleted:
        deleting a message that is already gone is a no-op, and a list that tried
        to be clever about which ones survive would be wrong the moment two
        deletions raced. The sweep's own deletions come back as owner-side delete
        updates and ask for this again; the source key absorbs that, and the loop
        ends there.

        The key comes from the **group**, read off the parts, and not from
        `target.key`. They agree whenever the deleted id resolved through an
        alias, which is the ordinary path — but an id that resolved through the
        canonical row instead answers with its own number, and a sweep filed
        under that would be a second job for the same album. One album, one key,
        whichever part of it the owner happened to delete.
        """
        if self._albums is None:
            return
        parts = await self._albums.parts_of_link(target.link.id)
        if len(parts) < 2:
            # A single message. Nothing was collapsed, so nothing trails behind.
            return
        message_ids = [
            int(part.telegram_owner_message_id)
            for part in parts
            if part.telegram_owner_message_id is not None
        ]
        if not message_ids:
            # No part carries an owner-side id yet — its echo has not arrived, so
            # there is nothing this can name. Left alone rather than guessed at.
            logger.info("album %s has no owner-side ids to sweep", target.key)
            return
        await self._enqueue_mutation(
            bridge_name=target.link.bridge_name,
            kind=KIND_TG_ALBUM_SWEEP,
            source_key=album_sweep_source_key(parts[0].media_group_id),
            payload={
                "account_id": owner_account_id,
                "peer_id": int(parts[0].bot_id),
                "link_id": target.link.id,
                "owner_message_ids": message_ids,
            },
        )

    async def owner_bot_for(
        self, owner_account_id: int, owner_message_id: int
    ) -> int | None:
        """Which contact bot an owner-side id belongs to, aliases included.

        The durable inbox needs it before it can write a delete down, because
        `UpdateDeleteMessages` carries no peer and the inbox key does. Reusing
        the mutation resolver rather than reading `message_map` directly is the
        whole point: one part of an album has no row of its own, its id lives in
        the alias table, and a lookup that missed that dropped the deletion
        silently — which is exactly what it did on the first album smoke.
        """
        target = await self._owner_target(owner_account_id, owner_message_id)
        return int(target.link.telegram_bot_id) if target is not None else None

    async def _owner_target(
        self, owner_account_id: int, owner_message_id: int
    ) -> _OwnerTarget | None:
        """What an owner-side id names: the mapping, its canonical id, its send.

        A message the owner sent alone is its own canonical id and its own send
        job. One part of an album is neither: the mapping was written under the
        head's id and the send was enqueued under the album's namespace, so a
        mutation quoting the third photo has to be carried to both of those or it
        will address a message and a job that do not exist.

        Aliases are consulted first, and the head is looked up through them too.
        Resolving the head directly would find the right mapping and then hand
        back the *wrong* send key — the single-message one, which for an album
        names no job at all — and an edit arriving before the send would silently
        fail to coalesce into it.

        `key` is what the mutation's own source_key is built from, and for an
        album it is the group rather than any of its ids. That is what makes a
        batch of three deletes one delete: every part answers with the same key,
        whether or not the head has an owner-side id yet.
        """
        part = (
            await self._albums.by_owner_message(owner_account_id, owner_message_id)
            if self._albums is not None
            else None
        )
        if part is not None and part.link_id is not None:
            link = await self._messages.by_id(part.link_id)
            if link is not None:
                canonical_id = link.telegram_owner_message_id or owner_message_id
                send_key = (
                    owner_album_source_key(part.media_group_id)
                    if part.direction is Direction.TG_TO_MAX
                    else send_source_key(owner_account_id, canonical_id)
                )
                return _OwnerTarget(
                    link=link,
                    canonical_id=canonical_id,
                    send_key=send_key,
                    key=part.media_group_id,
                )

        link = await self._messages.by_owner_account_message(
            owner_account_id, owner_message_id
        )
        if link is None:
            return None
        return _OwnerTarget(
            link=link,
            canonical_id=owner_message_id,
            send_key=send_source_key(owner_account_id, owner_message_id),
            key=str(owner_message_id),
        )

    async def on_contact_echo(
        self,
        *,
        bot_id: int,
        owner_account_id: int,
        owner_message_id: int,
        fingerprint: str,
    ) -> None:
        """The owner's session saw a message the bridge sent them; bind their id.

        This never creates a delivery — the message is already in the chat, sent
        by the bot. All it does is give the existing mapping row the second half
        of its Telegram identity, so a reply or a deletion made from the owner's
        own client can find the MAX message behind it.
        """
        target = self._lookup.bridge_for_bot(bot_id)
        if target is None:
            return
        await self._enqueue_mutation(
            bridge_name=target.name,
            kind=KIND_OWNER_ECHO_BIND,
            source_key=echo_source_key(owner_account_id, owner_message_id),
            payload={
                "bot_id": bot_id,
                "bridge_name": target.name,
                "account_id": owner_account_id,
                "owner_message_id": owner_message_id,
                "fingerprint": fingerprint,
                "first_seen_ms": int(time.time() * 1000),
            },
        )

    async def on_contact_album_echo(
        self,
        *,
        bot_id: int,
        owner_account_id: int,
        namespace: str,
        parts: list[dict[str, Any]],
    ) -> None:
        """The owner's session saw a whole album the bridge sent them.

        One durable job for the group, on the same queue and of the same kind a
        single message's echo uses — the payload carries the ordered parts, which
        is what makes it survivable once the buffer they came from is cleared.
        Nothing is delivered: the album is already in the chat, and this only
        gives its aliases the second half of their Telegram identity.
        """
        target = self._lookup.bridge_for_bot(bot_id)
        if target is None or not parts:
            return
        await self._enqueue_mutation(
            bridge_name=target.name,
            kind=KIND_OWNER_ECHO_BIND,
            source_key=album_echo_source_key(namespace),
            payload={
                "bot_id": bot_id,
                "bridge_name": target.name,
                "account_id": owner_account_id,
                "namespace": namespace,
                "parts": parts,
                "first_seen_ms": int(time.time() * 1000),
            },
        )

    async def _apply_mutation_directly(self, kind: str, payload: dict[str, Any]) -> None:
        """An edit or a delete with no queue behind it.

        Only reachable where no outbox exists. Both are idempotent, so the loss
        here is the accounting rather than the correctness — and it is written
        out rather than hidden inside an `if` that returns.
        """
        link_id = payload.get("link_id")
        link = await self._messages.by_id(int(link_id)) if link_id is not None else None
        if link is None or link.max_message_id is None:
            return
        if kind == KIND_TG_TO_MAX_EDIT:
            await self._max.edit_text(link.max_chat_id, link.max_message_id, payload["text"])
        elif kind == KIND_TG_TO_MAX_DELETE:
            await self._max.delete_messages(link.max_chat_id, [link.max_message_id])

    async def _enqueue_mutation(
        self, *, bridge_name: str, kind: str, source_key: str, payload: dict[str, Any]
    ) -> None:
        """Put one owner mutation on the durable queue and take a first attempt.

        Dedup is the source_key: a replayed edit or delete finds the existing job
        and does nothing. The first attempt runs inline; if the predecessor send
        is still in flight it defers (stays PENDING for the worker), and a
        transient MAX error leaves it for the worker too — either way the job is
        durable and this returns.
        """
        if self._pipe is None:
            # No queue wired — the setup CLI, tests that build no outbox. Do the
            # thing rather than drop it: an edit or a delete replaces state, so
            # doing it directly is the same effect without the accounting, and
            # skipping it silently would be the only outcome that loses.
            await self._apply_mutation_directly(kind, payload)
            return
        job_id, ours = await self._pipe.submit(
            bridge_name=bridge_name,
            direction=Direction.TG_TO_MAX,
            kind=kind,
            payload=payload,
            source_key=source_key,
        )
        if not ours:
            logger.debug("owner mutation %s already in hand", source_key)
            return
        await self._pipe.attempt(
            job_id=job_id,
            bridge_name=bridge_name,
            direction=Direction.TG_TO_MAX,
            kind=kind,
            payload=payload,
        )
