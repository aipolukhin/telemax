"""Removing every trace of the old bots, in one transaction, and nothing else.

The tables are not listed here. They come from `inventory.classify_tables`,
which decides by looking at the columns — so a table added next year is
classified rather than forgotten, and the set the owner approved at the gate is
the set that is emptied.

Two things this deliberately does not touch, and both would be silent damage:

* **the account's own state** — the MAX session and device identity, both
  Telegram sessions, the sticker caches. A sticker sent to MAX creates one in
  the owner's collection, and `sticker_cache` is how the same picture resolves
  to the same id; wiping it litters the account with duplicates on the next
  sticker. It belongs to the account, not to any bot.
* **remote bots.** There is no `deleteManagedBot`. Pretending otherwise would be
  the one lie this project has consistently refused to tell, so the old bots are
  reported as RETIRED and the owner removes them by hand if they want to.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from bridge.provisioning.secrets import ContactBotSecretStore
from bridge.storage.database import Database

from .inventory import TableScope

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Purged:
    """What was actually removed."""

    tables: dict[str, int] = field(default_factory=dict)
    token_variables: list[str] = field(default_factory=list)
    journal: bool = False
    intent: bool = False

    @property
    def rows(self) -> int:
        return sum(self.tables.values())

    def report(self) -> str:
        lines = [f"  строк удалено: {self.rows}"]
        lines += [f"    {name:24s} {count}" for name, count in sorted(self.tables.items()) if count]
        if self.token_variables:
            lines.append(f"  токенов забыто: {len(self.token_variables)}")
            lines += [f"    ${name}" for name in self.token_variables]
        if self.journal:
            lines.append("  журнал провижининга очищен")
        if self.intent:
            lines.append("  intent стража очищен")
        return "\n".join(lines)


async def clear_database(database: Database, scoped: list[TableScope]) -> dict[str, int]:
    """Empty every bridge-scoped table, in one transaction.

    One transaction because half a cutover is the worst of both: bridges whose
    mappings are gone and mappings whose bridges are not.
    """
    emptied: dict[str, int] = {}
    async with database.transaction() as connection:
        for table in scoped:
            if not table.scoped:
                continue
            # Names come from the schema, never from input, and a table name
            # cannot be bound as a parameter.
            async with connection.execute(
                f"SELECT COUNT(*) AS n FROM {table.name}"  # noqa: S608
            ) as cursor:
                row = await cursor.fetchone()
            count = int(row["n"]) if row else 0
            await connection.execute(f"DELETE FROM {table.name}")  # noqa: S608 - name from schema
            emptied[table.name] = count
    return emptied


async def clear_tokens(store: ContactBotSecretStore, *, keep: set[str]) -> list[str]:
    """Forget every contact bot token. `keep` is for a run that is adopting some.

    Atomic per variable, through the one writer: the store replaces the whole
    file each time, so a crash leaves either the old set or the new one.
    """
    forgotten: list[str] = []
    for name in sorted(store.read()):
        if name in keep:
            continue
        await store.unset(name)
        forgotten.append(name)
    return forgotten


def clear_state_files(data_dir: Path) -> tuple[bool, bool]:
    """Drop the provisioning journal and the guardian intent. Never a session."""
    journal = data_dir / "state" / "provisioning.json"
    intent = data_dir / "state" / "guardian-intent.json"
    had_journal = journal.exists()
    had_intent = intent.exists()
    journal.unlink(missing_ok=True)
    intent.unlink(missing_ok=True)
    return had_journal, had_intent


async def purge(
    *,
    database: Database,
    scoped: list[TableScope],
    store: ContactBotSecretStore,
    data_dir: Path,
    keep_tokens: set[str] | None = None,
) -> Purged:
    """The whole destructive step. Called once, after the owner's first gate."""
    outcome = Purged()
    outcome.tables = await clear_database(database, scoped)
    outcome.token_variables = await clear_tokens(store, keep=keep_tokens or set())
    outcome.journal, outcome.intent = clear_state_files(data_dir)
    logger.warning(
        "cutover purge: %s rows, %s token(s) forgotten",
        outcome.rows,
        len(outcome.token_variables),
    )
    return outcome
