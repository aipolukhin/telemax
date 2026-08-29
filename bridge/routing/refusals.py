"""What to tell the owner when MAX will not take a message.

The *classification* moved to `bridge.max_client.max_errors`, which is the one
place that reads a MAX error — this module used to hold half of it and be called
from exactly two places, both inline, so the worker retried the same permanent
refusal twelve times behind an owner who had already been told about it.

What is left here is the part that is genuinely about routing: turning a verdict
into a sentence in the owner's own terms, about their situation rather than about
the protocol.
"""

from __future__ import annotations

from dataclasses import dataclass

from bridge.max_client.max_errors import RETRYABLE_MESSAGE, classify_max_error

__all__ = ["RETRYABLE_MESSAGE", "Refusal", "classify"]


@dataclass(frozen=True, slots=True)
class Refusal:
    """What to tell the owner, and whether the bridge should try again."""

    permanent: bool
    message: str


def classify(error: BaseException) -> Refusal:
    """Read one MAX error. Unknown wording is retryable, deliberately.

    A wrong "permanent" silently drops the owner's message; a wrong "retryable"
    only costs a queue slot and a log line.
    """
    verdict = classify_max_error(error)
    return Refusal(
        permanent=verdict.permanent,
        message=verdict.owner_message or RETRYABLE_MESSAGE,
    )
