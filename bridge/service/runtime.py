"""Composition: the one place that knows about every other package.

Startup order matters and is deliberate:

1. storage, so a failure to migrate stops everything before a bot is visible;
2. MAX, because a bridge with no MAX session can only accept messages it cannot
   deliver;
3. Telegram bots, one at a time, so a bad token names its own bridge;
4. handlers last, when both sides can actually carry a message.

Shutdown runs in reverse, and every step is best-effort: a failure to close one
bot must not leave the database open.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from datetime import tzinfo
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self, cast

from aiogram import Dispatcher

from bridge.config import (
    AppConfig,
    BridgeSource,
    LoadedConfig,
    ProvisioningMode,
    ReactionsConfig,
    ReactionStyle,
    ResolvedBridge,
)
from bridge.cutover.floors import apply_floor
from bridge.max_client import (
    ChatReaction,
    IncomingMaxMessage,
    MaxClient,
    MaxUnconfirmedSendError,
    NativeMediaState,
    PresenceMode,
    PresenceUpdate,
    ReactionUpdate,
    ReadMark,
    TypingSignal,
)
from bridge.media import HttpFetcher, MaxMediaSources, MediaPipeline, TempFiles
from bridge.media.delivery import DeliveryReceipt, MaxMediaDelivery
from bridge.observability.redaction import redact
from bridge.onboarding.state import StateStore
from bridge.presence import (
    AutoRead,
    PinnedStatus,
    ReadReceipts,
    StatusLine,
    TelegramPresenceAdapter,
)
from bridge.presence.typing import TelegramTypingMirror
from bridge.provisioning import (
    BotApiOwnedBots,
    BotProfileSync,
    BridgeReplayer,
    BridgeSummary,
    DialogFlow,
    DialogPicker,
    Guardian,
    GuardianContext,
    Provisioner,
    ProvisioningJournal,
    RepositoryKnownBots,
    TelegramProfileAdapter,
    announce_markup,
    announce_text,
    build_guardian_router,
    signature_of,
)
from bridge.provisioning.batch import BridgeConflictError
from bridge.provisioning.business import use_state_dir as use_business_state_dir
from bridge.provisioning.coordinator import ProvisioningCoordinator
from bridge.provisioning.history import ChatImport
from bridge.provisioning.managed import ManagedBotProvisioner
from bridge.provisioning.mtproto import OwnedBot
from bridge.provisioning.provisioner import MtprotoProvisioner
from bridge.provisioning.secrets import ContactBotSecretStore
from bridge.provisioning.service import (
    bridge_name_for_username,
    token_env_for,
)
from bridge.reactions import (
    DialogActivity,
    MaxReactionAdapter,
    ReactionSync,
    TelegramReactionAdapter,
)
from bridge.retry import OutboxWorker, PermanentDeliveryError, WorkerPool
from bridge.routing.adapters import (
    GuardianBridgeLink,
    MaxDisplayNames,
    MaxTextSender,
    RegistryLookup,
    RegistryMutations,
    RegistrySender,
    build_forwarding_router,
)
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
    DeferDelivery,
    DeliveryPipe,
    SendingHook,
    UnconfirmedDeliveryError,
    attachment_from_payload,
)
from bridge.routing.echo import OwnEchoes, Placed
from bridge.routing.max_mutation import resolve_max_delete, resolve_max_edit
from bridge.routing.media_adapter import RegistryMediaSender
from bridge.routing.owner_binding import OwnerBotSideBinding
from bridge.routing.owner_echo import (
    resolve_album_echo as resolve_owner_album_echo,
)
from bridge.routing.owner_echo import (
    resolve_echo as resolve_owner_echo,
)
from bridge.routing.owner_mutation import (
    resolve_delete as resolve_owner_delete,
)
from bridge.routing.owner_mutation import (
    resolve_edit as resolve_owner_edit,
)
from bridge.routing.owner_mutation import (
    resolve_reaction as resolve_owner_reaction,
)
from bridge.routing.owner_updates import OwnerUpdateDispatch
from bridge.routing.owner_voice import MtprotoOwnerSender, OwnerVoice
from bridge.routing.presence_router import build_presence_router
from bridge.routing.router import BridgeRouter
from bridge.routing.settlement import settle_max_delivery_mapping
from bridge.routing.upload_router import MediaUploader, build_upload_router
from bridge.storage import (
    AlbumSettlementError,
    AlbumSettlementRepository,
    AlertRepository,
    BridgeRecord,
    BridgeRepository,
    BridgeState,
    BridgeStateRepository,
    Database,
    Direction,
    ForwardAuthorRepository,
    HealthStateRepository,
    MediaGroupRepository,
    MessageMapRepository,
    OutboxRepository,
    OutboxState,
    OwnerMessageStateRepository,
    OwnerUpdateInboxRepository,
    PendingContactRepository,
    ReactionStateRepository,
    ReadStateRepository,
    StickerCacheRepository,
    StickerOriginRepository,
    TelegramInboxRepository,
)
from bridge.telegram import BotRegistry, BridgeRegistryError, build_dispatcher
from bridge.telegram.app import StatusProvider
from bridge.telegram.owner_snapshot import (
    EMPTY_CHOSEN,
    content_fingerprint_of_placement,
)
from bridge.telegram.quiet import quietly
from bridge.telegram.registry import ExpectedBot
from bridge.telegram.user_session import mtproto_entities

from .health import (
    ALERT_COOLDOWN_MS,
    BRIDGE_NOT_STARTED,
    HEALTH_INTERVAL_SECONDS,
    PROVISIONING_STUCK,
    AlertDispatcher,
    HealthService,
    HealthSnapshot,
    OwnerIngressState,
    OwnerSessionFacts,
)
from .notifications import NotificationCentre
from .supervisor import Supervisor

logger = logging.getLogger(__name__)


#: How often orphaned temp files are swept while the process runs.
SWEEP_INTERVAL_SECONDS = 3600

#: How long an album sweep waits when the owner's session is away. Long enough
#: that a session down for an hour costs a couple of hundred cheap re-checks
#: rather than twelve spent attempts and a permanent FAILED; short enough that
#: the album is swept within a minute of the session returning.
SWEEP_WAIT_MS = 30_000

#: How often expired leases are returned to the queue and overdue jobs retired.
#: Well under the lease itself, so a message whose worker died waits seconds
#: rather than minutes.
OUTBOX_SWEEP_SECONDS = 30

#: How often the MAX session is checked for signs of life.
MAX_WATCHDOG_INTERVAL_SECONDS = 60

#: How Telegram says a message is not there to delete. Distinct from «can\'t be
#: deleted», which means it *is* there and will not be removed — the difference
#: decides whether the mapping goes with it.
ALREADY_GONE_MARKERS = (
    "message to delete not found",
    "message not found",
    "message_id_invalid",
    "message identifier is invalid",
)


@dataclass(frozen=True, slots=True)
class ServiceHealth:
    """The three facts the home screen's glyph is chosen from.

    Facts rather than a rendered line: «🟡 требуется действие» is a decision
    about presentation, and the worker is the wrong place to be making it.
    """

    max_connected: bool
    degraded: bool
    bridges: int


#: How `/status` names the owner's puppet session. Six lines because the six
#: states want six different things from the owner — and because "мосты
#: приостановлены" has to be sayable without saying why four times over.
_OWNER_INGRESS_LINE = {
    OwnerIngressState.READY: "подключён",
    OwnerIngressState.CONNECTING: "подключается",
    # No backticks: the guardian sends HTML, so markdown renders literally —
    # the owner was being shown a command wrapped in two stray characters.
    OwnerIngressState.AUTHORIZATION_REQUIRED: "требуется авторизация: telemax telegram-sync",
    OwnerIngressState.DISCONNECTED: "соединение потеряно, восстанавливаем",
    OwnerIngressState.MISSING_CONFIGURATION: "не настроен — нет API-ключей или файла сессии",
    OwnerIngressState.STOPPED: "остановлен",
}


#: How `/status` names each native-media state. Six of them, and the reason they
#: are six rather than "on"/"off" is that "I turned it off", "MAX refused the
#: attach", "MAX gave us an uploader we cannot use" and "there is no decoder on
#: this machine" want four different things done about them.
_NATIVE_STATE_WORDS = {
    "healthy": "как есть",
    "idle": "как есть (пока не было)",
    "degraded": "файлом (были отказы)",
    "disabled": "выключено вами",
    "breaker-open": "MAX отклонил — уходит файлом",
    "uploader-drift": "не тот загрузчик — уходит файлом",
    "dependency-unavailable": "нет декодера — уходит файлом",
}


def _native_media_lines(snapshot: HealthSnapshot) -> list[str]:
    """The native voice/circle block of `/status`, or nothing at all.

    Silent while both kinds are healthy and nothing has gone out yet — `/status`
    is read on a phone, and a line that always says "fine" trains people to skip
    the block that one day will not.
    """
    names = {"voice": "Голосовые", "circle": "Кружки"}
    lines: list[str] = []
    for status in snapshot.native_media:
        state = snapshot.native_media_state(status)
        if state == "idle" and not status.attempts:
            continue
        word = _NATIVE_STATE_WORDS.get(state, state)
        line = f"{names.get(status.kind, status.kind):<10} {word}"
        if status.attempts:
            line += (
                f" · {status.successes}/{status.attempts}"
                f", файлом {status.ordinary_fallbacks}"
            )
        if status.unconfirmed:
            line += f", неясно {status.unconfirmed}"
        lines.append(line)
        if status.breaker_open and status.breaker_reason:
            lines.append(f"           причина: {status.breaker_reason}")
    return lines


async def _creating_send(call: Callable[[], Awaitable[int]]) -> int:
    """One message-creating MAX call, with "it may already be there" kept apart.

    `MaxUnconfirmedSendError` is the MAX client saying it wrote the frame and got
    nothing usable back — no answer, or an answer naming no message. The message
    may be in the chat, so trying again is how a person receives it twice, and
    the owner decides instead (ADR 0002).

    Everything short of the creating frame — an upload slot, a POST, a file that
    would not read — raises something else and stays an ordinary retry, because
    at that point the message does not exist yet.
    """
    try:
        return await call()
    except MaxUnconfirmedSendError as error:
        raise UnconfirmedDeliveryError(str(error)) from error


async def _carry_media_into_max(
    max_sender: Any,
    payload: dict[str, Any],
    items: list[tuple[str, Path, str]],
) -> int:
    """`send_media` through the creating boundary."""
    return await _creating_send(
        lambda: max_sender.send_media(
            payload["max_chat_id"],
            items,
            text=payload.get("caption", ""),
            reply_to=payload.get("reply_to"),
        )
    )


def _album_parts(receipt: DeliveryReceipt) -> list[tuple[int, int]]:
    """The receipt as the settlement reads it: position, then Telegram's id."""
    return [(part.part_index, part.telegram_message_id) for part in receipt.album]


async def settle_telegram_delivery_mapping(
    messages: MessageMapRepository | None,
    payload: dict[str, Any],
    *,
    telegram_message_id: int,
    receipt: DeliveryReceipt | None = None,
    albums: AlbumSettlementRepository | None = None,
) -> int:
    """Give a delivered MAX→TG message its bot-side id. The only place it happens.

    Called from inside the sender, by the inline attempt and by the retry worker
    alike, at the one moment when both things it needs are true: Telegram has
    answered, so there is an id to write, and the job has not been marked done
    yet, so the payload still carries `link_id` — `mark_done` clears the payload
    in the same statement that records the delivery, and after that there is
    nothing left to say *which* mapping this was.

    It used to be the router's job, on the inline path only. Anything the worker
    delivered after a failed first attempt therefore stayed in the map with no
    Telegram id at all: the message was in the chat, and a reply to it resolved
    to nothing for ever. That is the regression this function exists to hold shut,
    so it is a named function rather than a closure — something a test can call.

    Idempotent, and deliberately not a blind write: `attach_telegram_message`
    only fills the column while it is empty. A repeat with the same id is a
    no-op, a crash between the send and this line is recovered by the next
    attempt writing what the first one did not, and an id that disagrees with one
    already proved is left alone rather than silently replacing a mapping the
    unique index would then reject anyway.

    An album settles its parts here too, and only here. The canonical row gets
    the head id exactly as before; each expected alias gets the id of the
    Telegram message it actually became, matched by position — which is the only
    thing that can match them, since two parts of an album may be identical and
    the probe found no content signal that separates them.

    Returns the id it was given, so a caller can `return await settle(...)`.
    """
    link_id = payload.get("link_id")
    if link_id is None or messages is None:
        # Not every delivery has a mapping row: the guardian's own messages and
        # the notices sent beside a message have nothing to attach to.
        return telegram_message_id
    if receipt is not None and receipt.album and albums is not None:
        # An album's whole identity in one transaction — the head *and* every
        # alias, or nothing. The head used to be written first, before the
        # receipt had been checked, which left a canonical message pointing at a
        # delivery whose parts were unbound.
        try:
            await albums.settle_bot_album(link_id=int(link_id), parts=_album_parts(receipt))
        except AlbumSettlementError as error:
            raise UnconfirmedDeliveryError(str(error)) from error
        return telegram_message_id
    await messages.attach_telegram_message(int(link_id), telegram_message_id)
    return telegram_message_id


async def sweep_album_in_telegram(
    payload: dict[str, Any],
    *,
    session: Any,
    albums: MediaGroupRepository | None,
) -> int | None:
    """Take the remains of one album out of the owner's Telegram chat.

    The one job in the system whose effect is *deleting somebody's messages*,
    so what it is allowed to touch is re-derived here rather than trusted.
    The payload was written by the router from the album's own aliases, and
    that is still where it comes from — but the job has been on disk since
    then, and "the producer was correct when it wrote this" is not a property
    the executor can check. So the aliases are read again and the deletion is
    the **intersection**: an id in the payload that the album does not claim
    is not deleted, whatever put it there.

    Three things are proved against the database before anything is asked of
    Telegram, and each of them is a way an id could name a message in a chat
    this job has no business touching:

    * the parts belong to the `link_id` the job names;
    * they belong to the owner account the job names — an owner-side id means
      nothing outside the account that issued it;
    * the peer is a contact bot of a live bridge.

    **Remote boundary, decided and pinned (option A).** `sending()` is not
    called and this kind can never be AMBIGUOUS. Deleting a message that is
    already gone is a no-op in Telegram, so a repeat after a timeout is
    exactly as correct as the first attempt — and asking the owner to
    adjudicate "did the delete land?" would be asking them to decide
    something that does not matter. Every other kind stamps the boundary
    because a repeat there means a second message in somebody's chat; here it
    means nothing at all. `test_the_album_sweep_never_announces_a_remote_
    boundary` is what keeps this a contract rather than an omission.

    A session that is away defers instead of failing. It used to raise, which
    `classify()` reads as retryable — correct as far as it goes, but each
    attempt spent one of twelve, so a session down for twenty minutes turned
    an owed deletion into a permanent FAILED and left the album half-gone for
    ever. Deferring costs no attempt.
    """
    if session is None or not session.is_connected:
        raise DeferDelivery(SWEEP_WAIT_MS)

    link_id = payload.get("link_id")
    account_id = int(payload.get("account_id", 0))
    peer_id = int(payload["peer_id"])
    asked = {int(value) for value in payload.get("owner_message_ids", [])}
    if not asked or link_id is None or albums is None:
        return None

    parts = await albums.parts_of_link(int(link_id))
    allowed = {
        int(part.telegram_owner_message_id)
        for part in parts
        if part.telegram_owner_message_id is not None
        and part.telegram_owner_account_id == account_id
    }
    peers = {int(part.bot_id) for part in parts}

    if peer_id not in peers:
        raise PermanentDeliveryError(
            f"album sweep for link {link_id} names peer {peer_id}, which is not"
            f" the contact bot its parts were delivered to"
        )
    if session.is_connected and peer_id not in await session.allowed_peers():
        raise PermanentDeliveryError(
            f"album sweep for link {link_id} names peer {peer_id}, which is not"
            " a live bridge's contact bot"
        )

    message_ids = sorted(asked & allowed)
    if not message_ids:
        # Nothing the album still claims. Not an error to retry and not a
        # deletion to guess at: the aliases have moved on, and the owner sees
        # the job rather than a message disappearing from some other chat.
        raise PermanentDeliveryError(
            f"album sweep for link {link_id} names no id this album claims"
            f" ({len(asked)} asked, {len(allowed)} claimed)"
        )
    if len(message_ids) != len(asked):
        logger.warning(
            "album sweep for link %s: %s of %s ids are no longer claimed by the"
            " album and will not be deleted",
            link_id,
            len(asked) - len(message_ids),
            len(asked),
        )

    await session.delete_own_messages(peer_id, message_ids)
    logger.info(
        "swept %s remaining album part(s) out of Telegram for link %s",
        len(message_ids),
        link_id,
    )
    return None


class OwnerBaseline(Protocol):
    """Writes down what an owner-side message *is*, before anything is derived.

    Satisfied by `OwnerUpdateDispatch`, which owns the per-message lock the
    seed has to be taken under.
    """

    async def seed_baseline(
        self,
        *,
        account_id: int,
        bot_id: int,
        message_id: int,
        content_fingerprint: str,
        chosen_json: str,
    ) -> bool: ...


async def _seed_placed_baseline(
    baseline: Callable[[], OwnerBaseline | None] | None,
    payload: dict[str, Any],
    *,
    owner_message_id: int,
    text: str,
) -> None:
    """Record what the bridge just placed as the owner. Never fails the delivery.

    **The message is already in the chat by the time this runs.** A baseline that
    could not be written is a message whose first update has nothing to subtract
    from — recoverable, and handled honestly by the dispatch — while an exception
    here would turn a delivered message into a retry or a question. So this
    swallows, counts on the log, and returns.

    Only the plain-text branch calls it, and deliberately. A caption goes through
    the media pipeline, which may truncate it (`split_caption`), replace it with a
    placeholder notice, or split an album across parts — so the text this function
    would hash is not reliably the text Telegram stored, and a *wrong* baseline is
    worse than none: it makes the next ordinary update look like a content change
    and asks MAX to apply an edit nobody made. Absent, the dispatch declines to
    guess; wrong, it guesses confidently.
    """
    resolve = baseline() if baseline is not None else None
    if resolve is None:
        return
    account_id = payload.get("owner_account_id")
    bot_id = payload.get("bot_id")
    if account_id is None or bot_id is None:
        return
    try:
        await resolve.seed_baseline(
            account_id=int(account_id),
            bot_id=int(bot_id),
            message_id=int(owner_message_id),
            content_fingerprint=content_fingerprint_of_placement(
                text, mtproto_entities(payload.get("entities"))
            ),
            chosen_json=EMPTY_CHOSEN,
        )
    except Exception:
        logger.warning(
            "could not write the baseline for a message placed as the owner;"
            " its first update will be read as unknown",
            exc_info=True,
        )


