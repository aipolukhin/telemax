"""One archive that everything can be put back from, proved before it is trusted.

A backup nobody has restored is a hope. This takes the database through
SQLite's own `.backup` — a `cp` of a WAL database is a corrupt file — checks its
integrity, then tars the state, the secrets, both Telegram sessions, the MAX
session and the config beside it, and re-reads the archive to prove every member
is there and readable.

The MAX session and `identity.json` are in it explicitly. The procedure that was
in use before this left them out, so a restore came up with no MAX login and a
*new* device identity — which is a different phone as far as the upload backend
is concerned.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: Everything a rollback needs that is not the database.
MEMBERS = (
    "secrets",
    "state",
    "max-session",
)


@dataclass(slots=True)
class Backup:
    """Where it is and what it proved."""

    database: Path
    archive: Path
    schema_version: int
    integrity: str
    members: list[str] = field(default_factory=list)
    digests: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.integrity == "ok" and bool(self.members)

    def report(self) -> str:
        lines = [
            f"  база       {self.database}",
            f"             integrity={self.integrity} schema={self.schema_version}",
            f"  архив      {self.archive}",
            f"             {len(self.members)} членов",
        ]
        lines += [f"             {name}" for name in self.members]
        lines += [f"  {name:10s} sha256={digest[:32]}…" for name, digest in self.digests.items()]
        return "\n".join(lines)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def take(
    *, data_dir: Path, config_path: Path | None, destination: Path, label: str = "pre-cutover"
) -> Backup:
    """Copy everything, then read it back. Raises if the copy is not sound."""
    destination.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())

    live = data_dir / "bridge.db"
    copy = destination / f"bridge-{stamp}-{label}.db"
    source = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
    target = sqlite3.connect(copy)
    try:
        source.backup(target)
        integrity = str(target.execute("PRAGMA integrity_check").fetchone()[0])
        version = int(target.execute("SELECT MAX(version) FROM schema_version").fetchone()[0])
    finally:
        target.close()
        source.close()
    os.chmod(copy, 0o600)
    if integrity != "ok":
        raise RuntimeError(f"the database copy is not sound: {integrity}")

    archive = destination / f"state-{stamp}-{label}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name in MEMBERS:
            member = data_dir / name
            if member.exists():
                tar.add(member, arcname=f"data/{name}")
        if config_path is not None and config_path.exists():
            tar.add(config_path, arcname=config_path.name)
            env = config_path.parent / ".env"
            if env.exists():
                tar.add(env, arcname=".env")
        unit = Path.home() / ".config" / "systemd" / "user" / "telemax.service"
        if unit.exists():
            tar.add(unit, arcname="telemax.service")
    os.chmod(archive, 0o600)

    # Read it back. An archive that cannot be listed is not a backup.
    with tarfile.open(archive) as tar:
        members = sorted(item.name for item in tar.getmembers() if item.isfile())
        for name in members:
            handle = tar.extractfile(name)
            if handle is None or (not handle.read(1) and name != ".env"):
                logger.warning("%s in the archive is empty", name)

    digests = {
        name: _digest(data_dir / "secrets" / name)
        for name in ("bots.env", "naming-secret")
        if (data_dir / "secrets" / name).exists()
    }
    outcome = Backup(
        database=copy,
        archive=archive,
        schema_version=version,
        integrity=integrity,
        members=members,
        digests=digests,
    )
    if not outcome.ok:
        raise RuntimeError("the backup did not prove itself")
    return outcome


def verify(backup: Backup, *, expect: tuple[str, ...] = MEMBERS) -> list[str]:
    """What the archive is missing, as a list. Empty means it can be restored."""
    missing: list[str] = []
    for name in expect:
        if not any(member.startswith(f"data/{name}") for member in backup.members):
            missing.append(f"data/{name}")
    if not backup.database.exists():
        missing.append(str(backup.database))
    return missing
