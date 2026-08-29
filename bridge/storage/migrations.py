"""Schema migrations.

Versioned steps in code, no Alembic: the schema is small, the deployment is a
single personal process, and an extra dependency that rewrites tables at
startup is a worse trade than a list of SQL strings.

Rules for adding a step:

* append, never edit an existing one — someone's database already ran it;
* every step must be safe to re-run after a crash halfway through (the whole
  step runs in one transaction, so this mostly means avoiding `IF NOT EXISTS`
  gymnastics and writing plain DDL);
* never add a column that holds a secret. Tokens live in a 0600 file; the
  database only ever stores the *name* of an environment variable.
"""

from __future__ import annotations

from typing import Final

Step = tuple[int, tuple[str, ...]]

_V1: Final[tuple[str, ...]] = (
    # Which Telegram message corresponds to which MAX message, in both
    # directions. Written *before* delivery so a crash mid-send cannot produce a
    # duplicate on restart.
    """
    CREATE TABLE message_map (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        bridge_name         TEXT    NOT NULL,
        max_chat_id         INTEGER NOT NULL,
        max_message_id      INTEGER,
        telegram_bot_id     INTEGER NOT NULL,
        telegram_chat_id    INTEGER NOT NULL,
        telegram_message_id INTEGER,
        direction           TEXT    NOT NULL,
        source_marker       TEXT    NOT NULL,
        created_at          INTEGER NOT NULL
    )
    """,
    # The dedup key for MAX -> Telegram. Partial index: rows still waiting for a
    # max_message_id (Telegram -> MAX, before the server answers) must not
    # collide with each other.
    """
    CREATE UNIQUE INDEX idx_message_map_max
        ON message_map (max_chat_id, max_message_id, telegram_bot_id)
        WHERE max_message_id IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX idx_message_map_telegram
        ON message_map (telegram_bot_id, telegram_message_id)
        WHERE telegram_message_id IS NOT NULL
    """,
    "CREATE INDEX idx_message_map_bridge ON message_map (bridge_name, id)",
    # Persistent delivery queue, one logical queue per bridge.
    """
    CREATE TABLE outbox (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        bridge_name     TEXT    NOT NULL,
        direction       TEXT    NOT NULL,
        kind            TEXT    NOT NULL,
        payload_json    TEXT    NOT NULL,
        attempts        INTEGER NOT NULL DEFAULT 0,
        next_attempt_at INTEGER NOT NULL,
        state           TEXT    NOT NULL DEFAULT 'pending',
        last_error      TEXT,
        created_at      INTEGER NOT NULL,
        updated_at      INTEGER NOT NULL
    )
    """,
    "CREATE INDEX idx_outbox_due ON outbox (bridge_name, state, next_attempt_at)",
    # Health, as shown by /status. Nothing here is authoritative; it is a cache
    # of what the supervisor last observed.
    """
    CREATE TABLE bridge_state (
        bridge_name      TEXT PRIMARY KEY,
        telegram_bot_id  INTEGER,
        max_chat_id      INTEGER,
        last_delivery_at INTEGER,
        last_error_at    INTEGER,
        last_error       TEXT,
        queue_size       INTEGER NOT NULL DEFAULT 0
    )
    """,
    # Album assembly: Telegram delivers a media group as separate updates.
    """
    CREATE TABLE pending_media_group (
        media_group_id TEXT PRIMARY KEY,
        bridge_name    TEXT    NOT NULL,
        items_json     TEXT    NOT NULL,
        created_at     INTEGER NOT NULL
    )
    """,
)

