"""The MAX side of the bridge: one session, one client, our own event types.

Four PyMax behaviours shape this file:

1. `start()` never returns — it is a reconnect loop. So it runs as a task and
   readiness is signalled from `on_start`.
2. `stop()` is a coroutine despite being annotated `-> None`.
3. **An exception escaping a handler tears down the connection.** Every callback
   is therefore wrapped; a bug in delivery must not disconnect the account.
4. Some typed methods disagree with the wire protocol, so typing and reactions
   go through `_app.invoke` with hand-built frames (see `opcodes.py`).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pymax import Client, ExtraConfig

from .events import (
    ChatReaction,
    IncomingMaxMessage,
    MaxContact,
    MessageDeleted,
    MessageReactions,
    PresenceUpdate,
    ReactionUpdate,
    ReadMark,
    TypingSignal,
    chat_reaction_from,
    contact_id_from,
    enum_text,
    normalize_contact,
    normalize_message,
    normalize_presence,
    reaction_from,
    reactions_by_message,
)
from .identity import MaxIdentity, load_or_create
from .interactive import InteractivePings, PresenceMode
from .native_state import (
    NativeMediaState,
    UploaderUrlError,
    check_upload_url,
    classify_native_error,
)
from .opcodes import (
    MediaUploadType,
    Opcode,
    TypingKind,
    add_reaction_frame,
    audio_sources_frame,
    circle_attach,
    file_download_frame,
    get_reactions_frame,
    media_upload_frame,
    message_time_ms,
    native_media_frame,
    presence_frame,
    read_frame,
    remove_reaction_frame,
    sticker_message_frame,
    typing_frame,
    video_sources_frame,
    voice_attach,
)
from .pymax_compat import relaxed_fields, typed_message_error
from .session import ensure_session_dir, harden_session_files
from .state_files import read_with_backup, write_atomic
from .wire_vocabulary import keys_the_app_cannot_read

logger = logging.getLogger(__name__)

#: Guards the one-time line about what `pymax_compat` relaxed.
_relaxation_announced = False

#: Headers required by MAX web-compatible uploads.
UPLOAD_HEADERS = {"Origin": "https://web.max.ru", "Referer": "https://web.max.ru/"}

#: Defaults for clients constructed without an explicit `NativeMediaState`.
#: Running services read the actual switches from `max.native_media`.
NATIVE_MEDIA_ENABLED = True

#: Native voice and circle can be disabled independently because MAX validates
#: the two attachment kinds separately.
NATIVE_VOICE_ENABLED = True
NATIVE_CIRCLE_ENABLED = True

#: Compatibility floor for the native voice and video-note upload endpoints.
MIN_NATIVE_MEDIA_APP_VERSION = (26, 16)

#: Shared protocol compatibility version. Per-install device identity is stored
#: separately and remains stable across restarts.
NATIVE_MEDIA_APP_VERSION = "26.23.1"

#: Build paired with `NATIVE_MEDIA_APP_VERSION` in the MAX handshake.
NATIVE_MEDIA_BUILD_NUMBER = 6778


#: Where the launch counter lives, next to the session db.
CLIENT_SESSION_COUNTER = "client_session_id"


def next_client_session_id(session_dir: Path) -> int:
    """Return and persist a monotonic session launch counter.

    Atomic writes and a backup prevent a torn file from resetting the counter.
    """
    counter = session_dir / CLIENT_SESSION_COUNTER
    stored, from_backup = read_with_backup(counter, lambda raw: int(raw.strip()))
    if from_backup:
        # The backup holds the value from *before* the last increment, so the
        # count may repeat one launch rather than skipping one. Repeating is the
        # cheaper lie: a counter that stalls looks like a process that restarted
        # twice, and one that jumps looks like a different install.
        logger.warning("the MAX session counter was restored from its copy")
    current = (stored or 0) + 1
    try:
        write_atomic(counter, f"{current}\n")
    except OSError:
        logger.debug("could not persist the client session counter", exc_info=True)
    return current


class MaxStickerError(Exception):
    """A sticker could not be uploaded to MAX or created there."""


class MaxMediaError(Exception):
    """A native voice or video note could not be uploaded to MAX or sent.

    Raised only where the message provably does not exist yet: no upload slot, an
    uploader we have no path for, a file we could not read a duration or waveform
    from, or an upload the CDN refused. The upload itself creates nothing — only
    op64 does — so falling through to a plain attachment on any of these cannot
    put a second copy anywhere.
    """


class MaxNativeRejectedError(MaxMediaError):
    """MAX *answered* op64 with a refusal of the attach we built.

    A subclass because the outcome for this message is the same — degrade to a
    plain attachment, nothing was created — but the outcome for the *feature* is
    not: a refusal of our frame also drops the connection, so it trips the
    breaker for that kind (see `native_state`).
    """


class MaxUnconfirmedSendError(Exception):
    """op64 was written to the socket and no answer came back.

    Deliberately **not** a `MaxMediaError`: there is no safe fallback from here.
    The message may be in the chat, so sending a plain attachment as well is how
    a person receives it twice, and retrying the native send is the same bet.
    `send_media` lets this through untouched and the delivery layer turns it into
    AMBIGUOUS, which is what ADR 0002 exists for.
    """


def _render_sticker_png(source: Path) -> tuple[bytes, str]:
    """Convert a Telegram sticker to PNG bytes and hash them. Blocking on purpose."""
    from bridge.media.stickers import sticker_to_png

    target = source.with_suffix(".max.png")
    try:
        sticker_to_png(source, target)
        payload = target.read_bytes()
    finally:
        target.unlink(missing_ok=True)
    return payload, hashlib.sha256(payload).hexdigest()


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StickerCache(Protocol):
    """Where a PNG's MAX sticker id is remembered between sends."""

    async def get(self, png_sha256: str) -> int | None: ...

    async def put(self, png_sha256: str, max_sticker_id: int) -> None: ...


class StickerOrigins(Protocol):
    """Which MAX sticker a file the bridge handed Telegram originally was."""

    async def get(self, tg_sha256: str) -> int | None: ...


def _message_id_of(answer: Any) -> Any:
    """`payload["message"]["id"]` out of an op64 reply, or None.

    One reader for every hand-built send. It used to be written out at each call
    site, and the copies had drifted: a contact with an unreadable id came back
    as `None` (which the delivery layer reads as "unknown, ask the owner") while
    a sticker with the same answer raised `ValueError` (which it reads as
    "retry", and a retried sticker is a second sticker).
    """
    message = answer.get("message") if isinstance(answer, dict) else None
    return message.get("id") if isinstance(message, dict) else None


def _as_optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


EventT = TypeVar("EventT")
Handler = Callable[[EventT], Awaitable[None]]

# How long to wait for the first `on_start` before deciding the session is bad.
START_TIMEOUT_SECONDS = 60.0


#: One request's worth of history. MAX's own default is around forty; asking
#: for more in one go is what makes a thousand-message re-pull two dozen round
#: trips instead of hundreds.
MAX_HISTORY_PAGE = 200

#: The ceiling on "all of it". Not a limit anybody should hit — it is the guard
#: against a chat with a decade in it turning one tap into an hour of paging.
MAX_HISTORY_MESSAGES = 20_000


def _attachment_shapes(body: dict[str, Any]) -> str:
    """`VIDEO(duration,height,videoId,width)` — types and field names, no values.

    What a warning about an untypeable message has to say to be worth reading:
    *which* attachment, and which keys it came with. The values are exactly what
    must not be logged — a MAX attach carries CDN tokens — and the missing key
    is visible from the names alone.
    """
    attaches = body.get("attaches")
    if not isinstance(attaches, list) or not attaches:
        return "none"
    described = []
    for item in attaches:
        if not isinstance(item, dict):
            described.append(type(item).__name__)
            continue
        kind = enum_text(item.get("_type") or item.get("type")) or "?"
        described.append(f"{kind}({','.join(sorted(item))})")
    return ", ".join(described)


