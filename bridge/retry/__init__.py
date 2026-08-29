"""Delivery queue: backoff policy, per-bridge workers, failure classification."""

from .backoff import DEFAULT_POLICY, BackoffPolicy
from .worker import (
    OutboxWorker,
    PermanentDeliveryError,
    WorkerPool,
    classify,
)

__all__ = [
    "DEFAULT_POLICY",
    "BackoffPolicy",
    "OutboxWorker",
    "PermanentDeliveryError",
    "WorkerPool",
    "classify",
]
