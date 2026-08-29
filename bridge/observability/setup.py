"""Logging setup: key=value lines, redacted, with the noisy libraries turned down."""

from __future__ import annotations

import logging
import sys
from typing import Any

from .redaction import RedactingFilter, redact

# Libraries that log a line per request or per frame. At INFO they drown out
# everything the bridge itself says.
NOISY = ("aiogram.event", "aiogram.client", "aiohttp", "asyncio", "pymax.connection")


class KeyValueFormatter(logging.Formatter):
    """`2030-01-02T03:04:05 INFO bridge.routing message=...` — greppable.

    Redaction happens here as well as in the filter, and that is deliberate: a
    traceback only becomes text during formatting, so a filter alone would let
    `RuntimeError: failed for <token>` through.
    """

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging(level: str = "INFO", *, stream: Any = None) -> None:
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(
        KeyValueFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    # On the handler, not on a logger: a filter on a logger does not see records
    # that propagate up from its children, and libraries log through their own.
    handler.addFilter(RedactingFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    for name in NOISY:
        logging.getLogger(name).setLevel(max(logging.WARNING, root.level))
