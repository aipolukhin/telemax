"""Writing configuration back, without losing what a human put in it.

Two rules, and both exist because of a failure mode that has bitten this project
already:

* **every write is atomic.** A half-written config is worse than no config: the
  service refuses to start and the owner has nothing to compare against. So the
  bytes go to a temporary file in the same directory, get flushed and fsynced,
  and only then replace the original. A crash leaves either the old file or the
  new one, never a mixture.
* **comments survive.** A YAML round-trip through `yaml.safe_dump` drops every
  comment, and the comments are half of what makes `config.yaml` readable. So
  setting a value is a text edit that refuses rather than guesses when the
  section it was asked about is missing.

Secrets never come here. They go to `.env` beside the config, at 0600, and the
config only ever names the variable.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

#: Config and `.env` alike: readable by the owner, by nobody else.
FILE_MODE = stat.S_IRUSR | stat.S_IWUSR  # 0600


def atomic_write_text(path: Path, text: str, *, mode: int = FILE_MODE) -> None:
    """Replace `path` with `text`, or leave it exactly as it was.

    The temporary file is created in the destination directory on purpose:
    `os.replace` is only atomic within one filesystem, and `/tmp` is very often
    a different one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    handle, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    except BaseException:
        # Including KeyboardInterrupt: a cancelled setup must not leave litter.
        temporary_path.unlink(missing_ok=True)
        raise

    # The rename itself is only durable once the directory entry is on disk.
    # Best effort: some filesystems refuse to open a directory for fsync.
    try:
        directory = os.open(str(path.parent), os.O_RDONLY)
    except OSError:  # pragma: no cover - platform dependent
        return
    try:
        os.fsync(directory)
    except OSError:  # pragma: no cover - platform dependent
        pass
    finally:
        os.close(directory)


def set_config_value(config_path: Path, section: str, key: str, value: str) -> bool:
    """Set `section.key` in the YAML, in place, keeping every comment.

    Returns False when the section does not exist — the caller knows what to
    say about that, and inventing a section in somebody's file does not.
    """
    lines = config_path.read_text(encoding="utf-8").splitlines()

    def is_top_level(line: str) -> bool:
        return bool(line) and not line[0].isspace() and not line.lstrip().startswith("#")

    start: int | None = None
    for index, line in enumerate(lines):
        if is_top_level(line) and line.strip().startswith(f"{section}:"):
            start = index
            break
    if start is None:
        return False

    end = len(lines)
    for index in range(start + 1, len(lines)):
        if is_top_level(lines[index]):
            end = index
            break

    for index in range(start + 1, end):
        if lines[index].strip().startswith(f"{key}:"):
            lines[index] = f"  {key}: {value}"
            atomic_write_text(config_path, "\n".join(lines) + "\n")
            return True

    # Append to the section rather than to the blank line that ends it, or the
    # new key would sit visually with whatever comes next.
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines.insert(insert_at, f"  {key}: {value}")
    atomic_write_text(config_path, "\n".join(lines) + "\n")
    return True


def write_timezone(config_path: Path, timezone: str) -> bool:
    return set_config_value(config_path, "telegram", "timezone", timezone)


def remove_config_bridge(config_path: Path, name: str) -> bool:
    """Drop one entry from the YAML `bridges:` seed. False when it was not there.

    A seeded bridge names an environment variable, and the loader refuses to
    start when that variable is empty. So removing a bridge means removing it
    from *both* places — forgetting the YAML leaves a config that validates on
    paper and then kills the service at every restart.

    A text edit, like every other write here, so the comments around it survive.
    The key goes entirely when the last entry does: `bridges:` with nothing
    under it parses as `None`, which the model rejects.
    """
    lines = config_path.read_text(encoding="utf-8").splitlines()

    def is_top_level(line: str) -> bool:
        return bool(line) and not line[0].isspace() and not line.lstrip().startswith("#")

    start = next(
        (
            index
            for index, line in enumerate(lines)
            if is_top_level(line) and line.strip().startswith("bridges:")
        ),
        None,
    )
    if start is None:
        return False

    end = len(lines)
    for index in range(start + 1, len(lines)):
        if is_top_level(lines[index]):
            end = index
            break

    # Where each list item begins. Everything up to the next one belongs to it.
    starts = [
        index
        for index in range(start + 1, end)
        if lines[index].lstrip().startswith("- ")
    ]
    if not starts:
        return False

    doomed: tuple[int, int] | None = None
    for position, first in enumerate(starts):
        last = starts[position + 1] if position + 1 < len(starts) else end
        block = "\n".join(lines[first:last])
        if _entry_name(block) == name:
            doomed = (first, last)
            break
    if doomed is None:
        return False

    kept_entries = len(starts) - 1
    if kept_entries == 0:
        # Take the key with it, and any blank lines that were separating it.
        cut_from = start
        while cut_from > 0 and not lines[cut_from - 1].strip():
            cut_from -= 1
        remaining = lines[:cut_from] + lines[end:]
    else:
        remaining = lines[: doomed[0]] + lines[doomed[1] :]

    atomic_write_text(config_path, "\n".join(remaining).rstrip("\n") + "\n")
    return True


