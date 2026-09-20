"""Repositories: the only SQL in the project outside migrations.

Each one takes the `Database` and exposes a narrow, named set of operations.
The rest of the bridge never writes a query, which is what makes "no tokens in
the database" and "dedup happens before delivery" checkable by a test rather
than by discipline.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Any

import aiosqlite

from .database import Database, now_ms
from .models import (
    BridgeRecord,
    BridgeState,
    Direction,
    InboxFamily,
    InboxKey,
    InboxState,
    InboxUpdate,
    MediaGroupPart,
    MessageLink,
    OutboxItem,
    OutboxState,
    OwnerMessageState,
    OwnerUpdate,
    OwnerUpdateState,
    PendingContact,
    PendingContactState,
    PlacedMessage,
    ReactionSnapshot,
    ReadMarks,
    SourceMarker,
)

#: How long a worker holds a job before another one may take it back. Longer
#: than any single send, shorter than the owner's patience after a crash.
DEFAULT_LEASE_MS = 120_000

#: How long a job is worth delivering at all. A message from last week arriving
#: now reads as a glitch, not as a rescue.
DEFAULT_TTL_MS = 24 * 60 * 60 * 1000


def _link(row: aiosqlite.Row) -> MessageLink:
    return MessageLink(
        id=row["id"],
        bridge_name=row["bridge_name"],
        max_chat_id=row["max_chat_id"],
        max_message_id=row["max_message_id"],
        telegram_bot_id=row["telegram_bot_id"],
        telegram_chat_id=row["telegram_chat_id"],
        telegram_message_id=row["telegram_message_id"],
        direction=Direction(row["direction"]),
        source_marker=SourceMarker(row["source_marker"]),
        created_at=row["created_at"],
        echo_fingerprint=row["echo_fingerprint"],
        telegram_owner_message_id=row["telegram_owner_message_id"],
        telegram_owner_account_id=row["telegram_owner_account_id"],
    )


class MessageMapRepository:
    """The mapping between MAX and Telegram messages, and the dedup gate.

    The order matters and is the same one maxgram arrived at: claim the MAX
    message id *before* sending to Telegram. If delivery then fails and the
    event is replayed after a reconnect, the claim is what stops a second copy.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    async def claim_from_max(
        self,
        *,
        bridge_name: str,
        max_chat_id: int,
        max_message_id: int,
        telegram_bot_id: int,
        telegram_chat_id: int,
        echo_fingerprint: str | None = None,
    ) -> int | None:
        """Reserve a MAX message for delivery. `None` means it is already known.

        `echo_fingerprint` is the canonical form of what this message will put in
        the Telegram chat, and it is written *here* — in the same statement that
        claims the message, before anything is sent. That ordering is the whole
        guarantee: the owner's MTProto session cannot see an echo of a message
        that was never sent, so by the time any echo exists this row is already on
        disk with what to match it against, whichever side wins the race and
        whatever the process does in between. None means this increment cannot
        bind it (an album, a sticker) and it is skipped rather than blocking.
        """
        try:
            return await self._db.execute(
                "INSERT INTO message_map ("
                " bridge_name, max_chat_id, max_message_id, telegram_bot_id,"
                " telegram_chat_id, telegram_message_id, direction, source_marker,"
                " created_at, echo_fingerprint)"
                " VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                (
                    bridge_name,
                    max_chat_id,
                    max_message_id,
                    telegram_bot_id,
                    telegram_chat_id,
                    Direction.MAX_TO_TG.value,
                    SourceMarker.FROM_MAX.value,
                    now_ms(),
                    echo_fingerprint,
                ),
            )
        except aiosqlite.IntegrityError:
            # The unique index did its job: this MAX message already has a row.
            return None

    async def attach_telegram_message(self, link_id: int, telegram_message_id: int) -> None:
        """The bot's own id for a delivered message. Idempotent by design.

        Guarded on the column still being empty because two senders now call it —
        the inline path and the retry worker, through the one settle hook they
        share — and a second call must not overwrite an id that is already there.
        The unique index on (bot, telegram_message_id) makes overwriting a real
        hazard rather than a redundant write.
        """
        await self._db.execute(
            "UPDATE message_map SET telegram_message_id = ?"
            " WHERE id = ? AND telegram_message_id IS NULL",
            (telegram_message_id, link_id),
        )

    async def record_from_telegram(
        self,
        *,
        bridge_name: str,
        max_chat_id: int,
        telegram_bot_id: int,
        telegram_chat_id: int,
        telegram_message_id: int | None,
        max_message_id: int | None = None,
        telegram_owner_message_id: int | None = None,
        telegram_owner_account_id: int | None = None,
    ) -> int:
        """Reserve a TG→MAX message before it is sent, so the echo is recognised.

        Two intakes fill this. Bot API passes `telegram_message_id` (the bot's own
        id) and leaves the owner columns null. The MTProto owner-session transport
        has no bot-side id — it passes `telegram_owner_message_id` +
        `telegram_owner_account_id` and leaves `telegram_message_id` null, which is
        exactly the identity a later owner-side edit or delete resolves against.
        The row is written here, before the MAX send, so the owner identity is
        durable the moment the update is accepted — never deferred to after send.
        """
        return await self._db.execute(
            "INSERT INTO message_map ("
            " bridge_name, max_chat_id, max_message_id, telegram_bot_id,"
            " telegram_chat_id, telegram_message_id, telegram_owner_message_id,"
            " telegram_owner_account_id, direction, source_marker, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bridge_name,
                max_chat_id,
                max_message_id,
                telegram_bot_id,
                telegram_chat_id,
                telegram_message_id,
                telegram_owner_message_id,
                telegram_owner_account_id,
                Direction.TG_TO_MAX.value,
                SourceMarker.FROM_TG.value,
                now_ms(),
            ),
        )

    async def by_id(self, link_id: int) -> MessageLink | None:
        """One mapping row by its own id — what an album alias resolves through.

        A Telegram album is N messages and the MAX message it became is one, so
        every part carries the same `link_id` and every part-level event lands
        here. Without it an alias would name a row nothing could fetch.
        """
        row = await self._db.query_one("SELECT * FROM message_map WHERE id = ?", (link_id,))
        return _link(row) if row else None

    async def attach_max_message(self, link_id: int, max_message_id: int) -> bool:
        """Fill in the id MAX assigned to a message we sent. Idempotent.

        Returns False when the row already carries a **different** id. That is
        not a retry landing twice — the same send settling again writes the same
        number and is a quiet success. A different one means two remote messages
        are claiming one mapping row, and the caller has to say so rather than
        let the second silently overwrite the first: whichever id loses becomes
        a message the bridge can no longer resolve a reply, an edit or a delete
        against.

        The write is conditional in SQL rather than read-then-write, so two
        settlements racing cannot both believe they won.
        """
        await self._db.execute(
            "UPDATE message_map SET max_message_id = ?"
            " WHERE id = ? AND (max_message_id IS NULL OR max_message_id = ?)",
            (max_message_id, link_id, max_message_id),
        )
        row = await self._db.query_one(
            "SELECT max_message_id FROM message_map WHERE id = ?", (link_id,)
        )
        if row is None:
            return False
        stored = row["max_message_id"]
        return stored is None or int(stored) == int(max_message_id)

    async def by_max_message(
        self, max_chat_id: int, max_message_id: int, telegram_bot_id: int
    ) -> MessageLink | None:
        row = await self._db.query_one(
            "SELECT * FROM message_map"
            " WHERE max_chat_id = ? AND max_message_id = ? AND telegram_bot_id = ?",
            (max_chat_id, max_message_id, telegram_bot_id),
        )
        return _link(row) if row else None

    async def recent_max_messages(
        self, max_chat_id: int, telegram_bot_id: int, *, limit: int = 20
    ) -> list[MessageLink]:
        """The newest messages that exist on both sides, newest first.

        A dialog's reaction fields (opcode 135) name at most one message, so the
        only way to see a reaction on an older one is to ask the server about a
        window of messages. Bounded on purpose: this runs on chat updates, which
        are frequent.
        """
        rows = await self._db.query(
            "SELECT * FROM message_map"
            " WHERE max_chat_id = ? AND telegram_bot_id = ?"
            " AND max_message_id IS NOT NULL AND telegram_message_id IS NOT NULL"
            " ORDER BY max_message_id DESC LIMIT ?",
            (max_chat_id, telegram_bot_id, limit),
        )
        return [_link(row) for row in rows]

    async def placed_by(self, bridge_name: str) -> list[PlacedMessage]:
        """Every Telegram message this bridge put in the chat, oldest first.

        Both ids: the bot's own copy, and the one it placed as the owner through
        the business connection. A re-import has to remove both or the chat ends
        up holding half of the old conversation next to all of the new one.
        """
        rows = await self._db.query(
            "SELECT id, telegram_message_id, telegram_owner_message_id, telegram_chat_id"
            " FROM message_map WHERE bridge_name = ? AND max_message_id IS NOT NULL"
            " ORDER BY id",
            (bridge_name,),
        )
        return [
            PlacedMessage(
                link_id=row["id"],
                telegram_chat_id=row["telegram_chat_id"],
                telegram_message_id=row["telegram_message_id"],
                telegram_owner_message_id=row["telegram_owner_message_id"],
            )
            for row in rows
        ]

    async def forget(self, link_id: int) -> None:
        """Drop one mapping, so the MAX message it names can be delivered again.

        The dedup key *is* this row: `claim_from_max` refuses a second insert.
        Which is why the row is only ever dropped once its Telegram message is
        actually gone — otherwise a re-import would place a second copy beside
        the one that could not be deleted.
        """
        await self._db.execute("DELETE FROM message_map WHERE id = ?", (link_id,))

    async def unsent_claims(self, *, since_ms: int) -> list[MessageLink]:
        """MAX→TG claims with no delivery and no job behind them.

        The claim and the job are two statements, not one transaction: the row
        is written first — it has to be, because an echo of the message can
        already exist by the time the job does — and the job follows a render, a
        branch decision and possibly an album's aliases later. A process that
        dies in between leaves the claim, and `claim_from_max` then reads every
        MAX replay of that message as "already delivered". The message is gone
        for good, and nothing says so.

        A row is in that state when it names no Telegram message on either side
        *and* the queue holds nothing under its `source_key`. Both halves matter:

        * no ids means nothing was ever settled onto it;
        * no job means nothing ever reached a sender — every MAX→TG branch
          writes its job before its first remote call, including the two that
          only record a refusal (`submit_unstorable`, `submit_noop`).

        Together they prove nothing was sent, which is what makes releasing the
        claim safe rather than a guess at a possible duplicate.

        `since_ms` bounds it to the run that just died. Older rows are history —
        their MAX messages are long out of any catch-up window, and deciding
        what to do with them is the owner's, not a startup sweep's.
        """
        rows = await self._db.query(
            "SELECT * FROM message_map m"
            " WHERE m.direction = ?"
            "   AND m.telegram_message_id IS NULL"
            "   AND m.telegram_owner_message_id IS NULL"
            "   AND m.created_at >= ?"
            "   AND NOT EXISTS ("
            "     SELECT 1 FROM outbox o"
            "      WHERE o.source_key ="
            "            'max:' || m.max_chat_id || ':' || m.max_message_id)"
            " ORDER BY m.id",
            (Direction.MAX_TO_TG.value, since_ms),
        )
        return [_link(row) for row in rows]

    async def attach_owner_message(
        self,
        link_id: int,
        telegram_owner_message_id: int,
        *,
        telegram_owner_account_id: int | None = None,
    ) -> None:
        """Remember the owner's own id for a message placed on their behalf.

        Useless to the bot for anything it does — and the only id an owner-side
        event ever speaks, because `UpdateDeleteMessages` describes the owner's
        chat rather than the bot's view of it.

        `telegram_owner_account_id` travels with it, and the pair is the key
        (`_V13`): an owner-side id means nothing outside the account that issued
        it. Rows written before the MTProto session existed leave it None and
        read exactly as before — nothing infers one for them.
        """
        if telegram_owner_account_id is None:
            await self._db.execute(
                "UPDATE message_map SET telegram_owner_message_id = ? WHERE id = ?",
                (telegram_owner_message_id, link_id),
            )
            return
        await self._db.execute(
            "UPDATE message_map"
            " SET telegram_owner_message_id = ?, telegram_owner_account_id = ?"
            " WHERE id = ?",
            (telegram_owner_message_id, telegram_owner_account_id, link_id),
        )

    async def by_owner_message(
        self, telegram_bot_id: int, telegram_owner_message_id: int
    ) -> MessageLink | None:
        row = await self._db.query_one(
            "SELECT * FROM message_map"
            " WHERE telegram_bot_id = ? AND telegram_owner_message_id = ?",
            (telegram_bot_id, telegram_owner_message_id),
        )
        return _link(row) if row else None

    async def unattached_owner_rows(
        self, telegram_bot_id: int, *, since_ms: int
    ) -> list[MessageLink]:
        """Owner-authored rows this bot has no id of its own for, oldest first.

        The candidate set for the bot-side binding. Scoped three ways, and each
        one is load-bearing:

        * **the owner's session wrote it** — an owner-side id present, the bot's
          column empty — so a MAX→TG delivery waiting for its own settlement can
          never be mistaken for one of these;
        * **this bot** — one dialog;
        * **recent.** Without it the set is every owner message ever written
          before this observer existed. Measured on the live smoke: 79
          candidates, so nothing was ever unambiguous and nothing ever bound.
          Those rows are not stale candidates, they are *not candidates* — their
          Bot API sighting happened long ago and was dropped. The bound is what
          "in flight" means, not a tiebreaker between plausible rows.
        """
        rows = await self._db.query(
            "SELECT * FROM message_map"
            " WHERE telegram_bot_id = ? AND direction = ?"
            "   AND telegram_message_id IS NULL"
            "   AND telegram_owner_message_id IS NOT NULL"
            "   AND created_at >= ?"
            " ORDER BY id",
            (telegram_bot_id, Direction.TG_TO_MAX.value, since_ms),
        )
        return [_link(row) for row in rows]

    async def attach_bot_message_if_unset(
        self, link_id: int, telegram_message_id: int
    ) -> bool:
        """Record the bot's own id, unless something else got there first.

        Conditional rather than a plain update: the read that chose this row and
        the write are two statements, and a row that took an id in between may
        well have taken the right one. False means "look at this", not "retry".
        """
        rows = await self._db.query(
            "UPDATE message_map SET telegram_message_id = ?"
            " WHERE id = ? AND telegram_message_id IS NULL"
            " RETURNING id",
            (telegram_message_id, link_id),
        )
        return bool(rows)

    async def owner_bound_keys(self) -> list[tuple[int, int, int]]:
        """Every message the puppet session can name: account, bot, owner id.

        The bootstrap's work list. A row qualifies only when all three are
        present — an owner-side id without the account it belongs to is not an
        identity, and without the bot there is no dialog to fetch it from.
        """
        rows = await self._db.query(
            "SELECT DISTINCT telegram_owner_account_id AS account,"
            "       telegram_bot_id AS bot,"
            "       telegram_owner_message_id AS message"
            "  FROM message_map"
            " WHERE telegram_owner_account_id IS NOT NULL"
            "   AND telegram_owner_message_id IS NOT NULL"
            "   AND telegram_bot_id IS NOT NULL"
            " ORDER BY message"
        )
        return [(row["account"], row["bot"], row["message"]) for row in rows]

    async def owner_binding_counts(self) -> tuple[int, int]:
        """How many delivered messages the owner's session can name, and cannot.

        The second number is what a reaction over MTProto cannot resolve: rows
        the bot knows by its own id and the owner's session was never able to
        bind — albums and stickers, which the echo increment deliberately leaves
        unbindable. Counted rather than guessed at, and never backfilled.
        """
        row = await self._db.query_one(
            "SELECT"
            " sum(telegram_owner_message_id IS NOT NULL) AS bound,"
            " sum(telegram_owner_message_id IS NULL"
            "     AND telegram_message_id IS NOT NULL) AS unbound"
            " FROM message_map WHERE direction = ?",
            (Direction.MAX_TO_TG.value,),
        )
        if row is None:
            return (0, 0)
        return (int(row["bound"] or 0), int(row["unbound"] or 0))

    async def by_owner_account_message(
        self, telegram_owner_account_id: int, telegram_owner_message_id: int
    ) -> MessageLink | None:
        """Resolve an owner-side event that carries only the account and message id.

        This is the key `UpdateDeleteMessages` needs: it arrives with no peer, so
        the bot id cannot be part of the lookup. Keyed by account so a delete can
        never resolve against a mapping written under a different owner account.
        """
        row = await self._db.query_one(
            "SELECT * FROM message_map"
            " WHERE telegram_owner_account_id = ? AND telegram_owner_message_id = ?",
            (telegram_owner_account_id, telegram_owner_message_id),
        )
        return _link(row) if row else None

    # ------------------------------------------- owner-side echo binding (MAX→TG)

    async def oldest_unbound_echo(self, telegram_bot_id: int) -> MessageLink | None:
        """The head of this contact bot's queue of unbound MAX→TG messages.

        Head-of-line, deliberately: an echo is only ever tested against the
        *oldest* row still waiting for an owner-side id, never against whichever
        row happens to look like it. Two identical messages produce two identical
        fingerprints, so equality alone cannot tell them apart — order can, and
        the order is real, because MAX→TG sends for one bridge are serialised
        (see `OutboxRepository.older_undelivered`) and this table's `id` records
        exactly the sequence they were claimed in.

        Rows with no fingerprint are not candidates *and* do not block: they are
        the ones this increment knowingly cannot bind (an album, a sticker) or has
        given up on, and a row that can never be bound must not stop the ones
        behind it from being.
        """
        row = await self._db.query_one(
            "SELECT * FROM message_map"
            " WHERE telegram_bot_id = ? AND direction = ?"
            "  AND telegram_owner_message_id IS NULL AND echo_fingerprint IS NOT NULL"
            " ORDER BY id LIMIT 1",
            (telegram_bot_id, Direction.MAX_TO_TG.value),
        )
        return _link(row) if row else None

    async def bind_owner_message(
        self, link_id: int, *, account_id: int, owner_message_id: int
    ) -> bool:
        """Write the owner's own id onto a delivered message. Atomic, no overwrite.

        True when the row now carries this identity — including when it already
        did, so a replayed echo is an idempotent success rather than a conflict.
        False means the row belongs to a *different* owner-side message: the
        caller must not force it, because that would silently move a mapping some
        other echo already proved. The delivery itself is untouched either way.
        """
        async with self._db.transaction() as connection:
            cursor = await connection.execute(
                "UPDATE message_map"
                " SET telegram_owner_account_id = ?, telegram_owner_message_id = ?"
                " WHERE id = ?"
                "  AND (telegram_owner_message_id IS NULL"
                "   OR (telegram_owner_account_id = ? AND telegram_owner_message_id = ?))",
                (account_id, owner_message_id, link_id, account_id, owner_message_id),
            )
            return cursor.rowcount > 0

    async def stop_binding(self, link_id: int) -> bool:
        """Give up on ever binding this row, and let the queue move past it.

        Clearing the fingerprint is what makes the head-of-line rule survive a
        genuinely lost echo: without it one un-echoed message would block every
        later binding for that bot for ever. It is only ever done loudly — the
        caller raises the incident first — and it never touches the delivery,
        which stays exactly as delivered.
        """
        async with self._db.transaction() as connection:
            cursor = await connection.execute(
                "UPDATE message_map SET echo_fingerprint = NULL"
                " WHERE id = ? AND telegram_owner_message_id IS NULL",
                (link_id,),
            )
            return cursor.rowcount > 0

    async def by_telegram_message(
        self, telegram_bot_id: int, telegram_message_id: int
    ) -> MessageLink | None:
        row = await self._db.query_one(
            "SELECT * FROM message_map WHERE telegram_bot_id = ? AND telegram_message_id = ?",
            (telegram_bot_id, telegram_message_id),
        )
        return _link(row) if row else None

    async def is_echo_of_our_own(self, max_chat_id: int, max_message_id: int) -> bool:
        """True when MAX is telling us about a message the bridge itself sent."""
        row = await self._db.query_one(
            "SELECT 1 FROM message_map"
            " WHERE max_chat_id = ? AND max_message_id = ? AND source_marker = ?",
            (max_chat_id, max_message_id, SourceMarker.FROM_TG.value),
        )
        return row is not None

    async def last_outgoing(self, bridge_name: str) -> MessageLink | None:
        """Newest message the bridge delivered *into* Telegram."""
        row = await self._db.query_one(
            "SELECT * FROM message_map"
            " WHERE bridge_name = ? AND direction = ? AND telegram_message_id IS NOT NULL"
            " ORDER BY id DESC LIMIT 1",
            (bridge_name, Direction.MAX_TO_TG.value),
        )
        return _link(row) if row else None

    async def last_read_by_owner(
        self, bridge_name: str, *, owner_account_id: int, not_after_owner_id: int
    ) -> MessageLink | None:
        """Newest message carried MAX→Telegram at or before an **owner-side** id.

        The other half of a read mark, and the half that is easy to get wrong. A
        private chat numbers its messages once per account: the id the bot got
        back from `sendMessage` and the id the owner's own client shows for that
        same message are different numbers, and neither is derivable from the
        other (see `routing/echo.py`). `UpdateReadHistoryInbox` arrives on the
        owner's session, so its watermark is in the owner's numbering — and
        comparing it against `telegram_message_id`, the bot's, was reading two
        unrelated sequences as one. Measured live: bot-side ids in the hundreds,
        owner-side ids past 1.1 million on the same rows, so the comparison was
        true for every row and the mark always landed on the newest message the
        owner had not necessarily reached.

        Three things bound the answer, and all three matter:

        * **the bridge**, so one contact's reading never marks another's;
        * **the owner account**, the same key `by_owner_account_message` uses —
          an id is only meaningful inside the account that issued it;
        * **the watermark**, over owner-side ids only.

        A row with no owner-side id is not a candidate at all. It is not a
        message the owner cannot have read — it is a message whose owner-side
        identity has not arrived yet (its echo is still in flight, or it predates
        echo binding), and guessing would mark past it.

        Album parts are consulted too. An album is one MAX message and N Telegram
        messages, so the owner reads it by scrolling past its *last* part, whose
        owner-side id lives on the alias rather than on the canonical row. Both
        populations are searched and the greatest owner-side id at or below the
        watermark wins, then resolves to the one canonical message behind it.
        """
        row = await self._db.query_one(
            "SELECT m.* FROM ("
            "  SELECT link_id, owner_id FROM ("
            "    SELECT id AS link_id, telegram_owner_message_id AS owner_id"
            "      FROM message_map"
            "     WHERE bridge_name = ? AND direction = ?"
            "       AND telegram_owner_account_id = ?"
            "       AND telegram_owner_message_id IS NOT NULL"
            "       AND telegram_owner_message_id <= ?"
            "    UNION ALL"
            "    SELECT link_id, telegram_owner_message_id AS owner_id"
            "      FROM media_group_part"
            "     WHERE bridge_name = ? AND direction = ?"
            "       AND telegram_owner_account_id = ?"
            "       AND telegram_owner_message_id IS NOT NULL"
            "       AND telegram_owner_message_id <= ?"
            "       AND link_id IS NOT NULL"
            "  ) ORDER BY owner_id DESC LIMIT 1"
            ") AS reached"
            " JOIN message_map m ON m.id = reached.link_id",
            (
                bridge_name,
                Direction.MAX_TO_TG.value,
                owner_account_id,
                not_after_owner_id,
                bridge_name,
                Direction.MAX_TO_TG.value,
                owner_account_id,
                not_after_owner_id,
            ),
        )
        return _link(row) if row else None

    async def last_sent_to_max(
        self, bridge_name: str, *, not_after_ms: int | None = None
    ) -> MessageLink | None:
        """Newest message the *owner* sent, optionally no newer than a moment.

        This is what a read mark is about: the contact read what the owner
        wrote, not what the contact wrote themselves. The cutoff matters because
        MAX reports reading as a watermark — a message sent after that moment has
        not been read, and ticking it would be a lie.

        read and presence state marks only this one message: walking the history on every mark
        would burn the rate limit redrawing ticks nobody is looking at.
        """
        if not_after_ms is None:
            row = await self._db.query_one(
                "SELECT * FROM message_map"
                " WHERE bridge_name = ? AND direction = ? AND telegram_message_id IS NOT NULL"
                " ORDER BY id DESC LIMIT 1",
                (bridge_name, Direction.TG_TO_MAX.value),
            )
        else:
            row = await self._db.query_one(
                "SELECT * FROM message_map"
                " WHERE bridge_name = ? AND direction = ? AND telegram_message_id IS NOT NULL"
                "  AND created_at <= ?"
                " ORDER BY id DESC LIMIT 1",
                (bridge_name, Direction.TG_TO_MAX.value, not_after_ms),
            )
        return _link(row) if row else None


