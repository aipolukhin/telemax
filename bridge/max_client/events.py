"""Our own event types, and the translation from PyMax's.

PyMax types stop at this module. Twice now the typed surface has disagreed with
the wire protocol (`AUDIO` attachments, reaction ids), so the rest of the bridge
works with plain dataclasses it owns, and a PyMax upgrade can only break this
one file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)

# MAX reports `duration` in milliseconds everywhere except a FILE's MUSIC
# preview, where it is seconds. Getting this backwards silently mislabels every
# track sent to Telegram.
MS_PER_SECOND = 1000


class AttachmentKind(StrEnum):
    PHOTO = "photo"
    VIDEO = "video"
    VIDEO_NOTE = "video_note"
    VOICE = "voice"
    MUSIC = "music"
    FILE = "file"
    STICKER = "sticker"
    CONTACT = "contact"
    #: A finished or missed call. Not a file at all — nothing to download, and
    #: the whole content of the message is the fact that it happened.
    CALL = "call"
    #: A link with the preview MAX built for it. Also not a file: Telegram builds
    #: its own preview from the URL, so the URL is the whole payload.
    LINK = "link"
    #: MAX's own notice about the dialog rather than a message in it — «Теперь в
    #: MAX! 👉 Напишите что-нибудь!» when a contact joins, and whatever else the
    #: server decides to say. `_type: CONTROL`, no file, and the server has
    #: already rendered the sentence: `message` and `shortMessage` carry it.
    SERVICE = "service"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class MaxAttachment:
    """One attachment, described in terms the media pipeline can act on.

    `raw` keeps the original payload: the media paths need ids and tokens that differ
    per kind, and inventing a field for each of them here would just be a second
    protocol model to keep in sync.
    """

    kind: AttachmentKind
    duration_ms: int | None = None
    width: int | None = None
    height: int | None = None
    file_name: str | None = None
    size: int | None = None
    title: str | None = None
    performer: str | None = None
    #: Only meaningful for `CALL`: nobody picked up. MAX says so by sending no
    #: duration at all, or a zero one.
    missed: bool = False
    #: Only meaningful for `CALL`: `callType == "VIDEO"` rather than `AUDIO`.
    call_video: bool = False
    #: Only meaningful for `SERVICE`: the sentence MAX wrote for this notice.
    notice: str | None = None
    #: Only meaningful for `SERVICE`: the `event` discriminator, kept so an
    #: unfamiliar one is a measurement rather than a silent blank line.
    event: str | None = None
    #: Only meaningful for `LINK`: the address MAX previewed.
    url: str | None = None
    #: The rest are only meaningful for `CONTACT` (contact sharing). A shared contact is not a
    #: file — its whole content is these fields — and how many arrive depends on
    #: what was shared: a MAX user by id comes as name + `contact_user_id` with
    #: no phone (the server does not leak it), while a phonebook or vCard contact
    #: carries `contact_phone` and `contact_vcard`.
    contact_name: str | None = None
    contact_phone: str | None = None
    contact_vcard: str | None = None
    #: The MAX user id, when the shared contact has a MAX account.
    contact_user_id: int | None = None
    #: The shared contact's own avatar, when MAX carries one. Shown as the photo
    #: of the card rather than downloaded into the media pipeline — it is a
    #: profile picture, not an attachment the message is about.
    contact_photo_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MaxForward:
    """Where a message the contact passed on originally came from.

    Not decoration. MAX carries a forward as a `link` of type `FORWARD` whose
    `message` holds the whole original — text, formatting and attachments —
    while the message that actually arrived is an empty envelope. Without
    unwrapping the link there is nothing to deliver at all, which is precisely
    what used to happen: `_render` returned an empty string and the message was
    dropped *after* the dedup claim, so the replay would not bring it back
    either.
    """

    #: The original author, when MAX names them. A forward out of a channel
    #: names the channel instead, and one whose author hid their profile names
    #: nobody — so this is routinely None and the absence is not an error.
    sender_id: int | None = None
    #: The chat the original was written in. Not necessarily one this account
    #: can read: a contact may forward from anywhere.
    source_chat_id: int | None = None
    #: The label MAX itself attaches — a channel or group title, already
    #: rendered. Preferred over a lookup, because it is what the contact saw.
    chat_name: str | None = None
    #: When the original was written, in milliseconds. The envelope is stamped
    #: with the moment it was *forwarded*, which on a live bridge is always
    #: "just now" and therefore says nothing worth showing.
    original_timestamp: int | None = None
    #: Filled in by routing, the only layer that can ask MAX who a user id is.
    #: What the author calls *themselves* — never the owner's address-book
    #: label for them, which is a private note and not this contact's business.
    display_name: str | None = None
    #: `https://max.ru/<handle>`, when the author has one, so the name can be
    #: the link the owner would follow to reach them.
    profile_link: str | None = None


@dataclass(frozen=True, slots=True)
class IncomingMaxMessage:
    message_id: int
    chat_id: int
    sender_id: int | None
    text: str
    timestamp: int
    is_outgoing: bool
    attachments: tuple[MaxAttachment, ...] = ()
    reply_to_message_id: int | None = None
    #: Set when this message is somebody else's, passed on. `text`, `elements`
    #: and `attachments` above are then the *original's* — the envelope carries
    #: none of them — while the ids and the routing stay the envelope's.
    forward: MaxForward | None = None
    #: `updateTime` — when the message was last edited, in milliseconds. MAX
    #: also flags the message with `status: "EDITED"`, but the timestamp is the
    #: useful half: it is what tells the owner *when* the text changed.
    edited_at: int | None = None
    #: MAX formatting ranges, in UTF-16 code units — the same convention as
    #: Telegram entities, so they translate one to one.
    elements: tuple[dict[str, Any], ...] = ()
    #: True when the message was *fetched* rather than pushed: a history import or
    #: the catch-up after downtime. Real-time delivery is never marked, and the two are
    #: not interchangeable — the owner reads a backfill in Telegram long after
    #: writing it in MAX, which is what makes it worth placing as theirs.
    from_history: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MaxContact:
    """Who is on the other end, as far as dressing up their bot goes (profile synchronisation).

    `display_name` is the owner's own label for the contact when there is one:
    MAX keeps several names per user and the address-book entry is the one the
    owner recognises.

    `own_name` is the other one — what the person calls themselves. The two are
    routinely different, and the difference matters exactly once: a forward names its
    author to *somebody else*, and the owner's private label for a third party
    is nobody else's business. Everywhere else `display_name` is the right one
    and stays the default.
    """

    user_id: int
    display_name: str | None = None
    avatar_url: str | None = None
    #: `ONEME` alone — never the address-book entry.
    own_name: str | None = None
    #: `https://max.ru/<handle>`, when the person set a public one. Most have
    #: not: of five users measured, only the official MAX account had one.
    profile_link: str | None = None


@dataclass(frozen=True, slots=True)
class MessageDeleted:
    """MAX removed one or more messages from a chat.

    A single event can cover several ids: deleting a selection in the app sends
    them together.
    """

    chat_id: int
    message_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class TypingSignal:
    chat_id: int
    user_id: int


@dataclass(frozen=True, slots=True)
class ReadMark:
    """Someone marked a chat read.

    `mark` is a watermark, not a message id: everything older counts as read.
    Whether this fires for the *contact's* reading or only for our own other
    devices is research item read-receipt behaviour — until that is answered, `is_own` is how a
    caller tells the two apart.
    """

    chat_id: int
    user_id: int
    mark: int
    is_own: bool
    set_as_unread: bool = False


@dataclass(frozen=True, slots=True)
class PresenceUpdate:
    """When a contact was last active.

    MAX sends `seen` as a Unix timestamp in *seconds* — unlike every other time
    in the protocol, which is milliseconds — and a numeric `status` whose codes
    are not documented anywhere. So "online" is decided from `seen` being recent
    rather than from a code nobody has pinned down.
    """

    user_id: int
    seen: int | None = None
    status: int | None = None


@dataclass(frozen=True, slots=True)
class ReactionUpdate:
    """Reaction counters for one message.

    MAX sends totals, never authors. In a two-person dialog that is enough: our
    own reaction is known, so the rest belongs to the contact.
    """

    chat_id: int
    message_id: int
    counters: dict[str, int]
    total: int


@dataclass(frozen=True, slots=True)
class MessageReactions:
    """What one message carries, as op180 reports it.

    `yours` is the owner's own reaction, straight from the server — the one thing
    the counters cannot tell us.
    """

    counters: dict[str, int]
    yours: str | None


@dataclass(frozen=True, slots=True)
class ChatReaction:
    """A dialog's reaction state, the way `NOTIF_CHAT` reports it.

    In a private dialog the server does not push opcode 155 at all when the
    other side reacts: it pushes the whole
    chat, and the reaction is two of its fields — `lastReactedMessageId` and
    `lastReaction`, the emoji itself. There are no counters and no author.

    `emoji is None` means the chat currently carries no reaction, which is how a
    removal is announced: the two fields simply stop being sent.
    """

    chat_id: int
    message_id: int | None
    emoji: str | None


def contact_id_from(chat: Any, own_user_id: int | None) -> int | None:
    """The other person in a dialog, read from a chat object already in hand.

    A MAX dialog has no title and no "peer" field: the other participant *is* the
    contact, and the participant map is right there on the chat. Asking the server
    for the chat list again to find it — which is what `MaxClient.contact_of_chat`
    does — costs one round trip per dialog, and the dialog picker holds sixty.
    """
    participants = getattr(chat, "participants", None) or {}
    if not isinstance(participants, dict):
        return None
    ids = {
        int(key)
        for key in participants
        if str(key).lstrip("-").isdigit() and int(key) != own_user_id
    }
    return next(iter(sorted(ids)), None)


def enum_text(value: Any) -> str:
    """Text of a protocol value, whether it arrived as a string or an enum.

    PyMax declares its enums as `class X(str, Enum)`, and `str(member)` on those
    returns `"AttachmentType.PHOTO"`, not `"PHOTO"`. A raw event carries plain
    strings, so a bare `str()` works there and quietly fails on anything that
    came back through a PyMax model — which is how every attachment in a history
    backfill turned into `unknown`.
    """
    if value is None:
        return ""
    return str(getattr(value, "value", value)).upper()


def _as_dict(value: Any) -> dict[str, Any]:
    """Best-effort payload for an attachment, whatever PyMax handed us."""
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        result: Any = dump(mode="python", by_alias=True)
        if isinstance(result, dict):
            return result
    return {}


def _attachment_kind(raw: dict[str, Any]) -> AttachmentKind:
    """Classify one attachment the way the live protocol actually behaves.

    The two discriminators that matter, both verified on a real account:

    * `videoType == 1` is a video note; any other value is ordinary video.
    * `AUDIO` in MAX is *only* a voice message. Music arrives as `FILE` with a
      `preview` of type `MUSIC`, so the "voice or music inside AUDIO" heuristic
      that seems obvious from the type name is wrong.
    """
    kind = enum_text(raw.get("_type") or raw.get("type"))

    if kind == "PHOTO":
        return AttachmentKind.PHOTO
    if kind == "VIDEO":
        return (
            AttachmentKind.VIDEO_NOTE
            if int(raw.get("videoType") or raw.get("video_type") or 0) == 1
            else AttachmentKind.VIDEO
        )
    if kind == "AUDIO":
        return AttachmentKind.VOICE
    if kind == "FILE":
        preview = _as_dict(raw.get("preview"))
        preview_type = enum_text(preview.get("_type") or preview.get("type"))
        return AttachmentKind.MUSIC if preview_type == "MUSIC" else AttachmentKind.FILE
    if kind == "STICKER":
        return AttachmentKind.STICKER
    if kind == "CONTACT":
        return AttachmentKind.CONTACT
    if kind == "CALL":
        return AttachmentKind.CALL
    if kind == "CONTROL":
        return AttachmentKind.SERVICE
    if kind == "SHARE" and str(raw.get("url") or "").strip():
        # `SHARE` without a URL is something else wearing the same type — a
        # shared post, most likely. Left unknown rather than rendered as a link
        # to nowhere.
        return AttachmentKind.LINK
    return AttachmentKind.UNKNOWN


#: Compatibility fixtures show that a call arrives as
#: `{_type: CALL, callType, hangupType, duration, conversationId, contactIds}`.
#: `callType` was `AUDIO`; `hangupType` was `CANCELED` for a call the other side
#: gave up on, with `duration: 0`. PyMax models neither of the two discriminators.
CALL_TYPE_VIDEO = "VIDEO"

#: The hangup reasons seen so far. Anything else is logged rather than guessed:
#: the *duration* already decides whether anybody talked, and that is all the
#: wording needs — the reason only ever adds nuance we have not measured.
KNOWN_HANGUP_TYPES = frozenset({"CANCELED", "HANGUP", "REJECTED", "MISSED", "TIMEOUT"})

#: Above this, a call `duration` cannot be seconds: MAX has no six-hour calls,
#: and every other duration in the protocol is milliseconds. Below it the value
#: is read as seconds, which is what a call attachment has been observed to
#: carry. The two readings differ by a factor of a thousand, so guessing wrong
#: is the difference between «3 мин» and «3 часа».
CALL_SECONDS_CEILING = 6 * 60 * 60


def _call_seconds(raw: dict[str, Any]) -> int:
    """How long the call lasted, in seconds. 0 means nobody picked up."""
    value = raw.get("duration")
    if value is None:
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    if number <= 0:
        return 0
    return number if number <= CALL_SECONDS_CEILING else number // MS_PER_SECOND


def normalize_attachment(value: Any) -> MaxAttachment:
    raw = _as_dict(value)
    kind = _attachment_kind(raw)

    if kind is AttachmentKind.MUSIC:
        preview = _as_dict(raw.get("preview"))
        seconds = preview.get("duration")
        return MaxAttachment(
            kind=kind,
            # Seconds here, milliseconds everywhere else — see MS_PER_SECOND.
            duration_ms=int(seconds) * MS_PER_SECOND if seconds is not None else None,
            file_name=raw.get("name"),
            size=raw.get("size"),
            title=preview.get("title"),
            performer=preview.get("artistName") or preview.get("artist_name"),
            raw=raw,
        )

    if kind is AttachmentKind.CALL:
        seconds = _call_seconds(raw)
        hangup = enum_text(raw.get("hangupType") or raw.get("hangup_type"))
        if hangup and hangup not in KNOWN_HANGUP_TYPES:
            # Not an error: the wording is decided by the duration. Logged so the
            # next unfamiliar reason is a measurement rather than a surprise.
            logger.info("MAX call ended with an unfamiliar hangupType=%s", hangup)
        return MaxAttachment(
            kind=kind,
            duration_ms=seconds * MS_PER_SECOND if seconds else None,
            missed=not seconds,
            call_video=enum_text(raw.get("callType") or raw.get("call_type"))
            == CALL_TYPE_VIDEO,
            raw=raw,
        )

    if kind is AttachmentKind.LINK:
        return MaxAttachment(
            kind=kind,
            url=str(raw.get("url") or "").strip() or None,
            title=raw.get("title"),
            raw=raw,
        )

    if kind is AttachmentKind.SERVICE:
        # The server has already written the sentence, in the owner's language,
        # and it is the whole content of the message. `message` is the long form
        # and `shortMessage` the one MAX puts in its chat list; either is better
        # than naming the event, and naming the event is better than a blank.
        return MaxAttachment(
            kind=kind,
            notice=(
                str(raw.get("message") or "").strip()
                or str(raw.get("shortMessage") or raw.get("short_message") or "").strip()
                or None
            ),
            event=enum_text(raw.get("event")).lower() or None,
            raw=raw,
        )

    if kind is AttachmentKind.CONTACT:
        first = str(raw.get("firstName") or raw.get("first_name") or "").strip()
        last = str(raw.get("lastName") or raw.get("last_name") or "").strip()
        name = " ".join(part for part in (first, last) if part) or (
            str(raw.get("name") or "").strip() or None
        )
        phone = str(raw.get("phone") or "").strip() or None
        photo = str(raw.get("photoUrl") or raw.get("photo_url") or "").strip()
        return MaxAttachment(
            kind=kind,
            contact_name=name,
            # MAX sends the phone with a leading '+' on receipt; Telegram accepts
            # it either way, so it is passed through as it arrived.
            contact_phone=phone,
            contact_vcard=str(raw.get("vcfBody") or raw.get("vcf_body") or "").strip() or None,
            contact_user_id=_as_int(raw.get("contactId") or raw.get("contact_id")),
            contact_photo_url=photo if photo.startswith("http") else None,
            raw=raw,
        )

    duration = raw.get("duration")
    return MaxAttachment(
        kind=kind,
        duration_ms=int(duration) if duration is not None else None,
        width=raw.get("width"),
        height=raw.get("height"),
        file_name=raw.get("name"),
        size=raw.get("size"),
        raw=raw,
    )


def _reply_to(raw: dict[str, Any]) -> int | None:
    """Dig the replied-to message id out of the raw payload.

    PyMax's `Message` model drops the `link` field entirely, so a reply looks
    like a plain message through the typed API. link previews needs the id, and the raw
    payload still carries it.
    """
    link = _as_dict(raw.get("link"))
    if enum_text(link.get("type")) != "REPLY":
        return None
    replied = _as_dict(link.get("message"))
    value = replied.get("id") or link.get("messageId") or link.get("message_id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


#: How far to walk a chain of forwards. Passing a forward on again nests the
#: same shape once more, which is ordinary; a chain longer than this is either a
#: loop or a payload built so that reading it never ends.
FORWARD_DEPTH_LIMIT = 4


def _forward_of(raw: dict[str, Any]) -> tuple[MaxForward, dict[str, Any]] | None:
    """Unwrap a forwarded message down to the original inside it.

    The official web client confirms that a
    forward arrives as `link: {type: FORWARD, chatId, chatName, message: {...}}`
    and the envelope's own `text` and `attaches` are empty. The client sends the
    same shape back — `{type: FORWARD, messageId, chatId}` on opcode 64 — so
    this is the protocol's forward, not a rendering of one.

    The walk goes to the *bottom* of a chain rather than stopping at the first
    link, because what the owner wants to know is who wrote the thing, not which
    hand it last passed through.
    """
    forward: MaxForward | None = None
    body = raw

    for _ in range(FORWARD_DEPTH_LIMIT):
        link = _as_dict(body.get("link"))
        if enum_text(link.get("type")) != "FORWARD":
            break
        nested = _as_dict(link.get("message"))
        if not nested:
            # A FORWARD link with nothing under it. There is no original to
            # unwrap, so the envelope is left exactly as it arrived rather than
            # replaced by an empty one.
            break
        # `chatId` is read by presence, not by truthiness: zero is the owner's
        # saved-messages chat (§3), a real id that `or` would swallow — and
        # forwarding out of saved messages is one of the commonest forwards
        # there is.
        source = link.get("chatId")
        if source is None:
            source = link.get("chat_id")
        forward = MaxForward(
            sender_id=_as_int(nested.get("sender")),
            source_chat_id=_as_int(source),
            chat_name=str(link.get("chatName") or link.get("chat_name") or "").strip() or None,
            original_timestamp=_as_int(nested.get("time")),
        )
        body = nested

    if forward is None:
        return None
    return forward, body


def _field(message: Any, attribute: str, key: str, raw: dict[str, Any]) -> Any:
    """Read a field from either a PyMax model or a raw payload.

    Both shapes reach this module: typed events come from PyMax, while replayed
    and buffered messages are plain dicts out of the database.
    """
    if isinstance(message, dict):
        return raw.get(key)
    value = getattr(message, attribute, None)
    return value if value is not None else raw.get(key)


def _as_int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


#: MAX name kinds, best first: the owner's address-book label, then whatever
#: the person calls themselves.
_NAME_PRIORITY = ("CUSTOM", "ONEME")

#: What the person calls themselves, and nothing else. Used where a name leaves
#: the owner's own screen — see `MaxContact.own_name`.
_OWN_NAME_KIND = "ONEME"


def normalize_presence(event: Any) -> PresenceUpdate:
    """PyMax hands presence over as an event with a nested model."""
    raw = _as_dict(event)
    presence = _as_dict(raw.get("presence") or getattr(event, "presence", None))
    return PresenceUpdate(
        user_id=_as_int(raw.get("user_id") or raw.get("userId"), 0) or 0,
        seen=_as_int(presence.get("seen")),
        status=_as_int(presence.get("status")),
    )


def reaction_from(payload: Any) -> ReactionUpdate:
    """Read opcode 155 straight off the wire.

    PyMax cannot: its `ReactionUpdateEvent` types `messageId` as a string and
    the server sends a number, so `model_validate` raises inside its dispatcher
    and the whole frame is lost. The payload itself is plain JSON, so reading it
    here costs nothing and cannot be broken by the library's next release.

    The shape is `{chatId, messageId, reactionInfo: {counters, totalCount}}`,
    with the counters occasionally at the top level instead — both are accepted,
    because guessing wrong means a reaction that silently never appears.
    """
    raw = _as_dict(payload)
    info = _as_dict(raw.get("reactionInfo") or raw.get("reaction_info")) or raw

    counters: dict[str, int] = {}
    for entry in info.get("counters") or []:
        item = _as_dict(entry)
        emoji = item.get("reaction") or item.get("id") or item.get("emoji")
        if emoji:
            counters[str(emoji)] = _as_int(item.get("count"), 0) or 0

    return ReactionUpdate(
        chat_id=_as_int(raw.get("chatId") or raw.get("chat_id"), 0) or 0,
        message_id=_as_int(raw.get("messageId") or raw.get("message_id"), 0) or 0,
        counters=counters,
        total=_as_int(
            info.get("totalCount") or info.get("total_count"), sum(counters.values())
        )
        or 0,
    )


def chat_reaction_from(payload: Any) -> ChatReaction:
    """Read a dialog's reaction fields out of opcode 135.

    The payload is `{chat: {...}}`; a reaction shows up as `lastReactedMessageId`
    plus `lastReaction`. Both are absent on a chat update that has nothing to do
    with reactions *and* on a removal, so "absent" is reported as-is and what it
    means is decided against the last known state, not here.
    """
    raw = _as_dict(payload)
    chat = _as_dict(raw.get("chat")) or raw

    emoji = chat.get("lastReaction") or chat.get("last_reaction")
    message_id = _as_int(
        chat.get("lastReactedMessageId") or chat.get("last_reacted_message_id"), None
    )

    return ChatReaction(
        chat_id=_as_int(chat.get("id") or chat.get("chatId"), 0) or 0,
        message_id=message_id,
        emoji=str(emoji) if emoji else None,
    )


def reactions_by_message(payload: Any) -> dict[int, MessageReactions]:
    """Read an op180 answer: what each message carries, and which one is ours.

    The answer is `{messagesReactions: {"<message id>": {counters, yourReaction,
    totalCount}}}` — keyed by id, ids as strings, and an empty object for a
    message with no reactions.

    `yourReaction` is the useful part: MAX counts reactions without attributing
    them, so this is the only authoritative statement of which one is the
    owner's, and therefore of which one is not.
    """
    raw = _as_dict(payload)
    entries = raw.get("messagesReactions") or raw.get("messages_reactions") or {}
    if not isinstance(entries, dict):
        return {}

    result: dict[int, MessageReactions] = {}
    for key, value in entries.items():
        message_id = _as_int(key, None)
        if message_id is None:
            continue
        info = _as_dict(value)
        counters: dict[str, int] = {}
        for counter in info.get("counters") or []:
            fields = _as_dict(counter)
            emoji = fields.get("reaction") or fields.get("id") or fields.get("emoji")
            if emoji:
                counters[str(emoji)] = _as_int(fields.get("count"), 0) or 0
        yours = info.get("yourReaction") or info.get("your_reaction")
        result[message_id] = MessageReactions(
            counters=counters, yours=str(yours) if yours else None
        )
    return result


def normalize_contact(user: Any, *, user_id: int) -> MaxContact:
    """Pull a name and an avatar out of whatever PyMax returned."""
    raw = _as_dict(user)
    names = raw.get("names") or []

    def full(entry: Any) -> str | None:
        item = _as_dict(entry)
        keys = ("first_name", "firstName", "last_name", "lastName")
        parts = [str(item.get(key) or "").strip() for key in keys]
        joined = " ".join(part for part in parts if part)
        return joined or (str(item.get("name") or "").strip() or None)

    chosen: str | None = None
    for kind in _NAME_PRIORITY:
        for entry in names:
            item = _as_dict(entry)
            if enum_text(item.get("type")) == kind:
                chosen = full(entry)
                if chosen:
                    break
        if chosen:
            break
    if not chosen:
        for entry in names:
            chosen = full(entry)
            if chosen:
                break

    own_name: str | None = None
    for entry in names:
        item = _as_dict(entry)
        if enum_text(item.get("type")) == _OWN_NAME_KIND:
            own_name = full(entry)
            if own_name:
                break

    avatar = next(
        (
            raw[key]
            for key in ("base_url", "baseUrl", "base_raw_url", "baseRawUrl")
            if isinstance(raw.get(key), str)
        ),
        None,
    )
    # A full URL already, not a handle: `https://max.ru/maxbot`. Absent for
    # anyone who never set one, which is most people.
    link = raw.get("link")
    return MaxContact(
        user_id=user_id,
        display_name=chosen,
        avatar_url=str(avatar) if isinstance(avatar, str) and avatar.startswith("http") else None,
        own_name=own_name,
        profile_link=str(link) if isinstance(link, str) and link.startswith("http") else None,
    )


def _edited_at(message: Any, raw: dict[str, Any]) -> int | None:
    """When the message was last edited, if it ever was.

    MAX marks an edited message with `status: "EDITED"` and carries the moment
    in `updateTime`. PyMax keeps `updateTime` out of its model, so it is read
    from the raw payload — and only trusted when the status agrees, because the
    field also appears on messages that were never touched.
    """
    status = enum_text(_field(message, "status", "status", raw))
    if "EDIT" not in status:
        return None
    return _as_int(raw.get("updateTime") or raw.get("update_time"))


def _log_unmodelled(
    attachments: tuple[MaxAttachment, ...], *, chat_id: int, message_id: int, at: int
) -> None:
    """Say where an attachment we cannot render came from.

    Logged here rather than in the classifier because this is the layer that
    knows *which message* it was. Without that the line said only that MAX sends
    a type we do not model, which is half a diagnostic: the owner sees
    «[вложение: не удалось скачать]» and there is no way to find it again.

    Field names and ids only. A payload carries CDN tokens, and the text of the
    message is not needed to go and look at it.
    """
    for item in attachments:
        if item.kind is not AttachmentKind.UNKNOWN:
            continue
        logger.info(
            "MAX attachment we do not model: type=%s fields=%s chat=%s message=%s at=%s",
            enum_text(item.raw.get("_type") or item.raw.get("type")) or "?",
            sorted(item.raw),
            chat_id,
            message_id,
            at,
        )


def normalize_message(
    message: Any, *, own_user_id: int | None, chat_id: int | None = None
) -> IncomingMaxMessage:
    """Turn a PyMax message (or a raw payload) into our own type."""
    raw = _as_dict(message) or {}

    # A forward splits the message in two: the envelope decides where it goes
    # and what it is called, the original decides what is in it. PyMax's typed
    # model drops `link` entirely, so the original is only ever reachable as a
    # plain payload — which is why `body` stops being the PyMax object here.
    unwrapped = _forward_of(raw)
    forward: MaxForward | None = None
    body: Any = message
    body_raw = raw
    if unwrapped is not None:
        forward, body_raw = unwrapped
        body = body_raw

    sender = _as_int(_field(message, "sender", "sender", raw))
    resolved_chat = _as_int(_field(message, "chat_id", "chatId", raw), chat_id or 0)
    attaches = _field(body, "attaches", "attaches", body_raw) or []
    attachments = tuple(normalize_attachment(item) for item in attaches)
    message_id = _as_int(_field(message, "id", "id", raw), 0) or 0
    timestamp = _as_int(_field(message, "time", "time", raw), 0) or 0
    _log_unmodelled(attachments, chat_id=resolved_chat or 0, message_id=message_id, at=timestamp)

    return IncomingMaxMessage(
        message_id=message_id,
        chat_id=resolved_chat or 0,
        sender_id=sender,
        text=str(_field(body, "text", "text", body_raw) or ""),
        timestamp=timestamp,
        # Messages the owner sent from the official MAX app also arrive as
        # events; owner-side events delivers them with a marker instead of dropping them.
        is_outgoing=own_user_id is not None and sender is not None and sender == own_user_id,
        attachments=attachments,
        reply_to_message_id=_reply_to(raw),
        edited_at=_edited_at(message, raw),
        elements=tuple(
            _as_dict(item) for item in (_field(body, "elements", "elements", body_raw) or [])
        ),
        forward=forward,
        raw=raw,
    )
