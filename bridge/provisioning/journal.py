"""A written-down record of where each bridge got to while it was being made.

Provisioning is a sequence of steps that are individually irreversible: a bot is
created, then given a token, then polled, then written to. A crash between any
two of them leaves a state that cannot be worked out by looking around — "the
username is free" means both *nothing has happened yet* and *the bot is gone and
was never rebuilt*, and those need opposite recoveries.

So each contact carries its own step, on disk, updated as it moves. After a
restart the flow resumes from the last recorded step instead of starting over,
which is what stops a retry from creating a second bot.

Three properties this file is careful about, each of them a defect once:

* **`begin` merges; it never replaces the set.** The old shape assigned
  `self._entries = fresh`, so beginning a one-contact run threw away the record
  of every attempt that was still in flight — including the `telegram_bot_id`
  and `token_env` of a bot that already existed in Telegram. That turned a
  recoverable interruption into an orphan with no local trace.
* **every mutation is a read-modify-write under a lock keyed on the file.** Two
  `ProvisioningJournal` objects over one path used to each hold their own cache
  and overwrite the other's changes.
* **a generation tells a finished attempt from a stale one.** Without it a
  completion belonging to a run the owner abandoned an hour ago could mark the
  run they started a minute ago as healthy.

Nothing secret goes in here. A token is referred to by the name of the
environment variable that holds it, exactly as everywhere else in this project.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path

from bridge.config.writer import atomic_write_text

logger = logging.getLogger(__name__)

JOURNAL_FILE_NAME = "provisioning.json"

#: One lock per journal file, shared by every object over that path. Reentrant
#: because `begin` reads through `load()`, which takes it too.
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


class ItemState(StrEnum):
    """How far one contact's bot has got. Ordered as the flow walks them."""

    PENDING = "pending"
    #: Telegram's own creation dialog is open and the owner has not pressed
    #: Create yet. The one step in the whole flow that waits on a person.
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    BOT_CREATED = "bot_created"
    #: Only ever written by journals from before Managed Bots. Nothing emits
    #: them now — a bot is reused, not deleted and rebuilt — but a file written
    #: by an older version still has to load.
    OLD_BOT_STOPPED = "old_bot_stopped"
    OLD_BOT_DELETED = "old_bot_deleted"
    WAITING_USERNAME_RELEASE = "waiting_username_release"
    TOKEN_SAVED = "token_saved"  # noqa: S105 - a step name, not a secret
    WORKER_STARTED = "worker_started"
    START_SENT = "start_sent"
    HEALTHY = "healthy"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_PERMANENT = "failed_permanent"
    #: The owner looked at a stuck attempt and said "leave it". Terminal, and
    #: deliberately *not* a deletion: the bot it names may still exist, and the
    #: entry is the only place its username is written down.
    ABANDONED = "abandoned"


#: States a resumed run may pick up from. Anything else either finished or
#: failed in a way that needs the owner to decide.
RESUMABLE = frozenset(
    {
        ItemState.PENDING,
        ItemState.AWAITING_CONFIRMATION,
        ItemState.OLD_BOT_STOPPED,
        ItemState.OLD_BOT_DELETED,
        ItemState.WAITING_USERNAME_RELEASE,
        ItemState.BOT_CREATED,
        ItemState.TOKEN_SAVED,
        ItemState.WORKER_STARTED,
        ItemState.START_SENT,
        ItemState.FAILED_RETRYABLE,
    }
)

TERMINAL = frozenset(
    {ItemState.HEALTHY, ItemState.FAILED_PERMANENT, ItemState.ABANDONED}
)