class OutboxRepository:
    """Persistent per-bridge delivery queue."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def enqueue(
        self,
        *,
        bridge_name: str,
        direction: Direction,
        kind: str,
        payload: dict[str, Any],
        delay_ms: int = 0,
        source_key: str | None = None,
        ttl_ms: int | None = DEFAULT_TTL_MS,
    ) -> int:
        """Put one delivery on the queue. Idempotent when `source_key` is given.

        The source key is what makes a replayed event safe. MAX repeats its
        events after a reconnect and Telegram repeats an update whose offset was
        never acknowledged; both must find the job that already exists rather
        than create a second one. The unique index does the deciding, so two
        callers racing cannot both win.
        """
        stamp = now_ms()
        async with self._db.transaction() as connection:
            if source_key is not None:
                async with connection.execute(
                    "SELECT id FROM outbox WHERE source_key = ?", (source_key,)
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing is not None:
                    return int(existing["id"])

            cursor = await connection.execute(
                "INSERT INTO outbox ("
                " bridge_name, direction, kind, payload_json, attempts,"
                " next_attempt_at, state, created_at, updated_at, source_key, expires_at)"
                " VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
                (
                    bridge_name,
                    direction.value,
                    kind,
                    json.dumps(payload, ensure_ascii=False),
                    stamp + delay_ms,
                    OutboxState.PENDING.value,
                    stamp,
                    stamp,
                    source_key,
                    stamp + ttl_ms if ttl_ms is not None else None,
                ),
            )
            return int(cursor.lastrowid or 0)

    async def enqueue_batch(
        self,
        *,
        bridge_name: str,
        direction: Direction,
        items: Sequence[tuple[str, dict[str, Any], str]],
        ttl_ms: int | None = DEFAULT_TTL_MS,
    ) -> list[int]:
        """Persist an ordered set of idempotent jobs in one transaction.

        Long text uses this before the first remote call.  Writing its parts one
        at a time would leave a crash window in which a later Telegram message
        could receive an outbox id between part one and part two, permanently
        changing the order the worker must honour.  One transaction makes the
        group appear whole, in order, or not at all.

        Existing source keys are returned in place.  That makes replay safe and
        lets a caller reconstruct the same batch after a restart without adding
        a second copy of any part.
        """
        stamp = now_ms()
        ids: list[int] = []
        async with self._db.transaction() as connection:
            for kind, payload, source_key in items:
                async with connection.execute(
                    "SELECT id FROM outbox WHERE source_key = ?", (source_key,)
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing is not None:
                    ids.append(int(existing["id"]))
                    continue
                cursor = await connection.execute(
                    "INSERT INTO outbox ("
                    " bridge_name, direction, kind, payload_json, attempts,"
                    " next_attempt_at, state, created_at, updated_at, source_key, expires_at)"
                    " VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
                    (
                        bridge_name,
                        direction.value,
                        kind,
                        json.dumps(payload, ensure_ascii=False),
                        stamp,
                        OutboxState.PENDING.value,
                        stamp,
                        stamp,
                        source_key,
                        stamp + ttl_ms if ttl_ms is not None else None,
                    ),
                )
                ids.append(int(cursor.lastrowid or 0))
        return ids

    async def claim_for_attempt(
        self,
        *,
        bridge_name: str,
        direction: Direction,
        kind: str,
        payload: dict[str, Any],
        source_key: str,
        ttl_ms: int | None = DEFAULT_TTL_MS,
        lease_ms: int = DEFAULT_LEASE_MS,
    ) -> tuple[int, bool]:
        """Create the job **already held**, or say that somebody else holds it.

        Returns `(job_id, ours)`. Only the caller that gets `ours=True` may send.

        Creating the row as PENDING and sending straight after looked equivalent
        and was not: the worker polls the same queue, and in the moment between
        the insert and the send it would claim the job and deliver it too. The
        contact got the message twice. Live verification caught exactly that —
        two copies in Telegram, and a job whose remote id had been overwritten
        by the second writer.

        So the row is born leased. A crash before the send is still covered: the
        lease expires and the worker takes it, which is the recovery path this
        was designed around.
        """
        stamp = now_ms()
        async with self._db.transaction() as connection:
            async with connection.execute(
                "SELECT id, state, lease_expires_at FROM outbox WHERE source_key = ?",
                (source_key,),
            ) as cursor:
                row = await cursor.fetchone()

            if row is not None:
                held = row["state"] == OutboxState.INFLIGHT.value and (
                    row["lease_expires_at"] or 0
                ) > stamp
                takeable = row["state"] == OutboxState.PENDING.value or (
                    row["state"] == OutboxState.INFLIGHT.value and not held
                )
                if not takeable:
                    # Delivered, failed, ambiguous, expired, or genuinely in
                    # another worker's hands. Not ours to send.
                    return int(row["id"]), False
                await connection.execute(
                    "UPDATE outbox SET state = ?, lease_expires_at = ?, updated_at = ?"
                    " WHERE id = ?",
                    (OutboxState.INFLIGHT.value, stamp + lease_ms, stamp, row["id"]),
                )
                return int(row["id"]), True

            cursor = await connection.execute(
                "INSERT INTO outbox ("
                " bridge_name, direction, kind, payload_json, attempts,"
                " next_attempt_at, state, created_at, updated_at, source_key,"
                " expires_at, lease_expires_at)"
                " VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)",
                (
                    bridge_name,
                    direction.value,
                    kind,
                    json.dumps(payload, ensure_ascii=False),
                    stamp,
                    OutboxState.INFLIGHT.value,
                    stamp,
                    stamp,
                    source_key,
                    stamp + ttl_ms if ttl_ms is not None else None,
                    stamp + lease_ms,
                ),
            )
            return int(cursor.lastrowid or 0), True

    async def claim_due(
        self, bridge_name: str, *, limit: int = 1, lease_ms: int = DEFAULT_LEASE_MS
    ) -> list[OutboxItem]:
        """Take the due head of this bridge's queue under a lease.

        Two rules matter here.

        **Order.** One job at a time by default, and only the *contiguous due
        prefix* is ever claimed. Both halves are needed. Claiming a batch and
        walking it looks equivalent and is not: when the first job fails and
        goes back to wait out a backoff, the rest of the batch is already in
        hand and gets delivered ahead of it — the contact then reads the
        conversation out of order. A bridge carries one message at a time
        instead, which at ten contacts costs nothing measurable. A job that can
        never succeed stops blocking once it goes FAILED, so the queue cannot
        wedge for ever.

        **Leases.** A claimed job is held until `lease_expires_at` rather than
        marked in flight for ever. A process that dies mid-send leaves the job
        looking taken; the lease running out is what puts it back in play,
        without needing a startup sweep to guess what was interrupted.
        """
        stamp = now_ms()
        async with self._db.transaction() as connection:
            async with connection.execute(
                "SELECT * FROM outbox WHERE bridge_name = ? AND state = ? ORDER BY id LIMIT ?",
                (bridge_name, OutboxState.PENDING.value, limit),
            ) as cursor:
                candidates = list(await cursor.fetchall())

            rows: list[aiosqlite.Row] = []
            for row in candidates:
                if row["next_attempt_at"] > stamp:
                    # The head is still waiting. Everything behind it waits too.
                    break
                rows.append(row)

            if rows:
                # The f-string only expands into placeholders (`?,?,?`) — every
                # value still travels as a bound parameter.
                placeholders = ",".join("?" * len(rows))
                await connection.execute(
                    "UPDATE outbox SET state = ?, lease_expires_at = ?, updated_at = ?"  # noqa: S608
                    f" WHERE id IN ({placeholders})",
                    (
                        OutboxState.INFLIGHT.value,
                        stamp + lease_ms,
                        stamp,
                        *[row["id"] for row in rows],
                    ),
                )

        return [_outbox_item(row, state=OutboxState.INFLIGHT) for row in rows]

    async def mark_done(self, item_id: int, *, remote_message_id: int | None = None) -> None:
        """Delivered, and the remote id proves it.

        The payload is dropped at the same time: it was only ever kept so the
        send could be repeated, and it is the private part of the row.
        """
        await self._db.execute(
            "UPDATE outbox SET state = ?, last_error = NULL, remote_message_id = ?,"
            " lease_expires_at = NULL, send_started_at = NULL, payload_json = '{}',"
            " updated_at = ? WHERE id = ?",
            (OutboxState.DONE.value, remote_message_id, now_ms(), item_id),
        )

    async def mark_sending(self, item_id: int) -> None:
        """Stamp the moment just before the remote call goes out.

        This is the only thing that lets lease recovery tell "died before the
        send" from "died during it". Without it a crash mid-upload came back as
        an ordinary retry, and the contact got the album twice.
        """
        await self._db.execute(
            "UPDATE outbox SET send_started_at = ?, updated_at = ? WHERE id = ?",
            (now_ms(), now_ms(), item_id),
        )

    async def mark_ambiguous(self, item_id: int, *, error: str) -> None:
        """The send went out and the answer never came back.

        Deliberately terminal: no automatic retry follows. Guessing here puts a
        second copy in a real person's chat, so the owner decides (ADR 0002).
        """
        stamp = now_ms()
        await self._db.execute(
            "UPDATE outbox SET state = ?, attempts = attempts + 1, ambiguous_at = ?,"
            " lease_expires_at = NULL, last_error = ?, updated_at = ? WHERE id = ?",
            (OutboxState.AMBIGUOUS.value, stamp, error[:500], stamp, item_id),
        )

    async def mark_expired(self, item_id: int) -> None:
        """Retire a job that waited out its whole TTL without being delivered.

        The payload goes, because it is the private half of the row and a day-old
        undelivered message is not going to be sent from it. What stays is enough
        to say *what* expired: the kind, the source key, the direction, the
        bridge and the age. That is the difference between "one MAX→Telegram
        media job for this contact gave up after 24 hours" and a row nobody can
        interpret — and it is why expiry is archive-only rather than retryable.
        """
        stamp = now_ms()
        await self._db.execute(
            "UPDATE outbox SET state = ?, lease_expires_at = NULL, payload_json = '{}',"
            " last_error = 'ttl expired: the job waited its whole life undelivered',"
            " updated_at = ? WHERE id = ?",
            (OutboxState.EXPIRED.value, stamp, item_id),
        )

    async def reclaim_expired_leases(self) -> tuple[int, int]:
        """Deal with jobs whose lease ran out. Returns (requeued, ambiguous).

        This is the crash recovery path. It runs while the process is up, not
        only at startup, so a worker that hangs on a socket cannot hold a
        message hostage indefinitely.

        The two outcomes are not interchangeable:

        * the send had **not** started — nothing left the machine, so the job
          goes back on the queue and is simply tried again;
        * the send **had** started — the message may already be in the chat.
          Retrying is how a duplicate album lands in front of a real person, so
          the job becomes AMBIGUOUS and waits for the owner (ADR 0002).
        """
        stamp = now_ms()
        expired = await self._db.query(
            "SELECT id, send_started_at FROM outbox WHERE state = ?"
            " AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?",
            (OutboxState.INFLIGHT.value, stamp),
        )
        requeued = ambiguous = 0
        for row in expired:
            if row["send_started_at"] is not None:
                await self.mark_ambiguous(
                    int(row["id"]),
                    error="the process died while sending; the result is unknown",
                )
                ambiguous += 1
            else:
                await self._db.execute(
                    "UPDATE outbox SET state = ?, lease_expires_at = NULL, updated_at = ?"
                    " WHERE id = ?",
                    (OutboxState.PENDING.value, stamp, int(row["id"])),
                )
                requeued += 1
        return requeued, ambiguous

    async def expire_overdue(self) -> int:
        """Retire jobs that outlived their TTL without being delivered."""
        stamp = now_ms()
        rows = await self._db.query(
            "SELECT id FROM outbox WHERE state = ? AND expires_at IS NOT NULL AND expires_at <= ?",
            (OutboxState.PENDING.value, stamp),
        )
        for row in rows:
            await self.mark_expired(int(row["id"]))
        return len(rows)

    async def retry_now(self, item_id: int) -> bool:
        """Owner-driven retry of a failed or ambiguous job (Guardian)."""
        stamp = now_ms()
        row = await self._db.query_one(
            "SELECT state, payload_json FROM outbox WHERE id = ?", (item_id,)
        )
        if row is None or row["state"] not in {
            OutboxState.FAILED.value,
            OutboxState.AMBIGUOUS.value,
        }:
            return False
        if row["payload_json"] == "{}":
            # Cleared after a delivery: there is nothing left to send.
            return False
        await self._db.execute(
            "UPDATE outbox SET state = ?, next_attempt_at = ?, lease_expires_at = NULL,"
            " updated_at = ? WHERE id = ?",
            (OutboxState.PENDING.value, stamp, stamp, item_id),
        )
        return True

    async def resolve(self, item_id: int) -> bool:
        """Owner says an ambiguous job actually arrived. Stop showing it."""
        row = await self._db.query_one("SELECT state FROM outbox WHERE id = ?", (item_id,))
        if row is None or row["state"] != OutboxState.AMBIGUOUS.value:
            return False
        await self._db.execute(
            "UPDATE outbox SET state = ?, payload_json = '{}', updated_at = ? WHERE id = ?",
            (OutboxState.DONE.value, now_ms(), item_id),
        )
        return True

    async def archive(self, item_id: int, *, reason: str) -> bool:
        """Set a dead job aside without losing it.

        Only a job the owner has already been told about — failed or ambiguous —
        can be archived, and nothing is deleted: `last_error`, `attempts` and
        every timestamp stay exactly as they were, with the reason appended so
        the row still explains itself years later.
        """
        row = await self._db.query_one(
            "SELECT state, last_error FROM outbox WHERE id = ?", (item_id,)
        )
        if row is None or row["state"] not in {
            OutboxState.FAILED.value,
            OutboxState.AMBIGUOUS.value,
            # Expired too: it is the one state the owner can only set aside,
            # since the payload it would be retried from is gone by definition.
            OutboxState.EXPIRED.value,
        }:
            return False
        previous = row["last_error"] or "no error recorded"
        await self._db.execute(
            "UPDATE outbox SET state = ?, last_error = ?, updated_at = ? WHERE id = ?",
            (
                OutboxState.ARCHIVED.value,
                f"{previous} | archived: {reason}"[:500],
                now_ms(),
                item_id,
            ),
        )
        return True

    async def archived(self, bridge_name: str) -> list[OutboxItem]:
        """Set-aside evidence, kept out of the counts and still readable."""
        rows = await self._db.query(
            "SELECT * FROM outbox WHERE bridge_name = ? AND state = ? ORDER BY id",
            (bridge_name, OutboxState.ARCHIVED.value),
        )
        return [_outbox_item(row) for row in rows]

    async def counts(self, bridge_name: str) -> dict[str, int]:
        """How many jobs sit in each state. What `/status` is built from."""
        rows = await self._db.query(
            "SELECT state, COUNT(*) AS n FROM outbox WHERE bridge_name = ? GROUP BY state",
            (bridge_name,),
        )
        return {str(row["state"]): int(row["n"]) for row in rows}

    async def oldest_pending_ms(self, bridge_name: str) -> int | None:
        """Age of the oldest undelivered job, or None when the queue is clear."""
        row = await self._db.query_one(
            "SELECT MIN(created_at) AS oldest FROM outbox"
            " WHERE bridge_name = ? AND state IN (?, ?)",
            (bridge_name, OutboxState.PENDING.value, OutboxState.INFLIGHT.value),
        )
        if row is None or row["oldest"] is None:
            return None
        return now_ms() - int(row["oldest"])

    async def needing_attention(self, bridge_name: str) -> list[OutboxItem]:
        """Jobs the owner has to decide about: failed, ambiguous and expired.

        Expiry was invisible. A job that sat PENDING for its whole 24-hour TTL —
        a bridge whose bot was revoked, a queue behind a head that never
        resolved — was quietly retired: payload cleared, no incident, absent from
        `/failed`. The message was neither delivered nor mentioned anywhere, and
        the alert that might have caught it beforehand only fires on queue
        *depth*, so one stuck job never reached it.
        """
        rows = await self._db.query(
            "SELECT * FROM outbox WHERE bridge_name = ? AND state IN (?, ?, ?) ORDER BY id",
            (
                bridge_name,
                OutboxState.FAILED.value,
                OutboxState.AMBIGUOUS.value,
                OutboxState.EXPIRED.value,
            ),
        )
        return [_outbox_item(row) for row in rows]

    async def mark_retry(
        self, item_id: int, *, delay_ms: int, error: str, costs_attempt: bool = True
    ) -> None:
        """Put a job back on the queue, having spent a try — or not having.

        `costs_attempt=False` is for a server that named its own wait. A
        `retry_after` is "not yet", not "no": counting it as a failure let a busy
        minute walk a perfectly deliverable message to FAILED twelve 429s later,
        and put it in front of the owner as something to decide about. The clock
        still moves, so this is a wait rather than a spin — it is only the budget
        that is left alone.
        """
        stamp = now_ms()
        await self._db.execute(
            "UPDATE outbox SET state = ?, attempts = attempts + ?, lease_expires_at = NULL,"
            " send_started_at = NULL, next_attempt_at = ?, last_error = ?,"
            " updated_at = ? WHERE id = ?",
            (
                OutboxState.PENDING.value,
                1 if costs_attempt else 0,
                stamp + delay_ms,
                error[:500],
                stamp,
                item_id,
            ),
        )

    async def mark_failed(self, item_id: int, *, error: str) -> None:
        """Give up. There is no infinite retry anywhere in this project.

        The payload is kept: `failed` is exactly the state where the owner may
        still want to retry by hand, and there would be nothing to send.
        """
        stamp = now_ms()
        await self._db.execute(
            "UPDATE outbox SET state = ?, attempts = attempts + 1, lease_expires_at = NULL,"
            " last_error = ?, updated_at = ? WHERE id = ?",
            (OutboxState.FAILED.value, error[:500], stamp, item_id),
        )

    async def queue_size(self, bridge_name: str) -> int:
        row = await self._db.query_one(
            "SELECT COUNT(*) AS size FROM outbox WHERE bridge_name = ? AND state IN (?, ?)",
            (bridge_name, OutboxState.PENDING.value, OutboxState.INFLIGHT.value),
        )
        return int(row["size"]) if row else 0

    # ---------------------------------------- owner edit/delete dependency ops

    async def older_undelivered(
        self, bridge_name: str, *, direction: Direction, before_id: int
    ) -> bool:
        """Is anything older in this direction still undelivered on this bridge?

        The one question both senders ask before sending, and the reason there is
        a single MAX→TG order rather than two. The inline path used to send the
        moment a message arrived while the worker was still carrying an earlier
        one that had failed and gone back to the queue — two independent senders,
        one chat, and the contact reading the answer before the question.

        PENDING and INFLIGHT are both "not delivered yet": a job in another
        worker's hands is exactly the one that must not be overtaken. Terminal
        states are not, because nothing more will come of them.
        """
        row = await self._db.query_one(
            "SELECT 1 AS found FROM outbox"
            " WHERE bridge_name = ? AND direction = ? AND id < ? AND state IN (?, ?)"
            " LIMIT 1",
            (
                bridge_name,
                direction.value,
                before_id,
                OutboxState.PENDING.value,
                OutboxState.INFLIGHT.value,
            ),
        )
        return row is not None

    async def older_undelivered_kind(
        self,
        bridge_name: str,
        *,
        direction: Direction,
        kind: str,
        before_id: int,
    ) -> bool:
        """Whether an older job of this exact kind is still on its way.

        Text fan-out uses this narrower barrier.  Its parts and the next text
        must not overtake each other, while an idempotent reaction mutation is
        not a text bubble and must not wedge the conversation behind it.
        """
        row = await self._db.query_one(
            "SELECT 1 AS found FROM outbox"
            " WHERE bridge_name = ? AND direction = ? AND kind = ? AND id < ?"
            "  AND state IN (?, ?) LIMIT 1",
            (
                bridge_name,
                direction.value,
                kind,
                before_id,
                OutboxState.PENDING.value,
                OutboxState.INFLIGHT.value,
            ),
        )
        return row is not None

    async def by_source_key(self, source_key: str) -> OutboxItem | None:
        """The job a source_key claimed, whatever state it is in now.

        The owner-edit/delete jobs read the *original send* job by its source_key
        to decide whether it is safe to coalesce, cancel, wait, or apply.
        """
        row = await self._db.query_one(
            "SELECT * FROM outbox WHERE source_key = ?", (source_key,)
        )
        return _outbox_item(row) if row else None

    async def forget_settled_from_max(self, bridge_name: str, max_chat_id: int) -> int:
        """Free the delivery keys of one chat's MAX history. Returns how many.

        The outbox refuses a job whose `source_key` it has already seen, and
        that is exactly right for a retry: the key is what stops one MAX message
        becoming two Telegram ones. It is exactly wrong for a *re-pull*, which
        asks for the same messages to be placed again on purpose — every one of
        them came back «already in hand», the chat stayed empty, and nothing
        anywhere said why.

        Only settled rows go. A job still in flight owns its key, and taking it
        away would let a second copy in beside the one being sent right now.
        """
        return await self._db.execute_changed(
            "DELETE FROM outbox WHERE bridge_name = ? AND source_key LIKE ?"
            " AND state NOT IN ('pending', 'inflight')",
            (bridge_name, f"max:{max_chat_id}:%"),
        )

    async def cancel_pending(self, source_key: str, *, reason: str) -> bool:
        """Archive a send that has not touched the remote yet — atomically.

        The guard is the whole point: `state = pending AND send_started_at IS
        NULL` is the pre-remote boundary, and a worker claiming the job flips it
        to `inflight` in the same kind of transaction, so exactly one of the two
        wins. A zero-row result means the worker got there first and the caller
        must fall back to a durable job that waits for the outcome.
        """
        async with self._db.transaction() as connection:
            cursor = await connection.execute(
                "UPDATE outbox SET state = ?, last_error = ?, updated_at = ?"
                " WHERE source_key = ? AND state = ? AND send_started_at IS NULL",
                (
                    OutboxState.ARCHIVED.value,
                    reason[:500],
                    now_ms(),
                    source_key,
                    OutboxState.PENDING.value,
                ),
            )
            return cursor.rowcount > 0

    async def replace_pending_payload(self, source_key: str, payload: dict[str, Any]) -> bool:
        """Swap a still-unsent job's payload — same pre-remote guard as cancel.

        Coalesces an owner edit into the pending send: the latest version is what
        goes out. Zero rows means the send is already claimed, so the caller waits
        instead and applies the edit as a remote edit once the send is DONE.
        """
        async with self._db.transaction() as connection:
            cursor = await connection.execute(
                "UPDATE outbox SET payload_json = ?, updated_at = ?"
                " WHERE source_key = ? AND state = ? AND send_started_at IS NULL",
                (
                    json.dumps(payload, ensure_ascii=False),
                    now_ms(),
                    source_key,
                    OutboxState.PENDING.value,
                ),
            )
            return cursor.rowcount > 0

    async def replace_failed_payload(self, source_key: str, payload: dict[str, Any]) -> bool:
        """Swap the payload of a FAILED send, for the owner's manual retry.

        An edit that arrives after the original send confirmed-failed updates the
        version a hand retry would send, rather than editing a message MAX never
        received.
        """
        async with self._db.transaction() as connection:
            cursor = await connection.execute(
                "UPDATE outbox SET payload_json = ?, updated_at = ?"
                " WHERE source_key = ? AND state = ?",
                (
                    json.dumps(payload, ensure_ascii=False),
                    now_ms(),
                    source_key,
                    OutboxState.FAILED.value,
                ),
            )
            return cursor.rowcount > 0

    async def defer(self, item_id: int, *, delay_ms: int) -> None:
        """Put a job back to PENDING to re-check later, without spending a try.

        A mutation waiting on its predecessor is not failing. `attempts` is left
        untouched, so waiting never walks a job toward FAILED, and no error is
        recorded, so it raises no incident — only `next_attempt_at` moves.
        """
        stamp = now_ms()
        await self._db.execute(
            "UPDATE outbox SET state = ?, lease_expires_at = NULL, send_started_at = NULL,"
            " next_attempt_at = ?, updated_at = ? WHERE id = ?",
            (OutboxState.PENDING.value, stamp + delay_ms, stamp, item_id),
        )

    async def failed(self, bridge_name: str) -> list[OutboxItem]:
        rows = await self._db.query(
            "SELECT * FROM outbox WHERE bridge_name = ? AND state = ? ORDER BY id",
            (bridge_name, OutboxState.FAILED.value),
        )
        return [_outbox_item(row) for row in rows]

    async def requeue_inflight(self) -> tuple[int, int]:
        """After a crash, in-flight items are dealt with. Returns (requeued, ambiguous).

        Called once at startup: nothing is in flight when the process has just
        started, so any such row is a leftover — no need to wait out its lease.
        """
        rows = await self._db.query(
            "SELECT id, send_started_at FROM outbox WHERE state = ?",
            (OutboxState.INFLIGHT.value,),
        )
        requeued = ambiguous = 0
        for row in rows:
            if row["send_started_at"] is not None:
                # It was mid-send when the process died. Whether it arrived is
                # not knowable from here, and guessing duplicates it.
                await self.mark_ambiguous(
                    int(row["id"]),
                    error="the process died while sending; the result is unknown",
                )
                ambiguous += 1
            else:
                await self._db.execute(
                    "UPDATE outbox SET state = ?, lease_expires_at = NULL, updated_at = ?"
                    " WHERE id = ?",
                    (OutboxState.PENDING.value, now_ms(), int(row["id"])),
                )
                requeued += 1
        return requeued, ambiguous


class TelegramInboxRepository:
    """Telegram updates on disk, written before their offset is acknowledged.

    This is the table that decides whether a crash costs a message. Telegram
    re-sends an update until the offset moves past it, so the safe order is:
    store, then acknowledge. Doing it the other way — which is what this project
    did until now — means the only copy of the message lived in a task that a
    SIGTERM was free to discard.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    async def store(
        self,
        *,
        bot_id: int,
        update_id: int,
        payload: dict[str, Any],
        bridge_name: str | None = None,
    ) -> int | None:
        """Write one update down. Returns None when it was already stored.

        `None` is the normal answer after a restart: Telegram replays whatever
        was never acknowledged, and the unique index turns the replay into a
        no-op instead of a second delivery.
        """
        stamp = now_ms()
        try:
            return await self._db.execute(
                "INSERT INTO telegram_inbox ("
                " bot_id, update_id, bridge_name, payload_json, state, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    bot_id,
                    update_id,
                    bridge_name,
                    json.dumps(payload, ensure_ascii=False),
                    InboxState.RECEIVED.value,
                    stamp,
                    stamp,
                ),
            )
        except aiosqlite.IntegrityError:
            return None

    async def remember_offset(self, bot_id: int, next_offset: int) -> None:
        """Persist the polling cursor, so a restart does not refetch a day."""
        await self._db.execute(
            "INSERT INTO telegram_offset (bot_id, next_offset, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(bot_id) DO UPDATE SET"
            " next_offset = MAX(excluded.next_offset, telegram_offset.next_offset),"
            " updated_at = excluded.updated_at",
            (bot_id, next_offset, now_ms()),
        )

    async def offset(self, bot_id: int) -> int | None:
        row = await self._db.query_one(
            "SELECT next_offset FROM telegram_offset WHERE bot_id = ?", (bot_id,)
        )
        return int(row["next_offset"]) if row else None

    async def claim_open(
        self, *, limit: int = 20, lease_ms: int = DEFAULT_LEASE_MS, bot_id: int | None = None
    ) -> list[InboxUpdate]:
        """Take unfinished updates in arrival order, under a lease.

        Ordering is by `id`, which is arrival order per bot, so the owner's two
        messages reach MAX the way they were typed.
        """
        stamp = now_ms()
        async with self._db.transaction() as connection:
            if bot_id is None:
                query = (
                    "SELECT * FROM telegram_inbox WHERE state = ?"
                    " OR (state = ? AND lease_expires_at <= ?) ORDER BY id LIMIT ?"
                )
                params: tuple[Any, ...] = (
                    InboxState.RECEIVED.value,
                    InboxState.LEASED.value,
                    stamp,
                    limit,
                )
            else:
                query = (
                    "SELECT * FROM telegram_inbox WHERE bot_id = ? AND (state = ?"
                    " OR (state = ? AND lease_expires_at <= ?)) ORDER BY id LIMIT ?"
                )
                params = (
                    bot_id,
                    InboxState.RECEIVED.value,
                    InboxState.LEASED.value,
                    stamp,
                    limit,
                )
            async with connection.execute(query, params) as cursor:
                rows = list(await cursor.fetchall())

            if rows:
                placeholders = ",".join("?" * len(rows))
                await connection.execute(
                    "UPDATE telegram_inbox SET state = ?, lease_expires_at = ?,"  # noqa: S608
                    f" updated_at = ? WHERE id IN ({placeholders})",
                    (
                        InboxState.LEASED.value,
                        stamp + lease_ms,
                        stamp,
                        *[row["id"] for row in rows],
                    ),
                )

        return [_inbox_update(row, state=InboxState.LEASED) for row in rows]

    async def requeue_leased(self) -> int:
        """After a crash, leased updates belong back in the queue.

        Called once at startup, and the reasoning is the same as the outbox's:
        nothing can be in flight in a process that has just started, so any such
        row belongs to one that is gone.

        Without it a crash mid-update cost the full remaining lease — up to two
        minutes of a message sitting on disk, already accepted from Telegram,
        while the bridge that was restarted specifically to carry it did
        nothing. Live verification measured exactly that.
        """
        rows = await self._db.query(
            "SELECT id FROM telegram_inbox WHERE state = ?", (InboxState.LEASED.value,)
        )
        if rows:
            await self._db.execute(
                "UPDATE telegram_inbox SET state = ?, lease_expires_at = NULL, updated_at = ?"
                " WHERE state = ?",
                (InboxState.RECEIVED.value, now_ms(), InboxState.LEASED.value),
            )
        return len(rows)

    async def mark_done(self, inbox_id: int) -> None:
        """Handled. The payload goes with it — it was private and is now spent."""
        await self._db.execute(
            "UPDATE telegram_inbox SET state = ?, payload_json = '{}', lease_expires_at = NULL,"
            " updated_at = ? WHERE id = ?",
            (InboxState.DONE.value, now_ms(), inbox_id),
        )

    async def mark_failed(self, inbox_id: int, *, error: str) -> None:
        await self._db.execute(
            "UPDATE telegram_inbox SET state = ?, attempts = attempts + 1, last_error = ?,"
            " lease_expires_at = NULL, updated_at = ? WHERE id = ?",
            (InboxState.FAILED.value, error[:500], now_ms(), inbox_id),
        )

    async def release(self, inbox_id: int, *, error: str) -> None:
        """Hand it back unfinished, so the next pass picks it up."""
        await self._db.execute(
            "UPDATE telegram_inbox SET state = ?, attempts = attempts + 1, last_error = ?,"
            " lease_expires_at = NULL, updated_at = ? WHERE id = ?",
            (InboxState.RECEIVED.value, error[:500], now_ms(), inbox_id),
        )

    async def depth(self, bot_id: int | None = None) -> int:
        """How many updates are still unhandled — the intake half of /status."""
        if bot_id is None:
            row = await self._db.query_one(
                "SELECT COUNT(*) AS n FROM telegram_inbox WHERE state IN (?, ?)",
                (InboxState.RECEIVED.value, InboxState.LEASED.value),
            )
        else:
            row = await self._db.query_one(
                "SELECT COUNT(*) AS n FROM telegram_inbox WHERE bot_id = ? AND state IN (?, ?)",
                (bot_id, InboxState.RECEIVED.value, InboxState.LEASED.value),
            )
        return int(row["n"]) if row else 0


