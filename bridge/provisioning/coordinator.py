"""One walk per contact, whatever tapped it.

There are six ways to start provisioning a contact — the dialog picker, a phone
number, an attached contact card, a deep link, the «Создать» button under a new
contact's announcement, and startup reconciliation — and every one of them used
to build a fresh `ProvisioningBatch` with a fresh `asyncio.Lock` inside it. A
lock created by the thing that takes it excludes nobody: two taps on «Создать»
produced two complete walks, two `save_token`s, two `start_worker`s and two
`stop_bridge`s, the second of which tore down the worker the first had just
brought up.

This object outlives a tap. It holds one lock per contact, and the critical
section is exactly one contact's walk, so two people cannot be provisioned in
series just because one of them is slow.

**The key is the MAX user id.** A personal MAX chat id is `own ^ peer`, computed
on the client, so an entry point that knows only the chat id maps onto the same
key rather than opening a second, independent lock domain — which would leave
the picker and the by-phone path guarding different things about one person. A
chat that is not a personal dialog (a group, a negative id) keeps a key of its
own; nothing bridges those, and the fallback is there so the map cannot be
poisoned by an id the formula does not describe.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ContactKey:
    """The identity a provisioning walk is serialised on."""

    value: str

    @classmethod
    def for_user(cls, max_user_id: int) -> ContactKey:
        return cls(f"u:{int(max_user_id)}")

    @classmethod
    def for_chat(cls, max_chat_id: int, *, own_user_id: int | None = None) -> ContactKey:
        """The canonical key for a chat, folded onto the user id where it can be.

        `own ^ chat` is the peer of a personal dialog — MAX's own web client
        computes the chat id that way, and it is symmetric. Folding here is what
        keeps «добавить по номеру» and the picker guarding the same person.
        """
        if own_user_id is not None and max_chat_id > 0:
            return cls.for_user(int(own_user_id) ^ int(max_chat_id))
        return cls(f"c:{int(max_chat_id)}")

    def __str__(self) -> str:  # pragma: no cover - logging convenience
        return self.value


@dataclass(slots=True)
class _Slot:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: How many callers hold or want this slot. The map is dropped back to empty
    #: when the last one leaves, so a long-lived process does not accumulate a
    #: lock per contact it has ever seen.
    users: int = 0
    #: What the holder is doing, for `/status` and for the caller that is told
    #: "already running" instead of starting a second walk.
    doing: str = ""
    #: Which task holds it. An `asyncio.Lock` is not reentrant, so a caller that
    #: already owns a contact — start-up reconciliation running the same batch a
    #: tap would — has to be let through rather than deadlocked against itself.
    holder: asyncio.Task[Any] | None = None


class ProvisioningCoordinator:
    """Serialises provisioning per contact, for the life of the service."""

    def __init__(self) -> None:
        self._slots: dict[str, _Slot] = {}

    # --------------------------------------------------------------- claiming

    @asynccontextmanager
    async def claim(self, key: ContactKey, *, doing: str = "") -> AsyncIterator[bool]:
        """Hold this contact for the duration of the block.

        Yields True when the caller has it and should do the work.

        Yields False when somebody else was already walking this contact — after
        waiting for that walk to finish. The waiting matters and the *not
        re-running* matters more: a second tap is not a second job, so the work
        happens once, and the caller that lost still returns to a journal that
        describes the finished attempt rather than drawing a result screen for a
        run that had barely started.
        """
        slot = self._slots.get(key.value)
        if slot is None:
            slot = _Slot()
            self._slots[key.value] = slot
        slot.users += 1
        here = asyncio.current_task()
        try:
            if slot.holder is not None and slot.holder is here:
                # Already ours. Reconciliation holds a contact and then runs the
                # very batch a tap would, and a non-reentrant lock taken twice by
                # one task is a deadlock, not a guard.
                yield True
                return
            if slot.lock.locked():
                logger.info("contact %s is already being provisioned (%s)", key, slot.doing)
                async with slot.lock:
                    # Wait our turn and give it straight back: this caller is
                    # here to read the outcome, not to repeat the work.
                    pass
                yield False
                return
            # No await between the check above and the acquire below: an
            # uncontended `asyncio.Lock.acquire` returns without yielding to the
            # loop, so nothing can slip in and take it first.
            await slot.lock.acquire()
            previous, slot.doing = slot.doing, doing
            slot.holder = here
            try:
                yield True
            finally:
                slot.doing = previous
                slot.holder = None
                slot.lock.release()
        finally:
            slot.users -= 1
            if slot.users <= 0 and not slot.lock.locked():
                self._slots.pop(key.value, None)

    # --------------------------------------------------------------- watching

    def busy(self, key: ContactKey) -> bool:
        slot = self._slots.get(key.value)
        return slot is not None and slot.lock.locked()

    def running(self) -> dict[str, str]:
        """Contact key to what is being done, for `/status`. No content in it."""
        return {
            value: slot.doing
            for value, slot in self._slots.items()
            if slot.lock.locked()
        }

    @property
    def tracked(self) -> int:
        """How many contacts the map is holding. Should return to zero when idle."""
        return len(self._slots)
