"""The owner ingress is not ready without the owner's puppet session.

One transport, not the bridge. Contact bots keep delivering MAX→Telegram while
this is down, and Guardian keeps answering — which is where an unauthorised
session gets authorised again. What is paused is the owner's own half: new
owner-authored Telegram events, refetching owner media only the session can
reach, and Telegram→MAX synchronisation of what the owner did.

What must not happen is the bridge behaving as though it can carry the owner's
messages when the one transport that sees them is gone.

Six states rather than a boolean, because "not ready" covers conditions that
want opposite things from the owner. Nobody needs to hear about a socket that
will be back in a minute; everybody needs to hear about a session that will not
come back until they scan a QR. One incident either way — a session down for
four reasons in an hour is one problem.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from bridge.service.health import (
    HealthService,
    OwnerIngressState,
    OwnerSessionFacts,
    read_owner_ingress,
)
from bridge.storage import (
    AlertRepository,
    Database,
    HealthStateRepository,
    OutboxRepository,
    TelegramInboxRepository,
)


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(tmp_path / "bridge.db")
    try:
        yield db
    finally:
        await db.close()


def make_service(database: Database, facts: OwnerSessionFacts | None = None) -> HealthService:
    return HealthService(
        health=HealthStateRepository(database),
        outbox=OutboxRepository(database),
        inbox=TelegramInboxRepository(database),
        alerts=AlertRepository(database),
        db_path=Path(database.path),
        bridges=lambda: [("dad", True)],
        max_is_ready=lambda: True,
        owner_session_facts=(lambda: facts) if facts is not None else None,
    )


CONNECTED = OwnerSessionFacts(started=True, status="connected", connected=True)


# ------------------------------------------------------------ the invariant


def test_ready_means_all_four_things() -> None:
    """`configured and authorized and connected and supervisor healthy` — and
    each one is checked, not implied by the others."""
    assert read_owner_ingress(CONNECTED, connected_before=True).ready


@pytest.mark.parametrize(
    ("facts", "state"),
    [
        (OwnerSessionFacts(), OwnerIngressState.MISSING_CONFIGURATION),
        (
            OwnerSessionFacts(started=True, status="failed"),
            OwnerIngressState.MISSING_CONFIGURATION,
        ),
        (
            OwnerSessionFacts(started=True, status="unauthorized"),
            OwnerIngressState.AUTHORIZATION_REQUIRED,
        ),
        (OwnerSessionFacts(started=True, status="stopped"), OwnerIngressState.STOPPED),
    ],
)
def test_each_not_ready_state_is_named(facts: OwnerSessionFacts, state: OwnerIngressState) -> None:
    plane = read_owner_ingress(facts, connected_before=False)
    assert plane.state is state
    assert not plane.ready
    assert plane.reason


def test_coming_up_is_not_the_same_as_dropped() -> None:
    """The two states a boolean used to collapse. One is a first start; the other
    is a bridge that worked five minutes ago and stopped."""
    retrying = OwnerSessionFacts(started=True, status="retrying")
    coming_up = read_owner_ingress(retrying, connected_before=False)
    dropped = read_owner_ingress(retrying, connected_before=True)
    assert coming_up.state is OwnerIngressState.CONNECTING
    assert dropped.state is OwnerIngressState.DISCONNECTED


def test_a_dead_supervisor_is_not_ready_even_when_connected() -> None:
    """`connected` is the socket; the supervisor is whether anything is watching
    it. A live socket nobody supervises is one blip from silence."""
    facts = OwnerSessionFacts(
        started=True, status="connected", connected=True, supervisor_healthy=False
    )
    assert not read_owner_ingress(facts, connected_before=True).ready


def test_a_status_that_says_connected_but_is_not_is_not_ready() -> None:
    facts = OwnerSessionFacts(started=True, status="connected", connected=False)
    assert not read_owner_ingress(facts, connected_before=True).ready


def test_no_reason_ever_carries_a_secret() -> None:
    """Reasons go into `/status` and into an incident. Neither may carry a phone,
    an api_hash, a session token, an auth code or a line of a message."""
    for status in ("failed", "unauthorized", "retrying", "stopped", "connected"):
        for started in (True, False):
            plane = read_owner_ingress(
                OwnerSessionFacts(started=started, status=status), connected_before=False
            )
            lowered = plane.reason.lower()
            for forbidden in ("api_hash", "api_id", "token", "phone", "+7", "code"):
                assert forbidden not in lowered


# ------------------------------------------------------------- the incident


async def test_a_session_that_is_down_opens_one_incident(database: Database) -> None:
    service = make_service(database, OwnerSessionFacts(started=True, status="unauthorized"))
    touched = await service.evaluate(await service.snapshot())
    assert "owner-session-unavailable" in touched


async def test_repeated_reconnect_attempts_do_not_storm(database: Database) -> None:
    """The watchdog retries every few seconds. Four hundred attempts must not be
    four hundred notifications."""
    service = make_service(database, OwnerSessionFacts(started=True, status="retrying"))
    opened = 0
    for _ in range(20):
        if "owner-session-unavailable" in await service.evaluate(await service.snapshot()):
            opened += 1
    assert opened == 1


async def test_one_incident_covers_every_reason(database: Database) -> None:
    """A session down for four different reasons in an hour is one problem."""
    for status in ("retrying", "unauthorized", "failed", "stopped"):
        service = make_service(database, OwnerSessionFacts(started=True, status=status))
        await service.evaluate(await service.snapshot())

    rows = await database.query(
        "SELECT incident_key FROM alert_incidents WHERE resolved_at IS NULL"
    )
    assert [row["incident_key"] for row in rows] == ["owner-session-unavailable"]


async def test_reconnecting_closes_it(database: Database) -> None:
    down = make_service(database, OwnerSessionFacts(started=True, status="retrying"))
    await down.evaluate(await down.snapshot())

    up = make_service(database, CONNECTED)
    touched = await up.evaluate(await up.snapshot())
    assert "owner-session-unavailable:resolved" in touched


async def test_a_healthy_session_opens_nothing(database: Database) -> None:
    service = make_service(database, CONNECTED)
    assert "owner-session-unavailable" not in await service.evaluate(await service.snapshot())


async def test_health_without_a_data_plane_is_not_broken(database: Database) -> None:
    """The setup CLI and most tests build health with no session behind it. That
    reads as ready rather than as an incident nobody can act on."""
    service = make_service(database)
    snapshot = await service.snapshot()
    assert snapshot.owner_ingress.ready
    assert "owner-session-unavailable" not in await service.evaluate(snapshot)


# --------------------------------------------------------- the control plane


async def test_the_incident_text_says_what_still_works(database: Database) -> None:
    """A bridge that is not carrying the owner's messages is still holding their
    queue and still receiving from MAX. Saying only "session down" would read as
    "everything stopped"."""
    service = make_service(database, OwnerSessionFacts(started=True, status="unauthorized"))
    await service.evaluate(await service.snapshot())
    rows = await database.query("SELECT text FROM alert_outbox ORDER BY id DESC LIMIT 1")
    body = rows[0]["text"]
    assert "telegram-sync" in body.lower()
    assert "очередь" in body.lower()


def test_the_status_line_names_every_state() -> None:
    """Six states, six lines. A state with no wording would print its enum value
    at somebody who is trying to fix their bridge."""
    from bridge.service.runtime import _OWNER_INGRESS_LINE

    assert set(_OWNER_INGRESS_LINE) == set(OwnerIngressState)
    for state, line in _OWNER_INGRESS_LINE.items():
        assert line and line != state.value


# ---------------------------------------------------- the runtime's own answer


def _service(tmp_path: Path) -> Any:
    from bridge.config import load_config
    from bridge.config.writer import write_bootstrap_config
    from bridge.service import BridgeService

    config = tmp_path / "config.yaml"
    write_bootstrap_config(
        config,
        owner_user_id=1,
        timezone="Europe/Moscow",
        guardian_token_env="TG",
        data_dir=str(tmp_path / "data"),
    )
    return BridgeService(load_config(config, load_env_file=False))


class FakeSession:
    def __init__(self, status: Any, connected: bool) -> None:
        self.status = status
        self.is_connected = connected


def test_a_fresh_process_is_not_a_ready_bridge(tmp_path: Path) -> None:
    """Nothing has been started yet, so nothing can carry the owner's messages —
    and `/status` says which of the six reasons it is."""
    service = _service(tmp_path)
    assert not service.owner_ingress_ready
    assert service._owner_ingress_line()


def test_readiness_waits_for_the_connection_not_for_the_start(tmp_path: Path) -> None:
    """A session object exists from the moment the supervisor starts it. That is
    not the same as Telegram having accepted it."""
    from bridge.telegram.user_session import SessionStatus

    service = _service(tmp_path)
    service._owner_session = FakeSession(SessionStatus.RETRYING, connected=False)
    assert not service.owner_ingress_ready

    service._owner_session = FakeSession(SessionStatus.CONNECTED, connected=True)
    assert service.owner_ingress_ready


def test_a_drop_after_a_connection_reads_as_a_drop(tmp_path: Path) -> None:
    """The latch. Same status, two meanings, and the owner is told the right one."""
    from bridge.telegram.user_session import SessionStatus

    service = _service(tmp_path)
    service._owner_session = FakeSession(SessionStatus.RETRYING, connected=False)
    coming_up = service._owner_ingress_line()

    service._owner_session = FakeSession(SessionStatus.CONNECTED, connected=True)
    assert service.owner_ingress_ready
    service._owner_session = FakeSession(SessionStatus.RETRYING, connected=False)

    assert not service.owner_ingress_ready
    assert service._owner_ingress_line() != coming_up


def test_the_control_plane_answers_while_the_data_plane_is_down(tmp_path: Path) -> None:
    """The guardian is how the owner fixes an unauthorised session. Gating it on
    a ready ingress would lock the only door to the room with the key in it."""
    service = _service(tmp_path)
    assert not service.owner_ingress_ready
    assert "puppet" not in service._owner_ingress_line().lower()  # no jargon at the owner
    assert service._owner_ingress_line()  # ...but an answer, from an unready process


def test_readiness_does_not_gate_the_contact_bots(tmp_path: Path) -> None:
    """MAX → Telegram is the contact bot's own job and has nothing to do with the
    owner's session. A reader of `owner_ingress_ready` inside the delivery or
    placement path would stop a contact's message on an unrelated fault."""
    import ast

    from bridge.service import runtime as runtime_module

    tree = ast.parse(Path(runtime_module.__file__).read_text(encoding="utf-8"))
    readers = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "owner_ingress_ready"
    ]
    # Defined once, and read nowhere that carries a message.
    assert len(readers) <= 1, f"readiness is consulted in the delivery path: {readers}"


