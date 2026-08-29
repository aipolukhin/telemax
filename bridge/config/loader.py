"""Loading and validating configuration.

Order of sources, strongest last: built-in defaults, the YAML file named by
`BRIDGE_CONFIG`, and the environment (which is the only place tokens ever come
from). `.env` next to the config is loaded first so a plain `python -m bridge`
works without exporting anything by hand.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import SecretStr, ValidationError

from .models import AppConfig, BridgeSource, ResolvedBridge

CONFIG_ENV = "BRIDGE_CONFIG"
DATA_DIR_ENV = "BRIDGE_DATA_DIR"
LOG_LEVEL_ENV = "BRIDGE_LOG_LEVEL"

# A Telegram bot token looks like `123456789:AA...`. We never validate it against
# the network here — that is `getMe` in bot validation — but an obviously wrong shape is
# worth catching before the process starts talking to anybody.
_MIN_TOKEN_LENGTH = 20


class ConfigError(Exception):
    """Configuration is unusable. The message is meant for a human, not a log."""


@dataclass(slots=True)
class LoadedConfig:
    """Validated configuration plus everything derived from it at startup."""

    app: AppConfig
    bridges: tuple[ResolvedBridge, ...]
    config_path: Path | None
    warnings: tuple[str, ...] = field(default=())


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ConfigError(f"config file not found: {path}") from error
    except yaml.YAMLError as error:
        raise ConfigError(f"config file is not valid YAML: {path}\n{error}") from error

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"config file must contain a mapping at the top level: {path}")
    return raw


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """Environment beats file for the two knobs that differ per deployment."""
    data_dir = os.environ.get(DATA_DIR_ENV, "").strip()
    if data_dir:
        paths = dict(data.get("paths") or {})
        paths["data_dir"] = data_dir
        data["paths"] = paths

    log_level = os.environ.get(LOG_LEVEL_ENV, "").strip()
    if log_level:
        data["log_level"] = log_level.upper()
    return data


def _format_validation_error(error: ValidationError, path: Path | None) -> str:
    where = f" in {path}" if path else ""
    lines = [f"configuration is invalid{where}:"]
    for problem in error.errors():
        location = ".".join(str(part) for part in problem["loc"]) or "<root>"
        lines.append(f"  {location}: {problem['msg']}")
    return "\n".join(lines)


def _resolve_tokens(app: AppConfig) -> tuple[ResolvedBridge, ...]:
    """Pull each bridge's token out of the environment.

    Missing variables are collected and reported together: fixing five of them
    one process start at a time is a miserable way to spend an evening.
    """
    missing: list[str] = []
    short: list[str] = []
    resolved: list[ResolvedBridge] = []

    for entry in app.bridges:
        token = os.environ.get(entry.telegram_bot_token_env, "").strip()
        if not token:
            missing.append(f"{entry.name}: ${entry.telegram_bot_token_env}")
            continue
        if len(token) < _MIN_TOKEN_LENGTH or ":" not in token:
            short.append(f"{entry.name}: ${entry.telegram_bot_token_env}")
            continue
        resolved.append(
            ResolvedBridge(
                name=entry.name,
                max_chat_id=entry.max_chat_id,
                token_env=entry.telegram_bot_token_env,
                token=SecretStr(token),
                source=BridgeSource.YAML,
                enabled=entry.enabled,
            )
        )

    problems: list[str] = []
    if missing:
        problems.append("bot tokens are not set in the environment:\n  " + "\n  ".join(missing))
    if short:
        problems.append(
            "bot tokens do not look like Telegram tokens (expected `<digits>:<secret>`):\n  "
            + "\n  ".join(short)
        )
    if problems:
        raise ConfigError("\n".join(problems))

    return tuple(resolved)


def _check_uniqueness(app: AppConfig) -> None:
    """One bot = one MAX dialog. Both directions of that invariant checked here.

    Bot *identity* is checked against `getMe` in bot validation; this is the cheap part —
    the same variable or the same chat listed twice.
    """
    problems: list[str] = []

    seen_names: set[str] = set()
    seen_chats: dict[int, str] = {}
    seen_envs: dict[str, str] = {}

    for entry in app.bridges:
        if entry.name in seen_names:
            problems.append(f"duplicate bridge name: {entry.name}")
        seen_names.add(entry.name)

        if entry.max_chat_id in seen_chats:
            problems.append(
                f"max_chat_id {entry.max_chat_id} is used by both "
                f"'{seen_chats[entry.max_chat_id]}' and '{entry.name}' — "
                "one bot must map to exactly one MAX dialog"
            )
        seen_chats[entry.max_chat_id] = entry.name

        if entry.telegram_bot_token_env in seen_envs:
            problems.append(
                f"token variable ${entry.telegram_bot_token_env} is shared by "
                f"'{seen_envs[entry.telegram_bot_token_env]}' and '{entry.name}'"
            )
        seen_envs[entry.telegram_bot_token_env] = entry.name

    if problems:
        raise ConfigError("\n".join(problems))


def _ensure_writable(label: str, path: Path, problems: list[str]) -> None:
    """Create the directory if it is missing, then prove we can write into it."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        problems.append(f"{label}: cannot create {path}: {error}")
        return

    if not os.access(path, os.W_OK | os.X_OK):
        problems.append(f"{label}: {path} is not writable")


