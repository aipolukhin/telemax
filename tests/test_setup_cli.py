"""`bridge setup` — the console bootstrap, and everything it must stop doing.

The old flow asked for a timezone by hand, logged in to MAX in the terminal,
and finished by telling the owner to run three more commands. Half of these
tests are the negative of that: no MAX prompt, no "now run", no config left
behind after a Ctrl+C.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bridge.bootstrap import flow
from bridge.bootstrap.plan import read_plan
from bridge.bootstrap.systemd import ServiceError
from bridge.bootstrap.telegram import TelegramAccount
from bridge.config import load_config
from bridge.config.writer import (
    atomic_write_text,
    set_config_value,
    set_env_value,
    write_bootstrap_config,
    write_timezone,
)
from bridge.onboarding.state import StateStore
from tests.fake_console import FakeUi

CONFIG = """\
# Personal bridge.
paths:
  data_dir: ./data

telegram:
  # Your own Telegram user id.
  owner_user_id: 100000001

max:
  session_name: max-session.db
"""

API_HASH = "0" * 32
GUARDIAN_TOKEN = "9000000003:AAF-dummy-token-value-for-tests-0000"

ANSWERS = {
    "API ID": "28972505",
    "API Hash": API_HASH,
}


# ---------------------------------------------------------------- writing files


def test_the_key_lands_inside_the_telegram_section(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")

    assert write_timezone(path, "Asia/Almaty")

    lines = path.read_text(encoding="utf-8").splitlines()
    assert "  timezone: Asia/Almaty" in lines
    assert lines.index("  timezone: Asia/Almaty") == lines.index("  owner_user_id: 100000001") + 1
    assert lines[lines.index("max:") - 1] == "", "the blank line before the next section stays"


def test_comments_survive(tmp_path: Path) -> None:
    """A YAML round-trip would eat them, which is why this is a text edit."""
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")

    write_timezone(path, "Europe/Moscow")

    text = path.read_text(encoding="utf-8")
    assert "# Personal bridge." in text
    assert "# Your own Telegram user id." in text


def test_an_existing_value_is_replaced_not_duplicated(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG.replace("  owner_user_id", "  timezone: UTC\n  owner_user_id"), "utf-8")

    write_timezone(path, "Europe/Moscow")

    text = path.read_text(encoding="utf-8")
    assert text.count("timezone:") == 1
    assert "timezone: Europe/Moscow" in text


def test_a_config_without_a_telegram_section_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("paths:\n  data_dir: ./data\n", encoding="utf-8")

    assert write_timezone(path, "Europe/Moscow") is False


def test_any_section_can_be_written(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG + '\nprovisioning:\n  mode: "off"\n', encoding="utf-8")

    assert set_config_value(path, "provisioning", "mode", "auto_mtproto")
    assert set_config_value(path, "provisioning", "guardian_bot_token_env", "TELEMAX_GUARDIAN")

    text = path.read_text(encoding="utf-8")
    assert "  mode: auto_mtproto" in text
    assert "  guardian_bot_token_env: TELEMAX_GUARDIAN" in text
    assert text.count("mode:") == 1


def test_a_failed_write_leaves_the_old_config_intact(tmp_path: Path, monkeypatch: Any) -> None:
    """The whole reason the write is atomic."""
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")

    def explode(*_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(OSError, match="disk full"):
        atomic_write_text(path, "ruined")

    assert path.read_text(encoding="utf-8") == CONFIG
    leftovers = [item.name for item in tmp_path.iterdir() if item.name != "config.yaml"]
    assert leftovers == [], "a failed write must not leave a temp file behind"


def test_the_config_is_written_at_0600(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    write_bootstrap_config(
        path, owner_user_id=42, timezone="Europe/Moscow", guardian_token_env="TELEMAX_GUARDIAN"
    )
    assert (path.stat().st_mode & 0o777) == 0o600


def test_the_bootstrap_config_actually_loads(tmp_path: Path, monkeypatch: Any) -> None:
    """A config setup writes must pass the loader the service uses."""
    path = tmp_path / "config.yaml"
    write_bootstrap_config(
        path,
        owner_user_id=100000001,
        timezone="Europe/Moscow",
        guardian_token_env="TELEMAX_GUARDIAN",
        data_dir=str(tmp_path / "data"),
    )

    loaded = load_config(path, load_env_file=False)
    assert loaded.app.telegram.owner_user_id == 100000001
    assert loaded.app.telegram.timezone == "Europe/Moscow"
    assert loaded.app.provisioning.guardian_bot_token_env == "TELEMAX_GUARDIAN"


def test_a_secret_lands_in_env_at_0600_and_never_in_the_config(tmp_path: Path) -> None:
    """The config is meant to be readable and shareable; a token is not."""
    env = tmp_path / ".env"
    set_env_value(env, "TELEMAX_API_HASH", "s3cret")

    assert env.read_text(encoding="utf-8").strip() == "TELEMAX_API_HASH=s3cret"
    assert (env.stat().st_mode & 0o777) == 0o600
    assert os.environ["TELEMAX_API_HASH"] == "s3cret", "the running process needs it too"

    set_env_value(env, "TELEMAX_API_HASH", "rotated")
    assert env.read_text(encoding="utf-8").count("TELEMAX_API_HASH") == 1
    os.environ.pop("TELEMAX_API_HASH", None)


# ------------------------------------------------------------------ the whole run


@dataclass
class FakeSession:
    saved: list[str]

    async def send_to_saved(self, text: str) -> bool:
        self.saved.append(text)
        return True

    async def close(self) -> None:
        return None

    async def send_start(self, username: str, bot_id: int | None = None) -> None:
        return None


@dataclass
class FakeServiceManager:
    installed: list[Any]
    uninstalled: int = 0
    stopped: int = 0
    fail_with: Exception | None = None

    def stop(self) -> bool:
        self.stopped += 1
        return True

    def install(self, spec: Any) -> Path:
        if self.fail_with is not None:
            raise self.fail_with
        self.installed.append(spec)
        return Path("/tmp/telemax.service")  # noqa: S108 - never touched

    def uninstall(self) -> None:
        self.uninstalled += 1


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: Any) -> Any:
    """A setup run with Telegram, systemd and the handoff all replaced."""
    for name in ("TELEMAX_API_ID", "TELEMAX_API_HASH", "TELEMAX_PHONE", "TELEMAX_GUARDIAN_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    session = FakeSession(saved=[])
    account = TelegramAccount(
        session=session,  # type: ignore[arg-type]
        owner_user_id=100000001,
        guardian_username="telemax_guard_bot",
        guardian_token=GUARDIAN_TOKEN,
    )
    delivered: list[str] = []

    async def fake_connect(plan: Any, ui: Any, *, journal: Any = None) -> Any:
        # Ask exactly what the real one asks, so the transcript is honest.
        from bridge.bootstrap.telegram import connect_account
        from bridge.telegram.user_session import AuthorizedOwner, ConnectedOwner

        async def scan(**_: Any) -> ConnectedOwner:
            return ConnectedOwner(
                owner=AuthorizedOwner(
                    account_id=account.owner_user_id,
                    name="Owner",
                    username="owner",
                ),
                session=session,  # type: ignore[arg-type]
            )

        monkeypatch.setattr("bridge.bootstrap.telegram.connect_owner_session", scan)
        return await connect_account(plan, ui, journal=journal)

    async def fake_guardian(
        plan: Any, ui: Any, _session: Any, **_: Any
    ) -> TelegramAccount:
        ui.ok(f"Бот-страж: @{account.guardian_username}")
        return account

    async def fake_deliver(plan: Any, ui: Any, _account: Any) -> Any:
        from bridge.bootstrap.handoff import Delivery, Handoff

        StateStore.for_data_dir(plan.data_dir).update(owner_user_id=account.owner_user_id)
        delivered.append("saved")
        return Handoff(
            delivery=Delivery.SAVED_MESSAGES,
            link="https://t.me/telemax_guard_bot?start=xxx",
            bot_username=account.guardian_username,
        )

    monkeypatch.setattr(flow, "connect_account", fake_connect)
    monkeypatch.setattr(flow, "ensure_guardian", fake_guardian)
    monkeypatch.setattr(flow, "_deliver_link", fake_deliver)

    manager = FakeServiceManager(installed=[])
    plan = read_plan(tmp_path / "config.yaml")
    return plan, manager, delivered


def test_the_console_never_asks_about_max(sandbox: Any) -> None:
    """The regression this redesign is for: MAX moved into the bot."""
    plan, manager, _ = sandbox
    ui = FakeUi(answers=ANSWERS)

    code = asyncio.run(flow.bootstrap(plan, ui, manager=manager))

    assert code == 0
    joined = " ".join(ui.asked).lower()
    assert "max" not in joined
    assert "код" not in joined, "no MAX code is ever typed in the terminal"
    assert "диалог" not in joined, "dialogs are chosen in the bot"


def test_the_console_never_asks_about_the_timezone(sandbox: Any) -> None:
    """A server terminal cannot know what the owner's own clock says."""
    plan, manager, _ = sandbox
    ui = FakeUi(answers=ANSWERS)

    code = asyncio.run(flow.bootstrap(plan, ui, manager=manager))

    assert code == 0
    joined = " ".join(ui.asked).lower()
    assert "пояс" not in joined
    assert ui.choices_shown == [], "no menu at all is left in the console"


