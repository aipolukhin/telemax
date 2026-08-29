"""WP1 — configuration loading and validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from bridge.config import ConfigError, load_config
from bridge.config.models import AutoRead, OutgoingTyping, ProvisioningMode

MINIMAL = {
    "telegram": {"owner_user_id": 42},
    "bridges": [
        {"name": "mom", "max_chat_id": 111, "telegram_bot_token_env": "TOK_MOM"},
    ],
}

VALID_TOKEN = "123456789:AAdummy-token-value-for-tests"


def write_config(tmp_path: Path, data: dict[str, object]) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("BRIDGE_CONFIG", "BRIDGE_DATA_DIR", "BRIDGE_LOG_LEVEL", "TOK_MOM", "TOK_DAD"):
        monkeypatch.delenv(name, raising=False)


def test_loads_minimal_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = dict(MINIMAL, paths={"data_dir": str(tmp_path / "data")})
    monkeypatch.setenv("TOK_MOM", VALID_TOKEN)

    loaded = load_config(write_config(tmp_path, data), load_env_file=False)

    assert loaded.app.telegram.owner_user_id == 42
    assert [bridge.name for bridge in loaded.bridges] == ["mom"]
    assert loaded.bridges[0].token.get_secret_value() == VALID_TOKEN
    # Defaults that the rest of the code relies on.
    assert loaded.app.presence.outgoing_typing is OutgoingTyping.MEDIA_ONLY
    # The contact's second tick follows what the owner actually opened.
    assert loaded.app.presence.auto_read is AutoRead.ON_READ
    assert loaded.app.provisioning.mode is ProvisioningMode.OFF
    # Off by default: a message the owner sends from the MAX app is not carried.
    assert loaded.app.own_messages.mirror is False


def test_own_message_mirroring_can_be_turned_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = dict(
        MINIMAL, paths={"data_dir": str(tmp_path / "data")}, own_messages={"mirror": True}
    )
    monkeypatch.setenv("TOK_MOM", VALID_TOKEN)

    loaded = load_config(write_config(tmp_path, data), load_env_file=False)

    assert loaded.app.own_messages.mirror is True


def test_token_is_not_in_the_repr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dumped config must never leak a token — this is what SecretStr buys us."""
    monkeypatch.setenv("TOK_MOM", VALID_TOKEN)
    data = dict(MINIMAL, paths={"data_dir": str(tmp_path / "data")})

    loaded = load_config(write_config(tmp_path, data), load_env_file=False)

    assert VALID_TOKEN not in repr(loaded.bridges[0])
    assert VALID_TOKEN not in str(loaded.bridges[0].model_dump())


def test_missing_token_names_the_variable(tmp_path: Path) -> None:
    data = dict(MINIMAL, paths={"data_dir": str(tmp_path / "data")})

    with pytest.raises(ConfigError) as error:
        load_config(write_config(tmp_path, data), load_env_file=False)

    assert "TOK_MOM" in str(error.value)
    assert "mom" in str(error.value)


def test_malformed_token_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOK_MOM", "not-a-token")
    data = dict(MINIMAL, paths={"data_dir": str(tmp_path / "data")})

    with pytest.raises(ConfigError, match="do not look like Telegram tokens"):
        load_config(write_config(tmp_path, data), load_env_file=False)


def test_duplicate_max_chat_id_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The core invariant of the project: one bot, one dialog."""
    monkeypatch.setenv("TOK_MOM", VALID_TOKEN)
    monkeypatch.setenv("TOK_DAD", VALID_TOKEN.replace("123", "999"))
    data = {
        "telegram": {"owner_user_id": 42},
        "paths": {"data_dir": str(tmp_path / "data")},
        "bridges": [
            {"name": "mom", "max_chat_id": 111, "telegram_bot_token_env": "TOK_MOM"},
            {"name": "dad", "max_chat_id": 111, "telegram_bot_token_env": "TOK_DAD"},
        ],
    }

    with pytest.raises(ConfigError, match="one bot must map to exactly one MAX dialog"):
        load_config(write_config(tmp_path, data), load_env_file=False)


def test_shared_token_variable_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOK_MOM", VALID_TOKEN)
    data = {
        "telegram": {"owner_user_id": 42},
        "paths": {"data_dir": str(tmp_path / "data")},
        "bridges": [
            {"name": "mom", "max_chat_id": 111, "telegram_bot_token_env": "TOK_MOM"},
            {"name": "dad", "max_chat_id": 222, "telegram_bot_token_env": "TOK_MOM"},
        ],
    }

    with pytest.raises(ConfigError, match="shared by"):
        load_config(write_config(tmp_path, data), load_env_file=False)


def test_unknown_key_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo in YAML must fail at startup, not silently do nothing."""
    monkeypatch.setenv("TOK_MOM", VALID_TOKEN)
    data = dict(MINIMAL, paths={"data_dir": str(tmp_path / "data")}, presenc={"mirror_typing": 1})

    with pytest.raises(ConfigError, match="presenc"):
        load_config(write_config(tmp_path, data), load_env_file=False)


def test_owner_user_id_must_be_positive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOK_MOM", VALID_TOKEN)
    data = dict(MINIMAL, telegram={"owner_user_id": 0}, paths={"data_dir": str(tmp_path / "data")})

    with pytest.raises(ConfigError, match="owner_user_id"):
        load_config(write_config(tmp_path, data), load_env_file=False)


