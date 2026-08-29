"""the task supervisor — long-lived tasks that come back after they fall over.

Most of the bridge is already self-healing in the small: PyMax reconnects, each
Telegram poller retries with backoff, each outbox worker has its own queue. What
none of them survives is the case this module exists for — the task *itself*
ending, from a bug, a cancelled await deep in a library, or an exception nobody
expected. A dead task is silent: messages simply stop, and nothing says why.

So every background loop is registered here with a factory rather than a
coroutine, because restarting means building a fresh one. Three rules shape it:

* **a crash is retried with backoff**, so a permanently broken task cannot spin;
* **a task that exits cleanly stays stopped** — that is a decision it made, like
  a poller shutting down on a revoked token;
* **the last error is remembered**, because `/status` answering "polling is
  down" is worth more than a stack trace nobody reads at 3am.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

Factory = Callable[[], Awaitable[None]]

INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 60.0


@dataclass
class TaskState:
    """What `/status` needs to know about one supervised loop."""

    name: str
    running: bool = False
    restarts: int = 0
    last_error: str | None = None
    last_started_at: float = field(default_factory=time.time)
    stopped_deliberately: bool = False


class Supervisor:
    """Keeps a set of named background loops alive."""

    def __init__(
        self,
        *,
        initial_backoff: float = INITIAL_BACKOFF_SECONDS,
        max_backoff: float = MAX_BACKOFF_SECONDS,
    ) -> None:
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._states: dict[str, TaskState] = {}
        self._shutting_down = False

    def start(self, name: str, factory: Factory) -> None:
        """Run `factory()` under supervision. Replaces a task of the same name."""
        if name in self._tasks and not self._tasks[name].done():
            logger.debug("supervised task %s is already running", name)
            return

        self._states[name] = TaskState(name=name, running=True)
        self._tasks[name] = asyncio.create_task(self._supervise(name, factory), name=name)

    async def _supervise(self, name: str, factory: Factory) -> None:
        backoff = self._initial_backoff
        state = self._states[name]

        while not self._shutting_down:
            state.running = True
            state.last_started_at = time.time()
            try:
                await factory()
            except asyncio.CancelledError:
                state.running = False
                raise
            except Exception as error:
                state.running = False
                state.last_error = f"{type(error).__name__}: {error}"[:200]
                logger.exception("supervised task %s crashed", name)
            else:
                # A clean return is a decision, not a failure: a poller that
                # stops on a revoked token must not be started again in a loop.
                state.running = False
                state.stopped_deliberately = True
                logger.info("supervised task %s finished and will not restart", name)
                return

            if self._shutting_down:
                return

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._max_backoff)
            state.restarts += 1
            logger.info("restarting supervised task %s (attempt %s)", name, state.restarts)

    async def stop(self) -> None:
        """Cancel everything and wait for it, best effort."""
        self._shutting_down = True
        tasks, self._tasks = self._tasks, {}

        for task in tasks.values():
            if not task.done():
                task.cancel()
        for name, task in tasks.items():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            state = self._states.get(name)
            if state is not None:
                state.running = False

    def snapshot(self) -> dict[str, TaskState]:
        return dict(self._states)

    @property
    def unhealthy(self) -> list[TaskState]:
        """Tasks that are neither running nor deliberately finished."""
        return [
            state
            for state in self._states.values()
            if not state.running and not state.stopped_deliberately
        ]
