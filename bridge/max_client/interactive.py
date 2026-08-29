"""Control whether the bridge presents the MAX account as interactive.

The bridge keeps a long-lived socket, while presence should reflect the owner's
activity rather than the process lifetime. `MIRROR` raises presence only around
owner actions, `OFFLINE` keeps the bridge backgrounded, and `ONLINE` preserves
the upstream client's default. The setting affects presence only; background
connections continue receiving messages.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)

#: `Opcode.PING`. Not imported from `.opcodes` — that enum is the bridge's list
#: of frames PyMax gets wrong, and this one it merely fills in badly.
PING = 1

#: A little under the upstream cadence, so a slow round trip cannot look like a
#: dead socket.
PING_PERIOD_SECONDS = 25.0

#: How long after a connect the flag stays up before it can be lowered. The
#: server's own "you are online" step lands within about five seconds of the
#: login answer; a drop before that is silently overwritten, and since the flag
#: is edge-triggered, nothing after it would ever be applied.
SETTLE_SECONDS = 15.0


class PresenceMode(StrEnum):
    """What a MAX contact sees about the owner while the bridge is connected."""

    MIRROR = "mirror"
    OFFLINE = "offline"
    ONLINE = "online"


class InteractivePings:
    """PyMax's ping loop, with the `interactive` flag under our control.

    One instance per `MaxClient`, re-installed on every connect: `start()` is a
    reconnect loop, each round builds a fresh app with a fresh ping task, and a
    one-time patch would work exactly until the first blip.
    """

    def __init__(
        self,
        *,
        mode: PresenceMode = PresenceMode.MIRROR,
        idle_seconds: float = 90.0,
        period_seconds: float = PING_PERIOD_SECONDS,
        settle_seconds: float = SETTLE_SECONDS,
    ) -> None:
        self._mode = mode
        self._idle = max(0.0, float(idle_seconds))
        # A floor, not a policy: the period is a constant in this module and the
        # tests shorten it. Zero would be a ping storm against the server.
        self._period = max(0.05, float(period_seconds))
        self._settle = max(0.0, float(settle_seconds))
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        #: `None` is "the owner has not done anything yet", which is the honest
        #: state at start-up. Connecting is not an action of theirs, and treating
        #: it as one would hold the account online for a whole idle window after
        #: every restart.
        self._last_touch: float | None = None
        self._settled_at = 0.0
        #: What the server currently believes. A connect is a login, and a login
        #: declares `interactive: true`, so that is where every round starts.
        self._declared = True

    # ---------------------------------------------------------------- lifecycle

    def install(self, client: Any) -> None:
        """Take PyMax's ping task over for this connection.

        Called from `on_start`: the app exists by then, its own loop is already
        running, and the login that just happened is the `true` this state
        machine starts from.
        """
        app = getattr(client, "_app", None)
        if app is None:  # pragma: no cover - a stubbed client in the tests
            logger.debug("no PyMax app to install interactive pings on")
            return

        self._cancel_task(getattr(app, "_ping_task", None))
        self._cancel_task(self._task)

        # `_last_touch` is deliberately carried over: a reconnect in the middle
        # of a conversation should not throw away the fact that the owner was
        # writing a moment ago.
        self._settled_at = _now() + self._settle
        self._declared = True
        self._wake.clear()

        self._task = asyncio.create_task(self._loop(app), name="max-interactive-pings")
        # PyMax cancels `_ping_task` in `App.close()`; ours is the ping task now,
        # so a normal shutdown still takes it down.
        app._ping_task = self._task
        logger.debug("interactive pings installed (mode=%s)", self._mode.value)

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    def touch(self) -> None:
        """The owner just did something through the bridge."""
        if self._mode is not PresenceMode.MIRROR:
            return
        self._last_touch = _now()
        if not self._declared:
            # Raise it now rather than at the next tick: a reply that shows up
            # a quarter-minute after the owner sent it is worse than nothing.
            self._wake.set()

    # ------------------------------------------------------------------ internals

    def _wanted(self, now: float) -> bool:
        """Should the server think the owner is looking at MAX right now?"""
        if self._mode is PresenceMode.ONLINE:
            return True
        if now < self._settled_at:
            # The login's own "online" has not necessarily landed yet, and a drop
            # that lands before it would be overwritten and never re-sent.
            return True
        if self._mode is PresenceMode.OFFLINE:
            return False
        return self._last_touch is not None and now < self._last_touch + self._idle

    def _next_deadline(self, now: float) -> float:
        """When `_wanted` could change on its own, with no one touching it."""
        if self._mode is PresenceMode.ONLINE:
            return now + self._period
        if now < self._settled_at:
            return self._settled_at
        if self._mode is PresenceMode.OFFLINE:
            return now + self._period
        if self._last_touch is None:
            return now + self._period
        idle_ends = self._last_touch + self._idle
        return idle_ends if now < idle_ends else now + self._period

    async def _loop(self, app: Any) -> None:
        next_ping = 0.0
        try:
            while True:
                now = _now()
                wanted = self._wanted(now)
                if wanted != self._declared or now >= next_ping:
                    await self._send(app, wanted)
                    self._declared = wanted
                    next_ping = _now() + self._period
                self._wake.clear()
                delay = min(next_ping, self._next_deadline(_now())) - _now()
                if delay > 0:
                    try:
                        await asyncio.wait_for(self._wake.wait(), delay)
                    except TimeoutError:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            # PyMax fails the transport when its own ping loop dies, and the
            # reconnect loop is what notices. Keep that: a ping that cannot be
            # sent is how a half-open socket is found.
            logger.warning("interactive ping loop failed; closing transport: %s", error)
            with contextlib.suppress(Exception):
                await app.connection.fail(ConnectionError(f"Ping failed: {error}"))

    async def _send(self, app: Any, interactive: bool) -> None:
        await app.invoke(opcode=PING, payload={"interactive": interactive})
        logger.debug("ping sent interactive=%s", interactive)

    @staticmethod
    def _cancel_task(task: Any) -> None:
        if task is None or getattr(task, "done", lambda: True)():
            return
        task.cancel()


def _now() -> float:
    return asyncio.get_running_loop().time()