async def place_owner_message(
    payload: dict[str, Any],
    *,
    sending: SendingHook,
    voice: OwnerVoice,
    media: MaxMediaDelivery,
    messages: MessageMapRepository | None,
    albums: AlbumSettlementRepository | None = None,
    echoes: OwnEchoes | None = None,
    baseline: Callable[[], OwnerBaseline | None] | None = None,
) -> int:
    """Place one queued message as the owner, over the owner's own session.

    The whole of what `max_to_tg_owner` does, as a function rather than a branch
    so a test can drive the real thing instead of a copy of it. Text and
    attachments differ only in which transport call they make; everything either
    side of that — the remote boundary, the settlement, the refusal to guess — is
    shared, and shared on purpose.

    **No Bot API appears here at any point.** By the time this runs the transport
    has been chosen and a job exists; a failure is a retry or a question for the
    owner, and rerouting through the contact bot is how the same line ends up in
    the chat twice, once as the owner and once signed `Вы: …`. That happened, on
    a timeout that arrived after Telegram had already accepted the message.

    `sending()` fires immediately before the first request and not one step
    earlier: preparing an attachment — resolving it, downloading it, drawing its
    cover — is retryable work, and a crash during it must not ask the owner to
    adjudicate a message that never left the machine.

    `baseline` is what closes the hole this placement used to leave. The session
    that places a message gets **no update for it** (`send_own_message`), so the
    handler that normally writes a message's baseline never sees one placed here
    — and the first update that does arrive, which may be nothing more than a
    read tick, lands on a message with nothing to compare against. Persisting a
    baseline prevents that update from becoming a phantom edit containing the
    bridge's own presentation stamp.
    """
    peer_id = int(payload["peer_id"])
    if payload.get("attachments"):
        restored = IncomingMaxMessage(
            message_id=payload["max_message_id"],
            chat_id=payload["max_chat_id"],
            sender_id=None,
            text=payload.get("text", ""),
            timestamp=payload.get("timestamp", 0),
            is_outgoing=True,
            attachments=tuple(
                attachment_from_payload(item) for item in payload.get("attachments", [])
            ),
            reply_to_message_id=payload.get("reply_to_message_id"),
        )
        # The hook travels inside the delivery, so every attachment is resolved
        # and downloaded before anything announces itself — and the receipt names
        # the owner-side id of each part, which appears nowhere else.
        receipt = await media.deliver_receipt(
            restored,
            bot_id=payload["bot_id"],
            chat_id=peer_id,
            caption=payload.get("caption", ""),
            caption_entities=payload.get("caption_entities"),
            reply_to=None,
            on_sending=sending,
        )
        if receipt.head is None:
            raise UnconfirmedDeliveryError(
                "the owner's session returned no message id for the attachments"
            )
        return await settle_owner_delivery_mapping(
            messages, payload, owner_message_id=receipt.head, receipt=receipt, albums=albums
        )

    await sending()
    text = str(payload["outgoing_text"])
    placed = await voice.send_as_owner(peer_id, text, entities=payload.get("entities"))
    if placed is None:
        # The request went out and answered with nothing to identify it by. Not
        # retried: it may well be in the chat.
        raise UnconfirmedDeliveryError("the owner's session returned no message id")
    if echoes is not None:
        # The contact bot is about to receive this as the owner typing it, and
        # its copy is the only place the *bot's* id for the message appears.
        # Noted the moment the send returns and before the mapping is written:
        # the copy can arrive while this is still settling, and an echo that
        # finds nothing waiting for it is ignored in silence.
        echoes.note(
            payload["bot_id"],
            text,
            placed=Placed(
                max_chat_id=int(payload["max_chat_id"]),
                max_message_id=int(payload["max_message_id"]),
            ),
        )
    # Before the mapping and before the return: the first update on this message
    # can arrive while the settlement is still running, and a baseline written
    # afterwards would be too late to be the thing it is subtracted from.
    await _seed_placed_baseline(baseline, payload, owner_message_id=placed, text=text)
    return await settle_owner_delivery_mapping(messages, payload, owner_message_id=placed)


async def settle_owner_delivery_mapping(
    messages: MessageMapRepository | None,
    payload: dict[str, Any],
    *,
    owner_message_id: int,
    receipt: DeliveryReceipt | None = None,
    albums: AlbumSettlementRepository | None = None,
) -> int:
    """Give a message placed *as the owner* its owner-side id. The only place.

    The twin of `settle_telegram_delivery_mapping`, and separate from it for one
    reason that matters: the id a placement answers with is in the owner
    account's numbering, not the bot's. Writing it into `telegram_message_id`
    would put a number from one sequence in a column every other reader treats
    as belonging to the other — which is precisely the confusion that broke the
    read watermark.

    The account is stored with the id, because `by_owner_account_message` is
    keyed by both: an owner-side id means nothing outside the account that
    issued it, and an owner-side edit or delete resolves through exactly that
    pair. Under the business connection it was left null and those mutations
    resolved to nothing at all.

    Same timing as its twin, for the same reason: called from inside the sender,
    by the inline attempt and the worker alike, while the payload still carries
    `link_id` — `mark_done` clears it in the statement that records the delivery.
    Idempotent throughout, so a crash between the placement and `mark_done` is
    finished by the next attempt rather than duplicated by it.
    """
    link_id = payload.get("link_id")
    if link_id is None or messages is None:
        return owner_message_id
    account_id = payload.get("owner_account_id")
    if receipt is not None and receipt.album and albums is not None:
        # The same all-or-nothing settlement as the bot side, in the other id
        # space. The account travels with the id because an owner-side message id
        # means nothing outside the account that issued it.
        try:
            await albums.settle_owner_album(
                link_id=int(link_id),
                account_id=int(account_id) if account_id is not None else 0,
                parts=_album_parts(receipt),
            )
        except AlbumSettlementError as error:
            raise UnconfirmedDeliveryError(str(error)) from error
        return owner_message_id
    await messages.attach_owner_message(
        int(link_id),
        owner_message_id,
        telegram_owner_account_id=int(account_id) if account_id is not None else None,
    )
    return owner_message_id


