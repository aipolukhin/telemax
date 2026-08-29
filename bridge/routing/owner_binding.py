"""The bot's own id for a message the *owner* wrote. Observation, not intake.

A contact bot receives everything the owner types at it, and since owner intake
moved to the puppet session it does nothing with any of it. One thing was lost
with that: the bot's own id for the owner's message. Nothing else has it — the
MTProto intake writes the mapping row with the owner-side id and leaves the
bot's column empty — and a bot needs its own id to put a reaction on a message.

So a contact reacting in MAX to something the *owner* wrote was not mirrored
back into Telegram at all, while a reaction on the contact's own message was.
Measured: the owner's row carried `telegram_message_id = NULL` and
`ReactionSync._draw` returns on exactly that.

This closes it by observing, and the distinction matters:

* it records an id onto a row that already exists;
* it creates no job, no message, no edit, no delete and no reaction;
* it never makes the Bot API an owner-event ingress. A structural test holds
  that line, because this is the first code since the removal to touch an
  owner-authored message at all.

Only messages that are *in flight* are candidates. Everything the owner wrote
before this observer existed carries no bot-side id and never will — its Bot API
sighting happened and was dropped. Those rows are not stale candidates to be
disambiguated; they are not candidates.

**What it refuses to do is guess.** The mapping row carries no content identity
— the MTProto intake has nowhere to put one without a migration — so the only
honest evidence available is how many rows are waiting. Exactly one waiting row
is unambiguous and is bound. Two or more is *ambiguous*, and ambiguous means
nothing is bound and the number is counted: the owner sending two messages
inside the observer's latency must not have one of them wear the other's id.
Closing that case properly needs an owner-side content fingerprint on the row,
which is a schema change and is designed rather than applied here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Protocol

from bridge.routing.keyed_lock import KeyedLock
from bridge.storage.database import now_ms

logger = logging.getLogger(__name__)

#: How far back a row can have been written and still be what this observation
#: is about. Both transports see the same message at the same moment, so the gap
#: between the row and the sighting is one intake, not one conversation.
#:
#: It is not a tiebreaker. It is what makes "in flight" a set at all: without it
#: every owner message written before this observer existed is a candidate, and
#: the live smoke found 79 of them — so nothing was ever unambiguous and nothing
#: ever bound.
IN_FLIGHT_MS = 30_000


class Mappings(Protocol):
    async def unattached_owner_rows(
        self, telegram_bot_id: int, *, since_ms: int
    ) -> list[Any]: ...

    async def attach_bot_message_if_unset(
        self, link_id: int, telegram_message_id: int
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class BindingCounts:
    """Why a message has no bot-side id, for `/status` rather than an incident."""

    bound: int = 0
    #: No row was waiting. The owner typing into a bot with no bridge, or a
    #: message the session had not yet written down.
    unresolved: int = 0
    #: More than one row was waiting, so which is which cannot be told apart.
    ambiguous: int = 0
    #: The row took a different bot-side id between the read and the write.
    conflict: int = 0


class OwnerBotSideBinding:
    """Attach the bot's own id to the owner's own message. Nothing else."""

    def __init__(self, *, messages: Mappings) -> None:
        self._messages = messages
        # Per bot, because the candidate set is per bot: two observations for
        # one dialog must not both read the same single waiting row and both
        # decide it is theirs.
        self._locks = KeyedLock()
        self.counts = BindingCounts()

    def _count(self, **fields: int) -> None:
        self.counts = replace(
            self.counts, **{name: getattr(self.counts, name) + by for name, by in fields.items()}
        )

    async def observe(self, *, bot_id: int, telegram_message_id: int) -> None:
        async with self._locks.hold(("owner-binding", bot_id)):
            waiting = await self._messages.unattached_owner_rows(
                bot_id, since_ms=now_ms() - IN_FLIGHT_MS
            )

            if not waiting:
                self._count(unresolved=1)
                logger.debug("no owner message is waiting for a bot-side id")
                return

            if len(waiting) > 1:
                # Two messages in flight. Ordering across two transports is not
                # something this can prove, and binding the wrong one puts a
                # contact's reaction on the wrong message — worse than none.
                self._count(ambiguous=1)
                logger.info(
                    "%d owner messages are waiting for a bot-side id; none bound", len(waiting)
                )
                return

            link = waiting[0]
            if await self._messages.attach_bot_message_if_unset(link.id, telegram_message_id):
                self._count(bound=1)
                return

            # The row took an id between the read and the write. Not overwritten
            # — the other binding may well be the right one, and this is exactly
            # the kind of thing to look at rather than decide.
            self._count(conflict=1)
            logger.warning("an owner message already carries a different bot-side id")