_V2: Final[tuple[str, ...]] = (
    # The bridge registry. From dynamic provisioning this is the source of truth for which
    # bridges exist; YAML is only a seed. `token_env` is a variable name, never
    # a token — see the module docstring.
    """
    CREATE TABLE bridges (
        bridge_name     TEXT PRIMARY KEY,
        max_chat_id     INTEGER NOT NULL UNIQUE,
        max_user_id     INTEGER,
        telegram_bot_id INTEGER,
        token_env       TEXT    NOT NULL,
        title           TEXT,
        source          TEXT    NOT NULL DEFAULT 'yaml',
        state           TEXT    NOT NULL DEFAULT 'active',
        created_at      INTEGER NOT NULL,
        updated_at      INTEGER NOT NULL
    )
    """,
    # Read watermarks (read and presence state). MAX reports reading as "everything up to `mark`",
    # so per-message tick state would be a lie; one watermark per side is the
    # honest shape.
    """
    CREATE TABLE read_state (
        bridge_name             TEXT PRIMARY KEY,
        contact_read_mark       INTEGER NOT NULL DEFAULT 0,
        own_read_mark           INTEGER NOT NULL DEFAULT 0,
        last_ticked_message_id  INTEGER,
        updated_at              INTEGER NOT NULL
    )
    """,
    # Previous reaction counters per message (reaction synchronisation). MAX sends totals, not who
    # reacted; the delta against this snapshot is what tells us what changed.
    """
    CREATE TABLE reaction_state (
        max_chat_id    INTEGER NOT NULL,
        max_message_id INTEGER NOT NULL,
        counters_json  TEXT    NOT NULL,
        your_reaction  TEXT,
        updated_at     INTEGER NOT NULL,
        PRIMARY KEY (max_chat_id, max_message_id)
    )
    """,
    # A contact who wrote before a bridge existed for them (dynamic provisioning).
    """
    CREATE TABLE pending_contacts (
        max_chat_id  INTEGER PRIMARY KEY,
        max_user_id  INTEGER,
        display_name TEXT,
        state        TEXT    NOT NULL DEFAULT 'new',
        asked_at     INTEGER,
        first_seen_at INTEGER NOT NULL,
        updated_at   INTEGER NOT NULL
    )
    """,
    # Their messages, held until a bridge exists — then replayed in order.
    """
    CREATE TABLE pending_inbox (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        max_chat_id  INTEGER NOT NULL,
        payload_json TEXT    NOT NULL,
        created_at   INTEGER NOT NULL
    )
    """,
    "CREATE INDEX idx_pending_inbox_chat ON pending_inbox (max_chat_id, id)",
)

# The status line (read and presence state): a bot cannot edit a message the owner sent, so a text
# tick has to live in a message of the bot's own. One per bridge, edited in
# place and re-posted when the conversation has moved past it.
_V3: Final[tuple[str, ...]] = (
    "ALTER TABLE read_state ADD COLUMN status_message_id INTEGER",
    "ALTER TABLE read_state ADD COLUMN status_text TEXT",
    "ALTER TABLE read_state ADD COLUMN delivered_at INTEGER NOT NULL DEFAULT 0",
)

# What the bot's profile was last set from (profile synchronisation). Telegram rate-limits a name
# change hard, so a restart must not re-apply a profile that has not moved.
_V4: Final[tuple[str, ...]] = ("ALTER TABLE bridges ADD COLUMN profile_signature TEXT",)

# The pinned presence line (the presence status). Kept per bridge so a restart edits the message
# that is already pinned instead of posting a second one.
_V5: Final[tuple[str, ...]] = (
    "ALTER TABLE bridges ADD COLUMN pin_message_id INTEGER",
    "ALTER TABLE bridges ADD COLUMN pin_text TEXT",
)

# Deterministic provisioning. `expected_username` is what the bot *must*
# be called, derived from the MAX user id and stable across rebuilds; the
# lifecycle and health columns are what a restart reads to tell a bridge that
# was finished from one that was interrupted. `history_cursor` is the newest MAX
# message already imported, so "pull only the new ones" is a real answer.
_V6: Final[tuple[str, ...]] = (
    "ALTER TABLE bridges ADD COLUMN expected_username TEXT",
    "ALTER TABLE bridges ADD COLUMN lifecycle_state TEXT",
    "ALTER TABLE bridges ADD COLUMN health TEXT",
    "ALTER TABLE bridges ADD COLUMN history_cursor INTEGER",
)

#: A message the bridge places on the owner's behalf has two Telegram ids: the
#: bot's, which arrives in the copy the bot receives, and the owner's own, which
#: comes back from the send. Both are needed and they resolve different things —
#: replies and edits arrive with the bot's, while `deleted_business_messages`
#: reports the owner's, because that update describes the *account's* chat.
_V7: Final[tuple[str, ...]] = (
    "ALTER TABLE message_map ADD COLUMN telegram_owner_message_id INTEGER",
    "CREATE INDEX IF NOT EXISTS idx_map_owner_message"
    " ON message_map(telegram_bot_id, telegram_owner_message_id)",
)

