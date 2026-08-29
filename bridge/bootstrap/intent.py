"""What the console was about to ask @BotFather for, written down first.

`/newbot` is the one irreversible command in the whole bootstrap, and the reply
is prose over a chat. A timeout after sending the username is indistinguishable
from a timeout before it: both leave the console knowing nothing and the account
possibly holding a new bot.

So the intent goes to disk before the command — which username, for which owner,
at which stage — and the postcondition check that follows a failure has
something to compare against. A re-run reads it and asks for the same username
rather than inventing a second one.

Nothing secret is in here. Not the token, not the phone: a username, an id, a
stage and two timestamps. The file is 0600 anyway, because everything under
`data/state` is.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from bridge.config.writer import atomic_write_text

logger = logging.getLogger(__name__)

INTENT_FILE_NAME = "guardian-intent.json"


@dataclass(frozen=True, slots=True)
class Intent:
    """One attempt to put a guardian at a deterministic username."""

    username: str = ""
    owner_user_id: int = 0
    #: `asking`, `unknown`, `limit`, `done`. Prose for an operator reading the
    #: file, and the only thing a re-run branches on is whether it is `done`.
    stage: str = ""
    started_at: int = 0
    updated_at: int = 0

    @property
    def unfinished(self) -> bool:
        return bool(self.username) and self.stage != "done"


class GuardianIntent:
    """Reads and writes the one intent file, atomically."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> GuardianIntent:
        return cls(data_dir / "state" / INTENT_FILE_NAME)

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> Intent:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return Intent()
        except (OSError, ValueError):
            logger.warning("the guardian intent file is unreadable; ignoring it")
            return Intent()
        if not isinstance(raw, dict):
            return Intent()
        known = set(Intent.__slots__)
        try:
            return Intent(**{key: value for key, value in raw.items() if key in known})
        except TypeError:
            return Intent()

    def begin(self, *, username: str, owner_user_id: int) -> Intent:
        now = int(time.time())
        return self._write(
            Intent(
                username=username,
                owner_user_id=int(owner_user_id),
                stage="asking",
                started_at=now,
                updated_at=now,
            )
        )

    def note(self, stage: str) -> Intent:
        current = self.read()
        return self._write(
            Intent(
                username=current.username,
                owner_user_id=current.owner_user_id,
                stage=stage,
                started_at=current.started_at,
                updated_at=int(time.time()),
            )
        )

    def finish(self, *, username: str) -> Intent:
        current = self.read()
        return self._write(
            Intent(
                username=username,
                owner_user_id=current.owner_user_id,
                stage="done",
                started_at=current.started_at,
                updated_at=int(time.time()),
            )
        )

    def _write(self, intent: Intent) -> Intent:
        atomic_write_text(
            self._path, json.dumps(asdict(intent), ensure_ascii=False, indent=2) + "\n"
        )
        return intent