class MediaGroupRepository:
    """Album parts: durable during assembly, and durable as aliases after it.

    One table, two lives. While a group is arriving these rows are what survives
    a crash between part two and part three — the job V8 built it for. Once the
    group has become a message, the same rows are the per-part aliases of the one
    `message_map` row it became, which is what lets a reply to the third photo, a
    delete of the first and an owner echo of the second all resolve to it.

    Everything here is namespaced by `media_group_id`, and the namespace is the
    caller's: a raw Telegram group id for the Bot API collector,
    `own:{account}:{peer}:{grouped}` for owner→MAX, `max:{link_id}` for the
    aliases a MAX→TG send writes before it sends. That is what keeps the logical
    unique index from ever confusing two accounts, two peers or two directions.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    async def add_part(
        self,
        *,
        media_group_id: str,
        bridge_name: str,
        bot_id: int,
        telegram_message_id: int | None = None,
        payload: dict[str, Any],
        link_id: int | None = None,
        direction: Direction | None = None,
        part_index: int | None = None,
        media_kind: str | None = None,
        caption_present: bool | None = None,
        part_fingerprint: str | None = None,
        telegram_owner_account_id: int | None = None,
        telegram_owner_message_id: int | None = None,
    ) -> bool:
        """Store one part. False when a unique index says it is already stored.

        False is the whole point of the return value and not an error: a replayed
        update, a catch-up burst and a second pass over the same album all land
        here, and each one must add nothing. Which index refuses does not matter
        to the caller — the bot-side id, the logical index and the owner-side id
        each identify a part, and any of them already being present means this
        part exists.

        Every argument past `payload` is NULL for the Bot API collector, whose
        rows are assembled and cleared and bind nothing.
        """
        stamp = now_ms()
        try:
            await self._db.execute(
                "INSERT INTO media_group_part ("
                " media_group_id, bridge_name, bot_id, telegram_message_id,"
                " payload_json, created_at, link_id, direction, part_index,"
                " media_kind, caption_present, part_fingerprint,"
                " telegram_owner_account_id, telegram_owner_message_id, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    media_group_id,
                    bridge_name,
                    bot_id,
                    telegram_message_id,
                    json.dumps(payload, ensure_ascii=False),
                    stamp,
                    link_id,
                    direction.value if direction is not None else None,
                    part_index,
                    media_kind,
                    None if caption_present is None else int(caption_present),
                    part_fingerprint,
                    telegram_owner_account_id,
                    telegram_owner_message_id,
                    stamp,
                ),
            )
        except aiosqlite.IntegrityError:
            return False
        return True

    async def parts(self, media_group_id: str) -> list[MediaGroupPart]:
        """Every stored part of one album, in the order Telegram sent them."""
        rows = await self._db.query(
            "SELECT * FROM media_group_part WHERE media_group_id = ? ORDER BY id",
            (media_group_id,),
        )
        return [_media_group_part(row) for row in rows]

    async def ordered_parts(self, media_group_id: str) -> list[MediaGroupPart]:
        """One album's parts in canonical order, whatever stage it is at.

        The canonical order of a Telegram album is ascending Telegram message id.
        The live probe proved it holds everywhere the order can be read — the
        array `sendMediaGroup` returns, the individual `UpdateNewMessage`s, a
        Telethon `Album` event, and the same sequence again after a reconnect's
        catch-up — while `pts` steps by an amount that is not the part count and
        cannot be used as an index.

        So the ordering key is `part_index` once it has been assigned, the
        owner-side message id while a group is still being assembled and its
        indexes are not final yet, and `id` for a legacy row that has neither.
        One expression rather than three methods, because a group in mid-flight
        can legitimately hold rows of more than one of those shapes.
        """
        rows = await self._db.query(
            "SELECT * FROM media_group_part WHERE media_group_id = ?"
            " ORDER BY COALESCE(part_index, telegram_owner_message_id, id), id",
            (media_group_id,),
        )
        return [_media_group_part(row) for row in rows]

    async def link_of(self, media_group_id: str) -> int | None:
        """The canonical message this group already became, if it became one.

        Read before a group is carried anywhere, and it is what makes the carry
        idempotent: a restart between the mapping row and the send finds the link
        already on the parts and reuses that row instead of writing a second one
        for the same album.
        """
        row = await self._db.query_one(
            "SELECT link_id FROM media_group_part"
            " WHERE media_group_id = ? AND link_id IS NOT NULL LIMIT 1",
            (media_group_id,),
        )
        return int(row["link_id"]) if row else None

    async def part_at(self, media_group_id: str, part_index: int) -> MediaGroupPart | None:
        """The one part holding a given position. Unique by index, not by search."""
        row = await self._db.query_one(
            "SELECT * FROM media_group_part WHERE media_group_id = ? AND part_index = ?",
            (media_group_id, part_index),
        )
        return _media_group_part(row) if row else None

    async def parts_of_link(self, link_id: int) -> list[MediaGroupPart]:
        """Every alias of one canonical message, in canonical order.

        What a delete reads. Telegram will remove a single part of an album, and
        MAX has one message with N attachments and no way to remove one of them —
        so any part's deletion collapses onto this list's single `link_id` and
        becomes one idempotent MAX delete, not N.
        """
        rows = await self._db.query(
            "SELECT * FROM media_group_part WHERE link_id = ?"
            " ORDER BY COALESCE(part_index, telegram_owner_message_id, id), id",
            (link_id,),
        )
        return [_media_group_part(row) for row in rows]

    async def by_owner_message(
        self, telegram_owner_account_id: int, telegram_owner_message_id: int
    ) -> MediaGroupPart | None:
        """Resolve an owner-side event that names one part of an album.

        The key `UpdateDeleteMessages` and an owner-side reply both speak: an
        account and one message id, with no peer and no bot id anywhere in it.
        """
        row = await self._db.query_one(
            "SELECT * FROM media_group_part"
            " WHERE telegram_owner_account_id = ? AND telegram_owner_message_id = ?",
            (telegram_owner_account_id, telegram_owner_message_id),
        )
        return _media_group_part(row) if row else None

    async def by_bot_message(
        self, bot_id: int, telegram_message_id: int
    ) -> MediaGroupPart | None:
        """The alias a Bot API reply quotes — the bot's own id for one part."""
        row = await self._db.query_one(
            "SELECT * FROM media_group_part WHERE bot_id = ? AND telegram_message_id = ?",
            (bot_id, telegram_message_id),
        )
        return _media_group_part(row) if row else None

    async def open_groups(self, *, older_than_ms: int = 0) -> list[str]:
        """Bot API albums still on disk — after a restart, the ones to reassemble.

        Scoped to the collector's own population: rows with no owner account and
        no canonical row behind them. Without that scope a restart would hand the
        Bot API collector somebody else's album — an owner→MAX group mid-assembly,
        or a MAX→TG alias that is not an unsent upload at all but a mapping — and
        try to upload it into MAX a second time.
        """
        rows = await self._db.query(
            "SELECT media_group_id, MAX(created_at) AS newest FROM media_group_part"
            " WHERE telegram_owner_account_id IS NULL AND link_id IS NULL"
            " GROUP BY media_group_id HAVING newest <= ? ORDER BY MIN(id)",
            (now_ms() - older_than_ms,),
        )
        return [str(row["media_group_id"]) for row in rows]

    async def open_owner_groups(self, *, older_than_ms: int = 0) -> list[str]:
        """Owner→MAX albums that were accepted and have not become a message yet.

        The mirror of `open_groups` for the other transport: parts carrying an
        owner-side identity and no `link_id`, which is precisely "assembled, not
        yet settled". A group that already reached MAX has its link and drops out
        of this list, so a restart cannot send it twice.
        """
        return await self._open_owner_side(Direction.TG_TO_MAX, older_than_ms)

    async def open_echo_groups(self, *, older_than_ms: int = 0) -> list[str]:
        """Album echoes still waiting for the rest of their group.

        The same shape again, for the third population: the owner's own view of
        an album the bridge delivered, arriving one part at a time. Told apart
        from the outgoing one by direction, which is what stops a restart handing
        an echo to the transport that would upload it into MAX.
        """
        return await self._open_owner_side(Direction.MAX_TO_TG, older_than_ms)

    async def _open_owner_side(self, direction: Direction, older_than_ms: int) -> list[str]:
        rows = await self._db.query(
            "SELECT media_group_id, MAX(created_at) AS newest FROM media_group_part"
            " WHERE telegram_owner_account_id IS NOT NULL AND link_id IS NULL"
            "  AND direction = ?"
            " GROUP BY media_group_id HAVING newest <= ? ORDER BY MIN(id)",
            (direction.value, now_ms() - older_than_ms),
        )
        return [str(row["media_group_id"]) for row in rows]

    async def oldest_unbound_album(self, bot_id: int) -> list[MediaGroupPart]:
        """The head of this contact bot's queue of albums awaiting an owner id.

        Head-of-line, for exactly the reason the single-message rule is: an echo
        is tested against the *oldest* album still waiting, never against
        whichever one happens to look right. Two albums of the same three photos
        produce the same three fingerprints, so equality cannot separate them —
        order can, and the order is real, because MAX→TG sends for one bridge go
        through one serialised queue and these rows record the sequence they were
        claimed in.

        A group whose fingerprints have been cleared is not a candidate *and*
        does not block: those are the ones given up on loudly, and a group that
        can never be bound must not stop the ones behind it from being.
        """
        row = await self._db.query_one(
            "SELECT media_group_id FROM media_group_part"
            " WHERE bot_id = ? AND direction = ? AND link_id IS NOT NULL"
            "  AND telegram_owner_message_id IS NULL AND part_fingerprint IS NOT NULL"
            " GROUP BY media_group_id ORDER BY MIN(id) LIMIT 1",
            (bot_id, Direction.MAX_TO_TG.value),
        )
        if row is None:
            return []
        return await self.ordered_parts(str(row["media_group_id"]))

    async def assign_part_index(self, part_id: int, part_index: int) -> bool:
        """Fix a part's position once the group's order is known. Never moves one.

        Assembly writes parts as they arrive, which is not necessarily the order
        they belong in; the index is assigned when the group closes and the
        ascending message ids can be read as a whole. Guarded on the column still
        being empty so a second pass over a closed group changes nothing.
        """
        async with self._db.transaction() as connection:
            cursor = await connection.execute(
                "UPDATE media_group_part SET part_index = ?, updated_at = ?"
                " WHERE id = ? AND (part_index IS NULL OR part_index = ?)",
                (part_index, now_ms(), part_id, part_index),
            )
            return cursor.rowcount > 0

    async def bind_link(self, media_group_id: str, link_id: int) -> int:
        """Point every part of one group at the canonical message it became.

        Returns how many rows now carry it. Conditional on the column being empty
        or already holding this very link, so re-running a settlement is a no-op
        and a group can never be re-pointed at a different canonical row.
        """
        async with self._db.transaction() as connection:
            cursor = await connection.execute(
                "UPDATE media_group_part SET link_id = ?, updated_at = ?"
                " WHERE media_group_id = ? AND (link_id IS NULL OR link_id = ?)",
                (link_id, now_ms(), media_group_id, link_id),
            )
            return int(cursor.rowcount)

    async def attach_bot_message(self, part_id: int, telegram_message_id: int) -> bool:
        """Give one alias the bot's own id for it. Atomic, and never an overwrite.

        True when the row now carries this id, including when it already did — so
        settling the same Telegram answer twice is an idempotent success. False
        means the row holds a *different* id, or another row in the same group
        already claimed this one: either way something disagrees with what was
        already proved, and the caller raises rather than deciding.
        """
        try:
            async with self._db.transaction() as connection:
                cursor = await connection.execute(
                    "UPDATE media_group_part"
                    " SET telegram_message_id = ?, updated_at = ?"
                    " WHERE id = ?"
                    "  AND (telegram_message_id IS NULL OR telegram_message_id = ?)",
                    (telegram_message_id, now_ms(), part_id, telegram_message_id),
                )
                return cursor.rowcount > 0
        except aiosqlite.IntegrityError:
            # The unique index refused it: this Telegram id belongs to another
            # part of the same group. Never forced — see the index comment.
            return False

    async def attach_owner_message(
        self, part_id: int, *, account_id: int, owner_message_id: int
    ) -> bool:
        """Write the owner's own id onto one alias. Atomic, and never an overwrite.

        The conditional is the whole guarantee. An empty column takes the value;
        the same account and id read as an idempotent success, which is what a
        catch-up replay of the echo must be; anything else leaves the row alone
        and returns False, because a row bound to a different owner message was
        bound by evidence this call does not have. The delivery is untouched
        either way — a binding that fails leaves a delivered album delivered.
        """
        try:
            async with self._db.transaction() as connection:
                cursor = await connection.execute(
                    "UPDATE media_group_part"
                    " SET telegram_owner_account_id = ?, telegram_owner_message_id = ?,"
                    "  updated_at = ?"
                    " WHERE id = ?"
                    "  AND (telegram_owner_message_id IS NULL"
                    "   OR (telegram_owner_account_id = ? AND telegram_owner_message_id = ?))",
                    (
                        account_id,
                        owner_message_id,
                        now_ms(),
                        part_id,
                        account_id,
                        owner_message_id,
                    ),
                )
                return cursor.rowcount > 0
        except aiosqlite.IntegrityError:
            # Another part already holds this owner-side id. A conflict, not a
            # race to resolve: two aliases claiming one message means the order
            # they were matched in was wrong, and that is for the owner to see.
            return False

    async def stop_binding(self, part_id: int) -> bool:
        """Give up on binding one alias, and let the queue move past it.

        The same escape the `message_map` head-of-line rule has, for the same
        reason: without it one part whose echo never arrived would block every
        later binding for that bridge for ever. Only ever done loudly — the caller
        raises the incident first — and it touches nothing but the fingerprint, so
        the delivery and the canonical mapping stay exactly as they are.
        """
        async with self._db.transaction() as connection:
            cursor = await connection.execute(
                "UPDATE media_group_part SET part_fingerprint = NULL, updated_at = ?"
                " WHERE id = ? AND telegram_owner_message_id IS NULL",
                (now_ms(), part_id),
            )
            return cursor.rowcount > 0

    async def clear(self, media_group_id: str) -> None:
        await self._db.execute(
            "DELETE FROM media_group_part WHERE media_group_id = ?", (media_group_id,)
        )

    async def clear_link(self, link_id: int) -> int:
        """Drop the aliases of one canonical message, with the message itself.

        The expected parts of an album are written before its job, so a claim
        released for want of a job takes them with it. Left behind they would be
        aliases of a `link_id` that no longer names anything, and the replay
        writes its own set under a new one.
        """
        await self._db.execute(
            "DELETE FROM media_group_part WHERE link_id = ?", (link_id,)
        )
        return 0


