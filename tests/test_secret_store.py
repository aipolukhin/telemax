"""`bots.env` is replaced whole, atomically, or not at all.

The old writer truncated the file in place. A crash between the truncation and
the write lost *every* contact bot's token, not just the one being written —
and two provisioning walks at once dropped one of the two tokens with no error
anywhere. Both are reproduced below against the real store.
"""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

from bridge.provisioning.secrets import ContactBotSecretStore, parse_env, render_env

pytestmark = pytest.mark.asyncio

TOKEN_A = "111111111:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
TOKEN_B = "222222222:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"


@pytest.fixture
def store(tmp_path: Path) -> ContactBotSecretStore:
    return ContactBotSecretStore(tmp_path / "secrets" / "bots.env")


@pytest.fixture(autouse=True)
def _clean_environment() -> object:
    before = dict(os.environ)
    yield
    for key in set(os.environ) - set(before):
        os.environ.pop(key, None)


# ------------------------------------------------------------------- parsing


async def test_a_hand_edited_file_survives_a_round_trip() -> None:
    text = "# comment\n\nA=1\nB=two=three\n"
    values = parse_env(text)
    assert values == {"A": "1", "B": "two=three"}
    assert parse_env(render_env(values)) == values


# -------------------------------------------------------------------- basics


async def test_a_saved_token_is_on_disk_at_0600_and_in_the_environment(
    store: ContactBotSecretStore,
) -> None:
    await store.save("TELEMAX_BOT_A", TOKEN_A)

    assert store.read() == {"TELEMAX_BOT_A": TOKEN_A}
    assert os.environ["TELEMAX_BOT_A"] == TOKEN_A
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700


async def test_saving_one_token_keeps_the_others(store: ContactBotSecretStore) -> None:
    await store.save("TELEMAX_BOT_A", TOKEN_A)
    await store.save("TELEMAX_BOT_B", TOKEN_B)

    assert store.read() == {"TELEMAX_BOT_A": TOKEN_A, "TELEMAX_BOT_B": TOKEN_B}


async def test_unsetting_one_token_keeps_the_others(store: ContactBotSecretStore) -> None:
    await store.save("TELEMAX_BOT_A", TOKEN_A)
    await store.save("TELEMAX_BOT_B", TOKEN_B)

    await store.unset("TELEMAX_BOT_A")

    assert store.read() == {"TELEMAX_BOT_B": TOKEN_B}
    assert "TELEMAX_BOT_A" not in os.environ
    assert os.environ["TELEMAX_BOT_B"] == TOKEN_B


async def test_the_same_value_twice_is_not_a_second_rewrite(
    store: ContactBotSecretStore,
) -> None:
    await store.save("TELEMAX_BOT_A", TOKEN_A)
    first = store.path.stat().st_ino

    await store.save("TELEMAX_BOT_A", TOKEN_A)

    assert store.path.stat().st_ino == first, "an unchanged value replaces nothing"


async def test_replacing_a_value_replaces_only_it(store: ContactBotSecretStore) -> None:
    await store.save("TELEMAX_BOT_A", TOKEN_A)
    await store.save("TELEMAX_BOT_B", TOKEN_B)

    await store.save("TELEMAX_BOT_A", TOKEN_B)

    assert store.read() == {"TELEMAX_BOT_A": TOKEN_B, "TELEMAX_BOT_B": TOKEN_B}


# ---------------------------------------------------------------- durability


