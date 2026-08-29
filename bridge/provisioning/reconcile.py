"""Picking up an interrupted provisioning run, without being asked to.

The journal recorded every step and nothing ever read it at start-up. A process
that died between creating a bot and writing its token down left the bot in
Telegram, the entry at `bot_created`, and no path back that did not begin with
the owner opening `/dialogs` and pressing a button again — for a failure they
were never told about.

This reads the journal on the way up and finishes what can be finished. Two
rules shape all of it:

* **an attempt is resumed; it is never restarted.** Everything the batch does
  is idempotent — `getManagedBotToken` returns the token of a bot that exists,
  `start_worker` replaces its own worker — so continuing is safe in a way
  starting over would not be.
* **nothing irreversible happens without the owner.** A `pending` attempt has no
  remote effect behind it and an `awaiting_confirmation` one may have none
  either; creating a bot for either would be this process deciding to spend a
  slot on a decision nobody made. Those are reported and left.

It is not a supervisor. It runs once, inside the service's own start-up, after
the control plane is up and off the critical path of the bridges that already
work.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .batch import ProvisioningBatch
from .coordinator import ContactKey, ProvisioningCoordinator
from .journal import TERMINAL, ItemState, JournalEntry, ProvisioningJournal
from .provisioner import UsernameState

logger = logging.getLogger(__name__)

#: How many interrupted attempts to walk at once. Small: each one is several
#: Telegram round trips, and this is competing with four bridges coming up.
DEFAULT_CONCURRENCY = 2

#: An attempt older than this and still unfinished is worth an incident. Long
#: enough that a confirmation the owner is walking to another room for does not
#: raise one.
STUCK_AFTER_SECONDS = 3600


class Verdict(StrEnum):
    """What start-up decided about one interrupted attempt."""

    #: Picked up and walked to the end, or to its next honest failure.
    RESUMED = "resumed"
    #: A remote effect nobody has authorised. Left alone, and said out loud.
    AWAITING_OWNER = "awaiting_owner"
    #: The username belongs to somebody else. Nothing to do, ever.
    COLLISION = "collision"
    #: Telegram or MAX could not be asked. Next start will try again.
    UNAVAILABLE = "unavailable"
    #: Finished, permanently failed, or abandoned. Not touched.
    SETTLED = "settled"


@dataclass(frozen=True, slots=True)
class Outcome:
    """One attempt, and what became of it. Nothing private in any field."""

    max_chat_id: int
    expected_username: str
    verdict: Verdict
    state: str
    telegram_bot_id: int | None = None
    detail: str = ""


@dataclass(slots=True)
class ReconcileReport:
    """What start-up found, for `/status`, for incidents, and for the log."""

    outcomes: list[Outcome] = field(default_factory=list)
    #: Bridge rows that exist and are not serving. A row in `provisioning` is a
    #: bot that was brought up far enough to be written down and not far enough
    #: to be trusted.
    unfinished_rows: list[str] = field(default_factory=list)

    def of(self, verdict: Verdict) -> list[Outcome]:
        return [item for item in self.outcomes if item.verdict is verdict]

    @property
    def resumed(self) -> int:
        return len(self.of(Verdict.RESUMED))

    @property
    def awaiting_owner(self) -> int:
        return len(self.of(Verdict.AWAITING_OWNER))

    @property
    def unresolved(self) -> list[Outcome]:
        return [
            item
            for item in self.outcomes
            if item.verdict in {Verdict.AWAITING_OWNER, Verdict.COLLISION, Verdict.UNAVAILABLE}
        ]


#: Raised once per stuck attempt, keyed on the chat. Takes the sanitised stage.
Incident = Callable[[Outcome], Awaitable[None]]


class ProvisioningReconciler:
    """Reads the journal at start-up and finishes what may be finished."""

    def __init__(
        self,
        *,
        journal: ProvisioningJournal,
        provisioner: Any,
        gateway: Any,
        coordinator: ProvisioningCoordinator,
        display_name_of: Callable[[JournalEntry], str],
        own_user_id: int | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
        incident: Incident | None = None,
    ) -> None:
        self._journal = journal
        self._provisioner = provisioner
        self._gateway = gateway
        self._coordinator = coordinator
        self._display_name_of = display_name_of
        self._own_user_id = own_user_id
        self._concurrency = max(1, concurrency)
        self._incident = incident

    async def run(self) -> ReconcileReport:
        report = ReconcileReport()
        unfinished = [
            entry for entry in self._journal.refresh() if entry.state not in TERMINAL
        ]
        if not unfinished:
            return report

        logger.info("resuming %s unfinished provisioning attempt(s)", len(unfinished))
        limit = asyncio.Semaphore(self._concurrency)

        async def one(entry: JournalEntry) -> Outcome:
            async with limit:
                return await self._reconcile(entry)

        settled = await asyncio.gather(
            *(one(entry) for entry in unfinished), return_exceptions=True
        )
        for entry, outcome in zip(unfinished, settled, strict=True):
            if isinstance(outcome, BaseException):
                # One attempt's failure never stops the others, and never stops
                # the service coming up.
                logger.warning(
                    "could not reconcile MAX chat %s: %s",
                    entry.max_chat_id,
                    type(outcome).__name__,
                )
                report.outcomes.append(
                    Outcome(
                        max_chat_id=entry.max_chat_id,
                        expected_username=entry.expected_username,
                        verdict=Verdict.UNAVAILABLE,
                        state=entry.state.value,
                        telegram_bot_id=entry.telegram_bot_id,
                        detail=type(outcome).__name__,
                    )
                )
            else:
                report.outcomes.append(outcome)

        await self._announce(report)
        return report

    # ------------------------------------------------------------------ one item

    async def _reconcile(self, entry: JournalEntry) -> Outcome:
        """Decide by durable evidence, then by what Telegram says. Never by hope."""
        if entry.state in TERMINAL:
            return self._outcome(entry, Verdict.SETTLED)

        try:
            state = await self._provisioner.check_username(entry.expected_username)
        except Exception as error:  # noqa: BLE001 - unreachable is not a verdict
            return self._outcome(entry, Verdict.UNAVAILABLE, detail=type(error).__name__)

        if state in {UsernameState.FOREIGN, UsernameState.UNMANAGEABLE}:
            foreign = state is UsernameState.FOREIGN
            self._journal.note(
                entry.max_chat_id,
                state=ItemState.FAILED_PERMANENT,
                error=(
                    "детерминированный username занят другим Telegram-аккаунтом"
                    if foreign
                    else "бот есть, но страж им не управляет — создан вручную"
                ),
                failure="username_occupied" if foreign else "bot_not_manageable",
            )
            return self._outcome(entry, Verdict.COLLISION)

        if state is not UsernameState.OWNED:
            # The bot does not exist. Whatever the journal remembers about an
            # earlier attempt, finishing this one would mean *creating* it — and
            # a restart that spends a slot on a decision the owner never made is
            # the one thing reconciliation must never do.
            return self._outcome(entry, Verdict.AWAITING_OWNER)

        return await self._resume(entry)

    async def _resume(self, entry: JournalEntry) -> Outcome:
        """Walk the existing batch over one contact. Everything in it is idempotent."""
        key = (
            ContactKey.for_user(entry.max_peer_id)
            if entry.max_peer_id is not None
            else ContactKey.for_chat(entry.max_chat_id, own_user_id=self._own_user_id)
        )
        async with self._coordinator.claim(key, doing="resuming after a restart") as mine:
            if not mine:
                return self._outcome(entry, Verdict.SETTLED, detail="already running")
            batch = ProvisioningBatch(
                provisioner=self._provisioner,
                gateway=self._gateway,
                journal=self._journal,
                display_name_of=self._display_name_of,
                chat_ids=[entry.max_chat_id],
                coordinator=self._coordinator,
                own_user_id=self._own_user_id,
                doing="resuming after a restart",
            )
            await batch.run()
        settled = self._journal.get(entry.max_chat_id) or entry
        verdict = Verdict.RESUMED if settled.finished else Verdict.AWAITING_OWNER
        if settled.state is ItemState.FAILED_PERMANENT:
            verdict = Verdict.COLLISION
        return self._outcome(settled, verdict)

    def _outcome(self, entry: JournalEntry, verdict: Verdict, *, detail: str = "") -> Outcome:
        return Outcome(
            max_chat_id=entry.max_chat_id,
            expected_username=entry.expected_username,
            verdict=verdict,
            state=entry.state.value,
            telegram_bot_id=entry.telegram_bot_id,
            detail=detail,
        )

    async def _announce(self, report: ReconcileReport) -> None:
        if self._incident is None:
            return
        for outcome in report.unresolved:
            try:
                await self._incident(outcome)
            except Exception:
                logger.debug("could not raise a provisioning incident", exc_info=True)
