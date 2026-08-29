"""AU-2 F3/F8 — state files that survive the machine stopping badly.

`identity.json` decides which phone this install claims to be, and
`client_session_id` counts how many times it has started. Both were written with
`write_text`, which truncates first and writes second, so a power cut in between
left a file that exists, is readable, and is empty.

Neither failure looks like a lost setting from the server's side.

An unreadable identity used to fall back to the legacy device *with a freshly
drawn `device_id`* — and never write it down. So a single torn write changed the
phone model once, and then changed the `ANDROID_ID` on every restart afterwards,
for ever, because nothing repaired the file. That is exactly the "stolen account"
signal `identity.py` exists to avoid.

An empty counter made `int("")` raise, and the count restarted at 1. The module's
own reason for existing is that a counter walking backwards says more than any
single value — and a test used to assert that it does.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from bridge.max_client.client import CLIENT_SESSION_COUNTER, next_client_session_id
from bridge.max_client.identity import IDENTITY_FILE, LEGACY_IDENTITY, load_or_create
from bridge.max_client.state_files import BACKUP_SUFFIX, read_with_backup, write_atomic


def identity_path(directory: Path) -> Path:
    return directory / IDENTITY_FILE


def counter_path(directory: Path) -> Path:
    return directory / CLIENT_SESSION_COUNTER


# ------------------------------------------------------------ the write itself


def test_a_write_never_leaves_a_truncated_file(tmp_path: Path) -> None:
    """The whole failure in one line: the reader sees the old bytes or the new
    ones, never a prefix of either."""
    target = tmp_path / "state"
    write_atomic(target, "first\n")

    seen: list[str] = []
    original = os.replace

    def watched(src: object, dst: object) -> None:
        # Whatever a reader does at the instant of the rename, it reads a whole
        # file. Before this, the same moment held an empty one.
        seen.append(target.read_text(encoding="utf-8"))
        original(src, dst)  # type: ignore[arg-type]

    import bridge.max_client.state_files as state_files

    state_files.os.replace = watched  # type: ignore[assignment]
    try:
        write_atomic(target, "second\n")
    finally:
        state_files.os.replace = original  # type: ignore[assignment]

    # Two renames — the copy, then the primary — and at both instants the
    # primary holds its old contents whole. Before this, the same moment held an
    # empty file.
    assert seen == ["first\n", "first\n"]
    assert target.read_text(encoding="utf-8") == "second\n"


def test_a_write_keeps_the_previous_contents_beside_it(tmp_path: Path) -> None:
    target = tmp_path / "state"
    write_atomic(target, "one\n")
    write_atomic(target, "two\n")

    assert target.read_text(encoding="utf-8") == "two\n"
    assert (tmp_path / f"state{BACKUP_SUFFIX}").read_text(encoding="utf-8") == "one\n"


def test_state_files_are_owner_only(tmp_path: Path) -> None:
    """Set on the temporary file before anything is written into it, so the
    content never exists at a wider mode even for an instant."""
    target = tmp_path / "state"
    write_atomic(target, "one\n")
    write_atomic(target, "two\n")

    for path in (target, tmp_path / f"state{BACKUP_SUFFIX}"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_no_temporary_files_are_left_behind(tmp_path: Path) -> None:
    target = tmp_path / "state"
    for value in ("one", "two", "three"):
        write_atomic(target, value)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["state", f"state{BACKUP_SUFFIX}"]


def test_a_failed_write_cleans_up_and_leaves_the_old_file(tmp_path: Path) -> None:
    target = tmp_path / "state"
    write_atomic(target, "good\n")

    import bridge.max_client.state_files as state_files

    original = state_files.os.replace

    def explode(src: object, dst: object) -> None:
        raise OSError("disk full")

    state_files.os.replace = explode  # type: ignore[assignment]
    try:
        with pytest.raises(OSError):
            write_atomic(target, "bad\n")
    finally:
        state_files.os.replace = original  # type: ignore[assignment]

    assert target.read_text(encoding="utf-8") == "good\n"
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


def test_reading_prefers_the_primary_and_falls_back_to_the_copy(tmp_path: Path) -> None:
    target = tmp_path / "state"
    write_atomic(target, "1")
    write_atomic(target, "2")

    assert read_with_backup(target, int) == (2, False)

    target.write_text("", encoding="utf-8")  # a torn write
    assert read_with_backup(target, int) == (1, True)


# -------------------------------------------------------------------- identity


def test_an_identity_is_drawn_once_and_then_only_read(tmp_path: Path) -> None:
    first = load_or_create(tmp_path)
    assert load_or_create(tmp_path) == first
    assert identity_path(tmp_path).exists()


def test_a_torn_identity_is_restored_from_its_copy(tmp_path: Path) -> None:
    """The account keeps the device it has been showing — which is the point."""
    original = load_or_create(tmp_path)
    identity_path(tmp_path).write_text('{"device_name": "Xiaomi 221', encoding="utf-8")

    repairs: list[str] = []
    recovered = load_or_create(tmp_path, on_repair=repairs.append)

    assert recovered == original
    assert repairs, "a recovered identity file is worth telling somebody about"
    # And the primary is good again, so the next start is an ordinary read.
    assert load_or_create(tmp_path) == original
    assert json.loads(identity_path(tmp_path).read_text())["device_id"] == original.device_id


def test_a_totally_lost_identity_is_replaced_exactly_once(tmp_path: Path) -> None:
    """No primary, no copy. Drawing a *new* phone would read as a stolen account,
    so the legacy device is written — and written is the operative word: the old
    code returned one and kept none, so every restart drew another."""
    load_or_create(tmp_path)
    identity_path(tmp_path).write_text("{", encoding="utf-8")
    (tmp_path / f"{IDENTITY_FILE}{BACKUP_SUFFIX}").write_text("also broken", encoding="utf-8")

    repairs: list[str] = []
    first = load_or_create(tmp_path, on_repair=repairs.append)
    second = load_or_create(tmp_path)
    third = load_or_create(tmp_path)

    assert first.device_id == second.device_id == third.device_id
    assert first.device_name == LEGACY_IDENTITY["device_name"]
    assert len(repairs) == 1  # said once, not on every start


def test_a_missing_identity_is_a_first_run_not_a_repair(tmp_path: Path) -> None:
    repairs: list[str] = []
    load_or_create(tmp_path, on_repair=repairs.append)
    assert repairs == []


def test_an_existing_session_still_keeps_the_identity_it_was_created_with(
    tmp_path: Path,
) -> None:
    """The upgrade path, unchanged: an account logged in before this file existed
    must not turn into a different phone."""
    identity = load_or_create(tmp_path, session_exists=True)
    for field, value in LEGACY_IDENTITY.items():
        assert getattr(identity, field) == value


def test_a_valid_identity_is_never_rewritten_on_deploy(tmp_path: Path) -> None:
    """What must not happen when this ships: the running bridge's file is read,
    not touched."""
    load_or_create(tmp_path)
    path = identity_path(tmp_path)
    before = (path.read_bytes(), path.stat().st_mtime_ns)

    load_or_create(tmp_path)
    load_or_create(tmp_path)

    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_an_identity_from_before_this_change_gets_a_copy(tmp_path: Path) -> None:
    """Found on the live deploy, not in a test: every install that already had an
    `identity.json` had no copy, and — being valid — would never be rewritten, so
    it would never get one. The recovery above would have been inert on exactly
    the installs with the most to lose.

    Seeding touches only the copy: the primary keeps its bytes and its mtime.
    """
    path = identity_path(tmp_path)
    backup = tmp_path / f"{IDENTITY_FILE}{BACKUP_SUFFIX}"
    # An install as it looked before this commit: a primary, and nothing else.
    load_or_create(tmp_path)
    backup.unlink()
    before = (path.read_bytes(), path.stat().st_mtime_ns)

    identity = load_or_create(tmp_path)

    assert backup.exists()
    assert json.loads(backup.read_text())["device_id"] == identity.device_id
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    # And now the torn-write path has something to recover from.
    path.write_text("{", encoding="utf-8")
    assert load_or_create(tmp_path) == identity


def test_an_identity_missing_a_field_is_not_half_accepted(tmp_path: Path) -> None:
    """A half-read identity is a different phone, so it is not an identity."""
    load_or_create(tmp_path)
    original = json.loads(identity_path(tmp_path).read_text())
    del original["screen"]
    identity_path(tmp_path).write_text(json.dumps(original), encoding="utf-8")

    # The copy from the first write is intact, so recovery finds the real one.
    recovered = load_or_create(tmp_path)
    assert recovered.screen


# ---------------------------------------------------------------- the counter


def test_the_counter_counts_up_and_persists(tmp_path: Path) -> None:
    assert next_client_session_id(tmp_path) == 1
    assert next_client_session_id(tmp_path) == 2
    assert counter_path(tmp_path).read_text().strip() == "2"


def test_the_counter_does_not_walk_backwards_after_a_torn_write(tmp_path: Path) -> None:
    """The behaviour a test used to assert the opposite of. A counter that goes
    5, 6, 7, 1 tells the server this is a different install."""
    for _ in range(7):
        next_client_session_id(tmp_path)
    counter_path(tmp_path).write_text("", encoding="utf-8")  # power cut mid-write

    following = next_client_session_id(tmp_path)

    assert following > 1
    assert following >= 7  # from the copy, which holds the value before the last bump
    assert next_client_session_id(tmp_path) > following


def test_a_counter_with_no_history_at_all_starts_at_one(tmp_path: Path) -> None:
    """Nothing to be monotonic about: this is a first run, or somebody deleted
    both copies. Starting at 1 is honest; inventing a large number is not."""
    counter_path(tmp_path).write_text("nonsense", encoding="utf-8")
    assert next_client_session_id(tmp_path) == 1


def test_the_counter_file_is_owner_only(tmp_path: Path) -> None:
    next_client_session_id(tmp_path)
    assert stat.S_IMODE(counter_path(tmp_path).stat().st_mode) == 0o600
