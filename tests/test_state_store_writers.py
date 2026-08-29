"""Two writers over `onboarding.json`, and neither may roll the other back.

This is not a timing test and there is nothing to race. `update()` contains no
`await`, asyncio is single threaded, and nothing runs a store in a thread — so
two writes never interleave. What went wrong was quieter than that: `load()`
answers from a per-instance cache, which is right for a reader and wrong for a
writer, and there were two live stores over one file. The runtime held one (the
status board, the state machine, the onboarding router); the notification centre
built a second per alert. The first cached a record from before the map existed,
and its next write — a screen revision bumped by any confirmation screen — put
that record back on disk.

The consequence was not a lost preference. The notification map went, the
recovery for the open incident was dropped for want of a message id, and the red
message stayed in the chat for ever. The map holds one entry now — the guardian's
single attention push — and losing it costs the same thing: a push nothing will
ever turn green.

Two defences, and both are tested here: the serving runtime shares one store,
and `update()` re-reads whatever is on disk before it changes anything.
"""

from __future__ import annotations

import json
from pathlib import Path

from bridge.onboarding.state import Stage, StateStore, Step


def test_a_second_store_does_not_roll_the_first_one_back(tmp_path: Path) -> None:
    """The invariant, in the order that used to break it.

    A writes X · B writes Y · A writes Z — and all three survive.
    """
    a = StateStore.for_data_dir(tmp_path)
    b = StateStore.for_data_dir(tmp_path)

    a.update(status_message_id=1001)
    b.update(notifications={"connectivity": 4242})
    a.update(screen_revision=7)

    fresh = StateStore.for_data_dir(tmp_path).load()

    assert fresh.status_message_id == 1001, "A's first field"
    assert fresh.notifications == {"connectivity": 4242}, "B's field, previously lost"
    assert fresh.screen_revision == 7, "A's second field"


def test_a_notification_map_survives_a_later_guardian_write(tmp_path: Path) -> None:
    """The exact production sequence: an alert lands, then the owner taps."""
    guardian = StateStore.for_data_dir(tmp_path)
    guardian.update(stage=Stage.BRIDGE_RUNNING, status_message_id=500)
    alerts = StateStore.for_data_dir(tmp_path)

    alerts.update(notifications={"connectivity": 4242})
    # Every confirmation screen bumps the revision, twice: once when drawn and
    # once when its button is honoured.
    guardian.update(screen_revision=guardian.load().screen_revision + 1)
    guardian.update(mirror_own_messages=True)

    fresh = StateStore.for_data_dir(tmp_path).load()

    assert fresh.notifications == {"connectivity": 4242}
    assert fresh.mirror_own_messages is True
    assert fresh.stage is Stage.BRIDGE_RUNNING


def test_guardian_state_survives_a_notification_write(tmp_path: Path) -> None:
    """The other direction, which happened to work and must keep working."""
    guardian = StateStore.for_data_dir(tmp_path)
    guardian.update(
        stage=Stage.BRIDGE_RUNNING, status_chat_id=7, status_message_id=500,
        mirror_own_messages=True,
    )

    StateStore.for_data_dir(tmp_path).update(notifications={"storage": 9})

    fresh = StateStore.for_data_dir(tmp_path).load()

    assert fresh.status_message_id == 500
    assert fresh.status_chat_id == 7
    assert fresh.mirror_own_messages is True
    assert fresh.stage is Stage.BRIDGE_RUNNING


def test_a_write_reads_what_is_on_disk_not_what_it_cached(tmp_path: Path) -> None:
    """The mechanism, stated on its own so the reason cannot be refactored out."""
    stale = StateStore.for_data_dir(tmp_path)
    stale.update(screen_revision=1)
    assert stale.load().notifications == {}, "its cache has no map"

    # Somebody else — another store, or a hand-edit between two writes.
    (tmp_path / "state" / "onboarding.json").write_text(
        json.dumps({"screen_revision": 1, "notifications": {"media": 77}}),
        encoding="utf-8",
    )

    stale.update(screen_revision=2)

    assert StateStore.for_data_dir(tmp_path).load().notifications == {"media": 77}


