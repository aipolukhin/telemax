"""Reactions: the verified MAX emoji set, the mapping, and the sync."""

from .activity import DialogActivity
from .adapters import MaxReactionAdapter, TelegramReactionAdapter
from .mapping import TELEGRAM_FREE, describe, to_max, to_telegram
from .max_set import (
    LEGACY_ACCEPTED,
    REFUSED_BY_SERVER,
    SAFE,
    can_send,
    is_in_picker,
    unsupported,
)
from .sync import ReactionSync

__all__ = [
    "LEGACY_ACCEPTED",
    "REFUSED_BY_SERVER",
    "SAFE",
    "TELEGRAM_FREE",
    "DialogActivity",
    "MaxReactionAdapter",
    "ReactionSync",
    "TelegramReactionAdapter",
    "can_send",
    "describe",
    "is_in_picker",
    "to_max",
    "to_telegram",
    "unsupported",
]
