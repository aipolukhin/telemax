"""The keepalive flag that decides whether MAX says «В сети» about the owner.

Each compatibility rule has a test here:

* the server acts on a *change* of `interactive`, never on a repeat;
* a login raises it, and the server's own "online" step lands a few seconds
  after the login answer — so the first lowering has to wait out `settle`;
* nothing else raises it: not sending, not typing, not receiving.

So the two failures worth catching are a flag that never goes down (the bug this
came from: permanently online) and one that goes down too early (silently
overwritten by the login, and never re-sent because a second `false` is not an
edge).
"""

from __future__ import annotations

import asyncio
from typing import Any

from bridge.max_client.interactive import PING, InteractivePings, PresenceMode


class FakeApp:
    """Just enough PyMax `App` to run the ping loop against."""

    def __init__(self) -> None:
        self.sent: list[bool] = []
        self.fails: list[Exception] = []
        self._ping_task: asyncio.Task[None] | None = None
        self.raise_on_ping: Exception | None = None
        app = self

        class _Connection:
            async def fail(self, error: Exception) -> None:
                app.fails.append(error)

        self.connection = _Connection()

    async def invoke(self, opcode: int, payload: dict[str, Any]) -> None:
        assert opcode == PING
        if self.raise_on_ping is not None:
            raise self.raise_on_ping
        self.sent.append(bool(payload["interactive"]))


class FakeClient:
    def __init__(self, app: FakeApp) -> None:
        self._app = app


async def settle(times: int = 6) -> None:
    """Let the loop run without advancing the clock more than it must."""
    for _ in range(times):
        await asyncio.sleep(0)


def pings(mode: PresenceMode, **kwargs: Any) -> InteractivePings:
    # Short enough that a test is a test and not a wait; the values themselves
    # are what production reads from the config.
    defaults = {"idle_seconds": 0.2, "period_seconds": 0.15, "settle_seconds": 0.1}
    return InteractivePings(mode=mode, **{**defaults, **kwargs})


async def test_mirror_starts_online_then_drops_to_background() -> None:
    """The login says interactive; the bridge takes that back once it is safe."""
    app = FakeApp()
    flag = pings(PresenceMode.MIRROR)
    flag.install(FakeClient(app))
    try:
        await settle()
        assert app.sent == [True], "the settle window must keep the login's own state"

        await asyncio.sleep(0.25)
        assert False in app.sent, "the flag never went down: the owner stays «В сети»"
        assert app.sent[-1] is False
    finally:
        await flag.close()


async def test_a_connect_is_not_an_owner_action() -> None:
    """Otherwise every restart holds the account online for a whole idle window.

    Caught live: the first deploy of this stayed «В сети» for 90 s after each
    start because `install` stamped the connect as activity.
    """
    app = FakeApp()
    flag = pings(PresenceMode.MIRROR, settle_seconds=0.1, idle_seconds=5.0)
    flag.install(FakeClient(app))
    try:
        await asyncio.sleep(0.25)
        assert app.sent[-1] is False, "the settle window, not the idle window, sets the drop"
    finally:
        await flag.close()


async def test_a_reconnect_keeps_a_recent_action() -> None:
    """The socket dropping is not the owner walking away mid-sentence."""
    app = FakeApp()
    flag = pings(PresenceMode.MIRROR, settle_seconds=0.05, idle_seconds=5.0)
    flag.install(FakeClient(app))
    try:
        flag.touch()
        await settle()

        again = FakeApp()
        flag.install(FakeClient(again))
        await asyncio.sleep(0.2)
        assert again.sent[-1] is True
    finally:
        await flag.close()


async def test_the_drop_waits_out_the_settle_window() -> None:
    """A `false` sent before the server's late "online" is lost for good.

    Nothing after it is an edge, so the account would stay online forever — the
    exact way the first attempt at this failed in compatibility fixtures.
    """
    app = FakeApp()
    flag = pings(PresenceMode.MIRROR, settle_seconds=0.4)
    flag.install(FakeClient(app))
    try:
        await asyncio.sleep(0.2)
        assert False not in app.sent, "lowered inside the settle window: the drop is lost"
    finally:
        await flag.close()