def _entry_name(block: str) -> str | None:
    """The `name:` of one YAML list item, wherever in the item it sits."""
    for line in block.splitlines():
        stripped = line.lstrip().removeprefix("- ").strip()
        if stripped.startswith("name:"):
            return stripped.partition(":")[2].strip().strip("\"'")
    return None


def set_env_value(env_path: Path, key: str, value: str) -> None:
    """Store a secret beside the config, at 0600, replacing any previous value.

    Also exported into this process: the code that reads it next is usually
    running right now, and re-reading the file would be the only alternative.
    """
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    replaced = False
    for index, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == key:
            lines[index] = f"{key}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{key}={value}")

    atomic_write_text(env_path, "\n".join(lines) + "\n")
    os.environ[key] = value


def unset_env_value(env_path: Path, key: str) -> None:
    """Forget a value again — the rollback half of `set_env_value`.

    Used when setup is cancelled: "изменения не сохранены" has to be true, and
    the owner's api_hash sitting in a file after they pressed Ctrl+C is a change.
    """
    os.environ.pop(key, None)
    if not env_path.exists():
        return

    kept = [
        line
        for line in env_path.read_text(encoding="utf-8").splitlines()
        if line.split("=", 1)[0].strip() != key
    ]
    if any(line.strip() for line in kept):
        atomic_write_text(env_path, "\n".join(kept) + "\n")
    else:
        # Nothing left worth keeping; an empty .env is just litter.
        env_path.unlink(missing_ok=True)


#: Everything the guardian bot needs to come up, and nothing else. The rest of
#: the configuration is written after MAX is connected, when the values are
#: actually known — a config half-filled with placeholders would validate and
#: then behave strangely.
MINIMAL_CONFIG = """\
# Telemax — personal MAX <-> Telegram bridge.
#
# Written by `telemax setup`. Secrets live in .env beside this file at 0600.
# This file still contains installation-specific ids and paths: do not commit it.

paths:
  data_dir: {data_dir}

telegram:
  # Your own Telegram user id. Every update from anybody else is dropped.
  owner_user_id: {owner_user_id}
  # Telegram renders times in the reader's timezone and never tells a bot what
  # that is, so it has to be said here. `null` means the guardian bot has not
  # asked yet — it is the first question of onboarding, because a terminal on a
  # server cannot know what the owner's own clock says.
  timezone: {timezone}

max:
  session_name: max-session.db
  phone_env: MAX_PHONE
  # The bridge keeps a MAX socket open around the clock, which MAX reads as
  # «В сети». `mirror` says it only while you are actually writing through the
  # bridge; `offline` never says it; `online` is the old always-on behaviour.
  own_presence: mirror

media:
  max_file_size_mb: 50

presence:
  mirror_typing: true
  outgoing_typing: media_only
  read_receipt_style: reaction
  auto_read: on_read
  pin_contact_status: true

reactions:
  style: native

provisioning:
  # The guardian bot is the whole administrative surface: MAX login, dialog
  # picking, status and restart all happen there. `managed` keeps no Telegram
  # account credential on disk — contact bots come from Managed Bots, and the
  # guardian only needs Bot Management Mode, enabled once in @BotFather.
  mode: managed
  unknown_chat_policy: ask
  guardian_bot_token_env: {guardian_token_env}
  pending_ttl_days: 7
  pending_max_messages: 200

log_level: INFO
"""


def write_bootstrap_config(
    config_path: Path,
    *,
    owner_user_id: int,
    timezone: str | None = None,
    guardian_token_env: str,
    data_dir: str = "./data",
) -> None:
    """Create the config the guardian needs, atomically.

    Only ever called when there is nothing usable at `config_path`: an existing
    file is edited key by key instead, so a config somebody has tuned is never
    overwritten by a re-run of `setup`.

    `timezone=None` writes a literal `null`, which the loader accepts and the
    guardian reads as "not asked yet".
    """
    atomic_write_text(
        config_path,
        MINIMAL_CONFIG.format(
            data_dir=data_dir,
            owner_user_id=owner_user_id,
            timezone=timezone or "null",
            guardian_token_env=guardian_token_env,
        ),
    )