def test_a_write_does_not_resurrect_an_ephemeral_step(tmp_path: Path) -> None:
    """`load()` rewinds a step that cannot survive a restart; a write must too.

    Re-reading from disk means reading the raw record, and the raw record can
    hold `waiting_for_code` — a step bound to a MAX auth attempt that died with
    the process. Writing it back would ask the owner for a code nobody can
    answer.
    """
    (tmp_path / "state").mkdir(parents=True)
    (tmp_path / "state" / "onboarding.json").write_text(
        json.dumps({"step": Step.WAITING_FOR_CODE.value}), encoding="utf-8"
    )

    StateStore.for_data_dir(tmp_path).update(screen_revision=3)

    assert StateStore.for_data_dir(tmp_path).load().step is Step.WAITING_FOR_PHONE


def test_the_atomic_write_contract_is_unchanged(tmp_path: Path) -> None:
    """Temp file in the destination directory, 0600, replaced in one step."""
    store = StateStore.for_data_dir(tmp_path)
    store.update(status_message_id=1)

    assert store.path.exists()
    assert store.path.stat().st_mode & 0o777 == 0o600
    leftovers = [one for one in store.path.parent.iterdir() if one.name.endswith(".tmp")]
    assert leftovers == [], "no litter beside the file"


async def test_the_notification_centre_writes_through_the_runtime_store(
    tmp_path: Path,
) -> None:
    """The real wiring, not a stand-in for it.

    A `BridgeService` that builds its own store is a second cache over one file,
    which is the whole of the defect above. Exercised at the seam because the
    failure it causes is three layers away and only visible hours later: the map
    is written, a guardian tap rolls it back, and the red notification the owner
    is looking at never clears.
    """
    from bridge.onboarding.views import AttentionFacts
    from bridge.service.notifications import PUSH_KEY, SUSTAINED_OUTAGE_MS
    from bridge.service.runtime import BridgeService

    store = StateStore.for_data_dir(tmp_path)
    guardian = _FakeGuardian()
    service = BridgeService(_minimal_config(tmp_path), state=store)

    assert service._state is store, "one store, handed in"

    # The guardian only ever posts under a condition that may interrupt the
    # owner. Which condition that is belongs to `test_quiet_guardian`; what is
    # checked here is *which store* the resulting message id is written through.
    async def outage() -> AttentionFacts:
        return AttentionFacts(
            worker_running=True,
            max_connected=False,
            max_offline_ms=SUSTAINED_OUTAGE_MS + 1,
        )

    service.attention_facts = outage  # type: ignore[method-assign]

    centre = service._notification_centre(guardian)  # type: ignore[arg-type]
    assert service._notification_centre(guardian) is centre, (  # type: ignore[arg-type]
        "built once — it holds the push and retires the old design's messages once"
    )

    await centre.publish(
        {"incident_key": "max-offline", "severity": "warning", "text": "…"}
    )
    assert store.load().notifications == {PUSH_KEY: guardian.sent[0]}

    # And now the tap that used to destroy it.
    store.update(screen_revision=store.load().screen_revision + 1)

    assert StateStore.for_data_dir(tmp_path).load().notifications == {
        PUSH_KEY: guardian.sent[0]
    }


class _FakeGuardian:
    """Records the message ids it hands back. No Telegram, no network."""

    def __init__(self) -> None:
        self.sent: list[int] = []
        self.bot = self

    async def send_message(self, **kwargs: object) -> object:
        self.sent.append(9000 + len(self.sent))
        return type("Sent", (), {"message_id": self.sent[-1]})()

    async def edit_message_text(self, **kwargs: object) -> None:
        return None


def _minimal_config(tmp_path: Path):  # type: ignore[no-untyped-def]
    """Just enough `LoadedConfig` to construct a service without starting one."""
    from bridge.config import load_config
    from tests.test_runtime_service import write_bootstrap_config

    config = tmp_path / "config.yaml"
    write_bootstrap_config(
        config,
        owner_user_id=1,
        timezone="Europe/Moscow",
        guardian_token_env="TG",
        data_dir=str(tmp_path),
    )
    return load_config(config, load_env_file=False)
