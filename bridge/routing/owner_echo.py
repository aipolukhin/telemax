"""Binding the owner's own id for a message the bridge sent them. No new store.

A contact bot delivers a MAX message into the owner's Telegram chat over the Bot
API. The owner's MTProto session sees that very message arrive — but with the
*owner's* id for it, a number that appears nowhere in the Bot API result. Until
the two are joined, a reply to it resolves to nothing and a deletion of it cannot
find the MAX message behind it.

The join is written into the same `message_map` row the delivery already claimed,
and it rests on two things and no others:

* **What was sent.** `echo_fingerprint` is recorded with the claim, before the
  first Telegram call, so by the time an echo can exist the row is already on disk
  with something to match it against — whichever side of the race lands first, and
  across any restart in between.
* **The order it was sent in.** Identical messages produce identical fingerprints,
  so equality cannot tell two of them apart. Order can, and the order is real:
  MAX→TG sends for one bridge are serialised through one queue, so the row ids
  *are* the sequence. An echo is therefore only ever tested against the **oldest**
  unbound row for that contact bot — never against a newer one that looks right.

A head that does not match is not stepped around. The echo waits, and if it is
still blocked when its own patience runs out, the head is given up on **loudly**
— one incident, the row marked unbindable so the queue can move again — and only
then does the echo bind to what is by that point genuinely its own row.

Nothing here touches the delivery: a binding that fails leaves a delivered message
delivered, DONE and untouched.
"""

from __future__ import annotations

import logging
from typing import Any

from bridge.routing.delivery import DeferDelivery, UnconfirmedDeliveryError
from bridge.storage.database import now_ms

logger = logging.getLogger(__name__)

#: How long an echo waits before looking at the head of the queue again. The
#: thing it waits for is one send, so this is short — and the wait costs no
#: attempt, so a slow one never walks the binding toward failure.
HEAD_WAIT_MS = 500

#: How long an echo keeps waiting for a head that never resolves before giving up
#: on it. Long enough to outlast a send in flight and a reconnect's catch-up
#: burst, short enough that a genuinely lost echo does not block a bridge's
#: bindings for the rest of the day.
GIVE_UP_MS = 30_000


def echo_source_key(account_id: int, owner_message_id: int) -> str:
    """One key per owner-side message, so a replayed update binds once."""
    return f"tg-owner-echo:{account_id}:{owner_message_id}"


def album_echo_source_key(namespace: str) -> str:
    """One key per owner-side *album*, so a replayed group binds once.

    Keyed by the group rather than by any of its ids: the parts arrive
    separately and a catch-up can replay them in any order, so a key built from
    whichever one closed the group would let the same album be bound twice.
    """
    return f"tg-owner-echo:{namespace}"


def _diagnostic(bridge_name: str, *, expected: str | None, observed: str, head_id: int) -> str:
    """What an incident may say about a binding. Structure only, never content.

    The fingerprints are already hashes and the ids are ours; nothing here can
    carry a word of the message, a caption, a filename or a number belonging to a
    person.
    """
    return (
        f"owner echo binding on {bridge_name}: head #{head_id}"
        f" expected {expected or 'none'}, observed {observed}"
    )


async def resolve_echo(*, messages: Any, state: Any, payload: dict[str, Any]) -> int | None:
    """Bind one owner-side echo, or refuse to and say why. Never guesses."""
    bot_id = int(payload["bot_id"])
    account_id = int(payload["account_id"])
    owner_message_id = int(payload["owner_message_id"])
    observed = str(payload["fingerprint"])
    bridge_name = str(payload["bridge_name"])
    first_seen = int(payload["first_seen_ms"])

    if await messages.by_owner_account_message(account_id, owner_message_id) is not None:
        # This update has already been bound — a catch-up replaying it, or this
        # job running twice. Idempotent success, not a second mapping.
        return None

    head = await messages.oldest_unbound_echo(bot_id)
    if head is None:
        # Nothing is waiting for an owner-side id, so this is not a message the
        # bridge sent: a status line, a refusal notice, something the guardian
        # posted. Ignored in silence — an incident per status tick would be noise.
        logger.debug("owner echo with nothing waiting to be bound; ignored")
        return None

    if head.echo_fingerprint == observed:
        if await messages.bind_owner_message(
            head.id, account_id=account_id, owner_message_id=owner_message_id
        ):
            return None
        # The row took a different owner-side id between the read and the write.
        # Not overwritten: the other binding may well be the right one, and this
        # is exactly the kind of thing that must be looked at rather than decided.
        raise UnconfirmedDeliveryError(
            _diagnostic(bridge_name, expected=head.echo_fingerprint, observed=observed,
                        head_id=head.id)
            + " — the row was bound to another owner message"
        )

    if now_ms() - first_seen < GIVE_UP_MS:
        # The head is someone else's echo that has not arrived yet. Wait for it
        # rather than binding around it: skipping ahead is how an answer ends up
        # attached to the wrong question when two messages read alike.
        raise DeferDelivery(HEAD_WAIT_MS)

    # The head never got its echo. Say so once, mark it unbindable so it stops
    # holding up everything behind it, and try again — by now the head should be
    # this echo's own row.
    await state.note_error(
        bridge_name,
        _diagnostic(bridge_name, expected=head.echo_fingerprint, observed=observed,
                    head_id=head.id)
        + " — no echo arrived for it; owner-side id left unbound",
    )
    await messages.stop_binding(head.id)

    following = await messages.oldest_unbound_echo(bot_id)
    if following is not None and following.echo_fingerprint == observed:
        await messages.bind_owner_message(
            following.id, account_id=account_id, owner_message_id=owner_message_id
        )
        return None
    # The only remote effect already happened in the MAX→Telegram delivery;
    # this job merely enriches its mapping. Once the stale head has been
    # retired, an echo matching no remaining row is a bot-side representation
    # (for example a permanent failure notice), not an unconfirmed send. Making
    # the idempotent binding job AMBIGUOUS creates a false delivery incident and
    # gives the owner nothing actionable to decide.
    logger.info(
        "owner echo matched no remaining bindable message after head %s; ignored",
        head.id,
    )
    return None


