"""A failed transaction must not poison the connection for the rest of the run.

It did. On 2026-08-05 a commit failed with `cannot commit transaction - SQL
statements in progress`, the commit was outside the `try` so nothing rolled it
back, and from then on every `BEGIN IMMEDIATE` raised `cannot start a
transaction within a transaction`. Writes went into a transaction nothing would
ever finish, the process stayed `active`, and the bridge carried nothing for the
best part of an hour while logging a stack trace per poll. Seven owner updates
were lost.

Every failure below ends with the same four questions: is the transaction
closed, is the connection usable, does the next write land, and did the caller
find out. Nothing here runs against production.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from bridge.storage import Database
from bridge.storage.database import DatabaseUnavailableError


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    await db.execute("CREATE TABLE probe (x INTEGER)")
    try:
        yield db
    finally:
        await db.close()


async def _still_works(db: Database) -> None:
    """The four questions, asked the same way every time."""
    assert db.poisoned is None, db.poisoned
    assert not db._connection.in_transaction
    assert await db.query("SELECT 1 AS one")
    before = (await db.query_one("SELECT count(*) AS n FROM probe WHERE x = -1"))["n"]  # type: ignore[index]
    async with db.transaction() as connection:
        await connection.execute("INSERT INTO probe (x) VALUES (-1)")
    after = (await db.query_one("SELECT count(*) AS n FROM probe WHERE x = -1"))["n"]  # type: ignore[index]
    assert after == before + 1


# ------------------------------------------------------------- the real failure


async def test_an_unfinished_read_cannot_break_a_commit(database: Database) -> None:
    """The shape of the outage: a cursor left open while a transaction commits.

    It cannot happen now because every statement is under one lock, and this
    holds the reads to that — a read that returned a live cursor would put the
    window straight back.
    """
    await database.execute("INSERT INTO probe (x) VALUES (1)")
    await database.execute("INSERT INTO probe (x) VALUES (2)")

    async def read() -> None:
        for _ in range(50):
            await database.query("SELECT x FROM probe")

    async def write() -> None:
        for value in range(50):
            async with database.transaction() as connection:
                await connection.execute("INSERT INTO probe (x) VALUES (?)", (value,))

    await asyncio.gather(read(), write(), read(), write())
    await _still_works(database)


async def test_a_failing_commit_leaves_no_transaction_open(database: Database) -> None:
    """The exact defect. The commit used to be outside the `try`, so a commit
    that raised propagated with the transaction still open and nothing ever
    closed it."""
    original = database._connection.execute

    async def refuse_commit(sql: str, *args: Any, **kwargs: Any) -> Any:
        if sql == "COMMIT":
            raise __import__("sqlite3").OperationalError(
                "cannot commit transaction - SQL statements in progress"
            )
        return await original(sql, *args, **kwargs)

    database._connection.execute = refuse_commit  # type: ignore[method-assign]
    with pytest.raises(Exception, match="cannot commit"):
        async with database.transaction() as connection:
            await connection.execute("INSERT INTO probe (x) VALUES (7)")
    database._connection.execute = original  # type: ignore[method-assign]

    await _still_works(database)
    assert await database.query("SELECT x FROM probe WHERE x = 7") == []


async def test_a_transaction_that_cannot_be_closed_at_all_refuses_callers(
    database: Database,
) -> None:
    """When neither the commit nor the rollback works there is nothing honest
    left to do but stop. A caller that got a silent success here would report a
    message as delivered that was never written."""
    import sqlite3

    original = database._connection.execute

    async def refuse_both(sql: str, *args: Any, **kwargs: Any) -> Any:
        if sql in ("COMMIT", "ROLLBACK"):
            raise sqlite3.OperationalError("disk I/O error")
        return await original(sql, *args, **kwargs)

    database._connection.execute = refuse_both  # type: ignore[method-assign]
    with pytest.raises(DatabaseUnavailableError):
        async with database.transaction() as connection:
            await connection.execute("INSERT INTO probe (x) VALUES (8)")

    assert database.poisoned is not None
    with pytest.raises(DatabaseUnavailableError):
        await database.execute("INSERT INTO probe (x) VALUES (9)")
    with pytest.raises(DatabaseUnavailableError):
        await database.query("SELECT 1")

    database._connection.execute = original  # type: ignore[method-assign]
    assert await database.recover()
    await _still_works(database)


async def test_the_reason_carries_no_sql_and_no_content(database: Database) -> None:
    import sqlite3

    original = database._connection.execute

    async def refuse_both(sql: str, *args: Any, **kwargs: Any) -> Any:
        if sql in ("COMMIT", "ROLLBACK"):
            raise sqlite3.OperationalError('near "привет": syntax error')
        return await original(sql, *args, **kwargs)

    database._connection.execute = refuse_both  # type: ignore[method-assign]
    with pytest.raises(DatabaseUnavailableError):
        async with database.transaction() as connection:
            await connection.execute("INSERT INTO probe (x) VALUES (10)")

    assert database.poisoned is not None
    assert "привет" not in database.poisoned
    assert "INSERT" not in database.poisoned
    database._connection.execute = original  # type: ignore[method-assign]
    await database.recover()


# --------------------------------------------------------------- ordinary faults


async def test_a_body_that_raises_rolls_back(database: Database) -> None:
    with pytest.raises(ValueError):
        async with database.transaction() as connection:
            await connection.execute("INSERT INTO probe (x) VALUES (11)")
            raise ValueError("no")

    await _still_works(database)
    assert await database.query("SELECT x FROM probe WHERE x = 11") == []


async def test_a_body_that_raises_before_writing_anything_rolls_back(
    database: Database,
) -> None:
    with pytest.raises(ValueError):
        async with database.transaction():
            raise ValueError("no")
    await _still_works(database)


async def test_several_writes_are_all_undone(database: Database) -> None:
    with pytest.raises(ValueError):
        async with database.transaction() as connection:
            for value in (21, 22, 23):
                await connection.execute("INSERT INTO probe (x) VALUES (?)", (value,))
            raise ValueError("no")

    rows = await database.query("SELECT x FROM probe WHERE x IN (21, 22, 23)")
    assert rows == []
    await _still_works(database)


async def test_cancellation_inside_a_transaction_rolls_back(database: Database) -> None:
    """Cancellation is how a shutdown reaches a handler. A transaction left open
    by one would take the next process start down with it."""
    started = asyncio.Event()

    async def held() -> None:
        async with database.transaction() as connection:
            await connection.execute("INSERT INTO probe (x) VALUES (31)")
            started.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(held())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await _still_works(database)
    assert await database.query("SELECT x FROM probe WHERE x = 31") == []


async def test_execute_many_rolls_back_as_a_whole(database: Database) -> None:
    with pytest.raises(Exception, match=r"no such table|syntax"):
        await database.execute_many("INSERT INTO missing (x) VALUES (?)", [(1,), (2,)])
    await _still_works(database)


# ------------------------------------------------------------- under load


async def test_a_hundred_readers_beside_the_writers(database: Database) -> None:
    async def reader() -> None:
        await database.query("SELECT count(*) AS n FROM probe")

    async def claimer(value: int) -> None:
        async with database.transaction() as connection:
            await connection.execute("INSERT INTO probe (x) VALUES (?)", (value,))
            async with connection.execute("SELECT count(*) AS n FROM probe") as cursor:
                await cursor.fetchone()

    await asyncio.gather(
        *(reader() for _ in range(100)),
        *(claimer(value) for value in range(100)),
    )
    await _still_works(database)


async def test_a_retry_storm_beside_claims_does_not_poison_anything(
    database: Database,
) -> None:
    """What actually happened: reaction jobs failing on a missing method and
    retrying hard enough to overlap a claim."""

    async def failing_attempt() -> None:
        for _ in range(20):
            with __import__("contextlib").suppress(ValueError):
                async with database.transaction() as connection:
                    await connection.execute("INSERT INTO probe (x) VALUES (41)")
                    raise ValueError("AttributeError, in effect")

    async def claim() -> None:
        for value in range(20):
            async with database.transaction() as connection:
                await connection.execute("INSERT INTO probe (x) VALUES (?)", (value,))

    await asyncio.gather(failing_attempt(), claim(), failing_attempt(), claim())
    await _still_works(database)
    assert await database.query("SELECT x FROM probe WHERE x = 41") == []


# ------------------------------------------------------------------ the canary


async def test_the_canary_proves_the_write_path(database: Database) -> None:
    """`SELECT 1` would have passed throughout the outage. This does a real
    BEGIN, a write, a read-back inside the transaction, and a COMMIT."""
    assert await database.write_canary()
    assert database.last_write_ok_ms > 0


async def test_the_canary_fails_when_the_write_path_is_broken(
    database: Database,
) -> None:
    import sqlite3

    original = database._connection.execute

    async def refuse(sql: str, *args: Any, **kwargs: Any) -> Any:
        if sql.startswith("BEGIN"):
            raise sqlite3.OperationalError("database is locked")
        return await original(sql, *args, **kwargs)

    database._connection.execute = refuse  # type: ignore[method-assign]
    assert await database.write_canary() is False
    assert database.write_failures > 0

    database._connection.execute = original  # type: ignore[method-assign]
    assert await database.write_canary() is True


async def test_recovery_is_single_flight(database: Database) -> None:
    """Ten callers noticing at once must not open ten connections."""
    database._poison("test")
    results = await asyncio.gather(*(database.recover() for _ in range(10)))
    assert all(results)
    await _still_works(database)


async def test_the_canary_touches_only_its_own_row(database: Database) -> None:
    await database.write_canary()
    rows = await database.query("SELECT key FROM health_state")
    assert [row["key"] for row in rows] == ["database-write-canary"]