@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One contact's provisioning, as it will be found again after a crash."""

    max_chat_id: int
    expected_username: str
    title: str = ""
    max_peer_id: int | None = None
    state: ItemState = ItemState.PENDING
    #: The *name* of the variable holding the bot token, never its value.
    token_env: str | None = None
    #: Written the moment any remote answer names the bot: a `managed_bot`
    #: update, `getAdminedBots`, `getMe`. It is the only thing an operator
    #: cleaning up by hand can work from, and it used to be declared and never
    #: filled in.
    telegram_bot_id: int | None = None
    bridge_name: str | None = None
    #: A sanitised sentence, kept so a retry screen can say what went wrong.
    error: str | None = None
    #: The same thing as a code, from `ProvisioningFailure`. The sentence is for
    #: the owner and changes with the wording; this is what a screen branches on
    #: when it has to tell "wait a few minutes" from "delete a bot first".
    failure: str | None = None
    #: The bot exists and polls, but nobody has opened the chat with it, so it
    #: cannot write first. The result screen says so once, next to its button.
    needs_open: bool = False
    #: Which attempt this is. Bumped when a contact is begun again after a
    #: terminal state, so a completion belonging to the previous attempt cannot
    #: settle this one.
    generation: int = 1
    started_at: int = 0
    updated_at: int = 0

    @property
    def finished(self) -> bool:
        return self.state is ItemState.HEALTHY

    @property
    def failed(self) -> bool:
        return self.state in {ItemState.FAILED_RETRYABLE, ItemState.FAILED_PERMANENT}

    @property
    def resumable(self) -> bool:
        return self.state in RESUMABLE

    @property
    def proven_bot(self) -> bool:
        """Whether a remote effect for this attempt is known to have happened."""
        return self.telegram_bot_id is not None or self.state in {
            ItemState.BOT_CREATED,
            ItemState.TOKEN_SAVED,
            ItemState.WORKER_STARTED,
            ItemState.START_SENT,
        }


class StaleGenerationError(RuntimeError):
    """A completion arrived for an attempt that has already been superseded."""


