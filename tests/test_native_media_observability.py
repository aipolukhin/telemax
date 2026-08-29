"""AU-2 F4/F11 — native media that degrades has to say so, and be switchable.

Before this, a MAX that stopped accepting voice messages produced exactly one
WARNING, once per process, and nothing else: no counter, no `/status` line, no
incident. Every voice after that quietly arrived as a file. The realistic
outcome was weeks.

The hard part is not counting — it is deciding what deserves waking somebody.
One file with no audio track, one codec PyAV will not open, one duration that
read as zero: those are the design working, and an alert for each turns the
channel into noise nobody reads. A tripped breaker, an uploader we have no path
for, or no decoder at all are the opposite — they are true of every message from
now on, and nobody would notice unaided.

The other half is being able to do something about it without a deploy. The
flags were module constants, and they were flipped three times in six hours the
night the protocol was worked out — each flip a commit and a restart, mid
incident.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from bridge.max_client.native_state import NativeMediaState
from bridge.service.health import HealthService
from bridge.service.runtime import _native_media_lines
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


def make_service(
    database: Database,
    state: NativeMediaState | None = None,
) -> HealthService:
    return HealthService(
        health=HealthStateRepository(database),
        outbox=OutboxRepository(database),
        inbox=TelegramInboxRepository(database),
        alerts=AlertRepository(database),
        db_path=Path(database.path),
        bridges=lambda: [("dad", True)],
        max_is_ready=lambda: True,
        native_media=(lambda: state.snapshot()) if state is not None else None,
    )


# ------------------------------------------------------------------- config


def test_the_defaults_are_what_production_already_does() -> None:
    """This commit must not change a single delivery on the running bridge."""
    from bridge.config.models import MaxConfig

    native = MaxConfig().native_media
    assert (native.enabled, native.voice_enabled, native.circle_enabled) == (True, True, True)


def test_a_config_without_the_section_still_loads() -> None:
    """Backwards compatible: the running config.local.yaml has no `native_media`."""
    from bridge.config.models import MaxConfig

    parsed = MaxConfig.model_validate({"session_name": "max-session.db"})
    assert parsed.native_media.enabled


def test_each_kind_switches_off_on_its_own() -> None:
    """The asymmetry the constants had — voice could be switched off, circle
    could not — is gone. The server validates them apart, so the switch does."""
    voice_off = NativeMediaState(voice_enabled=False)
    assert not voice_off.allows("voice")
    assert voice_off.allows("circle")

    circle_off = NativeMediaState(circle_enabled=False)
    assert circle_off.allows("voice")
    assert not circle_off.allows("circle")

    both_off = NativeMediaState(enabled=False)
    assert not both_off.allows("voice")
    assert not both_off.allows("circle")


def test_a_disabled_kind_never_asks_for_an_upload_slot(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The operator switch has to be real, not cosmetic: op82 is a round trip to
    MAX, and a kind that is off must not make it."""
    monkeypatch.setattr(
        "bridge.media.native_max.probe_media", lambda path, kind="voice": (1000, bytes(80))
    )
    from test_native_media_faults import Rig

    rig = Rig(tmp_path, state=NativeMediaState(enabled=False))
    assert rig.send() == 9  # still delivered, plainly
    assert rig.invokes == []


# ---------------------------------------------------------------- counters


async def test_the_snapshot_carries_a_row_per_kind(database: Database) -> None:
    state = NativeMediaState()
    state.note_attempt("voice")
    state.note_success("voice")
    state.note_attempt("circle")
    state.note_ordinary_fallback("circle")

    snapshot = await make_service(database, state).snapshot()
    by_kind = {status.kind: status for status in snapshot.native_media}

    assert by_kind["voice"].successes == 1
    assert by_kind["circle"].ordinary_fallbacks == 1
    assert by_kind["voice"].attempts == 1


async def test_a_service_with_no_max_client_says_nothing_rather_than_broken(
    database: Database,
) -> None:
    """The setup CLI and most tests build health without a MAX client at all."""
    snapshot = await make_service(database).snapshot()
    assert snapshot.native_media == ()
    assert snapshot.native_media_systemic == ()


# ---------------------------------------------------------------- incidents


async def test_a_tripped_breaker_opens_the_incident(database: Database) -> None:
    state = NativeMediaState()
    state.trip("voice", "Invalid media wave")
    service = make_service(database, state)

    touched = await service.evaluate(await service.snapshot())
    assert "native-media-degraded" in touched


async def test_uploader_drift_opens_the_incident(database: Database) -> None:
    """The silent one: circles quietly become ordinary videos and nothing else
    in the system changes."""
    state = NativeMediaState()
    state.note_uploader_drift("circle")
    service = make_service(database, state)

    assert "native-media-degraded" in await service.evaluate(await service.snapshot())


async def test_a_missing_decoder_opens_the_incident(
    database: Database, monkeypatch: Any
) -> None:
    """No PyAV means every voice and every circle degrades, for every message —
    which the per-kind counters alone cannot distinguish from bad luck."""
    monkeypatch.setattr("bridge.service.health.decoder_available", lambda: False)
    state = NativeMediaState()
    state.note_attempt("voice")
    service = make_service(database, state)

    snapshot = await service.snapshot()
    assert not snapshot.native_media_decoder
    assert "native-media-degraded" in await service.evaluate(snapshot)
    assert snapshot.native_media_state(snapshot.native_media[0]) == "dependency-unavailable"


