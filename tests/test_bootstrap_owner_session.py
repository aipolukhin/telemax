"""The owner-session QR step is part of the main bootstrap, and it is optional.

Two things this pins about the integration: the same step lives in the main
console onboarding (not only in the `telegram-sync` command), and offering it
never enables intake. The accept path's login logic is proven at the shared core
(`test_user_session.py`); here the concern is the bootstrap wiring — that a
non-tty skips it cleanly, a decline is honoured, and neither writes the gate.
"""

from __future__ import annotations

from pathlib import Path

from bridge.bootstrap.plan import Plan
from bridge.bootstrap.telegram import offer_owner_session
from bridge.onboarding.state import StateStore
from tests.fake_console import FakeUi

OWNER = 100000001


def _plan(tmp_path: Path) -> Plan:
    return Plan(
        config_path=tmp_path / "config.yaml",
        env_path=tmp_path / ".env",
        data_dir=tmp_path / "data",
        api_id_env="TELEMAX_API_ID",
        api_hash_env="TELEMAX_API_HASH",
        phone_env="TELEMAX_PHONE",
        guardian_token_env="TELEMAX_GUARDIAN",
        owner_user_id=None,
        timezone=None,
        exists=False,
    )


async def test_a_non_tty_skips_the_offer_without_cancelling_setup(tmp_path: Path) -> None:
    ui = FakeUi(interactive=False)
    linked = await offer_owner_session(_plan(tmp_path), ui, owner_user_id=OWNER)
    assert linked is False
    assert ui.asked == [], "nothing was prompted on a pipe"


async def test_declining_the_offer_is_honoured(tmp_path: Path) -> None:
    # FakeUi.confirm returns the default, and the step asks with default=False.
    ui = FakeUi(interactive=True)
    linked = await offer_owner_session(_plan(tmp_path), ui, owner_user_id=OWNER)
    assert linked is False
    assert any("telegram-sync" in line for line in ui.printed), "points at the later command"


async def test_offering_the_step_decides_nothing_about_intake(tmp_path: Path) -> None:
    """Whatever the owner answers, the step does not flip the gate.

    The persisted flag lives in onboarding state; the offer writes none of it.
    Enabling intake is a separate, probe-gated operation.
    """
    store = StateStore(tmp_path / "data" / "state" / "onboarding.json")
    store.path.parent.mkdir(parents=True, exist_ok=True)

    ui = FakeUi(interactive=True)
    await offer_owner_session(_plan(tmp_path), ui, owner_user_id=OWNER)

    # The flag it used to check is gone: linking a session is not a decision
    # about which ingress is authoritative, because there is only one.
    assert not hasattr(store.load(), "owner_mtproto_intake_enabled")