class ProvisioningJournal:
    """Loads and saves the whole batch, atomically, at 0600.

    Safe to construct more than once over the same path: every mutation re-reads
    the file under a lock keyed on it, so two objects cannot overwrite each
    other's work.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._entries: dict[int, JournalEntry] = {}
        self._loaded = False
        self._lock = _lock_for(path)

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> ProvisioningJournal:
        return cls(data_dir / "state" / JOURNAL_FILE_NAME)

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------------ reading

    def load(self) -> list[JournalEntry]:
        with self._lock:
            if not self._loaded:
                self._entries = self._read()
                self._loaded = True
            return list(self._entries.values())

    def refresh(self) -> list[JournalEntry]:
        """Re-read from disk, discarding the cache. What reconciliation uses."""
        with self._lock:
            self._entries = self._read()
            self._loaded = True
            return list(self._entries.values())

    def _read(self) -> dict[int, JournalEntry]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A damaged journal must not wedge provisioning: the worst case is
            # that the owner is asked to press «Готово» again, and every step
            # below is idempotent.
            logger.warning("the provisioning journal is unreadable; starting a new one")
            return {}
        entries: dict[int, JournalEntry] = {}
        for item in raw if isinstance(raw, list) else []:
            entry = _entry_from(item)
            if entry is not None:
                entries[entry.max_chat_id] = entry
        return entries

    def get(self, max_chat_id: int) -> JournalEntry | None:
        self.load()
        return self._entries.get(max_chat_id)

    def entries(self) -> list[JournalEntry]:
        """In the order they were selected, which is the order they are shown."""
        return self.load()

    def selected(self, max_chat_ids: list[int]) -> list[JournalEntry]:
        """Just these contacts, in the order asked for.

        What a batch walks. `entries()` is the whole journal now that `begin`
        merges, and a run must not silently adopt an attempt nobody selected.
        """
        self.load()
        return [
            entry
            for max_chat_id in max_chat_ids
            if (entry := self._entries.get(max_chat_id)) is not None
        ]

    @property
    def unfinished(self) -> list[JournalEntry]:
        return [entry for entry in self.load() if entry.state not in TERMINAL]

    # ------------------------------------------------------------------ writing

    def begin(self, entries: list[JournalEntry]) -> list[JournalEntry]:
        """Record a selection, keeping everything already in the journal.

        Merges. The set is never replaced: an attempt that is still in flight for
        a contact nobody selected this time is the record of a bot that exists,
        and dropping it is how a recoverable interruption became an orphan.

        Per contact:

        * finished — left exactly as it is, and the batch skips it;
        * mid-flight — resumed where it stopped, with the title refreshed;
        * terminal-but-not-finished — a **new generation**, carrying whatever the
          previous attempt proved about the remote side;
        * absent — added as given.
        """
        with self._lock:
            self._entries = self._read()
            stamp = int(time.time())
            for entry in entries:
                previous = self._entries.get(entry.max_chat_id)
                self._entries[entry.max_chat_id] = _merged(previous, entry, stamp=stamp)
            self._flush()
            return [
                self._entries[entry.max_chat_id]
                for entry in entries
                if entry.max_chat_id in self._entries
            ]

    def note(
        self, max_chat_id: int, *, generation: int | None = None, **changes: object
    ) -> JournalEntry:
        """Move one entry on, refusing an update from a superseded attempt.

        `generation` is what the caller believed it was working on. Passing it is
        how a walk that has been overtaken finds out rather than settling
        somebody else's attempt.
        """
        with self._lock:
            self._entries = self._read()
            current = self._entries.get(max_chat_id)
            if current is None:
                raise KeyError(max_chat_id)
            if generation is not None and generation != current.generation:
                raise StaleGenerationError(
                    f"attempt {generation} for chat {max_chat_id} was superseded"
                    f" by attempt {current.generation}"
                )
            updated = replace(current, **changes)  # type: ignore[arg-type]
            self._entries[max_chat_id] = replace(updated, updated_at=int(time.time()))
            self._flush()
            return self._entries[max_chat_id]

    def forget(self, max_chat_id: int) -> bool:
        """Drop one entry. The documented terminal cleanup, called out loud.

        Deliberately separate from everything else: removing a record is the one
        operation that can lose the username of a bot that still exists.
        """
        with self._lock:
            self._entries = self._read()
            if self._entries.pop(max_chat_id, None) is None:
                return False
            self._flush()
            return True

    def prune_finished(self, *, older_than_seconds: int) -> int:
        """Forget entries that finished long ago. Never touches an unfinished one."""
        cutoff = int(time.time()) - older_than_seconds
        with self._lock:
            self._entries = self._read()
            doomed = [
                max_chat_id
                for max_chat_id, entry in self._entries.items()
                if entry.state is ItemState.HEALTHY and entry.updated_at < cutoff
            ]
            for max_chat_id in doomed:
                self._entries.pop(max_chat_id)
            if doomed:
                self._flush()
            return len(doomed)

    def clear(self) -> None:
        """Throw the whole journal away. Only ever an explicit operator action."""
        with self._lock:
            self._entries = {}
            self._loaded = True
            self._flush()

    def _flush(self) -> None:
        payload = [asdict(entry) for entry in self._entries.values()]
        atomic_write_text(
            self._path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        )
        self._loaded = True


def _merged(
    previous: JournalEntry | None, requested: JournalEntry, *, stamp: int
) -> JournalEntry:
    """One contact's entry after a `begin`. See `begin` for the four cases."""
    if previous is None:
        return replace(requested, started_at=requested.started_at or stamp)

    if previous.finished:
        return previous

    if previous.state in TERMINAL:
        # A contact chosen again after a terminal failure is a fresh attempt —
        # the collision it failed on may well have been fixed. The evidence
        # travels with it: a bot that exists still exists.
        return replace(
            requested,
            generation=previous.generation + 1,
            started_at=stamp,
            telegram_bot_id=previous.telegram_bot_id,
            token_env=previous.token_env,
            bridge_name=previous.bridge_name,
        )

    # Still in flight: resume where the last run stopped rather than from the
    # top, taking only the title, which is display and may have changed.
    return replace(previous, title=requested.title or previous.title, error=None)


def _entry_from(item: object) -> JournalEntry | None:
    if not isinstance(item, dict):
        return None
    known = set(JournalEntry.__slots__)
    data = {key: value for key, value in item.items() if key in known}
    try:
        if "state" in data:
            data["state"] = ItemState(data["state"])
        return JournalEntry(**data)
    except (TypeError, ValueError):
        logger.warning("a provisioning journal entry does not match the schema; dropping it")
        return None