def _keys_beyond_the_app(body: dict[str, Any]) -> list[str]:
    """Keys in these attachments that the MAX app's own reader would skip.

    A missing field says MAX dropped something; this says MAX *added* something
    its own client cannot read yet, which is a different and rarer event — the
    protocol moving ahead of every parser at once, ours and theirs. Normally
    empty, which is what makes it worth printing when it is not.
    """
    attaches = body.get("attaches")
    if not isinstance(attaches, list):
        return []
    novel: set[str] = set()
    for item in attaches:
        if isinstance(item, dict):
            novel.update(keys_the_app_cannot_read(item))
    return sorted(novel)


def _history_key(item: Any) -> Any:
    """What makes two history entries the same one. Falls back to identity."""
    for name in ("id", "message_id", "messageId"):
        found = getattr(item, name, None)
        if found is None and isinstance(item, dict):
            found = item.get(name)
        if found is not None:
            return found
    return id(item)


def _history_time(item: Any) -> int | None:
    """When MAX says it happened, in milliseconds, or None."""
    for name in ("time", "timestamp", "created_at"):
        found = getattr(item, name, None)
        if found is None and isinstance(item, dict):
            found = item.get(name)
        if isinstance(found, int | float) and found > 0:
            return int(found)
    return None


async def _page_history(client: Any, chat_id: int, limit: int | None) -> list[Any]:
    """Walk back through MAX history until it stops giving anything new."""
    wanted = limit if limit is not None else MAX_HISTORY_MESSAGES
    page = min(MAX_HISTORY_PAGE, wanted)
    collected: list[Any] = []
    seen: set[Any] = set()
    from_time: int | None = None

    while len(collected) < wanted:
        try:
            batch = await client.fetch_history(
                chat_id=chat_id, backward=page, from_time=from_time
            )
        except TypeError:
            # An older PyMax without the paging arguments. One page, as before.
            batch = await client.fetch_history(chat_id=chat_id)
            batch = list(batch or [])
            collected.extend(item for item in batch if id(item) not in seen)
            break
        batch = list(batch or [])
        fresh = [item for item in batch if _history_key(item) not in seen]
        if not fresh:
            break
        seen.update(_history_key(item) for item in fresh)
        collected.extend(fresh)
        stamps = [
            stamp for item in fresh if (stamp := _history_time(item)) is not None
        ]
        if not stamps:
            break
        from_time = min(stamps)
    return collected


class MaxClientError(Exception):
    """The MAX side is unusable — bad session, or it never came up."""