async def test_a_crash_before_the_replace_leaves_the_previous_file(
    store: ContactBotSecretStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure the old writer could not survive: the file is never partial."""
    await store.save("TELEMAX_BOT_A", TOKEN_A)

    def explode(source: str, destination: str) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(OSError, match="no space"):
        await store.save("TELEMAX_BOT_B", TOKEN_B)
    monkeypatch.undo()

    assert store.read() == {"TELEMAX_BOT_A": TOKEN_A}, "the other token is still there"
    assert "TELEMAX_BOT_B" not in os.environ, "nothing is exported that is not on disk"
    assert not list(store.path.parent.glob(".*tmp")), "no temporary file is left behind"


async def test_a_crash_while_writing_the_temporary_file_changes_nothing(
    store: ContactBotSecretStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    await store.save("TELEMAX_BOT_A", TOKEN_A)

    def explode(fd: int) -> None:
        raise OSError("I/O error")

    monkeypatch.setattr(os, "fsync", explode)
    with pytest.raises(OSError):
        await store.save("TELEMAX_BOT_B", TOKEN_B)
    monkeypatch.undo()

    assert store.read() == {"TELEMAX_BOT_A": TOKEN_A}
    assert not list(store.path.parent.glob(".*tmp"))


async def test_a_failed_chmod_leaves_nothing_behind(
    store: ContactBotSecretStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    await store.save("TELEMAX_BOT_A", TOKEN_A)

    def explode(fd: int, mode: int) -> None:
        raise PermissionError("nope")

    monkeypatch.setattr(os, "fchmod", explode)
    with pytest.raises(PermissionError):
        await store.save("TELEMAX_BOT_B", TOKEN_B)
    monkeypatch.undo()

    assert store.read() == {"TELEMAX_BOT_A": TOKEN_A}
    assert not list(store.path.parent.glob(".*tmp"))


async def test_cancellation_leaves_the_file_and_no_litter(
    store: ContactBotSecretStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    await store.save("TELEMAX_BOT_A", TOKEN_A)

    def cancel(fd: int) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(os, "fsync", cancel)
    with pytest.raises(asyncio.CancelledError):
        await store.save("TELEMAX_BOT_B", TOKEN_B)
    monkeypatch.undo()

    assert store.read() == {"TELEMAX_BOT_A": TOKEN_A}
    assert not list(store.path.parent.glob(".*tmp"))


# --------------------------------------------------------------- concurrency


async def test_a_hundred_concurrent_saves_all_survive(
    store: ContactBotSecretStore,
) -> None:
    """The race the old writer lost: each save read the file before the last one
    wrote it, so the loser's token vanished without an error anywhere."""
    names = [f"TELEMAX_BOT_{index:03d}" for index in range(100)]
    await asyncio.gather(*(store.save(name, f"{index}:{name}") for index, name in enumerate(names)))

    stored = store.read()
    assert set(stored) == set(names)
    assert all(stored[name] == f"{index}:{name}" for index, name in enumerate(names))


async def test_two_writers_of_the_same_key_leave_one_of_the_two_values(
    store: ContactBotSecretStore,
) -> None:
    await asyncio.gather(
        store.save("TELEMAX_BOT_A", TOKEN_A), store.save("TELEMAX_BOT_A", TOKEN_B)
    )
    assert store.read()["TELEMAX_BOT_A"] in {TOKEN_A, TOKEN_B}


async def test_a_save_and_an_unset_do_not_lose_each_other(
    store: ContactBotSecretStore,
) -> None:
    await store.save("TELEMAX_BOT_A", TOKEN_A)
    await asyncio.gather(store.save("TELEMAX_BOT_B", TOKEN_B), store.unset("TELEMAX_BOT_A"))

    assert store.read() == {"TELEMAX_BOT_B": TOKEN_B}


# -------------------------------------------------------------------- extras


async def test_the_fingerprint_names_the_file_without_printing_it(
    store: ContactBotSecretStore,
) -> None:
    assert store.fingerprint() == ""
    await store.save("TELEMAX_BOT_A", TOKEN_A)

    digest = store.fingerprint()
    assert len(digest) == 64
    assert TOKEN_A not in digest


async def test_unsetting_something_absent_is_not_an_error(
    store: ContactBotSecretStore,
) -> None:
    await store.unset("TELEMAX_BOT_MISSING")
    assert store.read() == {}
