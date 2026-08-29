"""Row types. Plain dataclasses, not pydantic: nothing here is user input."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Direction(StrEnum):
    MAX_TO_TG = "max_to_tg"
    TG_TO_MAX = "tg_to_max"


class SourceMarker(StrEnum):
    """Which side authored a message.

    This is the loop guard: an echo of our own delivery comes back from MAX as a
    normal event, and re-sending it would ping-pong forever.
    """

    FROM_MAX = "from_max"
    FROM_TG = "from_tg"


class OutboxState(StrEnum):
    """Where a delivery job is. See docs/architecture/delivery-semantics.md.

    `INFLIGHT` predates the lease and is kept as the wire value so an existing
    database keeps working; it means LEASED — a worker holds this job until
    `lease_expires_at`.
    """

    PENDING = "pending"
    INFLIGHT = "inflight"
    DONE = "done"
    FAILED = "failed"
    #: The send went out; the confirmation did not come back. Never retried
    #: automatically — a duplicate lands in a real person's chat (ADR 0002).
    AMBIGUOUS = "ambiguous"
    #: TTL passed before it could be delivered.
    EXPIRED = "expired"
    #: A failure the owner has read and set aside. The row keeps its error, its
    #: attempts and its timestamps — this is evidence, not a delete — but it
    #: stops counting as something that still needs doing. Without it a job that
    #: can never be recovered holds an incident open for ever and masks the next
    #: real failure behind it.
    ARCHIVED = "archived"


class InboxState(StrEnum):
    """Where a durably-stored Telegram update is.

    `RECEIVED` is the state that makes moving the polling offset safe: the
    update is on disk, so losing the process no longer loses the message.
    """

    RECEIVED = "received"
    LEASED = "leased"
    DONE = "done"
    FAILED = "failed"


class BridgeState(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    #: The row exists and the bot is known, but nothing is polling it yet. Written
    #: *before* the transport is started so a crash between the two leaves a
    #: durable record rather than a bot that was live until the next restart and
    #: then vanished. `active()` does not return it, which is the point: a bridge
    #: is only served once it has proved it can be.
    PROVISIONING = "provisioning"


class PendingContactState(StrEnum):
    NEW = "new"
    ASKED = "asked"
    IGNORED = "ignored"
    BLOCKED = "blocked"
    PROVISIONED = "provisioned"


@dataclass(frozen=True, slots=True)
class MessageLink:
    id: int
    bridge_name: str
    max_chat_id: int
    max_message_id: int | None
    telegram_bot_id: int
    telegram_chat_id: int
    telegram_message_id: int | None
    direction: Direction
    source_marker: SourceMarker
    created_at: int
    #: Canonical hash of what a MAX→TG message put in the Telegram chat, written
    #: before it was sent, so an owner-side echo can be matched to it by equality.
    #: None on every TG→MAX row, and on MAX→TG rows this increment cannot bind.
    echo_fingerprint: str | None = None
    #: The owner's own ids for this message. A message can be known by two
    #: Telegram identities at once — the bot's, and the owner account's — and
    #: only the second one appears in what the owner's client sends back.
    telegram_owner_message_id: int | None = None
    telegram_owner_account_id: int | None = None


@dataclass(frozen=True, slots=True)
class PlacedMessage:
    """One Telegram message the bridge put in the chat, and how to remove it.

    Two ids because a message can exist twice over: the bot's own copy of what a
    contact said, and the copy placed *as the owner* through the business
    connection for what the owner said. A wipe has to reach both.
    """

    link_id: int
    telegram_chat_id: int
    telegram_message_id: int | None
    telegram_owner_message_id: int | None

    @property
    def ids(self) -> tuple[int, ...]:
        return tuple(
            value
            for value in (self.telegram_message_id, self.telegram_owner_message_id)
            if value is not None
        )


@dataclass(frozen=True, slots=True)
class OutboxItem:
    id: int
    bridge_name: str
    direction: Direction
    kind: str
    payload_json: str
    attempts: int
    next_attempt_at: int
    state: OutboxState
    last_error: str | None
    created_at: int
    updated_at: int
    #: Identifies the source event, so a replay cannot enqueue a second job.
    source_key: str | None = None
    lease_expires_at: int | None = None
    expires_at: int | None = None
    #: The id the remote API gave back. Set only on a confirmed delivery.
    remote_message_id: int | None = None
    ambiguous_at: int | None = None
    #: Stamped just before the remote call, so lease recovery can tell "never
    #: sent" from "sent, outcome unknown".
    send_started_at: int | None = None


@dataclass(frozen=True, slots=True)
class InboxUpdate:
    """One Telegram update, on disk before its offset was acknowledged."""

    id: int
    bot_id: int
    update_id: int
    bridge_name: str | None
    payload_json: str
    state: InboxState
    attempts: int
    lease_expires_at: int | None
    last_error: str | None
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class MediaGroupPart:
    """One attachment of a Telegram album, durable on its own.

    An album has no "last part" marker, so completeness is only ever guessed by
    a pause. Storing each part means the guess can be made again after a restart
    instead of the parts vanishing with the process.

    From V15 a part is also an **alias**: one Telegram message standing for one
    row of `message_map`. Telegram lets an album be replied to, edited or deleted
    one part at a time; MAX carries the whole group as a single message with N
    attachments and has no per-attachment mutation. So N aliases point at one
    canonical row through `link_id`, and every part-level event collapses onto it.
    Aliases do not replace the mapping and never form a second one.

    Everything below `created_at` is NULL on a legacy row — the Bot API album
    collector's parts, which are assembled and cleared and never bind anything.
    """

    id: int
    media_group_id: str
    bridge_name: str
    bot_id: int
    #: The bot's own id for this part. NULL until it exists: a MAX→TG alias is
    #: written before the album is sent, and an owner→MAX part never has one.
    telegram_message_id: int | None
    payload_json: str
    created_at: int
    #: The one `message_map` row every part of this group resolves to.
    link_id: int | None = None
    direction: Direction | None = None
    #: Position in the canonical order — ascending Telegram message id, which
    #: live verification showed is the order both sides always agree on.
    part_index: int | None = None
    media_kind: str | None = None
    #: Whether this part carries the group's caption. Telegram puts it on
    #: whichever part the sender typed it on, so its position is structure.
    caption_present: bool | None = None
    part_fingerprint: str | None = None
    telegram_owner_account_id: int | None = None
    telegram_owner_message_id: int | None = None
    updated_at: int | None = None


@dataclass(frozen=True, slots=True)
class BridgeRecord:
    bridge_name: str
    max_chat_id: int
    token_env: str
    max_user_id: int | None = None
    telegram_bot_id: int | None = None
    title: str | None = None
    source: str = "yaml"
    state: BridgeState = BridgeState.ACTIVE
    #: The username this bridge's bot must always have, derived from the MAX
    #: user id. Stored so a rebuild can be recognised without re-deriving it.
    expected_username: str | None = None
    #: Nothing at or below this MAX message id is ever carried by the automatic
    #: backfill. Written when the bot behind a chat changes — the dedup is keyed
    #: on the bot, so a new bot means an unclaimed tail and a second delivery of
    #: everything in it.
    history_floor: int | None = None


@dataclass(frozen=True, slots=True)
class ReadMarks:
    bridge_name: str
    contact_read_mark: int
    own_read_mark: int
    last_ticked_message_id: int | None
    #: The bot's own status-line message, when that tick style is on.
    status_message_id: int | None = None
    #: What that line currently says, so an unchanged edit is never sent.
    status_text: str | None = None
    #: When MAX last accepted one of our sends — the single tick.
    delivered_at: int = 0


@dataclass(frozen=True, slots=True)
class ReactionSnapshot:
    max_chat_id: int
    max_message_id: int
    counters: dict[str, int]
    your_reaction: str | None


@dataclass(frozen=True, slots=True)
class PendingContact:
    max_chat_id: int
    max_user_id: int | None
    display_name: str | None
    state: PendingContactState
    first_seen_at: int
    asked_at: int | None
    buffered: int = 0


@dataclass(frozen=True, slots=True)
class OwnerMessageState:
    """The last confirmed reading of one of the owner's own Telegram messages.

    `pts = 0` means the row was seeded from a fetch of the message rather than
    from an update — a picture of the present, not a position in the update
    stream, so every real update is newer than it by construction.
    """

    telegram_owner_account_id: int
    telegram_bot_id: int
    telegram_owner_message_id: int
    content_fingerprint: str
    chosen_json: str
    pts: int
    updated_at: int


class InboxFamily(StrEnum):
    """What kind of owner update a row stands for.

    Two, and no `new_message`: every new-message form already writes its mapping
    row and its outbox job before any remote work, so a third family would
    duplicate a guarantee rather than add one.
    """

    #: One `UpdateEditMessage`: the message as Telegram described it. One row,
    #: not two, because a snapshot with two derived diffs read as two events
    #: could have one accounted while the other was not.
    SNAPSHOT = "message_snapshot"
    #: One target of an `UpdateDeleteMessages`. A batch is N rows sharing a pts,
    #: which the primary key allows because the message id is in it.
    DELETE = "delete"


class OwnerUpdateState(StrEnum):
    OPEN = "open"
    CLAIMED = "claimed"
    ACCOUNTED = "accounted"
    #: Never deleted by age. A row here is a question for the owner.
    DEAD = "dead"


@dataclass(frozen=True, slots=True)
class InboxKey:
    """What identifies one owner update. Exactly the primary key."""

    account_id: int
    bot_id: int
    message_id: int
    family: InboxFamily
    pts: int


@dataclass(frozen=True, slots=True)
class OwnerUpdate:
    """One durable owner update, as the processor reads it."""

    key: InboxKey
    state: OwnerUpdateState
    attempts: int
    content_text: str | None
    content_fingerprint: str | None
    chosen_json: str | None
    created_at: int
    last_error: str | None = None
