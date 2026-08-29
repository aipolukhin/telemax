"""Keeping reactions in step between the two sides.

MAX sends counters, never authors. In a two-person dialog that is enough: our
own reaction is known from `yourReaction`, so any other change belongs to the
contact. The previous snapshot in the database is what turns a set of totals
into "the contact just added 🔥".

Telegram's side does not enter here at all any more. A reaction the owner makes
is read from their puppet session's `UpdateEditMessage` against durable state
(`bridge/routing/owner_updates.py`), which is the only transport that sees every
one of them; `apply_owner_reaction` below is the projection of what that reading
decided, not a second reader of it.

Two protocol facts from the reaction protocol drive the sending side:

* setting a reaction *replaces* the previous one, so changing a reaction is one
  call and never a remove-then-add;
* removing one is rate limited server-side for a long time after a burst
  (`error.too-many-unlikes-dialog`), so removals go through the outbox instead
  of being retried inline.
"""

from __future__ import annotations

import logging
from typing import Protocol

from bridge.config import ReactionsConfig, ReactionStyle
from bridge.max_client import ChatReaction, MessageReactions, ReactionUpdate
from bridge.storage import (
    MessageLink,
    MessageMapRepository,
    ReactionSnapshot,
    ReactionStateRepository,
)

from .mapping import describe, to_max, to_telegram

logger = logging.getLogger(__name__)


class TelegramReactionRenderer(Protocol):
    async def set_reaction(
        self, bot_id: int, chat_id: int, message_id: int, emoji: str | None
    ) -> bool: ...



class MaxReactionSender(Protocol):
    async def add_reaction(self, chat_id: int, message_id: int, emoji: str) -> None: ...

    async def remove_reaction(self, chat_id: int, message_id: int) -> None: ...

    async def send_text(
        self, chat_id: int, text: str, *, reply_to: int | None = None
    ) -> int | None: ...

    async def reactions_for(
        self, chat_id: int, message_ids: list[int]
    ) -> dict[int, MessageReactions]: ...


class EmojiNotes(Protocol):
    """Where a note goes: the same durable queue as every message, both ways.

    Separate from `MaxReactionSender` because it is a different kind of act.
    Setting and clearing a reaction replace state and can be repeated for free;
    a note is a message somebody receives, and a second one is a second message.
    """

    async def carry_reaction_note(
        self,
        *,
        bot_id: int,
        chat_id: int,
        reply_to: int,
        text: str,
        source_key: str,
    ) -> None: ...

    async def carry_emoji_note(
        self,
        *,
        bot_id: int,
        max_chat_id: int,
        reply_to: int,
        emoji: str,
        source_key: str,
    ) -> None: ...

    async def carry_owner_reaction(
        self,
        *,
        bot_id: int,
        max_chat_id: int,
        max_message_id: int,
        owner_account_id: int,
        owner_message_id: int,
        emoji: str | None,
        pts: int,
    ) -> None: ...


