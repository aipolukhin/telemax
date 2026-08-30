"""`python -m bridge setup` — the console half, and only the console half.

The whole implementation lives in `bridge.bootstrap`; this is the entry point
the argument parser calls. It is a separate module because what `setup` *is*
changed: it used to be a linear list of questions ending in "now run these three
commands", and it is now a bootstrap that finishes by handing the owner a link
and stopping.
"""

from __future__ import annotations

from pathlib import Path

from bridge.bootstrap.flow import run as run_bootstrap


def run(
    path: Path | None = None,
    *,
    instance: str | None = None,
    use_session: bool = True,
    adopt: bool = False,
    deployment: str = "systemd",
) -> int:
    return run_bootstrap(
        path,
        instance=instance,
        use_session=use_session,
        adopt=adopt,
        deployment=deployment,
    )