def _check_paths(app: AppConfig) -> None:
    problems: list[str] = []
    _ensure_writable("data_dir", app.paths.data_dir, problems)
    _ensure_writable("temp_dir", app.paths.resolved_temp_dir, problems)
    _ensure_writable("media_cache_dir", app.paths.resolved_media_cache_dir, problems)
    _ensure_writable("max session dir", app.max_session_dir, problems)
    if app.provisioning.mode is not app.provisioning.mode.OFF:
        _ensure_writable("secrets_dir", app.paths.resolved_secrets_dir, problems)
    if problems:
        raise ConfigError("\n".join(problems))


def _collect_warnings(app: AppConfig, bridges: tuple[ResolvedBridge, ...]) -> tuple[str, ...]:
    """Things that are legal but worth saying out loud once, at startup."""
    warnings: list[str] = []

    if not bridges and app.provisioning.mode is app.provisioning.mode.OFF:
        warnings.append(
            "no bridges configured and provisioning is off: the process would idle. "
            "Add a bridge or set provisioning.mode to 'guardian'."
        )

    disabled = [bridge.name for bridge in bridges if not bridge.enabled]
    if disabled:
        warnings.append("bridges disabled in config: " + ", ".join(disabled))

    if app.presence.auto_read is app.presence.auto_read.ON_DELIVERY:
        warnings.append(
            "presence.auto_read=on_delivery: contacts will see messages as read the moment "
            "they reach Telegram, whether or not you looked at them"
        )

    if app.provisioning.mode in (
        app.provisioning.mode.MANAGED,
        app.provisioning.mode.AUTO_MTPROTO,
    ):
        # Which of the two actually creates bots is decided at start-up, by
        # whether an owner session is configured. Saying "managed bots" here
        # regardless was advice about a dialog this install no longer opens.
        warnings.append(
            f"provisioning.mode={app.provisioning.mode.value}: contact bots are created "
            + (
                "by driving @BotFather over the owner's session (paced, and it stops "
                "itself when @BotFather asks for a wait)"
                if app.provisioning.use_owner_session
                else "through Telegram's own Managed Bots dialog, which needs Bot "
                "Management Mode enabled on the guardian bot once, in @BotFather's mini app"
            )
        )

    if app.provisioning.mode is app.provisioning.mode.AUTO_MTPROTO:
        warnings.append(
            "provisioning.mode=auto_mtproto keeps a full Telegram account session on disk "
            "for bot counts and the /start that opens a new chat. Production does not need "
            "it: `managed` answers both over Bot API"
        )

    if app.provisioning.mode is app.provisioning.mode.GUARDIAN and not (
        app.provisioning.guardian_bot_token_env
    ):
        warnings.append(
            "provisioning.mode=guardian but provisioning.guardian_bot_token_env is unset: "
            "new contacts cannot be announced anywhere"
        )

    return tuple(warnings)


def config_path_from_env() -> Path | None:
    raw = os.environ.get(CONFIG_ENV, "").strip()
    return Path(raw).expanduser() if raw else None


def load_config(path: Path | None = None, *, load_env_file: bool = True) -> LoadedConfig:
    """Read, validate and resolve the configuration, or raise `ConfigError`."""
    config_path = path or config_path_from_env()

    if load_env_file:
        # A .env beside the config beats one in the working directory: the config
        # file is what pins a deployment, not wherever the process was started.
        for candidate in (
            config_path.parent / ".env" if config_path else None,
            Path(".env"),
        ):
            if candidate and candidate.is_file():
                load_dotenv(candidate, override=False)

    data = _apply_env_overrides(_read_yaml(config_path) if config_path else {})

    try:
        app = AppConfig.model_validate(data)
    except ValidationError as error:
        raise ConfigError(_format_validation_error(error, config_path)) from error

    secrets_file = app.paths.resolved_secrets_dir / app.provisioning.secrets_file_name
    if load_env_file and secrets_file.is_file():
        # Tokens the guardian handed out at runtime live here, and nothing else
        # reads them: without this a bridge created yesterday would not come
        # back after a restart, and the process would say "token is not set".
        load_dotenv(secrets_file, override=False)

    _check_uniqueness(app)
    bridges = _resolve_tokens(app)
    _check_paths(app)

    return LoadedConfig(
        app=app,
        bridges=bridges,
        config_path=config_path,
        warnings=_collect_warnings(app, bridges),
    )
