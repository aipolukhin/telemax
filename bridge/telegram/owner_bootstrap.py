"""Giving the puppet session a baseline for messages it never watched arrive.

Every reading of an `UpdateEditMessage` is a subtraction, so the first update on
a message the bridge has no state for has nothing to subtract from. Guessing
that it previously had no reactions would be wrong in exactly the case that
matters — a message the owner reacted to yesterday, whose first update after
this release is the *removal* of that reaction.

So the state is seeded from the messages themselves, once, before anything is
derived from an update: fetch what each mapped owner message currently is, write
down its content fingerprint and the owner's own reaction set, and produce no
effect of any kind. Nothing is sent to MAX, no job is created, and Telegram is
only read.

Two things make the seeding safe next to a live dispatcher rather than needing a
pause:

* **A baseline is a state, not a position in a stream.** An update that crosses
  the fetch either describes the same state — and subtracts to nothing — or a
  newer one, which subtracts correctly against the fetched picture. There is no
  reading of it that produces a wrong effect.
* **`seed` never overwrites.** A row written by a live update is newer than any
  fetch by construction, so the fetch steps aside instead of dragging the state
  backwards. `pts = 0` marks a row as a picture of the present rather than an
  accepted update, and every real update outranks it.

A message that cannot be fetched — deleted, or too old for the account to still
hold — is **left with no row at all**. That is the honest state: unknown. What
the dispatcher does with unknown is its own contract, and it is not "assume
there were no reactions".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from .owner_snapshot import (
    Chosen,
    chosen_of,
    content_fingerprint_of,
    encode_chosen,
    representative,
)

logger = logging.getLogger(__name__)

#: How many ids to ask for in one call. Telegram accepts a hundred, and the
#: bootstrap runs once per install rather than per message, so there is nothing
#: to win by going smaller.
BATCH = 100


class StateWriter(Protocol):
    async def seed_baseline(
        self,
        *,
        account_id: int,
        bot_id: int,
        message_id: int,
        content_fingerprint: str,
        chosen_json: str,
    ) -> bool: ...


class Mappings(Protocol):
    async def owner_bound_keys(self) -> list[tuple[int, int, int]]: ...

    async def by_owner_account_message(
        self, telegram_owner_account_id: int, telegram_owner_message_id: int
    ) -> Any: ...


class Shown(Protocol):
    """What the bridge last put on a MAX message on the owner's behalf."""

    async def get(self, max_chat_id: int, max_message_id: int) -> Any: ...


@dataclass(frozen=True, slots=True)
class BootstrapReport:
    """What one bootstrap run did. Counts only — never a message, never an id."""

    considered: int = 0
    seeded: int = 0
    already_known: int = 0
    unavailable: int = 0

    @property
    def line(self) -> str:
        return (
            f"owner message baseline: {self.seeded} seeded,"
            f" {self.already_known} already known,"
            f" {self.unavailable} unavailable of {self.considered}"
        )


async def bootstrap_owner_state(
    *,
    client: Any,
    mappings: Mappings,
    state: StateWriter,
    shown: Shown | None = None,
    account_id: int,
) -> BootstrapReport:
    """Seed a baseline for every mapped owner message this account can name."""
    from telethon.tl import types  # type: ignore[import-untyped]

    keys = [key for key in await mappings.owner_bound_keys() if key[0] == account_id]
    if not keys:
        return BootstrapReport()

    by_bot: dict[int, list[int]] = {}
    for _, bot_id, message_id in keys:
        by_bot.setdefault(bot_id, []).append(message_id)

    considered = seeded = already = unavailable = 0
    for bot_id, ids in by_bot.items():
        peer = types.PeerUser(user_id=bot_id)
        for start in range(0, len(ids), BATCH):
            chunk = ids[start : start + BATCH]
            considered += len(chunk)
            try:
                fetched = await client.get_messages(peer, ids=chunk)
            except Exception:  # noqa: BLE001 - one unreadable dialog, not a stop
                # One dialog the session cannot read right now. The rest of the
                # bootstrap is unaffected, and these ids simply stay unknown.
                logger.warning("owner baseline: a dialog could not be read")
                unavailable += len(chunk)
                continue
            for message in fetched:
                if message is None or getattr(message, "id", None) is None:
                    unavailable += 1
                    continue
                written = await state.seed_baseline(
                    account_id=account_id,
                    bot_id=bot_id,
                    message_id=int(message.id),
                    content_fingerprint=content_fingerprint_of(message),
                    chosen_json=await _reflected(
                        mappings, shown, account_id, int(message.id), message
                    ),
                )
                if written:
                    seeded += 1
                else:
                    already += 1

    report = BootstrapReport(
        considered=considered,
        seeded=seeded,
        already_known=already,
        unavailable=unavailable,
    )
    logger.info("%s", report.line)
    return report


async def _reflected(
    mappings: Mappings,
    shown: Shown | None,
    account_id: int,
    message_id: int,
    message: Any,
) -> str:
    """The reaction baseline: what MAX has been told, never what Telegram says.

    The baseline's whole job is to be the thing a later update subtracts from,
    so it must describe a state MAX is *already in*. Recording the fetched
    Telegram set as though it had been applied is what loses an owner action:

        MAX is showing 👍
        the owner switches to ❤ while the fetch is running
        the fetch returns [❤] and the baseline records [❤]
        the update for that switch arrives, [❤] → [❤] is empty
        MAX stays on 👍

    Measured against this function before it compared representatives — and it
    failed the same way for [👍, ❤] against an applied 👍.

    So the two are compared as *projections*, because that is what MAX can hold:

    * the fetched representative maps to what MAX already shows — the two agree,
      and the whole fetched ordered set is recorded. The set matters and not just
      its representative: with [👍, ❤] applied as ❤, taking 👍 off later must
      read as no change;
    * they differ — then nothing about the fetched state has been applied, and
      recording it would swallow the transition. What is recorded instead is a
      synthetic set describing only what MAX is showing, so the update that
      follows derives exactly the transition that is missing.

    A custom emoji has no MAX projection at all, so it never agrees, and the
    synthetic baseline is used — which is right: the update carrying it will
    clear MAX, per the custom-emoji policy.

    Nothing here applies a historical effect. A message the owner reacted to
    long ago, whose reaction MAX was never told about, stays that way until they
    touch it again.
    """
    from bridge.reactions.mapping import to_max

    fetched = chosen_of(message)
    if shown is None:
        # No reader (the setup CLI, tests with no queue). Nothing can be
        # compared, so the fetched state is recorded as before.
        return encode_chosen(fetched)

    link = await mappings.by_owner_account_message(account_id, message_id)
    if link is None or link.max_message_id is None:
        return encode_chosen(fetched)

    snapshot = await shown.get(link.max_chat_id, link.max_message_id)
    applied = getattr(snapshot, "your_reaction", None) if snapshot is not None else None

    top = representative(fetched)
    projected = to_max(top.value) if top is not None and top.kind == "e" else None
    # Both sides normalised the same way. `❤` and `❤️` are one reaction to MAX
    # and two strings here, and a comparison that missed that would fall to the
    # narrow branch for no reason.
    already = to_max(str(applied)) if applied else None

    if projected == already:
        return encode_chosen(fetched)
    return encode_chosen(() if applied is None else (Chosen("e", str(applied)),))
