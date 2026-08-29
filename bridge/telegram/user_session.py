"""The owner's Telegram MTProto user session — an additional intake transport.

Not a new domain layer, and not a second product. It is one more way owner-side
Telegram events reach the *existing* seams: the same `message_map`, the same
owner-side ids and delete-by-owner path Secretary Mode first needed, the same
durable delivery. This module is only the transport's lifecycle.

**Stage 1 wires the lifecycle and nothing that changes behaviour.** QR login
into the existing secrets directory, owner verification against the one owner id
Telemax already knows, connect/disconnect under the existing supervisor, and one
health component key. It does **not** turn updates into delivery jobs — that is
Stage 2 — and Bot API stays the authoritative intake until an explicit,
probe-gated flip. The three facts kept deliberately separate:

* **intake enabled** — a stable onboarding-state flag, the gate Bot API reads;
* **session authorized** — the session file exists and `get_me()` is the owner;
* **session connected** — transient, tracked here and mirrored into health.

Telethon is imported lazily, the same rule as `provisioning/mtproto.py`: unused,
the dependency need not exist. Nothing here imports the MAX client — an MTProto
handler that reached PyMax directly would be exactly the bypass the durable
pipeline exists to prevent, and a test guards it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import stat
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import tzinfo
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from bridge.config import TimestampStyle
from bridge.provisioning.mtproto import harden

logger = logging.getLogger(__name__)

#: The owner's read session, kept apart from the BotFather-provisioning session
#: (`SESSION_FILE`): different purpose, and a delete of one must not touch the
#: other. As sensitive as the account itself — 0600, in the secrets directory,
#: never in a backup or diagnostics bundle.
USER_SESSION_FILE = "telegram-user.session"

#: How long one QR code is offered before it is refreshed. Telegram expires the
#: login token on its own timetable; this only bounds how long a stale code sits
#: on screen before `recreate()` draws a fresh one.
QR_REFRESH_SECONDS = 30.0

#: How many times a QR is refreshed before giving up, so an unattended terminal
#: does not loop for ever.
QR_MAX_REFRESHES = 20

#: How many forwarded authors to remember. A dialog forwards from a handful of
#: places, so this is generous; the bound only stops a long-lived process from
#: growing a name cache without limit.
FORWARD_NAME_CACHE_LIMIT = 256

#: Console wording, shared by both frontends (the `telegram-sync` command and the
#: main bootstrap) so there is one voice, not two. It says what the session can
#: do and, plainly, the access it carries — neither hidden nor dramatised.
SYNC_HEADER = (
    "Telegram-синхронизация\n"
    "Telemax подключится как дополнительное устройство Telegram, чтобы видеть\n"
    "отправленные вами сообщения, изменения и удаления только в чатах Telemax."
)
SYNC_SCAN_HINT = (
    "Откройте Telegram:  Настройки → Устройства → Подключить устройство\n"
    "и наведите камеру на код."
)
SYNC_ACCESS_WARNING = (
    "Сохранённая сессия технически имеет доступ к Telegram-аккаунту. Telemax\n"
    "обрабатывает только чаты с созданными contact-ботами, но это локальное\n"
    "ограничение приложения. Держите сервер как доверенное устройство."
)
#: Said after a successful link, so nobody assumes the session started carrying
#: messages. It has not — the gate is a separate, probe-gated step.
SYNC_NOT_ENABLED_YET = (
    "Приём сообщений через эту сессию пока НЕ включён — это отдельный шаг после "
    "живой проверки."
)


#: Bot API entity names to the MTProto constructors that mean the same thing.
#: Both count offsets in UTF-16 code units, so the numbers travel unchanged and
#: only the wrapper differs — which is the whole of the conversion.
_ENTITY_TYPES: dict[str, str] = {
    "bold": "MessageEntityBold",
    "italic": "MessageEntityItalic",
    "underline": "MessageEntityUnderline",
    "strikethrough": "MessageEntityStrike",
    "code": "MessageEntityCode",
    "pre": "MessageEntityPre",
    "text_link": "MessageEntityTextUrl",
    "spoiler": "MessageEntitySpoiler",
    "blockquote": "MessageEntityBlockquote",
}


def mtproto_entities(entities: list[dict[str, Any]] | None) -> list[Any] | None:
    """Bot API formatting ranges as MTProto ones. Unknown kinds are dropped.

    Dropped rather than approximated: an entity Telegram refuses takes the whole
    message with it, and losing one bold range is not worth losing the line.
    """
    if not entities:
        return None
    from telethon.tl import types  # type: ignore[import-untyped]

    built: list[Any] = []
    for entity in entities:
        name = _ENTITY_TYPES.get(str(entity.get("type")))
        if name is None:
            continue
        factory = getattr(types, name, None)
        if factory is None:
            continue
        offset, length = int(entity.get("offset", 0)), int(entity.get("length", 0))
        if length <= 0:
            continue
        if name == "MessageEntityTextUrl":
            url = entity.get("url")
            if not url:
                continue
            built.append(factory(offset=offset, length=length, url=str(url)))
        elif name == "MessageEntityPre":
            built.append(factory(offset=offset, length=length, language=""))
        else:
            built.append(factory(offset=offset, length=length))
    return built or None


#: How long the watchdog waits before the first reconnect, and the ceiling it
#: backs off to. Unbounded in attempts on purpose — a transport being away is not
#: a message that can be given up on, and the gate keeps the Bot API path closed
#: for as long as this is down, so silently stopping would strand the owner.
RECONNECT_INITIAL_SECONDS = 5.0
RECONNECT_MAX_SECONDS = 300.0

#: How long the loop may sleep between passes of the periodic work, and the
#: shortest it will sleep however soon the next row claims to be due. The
#: ceiling is what stops a row inserted by a live handler waiting for a
#: reconnect; the floor is what stops one that keeps failing becoming a spin.
TICK_MAX_SECONDS = 20.0
TICK_MIN_SECONDS = 1.0


class SessionStatus(StrEnum):
    """What the owner's transport is doing, in the words `/status` needs.

    Four states and not a boolean, because "not connected" covered two conditions
    that call for opposite reactions. A dropped socket fixes itself and the owner
    need not hear about it; a revoked session never will, and every minute it goes
    unmentioned is a minute of the owner's messages being held back by a gate that
    is waiting for a transport that is not coming.
    """

    #: The client is up and the account is the owner's.
    CONNECTED = "connected"
    #: Down, and the watchdog is trying. The ordinary state after a network blip.
    RETRYING = "retrying"
    #: Terminal. The session file is gone, revoked, or belongs to someone else.
    #: Only a new `telegram-sync` fixes it, so retrying is noise.
    UNAUTHORIZED = "unauthorized"
    #: Terminal for a different reason: the process cannot even try — no API
    #: credentials, no session file to open. A configuration answer, not a login.
    FAILED = "failed"
    #: Deliberately closed, on shutdown.
    STOPPED = "stopped"


#: The statuses the watchdog stops on. Anything else is worth another attempt.
TERMINAL_STATUSES = frozenset({SessionStatus.UNAUTHORIZED, SessionStatus.FAILED})


class OwnerMismatchError(Exception):
    """The scanned account is not the Guardian owner. The session was discarded."""


class SessionUnauthorizedError(Exception):
    """The stored session will not authorise. Terminal until it is re-linked."""


class SessionUnusableError(Exception):
    """The session cannot be opened at all — credentials or files, not login."""


@dataclass(frozen=True, slots=True)
class AuthorizedOwner:
    """Who a verified session belongs to — for the console to show, not to store.

    Identity is never a second source of truth: `account_id` is proven equal to
    the Guardian's `owner_user_id` before this is built. The name and username
    are only to let the owner recognise the account they just linked.
    """

    account_id: int
    name: str | None
    username: str | None


class SessionHealth(Protocol):
    """The two health calls this transport makes — `HealthService` implements them."""

    async def note_tg_session_connected(self) -> None: ...

    async def note_tg_session_disconnected(self, error: str | None = None) -> None: ...


def user_session_path(secrets_dir: Path) -> Path:
    return secrets_dir / USER_SESSION_FILE


class ActiveBridges(Protocol):
    """The slice of `BridgeRepository` the allowlist needs."""

    async def active(self) -> list[Any]: ...


def contact_bot_allowlist(bridges: ActiveBridges) -> Callable[[], Awaitable[set[int]]]:
    """The set of contact-bot ids the session may observe, from live bridges.

    There is no second list of bots or contacts: the allowlist *is* the active
    bridge set, so a bridge disconnected mid-run stops being observed with no
    extra bookkeeping.
    """

    async def resolve() -> set[int]:
        return {
            int(record.telegram_bot_id)
            for record in await bridges.active()
            if getattr(record, "telegram_bot_id", None)
        }

    return resolve


def qr_ascii(url: str) -> str:
    """A login URL as a scannable block-character QR for the terminal.

    Pure and side-effect-free so it can be unit-tested without a login. `qrcode`
    is imported lazily, like Telethon: it is only needed while a human is at the
    console.
    """
    import qrcode  # type: ignore[import-untyped]

    code = qrcode.QRCode(border=2)
    code.add_data(url)
    code.make(fit=True)
    matrix = code.get_matrix()
    # Two rows per line via half-block glyphs keeps the square from being twice
    # as tall as it is wide in a terminal cell.
    lines: list[str] = []
    for top in range(0, len(matrix), 2):
        row = ""
        for col in range(len(matrix[top])):
            upper = matrix[top][col]
            lower = matrix[top + 1][col] if top + 1 < len(matrix) else False
            row += {(True, True): "█", (True, False): "▀", (False, True): "▄"}.get(
                (upper, lower), " "
            )
        lines.append(row)
    return "\n".join(lines)


TelethonFactory = Callable[[str, int, str], Any]


def _default_factory(session: str, api_id: int, api_hash: str) -> Any:
    from telethon import TelegramClient  # type: ignore[import-untyped]

    return TelegramClient(session, api_id, api_hash)


async def _account_id(client: Any) -> int | None:
    me = await client.get_me()
    value = getattr(me, "id", None)
    return int(value) if value is not None else None


async def _discard(client: Any, path: Path) -> None:
    """Log the account out, drop the connection, and delete the session file.

    Used both when the wrong account scans and when a stored session is revoked:
    a half-authorised file left on disk is worse than none.
    """
    with contextlib.suppress(Exception):
        await client.log_out()
    with contextlib.suppress(Exception):
        await client.disconnect()
    for candidate in (path, path.with_suffix(".session")):
        with contextlib.suppress(FileNotFoundError):
            candidate.unlink()


async def authorize_owner_session(
    *,
    api_id: int,
    api_hash: str,
    secrets_dir: Path,
    owner_user_id: int,
    on_qr: Callable[[str], Awaitable[None]] | Callable[[str], None],
    password_provider: Callable[[str], Awaitable[str]] | Callable[[str], str] | None = None,
    client_factory: TelethonFactory = _default_factory,
    refresh_seconds: float = QR_REFRESH_SECONDS,
    max_refreshes: int = QR_MAX_REFRESHES,
) -> AuthorizedOwner:
    """Bring up an owner session — reused if valid, QR-scanned if not — and verify it.

    Returns the authorised owner's identity. Guarantees on return: the session
    file belongs to `owner_user_id`, it is hardened to 0600, and the client is
    disconnected (the runtime opens its own connection later). Any other account
    is logged out and its file deleted before `OwnerMismatchError` is raised.

    `api_hash` is never logged, echoed or returned. The QR url reaches the caller
    only through `on_qr`, which the console renders and never persists.
    """
    # One directory creation while a human is at the console; not worth an async
    # filesystem layer, same calls the provisioning session makes. 0700 on the
    # directory, 0600 on the file (via `harden`): the session is a whole-account
    # credential and is treated like one.
    secrets_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    os.chmod(secrets_dir, stat.S_IRWXU)
    path = user_session_path(secrets_dir)
    client = client_factory(str(path.with_suffix("")), api_id, api_hash)
    await client.connect()

    try:
        if await client.is_user_authorized():
            owner = await _verify_or_discard(client, path, owner_user_id)
            logger.info("reused an existing Telegram user session")
            return owner
        await _run_qr(
            client,
            on_qr=on_qr,
            password_provider=password_provider,
            refresh_seconds=refresh_seconds,
            max_refreshes=max_refreshes,
        )
        owner = await _verify_or_discard(client, path, owner_user_id)
        logger.info("authorised a new Telegram user session")
        return owner
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()


async def _verify_or_discard(client: Any, path: Path, owner_user_id: int) -> AuthorizedOwner:
    me = await client.get_me()
    account_id = int(getattr(me, "id", 0) or 0)
    if account_id != owner_user_id:
        await _discard(client, path)
        raise OwnerMismatchError(
            "этот Telegram-аккаунт не совпадает с владельцем Telemax — сессия удалена"
        )
    harden(path)
    return AuthorizedOwner(
        account_id=account_id,
        name=(getattr(me, "first_name", None) or None),
        username=(getattr(me, "username", None) or None),
    )


async def _run_qr(
    client: Any,
    *,
    on_qr: Any,
    password_provider: Any,
    refresh_seconds: float,
    max_refreshes: int,
) -> None:
    from telethon import errors

    qr = await client.qr_login()
    for _ in range(max_refreshes):
        await _maybe_await(on_qr(qr.url))
        try:
            await qr.wait(refresh_seconds)
            return
        except errors.SessionPasswordNeededError:
            if password_provider is None:
                raise
            password = await _maybe_await(
                password_provider("Пароль двухфакторной аутентификации: ")
            )
            await client.sign_in(password=password)
            return
        except TimeoutError:
            await qr.recreate()
    raise TimeoutError("QR-код так и не отсканировали")


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


class TelegramUserSession:
    """The supervised transport. Stage 1: connect, verify, report health.

    Deliberately without update handlers yet — connecting is all it does, so
    nothing it can do reaches the delivery pipeline. Reconnection is the existing
    supervisor's job; `watch()` is the factory it registers, shaped like the MAX
    watchdog so the two read the same in `/status`.

    The `connect` callable is injected, and this object reads no gate of its own:
    the runtime only *auto-starts* it when intake is enabled, but the ability to
    open the client is not tied to that flag. A future probe command can build
    one of these with its own `connect` (over the same factory) and observe
    updates without touching `owner_mtproto_intake_enabled` and without creating
    a delivery job — which is exactly how the pre-flip live probe will run.
    """

    def __init__(
        self,
        *,
        connect: Callable[[], Awaitable[Any]],
        owner_user_id: int,
        health: SessionHealth,
        allowed_bot_ids: Callable[[], Awaitable[set[int]]],
        intake: Any = None,
        forward_authors: Any = None,
        on_connected: Callable[[Any], Awaitable[None]] | None = None,
        on_tick: Callable[[], Awaitable[float | None]] | None = None,
        watch_interval_seconds: float = 60.0,
        timestamp_style: TimestampStyle = TimestampStyle.COMPACT,
        timezone: tzinfo | None = None,
    ) -> None:
        self._connect = connect
        self._owner_user_id = owner_user_id
        self._health = health
        self._allowed_bot_ids = allowed_bot_ids
        # The normaliser owner-outgoing updates are handed to. None keeps the
        # session observe-only (Stage 1). It is called, never PyMax — the module
        # boundary a test enforces.
        self._intake = intake
        # Names a contact bot recorded on the way past. Optional: without it a
        # forward from a saved contact is marked with their username or not
        # at all, never with the owner's private label for them.
        self._forward_authors = forward_authors
        # Run once the client is up and the account is verified, with the client
        # itself. What it is for is the owner-message baseline: reading a
        # message needs a connected session, and deriving anything from an
        # update needs the baseline. Failures here are logged and never taken
        # out on the connection.
        self._on_connected = on_connected
        # Run on the reconnect loop's own schedule while the session is up. What
        # it is for is the owner-update inbox: a row that failed and reopened
        # must run when its backoff is due, not when Telegram next reconnects.
        # It returns how long to wait, so the loop sleeps until the next due row
        # rather than on a fixed tick.
        self._on_tick = on_tick
        self._wake = asyncio.Event()
        self._interval = watch_interval_seconds
        self._timestamp_style = timestamp_style
        self._timezone = timezone
        self._client: Any = None
        self._status = SessionStatus.RETRYING
        # Authors of forwarded messages, by peer id. A dialog forwards from a
        # handful of places, so the cache is nearly always warm after the first
        # message; the bound is only there so a long-lived process cannot grow
        # it without limit.
        self._forward_names: dict[int, Any] = {}

    @property
    def status(self) -> SessionStatus:
        return self._status

    @property
    def is_connected(self) -> bool:
        """Connected means the *client* says so, not that `start()` once returned.

        It used to be a bool this object set on the way up and cleared only on
        shutdown. So a session that died — revoked, or a socket Telethon gave up
        on — went on reporting itself healthy for the life of the process:
        `/status` said connected, the watchdog saw nothing to reconnect, and every
        placement failed into a fallback that no longer exists. Asking the client
        costs nothing and cannot drift.
        """
        client = self._client
        if client is None or self._status is not SessionStatus.CONNECTED:
            return False
        probe = getattr(client, "is_connected", None)
        if callable(probe):
            try:
                return bool(probe())
            except Exception:  # noqa: BLE001 - a client that cannot answer is not up
                return False
        # A client without the probe (a test double) is taken at its word: it was
        # opened, and nothing has said otherwise.
        return True

    @property
    def client(self) -> Any:
        """The live Telethon client, so the media source can re-fetch through it."""
        return self._client

    async def start(self) -> None:
        """Open the session, prove it is the owner's, and mark health connected.

        A wrong account here is terminal, not transient: the session is dropped
        and the transport stays down rather than reading a stranger's chats. So
        is a session that will not authorise — retrying it every five minutes
        would only fill the log while the owner waits for a prompt to re-link.
        """
        try:
            client = await self._connect()
        except SessionUnauthorizedError as error:
            await self._enter_terminal(SessionStatus.UNAUTHORIZED, error)
            raise
        except SessionUnusableError as error:
            await self._enter_terminal(SessionStatus.FAILED, error)
            raise
        except Exception as error:
            self._status = SessionStatus.RETRYING
            await self._health.note_tg_session_disconnected(f"{type(error).__name__}: {error}")
            raise

        try:
            account_id = await _account_id(client)
        except Exception as error:
            # `_connect()` handed back an open client, and the identity check is
            # the first high-level request over it — the one that updates the
            # session table and so is the first thing to fail on a locked file or
            # a socket that dropped between connect and now. Nothing below closes
            # a client we never stored, so without this disconnect each retry
            # leaks one sqlite handle; enough of them fighting over the write lock
            # is exactly what turns a one-off «database is locked» into a
            # permanent one.
            with contextlib.suppress(Exception):
                await client.disconnect()
            self._status = SessionStatus.RETRYING
            await self._health.note_tg_session_disconnected(f"{type(error).__name__}: {error}")
            raise
        if account_id != self._owner_user_id:
            with contextlib.suppress(Exception):
                await client.disconnect()
            mismatch = OwnerMismatchError("the Telegram user session is not the owner's")
            await self._enter_terminal(SessionStatus.UNAUTHORIZED, mismatch)
            raise mismatch

        self._client = client
        self._status = SessionStatus.CONNECTED
        if self._intake is not None:
            self._register_intake(client)
        if self._on_connected is not None:
            try:
                await self._on_connected(client)
            except Exception:
                logger.exception("the post-connect step failed; the session stays up")
        await self._health.note_tg_session_connected()

    async def _enter_terminal(self, status: SessionStatus, error: Exception) -> None:
        """Stop the watchdog and say so once, loudly enough to be acted on.

        The gate keeps the Bot API path closed while the owner's session is the
        authoritative intake, so a terminal session is not a quiet degradation:
        it is the owner's messages going nowhere. It has to be visible in
        `/status` rather than only in a log line nobody has open.
        """
        self._client = None
        self._status = status
        logger.error(
            "the Telegram user session is %s and will not be retried: %s", status.value, error
        )
        await self._health.note_tg_session_disconnected(f"{status.value}: {error}")

    async def _forward_line(self, client: Any, message: Any) -> str | None:
        """The whole forward line for a message the owner passed on, or None.

        Built here because this is the only layer holding all of it: the store a
        contact bot filled in, the session that can resolve a stranger, and the
        timestamp settings that decide whether the original's clock is worth
        drawing.
        """
        from bridge.formatting import forward_prefix
        from bridge.telegram.forwards import is_mtproto_forward, mtproto_forward_date

        if not is_mtproto_forward(message):
            return None
        author = await self._forward_author(client, message)
        return forward_prefix(
            author.name,
            username=author.username,
            at_ms=mtproto_forward_date(message),
            style=self._timestamp_style,
            tz=self._timezone,
        )

    async def _forward_author(self, client: Any, message: Any) -> Any:
        """Who wrote a forwarded message, as *they* call themselves.

        Never as the owner filed them. Everything this session can ask about a
        saved contact comes back under the owner's own address-book label —
        four server paths were tried and all four agreed (see `author_name_of`)
        — and that label is nobody's business but the owner's. It must not be
        what a contact on the other side of the bridge reads.

        So the order is: what a contact bot was told, then what the account
        itself says if it is safe to use, and nothing at all rather than a
        private label. A hidden sender skips all of it — there is no account to
        ask, only the name they carried at the time.

        Cached including the misses, and only the misses expire with the
        process: a name learned from a bot lives in the database, so a restart
        does not go back to guessing.
        """
        from bridge.telegram.forwards import (
            ForwardAuthor,
            author_of_entity,
            forward_peer_id,
            mtproto_forward_name,
        )

        peer_id = forward_peer_id(message)
        if peer_id is None:
            # A hidden sender: no account to ask, and the name they carried at
            # the time is not a label anyone applied — it is what Telegram sent.
            return ForwardAuthor(name=mtproto_forward_name(message))
        if peer_id in self._forward_names:
            return self._forward_names[peer_id]

        author = ForwardAuthor()
        if self._forward_authors is not None:
            with contextlib.suppress(Exception):
                # What a bot was told. A bot has no address book, so this is the
                # profile itself — the one thing that is genuinely the author's.
                learned = await self._forward_authors.author_of(peer_id)
                if learned is not None:
                    author = ForwardAuthor(name=learned[0], username=learned[1])

        if not author:
            try:
                author = author_of_entity(await client.get_entity(peer_id))
            except Exception:
                # Unresolvable peers are ordinary: a channel this account never
                # joined, a user it shares nothing with. The forward still
                # travels, marked as anonymous.
                logger.debug("could not resolve the author of a forward", exc_info=True)

        if author.name:
            # Only a real name is cached. A handle-only answer means no bot has
            # carried this person past yet, and caching it would keep the first
            # forward's degraded header for every forward after it.
            if len(self._forward_names) >= FORWARD_NAME_CACHE_LIMIT:
                self._forward_names.clear()
            self._forward_names[peer_id] = author
        return author

    def _register_intake(self, client: Any) -> None:
        """Register the owner-outgoing handler — new messages only, Inc 1.

        The allowlist is checked inside the normaliser, not the event filter, so
        it tracks the live bridge set. A raising handler is logged, not swallowed
        into the connection: the durable job (if one was created) is already on
        the queue, and Telethon does not tear the socket down on a handler error.
        """
        from telethon import events
        from telethon.tl import types

        from bridge.telegram.mtproto_media import (
            echo_album_part_of,
            echo_fingerprint_of,
            owner_message_from,
        )

        @client.on(events.NewMessage(outgoing=True))  # type: ignore[untyped-decorator]
        async def _owner_outgoing(event: Any) -> None:
            peer_id = getattr(getattr(event.message, "peer_id", None), "user_id", None)
            if peer_id is None:
                return  # a group or channel, not a contact-bot DM
            try:
                message = owner_message_from(
                    event.message,
                    account_id=self._owner_user_id,
                    peer_id=int(peer_id),
                    forward_line=await self._forward_line(client, event.message),
                )
                await self._intake.on_owner_message(message)
                # What it looked like at the moment it was sent. The first
                # update on it may be a reaction, and a reaction is read as a
                # difference from something.
                await self._intake.on_owner_sent(
                    account_id=self._owner_user_id,
                    bot_id=int(peer_id),
                    message=event.message,
                )
            except Exception:
                logger.exception("owner MTProto intake failed for one message")

        @client.on(events.Raw)  # type: ignore[untyped-decorator]
        async def _owner_edits_and_reactions(update: Any) -> None:
            """One update, both meanings.

            `UpdateEditMessage` is what Telegram sends for a text edit *and* for
            a reaction — `edit_date` is set either way and there is no flag
            between them. Which it was
            cannot be read off the update at all; it is a subtraction against
            durable state, and the dispatch behind the intake owns that.

            So this handler no longer filters to outgoing messages. A reaction
            the owner puts on a *contact's* message is theirs too, and it
            arrives on the incoming copy — which the old `MessageEdited(
            outgoing=True)` handler never saw, while a reaction on their own
            message it did see went into MAX as a pointless edit.
            """
            if not isinstance(update, types.UpdateEditMessage):
                return
            message = getattr(update, "message", None)
            peer_id = getattr(getattr(message, "peer_id", None), "user_id", None)
            if peer_id is None or getattr(message, "id", None) is None:
                return  # a group, a channel, or a service update with no message
            pts = getattr(update, "pts", None)
            if not isinstance(pts, int):
                # Refused rather than carried unversioned, for the same reason
                # an edit was: a version is what tells one event from another
                # that happens to look identical, and one this handler cannot
                # version did not come from where it must have.
                logger.error("owner MTProto update arrived with no pts; not carried")
                return
            try:
                if int(peer_id) not in await self._intake.allowed_bots():
                    return
                outgoing = bool(getattr(message, "out", False))
                text: str | None = None
                if outgoing:
                    owner = owner_message_from(
                        message,
                        account_id=self._owner_user_id,
                        peer_id=int(peer_id),
                        forward_line=await self._forward_line(client, message),
                    )
                    text = owner.text
                await self._intake.on_owner_update(
                    account_id=self._owner_user_id,
                    bot_id=int(peer_id),
                    message=message,
                    pts=pts,
                    text=text,
                    outgoing=outgoing,
                )
            except Exception:
                logger.exception("owner MTProto update intake failed for one message")

        @client.on(events.NewMessage(incoming=True))  # type: ignore[untyped-decorator]
        async def _contact_bot_echo(event: Any) -> None:
            # The bridge's own delivery, seen from the owner's side of the chat.
            # The peer is checked first and nothing is read until it passes: an
            # unrelated conversation must not have its text hashed, its media
            # touched or its content logged on the way to being ignored.
            peer_id = getattr(getattr(event.message, "peer_id", None), "user_id", None)
            if peer_id is None:
                return
            try:
                if int(peer_id) not in await self._intake.allowed_bots():
                    return
                await self._intake.on_contact_echo(
                    bot_id=int(peer_id),
                    account_id=self._owner_user_id,
                    message_id=int(event.message.id),
                    fingerprint=echo_fingerprint_of(event.message),
                    # An album arrives one part at a time and is bound as a
                    # group, so what is passed is the part's structure rather
                    # than a fingerprint it could never be matched by alone.
                    album=echo_album_part_of(event.message),
                )
                # A contact's message can be reacted to as well, and its
                # baseline has to exist before that reaction arrives.
                await self._intake.on_owner_sent(
                    account_id=self._owner_user_id,
                    bot_id=int(peer_id),
                    message=event.message,
                )
            except Exception:
                logger.exception("owner MTProto echo binding failed for one message")

        @client.on(events.Raw)  # type: ignore[untyped-decorator]
        async def _owner_reads(update: Any) -> None:
            # `UpdateReadHistoryInbox` is "I read this dialog", pushed to every
            # session of the account, so it fires whether the owner reads on the
            # phone or here. The outbox variant is the contact-bot reading ours
            # and is none of our business.
            if not isinstance(update, types.UpdateReadHistoryInbox):
                return
            bot_id = getattr(update.peer, "user_id", None)
            if bot_id is None:
                return  # a group or channel, not a contact-bot DM
            try:
                # `max_id` is in this account's own numbering — the ids the owner's
                # client shows, which the contact bot never sees. It travels with
                # the account it belongs to and is never called a "Telegram id"
                # unqualified, because that is the confusion it caused once.
                await self._intake.on_owner_read(
                    bot_id=int(bot_id),
                    owner_account_id=self._owner_user_id,
                    owner_message_id=int(update.max_id),
                )
            except Exception:
                logger.exception("owner MTProto read mark failed for one chat")

        @client.on(events.Raw)  # type: ignore[untyped-decorator]
        async def _owner_deletes(update: Any) -> None:
            # `UpdateDeleteMessages` is the private-chat deletion, and it carries
            # no peer at all — only owner-side ids. Channel deletions are a
            # different constructor with a channel_id and are not ours.
            if not isinstance(update, types.UpdateDeleteMessages):
                return
            ids = [int(m) for m in (update.messages or [])]
            pts = getattr(update, "pts", None)
            try:
                await self._intake.on_owner_delete(
                    account_id=self._owner_user_id,
                    message_ids=ids,
                    # `UpdateDeleteMessages` carries one `pts` for the whole
                    # batch. The message id is in the inbox key, so the targets
                    # do not collide on it.
                    pts=pts if isinstance(pts, int) else None,
                )
            except Exception:
                logger.exception("owner MTProto delete intake failed")

    async def send_own_message(
        self, peer_id: int, text: str, *, entities: list[dict[str, Any]] | None = None
    ) -> int | None:
        """Write one line into the owner's chat with a contact bot, as the owner.

        This is what replaced Secretary Mode for the owner's own words: the same
        message, sent by the account it belongs to, with no Premium, no business
        connection and nothing that can be revoked mid-conversation.

        **It has to be this session.** A session gets no update for a message it
        sent itself, so a placement made here is invisible to the intake handlers
        registered on this very client and cannot come back as a MAX duplicate.
        Sent from any other session of the same account it would be seen, carried
        into MAX, and the owner would read their own line twice.

        Returns the owner's own id for the message — the one
        `attach_owner_message` stores, and the only id this transport ever sees.
        The bot has a different number for the same message and learns it from
        its own copy.

        `parse_mode=None` for the same reason `edit_own_message` passes it: with
        a parse mode Telethon reads the text as markdown, so an asterisk in
        somebody's own words becomes formatting and the body Telegram stores is
        not the body that went in. It is also what makes the placement's own
        baseline honest — `content_fingerprint_of_placement` hashes these two
        arguments and claims they are what the message *is*.
        """
        client = self._require_client()
        sent = await client.send_message(
            peer_id,
            text,
            formatting_entities=mtproto_entities(entities) or [],
            parse_mode=None,
        )
        message_id = getattr(sent, "id", None)
        return int(message_id) if message_id is not None else None

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
    ) -> int | None:
        """One attachment, uploaded as the owner. Same session, same reason.

        The kind decides the attributes rather than the method, which is how
        MTProto works: a voice note, a round video and a plain file are all one
        document told apart by what is declared about them. Getting that wrong is
        how a voice message loses its waveform and a circle stops being round —
        the same failure the Bot API side spells out one method at a time.

        `thumbnail` is the upright cover the delivery drew. Without it Telegram
        makes its own out of the stored frame, and a phone video carries a
        rotation the stored frame does not — which is how a clip that plays the
        right way up ends up under a cover lying on its side. The bytes go
        straight to Telethon: it is given no `file_size`, so it measures the
        cover itself rather than believing the video's size.
        """
        from telethon.tl import types

        client = self._require_client()
        attributes: list[Any] = []
        force_document = False
        voice_note = False
        video_note = False

        if kind in ("video", "video_note"):
            attributes.append(
                types.DocumentAttributeVideo(
                    duration=int(duration or 0),
                    w=int(width or 0),
                    h=int(height or 0),
                    round_message=kind == "video_note",
                    supports_streaming=kind == "video",
                )
            )
            video_note = kind == "video_note"
        elif kind in ("voice", "audio"):
            attributes.append(
                types.DocumentAttributeAudio(
                    duration=int(duration or 0),
                    voice=kind == "voice",
                    title=title,
                    performer=performer,
                )
            )
            voice_note = kind == "voice"
        elif kind == "document":
            force_document = True

        sent = await client.send_file(
            peer_id,
            path,
            caption=caption,
            formatting_entities=mtproto_entities(entities),
            attributes=attributes or None,
            file_name=file_name,
            thumb=thumbnail,
            force_document=force_document,
            voice_note=voice_note,
            video_note=video_note,
        )
        message_id = getattr(sent, "id", None)
        return int(message_id) if message_id is not None else None

    def _require_client(self) -> Any:
        client = self._client
        if client is None:
            raise RuntimeError("the Telegram user session is not connected")
        return client

    async def send_own_album(
        self,
        peer_id: int,
        paths: list[str],
        *,
        caption: str | None = None,
        entities: list[dict[str, Any]] | None = None,
    ) -> list[int | None]:
        """One grouped upload, so the owner's own album stays one album.

        **The contract, read out of the installed Telethon 1.44.0 rather than
        assumed.** `send_file` with a list goes to `_send_album`, which uploads
        each file with `messages.uploadMedia` — that creates no message — and
        then makes exactly **one** `messages.sendMultiMedia` carrying all of
        them. So the group is atomic at the server: either every part exists or
        none does. Each part carries its own auto-generated `random_id`, and the
        answer is mapped back through them, which means the returned list is in
        *our file order* and may hold a `None` for a part Telegram did not name.
        Ten per request is Telethon's chunk size; this bridge never sends more.

        The caption travels as a plain string and lands on the first file, which
        is where Telegram shows an album's caption and where the aliases were
        written down expecting it. `parse_mode=None` is not optional: with empty
        entities Telethon falls back to the client's default parse mode and would
        read the timestamp stamp and a contact's asterisk as markdown.

        Returns the owner-side id of each part, positionally — `None` where the
        answer named none.
        """
        client = self._require_client()
        sent = await client.send_file(
            peer_id,
            list(paths),
            caption=caption or "",
            formatting_entities=mtproto_entities(entities) or [],
            parse_mode=None,
        )
        placed = sent if isinstance(sent, list) else [sent]
        return [
            int(message.id)
            if message is not None and getattr(message, "id", None) is not None
            else None
            for message in placed
        ]

    async def edit_own_message(
        self,
        peer_id: int,
        message_id: int,
        text: str,
        *,
        entities: list[dict[str, Any]] | None = None,
    ) -> None:
        """Change what one of the owner's own messages says.

        The second thing this session does that is not reading, and it is as
        narrow as the first: a message the *bridge* placed as the owner, whose id
        came back from that placement. The bot cannot do it — a bot may only edit
        its own messages, and the id it holds for an owner-authored message is
        its own view of somebody else's.

        `messages.editMessage` carries the new body whether the message is text
        or media, so one call covers both: on a photo the same field *is* the
        caption. Checked against the installed Telethon 1.44.0 rather than
        assumed — `edit_message` builds exactly that request and returns the
        edited message.

        `formatting_entities` is passed as a list even when it is empty, because
        `None` makes Telethon fall back to the client's parse mode and parse the
        text as markdown. A stamp like `[30/07 02:13]` is not markdown anybody
        wrote, and neither is a contact's name containing an asterisk.
        """
        client = self._require_client()
        await client.edit_message(
            peer_id,
            message_id,
            text,
            formatting_entities=mtproto_entities(entities) or [],
            parse_mode=None,
        )

    async def delete_own_messages(self, peer_id: int, message_ids: list[int]) -> None:
        """Remove messages from the owner's own chat with a contact bot.

        **This is the one thing the session does that is not reading.** It was
        deliberately observe-only until here, and the exception is narrow and has
        a reason: MAX cannot delete one attachment of an album, so a part deleted
        in Telegram takes the whole MAX message with it, and the parts still
        sitting in the Telegram chat would be a group whose counterpart no longer
        exists anywhere. Only the owner's own account can take those out — the
        bot never learns its own ids for messages the owner sent — so the session
        that already watches the chat is what removes them.

        Bounded on purpose: one peer, an explicit list of ids, and callers that
        can only build that list out of album parts the bridge itself mapped.
        `revoke=True` because a half-deleted album on the contact's side is the
        same untidiness one message further along.

        Raises when the session is not connected rather than reporting success:
        the caller is a durable job, and a retry once the session is back is the
        right outcome — not a silent skip.
        """
        client = self._client
        if client is None:
            raise RuntimeError("the Telegram user session is not connected")
        await client.delete_messages(peer_id, message_ids, revoke=True)

    async def stop(self, error: str | None = None) -> None:
        client, self._client = self._client, None
        self._status = SessionStatus.STOPPED
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()
        await self._health.note_tg_session_disconnected(error)

    async def allowed_peers(self) -> set[int]:
        """The contact-bot ids this session is allowed to observe.

        Straight from the live bridge repository — there is no second list of
        contacts or bots, and a bridge disconnected mid-run drops out of it.
        """
        return await self._allowed_bot_ids()

    def watch(self) -> Callable[[], Awaitable[None]]:
        """The reconnect loop, registered whether or not the first `start()` worked.

        That is the change that matters. It used to be registered only after a
        successful start, so a session that failed to come up at boot was never
        retried for the life of the process — while the gate went on closing the
        Bot API path against it. The owner's messages simply stopped, and the only
        trace was a counter.

        Backoff, not a fixed tick: a session that is refusing must not be asked
        every five seconds. A terminal status ends the loop — the supervisor
        treats a clean exit as a decision, which this is: nothing here can fix a
        revoked session, and `/status` says so instead.
        """

        async def loop() -> None:
            delay = RECONNECT_INITIAL_SECONDS
            while True:
                if self._status in TERMINAL_STATUSES:
                    return
                if self.is_connected:
                    delay = RECONNECT_INITIAL_SECONDS
                    await self._tick()
                    continue
                logger.warning("Telegram user session is down; reconnecting")
                try:
                    await self.start()
                except (SessionUnauthorizedError, SessionUnusableError, OwnerMismatchError):
                    return  # already recorded and already terminal
                except Exception:
                    logger.info("the Telegram user session did not come back", exc_info=True)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, RECONNECT_MAX_SECONDS)

        return loop

    def wake(self) -> None:
        """Ask the loop to tick now. Safe from any task, and never blocks.

        A row reopened with a short backoff would otherwise wait out a sleep
        computed before it existed.
        """
        self._wake.set()

    async def _tick(self) -> None:
        """One pass of the periodic work, then sleep until there is more to do.

        The bound matters in both directions. Without a ceiling a quiet bridge
        would not notice a row inserted by a live handler until the next
        reconnect — which is the defect this exists to fix. Without a floor a
        row that keeps failing would be a tight loop.
        """
        wait = self._interval
        if self._on_tick is not None:
            try:
                asked = await self._on_tick()
            except Exception:
                logger.exception("the owner session tick failed; the session stays up")
                asked = None
            wait = TICK_MAX_SECONDS if asked is None else max(
                TICK_MIN_SECONDS, min(asked, TICK_MAX_SECONDS)
            )
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=wait)
