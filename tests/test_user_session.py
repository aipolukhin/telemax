"""The owner's MTProto user session: QR login, owner verification, lifecycle.

Driven entirely through a fake Telethon client injected as the factory — no
network, no real account. What is pinned here is the contract the transport
depends on: the wrong account is refused and its file deleted, a valid session is
reused without a QR, a revoked one recovers by QR, 2FA is handled, the code
refreshes, and connect/disconnect move the one health component key.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from telethon import errors

from bridge.telegram.user_session import (
    OwnerMismatchError,
    TelegramUserSession,
    authorize_owner_session,
    contact_bot_allowlist,
    qr_ascii,
    user_session_path,
)

OWNER = 100000001
STRANGER = 999


class FakeMe:
    def __init__(self, account_id: int, name: str | None = None, username: str | None = None):
        self.id = account_id
        self.first_name = name
        self.username = username


class FakeQR:
    def __init__(self, client: FakeTelethon) -> None:
        self._client = client
        self.url = client._next_url()

    async def wait(self, timeout: float | None = None) -> None:  # noqa: ASYNC109
        outcome = self._client.qr_outcomes.pop(0)
        if outcome == "ok":
            self._client.authorized = True
            return
        if outcome == "password":
            raise errors.SessionPasswordNeededError(request=None)
        raise TimeoutError

    async def recreate(self) -> None:
        self.url = self._client._next_url()
        self._client.recreated += 1


class FakeTelethon:
    """Only the surface `user_session.py` touches, with its awkward parts kept."""

    def __init__(
        self,
        session: str,
        api_id: int,
        api_hash: str,
        *,
        authorized: bool = False,
        account_id: int = OWNER,
        name: str | None = "Owner",
        username: str | None = "owner",
        qr_outcomes: list[str] | None = None,
    ) -> None:
        self.session = session
        self.api_id = api_id
        self.api_hash = api_hash
        self.authorized = authorized
        self.account_id = account_id
        self.name = name
        self.username = username
        self.qr_outcomes = qr_outcomes if qr_outcomes is not None else ["ok"]
        self.connected = False
        self.disconnected = False
        self.logged_out = False
        self.qr_logins = 0
        self.recreated = 0
        self.password_used: str | None = None
        self._url_seq = 0
        #: (event spec, handler), in registration order — what `@client.on` took.
        self.handlers: list[tuple[Any, Any]] = []

    def on(self, event: Any) -> Any:
        def register(handler: Any) -> Any:
            self.handlers.append((event, handler))
            return handler

        return register

    async def dispatch_raw(self, update: Any) -> None:
        """Feed one raw update to the handlers registered for `events.Raw`."""
        from telethon import events

        for spec, handler in self.handlers:
            if spec is events.Raw:
                await handler(update)

    def _next_url(self) -> str:
        self._url_seq += 1
        return f"tg://login?token=fake{self._url_seq}"

    async def connect(self) -> None:
        self.connected = True

    def is_connected(self) -> bool:
        """Telethon's own probe — a method, and the only honest source.

        Modelled rather than stubbed out, because the defect being closed was
        exactly the gap between "we opened it once" and "it is open".
        """
        return self.connected

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def qr_login(self) -> FakeQR:
        self.qr_logins += 1
        return FakeQR(self)

    async def sign_in(self, password: str | None = None) -> None:
        self.authorized = True
        self.password_used = password

    async def get_me(self) -> FakeMe:
        return FakeMe(self.account_id, self.name, self.username)

    async def log_out(self) -> None:
        self.logged_out = True
        self.authorized = False

    async def disconnect(self) -> None:
        self.disconnected = True


def _factory(**config: Any) -> tuple[Any, list[FakeTelethon]]:
    made: list[FakeTelethon] = []

    def factory(session: str, api_id: int, api_hash: str) -> FakeTelethon:
        client = FakeTelethon(session, api_id, api_hash, **config)
        made.append(client)
        return client

    return factory, made


async def _authorize(tmp_path: Path, factory: Any, **kwargs: Any) -> Any:
    shown: list[str] = []

    async def on_qr(url: str) -> None:
        shown.append(url)

    result = await authorize_owner_session(
        api_id=123,
        api_hash="hash-never-logged",
        secrets_dir=tmp_path / "secrets",
        owner_user_id=OWNER,
        on_qr=on_qr,
        password_provider=lambda _prompt: "2fa-pass",
        client_factory=factory,
        refresh_seconds=0.01,
        **kwargs,
    )
    return result, shown


# ------------------------------------------------------------------- QR login


async def test_a_fresh_qr_login_authorises_and_verifies_the_owner(tmp_path: Path) -> None:
    factory, made = _factory(
        authorized=False,
        qr_outcomes=["ok"],
        name="Владелец",
        username="owner_example",
    )
    owner, shown = await _authorize(tmp_path, factory)

    assert owner.account_id == OWNER
    assert owner.name == "Владелец" and owner.username == "owner_example"
    assert made[0].qr_logins == 1
    assert shown, "the QR url was rendered to the console"
    assert made[0].disconnected, "the login connection is closed before the runtime opens its own"


async def test_an_expired_qr_is_refreshed_then_scanned(tmp_path: Path) -> None:
    factory, made = _factory(authorized=False, qr_outcomes=["timeout", "timeout", "ok"])
    owner, shown = await _authorize(tmp_path, factory)

    assert owner.account_id == OWNER
    assert made[0].recreated == 2, "the code was refreshed twice before the scan"
    assert len(shown) == 3, "each refreshed code was shown"


async def test_two_factor_password_is_handled(tmp_path: Path) -> None:
    factory, made = _factory(authorized=False, qr_outcomes=["password"])
    owner, _ = await _authorize(tmp_path, factory)

    assert owner.account_id == OWNER
    assert made[0].password_used == "2fa-pass"


async def test_a_wrong_account_is_refused_and_its_session_deleted(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir(parents=True)
    session_file = user_session_path(secrets)
    session_file.write_text("stranger-session", encoding="utf-8")

    factory, made = _factory(authorized=False, qr_outcomes=["ok"], account_id=STRANGER)

    async def on_qr(_url: str) -> None:
        return None

    with pytest.raises(OwnerMismatchError):
        await authorize_owner_session(
            api_id=1,
            api_hash="h",
            secrets_dir=secrets,
            owner_user_id=OWNER,
            on_qr=on_qr,
            client_factory=factory,
            refresh_seconds=0.01,
        )

    assert made[0].logged_out, "the stranger's account was logged out"
    assert not session_file.exists(), "and its session file deleted"


async def test_a_valid_session_is_reused_without_a_qr(tmp_path: Path) -> None:
    factory, made = _factory(authorized=True, account_id=OWNER)
    owner, shown = await _authorize(tmp_path, factory)

    assert owner.account_id == OWNER
    assert made[0].qr_logins == 0, "no QR when the stored session already works"
    assert shown == []


async def test_a_revoked_session_recovers_by_qr(tmp_path: Path) -> None:
    # `is_user_authorized()` is False — the stored session was revoked — so the
    # QR path runs and re-authorises.
    factory, made = _factory(authorized=False, qr_outcomes=["ok"])
    owner, _ = await _authorize(tmp_path, factory)

    assert owner.account_id == OWNER
    assert made[0].qr_logins == 1


async def test_successful_qr_login_decides_nothing_about_intake(
    tmp_path: Path,
) -> None:
    """Authorising a session is not enabling intake — the gate is a later step.

    A valid, owner-verified session saved to disk still leaves
    `owner_mtproto_intake_enabled` false. Enabling it is a separate atomic
    operation gated on the live probe; nothing in the login path may flip it.
    """
    from bridge.onboarding.state import StateStore

    store = StateStore(tmp_path / "onboarding.json")
    assert not hasattr(store.load(), "owner_mtproto_intake_enabled")

    factory, _ = _factory(authorized=False, qr_outcomes=["ok"])
    owner, _shown = await _authorize(tmp_path, factory)
    assert owner.account_id == OWNER, "the login itself succeeded"

    assert not hasattr(store.load(), "owner_mtproto_intake_enabled")


# --------------------------------------------------------- runtime lifecycle


class FakeHealth:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def note_tg_session_connected(self) -> None:
        self.events.append("connected")

    async def note_tg_session_disconnected(self, error: str | None = None) -> None:
        self.events.append(f"disconnected:{error}")


async def _session(account_id: int, health: FakeHealth) -> tuple[TelegramUserSession, FakeTelethon]:
    client = FakeTelethon("s", 1, "h", account_id=account_id)
    client.connected = True

    async def connect() -> FakeTelethon:
        return client

    async def allow() -> set[int]:
        return {100, 200}

    session = TelegramUserSession(
        connect=connect,
        owner_user_id=OWNER,
        health=health,
        allowed_bot_ids=allow,
    )
    return session, client


async def test_starting_the_session_marks_health_connected() -> None:
    health = FakeHealth()
    session, _ = await _session(OWNER, health)

    await session.start()

    assert session.is_connected
    assert health.events == ["connected"]


async def test_stopping_the_session_marks_health_disconnected() -> None:
    health = FakeHealth()
    session, client = await _session(OWNER, health)
    await session.start()

    await session.stop()

    assert not session.is_connected
    assert client.disconnected
    assert health.events == ["connected", "disconnected:None"]


class FakeIntake:
    """Only the two hooks a read update travels through."""

    def __init__(self) -> None:
        self.reads: list[tuple[int, int, int]] = []

    async def on_owner_read(
        self, *, bot_id: int, owner_account_id: int, owner_message_id: int
    ) -> None:
        self.reads.append((bot_id, owner_account_id, owner_message_id))


async def test_reading_a_bot_chat_reaches_the_intake() -> None:
    """`UpdateReadHistoryInbox` is the only honest "the owner has seen it"."""
    from telethon.tl import types

    intake = FakeIntake()
    client = FakeTelethon("s", 1, "h", account_id=OWNER)
    client.connected = True

    async def connect() -> FakeTelethon:
        return client

    async def allow() -> set[int]:
        return {100}

    session = TelegramUserSession(
        connect=connect,
        owner_user_id=OWNER,
        health=FakeHealth(),
        allowed_bot_ids=allow,
        intake=intake,
    )
    await session.start()

    await client.dispatch_raw(
        types.UpdateReadHistoryInbox(
            peer=types.PeerUser(user_id=100),
            max_id=77,
            still_unread_count=0,
            pts=5,
            pts_count=1,
        )
    )
    # The contact bot reading *our* messages is the other constructor, and it is
    # none of the bridge's business.
    await client.dispatch_raw(
        types.UpdateReadHistoryOutbox(
            peer=types.PeerUser(user_id=100), max_id=90, pts=6, pts_count=1
        )
    )

    assert intake.reads == [(100, OWNER, 77)]


async def test_a_wrong_account_never_becomes_connected() -> None:
    health = FakeHealth()
    session, client = await _session(STRANGER, health)

    with pytest.raises(OwnerMismatchError):
        await session.start()

    assert not session.is_connected
    assert client.disconnected
    assert "connected" not in health.events


async def test_a_failed_identity_check_disconnects_the_client() -> None:
    """The leak that made «database is locked» permanent.

    `_connect()` hands back an already-open client, and the owner check is the
    first high-level request to run over it — the one that touches the session
    file. When *that* raises (a locked file, a socket dropped between connect and
    now), the earlier version dropped the client with its sqlite handle still
    open. One leak per five-second retry is what deadlocked the session file for
    good, so the failure path has to close the client just like the mismatch one
    does — and mark the session retrying, not terminal.
    """
    health = FakeHealth()
    session, client = await _session(OWNER, health)

    async def boom() -> FakeMe:
        raise sqlite3.OperationalError("database is locked")

    client.get_me = boom  # type: ignore[method-assign]

    with pytest.raises(sqlite3.OperationalError):
        await session.start()

    assert client.disconnected, "the open client is closed on failure, not leaked"
    assert not session.is_connected
    assert health.events == ["disconnected:OperationalError: database is locked"]


# -------------------------------------------------------------- allowlist


class _Bridge:
    def __init__(self, bot_id: int | None) -> None:
        self.telegram_bot_id = bot_id


class _Bridges:
    def __init__(self, bots: list[int | None]) -> None:
        self._bots = bots

    async def active(self) -> list[_Bridge]:
        return [_Bridge(b) for b in self._bots]


async def test_the_allowlist_is_the_active_bridge_bots() -> None:
    resolve = contact_bot_allowlist(_Bridges([100, 200, None, 300]))
    assert await resolve() == {100, 200, 300}


def test_qr_ascii_renders_something_scannable() -> None:
    art = qr_ascii("tg://login?token=abc")
    assert art and any(glyph in art for glyph in "█▀▄")


# ---------------------------------------------------- supervision and status


async def test_a_dead_client_is_not_reported_as_connected() -> None:
    """`is_connected` used to be a bool set on the way up and never cleared.

    So a session Telethon had given up on went on reporting itself healthy for
    the life of the process: `/status` said connected, the watchdog saw nothing
    to reconnect, and the gate went on holding the Bot API path shut against a
    transport that was not there.
    """
    from bridge.telegram.user_session import SessionStatus

    health = FakeHealth()
    session, client = await _session(OWNER, health)
    await session.start()
    assert session.is_connected
    assert session.status is SessionStatus.CONNECTED

    client.connected = False
    assert not session.is_connected, "the client is what knows, not a flag we set"


async def test_a_wrong_account_is_terminal_and_says_so() -> None:
    """Retrying a session that belongs to somebody else is noise, not resilience."""
    from bridge.telegram.user_session import SessionStatus

    health = FakeHealth()
    session, _ = await _session(STRANGER, health)

    with pytest.raises(OwnerMismatchError):
        await session.start()

    assert session.status is SessionStatus.UNAUTHORIZED
    assert not session.is_connected
    assert any("unauthorized" in event for event in health.events)


async def test_an_unauthorised_session_is_terminal_and_a_network_blip_is_not() -> None:
    """The two states that used to read the same, and ask for opposite things."""
    from bridge.telegram.user_session import (
        SessionStatus,
        SessionUnauthorizedError,
        SessionUnusableError,
    )

    async def allow() -> set[int]:
        return set()

    for error, expected in (
        (SessionUnauthorizedError("revoked"), SessionStatus.UNAUTHORIZED),
        (SessionUnusableError("no api id"), SessionStatus.FAILED),
        (OSError("network is down"), SessionStatus.RETRYING),
    ):
        health = FakeHealth()

        async def connect(raised: BaseException = error) -> Any:
            raise raised

        session = TelegramUserSession(
            connect=connect, owner_user_id=OWNER, health=health, allowed_bot_ids=allow
        )
        with pytest.raises(type(error)):
            await session.start()
        assert session.status is expected, f"{error} became {session.status}"


async def test_the_watchdog_stops_only_on_a_terminal_status() -> None:
    """A revoked session is a decision; a dropped socket is not.

    The loop returns for the first and keeps trying for the second — and the
    supervisor reads a clean return as "this task decided to stop", which is
    exactly what a session nobody can reconnect has done.
    """
    from bridge.telegram.user_session import SessionStatus, SessionUnauthorizedError

    async def allow() -> set[int]:
        return set()

    attempts: list[int] = []

    async def revoked() -> Any:
        attempts.append(1)
        raise SessionUnauthorizedError("revoked")

    health = FakeHealth()
    session = TelegramUserSession(
        connect=revoked, owner_user_id=OWNER, health=health, allowed_bot_ids=allow
    )
    # `watch()` is registered even though the session never came up — that is the
    # regression this closes — and it gives up once the status turns terminal.
    await asyncio.wait_for(session.watch()(), timeout=5)

    assert attempts == [1], "one attempt, then it stops asking"
    assert session.status is SessionStatus.UNAUTHORIZED


async def test_the_watchdog_backs_off_between_attempts(monkeypatch: Any) -> None:
    """Not a tight loop: a refusing session must not be asked every tick."""
    from bridge.telegram.user_session import RECONNECT_INITIAL_SECONDS

    async def allow() -> set[int]:
        return set()

    attempts: list[float] = []
    slept: list[float] = []

    async def failing() -> Any:
        attempts.append(1.0)
        if len(attempts) >= 3:
            raise asyncio.CancelledError
        raise OSError("network is down")

    health = FakeHealth()
    session = TelegramUserSession(
        connect=failing, owner_user_id=OWNER, health=health, allowed_bot_ids=allow
    )

    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)
        await real_sleep(0)

    monkeypatch.setattr("bridge.telegram.user_session.asyncio.sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await session.watch()()

    assert slept[:2] == [RECONNECT_INITIAL_SECONDS, RECONNECT_INITIAL_SECONDS * 2], (
        f"expected a doubling backoff, got {slept}"
    )
