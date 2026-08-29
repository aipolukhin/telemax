"""Removing one bridge entirely: its conversation, its bot, and its rows.

Disconnecting is reversible and keeps everything. This is the other one — the
owner is done with a contact, and wants the slot back.

**The order is not a preference. It is forced by capability decay.**

    stop the worker      nothing may land while the chat is being emptied
    wipe the dialog      needs the owner's session
    delete the bot       needs the owner's session
    purge local rows     needs nothing

Every step above depends on something the step below destroys. Delete the bot
first and there is no peer left to resolve, so the conversation stays for ever.
Purge the rows first and the local record of a remote resource is gone before
the resource is — which is exactly the mistake that cost this install eighty
eight minutes of delivery: local destructive cleanup ran before any of the
remote work it described had happened.

So the rule that shapes the whole module: **local state is dropped last, and
only after the remote effect it describes has been confirmed.** A failure at any
remote step stops the walk with the rows intact, and a retry finishes the job.

What is *not* touched, ever: the conversation in MAX. Only the Telegram side of
a bridge is this module's business.

The table list is not written down here. It is derived by the same column scan
the cutover uses — anything carrying `bridge_name`, `telegram_bot_id`, `bot_id`
or a `max_chat_id` — because a hand-written list is a list with one table
missing, and the missing one leaves an orphan.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from bridge.cutover.inventory import _BRIDGE_COLUMNS, KEEP

logger = logging.getLogger(__name__)


class Step(StrEnum):
    """How far a teardown got. Written before each step, never after.

    A crash between two irreversible remote effects must name its own state
    rather than leave a person guessing which of them happened.
    """

    ARMED = "armed"
    WORKER_STOPPED = "worker_stopped"
    DIALOG_WIPED = "dialog_wiped"
    BOT_DELETED = "bot_deleted"
    PURGED = "purged"
    DONE = "done"


#: Where a teardown in flight is remembered, one file per bridge.
def checkpoint_path(data_dir: Path, bridge_name: str) -> Path:
    return data_dir / "state" / f"teardown-{bridge_name}.step"


def record(data_dir: Path, bridge_name: str, step: Step) -> None:
    """One line on disk. There is no resume: this is for a person deciding."""
    from bridge.config.writer import atomic_write_text

    atomic_write_text(checkpoint_path(data_dir, bridge_name), f"{step.value}\n")


def forget(data_dir: Path, bridge_name: str) -> None:
    checkpoint_path(data_dir, bridge_name).unlink(missing_ok=True)


def read_step(data_dir: Path, bridge_name: str) -> Step | None:
    try:
        text = checkpoint_path(data_dir, bridge_name).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return Step(text)
    except ValueError:
        return None


@dataclass(slots=True)
class TornDown:
    """What actually happened, for the screen and for the log."""

    bridge_name: str
    username: str
    bot_id: int | None = None
    dialog_wiped: bool = False
    bot_deleted: bool = False
    rows: dict[str, int] = field(default_factory=dict)
    token_variable: str | None = None
    floor_dropped: bool = False
    stopped_at: Step = Step.ARMED

    @property
    def complete(self) -> bool:
        return self.stopped_at is Step.DONE

    @property
    def row_count(self) -> int:
        return sum(self.rows.values())


async def scoped_tables(database: Any) -> list[tuple[str, tuple[str, ...]]]:
    """Every table that carries a bridge identity, and which columns carry it.

    Derived, never listed. `KEEP` is skipped for the same reason the cutover
    skips it: those tables are about the account or the process, and a contact
    going away does not make a sticker cache wrong.
    """
    names = [
        str(row["name"])
        for row in await database.query(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    found: list[tuple[str, tuple[str, ...]]] = []
    for name in names:
        if name in KEEP:
            continue
        columns = {
            str(row["name"]) for row in await database.query(f"PRAGMA table_info({name})")
        }
        carried = tuple(column for column in _BRIDGE_COLUMNS if column in columns)
        if carried:
            found.append((name, carried))
    return found


async def purge_rows(
    *,
    database: Any,
    bridge_name: str,
    telegram_bot_id: int | None,
    max_chat_id: int | None,
) -> dict[str, int]:
    """Delete this bridge's rows everywhere they are, in one transaction.

    One transaction because a half-purged bridge is worse than either end of
    it: rows referring to a bot that is gone, in tables nothing will look at
    again.
    """
    identities: dict[str, Any] = {"bridge_name": bridge_name}
    if telegram_bot_id is not None:
        identities["telegram_bot_id"] = telegram_bot_id
        identities["bot_id"] = telegram_bot_id
    if max_chat_id is not None:
        identities["max_chat_id"] = max_chat_id

    # Scanned *before* the transaction is opened, and that is not tidiness: the
    # scan queries the same database, and asking it for a schema while holding
    # a write transaction on it deadlocks — the whole teardown hangs with the
    # bridge already stopped.
    tables = await scoped_tables(database)

    removed: dict[str, int] = {}
    async with database.transaction() as connection:
        for table, carried in tables:
            clauses = [f"{column} = ?" for column in carried if column in identities]
            values = [identities[column] for column in carried if column in identities]
            if not clauses:
                continue
            # Table and column names come from `sqlite_master` and a fixed
            # tuple; every value is bound. There is no user input in the text.
            statement = f"DELETE FROM {table} WHERE {' OR '.join(clauses)}"  # noqa: S608
            cursor = await connection.execute(statement, values)
            if cursor.rowcount:
                removed[table] = int(cursor.rowcount)
    return removed


def drop_floor(data_dir: Path, max_chat_id: int) -> bool:
    """Forget this chat's history floor. The bridge it guarded is gone."""
    from bridge.cutover.floors import read_floors, write_floors

    floors = read_floors(data_dir)
    if max_chat_id not in floors:
        return False
    del floors[max_chat_id]
    write_floors(data_dir, floors)
    return True
