"""What setup knows before anything has been decided.

`load_config` cannot help here: it insists on `telegram.owner_user_id`, and the
whole point of setup is that nobody knows it yet — it comes out of the Telegram
session this run is about to open. So the file is *peeked at* rather than
validated, with the model defaults filling in whatever is absent.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from bridge.config.models import PathsConfig, ProvisioningConfig

#: Where a config goes when neither `-c` nor `$BRIDGE_CONFIG` says otherwise.
DEFAULT_CONFIG_NAME = "config.yaml"

GUARDIAN_TOKEN_ENV = "TELEMAX_GUARDIAN_TOKEN"  # noqa: S105 - a variable name


@dataclass(frozen=True, slots=True)
class Plan:
    """Paths and variable names, resolved once, used by every step."""

    config_path: Path
    env_path: Path
    data_dir: Path
    api_id_env: str
    api_hash_env: str
    phone_env: str
    guardian_token_env: str
    owner_user_id: int | None
    timezone: str | None
    exists: bool

    @property
    def secrets_dir(self) -> Path:
        return self.data_dir / "secrets"


def _peek(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    return value if isinstance(value, dict) else {}


def read_plan(config_path: Path | None) -> Plan:
    path = (config_path or Path(DEFAULT_CONFIG_NAME)).expanduser()
    data = _peek(path)

    paths = _section(data, "paths")
    telegram = _section(data, "telegram")
    provisioning = _section(data, "provisioning")
    defaults = ProvisioningConfig()

    data_dir = Path(str(paths.get("data_dir") or PathsConfig().data_dir))
    if not data_dir.is_absolute():
        # Resolved against the config, not the working directory. Setup and the
        # service run from different places, and both have to agree about where
        # the onboarding state lives or the handoff token lands somewhere the
        # service will never look.
        data_dir = (path.expanduser().resolve().parent / data_dir).resolve()

    owner = telegram.get("owner_user_id")
    return Plan(
        config_path=path,
        env_path=path.parent / ".env",
        data_dir=data_dir,
        api_id_env=str(provisioning.get("mtproto_api_id_env") or defaults.mtproto_api_id_env),
        api_hash_env=str(
            provisioning.get("mtproto_api_hash_env") or defaults.mtproto_api_hash_env
        ),
        phone_env=str(provisioning.get("mtproto_phone_env") or defaults.mtproto_phone_env),
        guardian_token_env=str(
            provisioning.get("guardian_bot_token_env") or GUARDIAN_TOKEN_ENV
        ),
        owner_user_id=int(owner) if isinstance(owner, int) else None,
        timezone=str(telegram["timezone"]) if telegram.get("timezone") else None,
        exists=path.exists(),
    )
