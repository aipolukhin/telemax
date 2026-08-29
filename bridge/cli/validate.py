"""`python -m bridge validate-config` — say what the config means, or why it is wrong.

The report is deliberately chatty about *derived* values (where the database
will live, which directories were created) because those are the things that
differ between a checkout and a systemd unit, and the things people get wrong.
It never prints a token, a phone number or a chat title.
"""

from __future__ import annotations

from pathlib import Path

from bridge.config import ConfigError, LoadedConfig, load_config


def _describe(loaded: LoadedConfig) -> str:
    app = loaded.app
    lines: list[str] = []

    source = str(loaded.config_path) if loaded.config_path else "<defaults only, no BRIDGE_CONFIG>"
    lines.append(f"config source     : {source}")
    lines.append(f"data dir          : {app.paths.data_dir}")
    lines.append(f"database          : {app.paths.db_path}")
    lines.append(f"temp dir          : {app.paths.resolved_temp_dir}")
    lines.append(f"media cache       : {app.paths.resolved_media_cache_dir}")
    lines.append(f"MAX session       : {app.max_session_dir / app.max.session_name}")
    lines.append(f"owner user id     : {app.telegram.owner_user_id}")
    lines.append(f"log level         : {app.log_level}")
    lines.append(f"max file size     : {app.media.max_file_size_mb} MB")
    lines.append("")
    lines.append(
        f"presence          : typing mirror={'on' if app.presence.mirror_typing else 'off'}, "
        f"outgoing typing={app.presence.outgoing_typing.value}, "
        f"read ticks={app.presence.read_receipt_style.value}, "
        f"auto read={app.presence.auto_read.value}"
    )
    lines.append(f"reactions         : style={app.reactions.style.value}")
    lines.append(
        f"provisioning      : mode={app.provisioning.mode.value}, "
        f"unknown chats={app.provisioning.unknown_chat_policy.value}"
    )
    lines.append("")

    if loaded.bridges:
        lines.append(f"bridges ({len(loaded.bridges)}):")
        width = max(len(bridge.name) for bridge in loaded.bridges)
        for bridge in loaded.bridges:
            state = "enabled " if bridge.enabled else "disabled"
            lines.append(
                f"  {bridge.name:<{width}}  {state}  max_chat_id={bridge.max_chat_id}  "
                f"token=${bridge.token_env} (set)"
            )
    else:
        lines.append("bridges           : none configured")

    if loaded.warnings:
        lines.append("")
        lines.append("warnings:")
        lines.extend(f"  ! {warning}" for warning in loaded.warnings)

    return "\n".join(lines)


def run(path: Path | None = None) -> int:
    try:
        loaded = load_config(path)
    except ConfigError as error:
        print(f"config error:\n{error}")
        return 2

    print(_describe(loaded))
    print("\nconfiguration is valid.")
    return 0
