"""The line under which old history is never carried again.

The delivery dedup is `(max_chat_id, max_message_id, telegram_bot_id)`. It is
keyed on the *bot*, which is what makes the reconnect backfill free — the same
bot claims the same message twice and the second claim is refused.

Give a contact a different bot and that stops being true. Every message in the
chat's tail is unclaimed again, and the first backfill after the cutover would
deliver the last fifty messages of every conversation into the new chat. Keeping
the old `message_map` would not have helped: the bot id in the key is different.

So the newest MAX message id per chat is written down *before* the mappings go,
and the guardian stamps it onto each bridge row it creates. Nothing at or below
it is ever carried automatically again.

A file rather than a column, for the window in between: at the moment this is
written there are no bridge rows left to put it on.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from bridge.config.writer import atomic_write_text

logger = logging.getLogger(__name__)

FLOORS_FILE_NAME = "history-floors.json"


def floors_path(data_dir: Path) -> Path:
    return data_dir / "state" / FLOORS_FILE_NAME


def write_floors(data_dir: Path, floors: dict[int, int]) -> None:
    """Newest MAX message id per chat, as of the cutover."""
    atomic_write_text(
        floors_path(data_dir),
        json.dumps({str(chat): int(value) for chat, value in floors.items()}, indent=2) + "\n",
    )


def read_floors(data_dir: Path) -> dict[int, int]:
    try:
        raw = json.loads(floors_path(data_dir).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("the history floors file is unreadable; ignoring it")
        return {}
    if not isinstance(raw, dict):
        return {}
    floors: dict[int, int] = {}
    for chat, value in raw.items():
        try:
            floors[int(chat)] = int(value)
        except (TypeError, ValueError):
            continue
    return floors


async def apply_floor(bridges: object, *, data_dir: Path, bridge_name: str, chat_id: int) -> int:
    """Stamp the recorded floor onto a bridge that has just been created.

    Returns the floor applied, or 0 when there is none — an installation that
    never had a cutover has no file and this does nothing at all.
    """
    floor = read_floors(data_dir).get(int(chat_id), 0)
    if not floor:
        return 0
    setter = getattr(bridges, "set_history_floor", None)
    if setter is None:
        return 0
    await setter(bridge_name, floor)
    logger.info(
        "bridge %s will not carry MAX messages at or below %s (cutover floor)",
        bridge_name,
        floor,
    )
    return floor
