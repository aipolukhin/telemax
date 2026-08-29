"""One polling loop per bot, and the durable intake underneath it.

aiogram can poll several bots from one `start_polling` call, but that call takes
a lock on the dispatcher and takes the whole set of bots up front. This project
needs the opposite of both: a bot that dies must not disturb its siblings, and
dynamic provisioning adds bots to a running process. So each bot gets its own task that fetches
updates and hands them to the shared dispatcher — the same thing aiogram does
internally, minus the shared lifecycle.

**The order of two lines used to decide whether messages could vanish.** It was:

    for update in updates:
        self._offset = update.update_id + 1     # acknowledged to Telegram
        asyncio.create_task(self._feed(update))  # ...and only now processed

Moving the offset is how a bot tells Telegram "I have this, stop sending it".
Doing that before the update is written down anywhere meant the only copy lived
in a task nobody held a reference to — free for the garbage collector to drop,
free for a SIGTERM to cut in half, and gone for good either way, because
Telegram would never send it again. Two of the owner's messages could also
overtake each other, since the tasks ran concurrently.

Now an update is stored in SQLite first, and the offset moves only after the
store commits. Processing is a separate, sequential pass over that table, so a
restart resumes it and a contact's messages keep their order.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramUnauthorizedError
from aiogram.types import Update

logger = logging.getLogger(__name__)

# Long-polling window. Shorter means more empty round trips; longer delays a
# clean shutdown, because a pending getUpdates has to be cancelled.
POLL_TIMEOUT_SECONDS = 25

INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 60.0

# How long the processing pass sleeps when the inbox is empty.
IDLE_SECONDS = 0.5

# How long `stop()` waits for the update in flight before leaving the rest on
# disk. Nothing is lost when it runs out — the row is still there.
DRAIN_TIMEOUT_SECONDS = 20.0


def _unset(value: Any) -> None:
    """aiogram's `Default` marker, as JSON: `null`.

    `Default` means "not set, use the bot's own default". It is a marker rather
    than a value and pydantic has no JSON form for it, so an update carrying one
    raised on the way to disk.

    Narrow on purpose. A blanket fallback would quietly turn *any* type this
    module has not thought about into null, which is how a field stops arriving
    and nobody notices; anything else still raises and still stops the offset.
    """
    from aiogram.client.default import Default

    if isinstance(value, Default):
        return None
    raise TypeError(f"an update carries a {type(value).__name__} that cannot be stored")


def storable_update(update: Update) -> dict[str, Any]:
    """One update as JSON, ready for the durable inbox.

    Telegram fills `link_preview_options` on any message carrying a URL, and
    aiogram's model defaults its `is_disabled` to a `Default` marker — so the
    dump raised `PydanticSerializationError` and the row was never written. The
    offset then stayed put by design, Telegram re-sent the same batch, and the
    bridge polled that one update in a loop for as long as it was there.

    `null` round-trips back through `model_validate` as
    "unset", which is exactly what the marker meant.
    """
    return update.model_dump(mode="json", exclude_none=True, fallback=_unset)


@dataclass(frozen=True, slots=True)
class BotIdentity:
    bot_id: int
    username: str | None


class UpdateInbox(Protocol):
    """The durable side of intake. `TelegramInboxRepository` implements it."""

    async def store(
        self,
        *,
        bot_id: int,
        update_id: int,
        payload: dict[str, Any],
        bridge_name: str | None = None,
    ) -> int | None: ...

    async def remember_offset(self, bot_id: int, next_offset: int) -> None: ...

    async def offset(self, bot_id: int) -> int | None: ...

    async def claim_open(
        self, *, limit: int = 20, lease_ms: int = ..., bot_id: int | None = None
    ) -> list[Any]: ...

    async def mark_done(self, inbox_id: int) -> None: ...

    async def release(self, inbox_id: int, *, error: str) -> None: ...

    async def mark_failed(self, inbox_id: int, *, error: str) -> None: ...


#: After this many failed passes an update is parked rather than retried for
#: ever. It stays in the table, visible, instead of spinning.
MAX_INTAKE_ATTEMPTS = 5


class BotRunner:
    """Owns one `Bot`, its polling task, its intake and its update offset."""

    def __init__(
        self,
        *,
        bridge_name: str,
        bot: Bot,
        dispatcher: Dispatcher,
        allowed_updates: list[str],
        inbox: UpdateInbox | None = None,
        bot_id: int | None = None,
    ) -> None:
        self.bridge_name = bridge_name
        self.bot = bot
        self._dispatcher = dispatcher
        self._allowed_updates = allowed_updates
        # Without an inbox the runner dispatches straight through, which is what
        # the guardian's own bot does: its updates are commands, not messages
        # being carried anywhere, so losing one costs a re-tap and nothing else.
        self._inbox = inbox
        self._bot_id = bot_id
        self._task: asyncio.Task[None] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._offset: int | None = None
        self._stopping = asyncio.Event()
        self._work = asyncio.Event()
        # Live handler tasks, so shutdown can wait for them instead of
        # discovering they were cancelled halfway through a send.
        self._in_flight: set[asyncio.Task[None]] = set()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.is_running:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._poll(), name=f"tg-poll-{self.bridge_name}")
        if self._inbox is not None:
            self._worker = asyncio.create_task(
                self._process_inbox(), name=f"tg-intake-{self.bridge_name}"
            )

    async def stop(self) -> None:
        """Stop taking new work, finish what is in hand, then close the session.

        The order is the whole point. Polling stops first so nothing new arrives;
        the processing pass is given a bounded moment to finish the update it is
        holding; only then does the session close. Whatever does not finish stays
        in `telegram_inbox` and is picked up by the next start.
        """
        self._stopping.set()

        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        worker, self._worker = self._worker, None
        if worker is not None and not worker.done():
            self._work.set()
            try:
                await asyncio.wait_for(worker, timeout=DRAIN_TIMEOUT_SECONDS)
            except TimeoutError:
                logger.warning(
                    "bridge %s: intake did not drain in %ss; the rest stays on disk",
                    self.bridge_name,
                    DRAIN_TIMEOUT_SECONDS,
                )
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker
            except asyncio.CancelledError:
                pass

        if self._in_flight:
            done, pending = await asyncio.wait(
                self._in_flight, timeout=DRAIN_TIMEOUT_SECONDS
            )
            for stray in pending:
                stray.cancel()
            logger.debug(
                "bridge %s: %s handler task(s) finished, %s cancelled",
                self.bridge_name,
                len(done),
                len(pending),
            )

        await self.bot.session.close()

    async def _resume_offset(self) -> None:
        """Pick the cursor back up, so a restart does not refetch a day of updates.

        Correctness does not depend on this — the unique index makes a replay a
        no-op — but watching a day of traffic march past on every restart is its
        own kind of alarming.
        """
        if self._inbox is None or self._bot_id is None:
            return
        with contextlib.suppress(Exception):
            remembered = await self._inbox.offset(self._bot_id)
            if remembered is not None:
                self._offset = remembered

    async def _poll(self) -> None:
        await self._resume_offset()
        backoff = INITIAL_BACKOFF_SECONDS
        while not self._stopping.is_set():
            try:
                updates = await self.bot.get_updates(
                    offset=self._offset,
                    timeout=POLL_TIMEOUT_SECONDS,
                    allowed_updates=self._allowed_updates,
                )
                backoff = INITIAL_BACKOFF_SECONDS
            except asyncio.CancelledError:
                raise
            except TelegramRetryAfter as error:
                logger.warning(
                    "bridge %s is rate limited, waiting %ss", self.bridge_name, error.retry_after
                )
                await asyncio.sleep(error.retry_after)
                continue
            except TelegramUnauthorizedError:
                # A revoked token cannot fix itself; keep the process and the
                # other bridges alive and stop this one loudly.
                logger.error("bridge %s: token was revoked, polling stopped", self.bridge_name)
                return
            except (TelegramNetworkError, OSError) as error:
                logger.warning("bridge %s: polling failed (%s), retrying", self.bridge_name, error)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                continue
            except Exception:
                logger.exception("bridge %s: unexpected polling error", self.bridge_name)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                continue

            if not updates:
                continue

            try:
                await self._take(updates)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Intake failed, so the offset stays where it is and Telegram
                # sends these again. Retrying is the correct outcome here.
                logger.exception(
                    "bridge %s: could not store updates; they will be re-fetched",
                    self.bridge_name,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    async def _take(self, updates: list[Update]) -> None:
        """Store every update, then acknowledge them all with one offset move.

        If storing raises, the offset does not move and Telegram re-sends the
        batch. That is the entire safety property: an update is only ever
        acknowledged after it exists somewhere a crash cannot reach.

        It is also why an update that can *never* be stored is not a lost message
        but a stopped bridge — see `storable_update` for the one that was.
        """
        if self._inbox is None:
            # Straight-through mode: no durability to offer, so acknowledge as
            # we go and dispatch under a tracked task.
            for update in updates:
                self._offset = update.update_id + 1
                self._track(update)
            return

        bot_id = self._bot_id if self._bot_id is not None else self.bot.id
        for update in updates:
            await self._inbox.store(
                bot_id=bot_id,
                update_id=update.update_id,
                payload=storable_update(update),
                bridge_name=self.bridge_name,
            )

        highest = max(update.update_id for update in updates)
        self._offset = highest + 1
        await self._inbox.remember_offset(bot_id, self._offset)
        self._work.set()

    def _track(self, update: Update) -> None:
        """Dispatch under a task the runner keeps a reference to.

        The reference is not bookkeeping: a bare `create_task` may be collected
        while it is still running, which is what the old `noqa: RUF006` was
        silencing.
        """
        task = asyncio.create_task(
            self._feed(update), name=f"tg-update-{self.bridge_name}-{update.update_id}"
        )
        self._in_flight.add(task)
        task.add_done_callback(self._in_flight.discard)

    async def _process_inbox(self) -> None:
        """Hand stored updates to the dispatcher, oldest first, one at a time.

        Sequential on purpose. Two of the owner's messages processed in parallel
        can reach MAX in either order, and a reply can land before the message it
        answers. A slow media download delaying the next line of text in the same
        conversation is the cheaper problem.
        """
        assert self._inbox is not None
        while True:
            drained = await self._drain_inbox()
            if self._stopping.is_set() and not drained:
                return
            if drained:
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._work.wait(), IDLE_SECONDS)
            self._work.clear()

    async def _drain_inbox(self) -> bool:
        assert self._inbox is not None
        bot_id = self._bot_id if self._bot_id is not None else self.bot.id
        try:
            pending = await self._inbox.claim_open(limit=10, bot_id=bot_id)
        except Exception:
            logger.exception("bridge %s: cannot read the inbox", self.bridge_name)
            return False

        for stored in pending:
            await self._handle_stored(stored)
        return bool(pending)

    async def _handle_stored(self, stored: Any) -> None:
        assert self._inbox is not None
        try:
            update = Update.model_validate(json.loads(stored.payload_json))
        except Exception as error:
            # Unparseable after a round trip through the database. Retrying
            # cannot help, and it must not block the queue behind it.
            logger.exception(
                "bridge %s: stored update %s is unreadable", self.bridge_name, stored.id
            )
            await self._inbox.mark_failed(stored.id, error=f"unreadable: {error}")
            return

        try:
            await self._dispatcher.feed_update(self.bot, update)
        except asyncio.CancelledError:
            # Shutting down mid-update: hand it back so the next start redoes it.
            await self._inbox.release(stored.id, error="cancelled during shutdown")
            raise
        except Exception as error:  # noqa: BLE001 - classified by attempt count below
            message = f"{type(error).__name__}: {error}"
            if stored.attempts + 1 >= MAX_INTAKE_ATTEMPTS:
                logger.error(
                    "bridge %s: giving up on update %s after %s attempts (%s)",
                    self.bridge_name,
                    stored.update_id,
                    stored.attempts + 1,
                    message,
                )
                await self._inbox.mark_failed(stored.id, error=message)
            else:
                logger.warning(
                    "bridge %s: update %s failed (%s), will retry",
                    self.bridge_name,
                    stored.update_id,
                    message,
                )
                await self._inbox.release(stored.id, error=message)
            return

        await self._inbox.mark_done(stored.id)

    async def _feed(self, update: object) -> None:
        try:
            await self._dispatcher.feed_update(self.bot, update)  # type: ignore[arg-type]
        except asyncio.CancelledError:
            raise
        except Exception:
            # aiogram already logs handler errors; this is the last net so one
            # bad update cannot kill the polling task.
            logger.exception("bridge %s: update handling failed", self.bridge_name)