def test_no_configuration_makes_the_session_optional(tmp_path: Path) -> None:
    """`extra=forbid` is what enforces it: a key invented to turn the puppet
    session off does not quietly do nothing, it stops the service on that line."""
    import pytest as _pytest

    from bridge.config import load_config

    config = tmp_path / "config.yaml"
    config.write_text(
        "paths:\n  data_dir: ./data\n"
        "telegram:\n  owner_user_id: 1\n  timezone: Europe/Moscow\n"
        "  owner_session_optional: true\n"
        "provisioning:\n  guardian_bot_token_env: TG\n",
        encoding="utf-8",
    )
    with _pytest.raises(Exception, match=r"owner_session_optional|Extra inputs"):
        load_config(config, load_env_file=False)


# ------------------------------------------------------- what the words claim


def test_the_status_line_does_not_claim_the_bridges_stopped(tmp_path: Path) -> None:
    """It used to say «Мосты приостановлены», which was both frightening and
    false: MAX→Telegram runs on the contact bots and never touches this
    session."""
    service = _service(tmp_path)
    line = "Действия владельца приостановлены: Telegram-сессия недоступна"
    assert not service.owner_ingress_ready

    from bridge.service import runtime as runtime_module

    source = Path(runtime_module.__file__).read_text(encoding="utf-8")
    assert line in source
    assert "Мосты приостановлены" not in source


async def test_the_incident_says_which_half_is_paused(database: Database) -> None:
    """Naming what still works is the difference between "one transport is down"
    and "everything stopped"."""
    service = make_service(database, OwnerSessionFacts(started=True, status="unauthorized"))
    await service.evaluate(await service.snapshot())
    rows = await database.query("SELECT text FROM alert_outbox ORDER BY id DESC LIMIT 1")
    body = rows[0]["text"].lower()

    assert "приостановлено только" in body  # the owner's half, named as such
    assert "контактов приходят" in body  # ...and what keeps working
    assert "очередь" in body


def test_readiness_is_named_after_the_ingress_not_the_bridge() -> None:
    """The word does the work here: `owner_ingress_ready` cannot be read as "the
    bridge is down", which `owner_data_plane_ready` could and did."""
    from bridge.service import health, runtime

    assert hasattr(health, "OwnerIngress")
    assert not hasattr(health, "OwnerDataPlane")
    assert hasattr(runtime.BridgeService, "owner_ingress_ready")
    assert not hasattr(runtime.BridgeService, "owner_data_plane_ready")