class AlbumSettlementError(Exception):
    """One album's identity could not be written down as a whole.

    Never partially, and never on a guess. The delivery is untouched: what is in
    the chat stays in the chat, and what is missing is the mapping, which the
    caller turns into a question for the owner.
    """


class AlbumSettlementRepository:
    """One album's whole identity, written in one transaction or not at all.

    The settlement used to be a loop of conditional updates: the canonical row
    took the head id first, and then each alias was written on its own. Three
    things followed from that, and each of them was real:

    * the head id was written **before** the receipt had been checked, so a short
      answer left a canonical message pointing at a delivery whose parts were
      unbound;
    * a conflict on part *k* left parts 0..k-1 written and k..N not, so the album
      resolved through some of its photos and not through others;
    * "the ids are ascending" was a documented contract that nothing asserted.

    All of it is one method now, and it is transaction-aware rather than a caller
    of the other repositories: `Database.transaction()` holds a non-reentrant
    lock, so a method that opened its own would deadlock inside another's.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    @staticmethod
    def _check_receipt(parts: Sequence[tuple[int, int]]) -> None:
        """Everything about the answer that can be judged without the database.

        Cheap, and first, so a malformed receipt never reaches the write lock.
        """
        if not parts:
            raise AlbumSettlementError("an album settlement was asked to bind no parts")
        indexes = [index for index, _ in parts]
        message_ids = [message_id for _, message_id in parts]
        if indexes != sorted(indexes) or indexes != list(range(len(indexes))):
            raise AlbumSettlementError(
                f"the parts do not cover 0..{len(indexes) - 1} exactly: {indexes}"
            )
        if len(set(message_ids)) != len(message_ids):
            raise AlbumSettlementError("the same message id was named for two parts")
        if any(message_id <= 0 for message_id in message_ids):
            raise AlbumSettlementError("a message id that cannot exist was named")
        if any(
            message_ids[position] >= message_ids[position + 1]
            for position in range(len(message_ids) - 1)
        ):
            # The adapter's contract, asserted a second time at the only place
            # that turns positions into identity. Never sorted: a repair here
            # would bind somebody's third photo to their second and say nothing.
            raise AlbumSettlementError(
                f"the message ids are not ascending in part order: {message_ids}"
            )

    async def settle_bot_album(
        self, *, link_id: int, parts: Sequence[tuple[int, int]]
    ) -> None:
        """Bind a bot-delivered album: the canonical head and every alias.

        The head is `parts[0]`'s id, which is what `message_map` has always
        stored — a reply, an edit or a deletion refers to the message as a whole.
        """
        self._check_receipt(parts)
        head = parts[0][1]
        async with self._db.transaction() as connection:
            expected = await self._expected(connection, link_id)
            if expected:
                self._match(expected, parts, link_id=link_id)
            await self._claim_canonical(
                connection,
                link_id=link_id,
                column="telegram_message_id",
                value=head,
                extra=None,
            )
            for part_index, message_id in parts:
                if part_index not in expected:
                    # No aliases were written for this delivery at all — a router
                    # built without them. The canonical row is the whole mapping,
                    # and there is nothing else to bind.
                    continue
                await self._claim_part(
                    connection,
                    part_id=expected[part_index],
                    link_id=link_id,
                    part_index=part_index,
                    columns=("telegram_message_id",),
                    values=(message_id,),
                )

    async def settle_owner_album(
        self, *, link_id: int, account_id: int, parts: Sequence[tuple[int, int]]
    ) -> None:
        """The same, for an album placed as the owner over their own session.

        A different id space and therefore different columns, and the account
        travels with the id: an owner-side message id means nothing outside the
        account that issued it.
        """
        self._check_receipt(parts)
        head = parts[0][1]
        async with self._db.transaction() as connection:
            expected = await self._expected(connection, link_id)
            if expected:
                self._match(expected, parts, link_id=link_id)
            await self._claim_canonical(
                connection,
                link_id=link_id,
                column="telegram_owner_message_id",
                value=head,
                extra=("telegram_owner_account_id", account_id),
            )
            for part_index, message_id in parts:
                if part_index not in expected:
                    continue
                await self._claim_part(
                    connection,
                    part_id=expected[part_index],
                    link_id=link_id,
                    part_index=part_index,
                    columns=("telegram_owner_account_id", "telegram_owner_message_id"),
                    values=(account_id, message_id),
                )

    @staticmethod
    async def _expected(connection: aiosqlite.Connection, link_id: int) -> dict[int, int]:
        """The aliases this canonical message is expected to have, by position."""
        async with connection.execute(
            "SELECT id, part_index FROM media_group_part"
            " WHERE link_id = ? AND part_index IS NOT NULL",
            (link_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return {int(row["part_index"]): int(row["id"]) for row in rows}

    @staticmethod
    def _match(
        expected: dict[int, int], parts: Sequence[tuple[int, int]], *, link_id: int
    ) -> None:
        if len(expected) != len(parts):
            raise AlbumSettlementError(
                f"album {link_id}: Telegram named {len(parts)} message(s)"
                f" for {len(expected)} expected part(s); nothing was bound"
            )
        missing = [index for index, _ in parts if index not in expected]
        if missing:
            raise AlbumSettlementError(
                f"album {link_id}: no expected part at position(s) {missing}"
            )

    @staticmethod
    async def _claim_canonical(
        connection: aiosqlite.Connection,
        *,
        link_id: int,
        column: str,
        value: int,
        extra: tuple[str, int] | None,
    ) -> None:
        """Fill an empty column, accept the identical value, refuse anything else.

        The same conditional the one-at-a-time writes used, so a replayed
        settlement is still an idempotent success — what changed is that it now
        happens after the receipt has been proved and beside the aliases rather
        than before them.
        """
        assignments = [f"{column} = ?"]
        values: list[Any] = [value]
        if extra is not None:
            assignments.append(f"{extra[0]} = ?")
            values.append(extra[1])
        try:
            cursor = await connection.execute(
                f"UPDATE message_map SET {', '.join(assignments)}"  # noqa: S608 - column names are literals above
                f" WHERE id = ? AND ({column} IS NULL OR {column} = ?)",
                (*values, link_id, value),
            )
        except aiosqlite.IntegrityError as error:
            raise AlbumSettlementError(
                f"album {link_id}: {column} {value} already belongs to another message"
            ) from error
        if cursor.rowcount == 0:
            raise AlbumSettlementError(
                f"album {link_id}: the canonical row already names a different"
                f" {column} than {value}"
            )

    @staticmethod
    async def _claim_part(
        connection: aiosqlite.Connection,
        *,
        part_id: int,
        link_id: int,
        part_index: int,
        columns: tuple[str, ...],
        values: tuple[int, ...],
    ) -> None:
        identity = columns[-1]
        target = values[-1]
        assignments = ", ".join(f"{column} = ?" for column in columns)
        try:
            cursor = await connection.execute(
                f"UPDATE media_group_part SET {assignments}, updated_at = ?"  # noqa: S608 - column names are literals above
                f" WHERE id = ? AND ({identity} IS NULL OR {identity} = ?)",
                (*values, now_ms(), part_id, target),
            )
        except aiosqlite.IntegrityError as error:
            raise AlbumSettlementError(
                f"album {link_id}: part {part_index} cannot take {identity} {target};"
                " another part already claims it"
            ) from error
        if cursor.rowcount == 0:
            raise AlbumSettlementError(
                f"album {link_id}: part {part_index} already carries a different {identity}"
            )


def _outbox_item(row: aiosqlite.Row, *, state: OutboxState | None = None) -> OutboxItem:
    return OutboxItem(
        id=row["id"],
        bridge_name=row["bridge_name"],
        direction=Direction(row["direction"]),
        kind=row["kind"],
        payload_json=row["payload_json"],
        attempts=row["attempts"],
        next_attempt_at=row["next_attempt_at"],
        state=state if state is not None else OutboxState(row["state"]),
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        source_key=row["source_key"],
        lease_expires_at=row["lease_expires_at"],
        expires_at=row["expires_at"],
        remote_message_id=row["remote_message_id"],
        ambiguous_at=row["ambiguous_at"],
        send_started_at=row["send_started_at"],
    )


def _inbox_update(row: aiosqlite.Row, *, state: InboxState | None = None) -> InboxUpdate:
    return InboxUpdate(
        id=row["id"],
        bot_id=row["bot_id"],
        update_id=row["update_id"],
        bridge_name=row["bridge_name"],
        payload_json=row["payload_json"],
        state=state if state is not None else InboxState(row["state"]),
        attempts=row["attempts"],
        lease_expires_at=row["lease_expires_at"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _media_group_part(row: aiosqlite.Row) -> MediaGroupPart:
    caption_present = row["caption_present"]
    direction = row["direction"]
    return MediaGroupPart(
        id=row["id"],
        media_group_id=row["media_group_id"],
        bridge_name=row["bridge_name"],
        bot_id=row["bot_id"],
        telegram_message_id=row["telegram_message_id"],
        payload_json=row["payload_json"],
        created_at=row["created_at"],
        link_id=row["link_id"],
        direction=Direction(direction) if direction is not None else None,
        part_index=row["part_index"],
        media_kind=row["media_kind"],
        caption_present=None if caption_present is None else bool(caption_present),
        part_fingerprint=row["part_fingerprint"],
        telegram_owner_account_id=row["telegram_owner_account_id"],
        telegram_owner_message_id=row["telegram_owner_message_id"],
        updated_at=row["updated_at"],
    )


class BridgeIdentityConflictError(Exception):
    """Two bridges would hold one bot id or one username. V19 refuses.

    Its own type so nothing has to read an SQLite message: the batch classifies
    it as a permanent failure, and the guardian shows a sentence that names the
    bridge rather than the index.
    """

    #: Which column the driver complained about, in words for the owner. The
    #: SQLite text is the only thing that says which, and it must not be what the
    #: owner reads.
    _WHAT = (
        ("telegram_bot_id", "бот"),
        ("expected_username", "username"),
        ("max_chat_id", "диалог MAX"),
    )

    def __init__(self, record: BridgeRecord, cause: Exception) -> None:
        text = str(cause)
        detail = next(
            (word for column, word in self._WHAT if column in text), "идентификатор"
        )
        super().__init__(
            f"мост «{record.bridge_name}»: этот {detail} уже принадлежит другому мосту"
        )
        self.bridge_name = record.bridge_name


class BridgeRepository:
    """The registry of bridges. From dynamic provisioning this outranks the YAML file."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def upsert(self, record: BridgeRecord) -> None:
        try:
            await self._upsert(record)
        except sqlite3.IntegrityError as clash:
            # V19's identity indexes. Raised as something the guardian can read
            # and the batch can classify: a raw `IntegrityError` reaching a chat
            # tells the owner nothing and tells the code less.
            raise BridgeIdentityConflictError(record, clash) from clash

    async def _upsert(self, record: BridgeRecord) -> None:
        stamp = now_ms()
        await self._db.execute(
            "INSERT INTO bridges ("
            " bridge_name, max_chat_id, max_user_id, telegram_bot_id, token_env,"
            " title, source, state, expected_username, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(bridge_name) DO UPDATE SET"
            "  max_chat_id = excluded.max_chat_id,"
            "  max_user_id = COALESCE(excluded.max_user_id, bridges.max_user_id),"
            "  telegram_bot_id = COALESCE(excluded.telegram_bot_id, bridges.telegram_bot_id),"
            "  token_env = excluded.token_env,"
            "  title = COALESCE(excluded.title, bridges.title),"
            "  state = excluded.state,"
            "  expected_username = COALESCE("
            "   excluded.expected_username, bridges.expected_username),"
            "  updated_at = excluded.updated_at",
            (
                record.bridge_name,
                record.max_chat_id,
                record.max_user_id,
                record.telegram_bot_id,
                record.token_env,
                record.title,
                record.source,
                record.state.value,
                record.expected_username,
                stamp,
                stamp,
            ),
        )

    async def profile_signature(self, bridge_name: str) -> str | None:
        """What the bot's name and avatar were last set from (profile synchronisation)."""
        row = await self._db.query_one(
            "SELECT profile_signature FROM bridges WHERE bridge_name = ?", (bridge_name,)
        )
        return row["profile_signature"] if row else None

    async def set_profile_signature(self, bridge_name: str, signature: str) -> None:
        await self._db.execute(
            "UPDATE bridges SET profile_signature = ?, updated_at = ? WHERE bridge_name = ?",
            (signature, now_ms(), bridge_name),
        )

    async def pinned_status(self, bridge_name: str) -> tuple[int | None, str | None]:
        """The pinned presence line: its message id and what it currently says."""
        row = await self._db.query_one(
            "SELECT pin_message_id, pin_text FROM bridges WHERE bridge_name = ?", (bridge_name,)
        )
        if row is None:
            return None, None
        return row["pin_message_id"], row["pin_text"]

    async def set_pinned_status(
        self, bridge_name: str, *, message_id: int | None, text: str | None
    ) -> None:
        await self._db.execute(
            "UPDATE bridges SET pin_message_id = ?, pin_text = ?, updated_at = ?"
            " WHERE bridge_name = ?",
            (message_id, text, now_ms(), bridge_name),
        )

    async def history_cursor(self, bridge_name: str) -> int | None:
        """Newest MAX message already imported into this bridge, if any."""
        row = await self._db.query_one(
            "SELECT history_cursor FROM bridges WHERE bridge_name = ?", (bridge_name,)
        )
        return row["history_cursor"] if row else None

    async def set_history_cursor(self, bridge_name: str, cursor: int) -> None:
        """Only ever moves forward: an older tail must not rewind the watermark."""
        await self._db.execute(
            "UPDATE bridges SET history_cursor = MAX(COALESCE(history_cursor, 0), ?),"
            " updated_at = ? WHERE bridge_name = ?",
            (cursor, now_ms(), bridge_name),
        )

    async def set_history_floor(self, bridge_name: str, floor: int) -> None:
        """Nothing at or below this MAX message id is ever carried automatically.

        Only moves forward. The floor exists because the delivery dedup is keyed
        on the bot: change the bot behind a chat and its whole tail is unclaimed
        again, which is a second copy of the last fifty messages in the new chat.
        """
        await self._db.execute(
            "UPDATE bridges SET history_floor = MAX(COALESCE(history_floor, 0), ?),"
            " updated_at = ? WHERE bridge_name = ?",
            (floor, now_ms(), bridge_name),
        )

    async def clear_history_cursor(self, bridge_name: str) -> None:
        """Forget the watermark, so the next import starts from the beginning.

        A separate method because `set_history_cursor` deliberately only moves
        forward — passing it zero does nothing, which is exactly the protection
        that has to be stepped around, out loud, when the owner asks for a
        re-import from scratch.
        """
        await self._db.execute(
            "UPDATE bridges SET history_cursor = NULL, updated_at = ? WHERE bridge_name = ?",
            (now_ms(), bridge_name),
        )

    async def set_lifecycle(
        self,
        bridge_name: str,
        *,
        expected_username: str | None = None,
        lifecycle_state: str | None = None,
        health: str | None = None,
    ) -> None:
        """Record what provisioning knows, without touching what it does not."""
        await self._db.execute(
            "UPDATE bridges SET"
            " expected_username = COALESCE(?, expected_username),"
            " lifecycle_state = COALESCE(?, lifecycle_state),"
            " health = COALESCE(?, health),"
            " updated_at = ? WHERE bridge_name = ?",
            (expected_username, lifecycle_state, health, now_ms(), bridge_name),
        )

    async def by_expected_username(self, username: str) -> BridgeRecord | None:
        row = await self._db.query_one(
            "SELECT * FROM bridges WHERE expected_username = ?", (username.lower(),)
        )
        return self._record(row) if row else None

    async def get(self, bridge_name: str) -> BridgeRecord | None:
        row = await self._db.query_one(
            "SELECT * FROM bridges WHERE bridge_name = ?", (bridge_name,)
        )
        return self._record(row) if row else None

    async def by_max_chat(self, max_chat_id: int) -> BridgeRecord | None:
        row = await self._db.query_one(
            "SELECT * FROM bridges WHERE max_chat_id = ?", (max_chat_id,)
        )
        return self._record(row) if row else None

    async def active(self) -> list[BridgeRecord]:
        rows = await self._db.query(
            "SELECT * FROM bridges WHERE state = ? ORDER BY bridge_name",
            (BridgeState.ACTIVE.value,),
        )
        return [self._record(row) for row in rows]

    async def all(self) -> list[BridgeRecord]:
        """Every bridge this installation has, whatever state it is in.

        What ownership and capacity are counted from. A disabled bridge's bot
        still exists in Telegram and still occupies one of the twenty an account
        may own — reading only `active()` made it invisible, which is how a
        contact the owner had disconnected came back classified as somebody
        else's bot.
        """
        rows = await self._db.query("SELECT * FROM bridges ORDER BY bridge_name")
        return [self._record(row) for row in rows]

    async def by_bot_id(self, telegram_bot_id: int) -> BridgeRecord | None:
        row = await self._db.query_one(
            "SELECT * FROM bridges WHERE telegram_bot_id = ?", (telegram_bot_id,)
        )
        return self._record(row) if row else None

    async def set_state(self, bridge_name: str, state: BridgeState) -> None:
        await self._db.execute(
            "UPDATE bridges SET state = ?, updated_at = ? WHERE bridge_name = ?",
            (state.value, now_ms(), bridge_name),
        )

    @property
    def database(self) -> Any:
        """The connection this repository was built on.

        Exposed for the one caller that has to reach past the repository's own
        vocabulary: tearing a bridge down deletes from every table that carries
        a bridge identity, and that set is *derived by scanning the schema*
        rather than written out — precisely so a table added later is not
        forgotten. A repository method per table would be the hand-written list
        this avoids.
        """
        return self._db

    async def forget_bot(self, bridge_name: str) -> None:
        """Unbind the row from the bot it had. Everything about *that bot* goes.

        The row survives a bot — it holds the contact, the deterministic
        username and the MAX peer, none of which the bot owned. Four fields did
        belong to it, and leaving any of them behind hands a new bot claims
        about work that was done to a bot that no longer exists:

        * `telegram_bot_id` — not stale data but a live refusal. `_expected_bot`
          reads it, and a bridge rebuilt at the same username gets a *new* id,
          which the identity check then rejects as somebody else's.
        * `history_cursor` — how far the old chat was filled. The new chat is
          empty, and a cursor saying otherwise means «подтянуть историю» pulls
          nothing and the owner is looking at a bridge with no past.
        * `profile_signature` — proof the old bot was dressed as the contact.
          The sync skips a profile whose signature already matches, so the new
          bot keeps the default name and no avatar at all.
        * `pin_message_id` / `pin_text` — a pinned message in a chat that is not
          this bot's.

        `token_env` is left alone. It is NOT NULL, it names a variable rather
        than a secret, and the next run writes the new token under the same name.
        """
        await self._db.execute(
            "UPDATE bridges SET telegram_bot_id = NULL, history_cursor = NULL, "
            "profile_signature = NULL, pin_message_id = NULL, pin_text = NULL, "
            "updated_at = ? WHERE bridge_name = ?",
            (now_ms(), bridge_name),
        )

    @staticmethod
    def _record(row: aiosqlite.Row) -> BridgeRecord:
        return BridgeRecord(
            bridge_name=row["bridge_name"],
            max_chat_id=row["max_chat_id"],
            token_env=row["token_env"],
            max_user_id=row["max_user_id"],
            telegram_bot_id=row["telegram_bot_id"],
            title=row["title"],
            source=row["source"],
            state=BridgeState(row["state"]),
            expected_username=row["expected_username"],
            history_floor=row["history_floor"],
        )


