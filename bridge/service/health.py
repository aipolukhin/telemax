"""What the bridge knows about itself, and how the owner hears about it.

Two things that only look separate.

**The snapshot** answers "is this working" after a restart, not only during one.
Health kept in RAM answers "since this process started", which is the least
useful window there is: the question after a crash is what happened *before* it.
So the few facts that cannot be recounted — when the process started, when MAX
last dropped, whether the last shutdown was clean — are written down, and
everything countable is counted at the moment somebody asks.

**The alerts** exist because `/status` requires the owner to already suspect
something. A bridge that stops carrying messages is exactly the situation where
nobody is looking at it. So problems are pushed, once, through a durable queue —
durable because the outage that caused the alert is often the outage that would
swallow it.

What is deliberately not here: message text, phone numbers, tokens, session
secrets, signed URLs. An alert says how many and how old, never what was said.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from bridge.max_client.native_state import KindStatus
from bridge.media.native_max import decoder_available
from bridge.storage import (
    AlertRepository,
    HealthStateRepository,
    OutboxRepository,
    OutboxState,
    TelegramInboxRepository,
    now_ms,
)

logger = logging.getLogger(__name__)

# Durable keys. Named here so a typo is a NameError rather than a silently
# missing field in the snapshot.
KEY_PROCESS_STARTED_AT = "process_started_at"
KEY_LAST_CLEAN_SHUTDOWN = "last_clean_shutdown_at"
KEY_LAST_UNCLEAN_START = "last_unclean_start_at"
KEY_MAX_CONNECTED_AT = "max_connected_at"
KEY_MAX_DISCONNECTED_AT = "max_disconnected_at"
KEY_MAX_RECONNECTS = "max_reconnects"
KEY_MAX_LAST_ERROR = "max_last_error"
#: The one component key the owner's Telegram MTProto user session gets. Named
#: like the MAX pair so the snapshot reads the same after a restart: when it last
#: connected, when it last dropped, why. Intake being *enabled* is a separate,
#: stable fact (onboarding state) — this only tracks the live connection.
KEY_TG_SESSION_CONNECTED_AT = "telegram_user_session_connected_at"
KEY_TG_SESSION_DISCONNECTED_AT = "telegram_user_session_disconnected_at"
KEY_TG_SESSION_RECONNECTS = "telegram_user_session_reconnects"
KEY_TG_SESSION_LAST_ERROR = "telegram_user_session_last_error"
#: How many owner-originated messages the Bot API path has stood aside for
#: because the MTProto session is the authoritative intake. The gate closes that
#: path deliberately and *stays* closed across a disconnect — losing a message is
#: the accepted trade against duplicating one — but a message that reaches
#: neither transport must not do so invisibly. This is what makes the trade
#: countable: normally it tracks the number the session actually carried, and a
#: session that has stopped receiving owner updates shows up here as a number
#: that keeps climbing while nothing arrives in MAX.
KEY_OWNER_INTAKE_SUPPRESSED = "owner_intake_suppressed"
KEY_SCHEMA_VERSION = "schema_version"
KEY_COMMIT = "commit"
KEY_SHUTDOWN_MARK = "running"

#: How often health is refreshed while nothing in particular happens. Rare on
#: purpose: this is a write, and the numbers it stores are only read by a human.
HEALTH_INTERVAL_SECONDS = 60

#: How long MAX may be gone before the owner is told.
MAX_OFFLINE_ALERT_SECONDS = 600

#: Queue depth and age that count as "something is wrong", not "it is busy".
QUEUE_DEPTH_ALERT = 25
OLDEST_PENDING_ALERT_SECONDS = 900

#: One incident does not re-alert more often than this.
ALERT_COOLDOWN_MS = 6 * 60 * 60 * 1000

#: A bridge that is in the register and did not come up. One incident per
#: bridge, keyed on its name so four broken bridges are four answerable
#: questions rather than one alert that names none of them.
BRIDGE_NOT_STARTED = "bridge-not-started"

#: A provisioning attempt that has been unfinished for too long. Keyed on the
#: MAX chat id: the username would name the bot, and the bot is not the thing
#: the owner is being asked about.
PROVISIONING_STUCK = "provisioning-stuck"

#: What the owner is told for each not-ready state, in terms of what they can do
#: about it. Never a credential, a token or a line of anybody's message.
_OWNER_INGRESS_TEXT = {
    "missing-configuration": "Сессия не настроена: нет API-ключей или файла сессии.",
    "authorization-required": "Сессия отозвана — нужен telemax telegram-sync.",
    "connecting": "Подключаемся…",
    "disconnected": "Соединение потеряно, восстанавливаем.",
    "stopped": "Сессия остановлена.",
}


class OwnerIngressState(StrEnum):
    """What the owner's puppet session is, in the words readiness needs.

    Six, and not a boolean, because "not ready" covers conditions that call for
    opposite actions from the owner. Nobody needs to hear about a socket that
    will be back in a minute; everybody needs to hear about a session that will
    never come back until they scan a QR.
    """

    #: Configured, authorised, connected, and its supervisor is not complaining.
    READY = "ready"
    #: No API credentials or no session file. A configuration answer, not a login.
    MISSING_CONFIGURATION = "missing-configuration"
    #: The session is revoked, gone, or belongs to somebody else. Terminal until
    #: `telemax telegram-sync`.
    AUTHORIZATION_REQUIRED = "authorization-required"
    #: Coming up for the first time. Nothing has been carried through it yet.
    CONNECTING = "connecting"
    #: It worked before and the socket dropped. The watchdog is on it.
    DISCONNECTED = "disconnected"
    #: Deliberately closed, on shutdown.
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class OwnerSessionFacts:
    """What the runtime knows about the session, with no opinion attached."""

    #: A session object exists, so credentials and a session file were found.
    started: bool = False
    #: `SessionStatus` as a plain string, or None when there is no session.
    status: str | None = None
    connected: bool = False
    #: The one supervisor that owns `telegram-user-session` is not reporting it
    #: as dead. A second supervisor is deliberately not introduced.
    supervisor_healthy: bool = True


#: How many canaries in a row must pass before the incident is closed. One
#: success after a failure is a coincidence as often as a recovery, and telling
#: the owner it is fixed twice is worse than telling them once, late.
CANARY_SUCCESSES_TO_CLEAR = 2


@dataclass(frozen=True, slots=True)
class OwnerInboxFacts:
    """What is written down and not yet finished. Counts only, never content."""

    open: int = 0
    claimed: int = 0
    dead: int = 0
    oldest_open_ms: int | None = None


@dataclass(frozen=True, slots=True)
class DatabaseWrite:
    """Whether the bridge can still write down what it is told.

    Not the same question as "is the process running". The outage it exists for
    left `systemd` perfectly happy: a transaction could not be closed, every
    write after that went into one nothing would ever commit, and the only sign
    was a stack trace per poll that nobody was reading.
    """

    healthy: bool
    recovering: bool = False
    reason: str = ""
    last_ok_ms: int = 0
    failures: int = 0


@dataclass(frozen=True, slots=True)
class OwnerIngress:
    """Whether Telegram events authored by the owner can reach the bridge.

    The invariant, stated once:

        owner_ingress_ready =
            configured and authorized and connected and intake_supervisor_healthy

    This is one transport, not the bridge. What stops when it is false is the
    *owner's* half: new owner-authored Telegram events, refetching owner media
    that only the puppet session can reach, and Telegram→MAX synchronisation of
    what the owner did.

    What keeps running is most of the product. Contact bots still deliver
    MAX→Telegram — that path never touches this session. Guardian still answers,
    which is not incidental: it is where an unauthorised session is authorised
    again. Work already accepted onto the queue still settles, as long as it does
    not need the session to fetch something.
    """

    state: OwnerIngressState
    #: One short line for `/status` and for the incident. Never a phone number,
    #: an api_hash, a session token, an auth code or message content.
    reason: str

    @property
    def ready(self) -> bool:
        return self.state is OwnerIngressState.READY


def read_owner_ingress(facts: OwnerSessionFacts, *, connected_before: bool) -> OwnerIngress:
    """Turn what the runtime knows into the one answer everything else reads.

    `connected_before` is the durable "it has worked at some point" — the
    difference between a first start still coming up and a session that dropped.
    """
    if not facts.started or facts.status is None:
        return OwnerIngress(
            OwnerIngressState.MISSING_CONFIGURATION, "the owner session has not been started"
        )
    if facts.status == "failed":
        return OwnerIngress(
            OwnerIngressState.MISSING_CONFIGURATION,
            "the owner session cannot start: API credentials or session file are missing",
        )
    if facts.status == "unauthorized":
        return OwnerIngress(
            OwnerIngressState.AUTHORIZATION_REQUIRED,
            "the owner session is revoked; run `telemax telegram-sync`",
        )
    if facts.status == "stopped":
        return OwnerIngress(OwnerIngressState.STOPPED, "the owner session is stopped")
    if facts.status == "connected" and facts.connected:
        if not facts.supervisor_healthy:
            return OwnerIngress(
                OwnerIngressState.DISCONNECTED, "the owner session supervisor is not running"
            )
        return OwnerIngress(OwnerIngressState.READY, "connected")
    if connected_before:
        return OwnerIngress(
            OwnerIngressState.DISCONNECTED, "the owner session dropped; reconnecting"
        )
    return OwnerIngress(OwnerIngressState.CONNECTING, "the owner session is connecting")


@dataclass(frozen=True, slots=True)
class BridgeHealth:
    name: str
    running: bool
    last_intake_at: int | None
    last_delivery_at: int | None
    last_error: str | None
    pending: int
    failed: int
    ambiguous: int
    #: Jobs that waited out their whole TTL undelivered. Counted because they
    #: used to be retired in silence — no incident, no `/failed` entry, and a
    #: payload cleared on the way out.
    expired: int
    oldest_pending_ms: int | None


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    """Everything `/status` reads, and everything an alert is decided from."""

    now_ms: int
    process_started_at: int | None
    uptime_ms: int | None
    schema_version: int | None
    commit: str | None
    last_clean_shutdown_at: int | None
    last_unclean_start_at: int | None

    max_connected: bool
    max_connected_at: int | None
    max_disconnected_at: int | None
    max_offline_ms: int | None
    max_reconnects: int
    max_last_error: str | None

    #: The owner's MTProto user session. `tg_session_connected` is the live
    #: connection; whether that intake is *authoritative* is a separate, stable
    #: fact the snapshot does not carry (it lives in onboarding state).
    tg_session_connected: bool = False
    tg_session_connected_at: int | None = None
    tg_session_disconnected_at: int | None = None
    tg_session_offline_ms: int | None = None
    tg_session_reconnects: int = 0
    #: Owner messages the Bot API path left to the MTProto session.
    owner_intake_suppressed: int = 0
    tg_session_last_error: str | None = None

    bridges: tuple[BridgeHealth, ...] = ()

    inbox_open: int = 0
    outbox_pending: int = 0
    outbox_leased: int = 0
    outbox_failed: int = 0
    outbox_ambiguous: int = 0
    outbox_expired: int = 0
    oldest_pending_ms: int | None = None

    db_bytes: int = 0
    wal_bytes: int = 0
    open_media_groups: int = 0
    temp_files: int = 0
    oldest_temp_ms: int | None = None
    alerts_pending: int = 0

    #: Native voice and circle, one entry per kind. Counters since this process
    #: started — the breaker is a statement about this session, so counting it
    #: across restarts would say the wrong thing.
    native_media: tuple[KindStatus, ...] = ()
    #: Whether a decoder exists at all. False means every voice and every circle
    #: degrades, for every message, and nothing else in the counters says so.
    native_media_decoder: bool = True
    #: Whether the bridge may carry the owner's messages at all, and why not.
    owner_ingress: OwnerIngress = OwnerIngress(OwnerIngressState.READY, "connected")
    #: None where there is no connection to ask — the setup CLI, most tests.
    database_write: DatabaseWrite = DatabaseWrite(healthy=True)
    owner_inbox: OwnerInboxFacts = OwnerInboxFacts()
    #: Why the MAX identity file had to be recovered on this start, if it did.
    #: A machine that stopped badly enough for this is worth saying so about —
    #: the file decides which phone the account claims to be.
    identity_repair: str | None = None

    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def native_media_systemic(self) -> tuple[KindStatus, ...]:
        """Kinds whose native path is broken for everybody, not for one file.

        The distinction the incident hangs on. A file with no audio track, a
        codec PyAV will not open, a duration of zero — those are one message
        degrading, which is the design working, and waking somebody for each is
        how an alert channel becomes noise. A tripped breaker, an uploader we
        have no path for, or a decoder that is missing outright are different:
        every voice from now on arrives as a file, and nobody would otherwise
        notice for weeks.

        A kind the operator switched off is not systemic either. That is somebody
        already knowing.
        """
        return tuple(
            status
            for status in self.native_media
            if status.enabled
            and (status.breaker_open or status.uploader_drift or not self.native_media_decoder)
        )

    def native_media_state(self, status: KindStatus) -> str:
        """One word for a kind, with the process-wide facts folded in.

        `KindStatus.state` cannot see whether a decoder exists — that is not a
        property of voice or of circle, it is a property of the install — so the
        two are joined here rather than duplicated in the dataclass.
        """
        if not status.enabled:
            return "disabled"
        if status.breaker_open:
            return "breaker-open"
        if status.uploader_drift:
            return "uploader-drift"
        if not self.native_media_decoder:
            return "dependency-unavailable"
        return status.state


class HealthService:
    """Builds the snapshot, and decides what is worth waking the owner for."""

    def __init__(
        self,
        *,
        health: HealthStateRepository,
        outbox: OutboxRepository,
        inbox: TelegramInboxRepository,
        alerts: AlertRepository,
        db_path: Path,
        database: Any = None,
        owner_inbox: Any = None,
        temp_dir: Path | None = None,
        bridges: Callable[[], list[tuple[str, bool]]] | None = None,
        max_is_ready: Callable[[], bool] | None = None,
        tg_session_is_ready: Callable[[], bool] | None = None,
        native_media: Callable[[], tuple[KindStatus, ...]] | None = None,
        identity_repair: Callable[[], str | None] | None = None,
        owner_session_facts: Callable[[], OwnerSessionFacts] | None = None,
        offline_alert_seconds: int = MAX_OFFLINE_ALERT_SECONDS,
    ) -> None:
        self._health = health
        self._outbox = outbox
        self._inbox = inbox
        self._alerts = alerts
        self._db_path = db_path
        self._temp_dir = temp_dir
        self._bridges = bridges or (lambda: [])
        self._max_is_ready = max_is_ready or (lambda: False)
        # None until the MTProto transport is wired; the snapshot then falls back
        # to the durable timestamps, exactly like MAX.
        self._tg_session_is_ready = tg_session_is_ready
        # None wherever there is no MAX client to have counters — the setup CLI,
        # most tests. An empty tuple reads as "nothing to say", not as "broken".
        self._native_media = native_media or (lambda: ())
        self._identity_repair = identity_repair or (lambda: None)
        # None where there is no owner ingress to be ready — the setup CLI, tests
        # that build health alone. Those read as ready rather than as broken.
        self._owner_session_facts = owner_session_facts
        # The connection itself, so the canary goes through the production
        # wrapper rather than a second one that could be healthy while the real
        # one is not.
        self._database = database
        self._owner_inbox = owner_inbox
        self._canary_successes = 0
        self._write_outage_logged = False
        self._offline_alert_seconds = offline_alert_seconds

    # ------------------------------------------------------------- lifecycle

    async def note_start(self, *, schema_version: int, commit: str | None = None) -> bool:
        """Record this process. Returns True when the last run did not stop cleanly.

        The marker is the whole trick: it is set on start and cleared on a clean
        stop, so finding it still set means the previous process was killed.
        """
        was_running = bool(await self._health.get(KEY_SHUTDOWN_MARK, False))
        stamp = now_ms()
        await self._health.set(KEY_PROCESS_STARTED_AT, stamp)
        await self._health.set(KEY_SCHEMA_VERSION, schema_version)
        if commit:
            await self._health.set(KEY_COMMIT, commit)
        if was_running:
            await self._health.set(KEY_LAST_UNCLEAN_START, stamp)
        await self._health.set(KEY_SHUTDOWN_MARK, True)
        return was_running

    async def previous_start_at(self) -> int | None:
        """When the run that just ended began, before this one overwrites it.

        The bound the claim sweep needs: a `message_map` row written after that
        moment belongs to the process that has just gone, and a row older than it
        is history somebody has to decide about rather than something a restart
        may quietly undo. Must be read before `note_start`.
        """
        value = await self._health.get(KEY_PROCESS_STARTED_AT)
        return int(value) if value is not None else None

    async def note_clean_shutdown(self) -> None:
        await self._health.set(KEY_LAST_CLEAN_SHUTDOWN, now_ms())
        await self._health.set(KEY_SHUTDOWN_MARK, False)

    async def note_max_connected(self) -> None:
        if await self._health.get(KEY_MAX_DISCONNECTED_AT) is not None:
            await self._health.bump(KEY_MAX_RECONNECTS)
        await self._health.set(KEY_MAX_CONNECTED_AT, now_ms())
        await self._health.set(KEY_MAX_DISCONNECTED_AT, None)

    async def note_max_disconnected(self, error: str | None = None) -> None:
        if await self._health.get(KEY_MAX_DISCONNECTED_AT) is None:
            await self._health.set(KEY_MAX_DISCONNECTED_AT, now_ms())
        if error:
            await self._health.set(KEY_MAX_LAST_ERROR, error[:300])

    async def note_tg_session_connected(self) -> None:
        if await self._health.get(KEY_TG_SESSION_DISCONNECTED_AT) is not None:
            await self._health.bump(KEY_TG_SESSION_RECONNECTS)
        await self._health.set(KEY_TG_SESSION_CONNECTED_AT, now_ms())
        await self._health.set(KEY_TG_SESSION_DISCONNECTED_AT, None)

    async def note_tg_session_disconnected(self, error: str | None = None) -> None:
        if await self._health.get(KEY_TG_SESSION_DISCONNECTED_AT) is None:
            await self._health.set(KEY_TG_SESSION_DISCONNECTED_AT, now_ms())
        if error:
            await self._health.set(KEY_TG_SESSION_LAST_ERROR, error[:300])

    async def note_owner_intake_suppressed(self) -> None:
        """One owner message the Bot API path stood aside for. Never the content.

        Called from the three places the gate closes that path. It records
        nothing about the message — not its text, not its size, not its peer —
        only that the hand-off happened, which is the one fact needed to tell
        "the session is carrying them" from "they are going nowhere".
        """
        await self._health.bump(KEY_OWNER_INTAKE_SUPPRESSED)

    # -------------------------------------------------------------- snapshot

    async def _database_write(self) -> DatabaseWrite:
        """Run the write canary and turn its answer into one fact.

        The canary is a real transaction through the production wrapper, so a
        connection that cannot close one fails here rather than looking fine.
        Recovery is attempted by the canary itself; what this decides is only
        whether to say so, and it waits for two successes in a row before
        calling it fixed.
        """
        database = self._database
        if database is None:
            return DatabaseWrite(healthy=True)

        healthy = await database.write_canary()
        if healthy:
            self._canary_successes += 1
        else:
            self._canary_successes = 0
        self._write_outage_logged = False
        settled = self._canary_successes >= CANARY_SUCCESSES_TO_CLEAR
        return DatabaseWrite(
            healthy=healthy and settled,
            recovering=healthy and not settled,
            # Never the SQL, never a parameter: the reason is a shape, and the
            # wrapper already reduces it to one.
            reason="" if healthy else (database.poisoned or "a write did not complete"),
            last_ok_ms=int(database.last_write_ok_ms),
            failures=int(database.write_failures),
        )

    async def _owner_inbox_facts(self) -> OwnerInboxFacts:
        if self._owner_inbox is None:
            return OwnerInboxFacts()
        counts = await self._owner_inbox.counts()
        return OwnerInboxFacts(
            open=int(counts.get("open", 0)),
            claimed=int(counts.get("claimed", 0)),
            dead=int(counts.get("dead", 0)),
            oldest_open_ms=await self._owner_inbox.oldest_open_ms(),
        )

    async def snapshot(self) -> HealthSnapshot:
        database_write = await self._database_write()
        owner_inbox = await self._owner_inbox_facts()
        state = await self._health.all()
        stamp = now_ms()
        started = state.get(KEY_PROCESS_STARTED_AT)
        disconnected_at = state.get(KEY_MAX_DISCONNECTED_AT)
        connected = self._max_is_ready()
        tg_disconnected_at = state.get(KEY_TG_SESSION_DISCONNECTED_AT)
        tg_connected = (
            self._tg_session_is_ready()
            if self._tg_session_is_ready is not None
            else (
                state.get(KEY_TG_SESSION_CONNECTED_AT) is not None
                and tg_disconnected_at is None
            )
        )

        bridges: list[BridgeHealth] = []
        pending = leased = failed = ambiguous = expired = 0
        oldest: int | None = None

        for name, running in self._bridges():
            counts = await self._outbox.counts(name)
            age = await self._outbox.oldest_pending_ms(name)
            bridge_failed = counts.get(OutboxState.FAILED.value, 0)
            bridge_ambiguous = counts.get(OutboxState.AMBIGUOUS.value, 0)
            bridge_expired = counts.get(OutboxState.EXPIRED.value, 0)
            bridge_pending = counts.get(OutboxState.PENDING.value, 0)
            bridge_leased = counts.get(OutboxState.INFLIGHT.value, 0)

            pending += bridge_pending
            leased += bridge_leased
            failed += bridge_failed
            ambiguous += bridge_ambiguous
            expired += bridge_expired
            if age is not None:
                oldest = age if oldest is None else max(oldest, age)

            bridges.append(
                BridgeHealth(
                    name=name,
                    running=running,
                    last_intake_at=state.get(f"bridge:{name}:last_intake_at"),
                    last_delivery_at=state.get(f"bridge:{name}:last_delivery_at"),
                    last_error=state.get(f"bridge:{name}:last_error"),
                    pending=bridge_pending,
                    failed=bridge_failed,
                    ambiguous=bridge_ambiguous,
                    expired=bridge_expired,
                    oldest_pending_ms=age,
                )
            )

        temp_files, oldest_temp = self._temp_stats()
        return HealthSnapshot(
            now_ms=stamp,
            process_started_at=started,
            uptime_ms=(stamp - int(started)) if started else None,
            schema_version=state.get(KEY_SCHEMA_VERSION),
            commit=state.get(KEY_COMMIT),
            last_clean_shutdown_at=state.get(KEY_LAST_CLEAN_SHUTDOWN),
            last_unclean_start_at=state.get(KEY_LAST_UNCLEAN_START),
            max_connected=connected,
            max_connected_at=state.get(KEY_MAX_CONNECTED_AT),
            max_disconnected_at=disconnected_at,
            max_offline_ms=(stamp - int(disconnected_at)) if disconnected_at else None,
            max_reconnects=int(state.get(KEY_MAX_RECONNECTS, 0) or 0),
            max_last_error=state.get(KEY_MAX_LAST_ERROR),
            tg_session_connected=tg_connected,
            tg_session_connected_at=state.get(KEY_TG_SESSION_CONNECTED_AT),
            tg_session_disconnected_at=tg_disconnected_at,
            tg_session_offline_ms=(
                (stamp - int(tg_disconnected_at)) if tg_disconnected_at else None
            ),
            tg_session_reconnects=int(state.get(KEY_TG_SESSION_RECONNECTS, 0) or 0),
            owner_intake_suppressed=int(state.get(KEY_OWNER_INTAKE_SUPPRESSED, 0) or 0),
            tg_session_last_error=state.get(KEY_TG_SESSION_LAST_ERROR),
            bridges=tuple(bridges),
            inbox_open=await self._inbox.depth(),
            outbox_pending=pending,
            outbox_leased=leased,
            outbox_failed=failed,
            outbox_ambiguous=ambiguous,
            outbox_expired=expired,
            oldest_pending_ms=oldest,
            database_write=database_write,
            owner_inbox=owner_inbox,
            db_bytes=self._size(self._db_path),
            wal_bytes=self._size(self._db_path.with_name(self._db_path.name + "-wal")),
            open_media_groups=await self._open_media_groups(),
            temp_files=temp_files,
            oldest_temp_ms=oldest_temp,
            alerts_pending=await self._alerts.pending_count(),
            native_media=self._native_media(),
            native_media_decoder=decoder_available(),
            owner_ingress=(
                read_owner_ingress(
                    self._owner_session_facts(),
                    # Durable: "it has worked at some point", which is what tells
                    # a first start still coming up from a session that dropped.
                    connected_before=state.get(KEY_TG_SESSION_CONNECTED_AT) is not None,
                )
                if self._owner_session_facts is not None
                else OwnerIngress(OwnerIngressState.READY, "connected")
            ),
            identity_repair=self._identity_repair(),
        )

    async def _open_media_groups(self) -> int:
        """Albums mid-assembly — the number this has always meant.

        `media_group_part` holds two kinds of row since V15: parts still waiting
        for the rest of their group, and the per-part aliases of a group that has
        already become a message. Only the first kind is "open"; counting the
        aliases too would make the figure climb for ever and stop being a signal.
        A settled group is exactly one that has a canonical row behind it.
        """
        rows = await self._outbox._db.query(
            "SELECT COUNT(DISTINCT media_group_id) AS n FROM media_group_part"
            " WHERE link_id IS NULL"
        )
        return int(rows[0]["n"]) if rows else 0

    @staticmethod
    def _size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    def _temp_stats(self) -> tuple[int, int | None]:
        if self._temp_dir is None or not self._temp_dir.exists():
            return 0, None
        newest_age: int | None = None
        count = 0
        now = time.time()
        for item in self._temp_dir.iterdir():
            if not item.is_file():
                continue
            count += 1
            try:
                age = int((now - item.stat().st_mtime) * 1000)
            except OSError:
                continue
            newest_age = age if newest_age is None else max(newest_age, age)
        return count, newest_age

    # ---------------------------------------------------------------- alerts

    async def evaluate(self, snapshot: HealthSnapshot) -> list[str]:
        """What is worth waking the owner for. Never raises.

        Deciding an incident means writing one, and the failure this exists to
        report is writes not working. An evaluation that raised would take the
        health loop with it — which is how an hour-long outage stayed silent
        apart from a stack trace per poll.
        """
        try:
            return await self._evaluate(snapshot)
        except Exception as error:  # noqa: BLE001 - health must outlive its storage
            if not self._write_outage_logged:
                logger.error("health could not be recorded: %s", type(error).__name__)
                self._write_outage_logged = True
            return []

    async def _evaluate(self, snapshot: HealthSnapshot) -> list[str]:
        """Open or close incidents from one snapshot. Returns the keys touched.

        Each condition is one incident, so a queue forty jobs deep produces one
        message rather than forty, and a MAX outage produces one rather than one
        per retry.
        """
        touched: list[str] = []

        offline_ms = snapshot.max_offline_ms
        # Both conditions: a stale disconnect timestamp on a session that is
        # plainly working would otherwise alert for ever.
        if (
            not snapshot.max_connected
            and offline_ms is not None
            and offline_ms >= self._offline_alert_seconds * 1000
        ):
            if await self._raise(
                "max-offline",
                f"⚠️ Telemax: MAX недоступен {offline_ms // 60000} мин\n\n"
                "Сообщения сохраняются в очереди.\n"
                f"Ожидают отправки: {snapshot.outbox_pending}\n"
                f"Не доставлено: {snapshot.outbox_failed}",
            ):
                touched.append("max-offline")
        elif snapshot.max_connected:
            if await self._clear(
                "max-offline",
                "✅ Telemax: связь с MAX восстановлена\n\n"
                f"Ожидают отправки: {snapshot.outbox_pending}",
            ):
                touched.append("max-offline:resolved")

        if snapshot.outbox_failed:
            if await self._raise(
                "delivery-failed",
                f"⚠️ Не доставлено сообщений: {snapshot.outbox_failed}\n\n"
                f"{self._bridge_lines(snapshot, 'failed')}"
                "Нужен повтор или решение владельца.",
            ):
                touched.append("delivery-failed")
        elif await self._clear("delivery-failed", "✅ Недоставленных сообщений больше нет"):
            touched.append("delivery-failed:resolved")

        if snapshot.outbox_ambiguous:
            if await self._raise(
                "delivery-ambiguous",
                f"⚠️ Неясный результат отправки: {snapshot.outbox_ambiguous}\n\n"
                f"{self._bridge_lines(snapshot, 'ambiguous')}"
                "Проверьте в MAX, дошло ли сообщение, и отметьте решённым.",
            ):
                touched.append("delivery-ambiguous")
        elif await self._clear("delivery-ambiguous", "✅ Неясных отправок больше нет"):
            touched.append("delivery-ambiguous:resolved")

        if snapshot.outbox_expired:
            if await self._raise(
                "delivery-expired",
                f"⚠️ Сообщения не доставлены за сутки: {snapshot.outbox_expired}\n\n"
                f"{self._bridge_lines(snapshot, 'expired')}"
                "Повторить их уже нельзя — только посмотреть и убрать.",
            ):
                touched.append("delivery-expired")
        elif await self._clear("delivery-expired", "✅ Просроченных отправок больше нет"):
            touched.append("delivery-expired:resolved")

        depth = snapshot.outbox_pending + snapshot.outbox_leased
        if depth >= QUEUE_DEPTH_ALERT:
            if await self._raise(
                "queue-depth",
                f"⚠️ Очередь выросла: {depth} сообщений ждут отправки",
            ):
                touched.append("queue-depth")
        elif await self._clear("queue-depth", "✅ Очередь разошлась"):
            touched.append("queue-depth:resolved")

        # Whether the bridge can still write down what it is told. First,
        # because everything below is only meaningful if it can.
        write = snapshot.database_write
        try:
            if not write.healthy and not write.recovering:
                if await self._raise(
                    "database-write-unavailable",
                    "⚠️ Telemax: не могу надёжно сохранять события\n\n"
                    "Мост временно не записывает новые события, поэтому ваши "
                    "действия в Telegram приостановлены. Уже принятые до сбоя "
                    "события восстановить нельзя.\n\n"
                    "Этот чат работает. Восстановление идёт автоматически.",
                ):
                    touched.append("database-write-unavailable")
            elif write.healthy and await self._clear(
                "database-write-unavailable", "✅ Telemax снова сохраняет события"
            ):
                touched.append("database-write-unavailable:resolved")
        except Exception:  # noqa: BLE001 - the one incident that cannot rely on writing
            # Recording an incident is itself a write, so the one incident about
            # writes being broken is the one that may not be recordable. Said
            # once in the journal and shown in `/status` from the snapshot in
            # memory; the durable copy arrives when writing does.
            if not self._write_outage_logged:
                logger.error("the database cannot record its own write outage")
                self._write_outage_logged = True
        else:
            self._write_outage_logged = False

        # Owner updates nothing will retry on its own. One incident whatever the
        # count: five stuck updates are one thing to look at, not five.
        if snapshot.owner_inbox.dead:
            if await self._raise(
                "owner-update-stuck",
                f"⚠️ Telemax: {snapshot.owner_inbox.dead} действие(й) не удалось перенести\n\n"
                "Они сохранены и ничего не потеряно, но сами не повторятся. "
                "Посмотреть и повторить — в этом чате.",
            ):
                touched.append("owner-update-stuck")
        elif await self._clear("owner-update-stuck", "✅ Застрявших действий больше нет"):
            touched.append("owner-update-stuck:resolved")

        # The owner's puppet session. One incident, whatever the reason: a
        # session that is down for four different reasons in an hour is one
        # problem, and four incidents would be four notifications about it.
        plane = snapshot.owner_ingress
        if not plane.ready:
            if await self._raise(
                "owner-session-unavailable",
                "⚠️ Telemax: Telegram владельца недоступен\n\n"
                f"{_OWNER_INGRESS_TEXT.get(plane.state, plane.state.value)}\n\n"
                "Приостановлено только то, что вы делаете в Telegram: новые "
                "сообщения, правки, удаления и реакции не уходят в MAX. "
                "Сообщения контактов приходят как обычно, очередь сохраняется.",
            ):
                touched.append("owner-session-unavailable")
        elif await self._clear(
            "owner-session-unavailable", "✅ Telegram владельца снова подключён"
        ):
            touched.append("owner-session-unavailable:resolved")

        # Native voice/circle. Only systemic states open this — see
        # `native_media_systemic` for what is deliberately left out, and why one
        # unreadable file must never reach the owner as an alert.
        systemic = snapshot.native_media_systemic
        if systemic:
            if await self._raise("native-media-degraded", self._native_media_text(snapshot)):
                touched.append("native-media-degraded")
        elif snapshot.native_media and await self._clear(
            "native-media-degraded",
            "✅ Голосовые и кружки снова уходят в MAX как есть",
        ):
            touched.append("native-media-degraded:resolved")

        oldest = snapshot.oldest_pending_ms
        if oldest is not None and oldest >= OLDEST_PENDING_ALERT_SECONDS * 1000:
            if await self._raise(
                "queue-stalled",
                f"⚠️ Сообщение ждёт отправки {oldest // 60000} мин\n\n"
                "Очередь не движется.",
            ):
                touched.append("queue-stalled")
        elif await self._clear("queue-stalled", "✅ Очередь снова движется"):
            touched.append("queue-stalled:resolved")

        return touched

    @staticmethod
    def _native_media_text(snapshot: HealthSnapshot) -> str:
        """Why native media stopped, in the words the owner can act on.

        Kind names and states only. The breaker reason is a MAX error code, which
        is safe; a refusal's message is not, and is never stored.
        """
        names = {"voice": "голосовые", "circle": "кружки"}
        lines = []
        for status in snapshot.native_media_systemic:
            what = names.get(status.kind, status.kind)
            state = snapshot.native_media_state(status)
            if state == "breaker-open":
                lines.append(f"{what}: MAX отклонил вложение ({status.breaker_reason})")
            elif state == "uploader-drift":
                lines.append(f"{what}: MAX выдаёт не тот загрузчик")
            else:
                lines.append(f"{what}: нет декодера (PyAV)")
        return (
            "⚠️ Telemax: медиа уходит в MAX не как есть\n\n"
            + "\n".join(lines)
            + "\n\nСообщения доходят — голосовое файлом, кружок обычным видео.\n"
            "Подробности и счётчики: /status"
        )

    @staticmethod
    def _bridge_lines(snapshot: HealthSnapshot, field_name: str) -> str:
        """Which contacts are affected — by bridge name, never by message text."""
        lines = [
            f"Контакт: {bridge.name}\n"
            for bridge in snapshot.bridges
            if getattr(bridge, field_name, 0)
        ]
        return "".join(lines[:5])

    async def _raise(self, key: str, text: str) -> bool:
        return (
            await self._alerts.open_incident(
                incident_key=key, text=text, cooldown_ms=ALERT_COOLDOWN_MS
            )
        ) is not None

    async def _clear(self, key: str, text: str) -> bool:
        return (await self._alerts.resolve_incident(incident_key=key, text=text)) is not None


class AlertDispatcher:
    """Drains the alert queue into the guardian chat.

    Retries because the thing being reported is often the thing that stops the
    message getting out. An alert that cannot be sent stays queued rather than
    being dropped on the floor — the owner still has `/status`, and the message
    arrives when Telegram does.
    """

    def __init__(
        self,
        *,
        alerts: AlertRepository,
        deliver: Callable[[dict[str, Any]], Awaitable[None]],
        max_attempts: int = 8,
        backoff_ms: int = 60_000,
    ) -> None:
        self._alerts = alerts
        # The whole row rather than its text, and the only shape there is. There
        # used to be a `send` beside it that took `alert["text"]` and put it in
        # the chat — one incident, one message — and it outlived its last
        # production caller. A queue that can still be handed a "just post it"
        # callback is a cascade one wiring mistake away.
        self._deliver = deliver
        self._max_attempts = max_attempts
        self._backoff_ms = backoff_ms

    async def drain_once(self) -> int:
        sent = 0
        for alert in await self._alerts.claim_due():
            try:
                await self._deliver(dict(alert))
            except Exception as error:  # noqa: BLE001 - any failure is a retry
                attempts = int(alert["attempts"]) + 1
                detail = f"{type(error).__name__}: {error}"
                if attempts >= self._max_attempts:
                    logger.error("giving up on alert %s: %s", alert["id"], detail)
                    await self._alerts.mark_failed(int(alert["id"]), error=detail)
                else:
                    logger.warning("alert %s not sent (%s), retrying", alert["id"], detail)
                    await self._alerts.mark_retry(
                        int(alert["id"]), delay_ms=self._backoff_ms * attempts, error=detail
                    )
                continue
            await self._alerts.mark_sent(int(alert["id"]))
            sent += 1
        return sent
