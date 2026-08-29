"""What MAX did to a message it already sent — carried durably, to one transport.

Deleting and editing were the last two remote effects the bridge performed from
an ingress handler. `on_max_delete` called `bot.delete_message` and
`on_max_edit` called `bot.edit_message_text`, both through an adapter that
answered `False` for every failure, and both with no job, no retry and no
accounting behind them. Three things were wrong with that, and each of them
cost something real:

* **a delete reached the head and nothing else.** An album is one MAX message
  and N Telegram messages, and only the canonical id was ever removed — the
  other parts stayed in the chat as a group whose counterpart no longer existed
  anywhere. The mirror-image sweep for the other direction has been there since
  V16; this direction simply never had one;
* **a caption could not be edited at all.** `editMessageCaption` appeared
  nowhere in the tree, so every caption edit in MAX went out as
  `editMessageText`, which Telegram refuses on a media message — and the refusal
  was swallowed, so the two sides drifted apart in silence;
* **a message the owner had placed was addressed with the wrong id.** The bot
  cannot edit or delete somebody else's message, and the id the mapping holds
  for an owner-authored placement is the bot's *view* of it.

So the transport is chosen from what actually placed the message, and the choice
is read off the delivering job rather than guessed from which columns happen to
be filled: a bot-delivered message eventually carries an owner-side id too, and
an owner-placed one eventually carries a bot-side id, so neither column can tell
them apart. The job kind can, and it is durable.

**Neither operation creates anything.** Both replace or remove state that is
already there, so a repeat is free, "already gone" is success, and a failure
costs nothing but another attempt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from bridge.routing.delivery import (
    KIND_MAX_TO_TG_MEDIA,
    KIND_MAX_TO_TG_OWNER,
    DeferDelivery,
)
from bridge.storage import MediaGroupPart, MessageLink
from bridge.telegram.errors import TelegramOutcome, classify_telegram

logger = logging.getLogger(__name__)

#: Shown in place of a message MAX deleted that Telegram will no longer remove —
#: a bot may only delete its own messages, and only for 48 hours.
#:
#: **Policy, and narrow.** It is drawn only when Telegram has *confirmed* that
#: the deletion is refused, never as a catch-all for an exception, and only on a
#: bot-authored message, because it is an edit and the bot can only edit its own.
#: A timeout is a retry and gets no notice; a message the owner placed gets none
#: either, and the job says so in words the owner can read.
DELETED_NOTICE = "Сообщение удалено в MAX."

#: How long a mutation waits for the owner's session to come back. The same
#: shape the album sweep uses, and for the same reason: waiting costs no attempt,
#: so a session down for twenty minutes does not walk an owed deletion to FAILED.
SESSION_WAIT_MS = 5_000


class Authorship(StrEnum):
    """Who put the message in the chat, and therefore who may change it."""

    BOT = "bot"
    OWNER = "owner"


def delete_source_key(max_chat_id: int, max_message_id: int) -> str:
    """One MAX message, one delete job, however many times the event arrives.

    MAX repeats a deletion after a reconnect and can name several ids in one
    event; each id is its own logical message and gets its own key, so a batch is
    N independent, independently-deduped effects and a replay finds them all
    already made.

    No version in the key on purpose: a message is deleted once and stays
    deleted, so a second event for the same id is the same effect and not a new
    one.
    """
    return f"max-del:{max_chat_id}:{max_message_id}"


def edit_source_key(max_chat_id: int, max_message_id: int, version: str) -> str:
    """One MAX edit, keyed by the version it carried.

    Unlike a delete, an edit *can* legitimately happen twice with different
    content, so the version is part of the identity — otherwise the second edit
    would find the first one's job and do nothing, leaving Telegram showing text
    the owner had already replaced.

    Two edits therefore become two jobs, and the queue's FIFO order per bridge
    and direction is what makes the later one land last. That ordering is the
    coalescing: the final value wins because it is applied last, not because
    anything merged it.
    """
    return f"max-edit:{max_chat_id}:{max_message_id}:{version}"


class BotMutations(Protocol):
    """Editing and deleting the bot's own messages. Failures travel."""

    async def delete(self, bot_id: int, chat_id: int, message_id: int) -> None: ...

    async def edit_text(
        self,
        bot_id: int,
        chat_id: int,
        message_id: int,
        text: str,
        entities: list[dict[str, Any]] | None = None,
    ) -> None: ...

    async def edit_caption(
        self,
        bot_id: int,
        chat_id: int,
        message_id: int,
        caption: str,
        entities: list[dict[str, Any]] | None = None,
    ) -> None: ...


