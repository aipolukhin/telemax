"""The outbox worker: one per bridge, so a stuck queue stays local.

Failures are sorted into three buckets, and getting that wrong is what makes a
bridge either lose messages or spin forever:

* **retryable** — networks, timeouts, `service.unavailable`, a Telegram
  `retry_after`. Wait and try again.
* **permanent** — the content is wrong (too long, bad file, unknown chat) or the
  token is gone. Retrying cannot help; mark it failed and surface it.
* **rate limited** — Telegram told us exactly how long to wait, so wait exactly
  that long instead of applying our own guess.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)

from bridge.routing.delivery import SendingHook
from bridge.routing.settlement import Settlement, Verdict, settle
from bridge.storage import BridgeStateRepository, Direction, OutboxItem, OutboxRepository

from .backoff import DEFAULT_POLICY, MS_PER_SECOND, BackoffPolicy

logger = logging.getLogger(__name__)

# How long an idle worker sleeps before looking again. Enqueue wakes it up, so
# this only bounds the delay for items scheduled in the future.
IDLE_POLL_SECONDS = 1.0

#: How long a job waits when something older in its direction is still going out.
#: Short: the thing it is waiting for is one send, and the wait costs no attempt.
ORDER_WAIT_MS = 200

#: Returns the remote message id, so a delivery can be proven rather than
#: assumed. Raising `UnconfirmedDeliveryError` means the send went out and
#: the answer did not come back. The last argument is called by the sender
#: immediately before the remote request — see `SendingHook`.
Delivery = Callable[
    [str, Direction, dict[str, Any], SendingHook], Awaitable[int | None]
]


class PermanentDeliveryError(Exception):
    """The item can never be delivered. Do not retry it."""


def classify(error: BaseException) -> tuple[bool, float | None]:
    """Return (retryable, forced delay in seconds).

    A forced delay means the server named its own wait — Telegram's
    `retry_after` — and we respect it rather than guessing.
    """
    if isinstance(error, TelegramRetryAfter):
        return True, float(error.retry_after)
    if isinstance(error, PermanentDeliveryError):
        return False, None
    if isinstance(error, TelegramUnauthorizedError | TelegramForbiddenError):
        # A revoked token or a blocked bot will not fix itself on retry.
        return False, None
    if isinstance(error, TelegramBadRequest):
        # Bad content: the same request will fail the same way forever.
        return False, None
    if isinstance(error, TelegramNetworkError | TimeoutError | OSError | ConnectionError):
        return True, None
    # Unknown failures are treated as retryable: losing a message is worse than
    # trying it again a bounded number of times.
    return True, None


class OutboxWorker:
    """Drains one bridge's queue."""

    def __init__(
        self,
        *,
        bridge_name: str,
        outbox: OutboxRepository,
        state: BridgeStateRepository,
        deliver: Delivery,
        policy: BackoffPolicy = DEFAULT_POLICY,
        idle_poll_seconds: float = IDLE_POLL_SECONDS,
    ) -> None:
        self.bridge_name = bridge_name
        self._outbox = outbox
        self._state = state
        self._deliver = deliver
        self._policy = policy
        self._idle = idle_poll_seconds
        self._wakeup = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.is_running:
            return
        self._task = asyncio.create_task(self._loop(), name=f"outbox-{self.bridge_name}")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def wake(self) -> None:
        """Something was enqueued; look now instead of waiting out the idle sleep."""
        self._wakeup.set()

    async def _loop(self) -> None:
        while True:
            try:
                worked = await self.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The queue itself misbehaving must not kill the worker.
                logger.exception("bridge %s: outbox loop error", self.bridge_name)
                worked = False

            if worked:
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wakeup.wait(), self._idle)
            self._wakeup.clear()

    async def drain_once(self, *, limit: int = 1) -> bool:
        """Deliver the due head of the queue. True when something was attempted.

        One at a time by default: this is what keeps a contact's messages in the
        order they were written. The loop above calls it again immediately while
        there is work, so throughput does not suffer for it.
        """
        items = await self._outbox.claim_due(self.bridge_name, limit=limit)
        for item in items:
            await self._attempt(item)
        return bool(items)

    async def _attempt(self, item: OutboxItem) -> None:
        if await self._outbox.older_undelivered(
            self.bridge_name, direction=item.direction, before_id=item.id
        ):
            # Something older is still on its way out — the inline path holding a
            # job it claimed, most often. Claiming the head of the *pending* queue
            # is not the same as being first in line, and sending now is how the
            # two senders put a bridge's messages in the wrong order. Back to
            # PENDING without spending an attempt: waiting is not failing.
            await self._outbox.defer(item.id, delay_ms=ORDER_WAIT_MS)
            return
        marked = False
        try:
            payload = json.loads(item.payload_json)

            async def sending() -> None:
                # Fired by the sender just before the remote request, so a crash
                # while preparing stays a retry rather than a question. `marked`
                # is the same fact `send_started_at` records, held in memory
                # because this attempt is still running.
                nonlocal marked
                marked = True
                await self._outbox.mark_sending(item.id)

            remote_id = await self._deliver(item.kind, item.direction, payload, sending)
        except (Exception, asyncio.CancelledError) as error:
            # Deliberately not `BaseException`: `KeyboardInterrupt` and
            # `SystemExit` are the process being killed, and the row must stay
            # INFLIGHT so lease recovery judges it from `send_started_at`.
            verdict = settle(item.kind, error, remote_marked=marked)
            if verdict.verdict is Verdict.AMBIGUOUS:
                # The send may have landed and the answer never came. Retrying is
                # how a duplicate reaches a real person, so this stops here and
                # the owner decides (ADR 0002). A shutdown that lands here is the
                # window `mark_retry(…, "cancelled")` used to hide: it overwrote
                # the one mark that said the call had begun.
                logger.error(
                    "bridge %s: %s went out unconfirmed, leaving it for the owner (%s)",
                    self.bridge_name,
                    item.kind,
                    verdict.detail,
                )
                await self._outbox.mark_ambiguous(item.id, error=verdict.detail)
                await self._state.note_error(self.bridge_name, verdict.detail)
                if isinstance(error, asyncio.CancelledError):
                    raise
                return
            if verdict.verdict is Verdict.DEFER:
                # Waiting on a predecessor send. Not a failure: no attempt spent,
                # no error recorded — only the re-check time moves, so a slow send
                # never walks its edit or delete toward FAILED.
                await self._outbox.defer(item.id, delay_ms=verdict.delay_ms)
                return
            if isinstance(error, asyncio.CancelledError):
                # Shutdown before the remote call, or an idempotent mutation:
                # put it back so the next start picks it up.
                await self._outbox.mark_retry(item.id, delay_ms=0, error="cancelled")
                raise
            await self._handle_failure(item, error, verdict)
            return

        # The remote id travels with the success. Dropping it here left jobs
        # marked delivered with nothing to show for it, which is the same lie
        # the inline path was fixed for.
        await self._outbox.mark_done(item.id, remote_message_id=remote_id)
        await self._state.note_delivery(self.bridge_name)

    async def _handle_failure(
        self, item: OutboxItem, error: BaseException, verdict: Settlement
    ) -> None:
        """One failed attempt, priced against the job's budget and its clock.

        The verdict decides both, and it is the same verdict the inline path
        acted on — the forced-delay contract lives in one place now rather than
        being re-derived here from the exception.
        """
        retryable, forced_delay = classify(error)
        if verdict.verdict is Verdict.PERMANENT:
            # The settlement policy already proved this one undeliverable; the
            # generic classification below is for everything it had no opinion on.
            retryable = False
        # A rate limit is the server saying "not yet". The clock moves and the
        # budget does not, so a bridge that spends a minute inside a limit still
        # has all twelve of its tries when it comes out.
        attempts = item.attempts + 1 if verdict.costs_attempt else item.attempts
        message = f"{type(error).__name__}: {error}"

        if not retryable or (verdict.costs_attempt and self._policy.exhausted(attempts)):
            reason = "permanent" if not retryable else f"gave up after {attempts} attempts"
            logger.error("bridge %s: %s — %s", self.bridge_name, reason, message)
            await self._outbox.mark_failed(item.id, error=f"{reason}: {message}")
            await self._state.note_error(self.bridge_name, message)
            return

        if verdict.delay_ms:
            delay_ms = verdict.delay_ms
        elif forced_delay is not None:
            delay_ms = int(forced_delay * MS_PER_SECOND)
        else:
            delay_ms = self._policy.delay_ms(attempts)
        logger.warning(
            "bridge %s: attempt %s failed (%s), retrying in %sms",
            self.bridge_name,
            attempts,
            message,
            delay_ms,
        )
        await self._outbox.mark_retry(
            item.id, delay_ms=delay_ms, error=message, costs_attempt=verdict.costs_attempt
        )
        await self._state.note_error(self.bridge_name, message)


class WorkerPool:
    """One worker per bridge, plus the shared limit on concurrent media work.

    Queues are isolated on purpose: a 20 MB upload for one contact must not
    delay a one-line message to another. The semaphore is the one thing they do
    share, because bandwidth and temp space are finite.
    """

    def __init__(self, *, media_concurrency: int = 2) -> None:
        self._workers: dict[str, OutboxWorker] = {}
        self.media_slots = asyncio.Semaphore(media_concurrency)

    def add(self, worker: OutboxWorker, *, start: bool = True) -> None:
        self._workers[worker.bridge_name] = worker
        if start:
            worker.start()

    async def remove(self, bridge_name: str) -> None:
        worker = self._workers.pop(bridge_name, None)
        if worker is not None:
            await worker.stop()

    def wake(self, bridge_name: str) -> None:
        worker = self._workers.get(bridge_name)
        if worker is not None:
            worker.wake()

    async def close(self) -> None:
        for name in list(self._workers):
            await self.remove(name)

    @property
    def workers(self) -> tuple[OutboxWorker, ...]:
        return tuple(self._workers.values())
