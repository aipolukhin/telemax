"""Local supervisor health for Docker/systemd probes."""

from __future__ import annotations

from pathlib import Path

from bridge.config import ConfigError, load_config
from bridge.service.heartbeat import RuntimeHeartbeat, check_heartbeat


def run(path: Path | None = None) -> int:
    try:
        loaded = load_config(path)
    except ConfigError as error:
        print(f"unhealthy: config error: {error}")
        return 2

    heartbeat = RuntimeHeartbeat.for_data_dir(loaded.app.paths.data_dir)
    status = check_heartbeat(heartbeat.path)
    print(("healthy: " if status.healthy else "unhealthy: ") + status.detail)
    return 0 if status.healthy else 1


__all__ = ["run"]
