"""Logging, redaction, and anything else that watches the bridge run."""

from .redaction import MASK, RedactingFilter, redact
from .setup import configure_logging

__all__ = ["MASK", "RedactingFilter", "configure_logging", "redact"]