async def test_an_owner_action_raises_it_again() -> None:
    app = FakeApp()
    flag = pings(PresenceMode.MIRROR)
    flag.install(FakeClient(app))
    try:
        await asyncio.sleep(0.25)
        assert app.sent[-1] is False

        flag.touch()
        await settle()
        assert app.sent[-1] is True, "a reply must show the owner as present"

        await asyncio.sleep(0.3)
        assert app.sent[-1] is False, "and the presence must expire on its own"
    finally:
        await flag.close()


async def test_a_touch_while_online_does_not_re_send_the_flag() -> None:
    """Repeats are not edges; sending them is noise on a hot path."""
    app = FakeApp()
    flag = pings(PresenceMode.MIRROR)
    flag.install(FakeClient(app))
    try:
        await settle()
        before = len(app.sent)
        for _ in range(5):
            flag.touch()
        await settle()
        assert len(app.sent) == before
    finally:
        await flag.close()


async def test_offline_never_comes_back_up() -> None:
    app = FakeApp()
    flag = pings(PresenceMode.OFFLINE)
    flag.install(FakeClient(app))
    try:
        await asyncio.sleep(0.25)
        assert app.sent[-1] is False

        flag.touch()
        await asyncio.sleep(0.25)
        assert True not in app.sent[1:], "offline mode must ignore what the owner does"
    finally:
        await flag.close()


async def test_online_is_the_old_behaviour() -> None:
    app = FakeApp()
    flag = pings(PresenceMode.ONLINE)
    flag.install(FakeClient(app))
    try:
        await asyncio.sleep(0.3)
        assert app.sent and all(app.sent), "`online` must keep PyMax's own semantics"
        assert len(app.sent) > 1, "and must keep pinging: the socket dies without it"
    finally:
        await flag.close()


async def test_the_socket_keeps_being_pinged_while_backgrounded() -> None:
    """Background is not silence — PyMax's loop is also the dead-socket detector."""
    app = FakeApp()
    flag = pings(PresenceMode.OFFLINE)
    flag.install(FakeClient(app))
    try:
        await asyncio.sleep(0.5)
        assert app.sent.count(False) >= 2
    finally:
        await flag.close()


async def test_a_failed_ping_takes_the_transport_down() -> None:
    """PyMax fails the connection when its ping dies; the reconnect loop needs that."""
    app = FakeApp()
    app.raise_on_ping = ConnectionResetError("boom")
    flag = pings(PresenceMode.MIRROR)
    flag.install(FakeClient(app))
    try:
        await settle()
        assert app.fails, "a ping that cannot be sent must not be swallowed"
    finally:
        await flag.close()


async def test_installing_again_replaces_the_previous_loop() -> None:
    """`start()` is a reconnect loop: every connect brings a fresh app and task."""
    first = FakeApp()
    flag = pings(PresenceMode.MIRROR)
    flag.install(FakeClient(first))
    await settle()

    second = FakeApp()
    flag.install(FakeClient(second))
    try:
        await asyncio.sleep(0.25)
        assert second.sent[-1] is False
        # The old app's loop is gone, and PyMax's own handle points at ours so a
        # normal `App.close()` still takes it down.
        assert first._ping_task is not None and first._ping_task.cancelled()
        assert second._ping_task is not None and not second._ping_task.done()
    finally:
        await flag.close()


async def test_close_stops_the_loop() -> None:
    app = FakeApp()
    flag = pings(PresenceMode.MIRROR)
    flag.install(FakeClient(app))
    await settle()
    await flag.close()

    quiet = len(app.sent)
    await asyncio.sleep(0.3)
    assert len(app.sent) == quiet