class ReactionSync:
    def __init__(
        self,
        *,
        renderer: TelegramReactionRenderer,
        max_sender: MaxReactionSender,
        messages: MessageMapRepository,
        snapshots: ReactionStateRepository,
        config: ReactionsConfig,
        notes: EmojiNotes | None = None,
    ) -> None:
        self._renderer = renderer
        self._max = max_sender
        self._messages = messages
        self._snapshots = snapshots
        self._config = config
        # Where an emoji-reply note goes when MAX has no matching reaction. None
        # in the setup CLI and in tests that do not build a queue.
        self._notes = notes

    # -------------------------------------------------------------- MAX -> Telegram

    async def on_max_reaction(self, event: ReactionUpdate, *, telegram_bot_id: int) -> None:
        if self._config.style is ReactionStyle.OFF:
            return

        previous = await self._snapshots.get(event.chat_id, event.message_id)
        added, removed = _diff(previous.counters if previous else {}, event.counters)

        link = await self._messages.by_max_message(event.chat_id, event.message_id, telegram_bot_id)
        if link is None or link.telegram_message_id is None:
            # No target yet — an owner-placed message whose echo has not arrived,
            # or one this bridge never delivered. **The snapshot is not advanced.**
            # It used to be, first thing, which said "this reaction is accounted
            # for" about a reaction nothing had drawn: the next poll then compared
            # the dialog against a snapshot that already contained it, found no
            # difference, and the reaction was lost for good.
            logger.debug("reaction on unmapped message %s; left for the poll", event.message_id)
            return

        if not added:
            # Everything went away: clear ours too, if we drew one.
            if removed and self._config.style is ReactionStyle.NATIVE:
                if not await self._renderer.set_reaction(
                    telegram_bot_id, link.telegram_chat_id, link.telegram_message_id, None
                ):
                    return
        elif not await self._draw(added[-1], link, telegram_bot_id=telegram_bot_id):
            # Telegram refused and the note could not be written down either.
            # Nothing stands for this reaction yet, so nothing may say it does.
            return

        await self._snapshots.put(
            ReactionSnapshot(
                max_chat_id=event.chat_id,
                max_message_id=event.message_id,
                counters=event.counters,
                your_reaction=previous.your_reaction if previous else None,
            )
        )

    async def on_chat_reaction(self, event: ChatReaction, *, telegram_bot_id: int) -> bool:
        """A dialog's reactions may have changed — go and ask what they are.

        Returns whether anything actually changed, which is what tells the caller
        the dialog is awake and worth asking about often.

        This is the only path that fires in a private dialog: opcode 155 never
        arrives there, the server pushes the whole chat (135) instead. What that
        frame carries is not an event but a pointer, and it cannot be read as one
        because:

        * it names **one** message, `lastReactedMessageId`. Reactions on three
          messages produced two frames, and the one it named was not the one that
          had just been reacted to;
        * the fields are **absent** on a chat update that changed something else,
          while the reactions themselves are still there. Reading absence as a
          removal wiped a reaction that the contact had not removed.

        So the frame is used only as a signal that something may have moved, and
        the truth comes from op180 over a window of recent messages — which also
        covers reactions the push never mentioned at all.
        """
        if self._config.style is ReactionStyle.OFF:
            return False

        links = {
            link.max_message_id: link
            for link in await self._messages.recent_max_messages(event.chat_id, telegram_bot_id)
            if link.max_message_id is not None
        }
        if event.message_id is not None and event.message_id not in links:
            named = await self._messages.by_max_message(
                event.chat_id, event.message_id, telegram_bot_id
            )
            if named is not None:
                links[event.message_id] = named
            else:
                logger.debug("reaction on unmapped message %s", event.message_id)
        if not links:
            return False

        changed = False
        current = await self._max.reactions_for(event.chat_id, list(links))
        for message_id, link in links.items():
            reactions = current.get(message_id)
            if reactions is not None:
                changed |= await self._reconcile(
                    link, reactions, telegram_bot_id=telegram_bot_id
                )
        return changed

    async def poll(self, max_chat_id: int, *, telegram_bot_id: int) -> bool:
        """Re-read a dialog's reactions without waiting to be told.

        The push tells us about the chat's newest reaction and nothing else, so
        this is what catches a reaction put on — or taken off — an older message.
        """
        return await self.on_chat_reaction(
            ChatReaction(chat_id=max_chat_id, message_id=None, emoji=None),
            telegram_bot_id=telegram_bot_id,
        )

    async def _reconcile(
        self, link: MessageLink, reactions: MessageReactions, *, telegram_bot_id: int
    ) -> bool:
        """Bring Telegram in line with what MAX says this message carries now."""
        if link.max_message_id is None or link.telegram_message_id is None:
            return False

        previous = await self._snapshots.get(link.max_chat_id, link.max_message_id)
        theirs = _without(reactions.counters, reactions.yours)
        before = (
            _without(previous.counters, previous.your_reaction) if previous is not None else {}
        )

        if theirs == before:
            return False

        if not theirs:
            if self._config.style is ReactionStyle.NATIVE:
                if not await self._renderer.set_reaction(
                    telegram_bot_id, link.telegram_chat_id, link.telegram_message_id, None
                ):
                    return False
        # A bot holds one reaction per message, so a dialog's several become the
        # last one named.
        elif not await self._draw(list(theirs)[-1], link, telegram_bot_id=telegram_bot_id):
            # The snapshot stays where it is, so the next poll sees the same
            # difference and tries again. This is the whole retry mechanism for
            # this direction, and advancing first is what disabled it.
            return False

        await self._snapshots.put(
            ReactionSnapshot(
                max_chat_id=link.max_chat_id,
                max_message_id=link.max_message_id,
                counters=reactions.counters,
                your_reaction=reactions.yours,
            )
        )
        return True

    async def _draw(self, emoji: str, link: MessageLink, *, telegram_bot_id: int) -> bool:
        """Show a MAX reaction in Telegram: natively if it maps, in words if not.

        Returns whether the reaction is now *accounted for* — drawn, or written
        down as a job that will draw it. Only that answer may move the snapshot,
        because the snapshot is what tells the next poll there is nothing left to
        do about this message.
        """
        if link.telegram_message_id is None:  # pragma: no cover - callers check
            return False

        mapped = to_telegram(emoji) if self._config.style is ReactionStyle.NATIVE else None

        if mapped is not None and await self._renderer.set_reaction(
            telegram_bot_id, link.telegram_chat_id, link.telegram_message_id, mapped
        ):
            return True

        # No Telegram equivalent, or the API refused: say it in words rather than
        # let the reaction disappear. A note is a *message* — somebody receives
        # it, and a second one is a second message — so it goes on the durable
        # queue keyed by the message and the emoji, exactly like the note this
        # bridge already writes in the other direction. It used to be a bare
        # `send_message` from inside a poll whose exceptions were swallowed at
        # `debug` level.
        return await self._carry_note(emoji, link, telegram_bot_id=telegram_bot_id)

    async def _carry_note(
        self, emoji: str, link: MessageLink, *, telegram_bot_id: int
    ) -> bool:
        if self._notes is None or link.telegram_message_id is None:
            logger.debug("no durable route for a reaction note; not carried")
            return False
        if link.max_message_id is None:
            return False
        from bridge.routing.echo import max_reaction_note_source_key

        await self._notes.carry_reaction_note(
            bot_id=telegram_bot_id,
            chat_id=link.telegram_chat_id,
            reply_to=link.telegram_message_id,
            text=describe(emoji),
            source_key=max_reaction_note_source_key(
                link.max_chat_id, link.max_message_id, emoji
            ),
        )
        return True

    # -------------------------------------------------------------- Telegram -> MAX

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
    ) -> None:
        """Show the owner's current reaction in MAX, or take it off.

        The caller has already decided that something MAX can show has changed —
        this is the projection, not the diff. `emoji` is the representative the
        owner last chose, or None once the last of them is gone. `custom_id` is
        accepted so the shape is honest about what Telegram can send, and is
        refused rather than approximated: a document id names a sticker in
        somebody's pack, and there is no ordinary emoji that *is* it.

        Setting replaces whatever was there, so a change is one call and never a
        removal followed by an addition. Both mean the same thing however many
        times they run — but idempotent is not accounted, so the call itself is
        a durable job now, keyed by the update's version and refused by the
        worker if the owner has chosen something newer since.
        """
        if self._config.style is ReactionStyle.OFF:
            return

        if emoji is not None and to_max(emoji) is None:
            # MAX has nothing close enough. Said the way a person would, as a
            # reply carrying the emoji — a *message*, and therefore already on
            # the durable queue for its own reasons.
            await self._carry_owner_note(
                telegram_bot_id=telegram_bot_id,
                max_chat_id=max_chat_id,
                reply_to=max_message_id,
                emoji=emoji,
                owner_account_id=owner_account_id,
                owner_message_id=owner_message_id,
                pts=pts,
            )
            return

        if custom_id is not None:
            # Recorded rather than acted on: the caller has already decided that
            # a custom emoji is shown as nothing, and `emoji` is None here.
            logger.debug("owner reaction is a custom emoji; MAX is cleared")

        if self._notes is None:
            logger.debug("no durable route for an owner reaction; not carried")
            return
        await self._notes.carry_owner_reaction(
            bot_id=telegram_bot_id,
            max_chat_id=max_chat_id,
            max_message_id=max_message_id,
            owner_account_id=owner_account_id,
            owner_message_id=owner_message_id,
            emoji=to_max(emoji) if emoji is not None else None,
            pts=pts,
        )

    async def _carry_owner_note(
        self,
        *,
        telegram_bot_id: int,
        max_chat_id: int,
        reply_to: int,
        emoji: str,
        owner_account_id: int,
        owner_message_id: int,
        pts: int,
    ) -> None:
        """The emoji as a reply, through the durable queue every message uses.

        MAX has nothing close enough to the reaction. Rather than drop it or
        bother the owner with an error, it is said the way a person would — and
        it is a *message*, so it goes on the queue like every other created one,
        keyed by the update that caused it.
        """
        from bridge.routing.echo import owner_emoji_note_source_key

        if self._notes is None:
            logger.debug("no durable route for an emoji note; not carried")
            return
        await self._notes.carry_emoji_note(
            bot_id=telegram_bot_id,
            max_chat_id=max_chat_id,
            reply_to=reply_to,
            emoji=emoji,
            source_key=owner_emoji_note_source_key(
                owner_account_id, telegram_bot_id, owner_message_id, pts, emoji
            ),
        )

    async def _remember_ours(
        self, max_chat_id: int, max_message_id: int, emoji: str | None
    ) -> None:
        previous = await self._snapshots.get(max_chat_id, max_message_id)
        await self._snapshots.put(
            ReactionSnapshot(
                max_chat_id=max_chat_id,
                max_message_id=max_message_id,
                counters=previous.counters if previous else {},
                your_reaction=emoji,
            )
        )


def _without(counters: dict[str, int], ours: str | None) -> dict[str, int]:
    """The counters with our own reaction taken out of them.

    MAX counts reactions, it does not attribute them. In a two-person dialog the
    remainder is the contact's — except when both picked the same emoji, which
    the protocol gives no way to tell apart.
    """
    remaining = {emoji: count for emoji, count in counters.items() if count > 0}
    if ours is not None and ours in remaining:
        if remaining[ours] > 1:
            remaining[ours] -= 1
        else:
            del remaining[ours]
    return remaining


def _diff(before: dict[str, int], after: dict[str, int]) -> tuple[list[str], list[str]]:
    """Which reactions appeared and which disappeared."""
    added = [emoji for emoji, count in after.items() if count > before.get(emoji, 0)]
    removed = [emoji for emoji, count in before.items() if count > after.get(emoji, 0)]
    return added, removed