def _album_diagnostic(
    bridge_name: str, *, expected: int, observed: int, link_id: int | None
) -> str:
    """What an incident may say about an album binding. Counts and ids, never a
    word of what was in it — the same line every alert in this project draws."""
    return (
        f"owner album echo binding on {bridge_name}: head #{link_id or 0}"
        f" expected {expected} part(s), observed {observed}"
    )


async def resolve_album_echo(
    *, messages: Any, albums: Any, state: Any, payload: dict[str, Any]
) -> int | None:
    """Bind a whole album echo to the aliases of one delivered message.

    The single-message rule, applied to a group. An echo is tested against the
    **oldest** album still waiting for an owner-side id on this contact bot, and
    a head that does not match is waited for rather than stepped around: two
    albums of the same three photos hash the same three ways, so equality cannot
    separate them and only order can. Within the group the parts go by position
    — ascending owner-side id against ascending `part_index` — which is what the
    live probe showed is the one ordering both sides always agree on, and the
    only thing that can tell two byte-identical photos apart.

    Nothing here delivers anything. A binding that fails leaves a delivered
    album delivered, DONE and untouched; what it costs is an owner-side identity,
    and the incident says so.
    """
    bot_id = int(payload["bot_id"])
    account_id = int(payload["account_id"])
    bridge_name = str(payload["bridge_name"])
    namespace = str(payload["namespace"])
    first_seen = int(payload["first_seen_ms"])
    observed: list[dict[str, Any]] = list(payload["parts"])

    # The buffer has done its job: this payload is the durable record now, and a
    # buffered row holding an owner-side id would be refused the moment the alias
    # that must hold it is written. Repeated on every attempt on purpose — a
    # replay that arrived while this was deferred leaves rows behind too.
    await albums.clear(namespace)

    if not observed:
        return None
    if await albums.by_owner_message(account_id, int(observed[0]["owner_message_id"])):
        # Already bound: a catch-up replaying the group, or this job running
        # twice. Idempotent success, not a second binding.
        return None

    expected = await albums.oldest_unbound_album(bot_id)
    if not expected:
        # Nothing is waiting for an owner-side id, so this is not an album the
        # bridge sent. Ignored in silence rather than made into an incident.
        logger.debug("album echo with nothing waiting to be bound; ignored")
        return None

    if _matches(expected, observed):
        await _attach(albums, expected, observed, account_id=account_id)
        return None

    if now_ms() - first_seen < GIVE_UP_MS:
        # The head is some other album's echo that has not arrived yet. Waiting
        # costs no attempt; skipping ahead is how one album's parts end up
        # wearing another album's identity.
        raise DeferDelivery(HEAD_WAIT_MS)

    # The head never got its echo. Say so once, mark its parts unbindable so they
    # stop holding up everything behind them, and try again — by now the head
    # should be this echo's own album.
    await state.note_error(
        bridge_name,
        _album_diagnostic(
            bridge_name,
            expected=len(expected),
            observed=len(observed),
            link_id=expected[0].link_id,
        )
        + " — no echo arrived for it; owner-side ids left unbound",
    )
    for part in expected:
        await albums.stop_binding(part.id)

    following = await albums.oldest_unbound_album(bot_id)
    if following and _matches(following, observed):
        await _attach(albums, following, observed, account_id=account_id)
        return None
    # Unlike the one-message fallback notices handled by ``resolve_echo``, the
    # bridge never emits an untracked Bot API album.  If the observed group does
    # not match either the retired head or the next queued album, its identity
    # is genuinely unknown and must stay visible to the owner rather than being
    # silently accepted.
    raise UnconfirmedDeliveryError(
        _album_diagnostic(
            bridge_name,
            expected=len(following or expected),
            observed=len(observed),
            link_id=(following or expected)[0].link_id,
        )
        + " — echo matched no delivered album"
    )


def _matches(expected: list[Any], observed: list[dict[str, Any]]) -> bool:
    """Same length, same structure, position by position.

    Structure confirms the candidate is the album that was sent; it is never what
    tells two of them apart. A part whose fingerprint has been cleared is not a
    candidate at all, so a half-given-up group can never half-match.
    """
    if len(expected) != len(observed):
        return False
    return all(
        part.part_fingerprint is not None
        and part.part_fingerprint == echo["fingerprint"]
        and part.part_index == echo["part_index"]
        for part, echo in zip(expected, observed, strict=True)
    )


async def _attach(
    albums: Any, expected: list[Any], observed: list[dict[str, Any]], *, account_id: int
) -> None:
    """Write the owner's own id onto each alias. Conditional, never an overwrite.

    A refusal is a conflict, not a race to settle: the row either holds a
    different owner-side message, or that message already belongs to another
    part. Both mean the order this matched in was wrong, and both are for the
    owner to see rather than for this to decide.
    """
    for part, echo in zip(expected, observed, strict=True):
        if not await albums.attach_owner_message(
            part.id,
            account_id=account_id,
            owner_message_id=int(echo["owner_message_id"]),
        ):
            raise UnconfirmedDeliveryError(
                f"owner album echo: part {part.part_index} of #{part.link_id}"
                " is already bound to another owner message"
            )