#: Durable delivery (durable delivery). Three tables and a handful of columns, all in
#: service of one rule: a message that Telegram or MAX has acknowledged to us
#: must survive the process dying one instruction later.
#:
#: `telegram_inbox` is the piece that was missing outright. An update used to be
#: acknowledged to Telegram — by moving the polling offset — before anything had
#: been written down, so a crash in that window lost it with no trace. Now the
#: row lands first and the offset moves after; `UNIQUE(bot_id, update_id)` makes
#: the replay that follows a crash a no-op instead of a double send.
#:
#: `telegram_offset` keeps the cursor across restarts. Telegram holds updates for
#: 24 hours, so a restart could otherwise re-deliver a day of traffic — harmless
#: for correctness, thanks to the unique index, and unpleasant to watch.
#:
#: `media_group_part` replaces `pending_media_group`, which was created in V1 and
#: never read or written by a single line of code. An album arrives as several
#: updates with nothing marking the last one, so the parts are now durable
#: individually and the group is reassembled after a restart.
_V8: Final[tuple[str, ...]] = (
    """
    CREATE TABLE telegram_inbox (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_id              INTEGER NOT NULL,
        update_id           INTEGER NOT NULL,
        bridge_name         TEXT,
        payload_json        TEXT    NOT NULL,
        state               TEXT    NOT NULL DEFAULT 'received',
        attempts            INTEGER NOT NULL DEFAULT 0,
        lease_expires_at    INTEGER,
        last_error          TEXT,
        created_at          INTEGER NOT NULL,
        updated_at          INTEGER NOT NULL
    )
    """,
    # The idempotency key for Telegram -> MAX. A replayed update finds this row
    # and stops there.
    "CREATE UNIQUE INDEX idx_telegram_inbox_update ON telegram_inbox (bot_id, update_id)",
    # What the intake worker scans: unfinished rows, oldest first.
    "CREATE INDEX idx_telegram_inbox_open ON telegram_inbox (state, id)",
    """
    CREATE TABLE telegram_offset (
        bot_id      INTEGER PRIMARY KEY,
        next_offset INTEGER NOT NULL,
        updated_at  INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE media_group_part (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        media_group_id      TEXT    NOT NULL,
        bridge_name         TEXT    NOT NULL,
        bot_id              INTEGER NOT NULL,
        telegram_message_id INTEGER NOT NULL,
        payload_json        TEXT    NOT NULL,
        created_at          INTEGER NOT NULL
    )
    """,
    # One part is one Telegram message; a replayed update must not add it twice.
    "CREATE UNIQUE INDEX idx_media_group_part"
    " ON media_group_part (media_group_id, telegram_message_id)",
    "CREATE INDEX idx_media_group_part_group ON media_group_part (media_group_id, id)",
    # Provably empty: nothing ever wrote to it. Dropped rather than left to
    # confuse the next reader into thinking albums were once persisted.
    "DROP TABLE pending_media_group",
    # A lease is what lets a crash be distinguished from a slow send. A worker
    # takes a job until this moment; after it passes, another worker (or the
    # same one after a restart) may take it back.
    "ALTER TABLE outbox ADD COLUMN lease_expires_at INTEGER",
    # Delivering a week-old message helps nobody. After this the job expires.
    "ALTER TABLE outbox ADD COLUMN expires_at INTEGER",
    # The source event this job exists for: a MAX message id, or a Telegram
    # inbox row. Enqueueing twice for the same source is a no-op, which is what
    # makes a replayed event safe.
    "ALTER TABLE outbox ADD COLUMN source_key TEXT",
    "CREATE UNIQUE INDEX idx_outbox_source ON outbox (source_key)"
    " WHERE source_key IS NOT NULL",
    # What the remote API gave back. A job is only DELIVERED once this is set.
    "ALTER TABLE outbox ADD COLUMN remote_message_id INTEGER",
    # Set when a send went out and the answer never arrived. The owner decides
    # what happens next — see ADR 0002.
    "ALTER TABLE outbox ADD COLUMN ambiguous_at INTEGER",
    "CREATE INDEX idx_outbox_leased ON outbox (state, lease_expires_at)",
)