class ReadStateRepository:
    """Read watermarks per bridge (read and presence state)."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def get(self, bridge_name: str) -> ReadMarks:
        row = await self._db.query_one(
            "SELECT * FROM read_state WHERE bridge_name = ?", (bridge_name,)
        )
        if row is None:
            return ReadMarks(bridge_name, 0, 0, None)
        return ReadMarks(
            bridge_name=row["bridge_name"],
            contact_read_mark=row["contact_read_mark"],
            own_read_mark=row["own_read_mark"],
            last_ticked_message_id=row["last_ticked_message_id"],
            status_message_id=row["status_message_id"],
            status_text=row["status_text"],
            delivered_at=row["delivered_at"] or 0,
        )

    async def note_delivered(self, bridge_name: str, *, at_ms: int) -> None:
        """MAX accepted one of our sends — the single tick.

        There is no separate delivery signal in MAX, so this
        is exactly what `✓` means: the server took the message.
        """
        await self._db.execute(
            "INSERT INTO read_state (bridge_name, delivered_at, updated_at)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(bridge_name) DO UPDATE SET"
            "  delivered_at = excluded.delivered_at,"
            "  updated_at = excluded.updated_at",
            (bridge_name, at_ms, now_ms()),
        )

    async def set_status_line(
        self, bridge_name: str, *, message_id: int | None, text: str | None
    ) -> None:
        await self._db.execute(
            "INSERT INTO read_state (bridge_name, status_message_id, status_text, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(bridge_name) DO UPDATE SET"
            "  status_message_id = excluded.status_message_id,"
            "  status_text = excluded.status_text,"
            "  updated_at = excluded.updated_at",
            (bridge_name, message_id, text, now_ms()),
        )

    async def note_contact_read(
        self, bridge_name: str, *, mark: int, ticked_message_id: int | None
    ) -> bool:
        """Record the contact's watermark. False when it is not newer than ours.

        Read marks arrive repeatedly with the same value (every reconnect, every
        chat sync); returning False is how the caller knows not to re-edit the
        Telegram message.
        """
        current = await self.get(bridge_name)
        if mark <= current.contact_read_mark:
            return False
        await self._db.execute(
            "INSERT INTO read_state ("
            " bridge_name, contact_read_mark, own_read_mark, last_ticked_message_id, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(bridge_name) DO UPDATE SET"
            "  contact_read_mark = excluded.contact_read_mark,"
            "  last_ticked_message_id = excluded.last_ticked_message_id,"
            "  updated_at = excluded.updated_at",
            (bridge_name, mark, current.own_read_mark, ticked_message_id, now_ms()),
        )
        return True

    async def note_own_read(self, bridge_name: str, *, mark: int) -> bool:
        current = await self.get(bridge_name)
        if mark <= current.own_read_mark:
            return False
        await self._db.execute(
            "INSERT INTO read_state ("
            " bridge_name, contact_read_mark, own_read_mark, last_ticked_message_id, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(bridge_name) DO UPDATE SET"
            "  own_read_mark = excluded.own_read_mark,"
            "  updated_at = excluded.updated_at",
            (
                bridge_name,
                current.contact_read_mark,
                mark,
                current.last_ticked_message_id,
                now_ms(),
            ),
        )
        return True


class ReactionStateRepository:
    """Last known reaction counters, so an update can be diffed (reaction synchronisation)."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def get(self, max_chat_id: int, max_message_id: int) -> ReactionSnapshot | None:
        row = await self._db.query_one(
            "SELECT * FROM reaction_state WHERE max_chat_id = ? AND max_message_id = ?",
            (max_chat_id, max_message_id),
        )
        if row is None:
            return None
        return ReactionSnapshot(
            max_chat_id=row["max_chat_id"],
            max_message_id=row["max_message_id"],
            counters=json.loads(row["counters_json"]),
            your_reaction=row["your_reaction"],
        )

    async def put(self, snapshot: ReactionSnapshot) -> None:
        await self._db.execute(
            "INSERT INTO reaction_state ("
            " max_chat_id, max_message_id, counters_json, your_reaction, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(max_chat_id, max_message_id) DO UPDATE SET"
            "  counters_json = excluded.counters_json,"
            "  your_reaction = excluded.your_reaction,"
            "  updated_at = excluded.updated_at",
            (
                snapshot.max_chat_id,
                snapshot.max_message_id,
                json.dumps(snapshot.counters, ensure_ascii=False),
                snapshot.your_reaction,
                now_ms(),
            ),
        )


