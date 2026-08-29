"""Owner edit and delete, resolved against the original send. No new pipeline.

An owner edit or delete is a durable job on the *existing* outbox that depends on
the original send job, found by its source_key. This module is where that
dependency is read and the race (§4) resolved — run inline when the mutation
arrives, or by the worker later if the send is still in flight. Every path is
idempotent: the same source_key always resolves to one logical effect, across a
restart and a catch-up replay.

The states, mapped onto the real outbox states of the *predecessor send*:

* the send has reached MAX (`max_message_id` known) → apply the remote edit/delete;
* PENDING and pre-remote → coalesce the edit / cancel the delete, atomically;
* INFLIGHT, or PENDING but already claimed → wait (DeferDelivery), no retry spent;
* FAILED (confirmed, no remote effect) → edit updates the failed payload for a
  hand retry; delete archives it, nothing to remove in MAX;
* AMBIGUOUS (unknown remote outcome) → needs_attention, never a guessed mutation.

Deletes are keyed by their own durable job, which is the source of truth for a
message being terminally gone — a late edit that finds it does nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Protocol

from bridge.routing.delivery import DeferDelivery, UnconfirmedDeliveryError
from bridge.storage import OutboxState, ReactionSnapshot

logger = logging.getLogger(__name__)

#: How long a mutation waits before re-checking a send that is still in flight.
#: Short, because the send almost always resolves within one attempt; the wait
#: spends no retry budget, so a slow send simply re-checks a few more times.
WAIT_MS = 1000


def send_source_key(account_id: int, message_id: int) -> str:
    return f"tg-owner-msg:{account_id}:{message_id}"


def edit_source_key(account_id: int, message_id: int | str, pts: int, fingerprint: str) -> str:
    """One key per *edit event*: the update's own version, then what it said.

    `message_id` is what identifies the message being edited, and for an album it
    is the group rather than any one of its parts: the parts are separate
    Telegram messages and one MAX message, so an edit of the caption on the third
    photo and one on the first are edits of the same thing.

    `pts` is the identity half — Telegram's server-assigned update sequence
    number, stable across a reconnect replay and different for every edit, so
    `A → B → A → B` is four events and not three. The fingerprint stays as the
    content half: it keeps a replay of one update off a second effect even
    though the pts alone would already do that, and it makes the key readable
    as "this version of this message".
    """
    return f"tg-owner-edit:{account_id}:{message_id}:{pts}:{fingerprint}"


def delete_source_key(account_id: int, message_id: int | str) -> str:
    """One key per deleted message — or per deleted *album*.

    Telegram deletes one part of a group at a time and MAX holds the group as one
    message with no way to remove an attachment from it, so every part answers
    with the album's key and a batch of three becomes exactly one delete.
    """
    return f"tg-owner-delete:{account_id}:{message_id}"


def album_sweep_source_key(media_group_id: str) -> str:
    """One key per album, so its remaining parts are swept exactly once.

    Keyed by the group and not by the part that triggered it: every part's
    deletion asks for the same sweep, and the echo of the sweep's own deletions
    asks for it again. All of them find this job and do nothing.
    """
    return f"tg-album-sweep:{media_group_id}"


def fingerprint(text: str) -> str:
    """A stable fingerprint of the text an edit carried, for its source_key.

    The content half of the key, never the identity half — `pts` is what tells
    two edit events apart, because two edits can legitimately carry identical
    text. Content itself is never stored, only this hash.
    """
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _with_text(payload: dict[str, Any], text: str) -> dict[str, Any]:
    """The send's payload with its user text replaced — text or caption."""
    updated = dict(payload)
    if "text" in updated:
        updated["text"] = text
    elif "caption" in updated:
        updated["caption"] = text
    return updated


async def _target(
    messages: Any, account_id: int, message_id: int, link_id: Any = None
) -> Any:
    """The canonical mapping this mutation acts on.

    `link_id` is written into the payload when the router resolved it, which for
    an album is the only reliable way back: the mapping is keyed by the head's
    owner-side id, and the head may not have one yet — its echo can arrive after
    a part of it was already deleted. Falling back keeps every payload written
    before this existed working exactly as it did.
    """
    if link_id is not None:
        link = await messages.by_id(int(link_id))
        if link is not None:
            return link
    return await messages.by_owner_account_message(account_id, message_id)


async def resolve_delete(
    *, outbox: Any, messages: Any, max_sender: Any, payload: dict[str, Any]
) -> int | None:
    """Delete one owner-side message in MAX, resolving the race with its send."""
    account = int(payload["account_id"])
    message_id = int(payload["owner_message_id"])
    send_key = payload["send_source_key"]

    link = await _target(messages, account, message_id, payload.get("link_id"))
    if link is None:
        logger.debug("owner delete for an unmapped message; nothing to do")
        return None

    max_chat_id = link.max_chat_id
    send = await outbox.by_source_key(send_key)

    if link.max_message_id is not None:
        # The send reached MAX. Delete is idempotent — a retry is safe. MAX scopes
        # the deletion by authorship (own → both, contact's → owner only), so this
        # is one operation, never branched on direction.
        await max_sender.delete_messages(max_chat_id, [link.max_message_id], for_everyone=True)
        return None

    if send is None:
        return None  # the send left no job — nothing to remove
    if send.state is OutboxState.PENDING:
        if await outbox.cancel_pending(
            send_key, reason="cancelled: owner deleted before send"
        ):
            return None  # A: never sent, never will be — no remote delete
        raise DeferDelivery(WAIT_MS)  # a worker claimed it first; wait for the outcome
    if send.state is OutboxState.INFLIGHT:
        raise DeferDelivery(WAIT_MS)  # B: result unknown, do not guess
    if send.state is OutboxState.DONE and send.remote_message_id is not None:
        await max_sender.delete_messages(
            max_chat_id, [int(send.remote_message_id)], for_everyone=True
        )
        return None
    if send.state is OutboxState.FAILED:
        await outbox.archive(send.id, reason="owner deleted before delivery")
        return None  # D: confirmed unsent — no remote message to delete
    if send.state is OutboxState.AMBIGUOUS:
        raise UnconfirmedDeliveryError(
            "the original send is ambiguous; the delete is withheld for the owner"
        )
    return None  # archived / expired / done-without-id → terminal, nothing to delete