#: One column, for the one crash window that cannot be guessed at (retry recovery).
#:
#: A job whose lease expired means the process died holding it. What that job
#: needs next depends on something the row did not record: whether the send had
#: already gone out. Died before it — retry, nothing was duplicated. Died after
#: it — the message may well be in the chat, and retrying puts a second copy of
#: an album in front of a real person.
#:
#: `send_started_at` is stamped immediately before the remote call, so lease
#: recovery can tell the two apart instead of assuming the harmless one.
_V9: Final[tuple[str, ...]] = (
    "ALTER TABLE outbox ADD COLUMN send_started_at INTEGER",
)

#: Health that survives the process, and alerts that survive Telegram (persistent health reporting).
#:
#: Two problems, one shape. Health kept only in RAM answers "since this process
#: started", which is the least useful window: the question after a crash is
#: what happened *before* it. And an alert that exists only as a `send_message`
#: call is lost precisely when it matters — the bridge is unhealthy, so Telegram
#: is often the thing that is failing.
#:
#: `health_state` is a handful of durable scalars, not a metrics store: when the
#: process started, when MAX last connected and disconnected, how many times it
#: has reconnected, whether the last shutdown was clean. Everything else in the
#: snapshot is counted from the tables that already hold it.
#:
#: `alert_incidents` is what stops the spam. An incident is opened once, alerted
#: once, and resolved once; a queue that is 40 jobs deep does not produce 40
#: messages, and a MAX outage does not produce one per retry.
_V10: Final[tuple[str, ...]] = (
    """
    CREATE TABLE health_state (
        key        TEXT PRIMARY KEY,
        value      TEXT    NOT NULL,
        updated_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE alert_outbox (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        incident_key    TEXT    NOT NULL,
        severity        TEXT    NOT NULL,
        text            TEXT    NOT NULL,
        state           TEXT    NOT NULL DEFAULT 'pending',
        attempts        INTEGER NOT NULL DEFAULT 0,
        next_attempt_at INTEGER NOT NULL,
        last_error      TEXT,
        created_at      INTEGER NOT NULL,
        updated_at      INTEGER NOT NULL
    )
    """,
    "CREATE INDEX idx_alert_due ON alert_outbox (state, next_attempt_at)",
    """
    CREATE TABLE alert_incidents (
        incident_key  TEXT PRIMARY KEY,
        opened_at     INTEGER NOT NULL,
        last_alert_at INTEGER NOT NULL,
        resolved_at   INTEGER
    )
    """,
)

#: Sticker reuse (sticker handling). Uploading a sticker to MAX *creates* one, with
#: `authorType: USER` — so without this table the same frog forwarded ten times
#: would leave ten stickers in the owner's own MAX collection. The key is a hash
#: of the converted PNG rather than a Telegram file id: the retry path re-fetches
#: from Telegram and re-converts, and the bytes are what has to match.
_V11: Final[tuple[str, ...]] = (
    """
    CREATE TABLE sticker_cache (
        png_sha256      TEXT PRIMARY KEY,
        max_sticker_id  INTEGER NOT NULL,
        created_at      INTEGER NOT NULL
    )
    """,
)

#: Sticker round-trip (sticker handling). A sticker the bridge carried out of MAX can go back
#: as *itself* — MSG_SEND accepts a `stickerId` we do not own, so the original
#: animated sticker is sendable if we can recognise it coming back. Telegram
#: hands the same file back byte for byte (verified in compatibility tests), so the key is a
#: hash of what we uploaded, and no id has to travel through the queue.
_V12: Final[tuple[str, ...]] = (
    """
    CREATE TABLE sticker_origin (
        tg_sha256      TEXT PRIMARY KEY,
        max_sticker_id INTEGER NOT NULL,
        created_at     INTEGER NOT NULL
    )
    """,
)