async def test_one_unreadable_file_is_not_an_incident(database: Database) -> None:
    """A voice note with a codec this install cannot open degrades, and that is
    the design working. Twenty of them are twenty messages that arrived."""
    state = NativeMediaState()
    for _ in range(20):
        state.note_attempt("voice")
        state.note_ordinary_fallback("voice")
    service = make_service(database, state)

    snapshot = await service.snapshot()
    assert snapshot.native_media_systemic == ()
    assert "native-media-degraded" not in await service.evaluate(snapshot)


async def test_an_unconfirmed_send_is_not_a_native_media_incident(
    database: Database,
) -> None:
    """It already has one: the job is AMBIGUOUS, and `delivery-ambiguous` is the
    incident that asks the owner to look. Two alerts for one event is noise."""
    state = NativeMediaState()
    state.note_unconfirmed("voice")
    service = make_service(database, state)

    assert "native-media-degraded" not in await service.evaluate(await service.snapshot())


async def test_a_kind_the_operator_switched_off_is_not_an_incident(
    database: Database,
) -> None:
    """Somebody already knows: they typed it into the config."""
    state = NativeMediaState(voice_enabled=False)
    state.trip("voice", "Invalid media wave")
    service = make_service(database, state)

    assert "native-media-degraded" not in await service.evaluate(await service.snapshot())


async def test_the_incident_closes_when_the_kind_works_again(database: Database) -> None:
    """A fresh process with the same config and no repeat of the cause: the
    breaker is per process, so a restart that stays healthy resolves it."""
    broken = NativeMediaState()
    broken.trip("voice", "Invalid media wave")
    service = make_service(database, broken)
    await service.evaluate(await service.snapshot())

    healthy = NativeMediaState()
    healthy.note_attempt("voice")
    healthy.note_success("voice")
    after_restart = make_service(database, healthy)

    touched = await after_restart.evaluate(await after_restart.snapshot())
    assert "native-media-degraded:resolved" in touched


async def test_turning_the_kind_off_also_closes_the_incident(database: Database) -> None:
    """The other way to answer an alert: decide not to send them natively."""
    broken = NativeMediaState()
    broken.trip("circle", "proto.payload")
    service = make_service(database, broken)
    await service.evaluate(await service.snapshot())

    switched_off = make_service(database, NativeMediaState(enabled=False))
    touched = await switched_off.evaluate(await switched_off.snapshot())
    assert "native-media-degraded:resolved" in touched


async def test_the_incident_text_carries_no_message_content(database: Database) -> None:
    """A MAX refusal's message can hold a chat title or a person's name. The
    reason stored and shown is the error code."""
    state = NativeMediaState()
    state.trip("voice", "chat.blocked")
    service = make_service(database, state)
    snapshot = await service.snapshot()

    text = service._native_media_text(snapshot)
    assert "chat.blocked" in text
    assert "/status" in text
    assert "голосовые" in text


# ------------------------------------------------------------------ /status


async def test_status_tells_the_six_states_apart(database: Database) -> None:
    healthy = NativeMediaState()
    healthy.note_attempt("voice")
    healthy.note_success("voice")
    snapshot = await make_service(database, healthy).snapshot()
    assert any("как есть" in line for line in _native_media_lines(snapshot))

    off = await make_service(database, NativeMediaState(voice_enabled=False)).snapshot()
    assert any("выключено вами" in line for line in _native_media_lines(off))

    tripped = NativeMediaState()
    tripped.trip("voice", "Invalid media wave")
    snapshot = await make_service(database, tripped).snapshot()
    lines = _native_media_lines(snapshot)
    assert any("MAX отклонил" in line for line in lines)
    assert any("Invalid media wave" in line for line in lines)

    drifted = NativeMediaState()
    drifted.note_uploader_drift("circle")
    snapshot = await make_service(database, drifted).snapshot()
    assert any("не тот загрузчик" in line for line in _native_media_lines(snapshot))


async def test_status_stays_quiet_while_nothing_has_happened(database: Database) -> None:
    """`/status` is read on a phone. A block that always says "fine" trains
    people to skip the one that will not."""
    snapshot = await make_service(database, NativeMediaState()).snapshot()
    assert _native_media_lines(snapshot) == []


async def test_status_shows_the_counters_once_something_has(database: Database) -> None:
    state = NativeMediaState()
    for _ in range(3):
        state.note_attempt("voice")
    state.note_success("voice")
    state.note_ordinary_fallback("voice")
    state.note_unconfirmed("voice")

    lines = _native_media_lines(await make_service(database, state).snapshot())
    joined = "\n".join(lines)
    assert "1/3" in joined
    assert "файлом 1" in joined
    assert "неясно 1" in joined


@pytest.mark.parametrize("kind", ["voice", "circle"])
async def test_a_dependency_outage_is_reported_for_every_enabled_kind(
    database: Database, monkeypatch: Any, kind: str
) -> None:
    monkeypatch.setattr("bridge.service.health.decoder_available", lambda: False)
    state = NativeMediaState()
    state.note_attempt(kind)
    snapshot = await make_service(database, state).snapshot()

    assert any("нет декодера" in line for line in _native_media_lines(snapshot))
