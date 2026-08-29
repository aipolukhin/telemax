"""The one writer of `bots.env`, and the only place a contact token is stored.

Every contact bot's token lives in one file, one `NAME=value` per line, and the
database only ever holds the *name*. That file used to be rewritten by reading
it, editing a line and calling `write_text` — a truncating write with no
temporary file and no lock. Two failures come out of that shape and both were
reproduced:

* **a crash mid-write loses every other token in the file.** Not the one being
  written — all of them. Every bridge then fails to come up at the next restart
  with `$TELEMAX_BOT_… holds no token`, and the only way back is fetching each
  token from Telegram again by hand.
* **two writers race.** Provisioning two contacts at once is a supported thing
  to do; both read the same file, both write their own line, and the one that
  finishes second silently drops the other's token.

So a write here is read-modify-write under a lock the *service* owns, followed
by the same atomic replacement `bridge.config.writer` already uses for the
config: a temporary file in the same directory, 0600 **before** any bytes go
into it, flush, fsync, `os.replace`, fsync of the directory. A failure at any
point leaves the previous file exactly as it was.

`os.environ` is updated only after the bytes are on disk. The process should
never believe in a token the next restart will not find.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

#: Readable by the owner, by nobody else — set on the temporary file before it
#: is written, so the token never exists at a wider mode even for a moment.
FILE_MODE = stat.S_IRUSR | stat.S_IWUSR  # 0600

DIRECTORY_MODE = stat.S_IRWXU  # 0700


def parse_env(text: str) -> dict[str, str]:
    """`NAME=value` lines to a mapping, keeping the last value of a repeated key.

    Deliberately forgiving about what it reads and strict about what it writes:
    a hand-edited file with a comment or a blank line in it must not cost the
    owner their tokens.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        name = name.strip()
        if name:
            values[name] = value
    return values


def render_env(values: dict[str, str]) -> str:
    """The whole file, in insertion order, one variable per line."""
    return "".join(f"{name}={value}\n" for name, value in values.items())


class ContactBotSecretStore:
    """Owns `bots.env`: one lock, one atomic replacement per change.

    The lock lives on this object and this object lives on the service, which is
    the whole point — the lock a provisioning walk used to hold was created
    inside the walk, so two walks never contended for it.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------------ reading

    def read(self) -> dict[str, str]:
        """Whatever is on disk right now. Values are tokens; never log them."""
        try:
            return parse_env(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except OSError:
            logger.warning("could not read %s", self._path.name)
            raise

    def get(self, name: str) -> str | None:
        return self.read().get(name)

    def fingerprint(self) -> str:
        """SHA-256 of the file, for a backup check that prints no secret."""
        import hashlib

        try:
            return hashlib.sha256(self._path.read_bytes()).hexdigest()
        except FileNotFoundError:
            return ""

    # ------------------------------------------------------------------ writing

    async def save(self, name: str, value: str) -> str:
        """Store one token and export it. Returns the variable name.

        Every other variable in the file is preserved: the file is re-read
        inside the lock, so a concurrent save of a different key cannot be
        overwritten by a snapshot taken before it.
        """
        await self._mutate(name, value)
        return name

    async def unset(self, name: str) -> None:
        """Forget one token. A name that is not there is not an error."""
        await self._mutate(name, None)

    async def _mutate(self, name: str, value: str | None) -> None:
        async with self._lock:
            values = self.read()
            if value is None:
                if name not in values:
                    os.environ.pop(name, None)
                    return
                values.pop(name)
            else:
                if values.get(name) == value and os.environ.get(name) == value:
                    return
                values[name] = value
            # Blocking, and deliberately so: this is a few kilobytes written
            # under a lock nobody else may hold, and moving it to a thread would
            # buy nothing but a way for the fsync to outlive the lock.
            self._replace(values)
            # Only now: the process must not believe in a token a restart will
            # not find.
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _replace(self, values: dict[str, str]) -> None:
        """Write the whole file atomically, or leave it exactly as it was."""
        directory = self._path.parent
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, DIRECTORY_MODE)

        handle, temporary = tempfile.mkstemp(
            dir=str(directory), prefix=f".{self._path.name}.", suffix=".tmp"
        )
        temporary_path = Path(temporary)
        try:
            # Before the bytes, not after: `mkstemp` already opens at 0600, and
            # this keeps that true if the umask or the platform ever disagrees.
            os.fchmod(handle, FILE_MODE)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(render_env(values))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self._path)
        except BaseException:
            # Including CancelledError and KeyboardInterrupt: a cancelled save
            # must leave neither litter nor a half-written file.
            temporary_path.unlink(missing_ok=True)
            raise

        # The rename is only durable once the directory entry is on disk.
        try:
            fd = os.open(str(directory), os.O_RDONLY)
        except OSError:  # pragma: no cover - platform dependent
            return
        try:
            os.fsync(fd)
        except OSError:  # pragma: no cover - platform dependent
            pass
        finally:
            os.close(fd)
