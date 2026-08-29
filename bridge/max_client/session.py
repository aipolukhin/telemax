"""Session files: where they live and who may read them.

The session database holds an auth token for the owner's MAX account. It is the
single most sensitive file the bridge writes, so the directory is 0700 and every
file in it is 0600 — enforced after each connect, because PyMax creates the file
itself and does not care.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

DIR_MODE = stat.S_IRWXU  # 0700
FILE_MODE = stat.S_IRUSR | stat.S_IWUSR  # 0600


def ensure_session_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, DIR_MODE)
    return path


def harden_session_files(path: Path) -> None:
    """Tighten permissions on everything PyMax wrote (db, -wal, -shm)."""
    if not path.is_dir():
        return
    for item in path.iterdir():
        if item.is_file():
            os.chmod(item, FILE_MODE)