#: Owner Telegram account, beside the owner-side message id from _V7. The
#: MTProto user session (Stage 1) is a second transport into the same
#: `message_map`: its owner-originated events carry the owner-side message id
#: the account itself assigns, and `UpdateDeleteMessages` arrives with no peer —
#: only that id. The bot id cannot key it, so the account does.
#:
#: Why the account and not the message id alone: owner-side private message ids
#: are unique only *within one account*. If the owner ever re-authorises a
#: different Telegram account, its id ranges overlap the old ones, and a delete
#: for the new account could resolve against a stale mapping from the old. The
#: account disambiguates that, and makes the peer-less delete lookup exact.
#: Nullable and unset on every existing row, so old mappings stay valid and the
#: Bot API / Secretary-Mode paths that never fill it read exactly as before.
_V13: Final[tuple[str, ...]] = (
    "ALTER TABLE message_map ADD COLUMN telegram_owner_account_id INTEGER",
    "CREATE INDEX IF NOT EXISTS idx_map_owner_account"
    " ON message_map(telegram_owner_account_id, telegram_owner_message_id)",
)

#: The canonical form of what a MAX→TG message will put in the Telegram chat,
#: hashed, written in the same INSERT that claims the message — before the first
#: Telegram network call, therefore before any echo of it can exist.
#:
#: This is what makes owner-side echo binding provable rather than guessed. The
#: owner's MTProto session sees the contact bot's message with the *owner's* id
#: for it, which appears nowhere in the Bot API result, so the two sides can only
#: be joined by what was sent. Nothing durable recorded that: the job payload
#: holding the text is cleared by `mark_done`, and the two in-memory caches
#: (`OwnEchoes`, `_LAST_TEXT`) do not survive a restart. So an echo arriving after
#: a crash had nothing to match against, and matching by recency would bind the
#: wrong message whenever two look alike.
#:
#: Only a versioned hash goes in — never text, caption, filename or URL. NULL
#: means "not bindable by this increment" (an album, a sticker), which is read as
#: *skip*, not as a candidate: a row that can never be bound must not sit at the
#: head of the queue blocking the ones behind it.
#:
#: The index serves the head-of-line lookup the binding is built on — the *oldest*
#: unbound row for a contact bot — so it is ordered by `id`, the creation order
#: the table already guarantees, and not by `created_at`.
_V14: Final[tuple[str, ...]] = (
    "ALTER TABLE message_map ADD COLUMN echo_fingerprint TEXT",
    "CREATE INDEX IF NOT EXISTS idx_map_echo_unbound"
    " ON message_map (telegram_bot_id, id)"
    " WHERE direction = 'max_to_tg' AND telegram_owner_message_id IS NULL",
)

