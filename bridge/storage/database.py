"""The SQLite connection: pragmas, migrations, and the single write lock.

One connection for the whole process. SQLite handles concurrent readers fine,
but concurrent writers from several asyncio tasks turn into `database is
locked` under load, so every write goes through one `asyncio.Lock`. The bridge
writes a few rows per message — this is nowhere near a bottleneck, and it makes
"did this run inside a transaction" answerable by reading the code.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Self

import aiosqlite

from .migrations import LATEST_VERSION, MIGRATIONS

# SQLite waits this long for a competing writer before giving up. The lock below
# means we should never hit it; it exists for the case of a second process
# (a CLI command run while the service is up).
BUSY_TIMEOUT_MS = 5_000

#: The health row the write canary owns. Its own key, so proving the write path
#: works never touches a row anything else reads.
CANARY_KEY = "database-write-canary"


class DatabaseUnavailableError(RuntimeError):
    """The connection cannot be trusted, and callers must not pretend it can.

    Raised while a transaction could not be closed — committed *or* rolled back
    — because everything after that point would go into a transaction nothing
    will ever finish. Fail closed: a caller that gets this has not written
    anything and must not act as though it had.
    """


async def _open(path: Path) -> aiosqlite.Connection:
    """One connection, configured. The only place a connection is created.

    `isolation_level=None` turns off the driver's legacy transaction guessing,
    where it opens a transaction before INSERT/UPDATE/DELETE and leaves DDL in
    autocommit. That default silently broke migrations: a CREATE TABLE committed
    itself, so a failure later in the same step could not be rolled back.
    Everything is explicit instead.

    WAL lets readers work while a write is in flight; NORMAL trades a fsync per
    commit for the risk of losing the last transaction on a power cut, which for
    a chat bridge is the right trade.
    """
    connection = await aiosqlite.connect(path, isolation_level=None)
    connection.row_factory = aiosqlite.Row
    await connection.execute("PRAGMA journal_mode=WAL")
    await connection.execute("PRAGMA synchronous=NORMAL")
    await connection.execute("PRAGMA foreign_keys=ON")
    await connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return connection


def now_ms() -> int:
    """Unix time in milliseconds, UTC.

    Milliseconds because MAX's read marks are milliseconds, and mixing units is
    exactly how the `duration` bug in the media pipeline happened.
    """
    return int(time.time() * 1000)


class Database:
    """Owns the connection. Repositories borrow it, nobody else opens one."""

    def __init__(self, connection: aiosqlite.Connection, path: Path) -> None:
        self._connection = connection
        self._path = path
        self._lock = asyncio.Lock()
        # Set when a transaction could not be closed either way. Every entry
        # point refuses while it is set, so the process stops writing into a
        # transaction that will never commit instead of doing it silently for
        # an hour — which is what happened.
        self._poisoned: str | None = None
        # Single-flight: ten callers noticing at once must not open ten
        # connections.
        self._recovery = asyncio.Lock()
        self._last_write_ok_ms = 0
        self.write_failures = 0

    @property
    def poisoned(self) -> str | None:
        """Why the connection is not usable, or None."""
        return self._poisoned

    @property
    def last_write_ok_ms(self) -> int:
        return self._last_write_ok_ms

    def _require_usable(self) -> None:
        if self._poisoned is not None:
            raise DatabaseUnavailableError(self._poisoned)

    async def _close_transaction(self, *, committing: bool) -> None:
        """End the open transaction, or declare the connection unusable.

        The commit is inside the `try` on purpose. It was outside, and a commit
        that failed — `cannot commit transaction - SQL statements in progress` —
        propagated with the transaction still open. Nothing rolled it back,
        every later `BEGIN IMMEDIATE` failed, and the bridge went on "running"
        for the best part of an hour writing into it.
        """
        try:
            await self._connection.execute("COMMIT" if committing else "ROLLBACK")
        except Exception as error:
            if committing:
                # The commit failed; the transaction is still open. One attempt
                # to undo it, and if that does not work the connection is done.
                try:
                    await self._connection.execute("ROLLBACK")
                except Exception as rollback_error:
                    self._poison(f"rollback after a failed commit: {type(rollback_error).__name__}")
                    raise DatabaseUnavailableError(self._poisoned or "") from rollback_error
                raise
            self._poison(f"rollback: {type(error).__name__}")
            raise DatabaseUnavailableError(self._poisoned or "") from error

        if self._connection.in_transaction:
            # Neither statement raised and a transaction is still open. Nothing
            # here can be trusted to have landed.
            self._poison("a transaction stayed open after it was closed")
            raise DatabaseUnavailableError(self._poisoned or "")

    def _poison(self, reason: str) -> None:
        self._poisoned = reason
        self.write_failures += 1

    async def recover(self) -> bool:
        """Put a usable connection back, or stay refusing. Single-flight.

        Not a reconnect loop: it runs when something asks — the health canary —
        and either finishes with a connection that has passed a real write or
        leaves the refusal in place.
        """
        async with self._recovery:
            if self._poisoned is None:
                return True
            async with self._lock:
                with contextlib.suppress(Exception):
                    await self._connection.execute("ROLLBACK")
                if not self._connection.in_transaction:
                    self._poisoned = None
                    return True
                with contextlib.suppress(Exception):
                    await self._connection.close()
                try:
                    self._connection = await _open(self._path)
                except Exception as error:  # noqa: BLE001 - any failure means "still refusing"
                    self._poison(f"reopen: {type(error).__name__}")
                    return False
            self._poisoned = None
            return True

    @property
    def path(self) -> Path:
        return self._path

    @classmethod
    async def connect(cls, path: Path) -> Self:
        path.parent.mkdir(parents=True, exist_ok=True)
        database = cls(await _open(path), path)
        await database.migrate()
        return database

    async def close(self) -> None:
        await self._connection.close()

    # ---------------------------------------------------------------- migrations

    async def schema_version(self) -> int:
        async with self._lock:
            return await self._schema_version()

    async def _schema_version(self) -> int:
        await self._connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            " version INTEGER NOT NULL,"
            " applied_at INTEGER NOT NULL)"
        )
        async with self._connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM schema_version"
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["version"]) if row else 0

    async def migrate(self) -> int:
        """Apply every step newer than the recorded version. Idempotent.

        One step is one transaction: `BEGIN IMMEDIATE`, every statement of the
        step, the `schema_version` row, `COMMIT`. Anything raising takes a
        `ROLLBACK` with it, so a step is either fully applied and recorded or
        not applied at all.

        This has to be spelled out because SQLite's Python driver will not do
        it. In its default mode DDL runs outside any transaction, which meant a
        step that died on its fifth statement left four tables behind with
        `schema_version` untouched — and the next start re-ran the step, hit
        "table already exists", and the process never came up again.
        """
        current = await self.schema_version()
        if current >= LATEST_VERSION:
            return current

        async with self._lock:
            for version, statements in MIGRATIONS:
                if version <= current:
                    continue
                await self._connection.execute("BEGIN IMMEDIATE")
                try:
                    for statement in statements:
                        await self._connection.execute(statement)
                    await self._connection.execute(
                        "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                        (version, now_ms()),
                    )
                except BaseException:
                    await self._close_transaction(committing=False)
                    raise
                await self._close_transaction(committing=True)
                current = version
        return current

    # ------------------------------------------------------------------- access

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        self._require_usable()
        # Under the same lock as every other statement. One connection is shared
        # by every repository, and a cursor still open when `transaction()`
        # commits takes the whole process down with it:
        #
        #     cannot commit transaction - SQL statements in progress
        #     cannot start a transaction within a transaction   (for ever after)
        #
        # The transaction is left open, every later `BEGIN IMMEDIATE` fails, and
        # writes go into a transaction nothing will ever commit. Measured in
        # production: the bridge stopped carrying messages and said nothing
        # except a stack trace per poll.
        async with self._lock, self._connection.execute(sql, params) as cursor:
            return list(await cursor.fetchall())

    async def query_one(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        self._require_usable()
        async with self._lock, self._connection.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run one write statement. Returns `lastrowid`.

        The connection is in autocommit, so a single statement commits itself.
        The cursor is closed rather than dropped on the floor: a statement left
        unfinished is what a later COMMIT trips over.
        """
        self._require_usable()
        async with self._lock, self._connection.execute(sql, params) as cursor:
            self._last_write_ok_ms = now_ms()
            return int(cursor.lastrowid or 0)

    async def execute_changed(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run one write statement. Returns how many rows it changed.

        `execute` answers with `lastrowid`, which says nothing about an UPDATE.
        A conditional update — "spend this nonce, but only if it is still the
        current one" — needs the count, and needs it from the same statement
        rather than from a read before it.
        """
        self._require_usable()
        async with self._lock, self._connection.execute(sql, params) as cursor:
            self._last_write_ok_ms = now_ms()
            return int(cursor.rowcount or 0)

    async def execute_many(self, sql: str, params: Iterable[Sequence[Any]]) -> None:
        self._require_usable()
        async with self._lock:
            await self._connection.execute("BEGIN IMMEDIATE")
            try:
                await self._connection.executemany(sql, params)
            except BaseException:
                await self._close_transaction(committing=False)
                raise
            await self._close_transaction(committing=True)
            self._last_write_ok_ms = now_ms()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Several writes that must land together, or not at all.

        `BEGIN IMMEDIATE` takes the write lock up front rather than on the first
        write: a read-then-write block (claim a job, mark it leased) must not be
        able to see a value that another writer changes underneath it.
        """
        self._require_usable()
        async with self._lock:
            await self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                await self._close_transaction(committing=False)
                raise
            await self._close_transaction(committing=True)
            self._last_write_ok_ms = now_ms()

    async def write_canary(self) -> bool:
        """Prove the write path works, end to end, through the ordinary wrapper.

        `SELECT 1` proves nothing that matters: the failure this exists for is a
        transaction that cannot be *closed*, and only a real
        BEGIN/write/read-back/COMMIT touches that. It writes to its own row in
        `health_state` rather than to anything a person would notice, and the
        read-back is inside the transaction so a write that silently did not
        land is caught rather than assumed.
        """
        if self._poisoned is not None and not await self.recover():
            return False
        stamp = now_ms()
        try:
            async with self.transaction() as connection:
                await connection.execute(
                    "INSERT INTO health_state (key, value, updated_at) VALUES (?, ?, ?)"
                    " ON CONFLICT (key) DO UPDATE SET"
                    "   value = excluded.value, updated_at = excluded.updated_at",
                    (CANARY_KEY, str(stamp), stamp),
                )
                async with connection.execute(
                    "SELECT updated_at FROM health_state WHERE key = ?", (CANARY_KEY,)
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None or int(row["updated_at"]) != stamp:
                    raise DatabaseUnavailableError("the canary write did not read back")
        except Exception:  # noqa: BLE001 - a canary reports, it never raises
            self.write_failures += 1
            return False
        return True