def test_directories_are_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOK_MOM", VALID_TOKEN)
    data_dir = tmp_path / "fresh"
    data = dict(MINIMAL, paths={"data_dir": str(data_dir)})

    loaded = load_config(write_config(tmp_path, data), load_env_file=False)

    assert data_dir.is_dir()
    assert loaded.app.paths.resolved_temp_dir.is_dir()
    assert loaded.app.paths.resolved_media_cache_dir.is_dir()
    assert loaded.app.max_session_dir.is_dir()
    assert loaded.app.paths.db_path == data_dir / "bridge.db"


def test_unwritable_data_dir_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOK_MOM", VALID_TOKEN)
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    data = dict(MINIMAL, paths={"data_dir": str(locked / "data")})

    try:
        with pytest.raises(ConfigError, match=r"cannot create|not writable"):
            load_config(write_config(tmp_path, data), load_env_file=False)
    finally:
        locked.chmod(0o700)


def test_data_dir_env_overrides_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOK_MOM", VALID_TOKEN)
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path / "from-env"))
    data = dict(MINIMAL, paths={"data_dir": str(tmp_path / "from-file")})

    loaded = load_config(write_config(tmp_path, data), load_env_file=False)

    assert loaded.app.paths.data_dir == tmp_path / "from-env"


def test_warns_about_on_delivery_read_marks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legal, but it changes what the contact observes — say so once."""
    monkeypatch.setenv("TOK_MOM", VALID_TOKEN)
    data = dict(
        MINIMAL,
        paths={"data_dir": str(tmp_path / "data")},
        presence={"auto_read": "on_delivery"},
    )

    loaded = load_config(write_config(tmp_path, data), load_env_file=False)

    assert any("on_delivery" in warning for warning in loaded.warnings)


def test_warns_when_nothing_would_run(tmp_path: Path) -> None:
    data = {"telegram": {"owner_user_id": 42}, "paths": {"data_dir": str(tmp_path / "data")}}

    loaded = load_config(write_config(tmp_path, data), load_env_file=False)

    assert any("no bridges configured" in warning for warning in loaded.warnings)


def test_example_config_is_valid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The shipped example must actually load — it is the first thing anyone copies."""
    example = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    example["paths"]["data_dir"] = str(tmp_path / "data")
    monkeypatch.setenv("TELEMAX_BOT_MOM", VALID_TOKEN)
    monkeypatch.setenv("TELEMAX_BOT_DAD", VALID_TOKEN.replace("123", "999"))

    loaded = load_config(write_config(tmp_path, example), load_env_file=False)

    assert [bridge.name for bridge in loaded.bridges] == ["mom", "dad"]


# ------------------------------------------------------- removing a seeded bridge

SEEDED = """\
# Personal bridge.
telegram:
  owner_user_id: 1

bridges:
  - name: mama
    max_chat_id: 111
    telegram_bot_token_env: TELEMAX_BOT_MAMA
  - name: papa
    max_chat_id: 222
    telegram_bot_token_env: TELEMAX_BOT_PAPA

log_level: INFO
"""


def test_a_seeded_bridge_can_be_removed(tmp_path: Path) -> None:
    """Deleting the bot without this leaves a config that kills the service."""
    from bridge.config.writer import remove_config_bridge

    path = tmp_path / "config.yaml"
    path.write_text(SEEDED, encoding="utf-8")

    assert remove_config_bridge(path, "mama")

    text = path.read_text(encoding="utf-8")
    assert "mama" not in text
    assert "name: papa" in text
    assert "# Personal bridge." in text, "somebody's comments are not ours to delete"
    assert "log_level: INFO" in text


def test_removing_the_last_one_takes_the_key_with_it(tmp_path: Path) -> None:
    """`bridges:` with nothing under it parses as None, which the model rejects."""
    from bridge.config.writer import remove_config_bridge

    path = tmp_path / "config.yaml"
    path.write_text(SEEDED, encoding="utf-8")

    assert remove_config_bridge(path, "mama")
    assert remove_config_bridge(path, "papa")

    text = path.read_text(encoding="utf-8")
    assert "bridges:" not in text
    # And what is left still loads.
    loaded = load_config(path, load_env_file=False)
    assert loaded.bridges == ()


def test_removing_one_that_is_not_there_changes_nothing(tmp_path: Path) -> None:
    from bridge.config.writer import remove_config_bridge

    path = tmp_path / "config.yaml"
    path.write_text(SEEDED, encoding="utf-8")

    assert not remove_config_bridge(path, "timur")
    assert path.read_text(encoding="utf-8") == SEEDED


def test_a_config_without_a_bridges_section_is_refused(tmp_path: Path) -> None:
    from bridge.config.writer import remove_config_bridge

    path = tmp_path / "config.yaml"
    path.write_text("telegram:\n  owner_user_id: 1\n", encoding="utf-8")

    assert not remove_config_bridge(path, "mama")


def test_a_setting_the_bridge_removed_does_not_stop_the_service(
    tmp_path: Path, caplog: Any
) -> None:
    """`extra="forbid"` is right for a typo and wrong for a retirement. The
    picker was removed with the transport it belonged to; a config still naming
    it is not the owner's mistake, and refusing to start over one turns an
    upgrade into an outage."""
    from bridge.config.models import ReactionsConfig

    with caplog.at_level("WARNING"):
        config = ReactionsConfig(
            **{"style": "native", "picker": "on_demand", "picker_emoji": ["👍"]}
        )

    assert config.style.value == "native"
    assert "picker" in caplog.text
    assert not hasattr(config, "picker")


def test_a_typo_is_still_refused() -> None:
    from pydantic import ValidationError

    from bridge.config.models import ReactionsConfig

    with pytest.raises(ValidationError, match="Extra inputs"):
        ReactionsConfig(**{"stile": "native"})