#: `media_group_part`, rebuilt so one Telegram part can be an *alias* of one
#: canonical message (Inc 4).
#:
#: V8 created that table for a narrow job: a Bot API album arrives as several
#: updates with nothing marking the last one, so each part is written down and
#: the group reassembled after the pause. It therefore knows a part only as "a
#: Telegram message id inside a media group" — everything the TG→MAX collector
#: needs, and not nearly enough for an album that has to be recognised in both
#: directions.
#:
#: What is missing is the binding. A Telegram album is N messages; the MAX
#: message it becomes is one, because MAX carries the whole group as a single
#: message with N attachments and its API has no per-attachment mutation. So a
#: reply to *any* part, a delete of *any* part and an owner echo of *any* part
#: all have to resolve to one `message_map` row — and until each part has a
#: durable identity of its own, none of them can. The nine columns added here
#: are exactly that identity and nothing else: `link_id` names the canonical row,
#: the owner columns carry the id the owner's own client speaks, and
#: `part_index` / `media_kind` / `caption_present` / `part_fingerprint` are the
#: structure a match is checked against. No bytes, no URLs, no captions — the
#: same rule the rest of the schema keeps.
#:
#: Why a rebuild rather than `ALTER TABLE ADD COLUMN`. Two things have to change
#: that SQLite cannot change in place: `telegram_message_id` must become
#: nullable, because a MAX→TG alias is written *before* the album is sent and no
#: Telegram id for it can exist yet; and the unique index over it must become
#: partial, or every unsent alias would collide with every other one on NULL —
#: which SQLite would in fact permit, and which would then let two different
#: albums share a row the moment they were settled. Create, copy, drop, rename,
#: reindex: one table before, one table under the same name after, inside the one
#: transaction every step already runs in, so a failure anywhere leaves V14's
#: table exactly as it was.
#:
#: Namespacing lives inside `media_group_id`, which is why no column for it
#: appears here. Legacy Bot API rows keep the raw Telegram group id untouched;
#: owner→MAX parts use `own:{account}:{peer}:{grouped_id}`; MAX→TG aliases use
#: `max:{link_id}`. That keeps the logical unique index from ever confusing two
#: accounts, two peers, two contact bots or two directions that happen to share a
#: `grouped_id` — Telegram's is unique per chat, not per account.
_V15: Final[tuple[str, ...]] = (
    """
    CREATE TABLE media_group_part_rebuilt (
        id                        INTEGER PRIMARY KEY AUTOINCREMENT,
        media_group_id            TEXT    NOT NULL,
        bridge_name               TEXT    NOT NULL,
        bot_id                    INTEGER NOT NULL,
        telegram_message_id       INTEGER,
        payload_json              TEXT    NOT NULL,
        created_at                INTEGER NOT NULL,
        link_id                   INTEGER,
        direction                 TEXT,
        part_index                INTEGER,
        media_kind                TEXT,
        caption_present           INTEGER,
        part_fingerprint          TEXT,
        telegram_owner_account_id INTEGER,
        telegram_owner_message_id INTEGER,
        updated_at                INTEGER
    )
    """,
    # Column for column, id included: `id` is the arrival order an unfinished
    # album is reassembled in, so a copy that renumbered would silently reorder
    # somebody's photos. The new columns are left NULL, which is what every read
    # below treats as "a legacy part, not an alias".
    """
    INSERT INTO media_group_part_rebuilt (
        id, media_group_id, bridge_name, bot_id, telegram_message_id,
        payload_json, created_at)
    SELECT id, media_group_id, bridge_name, bot_id, telegram_message_id,
           payload_json, created_at
      FROM media_group_part
    """,
    # Takes V8's indexes with it; the names are free again by the next statement.
    "DROP TABLE media_group_part",
    "ALTER TABLE media_group_part_rebuilt RENAME TO media_group_part",
    # V8's dedup key, now partial. Every legacy row has a Telegram id, so for
    # them this is the same index under the same name doing the same thing: a
    # replayed Bot API update finds its part already stored. Unsent aliases are
    # excluded rather than piled onto one NULL.
    "CREATE UNIQUE INDEX idx_media_group_part"
    " ON media_group_part (media_group_id, telegram_message_id)"
    " WHERE telegram_message_id IS NOT NULL",
    # Logical identity: part 3 of a group is one row, whichever side wrote it and
    # however many times the update behind it is replayed. Namespaced by
    # `media_group_id`, so the same index cannot conflate two albums.
    "CREATE UNIQUE INDEX idx_media_group_part_logical"
    " ON media_group_part (media_group_id, part_index)"
    " WHERE part_index IS NOT NULL",
    # An owner-side message belongs to one part, and unique is how that stops
    # being a convention. Without it a mis-ordered echo could bind the same owner
    # id onto two aliases and both would look bound; with it the second write
    # fails loudly and reaches the owner as an incident instead of a silent
    # double mapping. Keyed by account because owner-side private message ids are
    # only unique within one account (the same reason `_V13` gave).
    "CREATE UNIQUE INDEX idx_media_group_part_owner"
    " ON media_group_part (telegram_owner_account_id, telegram_owner_message_id)"
    " WHERE telegram_owner_message_id IS NOT NULL",
    "CREATE INDEX idx_media_group_part_group ON media_group_part (media_group_id, id)",
    # Every part of one canonical message, for the collapse a delete performs.
    "CREATE INDEX idx_media_group_part_link ON media_group_part (link_id)"
    " WHERE link_id IS NOT NULL",
    # The bot-side lookup a Bot API reply resolves through.
    "CREATE INDEX idx_media_group_part_bot"
    " ON media_group_part (bot_id, telegram_message_id)"
    " WHERE telegram_message_id IS NOT NULL",
)

