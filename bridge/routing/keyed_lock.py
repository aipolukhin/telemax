"""One lock per identity, so two updates about one message cannot interleave.

A compare-and-set on the stored version keeps the *row* honest and says nothing
about the order remote calls were made in. Two `UpdateEditMessage` for one
message arrive as two `asyncio` tasks — Telethon dispatches with
`sequential_updates=False` by default and the bridge does not override it — so
both can read the same old state, both can decide, and the one that reaches MAX
last is the one MAX ends up showing. Measured on the deployed build: pts 11 set
❤ and committed, pts 10 set 👍 afterwards and lost the CAS, leaving the row
saying ❤ and MAX showing 👍.

So the whole critical section — read, staleness check, both diffs, both effects,
and the commit — runs under one lock keyed by the identity the update carries.
Different messages, different dialogs and different accounts do not wait on each
other; only two readings of the *same* message do.

One process is enough: Telemax refuses a second instance over one data directory
(`ProcessLock`), so there is no second reader to coordinate with.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Hashable
from contextlib import asynccontextmanager


class KeyedLock:
    """Mutual exclusion per key, with no map that grows for ever.

    A lock is created when the first holder asks for it and dropped when the
    last one leaves, counted rather than guessed at: dropping on "not locked"
    would race a waiter that has not been scheduled yet, and never dropping
    would keep one entry per message the bridge has ever seen.
    """

    __slots__ = ("_locks",)

    def __init__(self) -> None:
        self._locks: dict[Hashable, tuple[asyncio.Lock, list[int]]] = {}

    @asynccontextmanager
    async def hold(self, key: Hashable) -> AsyncIterator[None]:
        entry = self._locks.get(key)
        if entry is None:
            entry = (asyncio.Lock(), [0])
            self._locks[key] = entry
        lock, waiting = entry
        waiting[0] += 1
        try:
            # `finally` rather than a happy path: a holder cancelled while
            # waiting must not leave the count — or the lock — behind.
            async with lock:
                yield
        finally:
            waiting[0] -= 1
            if waiting[0] == 0:
                self._locks.pop(key, None)

    @property
    def held(self) -> int:
        """How many keys are live. Only a test and a leak check read this."""
        return len(self._locks)
