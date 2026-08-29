"""Normalising owner MTProto updates into the existing TG→MAX intake.

A second Telegram intake transport, not a new domain layer. It takes an owner
message already reduced to plain data — no Telethon types here, and never any
PyMax — and routes it through the *existing* `BridgeRouter`: the same durable
job, the same `message_map`, the same MAX adapter the Bot API path uses. The only
thing that differs is the identity carried alongside it (owner account + owner-
side id) and, for that, the source_key namespace.

Albums are the one shape that needs assembly before any of that can happen.
Telegram sends a media group as one `UpdateNewMessage` per part with nothing
marking the last, so the parts are written down individually — durably, in
`media_group_part` — and the group is carried as **one** MAX message once
nothing new has arrived for a moment. The parts live on disk rather than in this
process because the gap between part two and part three is exactly where a crash
loses photos Telegram will never send again, and because the order the parts go
out in is a fact about the album, not about which task woke up first.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from bridge.media.upload import ALBUM_WINDOW_SECONDS
from bridge.routing.echo import (
    album_part_fingerprint,
    echo_album_namespace,
    owner_album_namespace,
    owner_album_source_key,
)
from bridge.storage import Direction

from .commands import is_bot_command

#: How long a group waits after a flush that raised before it is tried again.
#: Long enough not to spin on a database that is refusing writes, short enough
#: that the group does not sit there until the next restart — which was the only
#: thing that used to pick it up.
FLUSH_RETRY_SECONDS = 30.0

#: How many times a group re-arms itself before it stops and waits for a restart.
#: Bounded for the same reason nothing else in this project retries forever: a
#: group that cannot be carried is a thing to look at, not a thing to loop on.
FLUSH_RETRIES = 6

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OwnerMedia:
    """One attachment, already classified, with the reference a retry re-fetches.

    `reference` carries no bytes and no expiring URL — the owner account, the
    peer and the owner-side message id, plus the kind and a name — so the media
    pipeline can pull it again over the session on every attempt.
    """

    kind: str
    reference: dict[str, Any]


@dataclass(frozen=True, slots=True)
class OwnerMessage:
    """An owner-outgoing Telegram message, reduced to what intake needs."""

    account_id: int
    peer_id: int
    message_id: int
    #: Text, or a media caption — markdown, ready for the same path Bot API uses.
    text: str
    media: OwnerMedia | None
    grouped_id: int | None
    reply_to_message_id: int | None
    #: A contact the owner shared, as its vCard. A contact is neither text nor a
    #: file, so it rides in its own field and takes its own branch — carried into
    #: MAX as a native contact, not as either of the two.
    contact_vcard: str | None = None


@dataclass(frozen=True, slots=True)
class OwnerAlbum:
    """One owner-side album, assembled and ready to become one MAX message.

    `references` are in canonical order — ascending Telegram message id — and
    each carries the `part_index` its alias was given, so the order survives the
    queue, a restart and every retry. Nothing perishable is in here: a reference
    is what the media pipeline re-fetches from, never a file and never a URL.
    """

    namespace: str
    bridge_name: str
    account_id: int
    peer_id: int
    #: The lowest owner-side id in the group — the identity the canonical
    #: `message_map` row is written under, and the one everything collapses onto.
    head_message_id: int
    caption: str
    reply_to_message_id: int | None
    references: list[dict[str, Any]]


class AlbumParts(Protocol):
    """The slice of `MediaGroupRepository` album assembly needs."""

    async def add_part(
        self,
        *,
        media_group_id: str,
        bridge_name: str,
        bot_id: int,
        telegram_message_id: int | None = ...,
        payload: dict[str, Any],
        link_id: int | None = ...,
        direction: Direction | None = ...,
        part_index: int | None = ...,
        media_kind: str | None = ...,
        caption_present: bool | None = ...,
        part_fingerprint: str | None = ...,
        telegram_owner_account_id: int | None = ...,
        telegram_owner_message_id: int | None = ...,
    ) -> bool: ...

    async def ordered_parts(self, media_group_id: str) -> list[Any]: ...

    async def open_owner_groups(self, *, older_than_ms: int = ...) -> list[str]: ...

    async def open_echo_groups(self, *, older_than_ms: int = ...) -> list[str]: ...

    async def assign_part_index(self, part_id: int, part_index: int) -> bool: ...


class OwnerAlbums:
    """Gathers one owner-side media group into one MAX message, durably.

    The timer is a guess about when the album ended and lives in memory, because
    a restart can simply make the guess again. What it is waiting for does not:
    every part is on disk the moment it is accepted, so the crash between part
    two and part three costs nothing, and the group is rebuilt on the next start
    from the rows rather than from anything this process remembered.

    Order is read at the end rather than assumed at the start. Parts arrive one
    at a time and a catch-up can replay them in any interleaving, so `part_index`
    is assigned when the group closes, from ascending Telegram message id — the
    one order the live probe found every reading of an album agrees on.
    """

    def __init__(
        self,
        *,
        store: AlbumParts,
        deliver: Callable[[OwnerAlbum], Awaitable[None]],
        window_seconds: float = ALBUM_WINDOW_SECONDS,
    ) -> None:
        self._store = store
        self._deliver = deliver
        self._window = window_seconds
        self._timers: dict[str, asyncio.Task[None]] = {}
        self._retries: dict[str, int] = {}

    async def add(self, message: OwnerMessage, *, bridge_name: str) -> None:
        """Accept one part. A part already stored re-arms nothing and adds nothing."""
        if message.grouped_id is None:
            raise ValueError("not an album part")
        if message.media is None:
            # A grouped message always carries media; one that does not is a
            # shape this bridge has not measured. Dropped rather than guessed at,
            # and never turned into a MAX message of its own — fragmenting an
            # album is the one thing this path must not do.
            logger.info("owner album part carries no media; not stored")
            return

        namespace = owner_album_namespace(
            message.account_id, message.peer_id, message.grouped_id
        )
        fresh = await self._store.add_part(
            media_group_id=namespace,
            bridge_name=bridge_name,
            bot_id=message.peer_id,
            payload={
                "reference": dict(message.media.reference),
                "caption": message.text,
                "reply_to": message.reply_to_message_id,
            },
            direction=Direction.TG_TO_MAX,
            media_kind=message.media.kind,
            caption_present=bool(message.text),
            telegram_owner_account_id=message.account_id,
            telegram_owner_message_id=message.message_id,
        )
        if not fresh:
            # A replayed update after a reconnect, or the same part twice. Adding
            # it again would put the photo in the album twice; re-arming for it
            # would let a replay hold a finished group open.
            logger.debug("owner album part already stored; ignored")
            return
        self._arm(namespace)

    async def restore(self) -> list[str]:
        """Re-arm every album a previous process accepted and never carried.

        Only groups with no canonical row behind them come back: one that already
        became a message has its `link_id` and is finished, whatever happened
        afterwards. That is what stops a restart from sending an album twice.
        """
        namespaces = await self._store.open_owner_groups()
        for namespace in namespaces:
            if namespace not in self._timers:
                self._arm(namespace)
        return namespaces

    async def drain(self) -> None:
        """Carry everything still waiting — used on shutdown.

        A group is only known to be complete once nothing has arrived for a
        moment, so stopping in that moment would otherwise leave photos the owner
        already watched leave Telegram sitting on disk until the next start.
        """
        for namespace in list(self._timers):
            task = self._timers.pop(namespace, None)
            if task is not None:
                task.cancel()
            await self.flush(namespace)

    def _arm(self, namespace: str) -> None:
        existing = self._timers.pop(namespace, None)
        if existing is not None:
            existing.cancel()
        self._timers[namespace] = asyncio.create_task(self._wait_and_flush(namespace))

    def _retry(self, namespace: str) -> None:
        """Come back to a group whose flush raised, a bounded number of times."""
        attempts = self._retries.get(namespace, 0) + 1
        if attempts > FLUSH_RETRIES:
            logger.error(
                "giving up on group %s after %s flush attempts; it waits for a restart",
                namespace,
                FLUSH_RETRIES,
            )
            return
        self._retries[namespace] = attempts
        self._timers[namespace] = asyncio.create_task(
            self._wait_and_flush(namespace, delay=FLUSH_RETRY_SECONDS)
        )

    async def _wait_and_flush(self, namespace: str, *, delay: float | None = None) -> None:
        try:
            await asyncio.sleep(self._window if delay is None else delay)
        except asyncio.CancelledError:
            # Another part arrived, or a shutdown is draining us; either way the
            # thing that cancelled this is responsible for the group now.
            return
        self._timers.pop(namespace, None)
        try:
            await self.flush(namespace)
        except Exception:
            # The parts are still on disk. `restore()` finds them at the next
            # start, and this re-arms so the group does not have to wait for one.
            logger.exception("could not carry an owner album into MAX")
            self._retry(namespace)

    async def flush(self, namespace: str) -> None:
        """Fix the order, then hand the whole group over as one message."""
        parts = await self._store.ordered_parts(namespace)
        if not parts:
            return

        references: list[dict[str, Any]] = []
        caption = ""
        reply_to: int | None = None
        for index, part in enumerate(parts):
            await self._store.assign_part_index(part.id, index)
            payload = json.loads(part.payload_json)
            reference = dict(payload.get("reference") or {})
            reference["part_index"] = index
            references.append(reference)
            if not caption and payload.get("caption"):
                # Telegram puts the caption on whichever part it was typed on —
                # the live probe found it on the second of three — so it is
                # looked for across the group rather than read off the head.
                caption = str(payload["caption"])
            if reply_to is None and payload.get("reply_to"):
                reply_to = int(payload["reply_to"])

        head = parts[0]
        await self._deliver(
            OwnerAlbum(
                namespace=namespace,
                bridge_name=head.bridge_name,
                account_id=int(head.telegram_owner_account_id),
                peer_id=int(head.bot_id),
                head_message_id=int(head.telegram_owner_message_id),
                caption=caption,
                reply_to_message_id=reply_to,
                references=references,
            )
        )


@dataclass(frozen=True, slots=True)
class EchoAlbum:
    """One incoming album, seen from the owner's side, ready to be bound.

    `parts` are in canonical order — ascending owner-side message id — each with
    the structure it must match: the kind, and the fingerprint the sender wrote
    down for that position before the album went out. Nothing is delivered off
    the back of this; it only gives an existing mapping the second half of its
    Telegram identity.
    """

    namespace: str
    bridge_name: str
    account_id: int
    bot_id: int
    parts: list[dict[str, Any]]


class ContactAlbums:
    """Buffers an incoming album echo until its order can be read.

    Symmetric with `OwnerAlbums`, and durable for the same reason: the parts
    arrive one at a time and the group is what has to be matched, so between the
    first part and the pause that ends the group there is a window where a crash
    would leave a delivered album with no owner-side identity for ever.

    The order is ascending owner-side message id and nothing else. Arrival order
    is not it — a reconnect's catch-up replays a group in whatever interleaving
    it likes — and neither is any timestamp, which the probe showed proves
    nothing about which of two identical photos came first.
    """

    def __init__(
        self,
        *,
        store: AlbumParts,
        bind: Callable[[EchoAlbum], Awaitable[None]],
        window_seconds: float = ALBUM_WINDOW_SECONDS,
    ) -> None:
        self._store = store
        self._bind = bind
        self._window = window_seconds
        self._timers: dict[str, asyncio.Task[None]] = {}
        self._retries: dict[str, int] = {}

    async def add(
        self,
        *,
        bridge_name: str,
        bot_id: int,
        account_id: int,
        message_id: int,
        part: dict[str, Any],
    ) -> None:
        namespace = echo_album_namespace(account_id, bot_id, int(part["grouped_id"]))
        fresh = await self._store.add_part(
            media_group_id=namespace,
            bridge_name=bridge_name,
            bot_id=bot_id,
            payload={"caption": part.get("caption") or ""},
            direction=Direction.MAX_TO_TG,
            media_kind=part.get("kind"),
            caption_present=part.get("caption") is not None,
            telegram_owner_account_id=account_id,
            telegram_owner_message_id=message_id,
        )
        if not fresh:
            # Either this part is already buffered, or the alias it belongs to
            # already carries this owner-side id — the unique index covers both,
            # and both mean the same thing: there is nothing left to do here.
            logger.debug("album echo part already known; ignored")
            return
        self._arm(namespace)

    async def restore(self) -> list[str]:
        namespaces = await self._store.open_echo_groups()
        for namespace in namespaces:
            if namespace not in self._timers:
                self._arm(namespace)
        return namespaces

    async def drain(self) -> None:
        for namespace in list(self._timers):
            task = self._timers.pop(namespace, None)
            if task is not None:
                task.cancel()
            await self.flush(namespace)

    def _arm(self, namespace: str) -> None:
        existing = self._timers.pop(namespace, None)
        if existing is not None:
            existing.cancel()
        self._timers[namespace] = asyncio.create_task(self._wait_and_flush(namespace))

    def _retry(self, namespace: str) -> None:
        """Come back to a group whose flush raised, a bounded number of times."""
        attempts = self._retries.get(namespace, 0) + 1
        if attempts > FLUSH_RETRIES:
            logger.error(
                "giving up on group %s after %s flush attempts; it waits for a restart",
                namespace,
                FLUSH_RETRIES,
            )
            return
        self._retries[namespace] = attempts
        self._timers[namespace] = asyncio.create_task(
            self._wait_and_flush(namespace, delay=FLUSH_RETRY_SECONDS)
        )

    async def _wait_and_flush(self, namespace: str, *, delay: float | None = None) -> None:
        try:
            await asyncio.sleep(self._window if delay is None else delay)
        except asyncio.CancelledError:
            return
        self._timers.pop(namespace, None)
        try:
            await self.flush(namespace)
        except Exception:
            # The parts are still buffered. `restore()` finds them at the next
            # start, and this re-arms so the binding does not have to wait for
            # one — an album echo that never binds costs the owner-side identity
            # of a delivered album, and it used to cost it until a restart.
            logger.exception("could not bind an album echo")
            self._retry(namespace)

    async def flush(self, namespace: str) -> None:
        parts = await self._store.ordered_parts(namespace)
        if not parts:
            return
        described: list[dict[str, Any]] = []
        for index, part in enumerate(parts):
            caption = json.loads(part.payload_json).get("caption") or None
            described.append(
                {
                    "owner_message_id": int(part.telegram_owner_message_id),
                    "part_index": index,
                    "kind": part.media_kind,
                    "fingerprint": album_part_fingerprint(
                        part.media_kind, part_index=index, caption=caption
                    ),
                }
            )
        head = parts[0]
        await self._bind(
            EchoAlbum(
                namespace=namespace,
                bridge_name=head.bridge_name,
                account_id=int(head.telegram_owner_account_id),
                bot_id=int(head.bot_id),
                parts=described,
            )
        )


class Router(Protocol):
    """The two existing `BridgeRouter` methods this transport calls, and no more."""

    def bridge_name_for_bot(self, bot_id: int) -> str | None: ...

    async def on_telegram_text(
        self,
        *,
        bot_id: int,
        telegram_chat_id: int,
        telegram_message_id: int,
        text: str,
        reply_to_telegram_message_id: int | None = ...,
        owner_account_id: int | None = ...,
    ) -> None: ...

    async def on_telegram_media(
        self,
        *,
        bot_id: int,
        telegram_chat_id: int,
        telegram_message_id: int,
        items: list[tuple[str, Any, str]],
        caption: str = ...,
        reply_to_telegram_message_id: int | None = ...,
        owner_account_id: int | None = ...,
        mtproto: list[dict[str, Any]] | None = ...,
        album_group_id: str | None = ...,
        source_key: str | None = ...,
    ) -> int | None: ...

    async def on_telegram_contact(
        self,
        *,
        bot_id: int,
        telegram_chat_id: int,
        telegram_message_id: int,
        vcard: str,
        contact_user_id: int | None = ...,
        reply_to_telegram_message_id: int | None = ...,
        owner_account_id: int | None = ...,
    ) -> None: ...

    async def on_owner_edit(
        self, *, owner_account_id: int, owner_message_id: int, text: str, edit_pts: int
    ) -> None: ...

    async def on_owner_delete(
        self, *, owner_account_id: int, owner_message_ids: list[int]
    ) -> None: ...

    async def on_contact_echo(
        self, *, bot_id: int, owner_account_id: int, owner_message_id: int, fingerprint: str
    ) -> None: ...

    async def on_contact_album_echo(
        self,
        *,
        bot_id: int,
        owner_account_id: int,
        namespace: str,
        parts: list[dict[str, Any]],
    ) -> None: ...


class ReadMarker(Protocol):
    """The one thing presence needs from this transport: "the owner read it".

    The id is the owner account's own — the numbering their client uses, which is
    not the one a contact bot sees for the same message — so the account travels
    with it. Both halves of the key or neither.
    """

    async def on_owner_read(
        self, bridge_name: str, *, owner_account_id: int, owner_message_id: int
    ) -> bool: ...


class MtprotoIntake:
    """Routes owner MTProto messages into the existing durable TG→MAX pipeline."""

    def __init__(
        self,
        *,
        router: Router,
        allowed_bots: Callable[[], Awaitable[set[int]]],
        album_parts: AlbumParts | None = None,
        album_window_seconds: float = ALBUM_WINDOW_SECONDS,
        read_marker: ReadMarker | None = None,
        updates: Any = None,
    ) -> None:
        self._router = router
        # The single reader of `UpdateEditMessage`. It owns the durable state a
        # content diff and a reaction diff are both taken against; this
        # transport only decides that the peer is ours and hands it over.
        self._updates = updates
        # Telegram's read events are the only truthful "the owner has seen it",
        # and they arrive on this session or nowhere.
        self._read_marker = read_marker
        # The allowlist is the live active-bridge bot set; a peer outside it is
        # an unrelated chat and is dropped before anything is fetched or stored.
        self._allowed_bots = allowed_bots
        # Without a store there is nowhere durable to put an album's parts, so
        # the group is refused whole rather than fragmented into N messages.
        self._albums = (
            OwnerAlbums(
                store=album_parts,
                deliver=self._carry_album,
                window_seconds=album_window_seconds,
            )
            if album_parts is not None
            else None
        )
        self._echo_albums = (
            ContactAlbums(
                store=album_parts,
                bind=self._bind_album_echo,
                window_seconds=album_window_seconds,
            )
            if album_parts is not None
            else None
        )

    async def on_owner_message(self, message: OwnerMessage) -> None:
        if message.media is None and is_bot_command(message.text):
            # Typed at the bot, not at the contact. The Bot API router has always
            # dropped these; this path never did, so every «/start», «/status»
            # and «/guard» the owner typed in a bridge chat was answered by the
            # bot and *also* delivered to the person on the other side.
            #
            # Text only. A photo whose caption happens to start with a slash is
            # still a photo, and refusing to carry it would lose the picture to
            # save a word.
            logger.debug("owner command not carried to MAX")
            return

        allowed = await self._allowed_bots()
        if message.peer_id not in allowed:
            # An unrelated Telegram chat. Nothing is downloaded, stored or logged
            # about it beyond this line — no text, caption, name or peer detail.
            logger.debug("owner message in a non-bridge chat ignored")
            return

        if message.grouped_id is not None:
            # An album: one Telegram group, one MAX message. The part is stored
            # and the group is carried whole once it has stopped growing. Never
            # split into independent MAX messages — that silent fragmentation is
            # the one thing this path must not do.
            await self._add_album_part(message)
            return

        if message.contact_vcard is not None:
            # A contact is neither text nor a file: its own branch, and ahead of
            # the media one because a contact has no bytes to fetch.
            await self._router.on_telegram_contact(
                bot_id=message.peer_id,
                telegram_chat_id=message.peer_id,
                telegram_message_id=message.message_id,
                vcard=message.contact_vcard,
                reply_to_telegram_message_id=message.reply_to_message_id,
                owner_account_id=message.account_id,
            )
            return

        if message.media is not None:
            # items empty on purpose: the worker fetches the bytes over the
            # session from the reference below, on the first attempt and every
            # retry, so nothing perishable is stored and there is one code path.
            await self._router.on_telegram_media(
                bot_id=message.peer_id,
                telegram_chat_id=message.peer_id,
                telegram_message_id=message.message_id,
                items=[],
                caption=message.text,
                reply_to_telegram_message_id=message.reply_to_message_id,
                owner_account_id=message.account_id,
                mtproto=[message.media.reference],
            )
            return

        if message.text:
            await self._router.on_telegram_text(
                bot_id=message.peer_id,
                telegram_chat_id=message.peer_id,
                telegram_message_id=message.message_id,
                text=message.text,
                reply_to_telegram_message_id=message.reply_to_message_id,
                owner_account_id=message.account_id,
            )

    async def _add_album_part(self, message: OwnerMessage) -> None:
        if self._albums is None:
            logger.info("owner album ignored: album assembly is not wired")
            return
        bridge_name = self._router.bridge_name_for_bot(message.peer_id)
        if bridge_name is None:
            # The allowlist said this peer is a live bridge and the router says
            # otherwise: a bridge disconnected between the two reads. Nothing is
            # stored for it rather than stored under a name nothing owns.
            logger.debug("owner album part for a bridge that is no longer live")
            return
        await self._albums.add(message, bridge_name=bridge_name)

    async def _carry_album(self, album: OwnerAlbum) -> None:
        """One assembled album, into MAX as one message, through the one router.

        Every reference travels; no bytes and no URL do. The source key is the
        album's namespace rather than any message id, so the same group can never
        be enqueued twice — not by a replay, not by a restart that closed a
        partial group on a different head.
        """
        await self._router.on_telegram_media(
            bot_id=album.peer_id,
            telegram_chat_id=album.peer_id,
            telegram_message_id=album.head_message_id,
            items=[],
            caption=album.caption,
            reply_to_telegram_message_id=album.reply_to_message_id,
            owner_account_id=album.account_id,
            mtproto=album.references,
            album_group_id=album.namespace,
            source_key=owner_album_source_key(album.namespace),
        )

    async def restore_albums(self) -> int:
        """Take back albums a previous process accepted and never carried."""
        if self._albums is None:
            return 0
        return len(await self._albums.restore())

    async def flush_albums(self) -> None:
        """Carry what is still waiting for its next part — used on shutdown."""
        if self._albums is not None:
            await self._albums.drain()

    async def on_owner_sent(self, *, account_id: int, bot_id: int, message: Any) -> None:
        """Record what a message looked like the moment it appeared.

        Both directions: a message the owner wrote, and one a contact bot placed
        in their chat. Either can be reacted to, and the reaction is read as a
        difference from this.
        """
        if self._updates is None:
            return
        if bot_id not in await self._allowed_bots():
            return
        await self._updates.note_new_message(
            account_id=account_id, bot_id=bot_id, message=message
        )

    async def on_owner_update(
        self,
        *,
        account_id: int,
        bot_id: int,
        message: Any,
        pts: int,
        text: str | None,
        outgoing: bool,
    ) -> None:
        """An `UpdateEditMessage` for a message in a contact-bot dialog.

        The same constructor for a text edit, for a reaction and for both, so
        nothing here decides which it was — that needs the durable state, and
        the dispatch owns it. This checks the allowlist, which is the one thing
        a transport is for, and hands the whole snapshot over.

        `text` is the markdown an edit would carry, and it is None for a message
        the owner did not write: a contact bot editing its own message is the
        bridge's own delivery being corrected, and there is nothing to carry
        back. Its reactions still are the owner's, which is why the update is
        passed on at all.
        """
        if self._updates is None:
            return
        if bot_id not in await self._allowed_bots():
            logger.debug("owner update in a non-bridge chat ignored")
            return
        await self._updates.on_owner_update(
            account_id=account_id,
            bot_id=bot_id,
            message=message,
            pts=pts,
            text=text,
            outgoing=outgoing,
        )

    async def allowed_bots(self) -> set[int]:
        """The live contact-bot set, so a transport can filter before it reads."""
        return await self._allowed_bots()

    async def on_owner_read(
        self, *, bot_id: int, owner_account_id: int, owner_message_id: int
    ) -> None:
        """The owner read a contact bot's chat up to `owner_message_id`.

        Telegram tells the owner's own session this whenever the owner opens the
        dialog, on any device — which is the only honest moment to tell MAX the
        message was read and let the contact's app draw its second tick.

        `owner_message_id` is `UpdateReadHistoryInbox.max_id`, and it is in the
        *owner's* Telegram numbering. It is passed on named as such, together
        with the account, so nothing downstream can mistake it for the id a
        contact bot would recognise.
        """
        if self._read_marker is None:
            return
        if bot_id not in await self._allowed_bots():
            return
        bridge_name = self._router.bridge_name_for_bot(bot_id)
        if bridge_name is None:
            return
        try:
            await self._read_marker.on_owner_read(
                bridge_name,
                owner_account_id=owner_account_id,
                owner_message_id=owner_message_id,
            )
        except Exception:
            logger.exception("could not carry an owner read mark into MAX")

    async def on_contact_echo(
        self,
        *,
        bot_id: int,
        account_id: int,
        message_id: int,
        fingerprint: str | None,
        album: dict[str, Any] | None = None,
    ) -> None:
        """A message a contact bot sent the owner, seen from the owner's side.

        It carries the owner's own id for a message the bridge already delivered,
        and that is the *only* thing taken from it: no delivery is created, and
        nothing travels back to MAX. The peer was checked against the live bridge
        set before this was called.

        An album arrives here as `album` rather than a fingerprint, because a
        single part of a group cannot be matched on its own — the group is what
        was sent, and its order is what tells two identical photos apart. It is
        buffered until the group stops growing. A shape nothing can bind (a
        sticker) arrives with neither and is dropped without a trace.
        """
        if album is not None:
            await self._add_echo_part(
                bot_id=bot_id, account_id=account_id, message_id=message_id, part=album
            )
            return
        if fingerprint is None:
            return
        await self._router.on_contact_echo(
            bot_id=bot_id,
            owner_account_id=account_id,
            owner_message_id=message_id,
            fingerprint=fingerprint,
        )

    async def _add_echo_part(
        self, *, bot_id: int, account_id: int, message_id: int, part: dict[str, Any]
    ) -> None:
        if self._echo_albums is None:
            logger.info("album echo ignored: album assembly is not wired")
            return
        bridge_name = self._router.bridge_name_for_bot(bot_id)
        if bridge_name is None:
            return
        await self._echo_albums.add(
            bridge_name=bridge_name,
            bot_id=bot_id,
            account_id=account_id,
            message_id=message_id,
            part=part,
        )

    async def _bind_album_echo(self, album: EchoAlbum) -> None:
        """One assembled echo, into the same durable binding job single ones use."""
        await self._router.on_contact_album_echo(
            bot_id=album.bot_id,
            owner_account_id=album.account_id,
            namespace=album.namespace,
            parts=album.parts,
        )

    async def restore_echo_albums(self) -> int:
        """Re-arm album echoes a previous process buffered and never bound."""
        if self._echo_albums is None:
            return 0
        return len(await self._echo_albums.restore())

    async def flush_echo_albums(self) -> None:
        if self._echo_albums is not None:
            await self._echo_albums.drain()

    async def on_owner_delete(
        self, *, account_id: int, message_ids: list[int], pts: int | None = None
    ) -> None:
        """A raw delete: no peer, so every id is resolved only via the mapping.

        Not filtered by an allowlist here — it cannot be, there is no peer — and
        it does not need to be: only ids the bridge itself mapped resolve to a
        bridge, so an unrelated chat's deletion falls through to nothing.
        """
        if not message_ids:
            return
        if self._updates is not None and pts is not None:
            # Written down before anything is derived from it. The dispatch
            # resolves each id's dialog, because the update carries no peer.
            await self._updates.on_owner_delete(
                account_id=account_id, message_ids=message_ids, pts=pts
            )
            return
        await self._router.on_owner_delete(
            owner_account_id=account_id, owner_message_ids=message_ids
        )