#: Who actually wrote a forwarded message, as a *bot* was told (W-forward).
#:
#: The owner's own session cannot answer this. Telegram hands a saved contact
#: over under the name the owner filed them as, and there is no API that gives
#: their own back — the header on a forward is drawn by each client from the
#: peer, so the server never has to send a name at all. A bot has no address
#: book, so what it is told is the profile itself.
#:
#: Which makes this a cache with one job: carry a name from the transport that
#: can see it to the transport that cannot. Persisted rather than kept in
#: memory, so a restart does not go back to guessing — the owner's private label
#: for somebody must never be the thing that reaches a contact.
_V16: Final[tuple[str, ...]] = (
    """
    CREATE TABLE forward_author (
        peer_id     INTEGER PRIMARY KEY,
        name        TEXT NOT NULL,
        username    TEXT,
        updated_at  INTEGER NOT NULL
    )
    """,
)

#: What the owner's puppet session last saw of one of their own messages.
#:
#: `UpdateEditMessage` is the single constructor Telegram uses for a text edit,
#: for a reaction, and for both at once — `edit_date` is set either way and there
#: is no flag between them. The update
#: says what the message *is* now, never what changed, so the only way to know
#: what the owner did is to hold what it was and subtract.
#:
#: Keyed by the update's own identity: the account whose numbering the id belongs
#: to, the peer the update names, and the owner-side message id. The peer is in
#: the key even though private-chat ids are unique per account today — it is what
#: the update actually carries, and a key that mirrors the event cannot be wrong
#: about which dialog a row belongs to.
#:
#: Content is never stored, only a fingerprint of it — the markdown body, by the
#: same hash the edit source_key uses. The body alone, because that is what an
#: edit actually carries: the forward line is drawn from the original author and
#: never moves, and media is not carried by an edit at all.
_V17: Final[tuple[str, ...]] = (
    """
    CREATE TABLE owner_message_state (
        telegram_owner_account_id INTEGER NOT NULL,
        telegram_bot_id           INTEGER NOT NULL,
        telegram_owner_message_id INTEGER NOT NULL,

        content_fingerprint       TEXT    NOT NULL,
        chosen_json               TEXT    NOT NULL,
        pts                       INTEGER NOT NULL,
        updated_at                INTEGER NOT NULL,

        PRIMARY KEY (
            telegram_owner_account_id,
            telegram_bot_id,
            telegram_owner_message_id
        )
    ) WITHOUT ROWID
    """,
)