def test_a_fresh_config_leaves_the_timezone_for_the_bot_to_ask(sandbox: Any) -> None:
    plan, manager, _ = sandbox
    ui = FakeUi(answers=ANSWERS)

    asyncio.run(flow.bootstrap(plan, ui, manager=manager))

    text = plan.config_path.read_text(encoding="utf-8")
    assert "timezone: null" in text
    # And it still loads: `null` is a value the loader accepts, not a hole.
    assert load_config(plan.config_path, load_env_file=False).app.telegram.timezone is None


def test_the_last_screen_asks_for_nothing_more(sandbox: Any) -> None:
    plan, manager, _ = sandbox
    ui = FakeUi(answers=ANSWERS)

    asyncio.run(flow.bootstrap(plan, ui, manager=manager))

    transcript = ui.transcript
    assert "Продолжите в Telegram" in transcript
    assert "Можно закрыть терминал." in transcript
    for banned in ("validate-config", "bridge run", "/dialogs", "Сохранить конфигурацию"):
        assert banned not in transcript, f"the console still tells the owner to {banned}"


def test_no_credential_is_ever_printed(sandbox: Any) -> None:
    plan, manager, _ = sandbox
    ui = FakeUi(answers=ANSWERS)

    asyncio.run(flow.bootstrap(plan, ui, manager=manager))

    assert API_HASH not in ui.transcript
    assert GUARDIAN_TOKEN not in ui.transcript
    assert any(question.startswith("API Hash") for question in ui.hidden), "api_hash is hidden"


