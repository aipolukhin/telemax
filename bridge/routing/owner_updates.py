"""One `UpdateEditMessage`, two independent questions.

Telegram sends the same constructor when the owner edits a message, when they
react to one, and when both happened — `edit_date` is set either way and there
is no flag between them. It does not say what changed; it says what the message
*is*. So the reading is a subtraction against `owner_message_state`, and it
yields two differences that are decided separately:

* **content** — the markdown body's fingerprint moved, so there is an edit to
  carry. Only for the owner's own messages: a contact bot editing its own
  message is the bridge's own delivery being corrected, and carrying that back
  into MAX would be an echo;
* **reactions** — the owner's ordered set moved. Whether MAX hears about it is a
  second question again, because MAX shows one reaction and Telegram allows
  several: taking off a reaction that was not the representative changes the set
  and changes nothing MAX can show.

Either, both or neither may be non-empty, and both are accounted before the
state moves. That ordering is the crash contract: a process that dies between
them leaves the row on its old version, so the next reading of that message —
a replay, or simply the owner's next action on it — derives what was missed. It
needs no marker of its own because both effects are identified deterministically:
the edit by its `source_key`, the reaction by being a set-or-clear that means
the same thing however many times it runs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Protocol

from bridge.routing.keyed_lock import KeyedLock
from bridge.telegram.owner_snapshot import (
    Chosen,
    chosen_of,
    content_fingerprint_of,
    decode_chosen,
    encode_chosen,
    representative,
)

logger = logging.getLogger(__name__)

#: How long a failed update waits before the next drain picks it up again.
RETRY_MS = 30_000


class Inbox(Protocol):
    """The durable record of an update, written before anything is derived."""

    async def remember_many(self, keys: list[Any]) -> int: ...

    async def remember(
        self,
        key: Any,
        *,
        content_text: str | None = ...,
        content_fingerprint: str | None = ...,
        chosen_json: str | None = ...,
    ) -> bool: ...

    async def claim_due(self, *, limit: int = ..., lease_ms: int = ...) -> list[Any]: ...

    async def account(self, key: Any) -> None: ...

    async def reopen(self, key: Any, *, delay_ms: int, error: str) -> None: ...


class OwnerState(Protocol):
    async def get(self, *, account_id: int, bot_id: int, message_id: int) -> Any: ...

    async def seed(
        self,
        *,
        account_id: int,
        bot_id: int,
        message_id: int,
        content_fingerprint: str,
        chosen_json: str,
    ) -> bool: ...

    async def advance(
        self,
        *,
        account_id: int,
        bot_id: int,
        message_id: int,
        content_fingerprint: str,
        chosen_json: str,
        pts: int,
    ) -> bool: ...


class Mappings(Protocol):
    async def by_owner_account_message(
        self, telegram_owner_account_id: int, telegram_owner_message_id: int
    ) -> Any: ...


class Edits(Protocol):
    async def on_owner_edit(
        self, *, owner_account_id: int, owner_message_id: int, text: str, edit_pts: int
    ) -> None: ...

    async def on_owner_delete(
        self, *, owner_account_id: int, owner_message_ids: list[int]
    ) -> None: ...

    async def owner_bot_for(
        self, owner_account_id: int, owner_message_id: int
    ) -> int | None: ...


class Reactions(Protocol):
    async def apply_owner_reaction(
        self,
        *,
        telegram_bot_id: int,
        max_chat_id: int,
        max_message_id: int,
        owner_account_id: int,
        owner_message_id: int,
        emoji: str | None,
        custom_id: str | None,
        pts: int,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class DispatchCounts:
    """What could not be derived, for `/status` rather than for an incident.

    A reaction on a message the owner's session cannot name is not a fault to
    alert on — it is the known limit of the echo binding, which leaves albums
    and stickers without an owner-side id. It is counted so the limit is visible
    instead of silent.
    """

    without_baseline: int = 0
    #: Updates that carried text on a message with no baseline, and were
    #: therefore *not* turned into a MAX edit. Separate from `without_baseline`
    #: because it is the one that would have produced a remote effect: if this
    #: climbs, placements are arriving without their seed and the seeding in
    #: `place_owner_message` is not doing its job.
    unproven_edit: int = 0
    unresolved_reaction: int = 0
    custom_emoji: int = 0
    resolved_reaction: int = 0
    stale_update: int = 0
    no_projection_change: int = 0


class OwnerUpdateDispatch:
    """The single reader of an owner-side `UpdateEditMessage`."""

    def __init__(
        self,
        *,
        state: OwnerState,
        messages: Mappings,
        edits: Edits,
        reactions: Reactions,
        inbox: Inbox | None = None,
    ) -> None:
        self._state = state
        # Where the update is written down before anything is derived from it.
        # None in the setup CLI and in tests that drive the dispatch directly;
        # the live path always has one.
        self._inbox = inbox
        self._messages = messages
        self._edits = edits
        self._reactions = reactions
        # One lock per (account, peer, message). The CAS keeps the row honest and
        # says nothing about the order remote calls were made in — see
        # `keyed_lock` for the divergence that was measured without it.
        self._locks = KeyedLock()
        self.counts = DispatchCounts()

    def _count(self, **fields: int) -> None:
        self.counts = replace(
            self.counts, **{name: getattr(self.counts, name) + by for name, by in fields.items()}
        )

    async def note_new_message(
        self, *, account_id: int, bot_id: int, message: Any
    ) -> None:
        """A message the session watched arrive. Its baseline is free and exact.

        Nothing has reacted to a message that has only just been sent, so the
        empty set is a fact here rather than the guess it would be on an old
        message. Written at intake so the *first* update on it — which may well
        be a reaction — has something to subtract from; without it the first
        reaction anyone put on a new message would be refused as unbaselined.

        `seed` never overwrites, so this cannot undo an update that raced it.
        """
        message_id = getattr(message, "id", None)
        if message_id is None:
            return
        await self.seed_baseline(
            account_id=account_id,
            bot_id=bot_id,
            message_id=int(message_id),
            content_fingerprint=content_fingerprint_of(message),
            chosen_json=encode_chosen(()),
        )

    async def seed_baseline(
        self,
        *,
        account_id: int,
        bot_id: int,
        message_id: int,
        content_fingerprint: str,
        chosen_json: str,
    ) -> bool:
        """Write a baseline under the same lock a live update takes.

        The bootstrap and the dispatcher can both be looking at one message, and
        a seed that landed in the middle of a reading would be a third opinion
        about what the message was before. `seed` still never overwrites, so a
        live version always wins; the lock is what stops the two overlapping
        rather than what decides between them.
        """
        async with self._locks.hold((account_id, bot_id, message_id)):
            return await self._state.seed(
                account_id=account_id,
                bot_id=bot_id,
                message_id=message_id,
                content_fingerprint=content_fingerprint,
                chosen_json=chosen_json,
            )

    async def on_owner_update(
        self,
        *,
        account_id: int,
        bot_id: int,
        message: Any,
        pts: int,
        text: str | None,
        outgoing: bool,
    ) -> None:
        """One update: write it down, then work from what was written down.

        The insert comes first and everything after it reads the row, not the
        Telethon message. That is the whole point — a process that dies here
        picks the update up again on the next start, and Telethon will not hand
        it back a second time.
        """
        message_id = int(message.id)
        fingerprint = content_fingerprint_of(message)
        chosen_json = encode_chosen(chosen_of(message))

        if self._inbox is None:
            await self._apply(
                account_id=account_id, bot_id=bot_id, message_id=message_id, pts=pts,
                fingerprint=fingerprint, chosen_json=chosen_json, text=text,
                outgoing=outgoing,
            )
            return

        from bridge.storage import InboxFamily, InboxKey

        key = InboxKey(account_id, bot_id, message_id, InboxFamily.SNAPSHOT, pts)
        await self._inbox.remember(
            key,
            # Only the owner's own words are kept: a contact's reactions are the
            # owner's, their text is not, and no edit is ever carried for one.
            content_text=text if outgoing else None,
            content_fingerprint=fingerprint,
            chosen_json=chosen_json,
        )
        await self._settle(
            key=key, fingerprint=fingerprint, chosen_json=chosen_json,
            text=text if outgoing else None, outgoing=outgoing,
        )

    async def on_owner_delete(
        self, *, account_id: int, message_ids: list[int], pts: int
    ) -> None:
        """The owner deleted messages. Written down first, then carried.

        `UpdateDeleteMessages` carries no peer at all — only owner-side ids and
        one `pts` — so the dialog comes from the same resolver the delete path
        has always used, aliases and all. An id it cannot name is a deletion in
        some other chat and is not ours; it cannot be a mapping that has *not
        been written yet*, because the row is written before the send and a
        message that was never sent cannot be deleted.

        One row per target. The batch shares its `pts`, which the key allows
        because the message id is in it, and they go down together: half a
        deletion written down is worse than none.
        """
        from bridge.storage import InboxFamily, InboxKey

        keys: list[InboxKey] = []
        for message_id in message_ids:
            # Through the mutation resolver, not the mapping: one part of an
            # album has no row of its own — its id lives in the alias table —
            # and a lookup that missed that dropped the deletion in silence.
            bot = await self._edits.owner_bot_for(account_id, message_id)
            if bot is None:
                continue
            keys.append(InboxKey(account_id, bot, message_id, InboxFamily.DELETE, pts))
        if not keys:
            logger.debug("owner delete for messages the bridge does not know; ignored")
            return

        if self._inbox is None:
            await self._edits.on_owner_delete(
                owner_account_id=account_id,
                owner_message_ids=[key.message_id for key in keys],
            )
            return

        await self._inbox.remember_many(list(keys))
        for key in keys:
            await self._settle_delete(key)

    async def _settle_delete(self, key: Any) -> None:
        """Carry one deletion, then mark its row done. Never the other way round."""
        try:
            await self._edits.on_owner_delete(
                owner_account_id=key.account_id, owner_message_ids=[key.message_id]
            )
        except Exception as error:
            if self._inbox is not None:
                await self._inbox.reopen(key, delay_ms=RETRY_MS, error=type(error).__name__)
            raise
        if self._inbox is not None:
            await self._inbox.account(key)

    async def drain(self, *, limit: int = 20) -> int:
        """Finish whatever is due. The same path a live update takes.

        Called at start-up and on the owner session's own schedule, so an update
        that was written down and never finished is not waiting for the owner to
        touch that message again.
        """
        if self._inbox is None:
            return 0
        from bridge.storage import InboxFamily

        done = 0
        for update in await self._inbox.claim_due(limit=limit):
            if update.key.family is InboxFamily.DELETE:
                await self._settle_delete(update.key)
                done += 1
                continue
            await self._settle(
                key=update.key,
                fingerprint=update.content_fingerprint or "",
                chosen_json=update.chosen_json or "[]",
                text=update.content_text,
                outgoing=update.content_text is not None,
            )
            done += 1
        return done

    async def _settle(
        self,
        *,
        key: Any,
        fingerprint: str,
        chosen_json: str,
        text: str | None,
        outgoing: bool,
    ) -> None:
        """Derive the effects, then mark the row done. Never the other way round.

        A failure leaves the row where it is — open again after a wait — so the
        next drain finishes it. Accounting before the effects were durable would
        be an event that quietly never happened.
        """
        try:
            await self._apply(
                account_id=key.account_id, bot_id=key.bot_id, message_id=key.message_id,
                pts=key.pts, fingerprint=fingerprint, chosen_json=chosen_json,
                text=text, outgoing=outgoing,
            )
        except Exception as error:
            if self._inbox is not None:
                await self._inbox.reopen(
                    key, delay_ms=RETRY_MS, error=type(error).__name__
                )
            raise
        if self._inbox is not None:
            await self._inbox.account(key)

    async def _apply(
        self,
        *,
        account_id: int,
        bot_id: int,
        message_id: int,
        pts: int,
        fingerprint: str,
        chosen_json: str,
        text: str | None,
        outgoing: bool,
    ) -> None:
        async with self._locks.hold((account_id, bot_id, message_id)):
            await self._read_decide_and_commit(
                account_id=account_id,
                bot_id=bot_id,
                message_id=message_id,
                pts=pts,
                fingerprint=fingerprint,
                chosen=decode_chosen(chosen_json),
                text=text,
                outgoing=outgoing,
            )

    async def _read_decide_and_commit(
        self,
        *,
        account_id: int,
        bot_id: int,
        message_id: int,
        pts: int,
        fingerprint: str,
        chosen: tuple[Chosen, ...],
        text: str | None,
        outgoing: bool,
    ) -> None:
        """The critical section. Every step of it is inside the lock, on purpose.

        Reading the state outside and deciding inside would be the same defect
        with a smaller window: what makes this correct is that no other reading
        of this message can begin between the read and the commit.
        """
        previous = await self._state.get(
            account_id=account_id, bot_id=bot_id, message_id=message_id
        )

        if previous is not None and pts <= previous.pts:
            self._count(stale_update=1)
            # A catch-up replaying a version already settled, or the same update
            # twice. Both readings would produce the same effects; neither would
            # produce a new one, and refusing here saves the work.
            logger.debug("owner update at a version already settled; ignored")
            return

        if previous is None:
            await self._first_sight(
                account_id=account_id,
                bot_id=bot_id,
                message_id=message_id,
                fingerprint=fingerprint,
                chosen=chosen,
                pts=pts,
                text=text,
                outgoing=outgoing,
            )
            return

        # The edit goes first because it is the durable one: if only one of the
        # two survives a crash, it should be the one that is written down.
        if outgoing and text is not None and fingerprint != previous.content_fingerprint:
            await self._edits.on_owner_edit(
                owner_account_id=account_id,
                owner_message_id=message_id,
                text=text,
                edit_pts=pts,
            )

        before = representative(decode_chosen(previous.chosen_json))
        after = representative(chosen)
        if before == after:
            self._count(no_projection_change=1)
        else:
            await self._carry_reaction(
                account_id=account_id,
                bot_id=bot_id,
                message_id=message_id,
                chosen=after,
                pts=pts,
            )

        await self._state.advance(
            account_id=account_id,
            bot_id=bot_id,
            message_id=message_id,
            content_fingerprint=fingerprint,
            chosen_json=encode_chosen(chosen),
            pts=pts,
        )

    async def _first_sight(
        self,
        *,
        account_id: int,
        bot_id: int,
        message_id: int,
        fingerprint: str,
        chosen: tuple[Chosen, ...],
        pts: int,
        text: str | None,
        outgoing: bool,
    ) -> None:
        """An update on a message with no baseline. Nothing here is a change.

        There is nothing to subtract from, so **neither** half of this update can
        be called a difference, and neither is derived:

        * **the reaction is not derived.** Guessing that there was no reaction
          before would be wrong in exactly the case that matters — a reaction
          put on yesterday whose first update after this release is its removal,
          which would then be read as an addition and put back at the contact;
        * **the edit is not carried either.** It used to be, on the reasoning
          that "at worst it is an edit that changes nothing, which MAX absorbs".
          MAX does not necessarily absorb it: the target may be past the edit
          window. And it does not change nothing: the text an update reports for
          a message the bridge
          rendered is the *rendering*, stamp and all, so what MAX would have
          absorbed is `[29/07 13:47]` written into the original.

        Missing baseline means **unknown**, and unknown is not "changed". The
        baseline is written from this update, so the very next one on the same
        message is an ordinary subtraction and a genuine later edit produces
        exactly one MAX edit — one, from the reading that can prove it.

        Counted rather than alerted on: it is the known cost of a message the
        session never watched arrive, and `place_owner_message` now seeds a
        baseline for the placements that used to land here in bulk.
        """
        if outgoing and text is not None:
            self._count(unproven_edit=1)
        self._count(without_baseline=1)
        logger.info(
            "owner update on a message with no baseline; nothing is derived from it"
        )
        await self._state.advance(
            account_id=account_id,
            bot_id=bot_id,
            message_id=message_id,
            content_fingerprint=fingerprint,
            chosen_json=encode_chosen(chosen),
            pts=pts,
        )

    async def _carry_reaction(
        self,
        *,
        account_id: int,
        bot_id: int,
        message_id: int,
        chosen: Chosen | None,
        pts: int,
    ) -> None:
        link = await self._messages.by_owner_account_message(account_id, message_id)
        if link is None or link.max_message_id is None:
            # An album or a sticker: the echo binding leaves those without an
            # owner-side id, so there is no MAX message to put a reaction on.
            # Counted, not alerted on, and never guessed at.
            self._count(unresolved_reaction=1)
            logger.info("owner reaction on a message the bridge cannot name; not carried")
            return

        emoji: str | None = None
        if chosen is not None:
            if chosen.kind == "c":
                # A custom emoji is a document in somebody's sticker pack, and
                # there is no ordinary emoji that *is* it. What MAX is told is
                # therefore "nothing" rather than the reaction the owner chose
                # before this one: leaving the old one there would show the
                # contact a choice the owner has since moved on from, dressed up
                # as the current one. Clearing shows less, and nothing false.
                self._count(custom_emoji=1)
                logger.info("owner reaction is a custom emoji; MAX is cleared instead")
            else:
                emoji = chosen.value

        self._count(resolved_reaction=1)
        await self._reactions.apply_owner_reaction(
            telegram_bot_id=bot_id,
            max_chat_id=link.max_chat_id,
            max_message_id=link.max_message_id,
            owner_account_id=account_id,
            owner_message_id=message_id,
            emoji=emoji,
            custom_id=chosen.value if chosen is not None and chosen.kind == "c" else None,
            pts=pts,
        )