#: Every owner update, written down before anything is derived from it.
#:
#: `owner_message_state` remembers what a message *was*; this remembers what
#: Telegram *said*, so a process that dies between the update and the effect can
#: pick it up again. Telethon does not replay: `pts` advances before the handler
#: runs, a raising handler is logged rather than retried, and `catch_up` is off.
#: Without this table, owner updates received during a crash window can be lost.
#:
#: The guarantee this buys is exact and worth stating narrowly. **After this
#: INSERT commits, the event survives.** Before it commits there is still a
#: window in which Telegram has delivered an update and nothing has written it
#: down, and this table does not close that window.
#:
#: One `UpdateEditMessage` is one row. It is a snapshot with two possible
#: derived diffs — content and reactions — not two events, because reading it as
#: two would let one be accounted while the other was not.
#:
#: The CHECKs are the invariants the application already keeps, written where
#: they cannot be forgotten. Two of them are deliberately narrower than they
#: could be:
#:
#: * the content one is relaxed for `accounted`, because the text is the one
#:   piece of message content this bridge stores at rest and scrubbing it from a
#:   row whose work is done must not be blocked by a constraint about rows whose
#:   work is not;
#: * `content_text` is *not* required. It is present for a snapshot of the
#:   owner's own message, which is the only kind an edit is ever carried for,
#:   and null for a contact's — whose reactions are the owner's but whose words
#:   are not, and storing them at rest would be an expansion this table does not
#:   need. The fingerprint and the reaction set are required either way.
_V18: Final[tuple[str, ...]] = (
    """
    CREATE TABLE owner_update_inbox (
        telegram_owner_account_id INTEGER NOT NULL,
        telegram_bot_id           INTEGER NOT NULL,
        telegram_owner_message_id INTEGER NOT NULL,
        family                    TEXT    NOT NULL,
        pts                       INTEGER NOT NULL,

        content_text              TEXT,
        content_fingerprint       TEXT,
        chosen_json               TEXT,

        state                     TEXT    NOT NULL DEFAULT 'open',
        attempts                  INTEGER NOT NULL DEFAULT 0,
        next_attempt_at           INTEGER NOT NULL,
        lease_expires_at          INTEGER,
        last_error                TEXT,
        created_at                INTEGER NOT NULL,
        updated_at                INTEGER NOT NULL,
        accounted_at              INTEGER,

        PRIMARY KEY (
            telegram_owner_account_id,
            telegram_bot_id,
            telegram_owner_message_id,
            family,
            pts
        ),

        CHECK (family IN ('message_snapshot', 'delete')),
        CHECK (state IN ('open', 'claimed', 'accounted', 'dead')),
        CHECK (pts >= 0),
        CHECK (attempts >= 0),
        CHECK (state <> 'accounted' OR accounted_at IS NOT NULL),
        CHECK (state <> 'claimed' OR lease_expires_at IS NOT NULL),
        CHECK (
            family <> 'delete'
            OR (content_text IS NULL AND chosen_json IS NULL)
        ),
        CHECK (
            family <> 'message_snapshot'
            OR state = 'accounted'
            OR (content_fingerprint IS NOT NULL AND chosen_json IS NOT NULL)
        )
    ) WITHOUT ROWID
    """,
    """
    CREATE INDEX owner_update_inbox_due
        ON owner_update_inbox (state, next_attempt_at)
    """,
)

#: One bot serves one bridge, and one deterministic username belongs to one
#: bridge. Both were true by construction and neither was enforced: two rows
#: could carry the same `telegram_bot_id` and the same `expected_username`, and
#: `by_expected_username` would answer with whichever SQLite reached first.
#: The mismatch surfaced only at the next start, as a bridge that silently did
#: not come up.
#:
#: Partial, in both cases, because the absences are legitimate. A row with no
#: `telegram_bot_id` is one whose bot has never been proved to exist — several
#: may be waiting at once and none of them names anything. An empty username is
#: the same statement in a column that predates `NULL` discipline.
#:
#: Folded, because Telegram is case-insensitive about usernames and hands them
#: back in whatever case the owner typed, while the derived name is lowercase.
_V19: Final[tuple[str, ...]] = (
    "CREATE UNIQUE INDEX idx_bridges_bot_id"
    " ON bridges (telegram_bot_id) WHERE telegram_bot_id IS NOT NULL",
    "CREATE UNIQUE INDEX idx_bridges_expected_username"
    " ON bridges (lower(expected_username))"
    " WHERE expected_username IS NOT NULL AND expected_username <> ''",
)

#: A watermark under the automatic backfill. `history_cursor` says what has been
#: *imported*; this says what may never be delivered at all.
#:
#: The two are not the same question and collapsing them would break both. The
#: dedup that makes the reconnect backfill free is
#: `(max_chat_id, max_message_id, telegram_bot_id)` — it is keyed on the *bot*.
#: Give a contact a different bot and every message in the chat's tail is
#: unclaimed again, so the tail is delivered a second time into the new chat.
#: That is exactly what a V2 cutover does, and no amount of keeping the old rows
#: would have prevented it.
_V20: Final[tuple[str, ...]] = (
    "ALTER TABLE bridges ADD COLUMN history_floor INTEGER",
)

MIGRATIONS: Final[tuple[Step, ...]] = (
    (1, _V1),
    (2, _V2),
    (3, _V3),
    (4, _V4),
    (5, _V5),
    (6, _V6),
    (7, _V7),
    (8, _V8),
    (9, _V9),
    (10, _V10),
    (11, _V11),
    (12, _V12),
    (13, _V13),
    (14, _V14),
    (15, _V15),
    (16, _V16),
    (17, _V17),
    (18, _V18),
    (19, _V19),
    (20, _V20),
)

LATEST_VERSION: Final[int] = MIGRATIONS[-1][0]