class ForwardAuthorRepository:
    """Who wrote a forwarded message, learned from the transport that can see it.

    A forward's header has to name the author as *they* call themselves, never
    as the owner filed them. The owner's MTProto session cannot supply that: for
    a saved contact Telegram answers with the owner's own label and offers no
    way to ask for anything else — each client draws a forward header by
    resolving the peer locally, so the name is never on the wire.

    A contact bot has no address book, so the same peer reaches it as the
    profile itself.

    So this table is a bridge between the two transports, and it is on disk
    rather than in memory for one reason — a restart must not put the owner's
    private label for somebody back on the way to a contact.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    async def remember(self, peer_id: int, *, name: str, username: str | None = None) -> None:
        """Record what a bot was told. The newest answer wins: people rename."""
        cleaned = name.strip()
        if not cleaned:
            return
        await self._db.execute(
            "INSERT INTO forward_author (peer_id, name, username, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(peer_id) DO UPDATE SET"
            " name = excluded.name, username = excluded.username,"
            " updated_at = excluded.updated_at",
            (int(peer_id), cleaned, username, now_ms()),
        )

    async def author_of(self, peer_id: int) -> tuple[str, str | None] | None:
        """`(name, username)` a bot reported for this peer, or None."""
        row = await self._db.query_one(
            "SELECT name, username FROM forward_author WHERE peer_id = ?", (int(peer_id),)
        )
        if row is None:
            return None
        username = row["username"]
        return str(row["name"]), str(username) if username else None


class StickerCacheRepository:
    """Which MAX sticker a given PNG already became (sticker handling).

    Uploading to MAX does not reference a sticker, it *creates* one — the
    answer comes back with `authorType: USER` and lands in the owner's own
    collection. Sending the same sticker twice must therefore reuse the id
    rather than mint a second copy of it.

    Keyed by the hash of the converted PNG, not by a Telegram file id, because
    a retry re-fetches from Telegram and re-runs the conversion: the bytes are
    the thing that is stable across both paths.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    async def get(self, png_sha256: str) -> int | None:
        row = await self._db.query_one(
            "SELECT max_sticker_id FROM sticker_cache WHERE png_sha256 = ?",
            (png_sha256,),
        )
        return int(row["max_sticker_id"]) if row is not None else None

    async def put(self, png_sha256: str, max_sticker_id: int) -> None:
        await self._db.execute(
            "INSERT INTO sticker_cache (png_sha256, max_sticker_id, created_at)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(png_sha256) DO UPDATE SET max_sticker_id = excluded.max_sticker_id",
            (png_sha256, max_sticker_id, now_ms()),
        )


