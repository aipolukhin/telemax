"""`python -m bridge run` — the whole process, guardian first.

What starts here is the runtime, not the bridge worker: the guardian bot has to
answer before MAX exists, because connecting MAX is something the owner does in
a chat. The worker comes up underneath it as soon as there is a session to work
with, and goes down and up again on `/restart` without the guardian noticing.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from bridge.config import ConfigError, load_config
from bridge.observability import configure_logging
from bridge.service import AlreadyRunning, StartupError, TelemaxRuntime


async def _serve(path: Path | None) -> int:
    loaded = load_config(path)
    configure_logging(loaded.app.log_level)
    for warning in loaded.warnings:
        logging.getLogger("bridge").warning("%s", warning)

    async with TelemaxRuntime(loaded, config_path=loaded.config_path) as runtime:
        await runtime.run_forever()
    return 0


def run(path: Path | None = None) -> int:
    try:
        return asyncio.run(_serve(path))
    except ConfigError as error:
        print(f"config error:\n{error}")
        return 2
    except AlreadyRunning as error:
        print(str(error))
        return 3
    except StartupError as error:
        # A misconfiguration is not a bug; a traceback here helps nobody.
        print(str(error))
        return 2
    except KeyboardInterrupt:
        return 0
