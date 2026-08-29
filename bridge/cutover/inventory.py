"""What a V2 cutover would destroy, and what it would create — read only.

Nothing in this module changes anything. It is the evidence the owner is shown
before either gate: the bots that exist now, the names the V2 contract gives
them, and the exact set of local rows that would go.

The destructive set is **derived**, not listed from memory. Every table in the
schema is classified by whether it carries a bridge identity — `bridge_name`,
`telegram_bot_id`, `bot_id`, or a `max_chat_id` that only means something
through a bridge — and anything that does not is account-level and survives. A
list written by hand would be wrong the first time a table was added.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from bridge.provisioning.naming_v2 import (
    contact_bot_username_v2,
    guardian_bot_username_v2,
)
from bridge.provisioning.provisioner import UsernameState
from bridge.storage.database import Database

logger = logging.getLogger(__name__)

#: Tables that hold nothing about a bridge. Account-level, process-level, or
#: caches of remote state that would cost something real to rebuild.
#:
#: `sticker_cache` and `sticker_origin` are the ones worth naming: sending a
#: sticker to MAX *creates one in the owner's collection*, and these are how the
#: same picture resolves to the same id. Wiping them would litter the owner's
#: MAX account with duplicates on the first sticker after a cutover. They belong
#: to the account, not to any bot.
KEEP: frozenset[str] = frozenset(
    {
        "schema_version",
        "health_state",
        "sticker_cache",
        "sticker_origin",
        "forward_author",
    }
)

#: Columns whose presence means a row belongs to a bridge identity.
_BRIDGE_COLUMNS = ("bridge_name", "telegram_bot_id", "bot_id", "max_chat_id")


class Role(StrEnum):
    GUARDIAN = "guardian"
    CONTACT = "contact"


@dataclass(frozen=True, slots=True)
class LegacyBot:
    """One Telegram bot this installation currently uses."""

    role: Role
    bot_id: int | None
    username: str
    bridge_name: str = ""
    state: str = ""
    max_chat_id: int | None = None
    max_user_id: int | None = None
    token_env: str = ""
    reachable: bool = True

    def line(self) -> str:
        where = (
            f" · MAX peer {self.max_user_id} / chat {self.max_chat_id}"
            if self.max_chat_id is not None
            else ""
        )
        alive = "" if self.reachable else " · TOKEN DEAD"
        return (
            f"{self.role.value:8s} @{self.username:34s} id={self.bot_id}"
            f" {self.state or '-':12s}{where}{alive}"
        )


@dataclass(frozen=True, slots=True)
class Planned:
    """One bot the cutover would end up with."""

    role: Role
    username: str
    state: UsernameState
    max_user_id: int | None = None
    max_chat_id: int | None = None
    title: str = ""

    @property
    def blocked(self) -> bool:
        """Held by something the cutover cannot drive. Both cases stop it.

        `UNMANAGEABLE` counts: the name is taken by a bot this manager cannot
        get a token for, and there is no `deleteManagedBot` to clear it. Nothing
        downstream can proceed on that name until a person removes the bot.
        """
        return self.state in {UsernameState.FOREIGN, UsernameState.UNMANAGEABLE}

    def line(self) -> str:
        verb = {
            UsernameState.OWNED: "adopt",
            UsernameState.FREE: "CREATE",
            UsernameState.FOREIGN: "BLOCKED — held by another account",
            UsernameState.UNMANAGEABLE: "BLOCKED — bot exists, not manageable",
        }[self.state]
        who = f" · {self.title}" if self.title else ""
        return f"{self.role.value:8s} @{self.username:34s} {verb}{who}"


@dataclass(frozen=True, slots=True)
class TableScope:
    name: str
    rows: int
    scoped: bool
    why: str


@dataclass(slots=True)
class CutoverInventory:
    """Everything both gates are decided from."""

    legacy: list[LegacyBot] = field(default_factory=list)
    planned: list[Planned] = field(default_factory=list)
    tables: list[TableScope] = field(default_factory=list)
    telegram_owner_user_id: int = 0
    max_owner_user_id: int = 0
    orphan_bridge_names: list[str] = field(default_factory=list)

    @property
    def destructive(self) -> list[TableScope]:
        return [item for item in self.tables if item.scoped]

    @property
    def preserved(self) -> list[TableScope]:
        return [item for item in self.tables if not item.scoped]

    @property
    def rows_to_clear(self) -> int:
        return sum(item.rows for item in self.destructive)

    @property
    def blocked(self) -> list[Planned]:
        return [item for item in self.planned if item.blocked]

    @property
    def to_create(self) -> list[Planned]:
        return [item for item in self.planned if item.state is UsernameState.FREE]

    @property
    def to_adopt(self) -> list[Planned]:
        return [item for item in self.planned if item.state is UsernameState.OWNED]


async def classify_tables(database: Database) -> list[TableScope]:
    """Every table, sorted into "belongs to a bridge" and "does not".

    Derived from the columns rather than from a list somebody maintains: the
    audit's whole complaint about decommission was that a hand-written set is
    wrong the first time a table is added.
    """
    names = [
        str(row["name"])
        for row in await database.query(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    scoped: list[TableScope] = []
    for name in names:
        columns = {
            str(row["name"]) for row in await database.query(f"PRAGMA table_info({name})")
        }
        # The name came out of `sqlite_master` a moment ago; there is no user
        # input anywhere in this statement, and a table name cannot be bound.
        count = int(
            (await database.query(f"SELECT COUNT(*) AS n FROM {name}"))[0]["n"]  # noqa: S608
        )
        if name in KEEP:
            scoped.append(
                TableScope(name=name, rows=count, scoped=False, why="account or process level")
            )
            continue
        carried = sorted(columns & set(_BRIDGE_COLUMNS))
        if carried:
            scoped.append(
                TableScope(name=name, rows=count, scoped=True, why="keyed on " + ", ".join(carried))
            )
        else:
            scoped.append(
                TableScope(name=name, rows=count, scoped=True, why="provisioning or incident state")
            )
    return scoped


async def take(
    *,
    database: Database,
    bridges: Any,
    guardian_bot_id: int | None,
    guardian_username: str,
    telegram_owner_user_id: int,
    max_owner_user_id: int,
    peers: list[tuple[int, int, str]],
    check_username: Any,
    token_alive: Any,
) -> CutoverInventory:
    """Build the whole picture. Reads Telegram and MAX; writes nothing.

    `peers` is `(max_user_id, max_chat_id, title)` for each contact that will get
    a V2 bot. `check_username` and `token_alive` are the two remote questions,
    injected so this can be exercised without an account.
    """
    inventory = CutoverInventory(
        telegram_owner_user_id=telegram_owner_user_id,
        max_owner_user_id=max_owner_user_id,
        tables=await classify_tables(database),
    )

    inventory.legacy.append(
        LegacyBot(
            role=Role.GUARDIAN,
            bot_id=guardian_bot_id,
            username=guardian_username,
            state="running",
        )
    )
    rows = await bridges.all()
    for record in rows:
        inventory.legacy.append(
            LegacyBot(
                role=Role.CONTACT,
                bot_id=record.telegram_bot_id,
                username=record.expected_username or "?",
                bridge_name=record.bridge_name,
                state=record.state.value,
                max_chat_id=record.max_chat_id,
                max_user_id=record.max_user_id,
                token_env=record.token_env,
                reachable=await token_alive(record.token_env),
            )
        )

    # Bridge names that appear in the mapping tables and in no row: earlier eras
    # of this install. They are part of the destructive set and worth naming.
    known = {record.bridge_name for record in rows}
    seen = {
        str(row["bridge_name"])
        for row in await database.query("SELECT DISTINCT bridge_name FROM message_map")
    }
    inventory.orphan_bridge_names = sorted(seen - known)

    wanted_guardian = guardian_bot_username_v2(telegram_owner_user_id, max_owner_user_id)
    inventory.planned.append(
        Planned(
            role=Role.GUARDIAN,
            username=wanted_guardian,
            state=await check_username(wanted_guardian),
        )
    )
    for max_user_id, max_chat_id, title in peers:
        username = contact_bot_username_v2(telegram_owner_user_id, max_user_id)
        inventory.planned.append(
            Planned(
                role=Role.CONTACT,
                username=username,
                state=await check_username(username),
                max_user_id=max_user_id,
                max_chat_id=max_chat_id,
                title=title,
            )
        )
    return inventory


def report(inventory: CutoverInventory) -> str:
    """The two gates' evidence, in one screen, with nothing secret in it."""
    lines = [
        "Telegram owner   " + str(inventory.telegram_owner_user_id),
        "MAX owner        " + str(inventory.max_owner_user_id),
        "",
        "СЕЙЧАС — боты, которые перестанут использоваться:",
    ]
    lines += ["  " + bot.line() for bot in inventory.legacy]
    lines += ["", "СТАНЕТ — V2:"]
    lines += ["  " + item.line() for item in inventory.planned]

    lines += ["", "БУДЕТ ОЧИЩЕНО локально:"]
    for table in inventory.destructive:
        lines.append(f"  {table.name:24s} {table.rows:6d} строк · {table.why}")
    lines.append(f"  {'ИТОГО':24s} {inventory.rows_to_clear:6d} строк")
    if inventory.orphan_bridge_names:
        lines.append(
            "  осиротевшие имена мостов в маппингах: "
            + ", ".join(inventory.orphan_bridge_names)
        )

    lines += ["", "СОХРАНЯЕТСЯ:"]
    for table in inventory.preserved:
        lines.append(f"  {table.name:24s} {table.rows:6d} строк · {table.why}")
    lines += [
        "  MAX session, MAX device identity, Telegram owner session,",
        "  BotFather session, config — файлы не трогаются.",
    ]

    if inventory.blocked:
        lines += ["", "ОСТАНОВКА — эти имена принадлежат другому аккаунту:"]
        lines += [f"  @{item.username}" for item in inventory.blocked]
    return "\n".join(lines)