class StickerOriginRepository:
    """Which MAX sticker a file we handed Telegram *came from* (sticker handling).

    The other half of `StickerCacheRepository`, and the opposite direction.
    That one remembers stickers the bridge created in MAX; this one remembers
    stickers MAX already had, so that one arriving back from Telegram can be
    sent as itself — animation included — instead of being flattened into a new
    static copy.

    Keyed by the hash of the bytes we uploaded to Telegram. Telegram hands the
    same file back byte for byte, so the key survives the round trip without any
    id having to travel through the delivery queue.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    async def get(self, tg_sha256: str) -> int | None:
        row = await self._db.query_one(
            "SELECT max_sticker_id FROM sticker_origin WHERE tg_sha256 = ?",
            (tg_sha256,),
        )
        return int(row["max_sticker_id"]) if row is not None else None

    async def put(self, tg_sha256: str, max_sticker_id: int) -> None:
        await self._db.execute(
            "INSERT INTO sticker_origin (tg_sha256, max_sticker_id, created_at)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(tg_sha256) DO UPDATE SET max_sticker_id = excluded.max_sticker_id",
            (tg_sha256, max_sticker_id, now_ms()),
        )


class PendingContactRepository:
    """Contacts without a bridge yet, and messages they sent meanwhile."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def note_seen(
        self, *, max_chat_id: int, max_user_id: int | None, display_name: str | None
    ) -> PendingContact:
        stamp = now_ms()
        await self._db.execute(
            "INSERT INTO pending_contacts ("
            " max_chat_id, max_user_id, display_name, state, first_seen_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(max_chat_id) DO UPDATE SET"
            "  max_user_id = COALESCE(excluded.max_user_id, pending_contacts.max_user_id),"
            "  display_name = COALESCE(excluded.display_name, pending_contacts.display_name),"
            "  updated_at = excluded.updated_at",
            (
                max_chat_id,
                max_user_id,
                display_name,
                PendingContactState.NEW.value,
                stamp,
                stamp,
            ),
        )
        contact = await self.get(max_chat_id)
        assert contact is not None  # just inserted
        return contact

    async def get(self, max_chat_id: int) -> PendingContact | None:
        row = await self._db.query_one(
            "SELECT p.*, ("
            " SELECT COUNT(*) FROM pending_inbox i WHERE i.max_chat_id = p.max_chat_id"
            ") AS buffered FROM pending_contacts p WHERE p.max_chat_id = ?",
            (max_chat_id,),
        )
        if row is None:
            return None
        return PendingContact(
            max_chat_id=row["max_chat_id"],
            max_user_id=row["max_user_id"],
            display_name=row["display_name"],
            state=PendingContactState(row["state"]),
            first_seen_at=row["first_seen_at"],
            asked_at=row["asked_at"],
            buffered=row["buffered"],
        )

    async def set_state(self, max_chat_id: int, state: PendingContactState) -> None:
        stamp = now_ms()
        asked = stamp if state is PendingContactState.ASKED else None
        await self._db.execute(
            "UPDATE pending_contacts SET state = ?,"
            " asked_at = COALESCE(?, asked_at), updated_at = ? WHERE max_chat_id = ?",
            (state.value, asked, stamp, max_chat_id),
        )

    async def note_asked(self, max_chat_id: int) -> int:
        """Mark the contact asked about and return the revision of that question.

        `asked_at` doubles as the nonce the announcement's buttons carry. It is
        durable, it is per contact, and it moves every time a new question is
        put — which is what makes a button from a question already answered do
        nothing at all, including after a restart.
        """
        stamp = now_ms()
        # `MAX(?, asked_at + 1)`: the nonce has to *move*, and two questions in
        # the same millisecond would otherwise land on the same number — which
        # would let the first question's button answer the second one.
        await self._db.execute(
            "UPDATE pending_contacts SET state = ?,"
            " asked_at = MAX(?, COALESCE(asked_at, 0) + 1), updated_at = ?"
            " WHERE max_chat_id = ?",
            (PendingContactState.ASKED.value, stamp, stamp, max_chat_id),
        )
        contact = await self.get(max_chat_id)
        return int(contact.asked_at or 0) if contact else 0

    async def consume_announcement(self, max_chat_id: int, revision: int) -> bool:
        """Spend the announcement's one answer, atomically. False when it is stale.

        A compare-and-swap in one statement rather than a read and a write: two
        taps land in the same millisecond often enough, and the whole point is
        that only one of them may proceed to create anything.
        """
        stamp = now_ms()
        changed = await self._db.execute_changed(
            "UPDATE pending_contacts SET asked_at = MAX(?, asked_at + 1), updated_at = ?"
            " WHERE max_chat_id = ? AND asked_at = ?",
            (stamp, stamp, max_chat_id, revision),
        )
        return changed == 1

    async def buffer(self, max_chat_id: int, payload: dict[str, Any], *, cap: int) -> bool:
        """Hold a message until a bridge exists. False when the cap is reached.

        The cap is not a nicety: an unknown contact could otherwise fill the
        disk before anyone answers the guardian's question.
        """
        row = await self._db.query_one(
            "SELECT COUNT(*) AS held FROM pending_inbox WHERE max_chat_id = ?", (max_chat_id,)
        )
        if row is not None and int(row["held"]) >= cap:
            return False
        await self._db.execute(
            "INSERT INTO pending_inbox (max_chat_id, payload_json, created_at) VALUES (?, ?, ?)",
            (max_chat_id, json.dumps(payload, ensure_ascii=False), now_ms()),
        )
        return True

    async def drain(self, max_chat_id: int) -> list[dict[str, Any]]:
        """Return the held messages in arrival order and forget them."""
        rows = await self._db.query(
            "SELECT payload_json FROM pending_inbox WHERE max_chat_id = ? ORDER BY id",
            (max_chat_id,),
        )
        await self._db.execute("DELETE FROM pending_inbox WHERE max_chat_id = ?", (max_chat_id,))
        return [json.loads(row["payload_json"]) for row in rows]

    async def count_in_state(self, state: PendingContactState) -> int:
        row = await self._db.query_one(
            "SELECT COUNT(*) AS held FROM pending_contacts WHERE state = ?", (state.value,)
        )
        return int(row["held"]) if row else 0

    async def expire(self, *, older_than_ms: int) -> int:
        """Drop buffered messages nobody claimed. Returns how many went."""
        cutoff = now_ms() - older_than_ms
        rows = await self._db.query("SELECT id FROM pending_inbox WHERE created_at < ?", (cutoff,))
        if rows:
            await self._db.execute("DELETE FROM pending_inbox WHERE created_at < ?", (cutoff,))
        return len(rows)


