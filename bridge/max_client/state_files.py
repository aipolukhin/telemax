"""Small state files that must survive a power cut without changing meaning.

Two files live next to the MAX session database, and both had the same shape of
bug: `write_text` truncates first and writes second, so a machine that dies in
between leaves a file that exists, is readable, and is empty.

What that costs is not a lost setting. `identity.json` is the phone this install
claims to be, and the fallback for an unreadable one drew a *new* random device
id — so one torn write turned the account into a device that changed model once
and then changed its `ANDROID_ID` on every restart afterwards, for ever, because
nothing repaired the file. That is precisely the "stolen account" signal the
identity module exists to avoid. `client_session_id` is a launch counter whose
whole point is that it only goes up; a torn write sent it back to 1.

So: write to a temporary file in the same directory, `fsync` it, `os.replace`
it over the target — atomic on POSIX, the reader sees either the old bytes or
the new ones and never a prefix — then `fsync` the directory so the rename
itself survives the same power cut. The previous contents are kept alongside as
`<name>.bak`, written the same way, which is what makes recovery possible rather
than merely hoped for.

`ProcessLock` remains the only thing keeping two processes off one data
directory. Nothing here is a lock and nothing here should grow into one: these
are single-writer files, and the failure they defend against is the machine
stopping, not a second writer.
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

#: Suffix for the copy kept beside each file. Written before the primary moves,
#: so at any instant at least one of the two is a complete previous state.
BACKUP_SUFFIX = ".bak"

#: Owner-only. The directory is 0700 already; this is the belt to that braces,
#: and it is set on the temporary file *before* anything is written into it, so
#: there is no window where the content exists at a wider mode.
FILE_MODE = 0o600

class StateFileError(Exception):
    """A state file could not be read, and no usable copy of it exists.

    Deliberately loud. The alternative — inventing a replacement — is what turned
    one torn write into an account that reported a different phone on every
    start, and an invented identity is worse than a refusal because nobody finds
    out.
    """


def write_atomic(path: Path, payload: str) -> None:
    """Replace `path` with `payload`, keeping a usable copy as `.bak`.

    Not atomic across the pair, and does not need to be: the copy is written
    first, so the two are never both mid-write, and a reader that finds a torn
    copy still has an intact primary.

    **The copy is seeded on the first write, not only on the second.** That looks
    like a detail and is the whole thing: `identity.json` is written exactly once
    in the life of an install, so a copy that only appears when a file is
    *rewritten* would never appear at all, and the recovery below would have
    nothing to recover from. Seeding it means the copy briefly equals the
    primary, which is a fine state to be in and an infinitely better one than
    having no copy.
    """
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    previous: str | None = None
    if path.exists():
        try:
            previous = path.read_text(encoding="utf-8")
        except OSError:
            # Unreadable rather than absent. Nothing to copy, and the write it
            # was meant to protect must still happen.
            logger.warning("could not read %s before rewriting it", path.name)

    if previous is not None:
        _try_replace(backup, previous, path.name)
    _replace(path, payload)
    if previous is None and not backup.exists():
        _try_replace(backup, payload, path.name)


def ensure_backup(path: Path) -> bool:
    """Make a copy of `path` if there is not one already. Returns True if it wrote.

    For files that are written once and then only read — `identity.json` is the
    whole reason this exists. `write_atomic` seeds the copy as it writes, which
    covers every file this install creates from now on and covers *nothing* that
    already exists: an identity drawn last week is valid, so it is never
    rewritten, so it would never get a copy, so the recovery below would have
    nothing to recover from on precisely the installs that have the most to lose.

    Writes only the copy. The primary is not touched, which is the property that
    lets this run on every start without ever changing what the account reports.
    """
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if backup.exists() or not path.exists():
        return False
    try:
        _replace(backup, path.read_text(encoding="utf-8"))
    except OSError:
        logger.warning("could not seed a copy of %s", path.name)
        return False
    return True


def _try_replace(path: Path, payload: str, describing: str) -> None:
    """A copy that fails to write is worth a line, never an exception: it is the
    safety net, and dropping the real write to complain about the net is worse."""
    try:
        _replace(path, payload)
    except OSError:
        logger.warning("could not keep a copy of %s", describing)


def _replace(path: Path, payload: str) -> None:
    handle, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        os.fchmod(handle, FILE_MODE)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    _sync_directory(path.parent)


def _sync_directory(directory: Path) -> None:
    """Make the rename itself durable, not just the bytes it points at."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        # Some filesystems refuse to fsync a directory. The rename is still
        # ordered; only its durability across a crash is weaker.
        logger.debug("could not fsync %s", directory, exc_info=True)
    finally:
        os.close(fd)


def read_with_backup[T](path: Path, parse: Callable[[str], T]) -> tuple[T | None, bool]:
    """`(value, repaired)` — the primary, or the backup, or nothing.

    `parse` raises for content it cannot make sense of; anything it raises is
    treated as "this copy is not usable", which is the only distinction that
    matters here.

    `repaired` is True when the answer came from the backup. The caller is
    expected to write it back so the next start reads a good primary — and to
    say so out loud, because a file that needed recovering is a machine that
    stopped badly.
    """
    for candidate, from_backup in (
        (path, False),
        (path.with_name(path.name + BACKUP_SUFFIX), True),
    ):
        try:
            raw = candidate.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError:
            logger.warning("could not read %s", candidate.name, exc_info=True)
            continue
        try:
            return parse(raw), from_backup
        except Exception:  # noqa: BLE001 - any parse failure means "not usable"
            logger.warning("%s is not readable state; trying the copy", candidate.name)
    return None, False


__all__ = [
    "BACKUP_SUFFIX",
    "StateFileError",
    "ensure_backup",
    "read_with_backup",
    "write_atomic",
]