async def resolve_edit(
    *, outbox: Any, messages: Any, max_sender: Any, payload: dict[str, Any]
) -> int | None:
    """Edit one owner-side message in MAX, unless a delete has made it terminal."""
    account = int(payload["account_id"])
    message_id = int(payload["owner_message_id"])
    send_key = payload["send_source_key"]
    text = str(payload["text"])

    # `mutation_key` is what this message's own jobs are filed under — the group
    # for an album, the id for anything else — so the delete this looks for is the
    # one that would actually exist.
    terminal = delete_source_key(account, payload.get("mutation_key", message_id))
    if await outbox.by_source_key(terminal) is not None:
        # A delete was accepted for this message: it is terminally gone. A late
        # edit — even a replayed older one — must not resurrect it.
        return None

    link = await _target(messages, account, message_id, payload.get("link_id"))
    if link is None:
        return None

    if link.max_message_id is not None:
        # Idempotent: editing to the same text twice is a no-op in MAX.
        await max_sender.edit_text(link.max_chat_id, link.max_message_id, text)
        return None

    send = await outbox.by_source_key(send_key)
    if send is None:
        return None
    if send.state is OutboxState.PENDING:
        updated = _with_text(json.loads(send.payload_json), text)
        if await outbox.replace_pending_payload(send_key, updated):
            return None  # absorbed: the pending send carries the latest version
        raise DeferDelivery(WAIT_MS)  # claimed first; apply as a remote edit once DONE
    if send.state is OutboxState.INFLIGHT:
        raise DeferDelivery(WAIT_MS)
    if send.state is OutboxState.DONE and send.remote_message_id is not None:
        await max_sender.edit_text(link.max_chat_id, int(send.remote_message_id), text)
        return None
    if send.state is OutboxState.FAILED:
        # The send never reached MAX; keep the latest version for a hand retry.
        latest = _with_text(json.loads(send.payload_json), text)
        await outbox.replace_failed_payload(send_key, latest)
        return None
    if send.state is OutboxState.AMBIGUOUS:
        raise UnconfirmedDeliveryError(
            "the original send is ambiguous; the edit is withheld for the owner"
        )
    return None


class ReactionSender(Protocol):
    """What this resolver needs from MAX, spelled out so the type checker sees it.

    `Any` here is what let the worker be handed `MaxTextSender`, which carries no
    `add_reaction` at all: every reaction job failed on an `AttributeError` and
    the smoke found it instead of the type checker. Narrow on purpose — a
    resolver that only sets and clears reactions should not be able to send a
    message by accident either.
    """

    async def add_reaction(self, chat_id: int, message_id: int, emoji: str) -> None: ...

    async def remove_reaction(self, chat_id: int, message_id: int) -> None: ...


async def resolve_reaction(
    *, state: Any, snapshots: Any, max_sender: ReactionSender, payload: dict[str, Any]
) -> int | None:
    """Put the owner's reaction on the MAX message, unless it has been overtaken.

    The generation check is the point of this running here rather than inline.
    A job that has been sitting in the queue — MAX away, a retry backing off —
    carries the reaction the owner had chosen *then*, and the state carries the
    one they have chosen since. Applying the old one would put a reaction back
    that the owner has already replaced, which no amount of idempotence would
    undo.

    So the job asks the state whether it is still the current version. Equal is
    current (the state advances immediately after the enqueue); lower has been
    overtaken and is finished without touching MAX. Higher cannot happen — the
    state is written after this job exists.

    What it does when it is current is the same idempotent pair as before:
    setting replaces whatever was there, clearing takes it off, and a repeat
    after a crash means the same thing as the first attempt.
    """
    account = int(payload["account_id"])
    bot_id = int(payload["bot_id"])
    owner_message_id = int(payload["owner_message_id"])
    pts = int(payload["pts"])

    current = await state.get(account_id=account, bot_id=bot_id, message_id=owner_message_id)
    if current is not None and current.pts > pts:
        logger.info("owner reaction superseded by a newer version; not applied")
        return None

    max_chat_id = int(payload["max_chat_id"])
    max_message_id = int(payload["max_message_id"])
    emoji = payload.get("emoji")

    if emoji is None:
        await max_sender.remove_reaction(max_chat_id, max_message_id)
    else:
        await max_sender.add_reaction(max_chat_id, max_message_id, str(emoji))

    # What MAX is showing on our behalf, so its echo of our own reaction is not
    # mirrored back into Telegram as though the contact had made it.
    previous = await snapshots.get(max_chat_id, max_message_id)
    await snapshots.put(
        ReactionSnapshot(
            max_chat_id=max_chat_id,
            max_message_id=max_message_id,
            counters=previous.counters if previous else {},
            your_reaction=emoji if emoji is None else str(emoji),
        )
    )
    return None
