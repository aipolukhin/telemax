"""One Telemax per data directory, enforced by the kernel.

Two copies of this process is the failure that produces the strangest bug
reports: both poll the same bot token, so each `getUpdates` steals the other's
updates, and roughly half of every conversation vanishes. Nothing in the logs
says so — each process looks healthy and merely quiet.

An advisory `flock` is the cheapest honest answer. It is held by the file
descriptor, so it disappears when the process does, including on a kill -9 —
there is no stale lock file to clean up, and no PID to guess about.
"""

from __future__ import annotations

import fcntl
import logging
import os
from pathlib import Path
from types import TracebackType
from typing import Self

logger = logging.getLogger(__name__)

LOCK_FILE_NAME = "telemax.lock"


class AlreadyRunning(Exception):  # noqa: N818 - reads as the condition it is
    """Another Telemax already holds this data directory."""


class ProcessLock:
    """An exclusive advisory lock on one file, released when the process ends."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> ProcessLock:
        return cls(data_dir / LOCK_FILE_NAME)

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> Self:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(fd)
            holder = self._holder()
            raise AlreadyRunning(
                f"Telemax уже работает{holder} — второй экземпляр не запускается."
            ) from error

        # The pid is a courtesy for whoever is reading the file, never a lock:
        # the flock above is what actually excludes anybody.
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        return self

    def _holder(self) -> str:
        try:
            pid = self._path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        return f" (pid {pid})" if pid.isdigit() else ""

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> Self:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