def _commit_of() -> str | None:
    """The commit this process is running, when the tree is a git checkout.

    Best effort and never fatal: knowing which build produced a symptom is worth
    a subprocess call at startup, and worth nothing at all if it costs a crash.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            cwd=Path(__file__).resolve().parent.parent.parent,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:  # noqa: BLE001 - knowing the build is never worth a crash
        return None
    value = result.stdout.strip()
    return value or None


def _sweeper(media: MediaPipeline) -> Callable[[], Awaitable[None]]:
    async def loop() -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            media.sweep_orphans()

    return loop


def _on_a_timer(
    period: float, action: Callable[[], Awaitable[None]], *, label: str
) -> Callable[[], Awaitable[None]]:
    """Run something every `period` seconds, for ever, without dying of it.

    A supervised loop that raises is restarted with backoff, which is right for
    a task that cannot work at all and wrong for one that failed once — a single
    unreachable history call must not put the whole catch-up into backoff. So the
    failure is logged and the next tick happens on time.

    Waits first. Whatever this repeats has just been done by the caller.
    """

    async def loop() -> None:
        while True:
            await asyncio.sleep(period)
            try:
                await action()
            except Exception:
                logger.exception("%s failed; the next pass runs as usual", label)

    return loop


def _max_watchdog(
    client: MaxClient, health: HealthService | None = None
) -> Callable[[], Awaitable[None]]:
    """Bring the MAX session back if it stops being usable, and write down when.

    `Client.start()` is its own reconnect loop, so this only fires when that
    loop itself is gone — a bug, or a cancelled await somewhere in PyMax. Losing
    MAX silently is the worst failure this bridge has: Telegram keeps accepting
    messages that go nowhere.

    The health calls are what turn "MAX is down" into something the owner is
    *told*. Without them `max_offline_ms` stays empty for ever and the outage
    alert can never fire — which is exactly what happened until a live check
    went looking for the number and found it never populated.
    """

    async def loop() -> None:
        while True:
            await asyncio.sleep(MAX_WATCHDOG_INTERVAL_SECONDS)
            if client.is_ready:
                if health is not None:
                    with contextlib.suppress(Exception):
                        await health.note_max_connected()
                continue
            logger.warning("MAX session is not ready; restarting it")
            if health is not None:
                with contextlib.suppress(Exception):
                    await health.note_max_disconnected("session was not ready")
            with contextlib.suppress(Exception):
                await client.stop()
            await client.start()
            if client.is_ready and health is not None:
                with contextlib.suppress(Exception):
                    await health.note_max_connected()

    return loop


def _timezone_of(app: AppConfig) -> tzinfo | None:
    """The owner's timezone, or the host's when none is configured.

    A bot is never told what timezone its reader is in, and a server is usually
    UTC, so an unset value is the one case where the stamps quietly disagree
    with what Telegram itself shows.
    """
    name = app.telegram.timezone
    if not name:
        return None
    from zoneinfo import ZoneInfo

    return ZoneInfo(name)


def _chain(
    *handlers: Callable[[IncomingMaxMessage], Awaitable[None]],
) -> Callable[[IncomingMaxMessage], Awaitable[None]]:
    """Run several message handlers in order, as one handler."""

    async def run(message: IncomingMaxMessage) -> None:
        for handler in handlers:
            await handler(message)

    return run


def _touch_dialog(
    activity: DialogActivity,
) -> Callable[[IncomingMaxMessage], Awaitable[None]]:
    """Put a dialog on the fast reaction cadence because a message arrived.

    Chat updates were the obvious signal and turned out to be the wrong one: they
    are not sent reliably enough to notice a dialog waking up (measured on the
    stand — a whole run of reactions produced none). Messages, typing and read
    marks do arrive, and a reaction almost always follows one of them.
    """

    async def run(message: IncomingMaxMessage) -> None:
        activity.touch(message.chat_id)

    return run


def _note_incoming(
    lookup: RegistryLookup, auto_read: AutoRead
) -> Callable[[IncomingMaxMessage], Awaitable[None]]:
    """Remember what a reply from the owner would be acknowledging."""

    async def run(message: IncomingMaxMessage) -> None:
        target = lookup.bridge_for_max_chat(message.chat_id)
        if target is None or message.is_outgoing:
            return
        auto_read.note_incoming(
            target.name, max_chat_id=message.chat_id, max_message_id=message.message_id
        )
        await auto_read.on_delivered_to_telegram(target.name)

    return run


def _typing_handler(
    lookup: RegistryLookup,
    mirror: TelegramTypingMirror,
    owner_chat_id: int,
    activity: DialogActivity,
) -> Callable[[TypingSignal], Awaitable[None]]:
    async def run(signal: TypingSignal) -> None:
        target = lookup.bridge_for_max_chat(signal.chat_id)
        if target is None:
            return
        activity.touch(signal.chat_id)
        await mirror.on_contact_typing(target.name, target.bot_id, owner_chat_id)

    return run


def _read_handler(
    lookup: RegistryLookup, receipts: ReadReceipts, activity: DialogActivity
) -> Callable[[ReadMark], Awaitable[None]]:
    async def run(mark: ReadMark) -> None:
        # Our own read marks come from the owner's other devices; a tick for
        # those would say the contact read something they have not seen.
        if mark.is_own:
            logger.debug("read mark from our own device in chat %s, ignored", mark.chat_id)
            return
        target = lookup.bridge_for_max_chat(mark.chat_id)
        if target is None:
            logger.debug("read mark for unbridged chat %s", mark.chat_id)
            return
        activity.touch(mark.chat_id)
        drawn = await receipts.on_contact_read(target.name, mark=mark.mark)
        # Worth a line either way: "the tick never appears" is otherwise
        # indistinguishable from "the mark never arrived".
        logger.info("contact read up to %s in %s; tick drawn: %s", mark.mark, target.name, drawn)

    return run


def _presence_handler(
    contacts: dict[int, tuple[str, int]], pinned: PinnedStatus, owner_chat_id: int
) -> Callable[[PresenceUpdate], Awaitable[None]]:
    """Presence arrives per *user*, so the bridge is found by contact id."""

    async def run(event: PresenceUpdate) -> None:
        target = contacts.get(event.user_id)
        if target is None:
            # The owner has other contacts in MAX; only bridged ones get a line.
            return
        bridge_name, bot_id = target
        await pinned.update(bridge_name, event, bot_id=bot_id, chat_id=owner_chat_id)

    return run


def _reaction_handler(
    lookup: RegistryLookup, reactions: ReactionSync
) -> Callable[[ReactionUpdate], Awaitable[None]]:
    async def run(event: ReactionUpdate) -> None:
        target = lookup.bridge_for_max_chat(event.chat_id)
        if target is None:
            return
        await reactions.on_max_reaction(event, telegram_bot_id=target.bot_id)

    return run


def _reaction_poller(
    registry: BotRegistry,
    reactions: ReactionSync,
    activity: DialogActivity,
    config: ReactionsConfig,
    state: BridgeStateRepository | None = None,
) -> Callable[[], Awaitable[None]]:
    """Ask MAX about each dialog's reactions on a timer.

    MAX pushes a chat update only when the chat's own "last reaction" pointer
    moves, so a reaction on an older message — and every removal — is never
    announced. Asking is cheap: one op180 per dialog covers a window of messages.

    Two cadences, because one number cannot be both: a dialog that has just seen
    something is asked about every few seconds, everything else waits. The tick
    is the fast interval, and each dialog is polled when its own is due.
    """
    tick = min(config.poll_active_seconds, config.poll_seconds)

    async def loop() -> None:
        last: dict[int, float] = {}
        while True:
            await asyncio.sleep(tick)
            now = time.monotonic()
            for live in registry.live:
                chat_id = live.max_chat_id
                due = (
                    config.poll_active_seconds if activity.is_warm(chat_id) else config.poll_seconds
                )
                # A tick of slack, or a period that is not a multiple of the tick
                # would always wait one tick too long.
                if now - last.get(chat_id, 0.0) < due - tick / 2:
                    continue
                last[chat_id] = now
                try:
                    if await reactions.poll(chat_id, telegram_bot_id=live.identity.bot_id):
                        # A reaction found in a sleeping dialog means it is awake:
                        # the next one should not wait out the idle interval too.
                        activity.touch(chat_id)
                except Exception as error:  # noqa: BLE001 - one dialog must not stop the rest
                    # This is the retry mechanism for reactions in this
                    # direction, so a poll that keeps failing is a bridge whose
                    # reactions have quietly stopped moving. It used to say so at
                    # `debug` level and nowhere else. Now it is on the bridge's
                    # own record, which `/status` reads — one line per failure,
                    # no incident, because a poll failing once is not an outage
                    # and one incident per tick would be the alert storm.
                    logger.warning(
                        "reaction poll failed for %s: %s", live.name, type(error).__name__
                    )
                    if state is not None:
                        with contextlib.suppress(Exception):
                            await state.note_error(
                                live.name, f"reaction poll: {type(error).__name__}"
                            )

    return loop


def _chat_reaction_handler(
    lookup: RegistryLookup, reactions: ReactionSync, activity: DialogActivity
) -> Callable[[ChatReaction], Awaitable[None]]:
    """Reactions in a private dialog arrive as a chat update, not as opcode 155."""

    async def run(event: ChatReaction) -> None:
        target = lookup.bridge_for_max_chat(event.chat_id)
        if target is None:
            return
        # Any chat update means the dialog is live, which is what puts it on the
        # fast polling cadence — including the updates that say nothing about
        # reactions, since a reaction usually follows a message.
        activity.touch(event.chat_id)
        await reactions.on_chat_reaction(event, telegram_bot_id=target.bot_id)

    return run


def _expected_bot(record: BridgeRecord | None) -> ExpectedBot | None:
    """What the database says a bridge's bot is, if it says anything.

    None for a bridge nothing has recorded yet — a first start, or a YAML seed
    that has never been through provisioning. Everything it *does* know is
    checked; the id it does not know is learned from `getMe` and written down.
    """
    if record is None:
        return None
    expected = ExpectedBot(
        bot_id=record.telegram_bot_id, username=record.expected_username
    )
    return expected if expected.known else None


class LiveBridgeGateway:
    """`BridgeGateway` over the running worker: registry, secrets, database.

    This is the only place provisioning touches the live process, and it is
    deliberately small. Everything it does is idempotent — stopping a bridge
    that is not up, saving a token that is already saved, starting a worker for
    a bridge that is already running — because the batch above it may be
    resuming after a crash and cannot know which of those has happened.
    """

    def __init__(
        self,
        *,
        registry: BotRegistry,
        bridges: BridgeRepository,
        secrets: ContactBotSecretStore,
        contact_of_chat: Callable[[int], Awaitable[int | None]],
        dress_bot: Callable[[str, int, int], Awaitable[None]] | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self._registry = registry
        self._bridges = bridges
        # The service's own store, not a path: the lock inside it is what makes
        # two contacts provisioning at once safe, and a lock nobody shares is
        # not a lock.
        self._secrets = secrets
        self._contact_of_chat = contact_of_chat
        self._dress_bot = dress_bot
        # Where a cutover left its history floors, if there was one.
        self._data_dir = data_dir or Path("data")

    async def stop_bridge(self, max_chat_id: int) -> None:
        live = self._registry.by_max_chat(max_chat_id)
        if live is None:
            return
        # Stops the polling task and closes the session: the old token must not
        # keep long-polling after the bot it belongs to has been deleted.
        await self._registry.remove(live.name)

    async def save_token(self, *, max_chat_id: int, username: str, token: str) -> str:
        return await self._secrets.save(token_env_for(username), token)

    async def forget_token(self, token_env: str | None) -> str | None:
        """Drop one bot's token from the store. Returns the variable it removed.

        Called last in a teardown, after @BotFather has confirmed the bot is
        gone. A token whose bot no longer exists is not a secret worth keeping,
        and leaving it behind is how the next install of the same bridge finds a
        key that opens nothing.
        """
        if not token_env:
            return None
        await self._secrets.unset(token_env)
        return token_env

    async def preflight(self, *, max_chat_id: int, username: str) -> None:
        """Refuse a binding the register cannot hold, before anything is created.

        Two shapes, and both used to surface as an `IntegrityError` on
        `UNIQUE(max_chat_id)` — *after* a bot had been created and its token
        written down, which left a resource nothing could use and nothing would
        clean up.
        """
        wanted = bridge_name_for_username(username)
        existing = await self._bridges.by_max_chat(max_chat_id)
        if existing is not None and existing.expected_username:
            if existing.expected_username.lower() != username.lower():
                raise BridgeConflictError(
                    f"мост «{existing.bridge_name}» уже держит этот диалог MAX"
                    f" под @{existing.expected_username}"
                )
        named = await self._bridges.get(wanted)
        if named is not None and named.max_chat_id != max_chat_id:
            raise BridgeConflictError(
                f"@{username} уже обслуживает диалог MAX {named.max_chat_id}"
            )

    async def start_worker(
        self,
        *,
        max_chat_id: int,
        username: str,
        token_env: str,
        title: str,
        max_user_id: int | None = None,
    ) -> str:
        """Durable row first, transport second, `active` only after the check.

        The old order was `registry.add` — which starts polling — and then the
        row. A crash in that window left a bot carrying somebody's messages with
        nothing in the register naming it: it worked until the next restart and
        then disappeared, and no reconciliation could have found it.
        """
        from pydantic import SecretStr

        # An existing row for this chat keeps its name, whatever the name was.
        # A bridge made by the old paste-the-token path is called `chat300000006`
        # and a deterministic one `examplebridge02`; inserting the second
        # alongside the first is the UNIQUE violation the preflight refuses.
        existing = await self._bridges.by_max_chat(max_chat_id)
        name = existing.bridge_name if existing is not None else bridge_name_for_username(
            username
        )
        # What the caller knows beats what the chat can be asked. A dialog the
        # owner started by number has no server-side existence until their first
        # message, so `contact_of_chat` on it answers None — and the bot would
        # come up with neither the contact's name nor their face.
        contact_id = max_user_id or await self._contact_of_chat(max_chat_id)
        token = os.environ.get(token_env, "").strip()
        if not token:
            raise RuntimeError(f"${token_env} holds no token")

        known_bot_id = existing.telegram_bot_id if existing is not None else None
        await self._bridges.upsert(
            BridgeRecord(
                bridge_name=name,
                max_chat_id=max_chat_id,
                token_env=token_env,
                max_user_id=contact_id,
                telegram_bot_id=known_bot_id,
                title=title or None,
                source=BridgeSource.MTPROTO.value,
                state=BridgeState.PROVISIONING,
                expected_username=username,
            )
        )

        if self._registry.by_name(name) is not None:
            # Exactly one worker per bot: a resumed run must not start a second.
            await self._registry.remove(name)

        live = await self._registry.add(
            ResolvedBridge(
                name=name,
                max_chat_id=max_chat_id,
                token_env=token_env,
                token=SecretStr(token),
                source=BridgeSource.MTPROTO,
            ),
            expected=ExpectedBot(bot_id=known_bot_id, username=username),
        )
        # `getMe` has now proved which bot this is. A legacy row with no id gets
        # one here, once, and it is checked against from the next start onwards.
        await self._bridges.upsert(
            BridgeRecord(
                bridge_name=name,
                max_chat_id=max_chat_id,
                token_env=token_env,
                max_user_id=contact_id,
                telegram_bot_id=live.identity.bot_id,
                title=title or None,
                source=BridgeSource.MTPROTO.value,
                state=BridgeState.PROVISIONING,
                expected_username=username,
            )
        )
        # A cutover recorded, per chat, the newest MAX message that existed when
        # the old bot stopped serving it. The dedup is keyed on the bot, so
        # without this the first backfill would carry that whole tail into the
        # new chat as if it were new.
        await apply_floor(
            self._bridges, data_dir=self._data_dir, bridge_name=name, chat_id=max_chat_id
        )
        if self._dress_bot is not None and contact_id is not None:
            # The same step `BridgeService.start` runs for bridges that were up
            # from the beginning. Skipping it here is what left the first
            # provisioned bot with no name and no avatar.
            await self._dress_bot(name, live.identity.bot_id, contact_id)
        return name

    async def is_healthy(self, max_chat_id: int) -> bool:
        """A bridge counts as up only when its own polling task is alive."""
        live = self._registry.by_max_chat(max_chat_id)
        return live is not None and live.runner.is_running

    async def forget_bot(self, max_chat_id: int, *, keeping: int | None = None) -> None:
        """Unbind a row from a Telegram bot that is no longer its own.

        Called when a walk has just ended with a bot for this contact. The old
        id is not stale data to tidy: `_expected_bot` reads it, and the identity
        check would refuse a *different* bot as somebody else's.

        `keeping` is the bot the walk ended with. Matching what the row already
        says means nothing changed — an existing bot was reused — and the row is
        left exactly as it is.
        """
        record = await self._bridges.by_max_chat(max_chat_id)
        if record is None or record.telegram_bot_id is None:
            return
        if keeping is not None and record.telegram_bot_id == keeping:
            return
        logger.info(
            "bridge %s no longer belongs to bot %s", record.bridge_name, record.telegram_bot_id
        )
        await self._bridges.forget_bot(record.bridge_name)

    async def mark_active(self, max_chat_id: int) -> None:
        """The last step: the row stops saying `provisioning` and starts serving."""
        record = await self._bridges.by_max_chat(max_chat_id)
        if record is None:
            return
        await self._bridges.set_state(record.bridge_name, BridgeState.ACTIVE)


class LazyOwnedBots:
    """The user session, opened on the first question that actually needs it.

    Exactly five calls wide, and every one of them is something Bot API cannot
    answer: which bots the account owns, who holds a username, what the creation
    limit is, whether the account is Premium, and `/start` — because a bot may
    not open a conversation.

    Creating and deleting bots is deliberately *not* reachable from here. Those
    used to live on the same object, and having them within reach is how the
    manager path could quietly fall back to driving @BotFather again.
    """

    def __init__(self, connect: Callable[[], Awaitable[Any]]) -> None:
        self._connect = connect
        self._session: Any = None
        self._lock = asyncio.Lock()

    async def _session_now(self) -> Any:
        if self._session is None:
            async with self._lock:
                if self._session is None:
                    self._session = await self._connect()
        return self._session

    async def admined_bots(self) -> list[Any]:
        session = await self._session_now()
        bots: list[Any] = await session.admined_bots()
        return bots

    async def username_holder(self, username: str) -> int | None:
        session = await self._session_now()
        holder: int | None = await session.username_holder(username)
        return holder

    async def bot_creation_limit(self) -> int | None:
        session = await self._session_now()
        limit: int | None = await session.bot_creation_limit()
        return limit

    async def is_premium(self) -> bool:
        session = await self._session_now()
        return bool(await session.is_premium())

    async def send_start(self, username: str, bot_id: int | None = None) -> None:
        session = await self._session_now()
        await session.send_start(username, bot_id)

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is None:
            return
        with contextlib.suppress(Exception):
            await session.close()


class MaxHistorySource:
    """Replay one chat's tail through the normal delivery path.

    Which is what makes a second import free: `claim_from_max` refuses a MAX
    message that already has a Telegram message, so the duplicates never reach
    Telegram at all.

    Silently, too. Fifty messages the owner read in MAX years ago are worth
    reading again in Telegram and are not worth fifty notifications, so the
    whole walk runs inside `quietly()` — see `bridge/telegram/quiet.py` for why
    that is a context variable and not an argument. Nothing is switched back
    afterwards: the variable is scoped to the import, and real-time delivery never
    entered it.
    """

    def __init__(
        self,
        *,
        client: MaxClient,
        deliver: Callable[[IncomingMaxMessage], Awaitable[None]],
    ) -> None:
        self._client = client
        self._deliver = deliver

    async def import_chat(
        self,
        max_chat_id: int,
        *,
        limit: int | None,
        after: int | None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> ChatImport:
        history = await self._client.fetch_history(max_chat_id, limit=limit)
        # Oldest first, so the conversation reads in the order it happened.
        wanted = [
            message for message in history if after is None or (message.message_id or 0) > after
        ]
        total = len(wanted)
        cursor = after
        with quietly():
            for index, message in enumerate(wanted, start=1):
                await self._deliver(message)
                identifier = message.message_id or 0
                if cursor is None or identifier > cursor:
                    cursor = identifier
                if on_progress is not None:
                    on_progress(index, total)
        return ChatImport(delivered=total, cursor=cursor)


class BridgeService:
    """Owns every long-lived object in the *bridge worker*.

    The guardian bot is deliberately not one of them when the process runs under
    `TelemaxRuntime`: the guardian answers before MAX exists and survives a
    restart of this service, so it is passed in, and a second polling loop for
    the same token is never created. Started on its own — the old shape, still
    used by tests — it makes its own.
    """

    def __init__(
        self,
        loaded: LoadedConfig,
        *,
        guardian: Guardian | None = None,
        guardian_context: GuardianContext | None = None,
        mirror_own_messages: Callable[[], bool] | None = None,
        state: StateStore | None = None,
    ) -> None:
        self._loaded = loaded
        # The runtime's own store, handed in, so there is exactly one of them
        # over `onboarding.json` in the serving process. A second store is not
        # a second file — it is a second cache, and the one that writes last
        # with a stale cache rolls the other's fields back. Built here only for
        # the standalone shape and for tests, which have no runtime above them.
        self._state: StateStore = state or StateStore.for_data_dir(loaded.app.paths.data_dir)
        #: Built once, because it remembers which lifecycles it has closed —
        #: state that must outlive one alert.
        self._notifications: NotificationCentre | None = None
        # How the router learns whether to carry the owner's own MAX messages.
        # A resolver rather than a value so an owner's flip on the home screen
        # takes effect without a restart; when nobody wires one (tests, the
        # standalone shape) the config default answers, read fresh each time.
        self._mirror_own_messages: Callable[[], bool] = (
            mirror_own_messages
            if mirror_own_messages is not None
            else (lambda: loaded.app.own_messages.mirror)
        )
        self._database: Database | None = None
        self._max: MaxClient | None = None
        self._registry: BotRegistry | None = None
        self._guardian: Guardian | None = guardian
        self._owns_guardian = guardian is None
        self._guardian_context = guardian_context
        self._uploader: MediaUploader | None = None
        # Native voice/circle counters and the per-kind breaker, shared between
        # the MAX client that writes them and health, which reads them.
        self._native_media: NativeMediaState | None = None
        # The retry side of delivery, and the durable intake underneath polling.
        self._workers: WorkerPool | None = None
        self._pipe: DeliveryPipe | None = None
        self._inbox: TelegramInboxRepository | None = None
        self._health: HealthService | None = None
        self._alerts: AlertRepository | None = None
        self._send_job: (
            Callable[[str, Direction, dict[str, Any], SendingHook], Awaitable[int | None]] | None
        ) = None
        self._supervisor = Supervisor()
        self._stopped = asyncio.Event()
        # Opened on the first operation that needs it, and shared: one Telethon
        # session per process, not one per thing that wants to create a bot.
        self._owned: LazyOwnedBots | None = None
        self._provisioner_port: Any = None
        # What start-up made of the attempts the last run left unfinished.
        # Read by `/status`; None until reconciliation has run.
        self._provisioning_report: Any = None
        self._contacts: Provisioner | None = None
        self._flow: DialogFlow | None = None
        # The one writer of `bots.env` for the life of this service. Built here
        # rather than per provisioning walk: its lock is the whole point, and a
        # lock created inside the thing that takes it excludes nobody.
        self._secrets = ContactBotSecretStore(loaded.app.secrets_file)
        # And the one thing that serialises a contact against itself, for the
        # same reason. Every entry point — picker, by-number, card, deep link,
        # the announcement button, startup reconciliation — goes through it.
        self._coordinator = ProvisioningCoordinator()
        self._status_of: StatusProvider | None = None
        self._messages: MessageMapRepository | None = None
        self._bridge_rows: BridgeRepository | None = None
        #: The read side of the queue and of the per-bridge observations, for
        #: the guardian's cards. Set with the database, never before it.
        self._outbox: OutboxRepository | None = None
        self._bridge_state: BridgeStateRepository | None = None
        self._dress_bot: Callable[[str, int, int], Awaitable[None]] | None = None
        # The owner's MTProto user session, opened only while intake is enabled.
        # In Stage 1 the gate is off, so this stays None and Bot API is
        # authoritative; the field exists so health can report "not connected".
        self._owner_session: Any = None
        #: Latched once the puppet session has connected in this process. Tells
        #: "coming up" from "dropped" without an await.
        self._owner_session_connected_before = False
        # The owner-message baseline is a per-process step, not a per-connect one.
        self._owner_baseline_done = False
        self._reactions: ReactionSync | None = None
        # The single reader of an owner-side UpdateEditMessage. Kept because
        # `/status` reads its counters and the baseline writes through it.
        self._owner_updates: OwnerUpdateDispatch | None = None
        # Records the bot's own id for the owner's own messages. Observation
        # only: it creates no owner event, and a guard says so.
        self._owner_binding: OwnerBotSideBinding | None = None
        self._owner_intake: Any = None
        # Re-downloads a media job's parts over the owner session on retry. Set
        # only when that session is up; None otherwise, which is fine because an
        # MTProto media job only exists once intake is enabled.
        self._mtproto_media: Any = None
        # The one router both intakes route into. Set once it is built.
        self._router: Any = None
        # Where a Telegram read mark turns into a MAX one. Set with the router.
        self._auto_read: Any = None

    @property
    def max_client(self) -> MaxClient | None:
        return self._max

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.stop()

    async def start(self) -> None:
        app = self._loaded.app

        self._database = await Database.connect(app.paths.db_path)
        messages = MessageMapRepository(self._database)
        state = BridgeStateRepository(self._database)
        bridges = BridgeRepository(self._database)
        # Kept: re-pulling a conversation has to remove what was placed before,
        # and that means reading the map and the cursor long after start-up.
        self._messages = messages
        self._bridge_rows = bridges
        outbox = OutboxRepository(self._database)
        # Kept for the read side. The guardian's bridge cards used to ask each
        # bridge bot for its own `/status` text and print it verbatim; they now
        # ask for the same numbers and say them in words, which needs the two
        # repositories rather than a rendered block.
        self._outbox = outbox
        self._bridge_state = state

        # Anything left in flight belongs to a process that is no longer running.
        requeued, unresolved = await outbox.requeue_inflight()
        if requeued:
            logger.info("requeued %s job(s) left over from the last run", requeued)
        if unresolved:
            logger.warning(
                "%s job(s) were mid-send when the last run died; marked ambiguous"
                " for the owner to resolve",
                unresolved,
            )

        phone = os.environ.get(app.max.phone_env, "").strip()
        if not phone:
            raise RuntimeError(
                f"${app.max.phone_env} is not set — connect MAX in the guardian bot first"
            )

        # Built here rather than inside the client so health can read the same
        # object the sender writes to. In RAM on purpose: the breaker is a
        # statement about this process's MAX session, and a restart is exactly
        # the event that should clear it.
        self._native_media = NativeMediaState(
            enabled=app.max.native_media.enabled,
            voice_enabled=app.max.native_media.voice_enabled,
            circle_enabled=app.max.native_media.circle_enabled,
        )
        self._max = MaxClient(
            phone=phone,
            session_dir=app.max_session_dir,
            session_name=app.max.session_name,
            # Sending a sticker to MAX creates one in the owner's collection, so
            # the same picture must resolve to the same id on every send.
            stickers=StickerCacheRepository(self._database),
            sticker_origins=StickerOriginRepository(self._database),
            # Only used the first time this install draws a device identity: the
            # phone it claims should sit in the owner's own timezone.
            timezone=app.telegram.timezone,
            native_media=self._native_media,
            # A socket the bridge never closes is what MAX reads as «В сети», so
            # the keepalive's `interactive` flag is what has to say otherwise.
            own_presence=PresenceMode(app.max.own_presence.value),
            own_presence_idle_seconds=app.max.own_presence_idle_seconds,
        )
        await self._max.start()

        # Kept on the service as well as handed to the dispatcher: the bridge
        # card in the guardian shows the same lines `/status` does in the bot's
        # own chat, and two renderings of one state would drift.
        self._status_of = self._make_status_provider(state, outbox)
        # The one thing only the Bot API side can see: who wrote a forwarded
        # message, as their own profile has them. The owner's session answers
        # with the owner's address-book label and offers no way to ask for
        # anything else, so a bot records it here and the MTProto intake reads
        # it back.
        self._forward_authors = ForwardAuthorRepository(self._database)
        dispatcher = build_dispatcher(
            owner_user_id=app.telegram.owner_user_id,
            status_provider=self._status_of,
            forward_authors=self._forward_authors,
            # Resolved on each `/start` rather than now: the guardian's `getMe`
            # has not answered at this point in start-up, and its username is
            # the whole content of the button.
            guardian_username=lambda: self._manager_username(app),
        )
        # Every contact bot writes its updates down before acknowledging them to
        # Telegram. Passing the inbox here is what turns that on.
        self._inbox = TelegramInboxRepository(self._database)
        # Updates the last process was carrying when it died. Nothing holds them
        # now, so they go straight back in the queue rather than waiting out a
        # lease that only a live worker could have been renewing.
        self._alerts = AlertRepository(self._database)
        self._health = HealthService(
            health=HealthStateRepository(self._database),
            outbox=outbox,
            inbox=self._inbox,
            alerts=self._alerts,
            db_path=app.paths.db_path,
            database=self._database,
            owner_inbox=OwnerUpdateInboxRepository(self._database),
            temp_dir=app.paths.resolved_temp_dir,
            bridges=lambda: [
                (live.name, live.runner.is_running)
                for live in (self._registry.live if self._registry else ())
            ],
            max_is_ready=lambda: bool(self._max and self._max.is_ready),
            tg_session_is_ready=lambda: bool(
                self._owner_session and self._owner_session.is_connected
            ),
            native_media=lambda: (
                self._native_media.snapshot() if self._native_media is not None else ()
            ),
            identity_repair=lambda: self._max.identity_repair if self._max else None,
            owner_session_facts=self._owner_session_facts,
        )
        if self._max.is_ready:
            # The session came up during startup; the watchdog only runs a
            # minute later and the snapshot should not claim ignorance until then.
            await self._health.note_max_connected()
        # Read before `note_start` overwrites it: this is when the run that just
        # ended began, and it is the exact bound the claim sweep needs.
        previous_run_started_at = await self._health.previous_start_at()
        unclean = await self._health.note_start(
            schema_version=await self._database.schema_version(), commit=_commit_of()
        )
        if unclean:
            logger.warning("the previous run did not shut down cleanly")

        released = await self._release_unsent_claims(
            messages, MediaGroupRepository(self._database), since_ms=previous_run_started_at
        )
        if released:
            logger.warning(
                "released %s MAX claim(s) the last run left without a delivery job;"
                " those messages will be carried again",
                released,
            )

        reclaimed = await self._inbox.requeue_leased()
        if reclaimed:
            logger.info("took back %s update(s) left in flight by the last run", reclaimed)
        self._registry = BotRegistry(
            dispatcher,
            inbox=self._inbox,
            guardian_bot_id=self._guardian.bot_id if self._guardian else None,
        )

        started_bridges: list[tuple[str, int, int]] = []
        # The YAML is a seed; the database is the register. A bridge the
        # guardian made lives only there, and iterating the config alone would
        # quietly drop it at the next restart — the bot would sit in Telegram
        # with nothing polling it.
        known = {record.bridge_name: record for record in await bridges.all()}
        for bridge in [*self._loaded.bridges, *await self._restore_bridges(bridges)]:
            if not bridge.enabled:
                continue
            try:
                # Validated and registered, but not polling yet. Starting here
                # would begin feeding updates to a dispatcher whose routers are
                # still a hundred lines and several MAX round trips away: no
                # handler matches, `feed_update` returns happily, and the update
                # is marked handled. That is a silently dropped message, and it
                # lands exactly on the durable inbox rows a restart exists to
                # deliver. Runners start at the end of `start()`.
                live = await self._registry.add(
                    bridge, start=False, expected=_expected_bot(known.get(bridge.name))
                )
            except BridgeRegistryError as error:
                # One broken bridge must not take the others down with it. Loud,
                # though: this used to be a line in the log and nothing else, so
                # a bridge that never came up was invisible everywhere the owner
                # looks.
                logger.error("%s", error)
                await state.note_error(bridge.name, str(error))
                await self._bridge_not_started(bridge.name, error)
                continue
            # Who is on the other end — needed to dress the bot up as them.
            contact_id = await self._max.contact_of_chat(bridge.max_chat_id)
            await bridges.upsert(
                BridgeRecord(
                    bridge_name=bridge.name,
                    max_chat_id=bridge.max_chat_id,
                    token_env=bridge.token_env,
                    max_user_id=contact_id,
                    telegram_bot_id=live.identity.bot_id,
                    source=bridge.source.value,
                )
            )
            if contact_id is not None:
                started_bridges.append((bridge.name, live.identity.bot_id, contact_id))

        media = MediaPipeline(
            sources=MaxMediaSources(self._max),
            temp_files=TempFiles(app.paths.resolved_temp_dir),
            fetcher=HttpFetcher(),
            max_file_size_mb=app.media.max_file_size_mb,
        )
        media.sweep_orphans()
        # A crash mid-transfer leaves a file behind; sweeping only at start-up
        # would let a long-running process accumulate them for months.
        self._supervisor.start("media-sweeper", _sweeper(media))
        # PyMax reconnects on its own, but nothing notices if its task dies.
        self._supervisor.start("max-watchdog", _max_watchdog(self._max, self._health))

        # One bot per dialog only reads as a conversation if the bot wears the
        # contact's name and face. Best-effort and off the critical path: a
        # rate-limited rename must not hold up delivery.
        profiles = BotProfileSync(
            writer=TelegramProfileAdapter(self._registry),
            contacts=self._max,
            pipeline=media,
            # So the About line of every bridge names the guardian — the way
            # back that does not depend on an update arriving.
            guardian_username=lambda: self._manager_username(app),
        )
        # Kept for the bridges the gateway brings up later: a bot provisioned
        # mid-run has to be dressed as its contact too, and doing it only here
        # is why the first one arrived with a blank avatar.
        self._dress_bot = self._make_profile_sync(profiles, bridges)
        for name, bot_id, contact_id in started_bridges:
            await self._dress_bot(name, bot_id, contact_id)

        lookup = RegistryLookup(self._registry)
        provisioner = self._build_provisioner(bridges, app)
        # Kept: disconnecting a bridge has to stop the guardian asking about that
        # contact again, and this is what owns the pending-contact state.
        self._contacts = provisioner
        presence = TelegramPresenceAdapter(self._registry)

        contacts_by_user: dict[int, tuple[str, int]] = {
            contact_id: (name, bot_id) for name, bot_id, contact_id in started_bridges
        }
        pinned_status = PinnedStatus(
            renderer=presence,
            store=bridges,
            config=app.presence,
            timezone=_timezone_of(app),
        )
        status_line = StatusLine(
            renderer=presence,
            read_state=ReadStateRepository(self._database),
            config=app.presence,
            timestamp_style=app.telegram.message_timestamp,
            timezone=_timezone_of(app),
        )
        # Shared by both directions on purpose: one side writes down what it
        # placed as the owner, the other refuses to send that same line back.
        own_echoes = OwnEchoes()

        telegram_sender = RegistrySender(self._registry)
        max_sender = MaxTextSender(self._max)
        # Built once and shared by the live path and the retry path. Two
        # instances would be two behaviours, and the retry is exactly where a
        # divergence would go unnoticed.
        # One repository for the album parts, shared by the three readers of
        # them: the sender that writes the expected aliases, the settlement that
        # binds them, and the owner-session intake that assembles incoming ones.
        album_parts = MediaGroupRepository(self._database)
        # The one place an album's identity is written down, and the only caller
        # of `Database.transaction()` in the settlement path: the head and every
        # alias land together or not at all.
        album_settlement = AlbumSettlementRepository(self._database)
        # Editing and deleting the bot's own messages. A separate adapter from
        # the sender because it is a separate contract: these raise, and the job
        # behind them is what decides what a failure meant.
        bot_mutations = RegistryMutations(self._registry)
        max_media = MaxMediaDelivery(
            pipeline=media,
            sender=RegistryMediaSender(self._registry),
            # So a sticker carried out of MAX can be recognised coming back and
            # returned as itself, animation included.
            sticker_origins=StickerOriginRepository(self._database),
            # The "поднять мост" button on a shared MAX contact points here — at
            # the guardian, by deep link, so no provisioning logic leaks into the
            # contact bot. Empty username → no button, card still shown.
            # From `getMe`, with the environment as a checked cache. An empty
            # variable used to yield a link with an empty path segment, which
            # opens nothing and reports nothing.
            bridge_link=GuardianBridgeLink(self._manager_username(app)),
        )

        # The owner's own transports, built before the sender that uses them and
        # handed to the router as the *same* objects: the router decides whether
        # a message goes out as the owner's, the sender is what places it, and a
        # second instance between them would be a second behaviour.
        owner_voice = self._own_voice(app)
        owner_media = self._own_media(app, media, own_echoes)

        async def send_job(
            kind: str,
            direction: Direction,
            payload: dict[str, Any],
            sending: SendingHook,
        ) -> int | None:
            """Perform one queued delivery. The only sender the queue knows.

            Used by the inline attempt *and* by the retry worker, so a message
            that is retried is sent the same way it was sent the first time.
            """

            async def settled_max_to_tg(sent: int, receipt: DeliveryReceipt | None = None) -> int:
                """This delivery's settle step, bound to this job's payload."""
                return await settle_telegram_delivery_mapping(
                    self._messages,
                    payload,
                    telegram_message_id=sent,
                    receipt=receipt,
                    albums=album_settlement,
                )

            if kind == KIND_MAX_TO_TG_TEXT:
                await sending()
                sent = await telegram_sender.send_text(
                    payload["bot_id"],
                    payload["chat_id"],
                    payload["text"],
                    reply_to=payload.get("reply_to"),
                    entities=payload.get("entities"),
                )
                if sent is None:
                    raise UnconfirmedDeliveryError("Telegram returned no message id")
                return await settled_max_to_tg(sent)
            if kind == KIND_MAX_TO_TG_MEDIA:
                # The attachments are rebuilt from the job and handed to the
                # same delivery object the live path uses, so an album, a
                # caption and a video note behave identically on a retry. The
                # download URL is resolved inside it, fresh, from the MAX ids in
                # the payload — never from a stored link.
                restored = IncomingMaxMessage(
                    message_id=payload["max_message_id"],
                    chat_id=payload["max_chat_id"],
                    sender_id=None,
                    text=payload.get("text", ""),
                    timestamp=payload.get("timestamp", 0),
                    is_outgoing=bool(payload.get("is_outgoing")),
                    attachments=tuple(
                        attachment_from_payload(item) for item in payload.get("attachments", [])
                    ),
                    reply_to_message_id=payload.get("reply_to_message_id"),
                )
                # The hook travels inside: resolving, downloading and
                # validating every attachment happens first, and only the first
                # actual Telegram request announces itself. The receipt is the
                # same delivery with the rest of Telegram's answer attached —
                # every album part's own id, which appears nowhere else.
                receipt = await max_media.deliver_receipt(
                    restored,
                    bot_id=payload["bot_id"],
                    chat_id=payload["chat_id"],
                    caption=payload.get("caption", ""),
                    caption_entities=payload.get("caption_entities"),
                    reply_to=payload.get("reply_to"),
                    on_sending=sending,
                )
                if receipt.head is None:
                    # Every attachment was refused or unresolvable. The owner
                    # already has a placeholder line explaining which; saying
                    # "delivered" on top of that would be a second untruth.
                    raise UnconfirmedDeliveryError(
                        "Telegram returned no message id for the attachments"
                    )
                return await settled_max_to_tg(receipt.head, receipt)
            if kind == KIND_MAX_TO_TG_OWNER:
                # The owner's own message, placed by their own account. Same
                # queue, same worker, same ordering, same AMBIGUOUS — the only
                # things that differ are the transport underneath and which
                # column the id that comes back belongs in.
                #
                # There is no Bot API anywhere in this branch, and that is the
                # point: by the time a job exists the transport has been chosen,
                # and a failure here is a retry or a question, never a second
                # copy signed `Вы: …`.
                return await place_owner_message(
                    payload,
                    sending=sending,
                    voice=owner_voice,
                    media=owner_media,
                    messages=self._messages,
                    albums=album_settlement,
                    echoes=own_echoes,
                    # Resolved per job, never captured: the dispatch is built
                    # when the owner session comes up, which is after this
                    # closure exists and again after every reconnect.
                    baseline=lambda: self._owner_updates,
                )
            if kind == KIND_MAX_TO_TG_EDIT:
                # Not a delivery: the message is already in the chat and this
                # replaces its body. `sending()` is never called and this kind is
                # never AMBIGUOUS — an edit applied twice is the same edit, so
                # asking the owner to adjudicate one would be asking about
                # nothing. Which id, which id space and which of the two edit
                # methods are all decided here, from durable state.
                assert self._messages is not None
                return await resolve_max_edit(
                    messages=self._messages,
                    albums=album_parts,
                    outbox=outbox,
                    bot=bot_mutations,
                    owner=self._owner_session,
                    payload=payload,
                )
            if kind == KIND_MAX_TO_TG_DELETE:
                # The same, for removal — and the same reason it is durable: the
                # album this collapses is N Telegram messages, and taking one of
                # them out is not the job.
                assert self._messages is not None
                return await resolve_max_delete(
                    messages=self._messages,
                    albums=album_parts,
                    outbox=outbox,
                    bot=bot_mutations,
                    owner=self._owner_session,
                    payload=payload,
                )
            if kind == KIND_TG_TO_MAX_TEXT:
                from bridge.routing.text_chunks import split_max_text, text_part_source_key

                chunks = split_max_text(str(payload["text"]))
                if len(chunks) > 1:
                    # Compatibility path for an oversized job written by an
                    # older process.  Fan it out before touching MAX; the batch
                    # transaction means the parent can then finish without a
                    # crash losing or interleaving its tail.
                    assert self._messages is not None
                    link_id = int(payload["link_id"])
                    link = await self._messages.by_id(link_id)
                    if link is None:
                        raise RuntimeError("an oversized text job lost its mapping")
                    base = f"legacy-text:{link_id}"
                    await outbox.enqueue_batch(
                        bridge_name=link.bridge_name,
                        direction=Direction.TG_TO_MAX,
                        items=[
                            (
                                KIND_TG_TO_MAX_TEXT,
                                {
                                    "max_chat_id": payload["max_chat_id"],
                                    "text": chunk,
                                    "reply_to": payload.get("reply_to") if index == 0 else None,
                                    **({"link_id": link_id} if index == 0 else {}),
                                },
                                text_part_source_key(base, index=index, total=len(chunks)),
                            )
                            for index, chunk in enumerate(chunks)
                        ],
                    )
                    logger.info(
                        "split one legacy Telegram text into %s MAX messages",
                        len(chunks),
                    )
                    return None
                await sending()
                sent = await _creating_send(
                    lambda: max_sender.send_text(
                        payload["max_chat_id"],
                        payload["text"],
                        reply_to=payload.get("reply_to"),
                    )
                )
                return await settle_max_delivery_mapping(self._messages, payload, sent)
            if kind == KIND_TG_TO_MAX_CONTACT:
                await sending()
                sent = await _creating_send(
                    lambda: max_sender.send_contact(
                        payload["max_chat_id"],
                        vcard=payload["vcard"],
                        contact_user_id=payload.get("contact_user_id"),
                        reply_to=payload.get("reply_to"),
                    )
                )
                return await settle_max_delivery_mapping(self._messages, payload, sent)
            if kind == KIND_OWNER_ECHO_BIND:
                # Not a delivery: the message is already in the chat. This only
                # gives the mapping the owner's own id for it, so a reply or a
                # deletion made from their client can find the MAX message. An
                # album carries its ordered parts in the payload and binds all of
                # them to the aliases of the one message they became.
                assert self._messages is not None
                if payload.get("parts"):
                    return await resolve_owner_album_echo(
                        messages=self._messages,
                        albums=album_parts,
                        state=state,
                        payload=payload,
                    )
                return await resolve_owner_echo(
                    messages=self._messages, state=state, payload=payload
                )
            if kind == KIND_TG_ALBUM_SWEEP:
                # Not a delivery either: the MAX message is already gone, and
                # this takes the rest of the Telegram album with it so the two
                # sides agree. Only the owner's own account can remove parts the
                # owner sent, so it goes through the session that watches them.
                return await self._sweep_album_in_telegram(payload)
            if kind == KIND_TG_TO_MAX_EDIT:
                assert self._messages is not None
                return await resolve_owner_edit(
                    outbox=outbox, messages=self._messages, max_sender=max_sender, payload=payload
                )
            if kind == KIND_TG_TO_MAX_REACTION:
                # Not a delivery: nobody receives a reaction. It is here because
                # a durable record is the only thing that survives a process
                # dying between the update and the call — and because a job that
                # waited out a MAX outage must not put back a reaction the owner
                # has replaced since.
                assert self._database is not None and self._max is not None
                # The MAX client itself, not the text adapter: a reaction is not
                # a message and `MaxTextSender` has no `add_reaction` on it. The
                # smoke found this the hard way — every reaction job failed on
                # an AttributeError until the job's own retry gave up.
                return await resolve_owner_reaction(
                    state=OwnerMessageStateRepository(self._database),
                    snapshots=ReactionStateRepository(self._database),
                    max_sender=MaxReactionAdapter(self._max),
                    payload=payload,
                )
            if kind == KIND_TG_TO_MAX_DELETE:
                assert self._messages is not None
                return await resolve_owner_delete(
                    outbox=outbox, messages=self._messages, max_sender=max_sender, payload=payload
                )
            if kind == KIND_TG_TO_MAX_MEDIA:
                items = [(item[0], Path(item[1]), item[2]) for item in payload.get("items", [])]
                if all(path.exists() for _, path, _ in items) and items:
                    await sending()
                    sent = await _carry_media_into_max(max_sender, payload, items)
                    return await settle_max_delivery_mapping(self._messages, payload, sent)

                # A retry: the temp files from the first attempt are long gone,
                # deleted when that attempt returned. The message is re-fetched
                # from whatever durable reference the intake left — a Bot API
                # file_id, or an MTProto reference re-downloaded over the owner
                # session. Without this every media retry failed on the missing
                # paths, which turned a MAX blip into a permanently undelivered
                # photo.
                mtproto_refs = payload.get("mtproto") or []
                if mtproto_refs:
                    async with AsyncExitStack() as stack:
                        refetched = await self._refetch_mtproto_media(stack, mtproto_refs)
                        await sending()
                        sent = await _carry_media_into_max(max_sender, payload, refetched)
                    return await settle_max_delivery_mapping(self._messages, payload, sent)
                sources = payload.get("sources") or []
                if not sources:
                    raise PermanentDeliveryError(
                        "the downloaded files are gone and this job has no Telegram file ids"
                    )
                async with AsyncExitStack() as stack:
                    # Re-fetching from Telegram happens before the mark: it is
                    # preparation, and a crash during it must stay a retry.
                    refetched = await self._refetch_media(stack, payload, sources, media)
                    await sending()
                    sent = await _carry_media_into_max(max_sender, payload, refetched)
                return await settle_max_delivery_mapping(self._messages, payload, sent)
            raise PermanentDeliveryError(f"unknown delivery kind {kind!r}")

        async def note_delivered(bridge_name: str, kind: str, remote_id: int | None) -> None:
            await state.note_delivery(bridge_name)

        pipe = DeliveryPipe(outbox=outbox, send=send_job, on_delivered=note_delivered)
        self._pipe = pipe
        self._send_job = send_job

        router = BridgeRouter(
            lookup=lookup,
            telegram=telegram_sender,
            max_sender=max_sender,
            pipe=pipe,
            messages=messages,
            state=state,
            owner_chat_id=app.telegram.owner_user_id,
            timestamp_style=app.telegram.message_timestamp,
            timezone=_timezone_of(app),
            on_unbridged=self._make_unbridged_handler(provisioner),
            delivery_observer=status_line,
            media=max_media,
            own_voice=owner_voice,
            own_media=owner_media,
            own_echoes=own_echoes,
            mirror_own_messages=self._mirror_own_messages,
            # The per-part aliases of an album, in both directions: the same
            # table the Bot API collector already uses, read here as the mapping
            # a reply, an edit or a delete of one part resolves through.
            albums=album_parts,
            # Only forwards need this: it is the one message that names somebody
            # the bridge has never routed anything for.
            display_names=MaxDisplayNames(lambda: self.max_client),
        )
        # Kept so the gated owner-session intake can route into the very same
        # router the Bot API path uses — one delivery engine, not two.
        self._router = router

        typing_mirror = TelegramTypingMirror(presence, app.presence)
        receipts = ReadReceipts(
            renderer=presence,
            messages=messages,
            read_state=ReadStateRepository(self._database),
            config=app.presence,
            status_line=status_line,
        )
        auto_read = AutoRead(
            marker=self._max,
            read_state=ReadStateRepository(self._database),
            config=app.presence,
            messages=messages,
        )
        # Kept for the same reason as the router: the owner session, when it is
        # enabled, is where "the owner actually read it" comes from.
        self._auto_read = auto_read
        # Built before the handlers: several of them mark a dialog as live, and
        # the reaction poller reads that to decide how often to ask about it.
        activity = DialogActivity()
        reactions = ReactionSync(
            renderer=TelegramReactionAdapter(self._registry),
            max_sender=MaxReactionAdapter(self._max),
            messages=messages,
            snapshots=ReactionStateRepository(self._database),
            config=app.reactions,
            # An emoji MAX has no reaction for becomes a *message*, so it takes
            # the same durable queue as every other one.
            notes=router,
        )
        # Kept because the owner's session needs it too: a reaction the owner
        # makes in Telegram is read from an update, and the domain logic that
        # puts it into MAX is this one object, not a second copy of it.
        self._reactions = reactions
        self._owner_binding = OwnerBotSideBinding(messages=messages)

        # Commands first, then presence (which owns /read), then the catch-all
        # that forwards to MAX. Reversing this would type `/read` at somebody's
        # mother.
        dispatcher.include_router(
            build_presence_router(auto_read=auto_read, lookup=lookup.bridge_for_bot)
        )
        picker = DialogPicker(self._max) if self._max is not None else None
        if provisioner is not None:
            if self._guardian_context is not None:
                # The guardian's handlers were registered once, before MAX
                # existed. Hand them the live objects rather than a second copy
                # of the same routes.
                self._guardian_context.provisioner = provisioner
                self._guardian_context.picker = picker
            else:
                # Registered before the catch-all so a pasted token is consumed
                # here and never forwarded to a contact.
                dispatcher.include_router(build_guardian_router(provisioner, picker=picker))
        if self._guardian_context is not None:
            # What /failed and /retry act on. The handlers live on the
            # guardian's dispatcher and outlive this worker.
            self._guardian_context.outbox = outbox
            self._guardian_context.bridge_names = tuple(
                live.name for live in (self._registry.live if self._registry else ())
            )
            # `/attempts`, `/provretry`, `/provabandon`: the same reason again —
            # registered once on the guardian's dispatcher, acting on a worker
            # that is replaced with every restart.
            self._guardian_context.journal = ProvisioningJournal.for_data_dir(
                app.paths.data_dir
            )
            self._guardian_context.resume_attempt = self._resume_attempt
        # Attachments first: the forwarding router answers plain text and would
        # otherwise tell the owner that media is unsupported.
        uploader = MediaUploader(
            bridge_router=router,
            pipeline=media,
            albums=album_parts,
            timestamp_style=app.telegram.message_timestamp,
            timezone=_timezone_of(app),
        )
        self._uploader = uploader
        dispatcher.include_router(
            build_upload_router(
                uploader,
                on_owner_message=auto_read,
                own_echoes=own_echoes,
                on_owner_intake_suppressed=self._note_intake_suppressed,
                on_owner_message_seen=self._note_owner_message_seen,
            )
        )
        dispatcher.include_router(
            build_forwarding_router(
                router,
                on_owner_message=auto_read,
                own_echoes=own_echoes,
                on_owner_intake_suppressed=self._note_intake_suppressed,
                on_owner_message_seen=self._note_owner_message_seen,
                timestamp_style=app.telegram.message_timestamp,
                timezone=_timezone_of(app),
            )
        )
        # The owner's MTProto user session, gated: opened and supervised only
        # while intake is enabled, and only now that the router it feeds exists.
        # Off in Stage 1, so this is a no-op in production.
        await self._maybe_start_owner_session(app, media)

        await self._start_guardian(dispatcher, app)

        deliver = _chain(
            router.on_max_message, _note_incoming(lookup, auto_read), _touch_dialog(activity)
        )
        self._build_dialog_flow(app, bridges, picker, deliver)
        self._max.on_message(deliver)
        self._max.on_reconnect(self._make_backfill(deliver))
        if provisioner is not None:
            provisioner.attach_backfiller(self._make_chat_backfill(deliver))
        self._max.on_message_edit(router.on_max_edit)
        self._max.on_message_delete(router.on_max_delete)
        self._max.on_typing(
            _typing_handler(lookup, typing_mirror, app.telegram.owner_user_id, activity)
        )
        self._max.on_read(_read_handler(lookup, receipts, activity))
        self._max.on_presence(
            _presence_handler(contacts_by_user, pinned_status, app.telegram.owner_user_id)
        )
        await self._fill_pinned_status(pinned_status, contacts_by_user, app.telegram.owner_user_id)
        self._max.on_reaction(_reaction_handler(lookup, reactions))
        self._max.on_chat_reaction(_chat_reaction_handler(lookup, reactions, activity))
        if app.reactions.poll_seconds > 0 and app.reactions.style is not ReactionStyle.OFF:
            self._supervisor.start(
                "reaction-poller",
                _reaction_poller(self._registry, reactions, activity, app.reactions, state),
            )

        # The retry side of delivery. Until now this existed, was tested, and was
        # never started: `enqueue()` had no caller outside the test suite, so a
        # failed send had nowhere to go. One worker per bridge, so a contact
        # whose queue is stuck cannot delay anybody else's messages.
        # Now that every handler is registered, the bots may start polling and
        # the intake may start draining what the last run left behind.
        for live in self._registry.live:
            live.runner.start()

        self._workers = WorkerPool()
        for live in self._registry.live:
            self._workers.add(
                OutboxWorker(
                    bridge_name=live.name,
                    outbox=outbox,
                    state=state,
                    deliver=self._retry_delivery(send_job),
                )
            )
        self._supervisor.start("outbox-sweeper", self._sweep_outbox(outbox))
        self._supervisor.start("health", self._health_loop())

        # Albums the last process accepted and never got to send. Their parts
        # were stored as they arrived, so what is left is to notice them and let
        # the ordinary flush timer take it from here.
        try:
            restored = await uploader.restore_pending()
            if restored:
                logger.info("restored %s unfinished album(s) from the last run", restored)
        except Exception:
            logger.exception("could not restore unfinished albums")

        # Catch up on anything that arrived while the process was not running.
        await self._make_backfill(deliver)()

        # And keep catching up. Start-up and reconnect were the only two moments
        # the tail was ever re-read, which assumes every message a live socket
        # was up for actually reached the router — and one that does not is
        # invisible by construction: no frame, no claim, no job, nothing to
        # notice. So the same catch-up runs on a timer as well.
        if app.max.history_reconcile_seconds > 0:
            # Said out loud, once: a safety net nobody can see is one nobody
            # trusts, and this one is silent by design when it finds nothing.
            logger.info(
                "re-reading each chat's tail every %ss to catch what live delivery missed",
                int(app.max.history_reconcile_seconds),
            )
            self._supervisor.start(
                "history-reconcile",
                _on_a_timer(
                    app.max.history_reconcile_seconds,
                    self._make_backfill(deliver),
                    label="history reconcile",
                ),
            )

        # And on anything the last run was in the middle of *making*. Last, and
        # deliberately: the four bridges that already work come up first, and a
        # provisioning attempt that cannot be finished must not delay them.
        await self._reconcile_provisioning(bridges)

        logger.info("bridge is up with %s live bridges", len(self._registry.live))

    def _health_loop(self) -> Callable[[], Awaitable[None]]:
        """Refresh health, decide what is worth an alert, and send what is queued.

        One loop rather than three: the numbers are read once, the decisions come
        from that reading, and the queue is drained in the same pass. A minute
        apart, because this writes to SQLite and nothing here changes faster than
        a person reads it.
        """

        async def loop() -> None:
            while True:
                await asyncio.sleep(HEALTH_INTERVAL_SECONDS)
                health, alerts = self._health, self._alerts
                if health is None or alerts is None:
                    continue
                try:
                    snapshot = await health.snapshot()
                    raised = await health.evaluate(snapshot)
                    if raised:
                        logger.info("health incidents changed: %s", ", ".join(raised))
                except Exception:
                    logger.exception("health evaluation failed")
                    continue

                guardian = self._guardian
                if guardian is None:
                    continue
                try:
                    await AlertDispatcher(
                        alerts=alerts, deliver=self._notify_lifecycle
                    ).drain_once()
                except Exception:
                    logger.exception("could not deliver alerts")

        return loop

    async def _release_unsent_claims(
        self,
        messages: MessageMapRepository,
        albums: MediaGroupRepository,
        *,
        since_ms: int | None,
    ) -> int:
        """Let go of MAX claims the last run wrote and never gave a job.

        The claim and the job are two commits, and everything between them —
        rendering, the branch decision, an album's aliases — is work a process
        can die in the middle of. What is left then is a `message_map` row that
        every later MAX replay reads as "already delivered", for a message that
        was never sent and now never can be.

        Releasing is safe because both halves of `unsent_claims` have to be
        true: no Telegram id on either side, and nothing in the queue under the
        message's `source_key`. Every MAX→TG branch writes its job before its
        first remote call — including the two that only record a refusal — so a
        row matching both has provably reached no sender.

        Bounded to the run that just ended. Older rows predate this recovery and
        are inventory, not accident: their MAX messages are far outside any
        catch-up window, and what to do with them is a decision with the owner's
        name on it rather than a sweep's.
        """
        if since_ms is None:
            return 0
        stranded = await messages.unsent_claims(since_ms=since_ms)
        for link in stranded:
            # The expected aliases of an album go with it: they name a canonical
            # row that is about to stop existing, and the replay writes its own.
            await albums.clear_link(link.id)
            await messages.forget(link.id)
            logger.info(
                "released claim #%s (%s, MAX message %s): no delivery job was ever written",
                link.id,
                link.bridge_name,
                link.max_message_id,
            )
        return len(stranded)

    async def _sweep_album_in_telegram(self, payload: dict[str, Any]) -> int | None:
        """This process's collaborators, handed to the sweep that does the work."""
        return await sweep_album_in_telegram(
            payload,
            session=self._owner_session,
            albums=(MediaGroupRepository(self._database) if self._database else None),
        )

    async def _note_intake_suppressed(self) -> None:
        """One owner message handed to the MTProto session, counted durably."""
        health = self._health
        if health is not None:
            await health.note_owner_intake_suppressed()

    async def _notify_lifecycle(self, alert: dict[str, Any]) -> None:
        """One queued alert, handed to the layer that decides whether to speak.

        Raises on a transport failure, so the alert queue's retry and give-up
        behaviour is untouched — the queue is the durable part, and this is only
        whether anything reaches the chat.

        There used to be a `_notify_owner` beside this that put the incident's
        own text into the guardian chat directly, one message per raise. It had
        no callers left once the notification layer existed, and a proactive
        `send_message` with no caller is the next cascade waiting for somebody
        to wire it back up.
        """
        guardian = self._guardian
        if guardian is None:
            raise RuntimeError("the guardian bot is not running")
        await self._notification_centre(guardian).publish(alert)

    def _notification_centre(self, guardian: Guardian) -> NotificationCentre:
        """Built once and kept.

        A centre rebuilt per alert would retire the previous design's messages
        on every single alert rather than once, and every rebuild is another
        chance for somebody to give it different collaborators than the one that
        is holding the push.
        """
        if self._notifications is not None:
            return self._notifications
        chat_id = self._loaded.app.telegram.owner_user_id
        store = self._state

        async def send(text: str, markup: Any) -> int | None:
            message = await guardian.bot.send_message(
                chat_id=chat_id, text=text, reply_markup=markup
            )
            found = getattr(message, "message_id", None)
            return int(found) if found is not None else None

        async def edit(message_id: int, text: str, markup: Any) -> bool:
            try:
                await guardian.bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup
                )
            except Exception as error:  # noqa: BLE001 - too old, deleted, unchanged
                if "not modified" in str(error).lower():
                    # The condition has not moved since the last pass. Nothing
                    # to say, and nothing failed.
                    return True
                logger.debug("could not edit a notification; posting a new one")
                return False
            return True

        def write(mapping: dict[str, int]) -> None:
            store.update(notifications=mapping)

        self._notifications = NotificationCentre(
            send=send,
            edit=edit,
            read=lambda: dict(store.load().notifications),
            write=write,
            facts=self.attention_facts,
        )
        return self._notifications

    async def _drain_owner_inbox(self) -> float | None:
        """Finish what is due, and say when the next row wants attention.

        Inside the owner session's own loop rather than beside it: a second
        supervised task would be a second thing to keep alive, and this has
        nothing to do while that session is down that it could not do a moment
        later.

        Refuses to claim while the database cannot be written. A row taken then
        would be a row leased into a transaction nothing will commit, and the
        gate that stops the ingress must stop this too.
        """
        database = self._database
        dispatch = self._owner_updates
        if database is None or dispatch is None:
            return None
        if database.poisoned is not None:
            return None  # the canary owns recovery; wake again on the next tick

        try:
            done = await dispatch.drain()
        except Exception:
            logger.exception("draining the owner inbox failed")
            return None
        if done:
            logger.info("finished %d owner update(s) from the inbox", done)

        due = await OwnerUpdateInboxRepository(database).next_due_ms()
        if due is None:
            return None
        from bridge.storage.database import now_ms

        return float(max(0.0, (due - now_ms()) / 1000))

    async def _baseline_owner_messages(self, client: Any) -> None:
        """Give every mapped owner message a baseline, once per process.

        Everything derived from an `UpdateEditMessage` is a subtraction against
        the stored state, and a message with no state has nothing to subtract
        from. Seeding it from the message itself is the only reading that is not
        a guess — and the guess it replaces would be wrong in exactly the case
        that matters, a reaction put on yesterday whose first update after this
        release is its removal.

        Reads Telegram, writes rows, produces no effect: no MAX call, no job.
        Once per process because a reconnect changes nothing about a baseline,
        and the seed refuses to overwrite anything a live update has written.
        """
        # Whatever was written down and never finished, before anything new is
        # read. Telethon will not hand those updates back, so this is the only
        # thing that finishes them.
        if self._owner_updates is not None:
            with contextlib.suppress(Exception):
                resumed = await self._owner_updates.drain()
                if resumed:
                    logger.info("resumed %d owner update(s) from the inbox", resumed)

        if self._owner_baseline_done:
            return
        self._owner_baseline_done = True
        database = self._database
        if database is None:
            return
        from bridge.telegram.owner_bootstrap import bootstrap_owner_state

        if self._owner_updates is None:
            return
        await bootstrap_owner_state(
            client=client,
            mappings=MessageMapRepository(database),
            state=self._owner_updates,
            shown=ReactionStateRepository(database),
            account_id=self._loaded.app.telegram.owner_user_id,
        )

    async def _note_owner_message_seen(self, bot_id: int, telegram_message_id: int) -> None:
        """The contact bot saw the owner's own message. Record its id, nothing else."""
        if self._owner_binding is not None:
            await self._owner_binding.observe(
                bot_id=bot_id, telegram_message_id=telegram_message_id
            )

    def _owner_reaction_lines(self) -> list[str]:
        """What could not be carried, when there is anything to say.

        Counted rather than raised: a reaction on a message the owner's session
        cannot name is the known limit of the echo binding, not a fault, and one
        incident per unbindable sticker would be noise the owner learns to
        ignore. Silence when every counter is zero.
        """
        dispatch = self._owner_updates
        if dispatch is None:
            return []
        counts = dispatch.counts
        lines: list[str] = []
        if counts.unresolved_reaction:
            lines.append(f"Реакций мимо  {counts.unresolved_reaction} — сообщение без привязки")
        if counts.without_baseline:
            lines.append(f"Без базы      {counts.without_baseline} — реакция не выведена")
        if counts.custom_emoji:
            lines.append(f"Свои эмодзи   {counts.custom_emoji} — в MAX не показать")
        return lines

    def _owner_session_facts(self) -> OwnerSessionFacts:
        """What the runtime knows about the puppet session. No opinion attached.

        The opinion — whether the owner ingress is ready — belongs to
        `read_owner_ingress`, in one place, so `/status`, the incident and any
        future reader cannot drift into three different answers.
        """
        session = self._owner_session
        if session is None:
            return OwnerSessionFacts()
        status = getattr(session, "status", None)
        connected = bool(getattr(session, "is_connected", False))
        if connected:
            # Latched, never cleared: "it has worked at some point in this
            # process" is what separates a first start still coming up from a
            # session that dropped. Health has the durable version of the same
            # fact; this is the one a synchronous reader can have.
            self._owner_session_connected_before = True
        unhealthy = {task.name for task in self._supervisor.unhealthy}
        return OwnerSessionFacts(
            started=True,
            status=str(status.value) if status is not None else None,
            connected=connected,
            supervisor_healthy="telegram-user-session" not in unhealthy,
        )

    @property
    def owner_ingress_ready(self) -> bool:
        """The one invariant: configured and authorized and connected and supervised.

        True of one transport, not of the bridge. Contact bots keep delivering
        MAX→Telegram while this is false, and Guardian keeps answering — which is
        where the session gets authorised again.
        """
        from bridge.service.health import read_owner_ingress

        if self._database is not None and self._database.poisoned is not None:
            # Fail closed. There is no owner inbox before V18, so an update
            # accepted while the first durable write cannot happen is an update
            # lost — and reporting it as carried would be the worse half of that.
            return False
        return read_owner_ingress(
            self._owner_session_facts(), connected_before=self._owner_session_connected_before
        ).ready

    async def _refetch_mtproto_media(
        self, stack: AsyncExitStack, refs: list[dict[str, Any]]
    ) -> list[tuple[str, Path, str]]:
        """Re-download a media job's parts over the owner's MTProto session.

        The reference carries no bytes and no expiring URL — only the owner
        account, the peer and the owner-side message id — so the file is fetched
        fresh each attempt. A deleted or unreachable source is an honest error
        the existing retry/FAILED policy handles, never a masked success.
        """
        source = self._mtproto_media
        if source is None:
            raise PermanentDeliveryError("no MTProto session to re-fetch this media from")
        refetched: list[tuple[str, Path, str]] = []
        for ref in refs:
            refetched.append(await source.fetch(stack, ref))
        if not refetched:
            raise PermanentDeliveryError("nothing could be re-fetched for this upload")
        return refetched

    async def _refetch_media(
        self,
        stack: AsyncExitStack,
        payload: dict[str, Any],
        sources: list[Any],
        media: MediaPipeline,
    ) -> list[tuple[str, Path, str]]:
        """Download a job's attachments again from their Telegram file ids.

        The bot is looked up by the job's own bridge rather than remembered, so
        a bridge rebuilt with a new token still retries correctly.
        """
        registry = self._registry
        if registry is None:
            raise PermanentDeliveryError("no Telegram registry to re-fetch from")
        link_bot = payload.get("bot_id")
        live = registry.by_bot_id(int(link_bot)) if link_bot else None
        if live is None:
            # Fall back to the bridge that owns the MAX chat in the payload.
            live = registry.by_max_chat(int(payload["max_chat_id"]))
        if live is None:
            raise PermanentDeliveryError("the bridge this upload belongs to is no longer running")

        bot = live.bot
        refetched: list[tuple[str, Path, str]] = []
        for kind, file_id, name in sources:
            file = await bot.get_file(file_id)
            url = f"https://api.telegram.org/file/bot{bot.token}/{file.file_path}"
            local = await stack.enter_async_context(media.fetch_from_url(url, file_name=name))
            refetched.append((kind, local.path, name))
        if not refetched:
            raise PermanentDeliveryError("nothing could be re-fetched for this upload")
        return refetched

    @staticmethod
    def _retry_delivery(
        send_job: Callable[[str, Direction, dict[str, Any], SendingHook], Awaitable[int | None]],
    ) -> Callable[[str, Direction, dict[str, Any], SendingHook], Awaitable[int | None]]:
        """Adapt the sender to what `OutboxWorker` expects.

        The worker treats a clean return as success, so an unconfirmed send has
        to keep raising here — otherwise the ambiguity would be flattened into a
        delivery that nobody can vouch for.
        """

        async def deliver(
            kind: str, direction: Direction, payload: dict[str, Any], sending: SendingHook
        ) -> int | None:
            return await send_job(kind, direction, payload, sending)

        return deliver

    def _sweep_outbox(self, outbox: OutboxRepository) -> Callable[[], Awaitable[None]]:
        """Return leases whose worker died, and retire jobs past their TTL.

        A lease that never comes back is a message nobody is carrying, and this
        is what notices. It runs while the process is up rather than only at
        startup: a worker can hang on a socket without the process dying.
        """

        async def loop() -> None:
            while True:
                await asyncio.sleep(OUTBOX_SWEEP_SECONDS)
                try:
                    reclaimed, unresolved = await outbox.reclaim_expired_leases()
                    expired = await outbox.expire_overdue()
                except Exception:
                    logger.exception("outbox sweep failed")
                    continue
                if unresolved:
                    logger.warning(
                        "%s job(s) died mid-send; marked ambiguous rather than resent",
                        unresolved,
                    )
                if reclaimed:
                    logger.warning("returned %s job(s) whose lease had expired", reclaimed)
                    for worker in self._workers.workers if self._workers else ():
                        worker.wake()
                if expired:
                    logger.warning("%s job(s) passed their TTL undelivered", expired)
                # `pending_ttl_days` was a config key with no caller: the buffer
                # for a contact nobody answered about grew for ever, and
                # production was holding messages older than the seven days the
                # setting claims. Dropping them creates nothing and deletes
                # nothing remote — it forgets a message the owner never asked to
                # keep.
                contacts = self._contacts
                if contacts is not None:
                    try:
                        dropped = await contacts.expire_buffered()
                    except Exception:
                        logger.debug("could not expire buffered messages", exc_info=True)
                    else:
                        if dropped:
                            logger.info("dropped %s buffered message(s) past their TTL", dropped)

        return loop

    def _make_backfill(
        self, deliver: Callable[[IncomingMaxMessage], Awaitable[None]]
    ) -> Callable[[], Awaitable[None]]:
        """Re-read each bridged chat's tail and push it through the same dedup.

        Duplicates are free: `claim_from_max` refuses anything already
        delivered, so a backfill after a five-second blip does nothing at all.

        Free *because the bot is the same*. The dedup key is
        `(max_chat_id, max_message_id, telegram_bot_id)`, so a contact given a
        different bot has an unclaimed tail again and the last fifty messages
        would arrive a second time, in the new chat. `history_floor` is the
        watermark under which nothing is ever carried — written once, by the
        thing that changed the bot.
        """

        async def run() -> None:
            registry, client = self._registry, self._max
            rows = self._bridge_rows
            if registry is None or client is None:
                return
            floors: dict[int, int] = {}
            if rows is not None:
                try:
                    floors = {
                        record.max_chat_id: record.history_floor
                        for record in await rows.all()
                        if record.history_floor
                    }
                except Exception:
                    logger.debug("could not read the history floors", exc_info=True)
            for live in registry.live:
                try:
                    history = await client.fetch_history(live.max_chat_id)
                except Exception:
                    logger.exception("could not read history of %s", live.name)
                    continue
                floor = floors.get(live.max_chat_id, 0)
                for message in history:
                    if floor and message.message_id <= floor:
                        continue
                    await deliver(message)

        return run

    def _make_chat_backfill(
        self, deliver: Callable[[IncomingMaxMessage], Awaitable[None]]
    ) -> Callable[[int, int], Awaitable[int]]:
        """Pull one chat's existing conversation into a bridge that just appeared.

        Same dedup as every other delivery, so asking twice costs nothing and
        changes nothing.
        """

        async def run(max_chat_id: int, limit: int) -> int:
            client = self._max
            if client is None:
                return 0
            history = await client.fetch_history(max_chat_id, limit=limit)
            for message in history:
                await deliver(message)
            return len(history)

        return run

    def _make_profile_sync(
        self, profiles: BotProfileSync, bridges: BridgeRepository
    ) -> Callable[[str, int, int], Awaitable[None]]:
        """Give one bot the contact's name and face. Never raises.

        Best-effort by design: a bridge carrying messages with a stale avatar is
        working, and a bridge that refused to start because Telegram rate
        limited a rename is not.
        """

        async def dress(name: str, bot_id: int, contact_id: int) -> None:
            try:
                contact = await profiles.apply(
                    bot_id=bot_id,
                    max_user_id=contact_id,
                    known_signature=await bridges.profile_signature(name),
                )
            except Exception:
                logger.debug("could not sync the profile of bot %s", bot_id, exc_info=True)
                return
            if contact is not None:
                await bridges.set_profile_signature(
                    name, signature_of(contact, about=profiles.about())
                )

        return dress

    async def _reconcile_provisioning(self, bridges: BridgeRepository) -> None:
        """Finish what the last run started, without waiting to be asked.

        The journal recorded every step and nothing read it: a process that died
        between creating a bot and writing its token down left the bot in
        Telegram and no way back that did not begin with the owner pressing a
        button again, for a failure they were never told about.

        Never fatal. A start-up that refuses to serve four working bridges
        because it could not resume a fifth has the priorities backwards.
        """
        flow = self._flow
        if flow is None:
            return
        from bridge.provisioning.reconcile import ProvisioningReconciler

        try:
            report = await ProvisioningReconciler(
                journal=ProvisioningJournal.for_data_dir(self._loaded.app.paths.data_dir),
                provisioner=self._provisioner_port,
                gateway=flow.gateway,
                coordinator=self._coordinator,
                display_name_of=flow.display_name_of,
                own_user_id=self._max.own_user_id if self._max else None,
                incident=self._provisioning_stuck,
            ).run()
        except Exception:
            logger.exception("could not reconcile unfinished provisioning")
            return

        report.unfinished_rows = [
            record.bridge_name
            for record in await bridges.all()
            if record.state is BridgeState.PROVISIONING
        ]
        self._provisioning_report = report
        if report.outcomes or report.unfinished_rows:
            logger.info(
                "provisioning reconciliation: %s resumed, %s awaiting the owner,"
                " %s row(s) still provisioning",
                report.resumed,
                report.awaiting_owner,
                len(report.unfinished_rows),
            )

    async def _provisioning_lines(self) -> list[str]:
        """What `/status` says about bridges that are being made, or stuck.

        Provisioning used to be invisible here entirely: a bridge that never came
        up, an attempt nobody finished and a contact nobody answered about were
        all states the owner could only find in the journal.
        """
        lines: list[str] = []
        rows = self._bridge_rows
        if rows is not None:
            try:
                everything = await rows.all()
            except Exception:
                logger.debug("could not count bridges", exc_info=True)
                everything = []
            if everything:
                started = len(self._registry.live) if self._registry else 0
                disabled = sum(
                    1 for item in everything if item.state is BridgeState.DISABLED
                )
                provisioning = sum(
                    1 for item in everything if item.state is BridgeState.PROVISIONING
                )
                lines.append(
                    f"Реестр     {len(everything)} всего · {started} работают"
                    f" · {disabled} отключены · {provisioning} создаются"
                )

        journal = ProvisioningJournal.for_data_dir(self._loaded.app.paths.data_dir)
        try:
            unfinished = journal.refresh() and journal.unfinished
        except Exception:
            logger.debug("could not read the provisioning journal", exc_info=True)
            unfinished = []
        if unfinished:
            oldest = min(entry.updated_at or entry.started_at or 0 for entry in unfinished)
            age = f" · старшей {(int(time.time()) - oldest) // 60} мин" if oldest else ""
            lines.append(f"Незавершённых мостов {len(unfinished)}{age} — /attempts")

        contacts = self._contacts
        if contacts is not None:
            try:
                waiting = await contacts.awaiting_decision()
            except Exception:
                logger.debug("could not count pending contacts", exc_info=True)
                waiting = 0
            if waiting:
                lines.append(f"Ждут решения {waiting} контакт(ов)")
        return lines

    async def _resume_attempt(self, max_chat_id: int) -> str | None:
        """Push one interrupted attempt on, from `/provretry`.

        The same reconciliation start-up runs, for one contact — not a second
        implementation of it, and through the same coordinator, so a tap while
        it is already running waits rather than races.
        """
        flow = self._flow
        if flow is None:
            return None
        from bridge.provisioning.reconcile import ProvisioningReconciler

        journal = ProvisioningJournal.for_data_dir(self._loaded.app.paths.data_dir)
        entry = journal.get(max_chat_id)
        if entry is None:
            return None
        report = await ProvisioningReconciler(
            journal=journal,
            provisioner=self._provisioner_port,
            gateway=flow.gateway,
            coordinator=self._coordinator,
            display_name_of=flow.display_name_of,
            own_user_id=self._max.own_user_id if self._max else None,
        ).run()
        for outcome in report.outcomes:
            if outcome.max_chat_id == max_chat_id:
                return (
                    f"@{outcome.expected_username} · {outcome.state} · {outcome.verdict.value}"
                )
        return None

    async def _provisioning_stuck(self, outcome: Any) -> None:
        """One incident per stuck attempt. Ids and usernames only, never a token."""
        if self._alerts is None:
            return
        with contextlib.suppress(Exception):
            await self._alerts.open_incident(
                incident_key=f"{PROVISIONING_STUCK}:{outcome.max_chat_id}",
                text=(
                    f"Создание моста не завершено: @{outcome.expected_username}"
                    f" · шаг {outcome.state} · {outcome.verdict.value}"
                    + (f" · бот {outcome.telegram_bot_id}" if outcome.telegram_bot_id else "")
                ),
                cooldown_ms=ALERT_COOLDOWN_MS,
            )

    async def _bridge_not_started(self, name: str, error: BaseException) -> None:
        """Say out loud that a bridge in the register did not come up.

        This was a `logger.error` and nothing else: the health snapshot counts
        the bridges that *are* running and has no idea how many there should be,
        so a contact whose bot had a revoked token simply stopped existing as far
        as every screen the owner looks at was concerned.

        The text carries the bridge name and whatever the registry said, which is
        ids and usernames. Never the token — the token is the thing that is
        wrong, and it is exactly what must not be written down.
        """
        if self._alerts is None:
            return
        with contextlib.suppress(Exception):
            await self._alerts.open_incident(
                incident_key=f"{BRIDGE_NOT_STARTED}:{name}",
                text=f"Мост «{name}» не поднялся: {redact(str(error))[:200]}",
                cooldown_ms=ALERT_COOLDOWN_MS,
            )

    async def _bridge_started(self, name: str) -> None:
        if self._alerts is None:
            return
        with contextlib.suppress(Exception):
            await self._alerts.resolve_incident(
                incident_key=f"{BRIDGE_NOT_STARTED}:{name}",
                text=f"Мост «{name}» снова работает.",
            )

    async def _restore_bridges(self, bridges: BridgeRepository) -> list[ResolvedBridge]:
        """Bridges provisioned at runtime, read back from the database.

        Their tokens are in the 0600 secrets file the loader already sourced, so
        this only has to name them. A row whose token has gone is reported and
        skipped rather than crashing the worker: one broken bridge must not take
        the others down.
        """
        from pydantic import SecretStr

        seeded = {bridge.max_chat_id for bridge in self._loaded.bridges}
        restored: list[ResolvedBridge] = []
        for record in await bridges.active():
            if record.max_chat_id in seeded:
                continue
            token = os.environ.get(record.token_env, "").strip()
            if not token:
                logger.error(
                    "bridge %s: $%s holds no token; it will not come up",
                    record.bridge_name,
                    record.token_env,
                )
                continue
            restored.append(
                ResolvedBridge(
                    name=record.bridge_name,
                    max_chat_id=record.max_chat_id,
                    token_env=record.token_env,
                    token=SecretStr(token),
                    source=BridgeSource.MTPROTO,
                )
            )
        if restored:
            logger.info("restored %s bridge(s) from the database", len(restored))
        return restored

    def _build_dialog_flow(
        self,
        app: AppConfig,
        bridges: BridgeRepository,
        picker: DialogPicker | None,
        deliver: Callable[[IncomingMaxMessage], Awaitable[None]],
    ) -> None:
        """Assemble the dialog picker's brain, if this deployment can have one.

        Needs both halves to exist: MAX, for the list of people, and a user
        session, for the ability to own bots at all. Without either, `/dialogs`
        says so rather than drawing a list nothing can act on.
        """
        if picker is None or self._registry is None or self._max is None:
            return
        provisioner = self._bot_provisioner(app)
        if provisioner is None:
            return

        from bridge.provisioning.profile import build_bot_name

        gateway = LiveBridgeGateway(
            registry=self._registry,
            bridges=bridges,
            secrets=self._secrets,
            contact_of_chat=self._max.contact_of_chat,
            dress_bot=self._dress_bot,
            data_dir=app.paths.data_dir,
        )
        self._flow = DialogFlow(
            picker=picker,
            provisioner=provisioner,
            gateway=gateway,
            journal=ProvisioningJournal.for_data_dir(app.paths.data_dir),
            bridges=bridges,
            # Half of every new contact bot's username; the contact's MAX id is
            # the other half. No secret and no installation identity, so the
            # same two accounts compute the same names on any machine.
            telegram_owner_user_id=app.telegram.owner_user_id,
            history_source=MaxHistorySource(client=self._max, deliver=deliver),
            # The same MAX session, for the one question the picker cannot
            # answer: who owns this phone number. Passed explicitly rather than
            # reached through the picker — the lookup writes nothing, and the
            # import next to it writes, so the boundary is worth naming.
            directory=self._max,
            display_name_of=lambda entry: build_bot_name(entry.title or f"MAX {entry.max_chat_id}"),
            coordinator=self._coordinator,
        )
        if self._guardian_context is not None:
            self._guardian_context.flow = self._flow

    def _own_media(
        self, app: AppConfig, media: MediaPipeline, echoes: OwnEchoes
    ) -> MaxMediaDelivery:
        """The same media pipeline, uploading as the owner over their own session.

        `MaxMediaDelivery` is reused whole and only the sender underneath differs,
        so the album rule, the caption split, the per-attachment placeholders and
        the upright cover all behave exactly as they do for the contact bot.

        Built unconditionally: the session is opened later than the router, so
        availability is a question asked at send time, not at wiring time.
        """
        return MaxMediaDelivery(
            pipeline=media,
            sender=cast("Any", MtprotoOwnerSender(session=lambda: self._owner_session)),
            sticker_origins=(StickerOriginRepository(self._database) if self._database else None),
        )

    def _own_voice(self, app: AppConfig) -> OwnerVoice:
        """The owner's own account, writing the owner's own words.

        This replaced the guardian's business connection, and the reason is the
        list of things that had to be true for that to work: Premium, Business
        switched on, a connection granted to the guardian, `can_reply` still set,
        and none of it revoked since. The session needs one thing — to be
        connected — and when it is not, the caller falls back to the `Вы: …` bot
        line exactly as it did before.

        The session is resolved on every call rather than captured: it is opened
        after the router is built, and a reconnect replaces the client.
        """
        return OwnerVoice(session=lambda: self._owner_session)

    def _manager_username(self, app: AppConfig) -> str | None:
        """What the guardian bot is called, for every link that names it.

        `getMe` first, the environment second. The variable is written by setup
        and can be stale, empty, or another install's; an empty one produced
        `t.me/newbot//<name>` and a card with no button, both silently.
        """
        guardian = self._guardian
        if guardian is not None and guardian.username:
            return guardian.username
        variable = app.provisioning.guardian_bot_token_env
        if not variable:
            return None
        return os.environ.get(f"{variable}_USERNAME", "").strip() or None

    def _bot_provisioner(self, app: AppConfig) -> Any:
        """How contact bots come into existence, and what owns them.

        The owner's session first, Managed Bots when there is no session.

        Managed Bots avoids an account credential and can reuse an existing bot,
        but it still depends on Telegram's interactive Create confirmation. The
        user session is therefore preferred when it is available.

        `MtprotoProvisioner` also paces `/newbot` walks: one walk
        at a time, twenty seconds between them, and an hour of silence the first
        time @BotFather says it has had enough.

        What is *lost* by not creating through the manager is small and worth
        naming: `getManagedBotToken` only answers for bots the manager itself
        created, so a bot made this way has no token recovery through Bot API.
        Its token is written down at creation, which is the same guarantee the
        five existing bridges have, and @BotFather's own `/token` is the way
        back if it is ever lost.
        """
        if self._provisioner_port is not None:
            return self._provisioner_port

        guardian = self._guardian
        if guardian is None:
            return None
        owned = self._owned_bots(app, guardian)
        if owned is None:
            return None

        session = self._botfather_session(app)
        if session is not None:
            self._provisioner_port = MtprotoProvisioner(session)
            return self._provisioner_port

        self._provisioner_port = ManagedBotProvisioner(
            manager=guardian.bot,
            # From the bot itself, with the environment as a checked cache. The
            # variable is written by setup and can be stale, empty, or from
            # another install — and an empty one produced `t.me/newbot//<name>`,
            # a link that opens nothing, with no error anywhere.
            manager_username=self._manager_username(app) or "",
            owned=owned,
        )
        return self._provisioner_port

    def _botfather_session(self, app: AppConfig) -> Any:
        """@BotFather over the intake connection, or None when there is none.

        Borrowed, never opened. Intake already holds an authorised client for
        this account, and a second Telethon connection on the same session file
        is how the account's keys get revoked — the same reason `AccountBots`
        takes a provider rather than a client.

        None when the owner's session is not configured at all, which is what
        keeps a credential-free install working on Managed Bots.
        """
        if not app.provisioning.use_owner_session:
            return None
        credentials = (
            os.environ.get(app.provisioning.mtproto_api_id_env, "").strip(),
            os.environ.get(app.provisioning.mtproto_api_hash_env, "").strip(),
        )
        if not all(credentials):
            logger.info(
                "no owner session configured; contact bots will be created "
                "through Telegram's managed-bots dialog"
            )
            return None
        from bridge.provisioning.mtproto import BorrowedBotFatherSession

        return BorrowedBotFatherSession(
            lambda: self._owner_session.client if self._owner_session else None,
            secrets_dir=app.paths.resolved_secrets_dir,
        )

    def _owned_bots(self, app: AppConfig, guardian: Guardian) -> Any:
        """Where the five non-creation answers come from.

        `managed` answers them over Bot API and keeps no account credential on
        disk; `auto_mtproto` keeps the user session, which reads the *whole*
        account rather than only what this install made — worth having while
        debugging, not worth a full account credential in production.
        """
        if app.provisioning.mode is ProvisioningMode.MANAGED:
            if self._database is None:
                return None
            bridges = BridgeRepository(self._database)
            from bridge.provisioning.mtproto import AccountBots

            return BotApiOwnedBots(
                manager=guardian.bot,
                # Every row, not only the running ones: a disabled bridge's bot
                # is still ours and still holds a slot.
                known=RepositoryKnownBots(bridges.all),
                assumed_limit=app.provisioning.assumed_bot_limit,
                # The owner's intake session, borrowed for the two questions Bot
                # API cannot answer: how many bots the account owns and how many
                # it may. Borrowed, not opened — a second connection on the same
                # session file is how an account gets its keys revoked. Through a
                # provider so a reconnect is picked up, and `None` while intake is
                # off or still coming up, which every caller handles.
                account=AccountBots(
                    lambda: self._owner_session.client if self._owner_session else None
                ),
                guardian=OwnedBot(
                    bot_id=guardian.bot_id,
                    username=os.environ.get(
                        f"{app.provisioning.guardian_bot_token_env}_USERNAME", ""
                    ).strip().lower()
                    or None,
                    name="Telemax",
                ),
            )
        return self._user_session(app)

    def _user_session(self, app: AppConfig) -> LazyOwnedBots | None:
        """One lazily-opened Telegram user session, or None when unconfigured."""
        if self._owned is not None:
            return self._owned
        if app.provisioning.mode is not ProvisioningMode.AUTO_MTPROTO:
            return None

        api_id = os.environ.get(app.provisioning.mtproto_api_id_env, "").strip()
        api_hash = os.environ.get(app.provisioning.mtproto_api_hash_env, "").strip()
        phone = os.environ.get(app.provisioning.mtproto_phone_env, "").strip()
        if not (api_id.isdigit() and api_hash and phone):
            logger.warning(
                "provisioning.mode=auto_mtproto but $%s/$%s/$%s are not all set; "
                "capacity cannot be read and bots cannot be created",
                app.provisioning.mtproto_api_id_env,
                app.provisioning.mtproto_api_hash_env,
                app.provisioning.mtproto_phone_env,
            )
            return None

        async def connect() -> Any:
            from bridge.provisioning import mtproto

            return await mtproto.connect(
                api_id=int(api_id),
                api_hash=api_hash,
                phone=phone,
                secrets_dir=app.paths.resolved_secrets_dir,
            )

        self._owned = LazyOwnedBots(connect)
        return self._owned

    async def _maybe_start_owner_session(self, app: AppConfig, media: MediaPipeline) -> None:
        """Open and supervise the owner's MTProto intake session — if enabled.

        Gated on the *stable* intake flag, never on anything transient. In Stage
        1 the flag is off, so this returns immediately and Bot API stays the
        authoritative intake. When it is on, a wrong account or an unauthorised
        session leaves intake closed rather than reading a stranger's chats — the
        session must be linked by `telegram-sync` first.

        Wired to the existing router and media pipeline: owner updates become
        ordinary TG→MAX jobs, and a media retry re-fetches over this same session.
        """
        if self._database is None or self._router is None:
            return
        from bridge.telegram.mtproto_intake import MtprotoIntake
        from bridge.telegram.mtproto_media import MtprotoMediaSource
        from bridge.telegram.user_session import (
            OwnerMismatchError,
            TelegramUserSession,
            contact_bot_allowlist,
        )

        assert self._health is not None
        bridges = self._bridge_rows or BridgeRepository(self._database)
        allow = contact_bot_allowlist(bridges)
        assert self._reactions is not None
        self._owner_updates = OwnerUpdateDispatch(
            state=OwnerMessageStateRepository(self._database),
            messages=MessageMapRepository(self._database),
            edits=self._router,
            reactions=self._reactions,
            inbox=OwnerUpdateInboxRepository(self._database),
        )
        intake = MtprotoIntake(
            router=self._router,
            allowed_bots=allow,
            # Album parts land in the same table the Bot API collector uses; the
            # two populations are scoped apart by the repository, not by a copy.
            album_parts=MediaGroupRepository(self._database),
            read_marker=self._auto_read,
            updates=self._owner_updates,
        )
        session = TelegramUserSession(
            connect=lambda: self._connect_owner_session(app),
            owner_user_id=app.telegram.owner_user_id,
            health=self._health,
            allowed_bot_ids=allow,
            intake=intake,
            forward_authors=self._forward_authors,
            on_connected=self._baseline_owner_messages,
            on_tick=self._drain_owner_inbox,
            timestamp_style=app.telegram.message_timestamp,
            timezone=_timezone_of(app),
        )
        # Held before the first attempt, and kept whatever that attempt makes of
        # it. The transport is what the gate is waiting for, so a session object
        # that exists and says "retrying" is worth far more than no session at
        # all: `/status` can name the state, and the watchdog below has something
        # to keep trying.
        self._owner_session = session
        self._owner_intake = intake
        # The one place the session downloads bytes: the media worker, by
        # reference, on the first attempt and every retry.
        self._mtproto_media = MtprotoMediaSource(
            client_provider=lambda: self._owner_session.client if self._owner_session else None,
            temp_files=media.temp_files,
        )

        started = False
        try:
            await session.start()
            started = True
        except OwnerMismatchError:
            logger.error("owner MTProto session is not the owner's; intake stays closed")
        except Exception:
            logger.exception("owner MTProto session did not come up; the watchdog will retry")

        # Registered whether or not that worked, which is the whole point. It
        # used to be registered only on success, so a session that failed at boot
        # was never retried for the life of the process — while the gate went on
        # closing the Bot API path against it and the owner's messages simply
        # stopped. The loop returns on its own for a terminal status, and the
        # supervisor reads a clean exit as the decision it is.
        self._supervisor.start("telegram-user-session", session.watch())
        if not started:
            return

        # Albums the last process accepted part by part and never got to carry.
        # Their parts are on disk with their owner-side ids, so what is left is
        # to notice them and let the ordinary quiet window take it from here; a
        # group that already became a message carries its link and is skipped.
        try:
            restored = await intake.restore_albums()
            if restored:
                logger.info("restored %s unfinished owner album(s) from the last run", restored)
            buffered = await intake.restore_echo_albums()
            if buffered:
                logger.info("restored %s album echo(es) waiting to be bound", buffered)
        except Exception:
            logger.exception("could not restore unfinished owner albums")

    async def _connect_owner_session(self, app: AppConfig) -> Any:
        """Open the authorised owner session over MTProto. Never logs the secret.

        Reuses the credentials the rest of the MTProto contour reads from the
        environment, and the session file `telegram-sync` wrote and hardened.
        """
        from bridge.provisioning.mtproto import harden
        from bridge.telegram.user_session import (
            SessionUnauthorizedError,
            SessionUnusableError,
            user_session_path,
        )

        api_id = os.environ.get(app.provisioning.mtproto_api_id_env, "").strip()
        api_hash = os.environ.get(app.provisioning.mtproto_api_hash_env, "").strip()
        if not (api_id.isdigit() and api_hash):
            # Terminal, and named as such: no amount of retrying supplies an API
            # id. Retrying it every five minutes only buries the one line that
            # says what to do.
            raise SessionUnusableError(
                "owner MTProto intake is enabled but api credentials are not set"
            )
        from telethon import TelegramClient  # type: ignore[import-untyped]

        path = user_session_path(app.paths.resolved_secrets_dir)
        client = TelegramClient(str(path.with_suffix("")), int(api_id), api_hash)
        # The constructor already opened the session's sqlite file, so from here
        # every exit but the authorised one has to close it again. A client
        # dropped with its handle still open is the leak that turned one
        # transient «database is locked» into a permanent storm: each
        # five-second retry left another connection behind, and past two they
        # deadlock each other on the write lock for good. `connect()` itself is
        # where that first lock is taken — it rewrites the session table — so the
        # guard has to wrap it, not just the checks after it.
        try:
            await client.connect()
            if not await client.is_user_authorized():
                # Terminal. A revoked or missing session is fixed by the owner
                # scanning a QR, never by another attempt — and telling that
                # apart from "the network is down" is the difference between a
                # status line they can act on and one they learn to ignore.
                raise SessionUnauthorizedError(
                    "owner MTProto session is not authorised; run `telemax telegram-sync`"
                )
            harden(path)
        except BaseException:
            with contextlib.suppress(Exception):
                await client.disconnect()
            raise
        return client

    async def bridges(self) -> list[BridgeSummary]:
        """Every live bridge, for the «Мосты» screen and the cards behind it."""
        if self._flow is None:
            return []
        return await self._flow.summaries()

    async def bridge_card(self, max_chat_id: int) -> tuple[BridgeSummary | None, list[str]]:
        """One bridge: who it is, and the same state its own `/status` reports.

        Falls back to the disabled row on purpose. The screen that explains how
        to delete the bot is reached *after* disconnecting, and it still has to
        name it.
        """
        summary = next(
            (item for item in await self.bridges() if item.max_chat_id == max_chat_id), None
        )
        if summary is None and self._flow is not None:
            summary = await self._flow.summary_of(max_chat_id)
        if summary is None:
            return None, []
        registry = self._registry
        live = registry.by_max_chat(max_chat_id) if registry else None
        if live is None or self._status_of is None:
            return summary, ["Мост не запущен"]
        return summary, (await self._status_of(live.bot.id)).splitlines()

    async def wipe_delivered(self, max_chat_id: int) -> tuple[int, int]:
        """Remove everything this bridge placed in the chat. Returns (gone, kept).

        The order is the whole safety of this operation: a mapping is dropped
        only after its Telegram message is actually deleted. Telegram refuses to
        delete a message older than 48 hours, and a row dropped for a message
        that survived would let the re-import put a *second* copy beside it. So
        what cannot be removed keeps its row, keeps deduplicating, and is
        reported as kept.
        """
        registry, messages = self._registry, self._messages
        rows = self._bridge_rows
        if registry is None or messages is None or rows is None:
            return 0, 0
        live = registry.by_max_chat(max_chat_id)
        record = await rows.by_max_chat(max_chat_id)
        if live is None or record is None:
            return 0, 0

        # The whole chat, through the owner's session, when there is one.
        #
        # This deletes the owner's own messages too, and that is the point. The
        # conversation lives in MAX; the Telegram side is a rendering of it, and
        # a re-pull is a request to render it again. Keeping the owner's half
        # while replacing the contact's produced a chat that was neither the old
        # one nor the new one — and it was defended on the grounds that their
        # messages might not come back, which is only true of a copy nobody
        # treats as the original.
        #
        # A bot could not do this at all: not the owner's messages, and nothing
        # older than forty-eight hours. The owner's account has neither limit.
        if await self._wipe_whole_dialog(record.telegram_bot_id, keep_dialog=True):
            forgotten = 0
            for placed in await messages.placed_by(record.bridge_name):
                await messages.forget(placed.link_id)
                forgotten += 1
            await rows.clear_history_cursor(record.bridge_name)
            # And the delivery keys, or nothing comes back. The outbox refuses a
            # job whose `source_key` it has seen — right for a retry, wrong for
            # a re-pull, which asks for those very messages again. Without this
            # every one returned «already in hand» and the chat stayed empty.
            freed = await self._free_delivery_keys(record.bridge_name, max_chat_id)
            logger.info(
                "emptied the chat of %s: %s mapping(s) dropped, %s delivery key(s) freed",
                record.bridge_name,
                forgotten,
                freed,
            )
            return forgotten, 0

        # No session: the bot does what it can, which is what it placed and
        # nothing older than forty-eight hours.
        by_session = None

        gone = kept = 0
        for placed in await messages.placed_by(record.bridge_name):
            if not placed.ids:
                # Claimed and never delivered: nothing in the chat to remove, and
                # the row is only in the way of a re-import.
                await messages.forget(placed.link_id)
                continue
            if by_session is not None and await by_session(list(placed.ids)):
                removed = [True]
            else:
                removed = [
                    await self._delete_one(live.bot, placed.telegram_chat_id, message_id)
                    for message_id in placed.ids
                ]
            if all(removed):
                await messages.forget(placed.link_id)
                gone += 1
            else:
                kept += 1

        await rows.clear_history_cursor(record.bridge_name)
        logger.info(
            "wiped %s delivered message(s) of %s, %s could not be deleted",
            gone,
            record.bridge_name,
            kept,
        )
        return gone, kept

    async def _free_delivery_keys(self, bridge_name: str, max_chat_id: int) -> int:
        """Let this chat's MAX messages be delivered again. Never fatal."""
        if self._database is None:
            return 0
        forget = OutboxRepository(self._database).forget_settled_from_max
        try:
            freed: int = await forget(bridge_name, max_chat_id)
        except Exception:
            logger.debug("could not free the delivery keys of %s", bridge_name, exc_info=True)
            return 0
        return freed

    async def _wipe_whole_dialog(self, bot_id: int | None, *, keep_dialog: bool = False) -> bool:
        """Empty the chat with this bot entirely. False when nobody can.

        `keep_dialog` for a re-pull: the chat is emptied but not removed.
        Removing it stops the bot — Telegram has no other way to say "this user
        is gone" — and the import that follows would write into a chat that
        refuses it.

        Never fatal: a re-pull that could not clear the chat first still has to
        be able to fall back on the bot rather than refuse outright.
        """
        provisioner = self._provisioner_port
        if bot_id is None or provisioner is None:
            return False
        wipe = getattr(provisioner, "wipe_dialog", None)
        if wipe is None:
            return False
        try:
            await wipe(int(bot_id), keep_dialog=keep_dialog)
        except Exception as error:  # noqa: BLE001 - the bot is the fallback
            logger.info(
                "session could not empty the chat with bot %s: %s", bot_id, type(error).__name__
            )
            return False
        return True

    def _session_deleter(self, bot_id: int | None) -> Any:
        """A callable that deletes message ids over the owner's session, or None.

        None when there is no session, no bot id, or a provisioner that cannot —
        and then the bot does it, forty-eight-hour rule and all.
        """
        provisioner, remove = self._provisioner_port, None
        if bot_id is None or provisioner is None:
            return None
        remove = getattr(provisioner, "delete_messages", None)
        if remove is None:
            return None

        async def delete(message_ids: list[int]) -> bool:
            try:
                await remove(int(bot_id), message_ids)
            except Exception as error:  # noqa: BLE001 - the bot is the fallback
                logger.info(
                    "session could not delete %s message(s): %s",
                    len(message_ids),
                    type(error).__name__,
                )
                return False
            return True

        return delete

    async def _delete_one(self, bot: Any, chat_id: int, message_id: int) -> bool:
        """True when the message is not in the chat any more.

        Two refusals that look alike and mean opposite things:

        * **«message to delete not found»** — it is already gone, because the
          owner deleted it, or the whole conversation, by hand. The mapping has
          to go with it: keeping it is what made a re-import deliver *nothing* to
          a chat the owner had just cleared.
        * **«message can't be deleted»** — it is still there and Telegram will not
          remove it, which is what happens past 48 hours. The mapping stays, and
          the message keeps deduplicating.

        Anything unfamiliar is read as the second, cautious case: a message the
        owner still sees is better than a second copy of one they do not.
        """
        from aiogram.methods import DeleteMessage

        try:
            await bot(DeleteMessage(chat_id=chat_id, message_id=message_id))
        except Exception as error:
            text = str(error).lower()
            if any(marker in text for marker in ALREADY_GONE_MARKERS):
                logger.debug("message %s was already gone", message_id)
                return True
            logger.debug("could not delete message %s", message_id, exc_info=True)
            return False
        return True

    async def repull_history(self, max_chat_id: int, draw: Any) -> tuple[int, int] | None:
        """Wipe what was delivered, then pull the conversation again from scratch.

        None means there is no such bridge. Otherwise `(gone, kept)` — and `kept`
        is the number the owner has to know about: those messages are still in the
        chat above the fresh copy, and they are the ones Telegram would not let
        the bot remove.
        """
        flow = self._flow
        if flow is None:
            return None
        gone, kept = await self.wipe_delivered(max_chat_id)
        await flow.import_one(max_chat_id, draw)
        return gone, kept

    async def disconnect_bridge(self, max_chat_id: int) -> bool:
        """Stop this bridge and mark it disabled. Nothing is deleted.

        Deliberately reversible, and deliberately *not* a deletion: Telegram has
        no way to delete a managed bot, so the bot stays in the account either
        way. Dropping the row as well would only lose the mapping that lets the
        same contact be reconnected to the same bot for free.
        """
        if self._flow is None:
            return False
        if not await self._flow.disconnect(max_chat_id):
            return False
        if self._contacts is not None:
            # Otherwise the contact's next message arrives unbridged and the
            # guardian asks «сделать бота?» about somebody just disconnected.
            with contextlib.suppress(Exception):
                await self._contacts.ignore(max_chat_id)
        return True

    @property
    def can_delete_bots(self) -> bool:
        """Whether the bot behind a bridge can really be deleted from here."""
        return self._flow is not None and self._flow.can_delete_bots

    @property
    def can_wipe_dialogs(self) -> bool:
        """Whether the chat with a bot can be emptied from here."""
        return self._flow is not None and self._flow.can_wipe_dialogs

    async def delete_bot(self, max_chat_id: int) -> str | None:
        """Delete this bridge's Telegram bot for real, freeing its slot.

        The other half of `disconnect_bridge`, and the destructive one. That
        stops a bridge and keeps everything; this ends the bot. Irreversible —
        the confirmation lives in the screen above.
        """
        if self._flow is None:
            return None
        username = await self._flow.delete_bot(max_chat_id)
        if username is not None and self._contacts is not None:
            # Same reason as disconnecting: otherwise the contact's next message
            # arrives unbridged and the guardian asks about somebody the owner
            # has just finished removing.
            with contextlib.suppress(Exception):
                await self._contacts.ignore(max_chat_id)
        return username

    async def tear_down_bridge(self, max_chat_id: int) -> Any:
        """Remove a bridge, its conversation and its bot in one move.

        The destructive twin of `disconnect_bridge`. That one stops and keeps
        everything; this one ends all three and gives the slot back.
        """
        if self._flow is None:
            return None
        outcome = await self._flow.tear_down(max_chat_id)
        if outcome is not None and self._contacts is not None:
            # Same reason as disconnecting: otherwise the contact's next message
            # arrives unbridged and the guardian asks about somebody the owner
            # has just finished removing.
            with contextlib.suppress(Exception):
                await self._contacts.ignore(max_chat_id)
        return outcome

    async def capacity_lines(self) -> list[str]:
        """Bot counts for the home screen — from the picker's own snapshot.

        This used to count bots itself, on its own schedule, and that is the
        whole of the bug where the owner was shown «свободно 39» beside a run
        that had just been refused. One reader, one number, one timestamp.
        """
        from bridge.provisioning.selection import capacity_lines as render

        flow = self._flow
        if flow is None:
            return []
        capacity = await flow.capacity_snapshot()
        return render(capacity, selection=False) if capacity is not None else []

    def health(self) -> ServiceHealth:
        """What the home screen's glyph is decided from, as facts not sentences."""
        registry = self._registry
        return ServiceHealth(
            max_connected=bool(self._max and self._max.is_ready),
            degraded=bool(self._supervisor.unhealthy),
            bridges=len(registry.live) if registry else 0,
        )

    async def attention_facts(self) -> Any:
        """Everything the home verdict is decided from, in one read.

        Read-only and entirely from providers that already existed: the health
        snapshot, the bridge register, the provisioning journal. The home screen
        used to choose its glyph from `max_connected` and "is a background task
        dead" alone, so an install with four dead bridges, three failed sends and
        a stuck provisioning attempt could say «🟢 Всё работает» and mean it.
        """
        from bridge.onboarding.views import AttentionFacts

        registry = self._registry
        rows = self._bridge_rows
        broken = provisioning = disabled = 0
        if rows is not None:
            try:
                everything = await rows.all()
            except Exception:
                logger.debug("could not read the bridge rows", exc_info=True)
                everything = []
            for record in everything:
                if record.state is BridgeState.DISABLED:
                    disabled += 1
                elif record.state is BridgeState.PROVISIONING:
                    provisioning += 1
                elif registry is None or registry.by_max_chat(record.max_chat_id) is None:
                    # The row says active and nothing is polling it. That is the
                    # `bridge-not-started` incident, seen from the read side.
                    broken += 1

        snapshot = None
        health = self._health
        if health is not None:
            try:
                snapshot = await health.snapshot()
            except Exception:
                logger.debug("could not read the health snapshot", exc_info=True)

        unfinished = 0
        try:
            journal = ProvisioningJournal.for_data_dir(self._loaded.app.paths.data_dir)
            unfinished = len(journal.unfinished) if journal.refresh() else 0
        except Exception:
            logger.debug("could not read the provisioning journal", exc_info=True)

        facts = AttentionFacts(
            worker_running=True,
            max_connected=bool(self._max and self._max.is_ready),
            degraded=bool(self._supervisor.unhealthy),
            bridges=len(registry.live) if registry else 0,
            bridges_broken=broken,
            bridges_provisioning=provisioning,
            bridges_disabled=disabled,
            provisioning_unfinished=unfinished,
        )
        if snapshot is None:
            return facts
        return replace(
            facts,
            database_healthy=snapshot.database_write.healthy,
            owner_ingress_ready=snapshot.owner_ingress.ready,
            queued=snapshot.outbox_pending + snapshot.outbox_leased,
            failed=snapshot.outbox_failed,
            ambiguous=snapshot.outbox_ambiguous,
            expired=snapshot.outbox_expired,
            oldest_pending_ms=snapshot.oldest_pending_ms,
            max_offline_ms=snapshot.max_offline_ms,
        )

    async def attention_jobs(self) -> list[Any]:
        """Every delivery job waiting on a decision, named by the contact.

        The queue keys on `bridge_name` and every listing printed it under the
        label «Контакт:». That string is a key, not a person — the owner's only
        handle for a stuck message was something like `p6wyzx5zu7vv6pwcddx5`.
        """
        from bridge.onboarding.views import JobFacts

        outbox = self._outbox
        if outbox is None:
            return []
        titles = {item.bridge_name: item.title for item in await self.bridges()}
        found: list[Any] = []
        for name in titles:
            with contextlib.suppress(Exception):
                for item in await outbox.needing_attention(name):
                    if item.state not in {OutboxState.FAILED, OutboxState.AMBIGUOUS}:
                        # EXPIRED is reported, never acted on: the payload it
                        # would be retried from is gone by definition.
                        continue
                    found.append(
                        JobFacts(
                            job_id=item.id,
                            bridge_name=item.bridge_name,
                            contact=titles.get(item.bridge_name, item.bridge_name),
                            direction=str(item.direction),
                            kind=item.kind,
                            ambiguous=item.state is OutboxState.AMBIGUOUS,
                            created_at=item.created_at,
                        )
                    )
        return found

    async def retry_job(self, job_id: int) -> bool:
        """The owner asked for one more try. `OutboxRepository.retry_now`."""
        outbox = self._outbox
        return bool(outbox is not None and await outbox.retry_now(job_id))

    async def settle_job(self, job_id: int) -> bool:
        """The owner checked MAX and it is there. `OutboxRepository.resolve`."""
        outbox = self._outbox
        return bool(outbox is not None and await outbox.resolve(job_id))

    async def archive_job(self, job_id: int) -> bool:
        """Set aside, not deleted: the row keeps its error and its timestamps."""
        outbox = self._outbox
        if outbox is None:
            return False
        return bool(await outbox.archive(job_id, reason="owner chose not to send"))

    async def unfinished_attempts(self) -> list[Any]:
        """Bridges that were being made and stopped, named by the contact."""
        from bridge.onboarding.views import AttemptFacts

        try:
            journal = ProvisioningJournal.for_data_dir(self._loaded.app.paths.data_dir)
            entries = journal.unfinished if journal.refresh() else []
        except Exception:
            logger.debug("could not read the provisioning journal", exc_info=True)
            return []
        return [
            AttemptFacts(
                max_chat_id=entry.max_chat_id,
                contact=entry.title or entry.expected_username,
                username=entry.expected_username,
                # The sanitised sentence, never the `ItemState` it stopped at:
                # `шаг awaiting_confirmation · timeout` is three enum values.
                reason=entry.error,
            )
            for entry in entries
        ]

    async def abandon_attempt(self, max_chat_id: int) -> bool:
        """Stop asking. Nothing remote is deleted — there is no way to.

        The entry is marked rather than removed because it is the only place the
        bot's username is written down, and a bot that was created stays created.
        """
        from bridge.provisioning.journal import ItemState

        try:
            journal = ProvisioningJournal.for_data_dir(self._loaded.app.paths.data_dir)
            journal.refresh()
            if journal.get(max_chat_id) is None:
                return False
            journal.note(max_chat_id, state=ItemState.ABANDONED)
        except Exception:
            logger.debug("could not abandon a provisioning attempt", exc_info=True)
            return False
        return True

    async def bridge_facts(self) -> list[Any]:
        """Every bridge with the state its list row needs, and nothing rendered.

        `BridgeSummary.running` answers "is the row marked active", which is not
        the same question as "is it carrying messages". Both halves already
        existed — the row and the register — and the list simply never asked the
        second one.
        """
        from bridge.onboarding.views import BridgeFacts, BridgeUiState

        registry = self._registry
        outbox, state = self._outbox, self._bridge_state
        rows = self._bridge_rows
        by_chat = {}
        if rows is not None:
            with contextlib.suppress(Exception):
                by_chat = {record.max_chat_id: record for record in await rows.all()}

        facts: list[Any] = []
        for summary in await self.bridges():
            record = by_chat.get(summary.max_chat_id)
            live = registry.by_max_chat(summary.max_chat_id) if registry else None
            if record is not None and record.state is BridgeState.DISABLED:
                ui = BridgeUiState.DISABLED
            elif record is not None and record.state is BridgeState.PROVISIONING:
                ui = BridgeUiState.PROVISIONING
            elif live is None:
                ui = BridgeUiState.BROKEN
            else:
                ui = BridgeUiState.ACTIVE

            queued = failed = ambiguous = 0
            if outbox is not None:
                with contextlib.suppress(Exception):
                    queued = await outbox.queue_size(summary.bridge_name)
                    counts = await outbox.counts(summary.bridge_name)
                    failed = counts.get(OutboxState.FAILED.value, 0)
                    ambiguous = counts.get(OutboxState.AMBIGUOUS.value, 0)
            snapshot: dict[str, Any] = {}
            if state is not None:
                with contextlib.suppress(Exception):
                    snapshot = await state.snapshot(summary.bridge_name) or {}
            facts.append(
                BridgeFacts(
                    title=summary.title,
                    username=summary.username,
                    bridge_name=summary.bridge_name,
                    max_chat_id=summary.max_chat_id,
                    bot_id=summary.bot_id,
                    state=ui,
                    queued=queued,
                    failed=failed,
                    ambiguous=ambiguous,
                    last_delivery_at=snapshot.get("last_delivery_at"),
                    detail=snapshot.get("last_error"),
                )
            )
        return facts

    def _build_provisioner(self, bridges: BridgeRepository, app: AppConfig) -> Provisioner | None:
        if app.provisioning.mode is ProvisioningMode.OFF or self._database is None:
            return None
        if self._registry is None:
            return None
        return Provisioner(
            config=app,
            bridges=bridges,
            pending=PendingContactRepository(self._database),
            activator=self._registry,
            replayer=BridgeReplayer(self._registry, app.telegram.owner_user_id),
            secrets=self._secrets,
        )

    def _make_unbridged_handler(
        self, provisioner: Provisioner | None
    ) -> Callable[[IncomingMaxMessage], Awaitable[None]] | None:
        if provisioner is None:
            return None

        async def run(message: IncomingMaxMessage) -> None:
            name = None
            if self._max is not None and message.sender_id is not None:
                name = await self._max.display_name(message.sender_id)
            announcement = await provisioner.on_unbridged_message(message, display_name=name)
            if announcement is None or self._guardian is None:
                return
            await self._guardian.announce(
                announce_text(announcement.display_name, announcement.buffered),
                announce_markup(announcement.max_chat_id, announcement.revision),
            )

        return run

    async def _start_guardian(self, dispatcher: Dispatcher, app: AppConfig) -> None:
        if app.provisioning.mode is ProvisioningMode.OFF or not self._owns_guardian:
            return
        variable = app.provisioning.guardian_bot_token_env
        token = os.environ.get(variable or "", "").strip()
        if not token:
            logger.warning(
                "provisioning is on but ${} holds no token: new contacts cannot be announced",
                variable or "<unset>",
            )
            return
        # Where a business connection is written down when the owner grants one.
        # Told before the guardian starts polling: the update can arrive on the
        # first getUpdates.
        use_business_state_dir(app.paths.data_dir / "state")
        self._guardian = await Guardian.start(
            token=token,
            dispatcher=dispatcher,
            owner_chat_id=app.telegram.owner_user_id,
        )
        logger.info("guardian bot is up")

    def _make_status_provider(
        self, state: BridgeStateRepository, outbox: OutboxRepository
    ) -> StatusProvider:
        async def status(bot_id: int) -> str:
            registry = self._registry
            live = registry.by_bot_id(bot_id) if registry else None
            if live is None:
                return "Мост не найден."

            snapshot = await state.snapshot(live.name) or {}
            queued = await outbox.queue_size(live.name)
            counts = await outbox.counts(live.name)
            max_state = "подключён" if self._max and self._max.is_ready else "нет связи"

            lines = [
                f"MAX: {max_state}",
                f"Очередь: {queued}",
            ]

            # Depth of the intake side. A number here means updates are on disk
            # and not yet carried — invisible before, and exactly what an owner
            # needs to see when messages feel slow.
            if self._inbox is not None:
                waiting = await self._inbox.depth(bot_id)
                if waiting:
                    lines.append(f"Принято, не отправлено: {waiting}")

            oldest = await outbox.oldest_pending_ms(live.name)
            if oldest is not None and oldest > 60_000:
                lines.append(f"Самое старое в очереди: {oldest // 60_000} мин")

            failed = counts.get(OutboxState.FAILED.value, 0)
            if failed:
                lines.append(f"Не доставлено: {failed} — нужен повтор")
            ambiguous = counts.get(OutboxState.AMBIGUOUS.value, 0)
            if ambiguous:
                # Never hidden: this is the one state the owner has to resolve,
                # because only they can tell whether the message actually landed.
                lines.append(f"Неясный результат: {ambiguous} — проверьте в MAX")
            expired = counts.get(OutboxState.EXPIRED.value, 0)
            if expired:
                lines.append(f"Просрочено: {expired}")
            archived = counts.get(OutboxState.ARCHIVED.value, 0)
            if archived:
                # Shown, never counted: it is history the owner already decided
                # about, and hiding it would make the queue look tidier than the
                # record actually is.
                lines.append(f"В архиве: {archived} (не требует действий)")

            broken = self._supervisor.unhealthy
            if broken:
                # Worth saying out loud: a dead background loop is otherwise
                # completely silent.
                names = ", ".join(task.name for task in broken)
                lines.append(f"Не работает: {names}")
            if snapshot.get("last_delivery_at"):
                lines.append(f"Последняя доставка: {snapshot['last_delivery_at']}")
            if snapshot.get("last_error"):
                lines.append(f"Последняя ошибка: {snapshot['last_error']}")
            return "\n".join(lines)

        return status

    def _owner_ingress_line(self) -> str:
        """The owner's own Telegram session, in one line they can act on.

        Five answers, because "не подключена" covered states that ask for
        opposite things. While the intake gate is on, the Bot API path is closed
        against this session — so a session that is never coming back is the
        owner's messages stopping, and it must not read the same as a socket that
        will be up again in a minute.
        """
        from bridge.service.health import read_owner_ingress

        plane = read_owner_ingress(
            self._owner_session_facts(), connected_before=self._owner_session_connected_before
        )
        return _OWNER_INGRESS_LINE.get(plane.state, plane.state.value)
    async def status_lines(self) -> list[str]:
        """What «Технические данные» in the guardian chat says about the worker.

        Read from the persistent snapshot rather than from this process's own
        memory: after a restart the interesting numbers are the ones from before
        it, and "uptime 4 seconds, everything fine" is the least useful answer a
        bridge can give somebody who just noticed it was down.

        Deliberately short and column-aligned in plain text: a phone renders a
        markdown table as rubble, and this is read on a phone.
        """
        return [line for _, line in await self._status_pairs()]

    async def diagnostic_sections(self) -> dict[str, list[str]]:
        """The same lines, grouped, for the four drill-downs under Diagnostics.

        One producer, four readers. Splitting the block into sections by parsing
        its own output would be a second source of truth for what a line means,
        and the first thing to drift.
        """
        sections: dict[str, list[str]] = {}
        for section, line in await self._status_pairs():
            sections.setdefault(section, []).append(line)
        return sections

    async def diagnostic_lights(self) -> dict[str, bool]:
        """Four green/red answers for the diagnostics landing screen."""
        snapshot = None
        health = self._health
        if health is not None:
            with contextlib.suppress(Exception):
                snapshot = await health.snapshot()
        connected = bool(self._max and self._max.is_ready)
        return {
            "telegram": bool(self._registry and self._registry.live) or self._guardian is not None,
            "max": connected,
            "delivery": snapshot is None
            or not (snapshot.outbox_failed or snapshot.outbox_ambiguous),
            "database": snapshot is None or snapshot.database_write.healthy,
        }

    async def diagnostic_pairs(self) -> list[tuple[str, str]]:
        """`status_lines`, still tagged, for a caller that groups them itself."""
        return await self._status_pairs()

    async def _status_pairs(self) -> list[tuple[str, str]]:
        """Every technical line, each tagged with the section it belongs to.

        The order is the order `/status` has always printed, so the composed
        block is unchanged; the tag is what lets Diagnostics show a quarter of
        it at a time.
        """
        registry = self._registry
        connected = "подключён" if self._max and self._max.is_ready else "нет связи"
        lines = [
            ("link", f"MAX        {connected}"),
            ("link", f"Мосты      {len(registry.live) if registry else 0}"),
            ("sessions", f"Telegram владельца {self._owner_ingress_line()}"),
        ]
        lines.extend(("queue", line) for line in await self._provisioning_lines())

        health = self._health
        if health is not None:
            try:
                snapshot = await health.snapshot()
            except Exception:
                logger.debug("could not read the health snapshot", exc_info=True)
                snapshot = None
            if snapshot is not None:
                if snapshot.uptime_ms is not None:
                    lines.append(("link", f"Аптайм     {snapshot.uptime_ms // 60000} мин"))
                if snapshot.max_offline_ms is not None:
                    lines.append(
                        ("link", f"MAX офлайн {snapshot.max_offline_ms // 60000} мин")
                    )
                if snapshot.max_reconnects:
                    lines.append(("link", f"Переподкл. {snapshot.max_reconnects}"))
                if snapshot.inbox_open:
                    lines.append(
                        ("queue", f"Принято    {snapshot.inbox_open} (ещё не унесено)")
                    )
                queued = snapshot.outbox_pending + snapshot.outbox_leased
                if queued:
                    lines.append(("queue", f"В очереди  {queued}"))
                if snapshot.oldest_pending_ms:
                    lines.append(
                        ("queue", f"Старейшее  {snapshot.oldest_pending_ms // 60000} мин")
                    )
                if snapshot.outbox_failed:
                    lines.append(
                        ("queue", f"Не дошло   {snapshot.outbox_failed} — нужен повтор")
                    )
                if snapshot.outbox_ambiguous:
                    lines.append(
                        ("queue", f"Неясно     {snapshot.outbox_ambiguous} — проверьте в MAX")
                    )
                write = snapshot.database_write
                if not write.healthy:
                    lines.append((
                        "queue",
                        "База        восстанавливается"
                        if write.recovering
                        else "База        не пишет — действия приостановлены",
                    ))
                inbox = snapshot.owner_inbox
                if inbox.open or inbox.claimed:
                    lines.append(
                        ("sessions", f"В работе    {inbox.open + inbox.claimed} действ.")
                    )
                if inbox.dead:
                    lines.append(("sessions", f"Застряло    {inbox.dead} — нужно повторить"))
                lines.extend(("media", line) for line in self._owner_reaction_lines())
                if not snapshot.owner_ingress.ready:
                    # One transport is paused, not the bridge. MAX→Telegram runs
                    # on the contact bots and never touches this session, so
                    # "мосты приостановлены" would have been false as well as
                    # frightening.
                    lines.append((
                        "sessions",
                        "Действия владельца приостановлены: Telegram-сессия недоступна",
                    ))
                lines.extend(("media", line) for line in _native_media_lines(snapshot))
                if snapshot.identity_repair:
                    lines.append(
                        ("sessions", "Отпечаток  восстановлен из копии — см. журнал")
                    )
                if snapshot.last_unclean_start_at:
                    lines.append(("link", "Прошлый запуск был после аварийного завершения"))
                if snapshot.alerts_pending:
                    lines.append(
                        ("queue", f"Уведомл.   {snapshot.alerts_pending} не отправлено")
                    )

        broken = self._supervisor.unhealthy
        if broken:
            lines.append(
                ("link", "Не работает: " + ", ".join(task.name for task in broken))
            )
        return lines

    async def _fill_pinned_status(
        self,
        pinned: PinnedStatus,
        contacts: dict[int, tuple[str, int]],
        owner_chat_id: int,
    ) -> None:
        """Ask MAX where everybody is, so the pinned line is right immediately.

        Without this the line would stay blank until a contact happened to move,
        which for a quiet dialog can be days.
        """
        if not pinned.enabled or not contacts or self._max is None:
            return
        try:
            snapshot = await self._max.contact_presence(list(contacts))
        except Exception:
            logger.debug("could not read contact presence at start-up", exc_info=True)
            return

        for user_id, presence in snapshot.items():
            target = contacts.get(user_id)
            if target is None:
                continue
            bridge_name, bot_id = target
            try:
                await pinned.update(
                    bridge_name, presence, bot_id=bot_id, chat_id=owner_chat_id, force=True
                )
            except Exception:
                logger.debug("could not draw the pinned status", exc_info=True)

    async def stop(self) -> None:
        await self._supervisor.stop()
        if self._health is not None:
            # Recorded before anything is torn down: the next start reads this
            # to tell a clean stop from a kill.
            with contextlib.suppress(Exception):
                await self._health.note_clean_shutdown()
        if self._workers is not None:
            # Before the bots close, so a job in flight finishes against a live
            # session instead of a closed one. Whatever does not finish keeps its
            # row and is picked up by the next start once its lease lapses.
            await self._workers.close()
            self._workers = None
        if self._uploader is not None:
            # Anything still waiting for the rest of its album goes now, before
            # the bots that would carry it are closed.
            with contextlib.suppress(Exception):
                await self._uploader.flush_pending()
            self._uploader = None
        if self._guardian_context is not None:
            # Leave the handlers registered and pointing at nothing: they answer
            # "not running yet", which is exactly true between a stop and the
            # next start.
            self._guardian_context.provisioner = None
            self._guardian_context.picker = None
            self._guardian_context.flow = None
        self._flow = None
        self._provisioner_port = None
        if self._owner_intake is not None:
            # An owner album still waiting for its next part goes now, before the
            # session that would re-fetch its media is closed. Its parts survive
            # either way; carrying them now is what keeps the owner from watching
            # photos leave Telegram and arrive in MAX a restart later.
            with contextlib.suppress(Exception):
                await self._owner_intake.flush_albums()
            with contextlib.suppress(Exception):
                # And any album echo mid-group: its binding job is what carries
                # it across the restart, and the job does not exist until here.
                await self._owner_intake.flush_echo_albums()
            self._owner_intake = None
        if self._owner_session is not None:
            # The owner's MTProto session holds a socket to Telegram; the worker
            # is rebuilt on every restart, so it is closed here like the other
            # account credential. Health records the disconnect.
            with contextlib.suppress(Exception):
                await self._owner_session.stop()
            self._owner_session = None
        if self._owned is not None:
            # A user session is a whole-account credential and the worker is
            # rebuilt on every restart: leaving it open would leak one per
            # restart, each holding a socket to Telegram.
            await self._owned.close()
            self._owned = None
        if self._guardian is not None and self._owns_guardian:
            await self._guardian.stop()
            self._guardian = None
        if self._registry is not None:
            await self._registry.close()
            self._registry = None
        if self._max is not None:
            await self._max.stop()
            self._max = None
        if self._database is not None:
            await self._database.close()
            self._database = None
        self._stopped.set()

    async def run_forever(self) -> None:
        """Serve until SIGTERM or SIGINT, then shut down cleanly."""
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()

        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)

        await stop.wait()
        logger.info("shutting down")
