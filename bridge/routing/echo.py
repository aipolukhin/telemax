"""Not sending back the words we just placed on the owner's behalf.

Writing a MAX message as the owner (see `owner_voice.py`) puts a message *from the
owner* into the chat with a contact bot — and that bot, quite correctly, receives
it as the owner typing something and forwards it to MAX. The owner then sees their
own line twice in the MAX app: once as they wrote it, once as the bridge echoed it
back.

Telegram gives the receiving bot nothing to recognise it by — the recipient is
never told who or what wrote the message, which was measured while the placement
still went over a business connection (compatibility test: `sender_business_bot`,
`business_connection_id` and `via_bot` all unset) and is no more forthcoming now
that the owner's own account sends it. So the bridge has to remember what it
wrote.

What that remembering is *for* has narrowed. While the owner's session is the
authoritative intake, the bot's copy is dropped by the gate whether or not it is
recognised here, so this is no longer what stops a loop — it is what picks up the
bot's own id for a message the owner placed, which is what makes a reply to that
message resolvable.

The match is the exact text, which is far more specific than it looks: what goes
out carries the MAX timestamp stamp, so the string the owner would have to type by
hand to be swallowed by accident includes `[30/07 02:13]`. Entries are consumed on
the first match and expire in a minute, so a suppression that somehow misses its
message cannot silence a later one.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: How long a written-as-the-owner line stays recognisable. Telegram delivers the
#: bot's copy within a second; a minute is generous and bounds the damage if a
#: match never arrives.
ECHO_TTL_SECONDS = 60.0

#: Nothing legitimate needs more than a handful in flight.
MAX_PENDING = 64


#: Media kinds whose Telegram send method and MTProto shape correspond one to
#: one, so the same message describes itself the same way on both sides: a photo
#: is `MessageMediaPhoto`, a voice note is a document with
#: `DocumentAttributeAudio(voice=True)`, a round video one with
#: `DocumentAttributeVideo(round_message=True)`. Anything outside this set is left
#: unbound rather than bound on a guess — a sticker is rebuilt or converted on the
#: way out, and an album is a whole increment of its own.
BINDABLE_ECHO_KINDS = frozenset({"photo", "video", "voice", "video_note", "audio", "document"})


#: MAX's own attachment vocabulary, mapped onto the shared one above. `music`
#: goes out through `sendAudio` and comes back as `DocumentAttributeAudio` without
#: the voice flag; `file` through `sendDocument`. The kinds left out — sticker,
#: contact, call, link, unknown — do not survive as themselves: they are converted,
#: rebuilt or rendered as text, so what arrives is not what the kind claims.
_MAX_ECHO_KINDS: dict[str, str] = {
    "photo": "photo",
    "video": "video",
    "video_note": "video_note",
    "voice": "voice",
    "music": "audio",
    "file": "document",
}


def echo_kind_of(max_attachment_kind: str) -> str | None:
    """The shared kind name for a MAX attachment, or None if it is not bindable."""
    return _MAX_ECHO_KINDS.get(max_attachment_kind)


def text_echo_fingerprint(text: str) -> str:
    """What a text message will look like in the chat, as a versioned hash.

    Exactly the string handed to the sender: MAX→TG text travels as plain text
    plus explicit entities, with no parse mode, so nothing re-escapes it on the
    way and the copy the owner's session receives carries that same string back.
    Hashed because the mapping row must not hold the message itself.
    """
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
    return f"t1:{digest}"


def media_echo_fingerprint(kind: str, *, caption: str) -> str | None:
    """The structural form of a single attachment, as a versioned hash.

    MTProto has no `file_unique_id`, so the file itself cannot be recognised;
    what both sides do agree on is the shape — which kind, how many parts, and
    what caption rides along. Deliberately *not* in it: filename (rewritten on the
    way out), size, duration and dimensions (no live evidence yet that Bot API and
    MTProto report them identically), and any timestamp, which proves nothing on
    its own. Low entropy is expected and safe: the fingerprint confirms the head
    of the queue is this message, it is not what tells two messages apart.

    None for a kind this increment will not bind on a structural match alone.
    """
    if kind not in BINDABLE_ECHO_KINDS:
        return None
    caption_digest = hashlib.sha256(caption.encode("utf-8")).hexdigest()[:32]
    digest = hashlib.sha256(f"{kind}|1|{caption_digest}".encode()).hexdigest()[:32]
    return f"m1:{digest}"


def owner_album_namespace(account_id: int, peer_id: int, grouped_id: int) -> str:
    """The `media_group_id` an owner→MAX album's parts are stored under.

    Telegram's `grouped_id` is unique within a chat, not within an account and
    certainly not across accounts, so it cannot key the parts on its own: two
    contacts' albums could collide, and after a re-authorisation an old group
    could collide with a new one. The account and the peer are what make the
    namespace exact, and they are already known before the first part is stored.
    """
    return f"own:{account_id}:{peer_id}:{grouped_id}"


def expected_album_namespace(link_id: int) -> str:
    """The `media_group_id` a MAX→TG album's expected aliases are stored under.

    `link_id` alone is enough, and deliberately so: it is `message_map.id`, one
    autoincrement sequence for the whole database, so it already names exactly
    one canonical message — and through it one bridge, one contact bot and one
    direction. Nothing else has to be folded in, and folding anything in would
    only invent a second identity for a row that already has one.
    """
    return f"max:{link_id}"


def echo_album_namespace(account_id: int, bot_id: int, grouped_id: int) -> str:
    """Where the parts of an incoming album echo wait until their order is known.

    A third population in the same table, kept apart from the other two because
    it means a third thing: not an album being assembled for MAX, and not the
    aliases of one already sent, but the owner's own view of an album the bridge
    delivered — arriving one part at a time, with the owner's ids for them.

    Keyed by account and contact bot as well as `grouped_id`, for the same reason
    the outgoing namespace is: `grouped_id` is unique within a chat and nothing
    more.
    """
    return f"echo:{account_id}:{bot_id}:{grouped_id}"


def owner_reaction_source_key(
    account_id: int, bot_id: int, owner_message_id: int, pts: int
) -> str:
    """One reaction job per accepted update version.

    The version is the identity: a replayed update finds the job it already
    made, and the owner setting the same emoji again later is a different
    version and a different job. What the reaction *is* stays out of the key —
    two updates carrying the same emoji are still two events, and the payload is
    where the emoji belongs.
    """
    return f"tg-owner-react:{account_id}:{bot_id}:{owner_message_id}:{pts}"


def owner_emoji_note_source_key(
    account_id: int, bot_id: int, owner_message_id: int, pts: int, reaction: str
) -> str:
    """The key an emoji-reply note is filed under when it comes from the session.

    Everything that makes the event unique and nothing that does not: the
    account whose numbering the message id belongs to, the dialog, the message,
    the update's own version, and what the reaction actually was. `pts` is the
    identity half — the owner setting the same unmappable emoji, clearing it and
    setting it again is three real events and three notes, while one update
    replayed by a catch-up keeps the version it always had and finds the note it
    already made.
    """
    return f"tg-owner-note:{account_id}:{bot_id}:{owner_message_id}:{pts}:{reaction}"


def max_reaction_note_source_key(max_chat_id: int, max_message_id: int, emoji: str) -> str:
    """The key a MAX reaction's written-out note is filed under.

    A note is a *message* somebody receives, so it needs the same protection
    every other created message has: a replayed MAX reaction, a second poll over
    the same window and a restart must all find the one job rather than put a
    second line in the chat.

    The emoji is in the key because the contact changing their reaction is a
    different note; the message and chat are what make it theirs. No version:
    MAX reaction events carry none, and the snapshot is what makes a repeat of
    the *same* reaction a no-op long before this key is reached.
    """
    digest = hashlib.sha256(emoji.encode("utf-8")).hexdigest()[:16]
    return f"max-react-note:{max_chat_id}:{max_message_id}:{digest}"


def owner_album_source_key(media_group_id: str) -> str:
    """The send job's key for a whole owner→MAX album. One album, one job.

    Deliberately derived from the namespace rather than from a message id. The
    parts arrive one at a time, so the head is not known until the group closes,
    and after a restart a partial group can close on a different head — a key
    built from it would let the same album be enqueued twice. The namespace is
    fixed the moment the first part is seen and carries the account, the peer and
    the `grouped_id`, which is exactly the identity the key needs.

    It lives in the `tg-owner-msg:` space because that is what it is: the send of
    one owner-side message. `own:` in the third segment is what keeps it from
    ever colliding with the single-message form, whose third segment is a number.
    """
    return f"tg-owner-msg:{media_group_id}"


def album_part_fingerprint(
    kind: str | None,
    *,
    part_index: int,
    caption: str | None,
) -> str:
    """The structural form of one album part, as a versioned hash.

    What both sides of an album can be made to agree on without trusting bytes:
    which kind of media it is, where it sits in the group, whether the group's
    caption rides on *this* part, and what that caption says when it does. The
    caption's position is in here because the live probe found it on part index 1
    of a three-part album — Telegram puts it wherever the sender typed it, and
    code that assumes the first part is code that mismatches whole albums.

    Deliberately not a way to tell two identical photos apart, and never used as
    one. Byte-identical images are byte-identical: the probe confirmed there is
    no content signal to separate them, and what separates them is FIFO order by
    ascending Telegram message id. This confirms a candidate is structurally the
    thing expected; `part_index` is what says *which* thing.

    `kind` is None for a part whose media this bridge does not recognise, which
    is kept in the hash rather than refused — an unknown part still occupies its
    position, and an album with a gap in it must not silently become a match.
    """
    caption_digest = hashlib.sha256((caption or "").encode("utf-8")).hexdigest()[:32]
    present = 1 if caption is not None else 0
    digest = hashlib.sha256(
        f"{kind or '?'}|{part_index}|{present}|{caption_digest}".encode()
    ).hexdigest()[:32]
    return f"a1:{digest}"


def media_key(file_unique_id: str) -> str:
    """The key an attachment is recognised by, rather than its text.

    A placed photo comes back as the owner sending a photo, with no text to match.
    `file_unique_id` is the same for the same file across bots, so the id in the
    send's own reply is the id the copy carries.
    """
    return f"file:{file_unique_id}"


@dataclass(frozen=True, slots=True)
class Placed:
    """The MAX message a placed line came from."""

    max_chat_id: int
    max_message_id: int


@dataclass(frozen=True, slots=True)
class Claim:
    """A message recognised as our own placement.

    `placed` is what makes the copy useful rather than merely harmless: the bot's
    own id for that message arrives only here, and without it a reply to the
    owner's own line has nothing to resolve against.
    """

    placed: Placed | None


class OwnEchoes:
    """What the bridge wrote as the owner and expects to be told about."""

    def __init__(self, ttl: float = ECHO_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._pending: deque[tuple[float, int, str, Placed | None]] = deque(maxlen=MAX_PENDING)

    def note(self, bot_id: int, text: str, *, placed: Placed | None = None) -> None:
        """Written as the owner: the bot is about to hear about it."""
        self._pending.append((time.monotonic(), bot_id, text, placed))

    def claim(self, bot_id: int, text: str) -> Claim | None:
        """Not None when this message is our own, and must not travel back to MAX."""
        now = time.monotonic()
        for index, (at, owner_bot, body, placed) in enumerate(self._pending):
            if now - at > self._ttl:
                continue
            if owner_bot == bot_id and body == text:
                del self._pending[index]
                logger.debug("suppressed the echo of a message written as the owner")
                return Claim(placed=placed)
        self._expire(now)
        return None

    def _expire(self, now: float) -> None:
        while self._pending and now - self._pending[0][0] > self._ttl:
            self._pending.popleft()