def test_the_service_is_installed_before_the_handoff(sandbox: Any) -> None:
    """A link to a bot that is not running is a dead end."""
    plan, manager, delivered = sandbox
    ui = FakeUi(answers=ANSWERS)

    asyncio.run(flow.bootstrap(plan, ui, manager=manager))

    assert manager.installed, "the unit must be installed"
    assert delivered == ["saved"]
    order = ui.transcript.index("Сервис Telemax запущен")
    assert order < ui.transcript.index("Продолжите в Telegram")


def test_docker_setup_prepares_state_without_touching_systemd(sandbox: Any) -> None:
    plan, manager, delivered = sandbox
    ui = FakeUi(answers=ANSWERS)

    code = asyncio.run(
        flow.bootstrap(plan, ui, manager=manager, install_runtime=False)
    )

    assert code == 0
    assert manager.installed == []
    assert delivered == ["saved"]
    assert "Конфигурация готова" in ui.transcript
    assert "Сервис Telemax запущен" not in ui.transcript


def test_docker_manual_guardian_also_avoids_systemd(
    tmp_path: Path, monkeypatch: Any
) -> None:
    plan = read_plan(tmp_path / "config.yaml")
    manager = FakeServiceManager(installed=[])
    ui = FakeUi(answers=[])

    async def fake_plan(*_: Any, **__: Any) -> tuple[None, None]:
        return None, None

    async def fake_adopt(*_: Any, **__: Any) -> Any:
        return SimpleNamespace(
            owner_user_id=100000001,
            username="telemax_guard_bot",
            token=GUARDIAN_TOKEN,
        )

    monkeypatch.setattr(flow, "_guardian_plan", fake_plan)
    monkeypatch.setattr(flow, "adopt_guardian", fake_adopt)

    code = asyncio.run(
        flow.bootstrap_managed(
            plan,
            ui,
            manager=manager,
            adopt=True,
            install_runtime=False,
        )
    )

    assert code == 0
    assert manager.installed == []
    assert "Конфигурация готова" in ui.transcript


def test_ctrl_c_leaves_nothing_behind(sandbox: Any) -> None:
    plan, manager, _ = sandbox
    ui = FakeUi(answers=ANSWERS, cancel_on={"API ID"})

    code = asyncio.run(flow.bootstrap(plan, ui, manager=manager))

    assert code == 130
    assert "Настройка отменена. Изменения не сохранены." in ui.transcript
    assert "Traceback" not in ui.transcript
    assert not plan.config_path.exists(), "a cancelled setup writes no config"
    assert manager.installed == []


