"""Where the guardian's business connection is written down.

Secretary Mode (Business Mode before May 2026) hands a bot one thing it cannot
ask for again: a `business_connection_id`. It arrives in a single update when the
owner connects the bot, and every action taken on the owner's behalf needs it. If
it is lost, the only way back is asking the owner to disconnect and reconnect.

So it goes to disk the moment it arrives, next to the other state, at 0600 —
it is not a secret in the token sense, but it authorises acting *as the owner*
and deserves the same treatment.

Nothing reads this yet on the delivery path. What it is *for* is the history
import: the owner's own past messages could then land in Telegram as the owner's
rather than as bot lines. Whether that is allowed at all rests on three questions
only a live attempt answers — the 24-hour window, whether a chat with a bot can be
connected, and whether the account needs Business/Premium. They are written up in
HANDOFF, «Режим секретаря».
"""

from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_NAME = "business.json"

_state_dir: Path | None = None


@dataclass(frozen=True, slots=True)
class BusinessConnectionRecord:
    connection_id: str
    user_chat_id: int
    enabled: bool
    can_reply: bool
    can_read: bool
    #: What Telegram said about the owner's account when it connected. Telegram
    #: Business is a Premium feature, so in practice a connection cannot exist
    #: without it — but saying so out loud is what makes the fallback to «Вы: »
    #: a decision rather than an accident. `None` is "not stated", which is
    #: treated as allowed: an older record predates this field.
    is_premium: bool | None = None


def use_state_dir(path: Path) -> None:
    """Told once at startup, so the module has no idea what a config is."""
    global _state_dir
    _state_dir = path


def record_business_connection(
    *,
    connection_id: str,
    user_chat_id: int,
    enabled: bool,
    can_reply: bool,
    can_read: bool,
    is_premium: bool | None = None,
) -> None:
    record = BusinessConnectionRecord(
        connection_id=connection_id,
        user_chat_id=user_chat_id,
        enabled=enabled,
        can_reply=can_reply,
        can_read=can_read,
        is_premium=is_premium,
    )
    if _state_dir is None:
        logger.debug("no state directory yet; business connection not persisted")
        return
    write_record(_state_dir, record)


def write_record(state_dir: Path, record: BusinessConnectionRecord) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / STATE_NAME
    path.write_text(json.dumps(asdict(record), ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


def read_record(state_dir: Path) -> BusinessConnectionRecord | None:
    path = state_dir / STATE_NAME
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("business connection state is unreadable; ignoring it")
        return None
    try:
        premium = raw.get("is_premium")
        return BusinessConnectionRecord(
            connection_id=str(raw["connection_id"]),
            user_chat_id=int(raw["user_chat_id"]),
            enabled=bool(raw.get("enabled", False)),
            can_reply=bool(raw.get("can_reply", False)),
            can_read=bool(raw.get("can_read", False)),
            is_premium=None if premium is None else bool(premium),
        )
    except (KeyError, TypeError, ValueError):
        logger.warning("business connection state is malformed; ignoring it")
        return None