class MaxClient:
    """Everything the rest of the bridge is allowed to know about MAX."""

    def __init__(
        self,
        *,
        phone: str,
        session_dir: Path,
        session_name: str,
        client_factory: Callable[..., Any] = Client,
        stickers: StickerCache | None = None,
        sticker_origins: StickerOrigins | None = None,
        timezone: str | None = None,
        native_media: NativeMediaState | None = None,
        own_presence: PresenceMode = PresenceMode.MIRROR,
        own_presence_idle_seconds: float = 90.0,
    ) -> None:
        self._phone = phone
        self._session_dir = session_dir
        self._session_name = session_name
        self._client_factory = client_factory
        # Only consulted the first time an identity is drawn: a user in
        # Novosibirsk whose phone reports Moscow is a free inconsistency.
        self._timezone = timezone
        self._identity: MaxIdentity | None = None
        # Set when the identity file had to be recovered or replaced. A machine
        # that stopped badly enough to need that is worth a line in `/status`,
        # not only a line in the journal somebody would have to go looking for.
        self._identity_repair: str | None = None
        # Counters and the per-kind breaker. The runtime builds one and shares it
        # with health; a client built without one (onboarding, tests) gets its
        # own, which is right — nothing reads it there.
        self._native = (
            native_media
            if native_media is not None
            else NativeMediaState(
                enabled=NATIVE_MEDIA_ENABLED,
                voice_enabled=NATIVE_VOICE_ENABLED,
                circle_enabled=NATIVE_CIRCLE_ENABLED,
            )
        )
        # Optional: without it every sticker sent creates a fresh one in the
        # owner's MAX collection, which works but litters.
        self._stickers = stickers
        # Optional: without it a MAX sticker sent back from Telegram is rebuilt
        # as a flat copy rather than returning as the original.
        self._sticker_origins = sticker_origins

        self._client: Any | None = None
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        # A login that is *refused* (a wrong code, a revoked session) must not
        # look like a slow one: waiting out the whole timeout to say "did not
        # come up" is useless to somebody typing a code into a chat.
        self._finished = asyncio.Event()
        self._failure: str | None = None
        self._stopping = False

        self._on_message: Handler[IncomingMaxMessage] | None = None
        self._on_edit: Handler[IncomingMaxMessage] | None = None
        self._on_delete: Handler[MessageDeleted] | None = None
        self._on_typing: Handler[TypingSignal] | None = None
        self._on_read: Handler[ReadMark] | None = None
        self._on_reaction: Handler[ReactionUpdate] | None = None
        self._on_chat_reaction: Handler[ChatReaction] | None = None
        self._on_presence: Handler[PresenceUpdate] | None = None
        self._on_reconnect: Callable[[], Awaitable[None]] | None = None
        self._connected_once = False

        # A socket held open around the clock is what made the owner permanently
        # «В сети»; this owns the one field that decides otherwise. See
        # `interactive.py` for what was measured before it was written.
        self._pings = InteractivePings(
            mode=own_presence, idle_seconds=own_presence_idle_seconds
        )

    # ------------------------------------------------------------- registration

    def on_message(self, handler: Handler[IncomingMaxMessage]) -> None:
        self._on_message = handler

    def on_message_edit(self, handler: Handler[IncomingMaxMessage]) -> None:
        self._on_edit = handler

    def on_message_delete(self, handler: Handler[MessageDeleted]) -> None:
        self._on_delete = handler

    def on_reconnect(self, handler: Callable[[], Awaitable[None]]) -> None:
        """Called after every reconnect, not on the first connect.

        PyMax's start() is a reconnect loop and re-emits on_start each time, so
        this is where a backfill belongs: whatever arrived while the socket was
        down was never delivered as an event.
        """
        self._on_reconnect = handler

    def on_typing(self, handler: Handler[TypingSignal]) -> None:
        self._on_typing = handler

    def on_read(self, handler: Handler[ReadMark]) -> None:
        self._on_read = handler

    def on_reaction(self, handler: Handler[ReactionUpdate]) -> None:
        self._on_reaction = handler

    def on_chat_reaction(self, handler: Handler[ChatReaction]) -> None:
        """A dialog's reaction fields changed (opcode 135).

        This — not `on_reaction` — is what fires when the contact reacts in a
        private dialog. Opcode 155 stays registered for group chats and for
        whatever else the server may still push it for.
        """
        self._on_chat_reaction = handler

    def on_presence(self, handler: Handler[PresenceUpdate]) -> None:
        """A contact came online or went quiet (the presence status)."""
        self._on_presence = handler

    # ------------------------------------------------------------------ lifecycle

    @property
    def own_user_id(self) -> int | None:
        profile = getattr(self._client, "me", None)
        contact = getattr(profile, "contact", None)
        user_id = getattr(contact, "id", None)
        return int(user_id) if user_id is not None else None

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    @property
    def identity_repair(self) -> str | None:
        """Why the identity file had to be recovered on this start, if it did."""
        return self._identity_repair

    def _note_identity_repair(self, reason: str) -> None:
        self._identity_repair = reason

    @staticmethod
    def _say_what_was_relaxed() -> None:
        """Name the requirements dropped from PyMax's models, once, out loud.

        The sweep itself runs at import, before logging is configured, so its
        own line would go nowhere — and a repair nobody can see is how the
        outage this all came from stayed invisible for three hours. Said here
        instead, where a session is being opened and the log exists.
        """
        global _relaxation_announced
        if _relaxation_announced:
            return
        _relaxation_announced = True
        relaxed = relaxed_fields()
        if relaxed:
            logger.info(
                "%s attachment field(s) PyMax requires and MAX does not are optional here: %s",
                len(relaxed),
                ", ".join(relaxed),
            )

    async def start(self, *, timeout: float = START_TIMEOUT_SECONDS) -> None:  # noqa: ASYNC109
        """Connect and wait until the session is usable."""
        ensure_session_dir(self._session_dir)
        self._say_what_was_relaxed()

        # Drawn once for this install and read from disk ever after, so the
        # account keeps reporting the same phone. `app_version` is the exception:
        # it lives in code because it is what op82 keys on when choosing between
        # the ONE_ME uploader (which can carry a voice and a circle) and the OK
        # CDN (which the bridge has no upload path for).
        identity = load_or_create(
            self._session_dir,
            timezone=self._timezone,
            session_exists=(self._session_dir / self._session_name).exists(),
            on_repair=self._note_identity_repair,
        )
        self._identity = identity

        client = self._client_factory(
            phone=self._phone,
            work_dir=str(self._session_dir),
            session_name=self._session_name,
            extra_config=ExtraConfig(
                user_agent=identity.user_agent(
                    app_version=NATIVE_MEDIA_APP_VERSION,
                    build_number=NATIVE_MEDIA_BUILD_NUMBER,
                ),
                device_id=identity.device_id,
            ),
        )
        # `clientSessionId` is the one identity field ExtraConfig has no slot for,
        # so it is set on the device config PyMax just built. Without this it is a
        # fresh `randint(1, 70)` on every connect.
        try:
            client._config.device.client_session_id = next_client_session_id(self._session_dir)
        except AttributeError:  # a stubbed client in the tests
            logger.debug("client has no device config to pin the session counter on")
        self._client = client
        self._stopping = False
        self._ready.clear()
        self._finished.clear()
        self._failure = None
        self._install_handlers(client)

        # start() blocks for the lifetime of the connection, so it owns a task.
        self._task = asyncio.create_task(self._run(client), name="max-client")

        await self._await_ready(timeout)
        harden_session_files(self._session_dir)

    async def _await_ready(self, timeout: float) -> None:  # noqa: ASYNC109
        """Wait for the first `on_start`, or for the attempt to fail."""
        ready = asyncio.create_task(self._ready.wait())
        finished = asyncio.create_task(self._finished.wait())
        try:
            _, pending = await asyncio.wait(
                {ready, finished}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
        finally:
            ready.cancel()
            finished.cancel()

        if self._ready.is_set():
            return

        failure = self._failure
        await self.stop()
        if failure is not None:
            raise MaxClientError(failure)
        raise MaxClientError(f"MAX session did not come up within {timeout:.0f}s")

    async def _run(self, client: Any) -> None:
        try:
            await client.start()
        except asyncio.CancelledError:
            # stop() cancels the receive loop; this is the normal exit path.
            raise
        except Exception as error:
            # The text, not the object: it is handed to a person, and the
            # traceback may carry a phone number through the exception context.
            self._failure = f"{type(error).__name__}: {error}"[:200]
            logger.exception("MAX client stopped with an error")
        finally:
            self._ready.clear()
            self._finished.set()

    async def stop(self) -> None:
        self._stopping = True
        await self._pings.close()
        client, self._client = self._client, None
        task, self._task = self._task, None

        if client is not None:
            try:
                await client.stop()
            except Exception:
                logger.debug("MAX client stop() complained", exc_info=True)

        if task is not None and not task.done():
            task.cancel()
            with_suppressed = (asyncio.CancelledError,)
            try:
                await task
            except with_suppressed:
                pass

        harden_session_files(self._session_dir)
        self._ready.clear()

    # -------------------------------------------------------------------- events

    def _install_handlers(self, client: Any) -> None:
        @client.on_start()  # type: ignore[untyped-decorator]
        async def _started(*_: object) -> None:
            self._ready.set()
            logger.info("MAX session ready (user id %s)", self.own_user_id)

            # Every connect is a login, and a login declares the session
            # interactive — so the flag has to be taken over again each time,
            # not once at construction.
            self._pings.install(client)

            reconnected = self._connected_once
            self._connected_once = True
            if reconnected and self._on_reconnect is not None:
                try:
                    await self._on_reconnect()
                except Exception:
                    # A failed backfill must not take the connection down with it.
                    logger.exception("history resync after reconnect failed")

        @client.on_message()  # type: ignore[untyped-decorator]
        async def _message(event: Any, *_: object) -> None:
            await self._safely(
                "message",
                self._on_message,
                lambda: normalize_message(event, own_user_id=self.own_user_id),
            )

        @client.on_message_edit()  # type: ignore[untyped-decorator]
        async def _edited(event: Any, *_: object) -> None:
            await self._safely(
                "edit",
                self._on_edit,
                lambda: normalize_message(event, own_user_id=self.own_user_id),
            )

        @client.on_message_delete()  # type: ignore[untyped-decorator]
        async def _deleted(event: Any, *_: object) -> None:
            await self._safely(
                "delete",
                self._on_delete,
                lambda: MessageDeleted(
                    chat_id=int(event.chat_id),
                    message_ids=tuple(int(item) for item in event.message_ids),
                ),
            )

        @client.on_typing()  # type: ignore[untyped-decorator]
        async def _typing(event: Any, *_: object) -> None:
            await self._safely(
                "typing",
                self._on_typing,
                lambda: TypingSignal(chat_id=int(event.chat_id), user_id=int(event.user_id)),
            )

        @client.on_message_read()  # type: ignore[untyped-decorator]
        async def _read(event: Any, *_: object) -> None:
            await self._safely(
                "read",
                self._on_read,
                lambda: ReadMark(
                    chat_id=int(event.chat_id),
                    user_id=int(event.user_id),
                    mark=int(event.mark),
                    is_own=self.own_user_id is not None and int(event.user_id) == self.own_user_id,
                    set_as_unread=bool(getattr(event, "set_as_unread", False)),
                ),
            )

        @client.on_presence()  # type: ignore[untyped-decorator]
        async def _presence(event: Any, *_: object) -> None:
            await self._safely("presence", self._on_presence, lambda: normalize_presence(event))

        # Reactions are read from the raw frame, not through
        # `@client.on_reaction_update()`. PyMax's `ReactionUpdateEvent` declares
        # `messageId: str` and the server sends a number, so `model_validate`
        # raises — inside PyMax's own dispatcher, which turns it into
        # `RuntimeError: Failed to dispatch inbound frame` and takes the whole
        # frame down. The typed handler is therefore never reached at all.
        #
        # This is the same field, the same library, the wrong way round from the
        # outgoing bug already known: 178/179 have to be sent with a *numeric*
        # id or the server closes the socket.
        self._intercept_reactions(client)

    def _intercept_reactions(self, client: Any) -> None:
        """Handle opcode 155 ourselves, before PyMax tries to type it.

        Wrapping `on_event` rather than registering a handler: the parse that
        fails happens *inside* dispatch, so anything registered downstream of it
        never runs. Every other opcode is passed straight through untouched.

        Two things about *where* the hook goes, both learned the hard way:

        * it goes on the **connection**, not on the app. `App.__init__` does
          `self.connection.on_event = self.on_event`, copying the bound method
          once at construction — so replacing `app.on_event` afterwards patches
          an object nothing calls, and the frames keep going to the original.
        * it is reinstalled by `_build_app`. `start()` is a reconnect loop, and
          `_reset_runtime()` builds a fresh connection *and* a fresh app on the
          way round, so a one-time patch works until the first blip.

        Both failures look identical from outside: reactions simply never
        arrive, with nothing in the log to say why.
        """
        installed = self._install_reaction_hook(getattr(client, "_app", None))
        builder = getattr(client, "_build_app", None)
        if builder is not None:

            def build_app() -> Any:
                app = builder()
                self._install_reaction_hook(app)
                return app

            client._build_app = build_app
            installed = True

        if not installed:  # pragma: no cover - PyMax shape changed
            logger.warning("cannot intercept reaction frames; reactions will not arrive")

    def _install_reaction_hook(self, app: Any) -> bool:
        """Put our handler in front of the one the connection actually calls."""
        target = getattr(app, "connection", None) or app
        original = getattr(target, "on_event", None)
        if original is None or getattr(original, "_telemax_wrapped", False):
            return original is not None

        async def on_event(frame: Any) -> None:
            opcode = int(getattr(frame, "opcode", 0) or 0)
            # Every inbound opcode, at debug. Cheap, and the only way to find
            # out what MAX actually pushes for an event it has no documentation
            # for — guessing the number is how this took three attempts.
            logger.debug("inbound frame opcode=%s", opcode)
            if opcode == Opcode.NOTIF_CHAT:
                # Not a `return`: a chat update means other things too, and
                # PyMax's own chat handling still has to see the frame.
                await self._safely(
                    "chat reaction",
                    self._on_chat_reaction,
                    lambda: chat_reaction_from(frame.payload),
                )
            if opcode == Opcode.REACTION_UPDATE:
                logger.debug("reaction frame intercepted")
                await self._safely(
                    "reaction", self._on_reaction, lambda: reaction_from(frame.payload)
                )
                return
            if opcode in (Opcode.NOTIF_MESSAGE, Opcode.MSG_EDIT):
                # Also not a `return`: PyMax gets the frame either way. This only
                # covers the case where it would get it and do nothing.
                await self._rescue_untyped_message(frame)
            await original(frame)

        on_event._telemax_wrapped = True  # type: ignore[attr-defined]
        target.on_event = on_event
        return True

    async def _rescue_untyped_message(self, frame: Any) -> None:
        """Carry a message PyMax's typed dispatch is about to drop in silence.

        The failure this exists for is not an error anywhere: `resolve_message`
        answers a `ValidationError` with a debug line and a `None`, so the frame
        is gone and every layer below it — the claim, the queue, `/failed`, the
        guardian — is never told a message existed. Without raw event handling,
        a contact's video note can fail to arrive with nothing in the log
        said so, and a restart's history re-read delivered it three hours late.

        The strictness is not one field either. `Attachment` is
        `KnownAttachment | UnknownAttachment` and `UnknownAttachment` *refuses*
        a known `_type`, so an attach PyMax half-recognises cannot degrade — it
        takes the whole message down with it. Which field a given build of MAX
        leaves out is therefore the wrong thing to chase: this covers the class.

        Order matters. PyMax is asked first and left alone when it is happy, so
        the ordinary path is untouched and nothing is delivered twice; only the
        branch that used to end in nothing ends here instead. Our own
        `normalize_message` reads the raw dict and classifies the attachments
        itself, so it does not care which field PyMax wanted.

        Nothing raises out of here — rule 3 of this module. This runs *inside*
        PyMax's dispatch, where an exception becomes `RuntimeError: Failed to
        dispatch inbound frame` and takes the account's connection with it. A
        bug in the rescue must cost the one message it was rescuing.
        """
        try:
            await self._rescue(frame)
        except Exception:
            logger.exception("could not carry a MAX message PyMax refused to type")

    async def _rescue(self, frame: Any) -> None:
        """The rescue itself. Its caller is where the reasons are."""
        payload = getattr(frame, "payload", None)
        if not isinstance(payload, dict):
            return
        body = payload.get("message")
        if not isinstance(body, dict):
            body = payload
        message_id = _as_optional_int(body.get("id"))
        if message_id is None:
            # Not a message we could name. Nothing to rescue, and nothing worth
            # a warning: a frame that carries no id is not the failure above.
            return
        reason = typed_message_error(payload)
        if reason is None:
            return

        # The envelope holds the chat, the body usually does not — and PyMax's
        # own unwrapping reads it from exactly this key.
        chat_id = _as_optional_int(payload.get("chatId")) or _as_optional_int(body.get("chatId"))
        novel = _keys_beyond_the_app(body)
        logger.warning(
            "PyMax cannot type MAX message %s in chat %s (%s); attaches=%s%s."
            " Carried from the raw frame instead of being dropped",
            message_id,
            chat_id,
            reason,
            _attachment_shapes(body),
            f"; keys the MAX app itself would skip: {', '.join(novel)}" if novel else "",
        )

        status = enum_text(body.get("status"))
        if "REMOV" in status or "DELET" in status:
            await self._safely(
                "delete",
                self._on_delete,
                lambda: MessageDeleted(
                    chat_id=int(chat_id or 0), message_ids=(int(message_id),)
                ),
            )
            return
        edited = "EDIT" in status
        await self._safely(
            "edit" if edited else "message",
            self._on_edit if edited else self._on_message,
            lambda: normalize_message(body, own_user_id=self.own_user_id, chat_id=chat_id),
        )

    async def _safely(
        self,
        label: str,
        handler: Handler[Any] | None,
        build: Callable[[], Any],
    ) -> None:
        """Normalise, dispatch, and swallow everything that goes wrong.

        This is the single most important defensive block in the project: PyMax
        wraps a handler exception in RuntimeError and drops the connection, so a
        malformed payload or a bug downstream would log the account out.
        """
        if handler is None:
            return
        try:
            event = build()
        except Exception:
            logger.exception("failed to normalise a MAX %s event", label)
            return
        try:
            await handler(event)
        except Exception:
            logger.exception("handler for MAX %s event failed", label)

    # ------------------------------------------------------------------- actions

    def _require(self) -> Any:
        if self._client is None or not self._ready.is_set():
            raise MaxClientError("MAX client is not connected")
        return self._client

    def _owner_acted(self) -> None:
        """The owner just did something a contact can see.

        Only calls the owner actually causes belong here — a send, an edit, a
        reaction, a typing burst. Reads are deliberately left out: `auto_read`
        can be set to mark on *delivery*, which would light the account up for
        every incoming message and put the permanent «В сети» straight back.
        Polls and history fetches are the bridge talking to itself.
        """
        self._pings.touch()

    async def _invoke(
        self,
        opcode: Opcode,
        payload: dict[str, Any],
        *,
        timeout: float = 20,  # noqa: ASYNC109 - per-call protocol timeout, not a cancel scope
    ) -> Any:
        """Raw protocol call, for the frames PyMax gets wrong or does not have."""
        client = self._require()
        frame = await asyncio.wait_for(client._app.invoke(int(opcode), payload), timeout)
        return getattr(frame, "payload", frame)

    async def _run_creating_call(
        self,
        *,
        what: str,
        call: Callable[[], Awaitable[Any]],
        extract_message_id: Callable[[Any], Any],
        on_api_error: Callable[[Any], None] | None = None,
        on_unconfirmed: Callable[[], None] | None = None,
    ) -> int:
        """The one boundary every message-creating MAX call goes through.

        "Creating" is the whole distinction: these put a bubble in somebody's
        chat, so a second attempt is a second message. Editing, deleting, marking
        read, setting a reaction and typing are not here, and must not be — they
        replace state rather than add to it, so retrying them is free and turning
        them into questions for the owner would be noise.

        Four outcomes, told apart by what can actually be proved.

        * **Provably not sent.** `_require()` runs before the call is even built,
          so a session that is already down cannot have written anything. That
          stays an ordinary retry, and it is the only failure here that does.
        * **The server answered with a refusal.** An `ApiError` *is* an answer:
          the message was not created. `on_api_error` lets a caller act on that
          — native media trips its breaker — and anything it does not raise for
          travels on unchanged, to be classified by the delivery layer.
        * **Cancellation.** Passed through: the settlement policy decides, and it
          has the one fact this layer does not — whether the job had already
          marked its remote boundary.
        * **Everything else.** `transport.send` and the wait for a reply raise
          the same exception types, and our own `wait_for` can fire while the
          write is still in progress, so "the frame did not land" is not provable
          from here for any of them. Unconfirmed, therefore, which means the
          owner decides (ADR 0002) — never a retry, never a fallback.

        A reply that carries no usable id lands in the same place. The frame was
        accepted and we cannot name what it created, which is exactly as unknown
        as silence. Ids are validated as positive: MAX ids are large positive
        longs, and a `0` or a negative would resolve to nothing later while
        looking like a success now.
        """
        from pymax.exceptions import ApiError

        # Before the call is built, so a down session is a provable not-sent
        # rather than one more indistinguishable failure.
        self._require()
        self._owner_acted()

        try:
            answer = await call()
        except ApiError as error:
            if on_api_error is not None:
                on_api_error(error)
            raise
        except MaxClientError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if on_unconfirmed is not None:
                on_unconfirmed()
            raise MaxUnconfirmedSendError(
                f"the {what} send went out and MAX did not answer "
                f"({type(error).__name__}); it may be in the chat"
            ) from error

        raw = extract_message_id(answer)
        try:
            message_id = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            message_id = None
        if message_id is None or message_id <= 0:
            if on_unconfirmed is not None:
                on_unconfirmed()
            raise MaxUnconfirmedSendError(
                f"MAX answered the {what} send without a usable message id"
            )
        return message_id

    async def send_text(self, chat_id: int, text: str, *, reply_to: int | None = None) -> int:
        """Send a message and return the id MAX assigned to it."""
        client = self._require()
        return await self._run_creating_call(
            what="text",
            call=lambda: client.send_message(chat_id=chat_id, text=text, reply_to=reply_to),
            extract_message_id=lambda message: getattr(message, "id", None),
        )

    async def send_contact(
        self,
        chat_id: int,
        *,
        vcard: str,
        contact_user_id: int | None = None,
        reply_to: int | None = None,
    ) -> int:
        """Share a contact into a MAX chat as a native CONTACT attach (contact sharing).

        Sent as the official client sends it — opcode 64 with a CONTACT attach —
        because PyMax has no model for it. The whole contact travels as its
        vCard: the server parses `firstName`, `lastName` and `phone` out of it
        so a phonebook contact with no MAX
        account shares fine. `contact_user_id` is added only when the contact is
        a known MAX user, which links the card to their profile; without it the
        card is the vCard alone, which is exactly right for a stranger.

        A bare `{firstName, phone}` with no vCard is rejected
        (`Missing info for contact attachment`), so the vCard is required.
        """
        attach: dict[str, Any] = {"_type": "CONTACT", "vcfBody": vcard}
        if contact_user_id is not None:
            attach["contactId"] = int(contact_user_id)

        # A negative, millisecond-clock cid, the way every client stamps an
        # outgoing message: the server echoes it back and pairs the two by it.
        message: dict[str, Any] = {"cid": -int(time.time() * 1000), "attaches": [attach]}
        if reply_to:
            message["link"] = {"type": "REPLY", "messageId": int(reply_to)}

        frame = {"chatId": chat_id, "message": message, "notify": True}
        return await self._run_creating_call(
            what="contact",
            call=lambda: self._invoke(Opcode.MSG_SEND, frame),
            extract_message_id=_message_id_of,
        )

    async def send_media(
        self,
        chat_id: int,
        items: list[tuple[str, Path, str]],
        *,
        text: str = "",
        reply_to: int | None = None,
    ) -> int:
        """Upload files into MAX and send them as one message.

        Each item is `(kind, path, name)`. `photo`, `video` and `file` take the
        two-step upload PyMax owns (opcode for a URL, then a multipart POST). A
        `sticker` is uploaded, created and referenced by id. A `voice` or a
        `circle` takes the second pipeline (`_send_native_media`, native media delivery) — always
        alone, so it is routed before the PyMax builders run.
        """
        client = self._require()

        # A sticker is not a file attach at all: it is uploaded, turned into a
        # sticker, and then referenced by id. Telegram never groups one with
        # anything else, so a sticker always arrives alone here.
        if any(kind == "sticker" for kind, _, _ in items):
            if len(items) != 1:
                raise MaxStickerError("a sticker cannot be sent alongside other attachments")
            _, path, _ = items[0]
            return await self._send_one_sticker(chat_id, path, text=text)

        # The second media pipeline (native media delivery): op82 upload + a hand-built attach, sent
        # alone like a sticker. Both a voice and a circle go through it, and it
        # only works while the server hands back the ONE_ME uploader — which it
        # does for the pinned `app_version` (see `MIN_NATIVE_MEDIA_APP_VERSION`).
        #
        # `allows` is the single gate: the operator's setting and the breaker in
        # one answer. A closed breaker means not even op82 goes out, which is the
        # whole point — a refused attach costs a session reconnect, so the second
        # one must not be sent at all.
        native = [kind for kind, _, _ in items if self._native.allows(kind)]
        if native:
            if len(items) != 1:
                raise MaxMediaError("a voice or video note cannot be sent alongside other media")
            kind, path, name = items[0]
            self._native.note_attempt(kind)
            try:
                sent = await self._send_native_media(chat_id, kind, path, name, reply_to=reply_to)
            except MaxNativeRejectedError:
                # MAX answered op64 with a refusal of the attach itself. The
                # message was not created, so this one degrades like any other —
                # but `_send_native_media` has already tripped the breaker, so it
                # is the last one that will try.
                logger.warning("native %s was refused by MAX, sending it plainly", kind)
                self._native.note_ordinary_fallback(kind)
            except MaxMediaError:
                # Everything else that stops short of a created message: a missing
                # slot, an uploader we have no path for, a file we could not read a
                # duration/waveform from, an upload the CDN refused. Fall through to
                # the plain attachment rather than lose the message — a voice
                # arriving as a file beats no voice at all.
                logger.warning(
                    "native %s failed, degrading to a plain attachment", kind, exc_info=True
                )
                self._native.note_ordinary_fallback(kind)
            else:
                self._native.note_success(kind)
                return sent
            # `MaxUnconfirmedSendError` is deliberately absent from both clauses: it
            # means op64 may have landed, and the one thing that must not happen
            # then is a second copy. It travels up to the delivery layer.

        from pymax.files import File, Photo, Video

        # `voice`/`circle` degrade here when native is off: a voice becomes a file,
        # a circle an ordinary video — what the bridge did before native media delivery.
        builders = {"photo": Photo, "video": Video, "file": File, "voice": File, "circle": Video}
        attachments = [
            builders.get(kind, File)(path=str(path), name=name) for kind, path, name in items
        ]
        return await self._run_creating_call(
            what="media",
            call=lambda: client.send_message(
                chat_id=chat_id, text=text, reply_to=reply_to, attachments=attachments
            ),
            extract_message_id=lambda message: getattr(message, "id", None),
        )

    async def _send_one_sticker(self, chat_id: int, source: Path, *, text: str = "") -> int:
        """Convert, reuse or create, then send.

        The conversion runs here rather than at download time because a retry
        re-fetches the original from Telegram: whatever path the job takes, the
        bytes MAX sees have to come out the same, which is also what makes the
        cache key stable.
        """
        # A sticker the bridge itself carried out of MAX comes back from
        # Telegram byte for byte, so its own id is recoverable — and sending
        # that id returns the original, animation and all, instead of a flat
        # copy. Checked before any conversion, because the conversion is
        # precisely what would destroy it.
        if self._sticker_origins is not None:
            source_digest = await asyncio.to_thread(_sha256_of, source)
            known = await self._sticker_origins.get(source_digest)
            if known is not None:
                logger.debug("sticker %s came from MAX; sending it back as itself", known)
                return await self.send_sticker(chat_id, known, text=text)

        # Rendering Lottie and decoding VP9 are real CPU work, and this process
        # is carrying a live conversation on the same loop. Off the loop it goes.
        payload, digest = await asyncio.to_thread(_render_sticker_png, source)

        sticker_id = await self._stickers.get(digest) if self._stickers else None
        if sticker_id is None:
            sticker_id = await self.create_sticker(payload)
            if self._stickers:
                await self._stickers.put(digest, sticker_id)
        else:
            logger.debug("reusing MAX sticker %s for %s", sticker_id, digest[:12])

        return await self.send_sticker(chat_id, sticker_id, text=text)

    async def create_sticker(self, png: bytes) -> int:
        """Upload a PNG and get back the id of the sticker MAX made from it.

        The operation has three steps:

            op81 {count:1}   -> an upload URL on iusmile.oneme.ru
            POST multipart   -> [{"token": …}]
            op193 {token}    -> {"sticker": {"id": …}}

        Two things about this differ from every other upload the bridge does.
        It is **synchronous** — no `MSG_TYPING` intent, no waiting for
        `NOTIF_ATTACH` — and it **creates** rather than references: the answer
        carries `authorType: USER`, and the sticker joins the owner's own
        collection. Callers must therefore cache the id (see
        `StickerCacheRepository`) instead of uploading the same picture twice.
        """
        slot = await self._invoke(Opcode.STICKER_UPLOAD, {"count": 1})
        url = slot.get("url") if isinstance(slot, dict) else None
        if not isinstance(url, str) or not url:
            raise MaxStickerError(f"no sticker upload slot: {slot!r}")

        token = await self._post_sticker(url, png)
        created = await self._invoke(Opcode.STICKER_CREATE, {"token": token})

        sticker = created.get("sticker") if isinstance(created, dict) else None
        sticker_id = sticker.get("id") if isinstance(sticker, dict) else None
        if not isinstance(sticker_id, int):
            raise MaxStickerError(f"sticker was not created: {created!r}")
        return sticker_id

    async def _post_sticker(self, url: str, png: bytes) -> str:
        """Upload the bytes. PNG only — WebP is answered with a bare server error."""
        import aiohttp

        data = aiohttp.FormData()
        data.add_field("file", png, filename="sticker.png", content_type="image/png")

        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data, headers=UPLOAD_HEADERS) as response:
                body = await response.text()

        # The upload answers 200 even when it refuses, with the reason in the
        # body — so the status alone says nothing.
        try:
            parsed = json.loads(body)
        except ValueError:
            raise MaxStickerError(f"sticker upload refused: {body[:200]}") from None

        token = parsed[0].get("token") if isinstance(parsed, list) and parsed else None
        if not isinstance(token, str) or not token:
            raise MaxStickerError(f"sticker upload returned no token: {body[:200]}")
        return token

    async def _send_native_media(
        self, chat_id: int, kind: str, source: Path, name: str, *, reply_to: int | None = None
    ) -> int:
        """Upload a voice or a video note into MAX and send it natively.

            op82 {uploaderType:1, type:2|1, count:1}  -> {info: {url}, token}
            POST <url>  (application/octet-stream)    -> 200
            op64 {message:{attaches:[<AUDIO|VIDEO>]}} -> the sent message

        `type` is `2` for a voice (an `au.oneme.ru/uploadAudio` URL), `1` for a
        circle (`vu.oneme.ru/uploadVideo`); the attach differs only by `_type`
        and, for a circle, `videoType:1`. `duration` and `wave` come off the file.

        The server must return the allowlisted endpoint for this kind. Any other
        answer is refused before a byte is read, and the note degrades to a file
        or an ordinary video.

        Degrading anywhere above op64 is safe, and not for the reason this used
        to give. It is not that every failure predates the upload — an HTTP
        refusal arrives after the whole body was sent. It is that **the upload
        does not create a message**: an object on the CDN that no op64 ever
        referenced does not exist as far as the chat is concerned. Past op64 that
        stops being true, which is why `_invoke_native_send` owns that boundary.
        """
        media_type = MediaUploadType.AUDIO if kind == "voice" else MediaUploadType.VIDEO
        slot = await self._invoke(Opcode.MEDIA_UPLOAD, media_upload_frame(media_type))
        info = slot.get("info") if isinstance(slot, dict) else None
        first = info[0] if isinstance(info, list) and info else info
        url = first.get("url") if isinstance(first, dict) else None
        token = first.get("token") if isinstance(first, dict) else None
        if not token and isinstance(slot, dict):
            token = slot.get("token")
        if not isinstance(url, str) or not url or not isinstance(token, str) or not token:
            # Which keys came back, never their values: the URL's query is where
            # the `signatureToken` lives, and the token is the upload's auth.
            keys = sorted(slot) if isinstance(slot, dict) else type(slot).__name__
            raise MaxMediaError(
                f"no {kind} upload slot: op82 answered {keys} with no usable url/token"
            )

        # Before a byte is read off disk and long before one is sent: the URL
        # comes from the server, so it is input. `check_upload_url` pins the
        # scheme, the exact host, the port and the endpoint — a substring test
        # would hand `au.oneme.ru.somewhere-else` a copy of private
        # correspondence, and it used to.
        try:
            host = check_upload_url(kind, url)
        except UploaderUrlError as error:
            # Either the identity did not take and MAX routed us to an uploader
            # we have no path for, or the answer is not an uploader at all. Both
            # end the same way: nothing uploaded, nothing sent, plain attachment.
            self._warn_wrong_uploader(kind, str(error))
            raise MaxMediaError(f"{kind}: {error}, not sending natively") from error

        from bridge.media.native_max import probe_media

        data = await asyncio.to_thread(source.read_bytes)
        duration_ms, wave = await asyncio.to_thread(probe_media, source, kind)
        logger.info(
            "native %s: host=%s size=%d duration=%dms wave=%dB",
            kind,
            host,
            len(data),
            duration_ms,
            len(wave),
        )
        if duration_ms <= 0 or not wave:
            # A token-based attach has to carry both — dropping either answers
            # `errors.process.attachment.video.not.supported` on op64, and that
            # rejection takes the MAX connection down with it. Bail here instead,
            # like the wrong-uploader case above.
            raise MaxMediaError(f"{kind}: no duration/waveform for {name!r}, not sending natively")
        await self._post_media(url, data, name)

        build = voice_attach if kind == "voice" else circle_attach
        attach = build(duration_ms=duration_ms, wave=wave, token=token)

        return await self._invoke_native_send(
            kind, native_media_frame(chat_id, attach, reply_to=reply_to)
        )

    async def _invoke_native_send(self, kind: str, frame: dict[str, Any]) -> int:
        """op64 for a voice or a circle — the shared creating boundary, specialised.

        Everything above this call is preparation: op82 hands out a slot and the
        POST puts bytes somewhere neither side calls a message. This frame is the
        one that makes a bubble appear, and it is told apart from every other
        creating send by exactly one thing — a refusal of *this* attach also
        drops the MAX connection, so the second one costs a reconnect. That is
        what the breaker is for, and it is the whole of the specialisation.

        Everything else — the pre-write proof, unconfirmed on anything the wire
        cannot rule out, an id that is missing or unusable — is
        `_run_creating_call`, unchanged and shared with text, contact, sticker
        and the ordinary media fallback.
        """

        def breaker(error: Any) -> None:
            verdict = classify_native_error(error)
            if not verdict.protocol_rejection:
                # A chat that refuses, a reply target that is gone, a code nobody
                # has seen: the delivery layer classifies it, and no breaker opens
                # over one difficult conversation.
                return
            self._native.trip(kind, verdict.reason)
            raise MaxNativeRejectedError(
                f"MAX refused the {kind} attach: {verdict.reason}"
            ) from error

        return await self._run_creating_call(
            what=kind,
            call=lambda: self._invoke(Opcode.MSG_SEND, frame),
            extract_message_id=_message_id_of,
            on_api_error=breaker,
            on_unconfirmed=lambda: self._native.note_unconfirmed(kind),
        )

    def _warn_wrong_uploader(self, kind: str, reason: str) -> None:
        """Say out loud that native media just stopped working, and why.

        Without this the failure is invisible: the send degrades to a plain
        attachment and the only symptom is that circles quietly arrive as
        ordinary videos. The uploader is chosen by the pinned `app_version`, so
        the fix is nearly always the same and worth spelling out — and worth
        saying at length only once per process, since it would otherwise repeat
        on every voice. The count keeps rising either way, and health reads that.

        `reason` names the host and what was wrong with it, never the query: the
        `signatureToken` lives there.
        """
        if not self._native.note_uploader_drift(kind):
            logger.debug("still not a usable %s uploader (%s)", kind, reason)
            return
        logger.warning(
            "MAX did not hand back the expected %s uploader (%s), so voices and circles "
            "will arrive as plain files/videos. The uploader is chosen by the app "
            "version we report (%s, floor %s): if this started on its own, that "
            "version may no longer be supported. Upgrade Telemax before changing "
            "the endpoint allowlist.",
            kind,
            reason,
            NATIVE_MEDIA_APP_VERSION,
            ".".join(str(part) for part in MIN_NATIVE_MEDIA_APP_VERSION),
        )

    def _upload_user_agent(self) -> str:
        """The upload header for this install's phone.

        Falls back to the identity on disk if a media send somehow runs before
        `start()` — the control socket and the upload POST describing different
        devices would be a contradiction with no innocent explanation.
        """
        identity = self._identity or load_or_create(
            self._session_dir,
            timezone=self._timezone,
            session_exists=(self._session_dir / self._session_name).exists(),
        )
        return identity.upload_user_agent(app_version=NATIVE_MEDIA_APP_VERSION)

    async def _post_media(self, url: str, data: bytes, filename: str) -> None:
        """POST the file to a ONE_ME upload URL exactly as the app does (stand
        capture): a single `application/octet-stream` body — no multipart, no
        Content-Range. The `signatureToken` in the URL is the auth.

        **Redirects are refused, not followed.** aiohttp follows them by default,
        and a 307 from the real host would re-send the whole body to whatever
        `Location` names — which would walk straight past the allowlist the
        caller just checked the URL against. A 3xx here is the uploader
        behaving in a way the captured flow never did, so it is an outcome, not
        a hop.
        """
        from urllib.parse import quote

        import aiohttp

        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Disposition": f"attachment; filename={quote(filename)}",
            "User-Agent": self._upload_user_agent(),
            "Connection": "keep-alive",
        }
        timeout = aiohttp.ClientTimeout(total=900, sock_read=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url, data=data, headers=headers, allow_redirects=False
            ) as response:
                if 300 <= response.status < 400:
                    # Deliberately without the `Location` — it is attacker- or
                    # server-chosen text that would end up in a log line.
                    raise MaxMediaError(
                        f"media upload answered {response.status}: the uploader redirected, "
                        "which the captured flow never does"
                    )
                if response.status != 200:
                    body = await response.text()
                    raise MaxMediaError(f"media upload refused ({response.status}): {body[:200]}")

    async def send_sticker(self, chat_id: int, sticker_id: int, *, text: str = "") -> int:
        """Send an existing MAX sticker by id."""
        frame = sticker_message_frame(chat_id, sticker_id, text=text)
        return await self._run_creating_call(
            what="sticker",
            call=lambda: self._invoke(Opcode.MSG_SEND, frame),
            extract_message_id=_message_id_of,
        )

    async def edit_text(self, chat_id: int, message_id: int, text: str) -> None:
        client = self._require()
        self._owner_acted()
        await client.edit_message(chat_id=chat_id, message_id=message_id, text=text)

    async def delete_messages(
        self, chat_id: int, message_ids: list[int], *, for_everyone: bool = True
    ) -> None:
        """Delete in MAX. `for_everyone` is what the owner means by "delete"."""
        client = self._require()
        self._owner_acted()
        await client.delete_message(
            chat_id=chat_id, message_ids=message_ids, for_me=not for_everyone
        )

    async def send_typing(self, chat_id: int, kind: TypingKind = TypingKind.TEXT) -> None:
        """Show "typing" in MAX. Fire-and-forget: the server sends no reply.

        A timeout here is the expected outcome, not a failure, and nothing about
        delivery depends on it — so this never raises.
        """
        self._owner_acted()
        try:
            await self._invoke(Opcode.MSG_TYPING, typing_frame(chat_id, kind), timeout=5)
        except TimeoutError:
            pass
        except Exception:
            logger.debug("typing signal failed for chat %s", chat_id, exc_info=True)

    async def add_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        """Set our reaction. Replaces any previous one — no removal needed first.

        Only emoji from the verified set should reach this method: an unknown one
        is answered with `error.message.like.unknown.like` (harmless), but the
        frame shape must be exact or the server drops the connection.
        """
        self._owner_acted()
        await self._invoke(Opcode.MSG_REACTION, add_reaction_frame(chat_id, message_id, emoji))

    async def remove_reaction(self, chat_id: int, message_id: int) -> None:
        """Clear our reaction.

        Rate limited server-side (`error.too-many-unlikes-dialog`) for a long
        time after a burst, so callers should queue this rather than retry it in
        a loop.
        """
        self._owner_acted()
        await self._invoke(Opcode.MSG_CANCEL_REACTION, remove_reaction_frame(chat_id, message_id))

    async def reactions_for(
        self, chat_id: int, message_ids: list[int]
    ) -> dict[int, MessageReactions]:
        """Ask the server what reactions those messages currently carry (op180).

        The push side cannot answer this. A dialog reports reactions as two chat
        fields (see `on_chat_reaction`), which name one message and are simply
        absent when nothing about them changed — so a reaction on an older
        message, and every removal, is invisible in the stream. Asking is the
        only reliable reading.
        """
        if not message_ids:
            return {}

        payload = await self._invoke(
            Opcode.MSG_GET_REACTIONS, get_reactions_frame(chat_id, message_ids)
        )
        logger.debug("reactions for %s messages: %s", len(message_ids), payload)
        return reactions_by_message(payload)

    # --------------------------------------------------------------- media sources

    async def video_sources(self, chat_id: int, message_id: int, video_id: int) -> Any:
        """Where a video can be downloaded from (op83).

        Raw on purpose: PyMax's `get_video_by_id` parses the answer into a model
        with a required `cache` field, which a video note's answer does not have,
        so the typed method cannot read a circle at all.
        """
        return await self._invoke(
            Opcode.VIDEO_PLAY, video_sources_frame(chat_id, message_id, video_id)
        )

    async def file_source(self, chat_id: int, message_id: int, file_id: int) -> Any:
        """Download URL for a FILE attachment (op88)."""
        return await self._invoke(
            Opcode.FILE_DOWNLOAD, file_download_frame(chat_id, message_id, file_id)
        )

    async def audio_sources(
        self, chat_id: int, message_id: int, audio_id: int, token: str | None = None
    ) -> Any:
        """Download URL for a voice message (op301).

        Voice uses a separate media path not exposed by PyMax.
        """
        return await self._invoke(
            Opcode.AUDIO_PLAY, audio_sources_frame(chat_id, message_id, audio_id, token)
        )

    async def mark_read(self, chat_id: int, message_id: int) -> None:
        """Tell MAX one message has been read — the contact's second tick.

        Sent as the app sends it, with the mark set to that message's own time
        rather than to the current second: the mark is a watermark, and PyMax's
        `read_message` always fills it with `now`, which would acknowledge every
        message written up to this moment instead of the one actually read.

        An id that carries no plausible time refuses rather than falling back to
        `now`. The fallback looked like resilience and was the opposite: "now" is
        a watermark over *everything*, so a single unreadable id would have told
        the contact that every message in the chat had been read — the precise
        thing `on_read` exists not to do. A missing tick is a missing tick; a
        wrong one is a lie about the owner.
        """
        mark = message_time_ms(message_id)
        if mark is None:
            raise MaxClientError(
                f"MAX message id {message_id} carries no plausible time; "
                "refusing to mark a read boundary we cannot place"
            )
        await self._invoke(Opcode.CHAT_MARK, read_frame(chat_id, message_id, mark=mark))

    async def fetch_history(
        self, chat_id: int, *, limit: int | None = 50
    ) -> list[IncomingMaxMessage]:
        """Messages of a chat, oldest first. `limit=None` means all of them.

        The bridge only sees events while it is connected, so this is what makes
        an hour of downtime survivable: the tail is re-read and pushed through
        the same dedup as live events.

        Paged, and that is the fix rather than a refinement. MAX takes `backward`
        — how many to load back from a point in time — and it was never passed,
        so every caller got one default page of about forty however much they
        asked for. A re-pull that says a thousand and delivers forty is not a
        smaller re-pull; it is a chat the owner believes is complete.

        Paging walks back from the oldest message seen so far and stops when a
        page brings nothing new. That condition, not a page count, is what ends
        it: a server that keeps returning the same tail would otherwise loop.
        """
        client = self._require()
        history = await _page_history(client, chat_id, limit)
        # Marked here rather than at each caller: everything this method returns
        # was fetched by definition, and the three replay paths (downtime
        # catch-up, a new bridge's first fill, an explicit import) would each have
        # had to remember. What reads it is `own_voice` — the owner's own words are
        # worth placing as theirs when they are being caught up, not when they were
        # typed a second ago.
        messages = [
            replace(
                normalize_message(item, own_user_id=self.own_user_id, chat_id=chat_id),
                from_history=True,
            )
            for item in (history or [])
        ]
        messages.sort(key=lambda item: item.message_id)
        return messages if limit is None else messages[-limit:]

    async def fetch_dialogs(self) -> list[Any]:
        client = self._require()
        chats = await client.fetch_chats()
        return list(chats or [])

    async def display_name(self, user_id: int) -> str | None:
        """Best-effort name for a contact, for the guardian bot in dynamic provisioning."""
        profile = await self.contact_profile(user_id)
        return profile.display_name if profile else None

    async def contact_presence(self, user_ids: list[int]) -> dict[int, PresenceUpdate]:
        """Ask when contacts were last active, rather than waiting to be told.

        Needed at start-up: the pinned status would otherwise stay empty until
        the contact happens to move.
        """
        if not user_ids:
            return {}
        payload = await self._invoke(Opcode.CONTACT_PRESENCE, presence_frame(user_ids))
        entries = (payload or {}).get("presence") if isinstance(payload, dict) else None
        if not isinstance(entries, dict):
            return {}

        result: dict[int, PresenceUpdate] = {}
        for key, value in entries.items():
            if not str(key).lstrip("-").isdigit() or not isinstance(value, dict):
                continue
            user_id = int(key)
            result[user_id] = PresenceUpdate(
                user_id=user_id,
                seen=_as_optional_int(value.get("seen")),
                status=_as_optional_int(value.get("status")),
            )
        return result

    async def contact_of_chat(self, chat_id: int) -> int | None:
        """Who the other person in a dialog is.

        A bridge is configured by chat id, but dressing its bot up needs the
        *user*: the participant list of a two-person dialog minus ourselves.

        Asked of one chat, not of all of them. `get_chats` answers from PyMax's own
        cache and, on a miss, fetches **those ids** (`CHAT_INFO`) instead of the
        whole list — which is what this used to do, once per bridge, at every
        start-up.
        """
        client = self._require()
        try:
            chats = await client.get_chats([int(chat_id)])
        except Exception:
            logger.debug("could not read MAX chat %s", chat_id, exc_info=True)
            return None
        for chat in chats or []:
            if int(getattr(chat, "id", 0) or 0) == int(chat_id):
                return contact_id_from(chat, self.own_user_id)
        return None

    def contact_of(self, chat: Any) -> int | None:
        """The contact of a dialog, from a chat object the caller already has.

        `contact_of_chat` asks MAX for the whole chat list to answer the same
        question. That is one round trip per dialog, and the picker asks about
        sixty of them — which is where five seconds of «Добавить диалоги» went.
        """
        return contact_id_from(chat, self.own_user_id)

    async def display_names(self, user_ids: list[int]) -> dict[int, str | None]:
        """Names for many contacts in one call.

        PyMax's `get_users` takes a list and caches what it gets; `get_user` is
        the same call with a list of one, so asking per contact is a round trip
        per contact.
        """
        if not user_ids:
            return {}
        client = self._require()
        wanted = sorted(set(user_ids))
        try:
            users = await client.get_users(wanted)
        except Exception:
            logger.debug("could not resolve %s MAX users", len(wanted), exc_info=True)
            return {}
        names: dict[int, str | None] = {}
        for user in users or []:
            user_id = _as_optional_int(getattr(user, "id", None))
            if user_id is None:
                continue
            names[user_id] = normalize_contact(user, user_id=user_id).display_name
        return names

    async def search_by_phone(self, phone: str) -> MaxContact | None:
        """Who this number belongs to in MAX, or None when it belongs to nobody.

        One number, never a list. The account this bridge runs on is usually
        registered through web/QR and has no address book at all, so this is the
        only way the owner can name somebody who has not written to them yet —
        and it is the reason `import_contacts` is not the entry point: a lookup
        reads, an import writes into the owner's own account.

        A number nobody has is not an error and does not raise: PyMax insists on
        a `contact` in the answer and fails when there is none, which is the
        server's way of saying "no such user".
        """
        client = self._require()
        try:
            user = await client.search_by_phone(phone)
        except Exception:
            # Deliberately not logged with the number in it: `phone` is exactly
            # the value redaction exists for, and "not found" is the common case.
            logger.debug("MAX found nobody for a searched phone number", exc_info=True)
            return None
        user_id = _as_optional_int(getattr(user, "id", None))
        if user_id is None:
            return None
        return normalize_contact(user, user_id=user_id)

    async def import_contact(self, phone: str, name: str) -> MaxContact | None:
        """Put one contact into the owner's MAX address book, and only one.

        The fallback for a number `search_by_phone` cannot see. It *writes* — the
        name and the number go to MAX and stay there — so nothing calls it
        without the owner having said yes to that in as many words.

        There is no bulk form of this on purpose. `import_contacts` takes a list,
        the phone book is a list, and the distance between the two is one careless
        line; this signature does not have the shape that mistake needs.
        """
        client = self._require()
        from pymax.types import ContactInfo

        try:
            users = await client.import_contacts([ContactInfo(phone=phone, first_name=name)])
        except Exception:
            logger.debug("MAX refused a single-contact import", exc_info=True)
            return None
        for user in users or []:
            user_id = _as_optional_int(getattr(user, "id", None))
            if user_id is not None:
                return normalize_contact(user, user_id=user_id)
        return None

    async def contact_profile(self, user_id: int) -> MaxContact | None:
        """Name and avatar of a contact, for dressing their bot up (profile synchronisation).

        MAX carries several names per user: `CUSTOM` is what the owner wrote in
        their own address book, `ONEME` is what the person calls themselves. The
        owner's own label wins — that is the name they expect to see on the bot.
        """
        client = self._require()
        try:
            user = await client.get_user(user_id)
        except Exception:
            logger.debug("could not resolve MAX user %s", user_id, exc_info=True)
            return None
        return normalize_contact(user, user_id=user_id)