class OwnerMutations(Protocol):
    """The same two verbs over the owner's own session, for what they placed."""

    @property
    def is_connected(self) -> bool: ...

    async def delete_own_messages(self, peer_id: int, message_ids: list[int]) -> None: ...

    async def edit_own_message(
        self,
        peer_id: int,
        message_id: int,
        text: str,
        *,
        entities: list[dict[str, Any]] | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class MaxTarget:
    """Everything a mutation needs, resolved from durable state alone.

    `message_ids` is every Telegram message this one MAX message became, in the
    id space the chosen transport speaks. `caption_target` is the one part a
    caption lives on, which for an album is read from the alias that recorded
    carrying it rather than assumed to be the first.
    """

    link: MessageLink
    authorship: Authorship
    bot_id: int
    chat_id: int
    owner_account_id: int | None
    message_ids: tuple[int, ...]
    caption_target: int | None
    is_media: bool


async def _delivering_job(outbox: Any, link: MessageLink) -> Any | None:
    """The job that put this message in the chat, if the queue still has it."""
    if outbox is None or link.max_chat_id is None or link.max_message_id is None:
        return None
    return await outbox.by_source_key(f"max:{link.max_chat_id}:{link.max_message_id}")


async def resolve_target(
    *,
    messages: Any,
    albums: Any,
    outbox: Any,
    link_id: int,
) -> MaxTarget | None:
    """What one MAX message became in Telegram, and who may change it.

    Returns None when there is nothing to act on: the mapping is gone, or the
    delivery never produced a Telegram message at all. Both are terminal
    successes for a mutation — there is no message to edit and none to delete.
    """
    link = await messages.by_id(link_id)
    if link is None:
        return None

    job = await _delivering_job(outbox, link)
    if job is not None:
        authorship = Authorship.OWNER if job.kind == KIND_MAX_TO_TG_OWNER else Authorship.BOT
        is_media = job.kind in (KIND_MAX_TO_TG_MEDIA, KIND_MAX_TO_TG_OWNER)
    else:
        # No job: a delivery from before the queue, or a router built without
        # one. Fall back to the columns, which are ambiguous only once both are
        # filled — and a message with a bot-side id was placed by the bot.
        authorship = (
            Authorship.BOT if link.telegram_message_id is not None else Authorship.OWNER
        )
        is_media = False

    parts: list[MediaGroupPart] = list(await albums.parts_of_link(link_id)) if albums else []

    if authorship is Authorship.OWNER:
        ids = tuple(
            int(part.telegram_owner_message_id)
            for part in parts
            if part.telegram_owner_message_id is not None
            and (
                link.telegram_owner_account_id is None
                or part.telegram_owner_account_id == link.telegram_owner_account_id
            )
        ) or (
            (int(link.telegram_owner_message_id),)
            if link.telegram_owner_message_id is not None
            else ()
        )
        caption = _caption_target(parts, owner=True) or (
            int(link.telegram_owner_message_id)
            if link.telegram_owner_message_id is not None
            else None
        )
    else:
        ids = tuple(
            int(part.telegram_message_id)
            for part in parts
            if part.telegram_message_id is not None
        ) or (
            (int(link.telegram_message_id),) if link.telegram_message_id is not None else ()
        )
        caption = _caption_target(parts, owner=False) or (
            int(link.telegram_message_id) if link.telegram_message_id is not None else None
        )

    if not ids:
        return None
    return MaxTarget(
        link=link,
        authorship=authorship,
        bot_id=int(link.telegram_bot_id),
        chat_id=int(link.telegram_chat_id),
        owner_account_id=link.telegram_owner_account_id,
        message_ids=ids,
        caption_target=caption,
        is_media=is_media or bool(parts),
    )


def _caption_target(parts: list[MediaGroupPart], *, owner: bool) -> int | None:
    """Which part of an album carries the group's caption.

    Read from the alias that recorded carrying it when the album was written
    down, not assumed to be part 0. The outgoing side does put it first, but the
    fact is persisted and the persisted fact is what a mutation should act on —
    the incoming side already proved a caption can sit anywhere in a group.
    """
    for part in parts:
        if not part.caption_present:
            continue
        value = part.telegram_owner_message_id if owner else part.telegram_message_id
        if value is not None:
            return int(value)
    return None


def _require_session(owner: OwnerMutations | None) -> OwnerMutations:
    if owner is None or not owner.is_connected:
        # Waiting, not failing. The message is the owner's and only their own
        # account can touch it, so there is nothing else to try.
        raise DeferDelivery(SESSION_WAIT_MS)
    return owner


async def resolve_max_delete(
    *,
    messages: Any,
    albums: Any,
    outbox: Any,
    bot: BotMutations,
    owner: OwnerMutations | None,
    payload: dict[str, Any],
) -> int | None:
    """Take one MAX message out of Telegram — all of it, or say why not.

    Idempotent throughout: Telegram answering "message to delete not found" is
    the state this job was asked to reach, so it counts as done. That is what
    makes a retry after a timeout safe and a partial batch self-healing — the
    ids that went are no-ops on the next pass and the ones that did not are
    tried again.
    """
    from bridge.retry.worker import PermanentDeliveryError

    link_id = payload.get("link_id")
    if link_id is None:
        return None
    target = await resolve_target(
        messages=messages, albums=albums, outbox=outbox, link_id=int(link_id)
    )
    if target is None:
        # Never delivered, or the mapping is gone. There is no message to remove.
        logger.debug("MAX delete for a message with nothing in Telegram behind it")
        return None

    if target.authorship is Authorship.OWNER:
        session = _require_session(owner)
        # One request for the whole group: MTProto takes a list, ignores ids that
        # are already gone, and revokes for both sides.
        await session.delete_own_messages(target.bot_id, list(target.message_ids))
        logger.info(
            "removed %s owner-placed message(s) from Telegram for link %s",
            len(target.message_ids),
            link_id,
        )
        return None

    refused: list[str] = []
    for message_id in target.message_ids:
        try:
            await bot.delete(target.bot_id, target.chat_id, message_id)
        except Exception as error:
            verdict = classify_telegram(error)
            if verdict.outcome is TelegramOutcome.NO_OP:
                # Already gone — which is the state this job wanted.
                continue
            if verdict.outcome in (TelegramOutcome.NO_ANSWER, TelegramOutcome.RATE_LIMITED):
                # Unknown or "not yet". Everything already removed will be a
                # no-op next time, so the retry finishes the job rather than
                # repeating it.
                raise
            refused.append(verdict.detail)

    if not refused:
        logger.info(
            "removed %s message(s) from Telegram for link %s",
            len(target.message_ids),
            link_id,
        )
        return None

    # Telegram confirmed it will not remove them: too old, or not ours. This is
    # the one place the notice is drawn, and it is an edit — idempotent, and
    # never a second message.
    await _say_it_is_gone(bot, target)
    raise PermanentDeliveryError(
        f"Telegram refused to delete {len(refused)} of {len(target.message_ids)}"
        f" message(s) for link {link_id}: {refused[0]}"
    )


async def _say_it_is_gone(bot: BotMutations, target: MaxTarget) -> None:
    """Mark the canonical message as deleted in MAX, best effort.

    Only the head, and only for a bot-authored message. Editing every part of an
    album into the same sentence would be four copies of one fact, and a message
    the owner placed cannot be edited by the bot at all.
    """
    head = target.message_ids[0]
    try:
        await bot.edit_text(target.bot_id, target.chat_id, head, DELETED_NOTICE)
    except Exception:
        # The job is already failing with a reason the owner can read; a notice
        # that could not be drawn does not add one.
        logger.debug("could not mark %s as deleted", head, exc_info=True)


async def resolve_max_edit(
    *,
    messages: Any,
    albums: Any,
    outbox: Any,
    bot: BotMutations,
    owner: OwnerMutations | None,
    payload: dict[str, Any],
) -> int | None:
    """Carry one MAX edit into Telegram, to whichever half of the message holds it.

    The method is decided by what put the message there rather than tried and
    corrected: a message the queue delivered as media carries its text as a
    *caption*, and `editMessageText` on it is refused by Telegram — which is
    exactly what used to happen to every caption edit in the system, silently.
    """
    link_id = payload.get("link_id")
    if link_id is None:
        return None
    target = await resolve_target(
        messages=messages, albums=albums, outbox=outbox, link_id=int(link_id)
    )
    if target is None:
        logger.debug("MAX edit for a message with nothing in Telegram behind it")
        return None

    # The body depends on who placed the message, and only this side knows which.
    # A bot line carries the `Вы: ` marker; the owner's own copy must not, or an
    # edit would stamp somebody's own message as though the bot had written it.
    as_owner = target.authorship is Authorship.OWNER
    text = str(payload.get("owner_text") or "") if as_owner else ""
    entities = payload.get("owner_entities") if as_owner else None
    if not text:
        text = str(payload.get("text") or "")
        entities = payload.get("entities")
    if not text:
        return None
    message_id = target.caption_target or target.message_ids[0]

    if target.authorship is Authorship.OWNER:
        session = _require_session(owner)
        # One MTProto call edits a message's text and a media message's caption
        # alike — `messages.editMessage` carries the new body either way.
        await _mutate(
            lambda: session.edit_own_message(
                target.bot_id, message_id, text, entities=entities
            ),
            what=f"owner edit of {message_id}",
        )
        return None

    if target.is_media:
        await _mutate(
            lambda: bot.edit_caption(
                target.bot_id, target.chat_id, message_id, text, entities
            ),
            what=f"caption of {message_id}",
            otherwise=lambda: bot.edit_text(
                target.bot_id, target.chat_id, message_id, text, entities
            ),
        )
        return None

    await _mutate(
        lambda: bot.edit_text(target.bot_id, target.chat_id, message_id, text, entities),
        what=f"text of {message_id}",
        otherwise=lambda: bot.edit_caption(
            target.bot_id, target.chat_id, message_id, text, entities
        ),
    )
    return None


async def _mutate(
    call: Any,
    *,
    what: str,
    otherwise: Any = None,
) -> None:
    """Apply one idempotent change, and treat "already there" as done.

    `otherwise` is the *other* edit method, and it is reached on exactly one
    named refusal: Telegram saying this message has no text, or no caption, to
    edit. That is not a fallback in the sense the creating path forbids — an edit
    creates nothing, so switching method cannot duplicate anything — but it is
    still gated on a specific answer rather than on "something went wrong", so a
    timeout never turns into a second attempt at a different verb.
    """
    from bridge.retry.worker import PermanentDeliveryError

    try:
        await call()
    except Exception as error:
        verdict = classify_telegram(error)
        if verdict.outcome is TelegramOutcome.NO_OP:
            # "not modified", or the target is already gone. Both are the state
            # this job was asked to reach.
            logger.debug("%s was already as asked (%s)", what, verdict.detail)
            return
        if verdict.outcome is TelegramOutcome.REFUSED_EDIT_FORM and otherwise is not None:
            logger.info("%s: %s; editing the other half instead", what, verdict.detail)
            await _mutate(otherwise, what=what)
            return
        if verdict.outcome in (TelegramOutcome.NO_ANSWER, TelegramOutcome.RATE_LIMITED):
            raise
        raise PermanentDeliveryError(f"{what}: {verdict.detail}") from error


__all__ = [
    "DELETED_NOTICE",
    "Authorship",
    "BotMutations",
    "MaxTarget",
    "OwnerMutations",
    "delete_source_key",
    "edit_source_key",
    "resolve_max_delete",
    "resolve_max_edit",
    "resolve_target",
]
