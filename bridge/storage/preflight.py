"""What has to be true before a uniqueness constraint can be added.

A `CREATE UNIQUE INDEX` on rows that already violate it fails, and it fails
after the migration has started. So the question is asked first, read-only, and
answered as a list of exactly which rows clash — because the remedy is never
"delete one of them": a bridge row names a bot that exists in Telegram, and
picking a winner by machine would strand somebody's conversation.

Checked in the same terms the index will use: `telegram_bot_id` where it is not
null, and `expected_username` case-folded, because Telegram treats usernames
case-insensitively and hands them back in whatever case the owner typed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .database import Database


@dataclass(frozen=True, slots=True)
class Clash:
    """One value held by more than one bridge row."""

    what: str
    value: str
    bridges: tuple[str, ...]

    def __str__(self) -> str:
        return f"{self.what} {self.value}: " + ", ".join(self.bridges)


@dataclass(slots=True)
class IdentityPreflight:
    """Everything that would stop V19, and the rows responsible."""

    clashes: list[Clash] = field(default_factory=list)
    #: Usernames stored in mixed case. Not a conflict on their own; the index
    #: folds them, so they are worth naming before it does.
    mixed_case: list[str] = field(default_factory=list)
    #: Rows whose username is an empty string rather than null. The partial
    #: index excludes them, and they are still worth knowing about.
    empty_usernames: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.clashes

    def report(self) -> str:
        lines: list[str] = []
        if self.clean:
            lines.append("V19 preflight: clean")
        else:
            lines.append(f"V19 preflight: {len(self.clashes)} conflict(s)")
            lines.extend(f"  {clash}" for clash in self.clashes)
            lines.append(
                "  Nothing was changed. Decide which bridge keeps the identity"
                " and correct the other by hand before migrating."
            )
        if self.mixed_case:
            lines.append("  mixed-case usernames: " + ", ".join(self.mixed_case))
        if self.empty_usernames:
            lines.append("  empty usernames: " + ", ".join(self.empty_usernames))
        return "\n".join(lines)


async def check_identities(database: Database) -> IdentityPreflight:
    """Read the bridges table and say whether V19 can be applied."""
    outcome = IdentityPreflight()
    rows = await database.query(
        "SELECT bridge_name, telegram_bot_id, expected_username FROM bridges"
        " ORDER BY bridge_name"
    )

    by_bot: dict[int, list[str]] = {}
    by_username: dict[str, list[str]] = {}
    for row in rows:
        name = str(row["bridge_name"])
        bot_id = row["telegram_bot_id"]
        if bot_id is not None:
            by_bot.setdefault(int(bot_id), []).append(name)
        username = row["expected_username"]
        if username is None:
            continue
        if not str(username):
            outcome.empty_usernames.append(name)
            continue
        if str(username) != str(username).lower():
            outcome.mixed_case.append(name)
        by_username.setdefault(str(username).lower(), []).append(name)

    outcome.clashes.extend(
        Clash(what="telegram_bot_id", value=str(bot_id), bridges=tuple(names))
        for bot_id, names in sorted(by_bot.items())
        if len(names) > 1
    )
    outcome.clashes.extend(
        Clash(what="expected_username", value=username, bridges=tuple(names))
        for username, names in sorted(by_username.items())
        if len(names) > 1
    )
    return outcome