class BridgeStateRepository:
    """What `/status` reports. A cache of observations, never authoritative."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def note_delivery(self, bridge_name: str) -> None:
        await self._db.execute(
            "INSERT INTO bridge_state (bridge_name, last_delivery_at) VALUES (?, ?)"
            " ON CONFLICT(bridge_name) DO UPDATE SET last_delivery_at = excluded.last_delivery_at",
            (bridge_name, now_ms()),
        )

    async def note_error(self, bridge_name: str, error: str) -> None:
        stamp = now_ms()
        await self._db.execute(
            "INSERT INTO bridge_state (bridge_name, last_error_at, last_error) VALUES (?, ?, ?)"
            " ON CONFLICT(bridge_name) DO UPDATE SET"
            "  last_error_at = excluded.last_error_at, last_error = excluded.last_error",
            (bridge_name, stamp, error[:500]),
        )

    async def snapshot(self, bridge_name: str) -> dict[str, Any] | None:
        row = await self._db.query_one(
            "SELECT * FROM bridge_state WHERE bridge_name = ?", (bridge_name,)
        )
        return dict(row) if row else None


class HealthStateRepository:
    """The handful of facts about the process that must outlive it.

    Deliberately a few scalars rather than a metrics store. Everything countable
    — queue depths, failed jobs, the age of the oldest pending item — is counted
    from the tables that already hold it, at the moment somebody asks. What
    cannot be recounted is history: when this process started, when MAX last
    dropped, whether the previous shutdown was clean. Those are written here.

    Writes are rare on purpose. A heartbeat every second would be a write every
    second, for a number nobody reads between restarts.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    async def set(self, key: str, value: Any) -> None:
        await self._db.execute(
            "INSERT INTO health_state (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), now_ms()),
        )

    async def get(self, key: str, default: Any = None) -> Any:
        row = await self._db.query_one("SELECT value FROM health_state WHERE key = ?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    async def bump(self, key: str) -> int:
        current = await self.get(key, 0)
        value = int(current) + 1 if isinstance(current, int | float) else 1
        await self.set(key, value)
        return value

    async def all(self) -> dict[str, Any]:
        rows = await self._db.query("SELECT key, value FROM health_state")
        out: dict[str, Any] = {}
        for row in rows:
            try:
                out[str(row["key"])] = json.loads(row["value"])
            except (TypeError, ValueError):
                continue
        return out


class AlertRepository:
    """Alerts the owner must see, and the incidents that stop them repeating.

    Two tables because they answer two questions. `alert_outbox` is a queue: a
    message that has to reach Telegram eventually, surviving the outage that
    probably caused it. `alert_incidents` is memory: this problem is already
    known, so do not say it again — a queue forty jobs deep is one incident, not
    forty notifications.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    async def open_incident(
        self,
        *,
        incident_key: str,
        text: str,
        severity: str = "warning",
        cooldown_ms: int = 0,
    ) -> int | None:
        """Raise an incident once. Returns the queued alert id, or None.

        `None` means the incident is already open and still inside its cooldown,
        which is the common case: the condition is checked on a timer and would
        otherwise fire on every pass.
        """
        stamp = now_ms()
        async with self._db.transaction() as connection:
            async with connection.execute(
                "SELECT opened_at, last_alert_at, resolved_at FROM alert_incidents"
                " WHERE incident_key = ?",
                (incident_key,),
            ) as cursor:
                existing = await cursor.fetchone()

            if existing is not None and existing["resolved_at"] is None:
                if cooldown_ms <= 0 or stamp - int(existing["last_alert_at"]) < cooldown_ms:
                    return None
                await connection.execute(
                    "UPDATE alert_incidents SET last_alert_at = ? WHERE incident_key = ?",
                    (stamp, incident_key),
                )
            else:
                await connection.execute(
                    "INSERT INTO alert_incidents (incident_key, opened_at, last_alert_at,"
                    " resolved_at) VALUES (?, ?, ?, NULL)"
                    " ON CONFLICT(incident_key) DO UPDATE SET opened_at = excluded.opened_at,"
                    " last_alert_at = excluded.last_alert_at, resolved_at = NULL",
                    (incident_key, stamp, stamp),
                )

            cursor = await connection.execute(
                "INSERT INTO alert_outbox (incident_key, severity, text, state, attempts,"
                " next_attempt_at, created_at, updated_at) VALUES (?, ?, ?, 'pending', 0, ?, ?, ?)",
                (incident_key, severity, text, stamp, stamp, stamp),
            )
            return int(cursor.lastrowid or 0)

    async def resolve_incident(self, *, incident_key: str, text: str | None = None) -> int | None:
        """Close an incident, and say so once if it was open."""
        stamp = now_ms()
        async with self._db.transaction() as connection:
            async with connection.execute(
                "SELECT resolved_at FROM alert_incidents WHERE incident_key = ?",
                (incident_key,),
            ) as cursor:
                existing = await cursor.fetchone()
            if existing is None or existing["resolved_at"] is not None:
                # Never opened, or already closed. A recovery notice for
                # something the owner was never told about is just noise.
                return None

            await connection.execute(
                "UPDATE alert_incidents SET resolved_at = ? WHERE incident_key = ?",
                (stamp, incident_key),
            )
            if text is None:
                return None
            cursor = await connection.execute(
                "INSERT INTO alert_outbox (incident_key, severity, text, state, attempts,"
                " next_attempt_at, created_at, updated_at) VALUES (?, ?, ?, 'pending', 0, ?, ?, ?)",
                (incident_key, "recovery", text, stamp, stamp, stamp),
            )
            return int(cursor.lastrowid or 0)

    async def is_open(self, incident_key: str) -> bool:
        row = await self._db.query_one(
            "SELECT resolved_at FROM alert_incidents WHERE incident_key = ?", (incident_key,)
        )
        return row is not None and row["resolved_at"] is None

    async def remember_many(self, keys: list[InboxKey]) -> int:
        """Write down every target of one Telegram update, together.

        One `UpdateDeleteMessages` names several messages and is one decision by
        the owner. Half of it written down is worse than none: the half that
        survived would be carried and the other half silently would not.
        """
        if not keys:
            return 0
        stamp = now_ms()
        written = 0
        async with self._db.transaction() as connection:
            for key in keys:
                cursor = await connection.execute(
                    "INSERT INTO owner_update_inbox ("
                    " telegram_owner_account_id, telegram_bot_id,"
                    " telegram_owner_message_id, family, pts,"
                    " state, attempts, next_attempt_at, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, 'open', 0, ?, ?, ?)"
                    " ON CONFLICT (telegram_owner_account_id, telegram_bot_id,"
                    "              telegram_owner_message_id, family, pts) DO NOTHING",
                    (*_key_params(key), stamp, stamp, stamp),
                )
                written += cursor.rowcount if cursor.rowcount > 0 else 0
        return written

    async def next_due_ms(self) -> int | None:
        """When the scheduler should wake next, or None if there is nothing.

        `accounted` and `dead` are excluded by the same states the claim uses:
        an accounted row is finished and a dead one waits for the owner, and
        waking for either would be a loop with nothing to do.
        """
        row = await self._db.query_one(
            "SELECT min(due) AS due FROM ("
            "  SELECT next_attempt_at AS due FROM owner_update_inbox WHERE state = 'open'"
            "  UNION ALL"
            "  SELECT lease_expires_at AS due FROM owner_update_inbox"
            "   WHERE state = 'claimed' AND lease_expires_at IS NOT NULL"
            ")"
        )
        return int(row["due"]) if row and row["due"] is not None else None

    async def claim_due(self, *, limit: int = 5) -> list[dict[str, Any]]:
        """Alerts ready to be sent, oldest first."""
        rows = await self._db.query(
            "SELECT * FROM alert_outbox WHERE state = 'pending' AND next_attempt_at <= ?"
            " ORDER BY id LIMIT ?",
            (now_ms(), limit),
        )
        return [dict(row) for row in rows]

    async def mark_sent(self, alert_id: int) -> None:
        await self._db.execute(
            "UPDATE alert_outbox SET state = 'sent', updated_at = ? WHERE id = ?",
            (now_ms(), alert_id),
        )

    async def mark_retry(self, alert_id: int, *, delay_ms: int, error: str) -> None:
        stamp = now_ms()
        await self._db.execute(
            "UPDATE alert_outbox SET attempts = attempts + 1, next_attempt_at = ?,"
            " last_error = ?, updated_at = ? WHERE id = ?",
            (stamp + delay_ms, error[:500], stamp, alert_id),
        )

    async def mark_failed(self, alert_id: int, *, error: str) -> None:
        """Give up on one alert. The incident stays open; the owner still has /status."""
        stamp = now_ms()
        await self._db.execute(
            "UPDATE alert_outbox SET state = 'failed', attempts = attempts + 1,"
            " last_error = ?, updated_at = ? WHERE id = ?",
            (error[:500], stamp, alert_id),
        )

    async def pending_count(self) -> int:
        row = await self._db.query_one(
            "SELECT COUNT(*) AS n FROM alert_outbox WHERE state = 'pending'"
        )
        return int(row["n"]) if row else 0


class OwnerMessageStateRepository:
    """What the puppet session last confirmed about an owner message.

    Two writers with different rules, and the difference matters:

    * `advance` is the live path and is a compare-and-set on `pts`. An update
      that is older than what is stored cannot move the row, so a catch-up
      replaying an old version after a newer one has landed changes nothing —
      and, because the caller only advances once every derived effect is
      accounted for, a crash in between leaves the row old and the next reading
      of that message re-derives what was missed.
    * `seed` is the bootstrap path and never overwrites. A row already written by
      a live update is newer than any fetch, so the fetch steps aside rather than
      dragging the state backwards.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    async def get(
        self, *, account_id: int, bot_id: int, message_id: int
    ) -> OwnerMessageState | None:
        row = await self._db.query_one(
            "SELECT * FROM owner_message_state"
            " WHERE telegram_owner_account_id = ? AND telegram_bot_id = ?"
            "   AND telegram_owner_message_id = ?",
            (account_id, bot_id, message_id),
        )
        return _owner_message_state(row) if row else None

    async def advance(
        self,
        *,
        account_id: int,
        bot_id: int,
        message_id: int,
        content_fingerprint: str,
        chosen_json: str,
        pts: int,
    ) -> bool:
        """Move the row forward, or refuse because something newer is there.

        One statement, so there is no window between reading `pts` and writing
        it: two handlers racing on the same message both run this, and the
        second one's `WHERE` decides it rather than a value it read earlier.
        """
        rows = await self._db.query(
            "INSERT INTO owner_message_state ("
            " telegram_owner_account_id, telegram_bot_id, telegram_owner_message_id,"
            " content_fingerprint, chosen_json, pts, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (telegram_owner_account_id, telegram_bot_id,"
            "              telegram_owner_message_id)"
            " DO UPDATE SET content_fingerprint = excluded.content_fingerprint,"
            "               chosen_json = excluded.chosen_json,"
            "               pts = excluded.pts,"
            "               updated_at = excluded.updated_at"
            "  WHERE excluded.pts > owner_message_state.pts"
            " RETURNING pts",
            (
                account_id,
                bot_id,
                message_id,
                content_fingerprint,
                chosen_json,
                pts,
                now_ms(),
            ),
        )
        return bool(rows)

    async def seed(
        self,
        *,
        account_id: int,
        bot_id: int,
        message_id: int,
        content_fingerprint: str,
        chosen_json: str,
    ) -> bool:
        """Write a baseline for a message that has none. Never overwrites."""
        rows = await self._db.query(
            "INSERT INTO owner_message_state ("
            " telegram_owner_account_id, telegram_bot_id, telegram_owner_message_id,"
            " content_fingerprint, chosen_json, pts, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 0, ?)"
            " ON CONFLICT (telegram_owner_account_id, telegram_bot_id,"
            "              telegram_owner_message_id) DO NOTHING"
            " RETURNING pts",
            (account_id, bot_id, message_id, content_fingerprint, chosen_json, now_ms()),
        )
        return bool(rows)

    async def count(self) -> int:
        row = await self._db.query_one("SELECT count(*) AS n FROM owner_message_state")
        return int(row["n"]) if row else 0


def _owner_message_state(row: Any) -> OwnerMessageState:
    return OwnerMessageState(
        telegram_owner_account_id=row["telegram_owner_account_id"],
        telegram_bot_id=row["telegram_bot_id"],
        telegram_owner_message_id=row["telegram_owner_message_id"],
        content_fingerprint=row["content_fingerprint"],
        chosen_json=row["chosen_json"],
        pts=row["pts"],
        updated_at=row["updated_at"],
    )


class OwnerUpdateInboxRepository:
    """Every owner update, written down before anything is derived from it.

    The guarantee is narrow and worth keeping narrow: **after the insert
    commits, the event survives a crash and will be finished.** Before it
    commits, Telegram has delivered an update and nothing has written it down,
    and nothing here changes that — Telethon advances `pts` before the handler
    runs and does not replay.

    `attempts` counts *failures*, the same as the outbox: a claim does not touch
    it, and a row that fails is reopened with one more. Two conventions for one
    word in one codebase is how a retry limit ends up meaning two things.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    async def remember(
        self,
        key: InboxKey,
        *,
        content_text: str | None = None,
        content_fingerprint: str | None = None,
        chosen_json: str | None = None,
    ) -> bool:
        """Write one update down. A second insert of the same one does nothing.

        `DO NOTHING` rather than an upsert on purpose: a replayed update must
        not reset `attempts`, revive a `dead` row, un-`accounted` a finished one
        or take a row away from whoever has it claimed.
        """
        stamp = now_ms()
        rows = await self._db.query(
            "INSERT INTO owner_update_inbox ("
            " telegram_owner_account_id, telegram_bot_id, telegram_owner_message_id,"
            " family, pts, content_text, content_fingerprint, chosen_json,"
            " state, attempts, next_attempt_at, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', 0, ?, ?, ?)"
            " ON CONFLICT (telegram_owner_account_id, telegram_bot_id,"
            "              telegram_owner_message_id, family, pts) DO NOTHING"
            " RETURNING pts",
            (
                key.account_id,
                key.bot_id,
                key.message_id,
                key.family.value,
                key.pts,
                content_text,
                content_fingerprint,
                chosen_json,
                stamp,
                stamp,
                stamp,
            ),
        )
        return bool(rows)

    async def remember_many(self, keys: list[InboxKey]) -> int:
        """Write down every target of one Telegram update, together.

        One `UpdateDeleteMessages` names several messages and is one decision by
        the owner. Half of it written down is worse than none: the half that
        survived would be carried and the other half silently would not.
        """
        if not keys:
            return 0
        stamp = now_ms()
        written = 0
        async with self._db.transaction() as connection:
            for key in keys:
                cursor = await connection.execute(
                    "INSERT INTO owner_update_inbox ("
                    " telegram_owner_account_id, telegram_bot_id,"
                    " telegram_owner_message_id, family, pts,"
                    " state, attempts, next_attempt_at, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, 'open', 0, ?, ?, ?)"
                    " ON CONFLICT (telegram_owner_account_id, telegram_bot_id,"
                    "              telegram_owner_message_id, family, pts) DO NOTHING",
                    (*_key_params(key), stamp, stamp, stamp),
                )
                written += cursor.rowcount if cursor.rowcount > 0 else 0
        return written

    async def next_due_ms(self) -> int | None:
        """When the scheduler should wake next, or None if there is nothing.

        `accounted` and `dead` are excluded by the same states the claim uses:
        an accounted row is finished and a dead one waits for the owner, and
        waking for either would be a loop with nothing to do.
        """
        row = await self._db.query_one(
            "SELECT min(due) AS due FROM ("
            "  SELECT next_attempt_at AS due FROM owner_update_inbox WHERE state = 'open'"
            "  UNION ALL"
            "  SELECT lease_expires_at AS due FROM owner_update_inbox"
            "   WHERE state = 'claimed' AND lease_expires_at IS NOT NULL"
            ")"
        )
        return int(row["due"]) if row and row["due"] is not None else None

    async def claim_due(self, *, limit: int = 10, lease_ms: int = 60_000) -> list[OwnerUpdate]:
        """Take ownership of what is due, in one transaction.

        Due is `open` past its `next_attempt_at`, or `claimed` past its lease —
        a row whose owner died holding it. Both in one statement so two drainers
        cannot both decide a row is theirs.
        """
        stamp = now_ms()
        async with self._db.transaction() as connection:
            async with connection.execute(
                "SELECT * FROM owner_update_inbox"
                " WHERE (state = 'open' AND next_attempt_at <= ?)"
                "    OR (state = 'claimed' AND lease_expires_at <= ?)"
                " ORDER BY next_attempt_at, created_at LIMIT ?",
                (stamp, stamp, limit),
            ) as cursor:
                rows = list(await cursor.fetchall())
            for row in rows:
                await connection.execute(
                    "UPDATE owner_update_inbox"
                    " SET state = 'claimed', lease_expires_at = ?, updated_at = ?"
                    " WHERE telegram_owner_account_id = ? AND telegram_bot_id = ?"
                    "   AND telegram_owner_message_id = ? AND family = ? AND pts = ?",
                    (
                        stamp + lease_ms,
                        stamp,
                        row["telegram_owner_account_id"],
                        row["telegram_bot_id"],
                        row["telegram_owner_message_id"],
                        row["family"],
                        row["pts"],
                    ),
                )
        return [_owner_update(row) for row in rows]

    async def reopen(self, key: InboxKey, *, delay_ms: int, error: str) -> None:
        """A transient failure. One more attempt on the count, and a wait."""
        stamp = now_ms()
        await self._db.execute(
            "UPDATE owner_update_inbox"
            " SET state = 'open', attempts = attempts + 1, lease_expires_at = NULL,"
            "     next_attempt_at = ?, last_error = ?, updated_at = ?"
            " WHERE telegram_owner_account_id = ? AND telegram_bot_id = ?"
            "   AND telegram_owner_message_id = ? AND family = ? AND pts = ?"
            "   AND state <> 'accounted'",
            (stamp + delay_ms, error, stamp, *_key_params(key)),
        )

    async def bury(self, key: InboxKey, *, error: str) -> None:
        """Retrying will not help. The row stays — it is a question, not litter."""
        stamp = now_ms()
        await self._db.execute(
            "UPDATE owner_update_inbox"
            " SET state = 'dead', attempts = attempts + 1, lease_expires_at = NULL,"
            "     last_error = ?, updated_at = ?"
            " WHERE telegram_owner_account_id = ? AND telegram_bot_id = ?"
            "   AND telegram_owner_message_id = ? AND family = ? AND pts = ?"
            "   AND state <> 'accounted'",
            (error, stamp, *_key_params(key)),
        )

    async def account(self, key: InboxKey) -> None:
        """Every effect this row stands for is durable. Mark it, then drop it.

        Two statements rather than one delete, because a crash between them must
        be harmless: an `accounted` row that was not deleted is skipped by the
        claim and removed by the next sweep, while a row deleted before its
        effects were durable would be an event that quietly never happened.

        The text goes with the row. It is the one piece of message content this
        bridge keeps at rest, and it is kept only for as long as the work is
        unfinished.
        """
        stamp = now_ms()
        await self._db.execute(
            "UPDATE owner_update_inbox"
            " SET state = 'accounted', accounted_at = ?, updated_at = ?,"
            "     content_text = NULL, chosen_json = NULL, lease_expires_at = NULL"
            " WHERE telegram_owner_account_id = ? AND telegram_bot_id = ?"
            "   AND telegram_owner_message_id = ? AND family = ? AND pts = ?",
            (stamp, stamp, *_key_params(key)),
        )
        await self._db.execute(
            "DELETE FROM owner_update_inbox"
            " WHERE telegram_owner_account_id = ? AND telegram_bot_id = ?"
            "   AND telegram_owner_message_id = ? AND family = ? AND pts = ?"
            "   AND state = 'accounted'",
            _key_params(key),
        )

    async def sweep_accounted(self) -> int:
        """Remove rows whose work is done. It can match nothing else.

        The state is in the `WHERE` rather than an age: an old row that is not
        accounted is the problem, not the litter, and deleting it by age is how
        an unfinished event disappears without anybody deciding it should.
        """
        rows = await self._db.query(
            "DELETE FROM owner_update_inbox WHERE state = 'accounted' RETURNING pts"
        )
        return len(rows)

    async def counts(self) -> dict[str, int]:
        rows = await self._db.query(
            "SELECT state, count(*) AS n FROM owner_update_inbox GROUP BY state"
        )
        return {str(row["state"]): int(row["n"]) for row in rows}

    async def oldest_open_ms(self) -> int | None:
        row = await self._db.query_one(
            "SELECT min(created_at) AS oldest FROM owner_update_inbox"
            " WHERE state IN ('open', 'claimed')"
        )
        return int(row["oldest"]) if row and row["oldest"] is not None else None

    async def stuck(self, *, limit: int = 20) -> list[OwnerUpdate]:
        """What the owner is being asked about. Never any content."""
        rows = await self._db.query(
            "SELECT * FROM owner_update_inbox WHERE state = 'dead'"
            " ORDER BY created_at LIMIT ?",
            (limit,),
        )
        return [_owner_update(row) for row in rows]

    async def retry(self, key: InboxKey) -> bool:
        """Put a dead row back in the queue, on the owner's word."""
        stamp = now_ms()
        rows = await self._db.query(
            "UPDATE owner_update_inbox"
            " SET state = 'open', next_attempt_at = ?, lease_expires_at = NULL,"
            "     last_error = NULL, updated_at = ?"
            " WHERE telegram_owner_account_id = ? AND telegram_bot_id = ?"
            "   AND telegram_owner_message_id = ? AND family = ? AND pts = ?"
            "   AND state = 'dead'"
            " RETURNING pts",
            (stamp, stamp, *_key_params(key)),
        )
        return bool(rows)

    async def archive(self, key: InboxKey) -> bool:
        """Give up on a dead row deliberately. It does **not** run the effect."""
        rows = await self._db.query(
            "DELETE FROM owner_update_inbox"
            " WHERE telegram_owner_account_id = ? AND telegram_bot_id = ?"
            "   AND telegram_owner_message_id = ? AND family = ? AND pts = ?"
            "   AND state = 'dead'"
            " RETURNING pts",
            _key_params(key),
        )
        return bool(rows)


def _key_params(key: InboxKey) -> tuple[int, int, int, str, int]:
    return (key.account_id, key.bot_id, key.message_id, key.family.value, key.pts)


def _owner_update(row: Any) -> OwnerUpdate:
    return OwnerUpdate(
        key=InboxKey(
            account_id=row["telegram_owner_account_id"],
            bot_id=row["telegram_bot_id"],
            message_id=row["telegram_owner_message_id"],
            family=InboxFamily(row["family"]),
            pts=row["pts"],
        ),
        state=OwnerUpdateState(row["state"]),
        attempts=row["attempts"],
        content_text=row["content_text"],
        content_fingerprint=row["content_fingerprint"],
        chosen_json=row["chosen_json"],
        created_at=row["created_at"],
        last_error=row["last_error"],
    )