def test_a_cancel_at_the_prompts_takes_the_credentials_back(sandbox: Any) -> None:
    """"Изменения не сохранены" has to be literally true."""
    plan, manager, _ = sandbox
    ui = FakeUi(answers=ANSWERS, cancel_on={"API Hash"})

    code = asyncio.run(flow.bootstrap(plan, ui, manager=manager))

    assert code == 130
    assert not plan.env_path.exists(), "the api_hash must not survive a Ctrl+C"
    assert os.environ.get(plan.api_hash_env) is None


def test_a_cancel_after_the_service_rolls_it_back(sandbox: Any, monkeypatch: Any) -> None:
    plan, manager, _ = sandbox
    ui = FakeUi(answers=ANSWERS)

    from bridge.bootstrap.ui import SetupCancelled

    async def cancel(*_: object, **__: object) -> Any:
        raise SetupCancelled

    monkeypatch.setattr(flow, "_deliver_link", cancel)
    code = asyncio.run(flow.bootstrap(plan, ui, manager=manager))

    assert code == 130
    assert manager.uninstalled == 1, "a half-finished install is undone"
    assert not plan.config_path.exists()


def test_a_service_that_will_not_start_is_not_called_a_success(sandbox: Any) -> None:
    """Reporting a running service that is not running is the worst outcome."""
    plan, manager, delivered = sandbox
    manager.fail_with = ServiceError("systemctl --user недоступен")
    ui = FakeUi(answers=ANSWERS)

    code = asyncio.run(flow.bootstrap(plan, ui, manager=manager))

    assert code == 1
    assert delivered == [], "the link is never sent before the service is up"
    assert "systemctl --user недоступен" in ui.transcript
    assert "Продолжите в Telegram" not in ui.transcript


def test_a_second_run_does_not_ask_again(tmp_path: Path, monkeypatch: Any) -> None:
    """The credentials are in .env from the first run; re-asking is a bug."""
    from bridge.bootstrap.flow import _load_existing_env
    from bridge.bootstrap.plan import read_plan

    config = tmp_path / "config.yaml"
    plan = read_plan(config)
    for name in (plan.api_id_env, plan.api_hash_env, plan.phone_env):
        monkeypatch.delenv(name, raising=False)
    plan.env_path.write_text(f"{plan.api_id_env}=28972505\n", encoding="utf-8")

    _load_existing_env(plan)
    assert os.environ[plan.api_id_env] == "28972505"


def test_a_second_deployment_does_not_inherit_the_first(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A .env in the working directory is not this config's .env."""
    from bridge.bootstrap.flow import _load_existing_env
    from bridge.bootstrap.plan import read_plan

    (tmp_path / "here").mkdir()
    (tmp_path / "here" / ".env").write_text("TELEMAX_API_ID=1111111\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path / "here")
    monkeypatch.delenv("TELEMAX_API_ID", raising=False)

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _load_existing_env(read_plan(elsewhere / "config.yaml"))

    assert "TELEMAX_API_ID" not in os.environ


def test_an_existing_config_is_edited_not_replaced(sandbox: Any) -> None:
    plan, manager, _ = sandbox
    plan.config_path.write_text(CONFIG, encoding="utf-8")
    plan = read_plan(plan.config_path)
    ui = FakeUi(answers=ANSWERS)

    asyncio.run(flow.bootstrap(plan, ui, manager=manager))

    text = plan.config_path.read_text(encoding="utf-8")
    assert "# Personal bridge." in text, "somebody's comments are not ours to delete"
    assert "timezone:" not in text, "an existing config keeps whatever it already said"
    assert "owner_user_id: 100000001" in text


def test_setup_command_defaults_to_qr_and_manual_guardian_is_explicit(
    monkeypatch: Any,
) -> None:
    from bridge import __main__ as entrypoint

    calls: list[dict[str, Any]] = []

    def capture(_path: Any, **kwargs: Any) -> int:
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(entrypoint.setup_cli, "run", capture)

    assert entrypoint.main(["setup"]) == 0
    assert calls[-1]["use_session"] is True

    assert entrypoint.main(["setup", "--manual-guardian"]) == 0
    assert calls[-1]["use_session"] is False
