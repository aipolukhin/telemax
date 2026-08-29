"""Deterministic usernames: the same input, the same name, forever.

Two properties are load-bearing and they pull in opposite directions. The name
has to be *stable*, or a re-run of setup hands the owner a link to a bot that no
longer exists; and it has to be *opaque*, or the username published in every
chat is a Telegram id and a MAX id in clear. Every test here defends one or the
other, and the last few defend the second one specifically — a plain digest
would pass all the stability tests and leak the id to anyone with a laptop.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bridge.provisioning.naming import (
    GUARD_NAMESPACE,
    MAX_USERNAME_LENGTH,
    PEER_NAMESPACE,
    InvalidUsernameError,
    contact_username,
    ensure_username,
    guard_username,
    load_or_create_naming_secret,
    stable_slug,
    validate_username,
)

SECRET = b"a-fixed-naming-secret-for-tests"
OWNER = 100000001
PEER = 200000002


# ------------------------------------------------------------------ stability


def test_one_owner_always_gets_one_guardian_username() -> None:
    assert guard_username(SECRET, OWNER) == guard_username(SECRET, OWNER)


def test_different_owners_get_different_guardian_usernames() -> None:
    assert guard_username(SECRET, OWNER) != guard_username(SECRET, OWNER + 1)


def test_one_max_peer_always_gets_one_contact_username() -> None:
    assert contact_username(SECRET, PEER) == contact_username(SECRET, PEER)


def test_different_max_peers_get_different_contact_usernames() -> None:
    assert contact_username(SECRET, PEER) != contact_username(SECRET, PEER + 1)


def test_the_two_namespaces_never_collide() -> None:
    """The id spaces overlap; without domain separation the names would too."""
    assert stable_slug(SECRET, GUARD_NAMESPACE, 1) != stable_slug(SECRET, PEER_NAMESPACE, 1)
    assert guard_username(SECRET, 1)[:16] != contact_username(SECRET, 1)[:16]


def test_a_renamed_contact_keeps_the_same_username() -> None:
    """MAX display names change constantly; a bot's @username cannot."""
    before = contact_username(SECRET, PEER)
    # The display name is not an input at all — there is nowhere to pass it.
    assert contact_username(SECRET, PEER) == before


# --------------------------------------------------------------------- shape


@pytest.mark.parametrize(
    "username",
    [guard_username(SECRET, OWNER), contact_username(SECRET, PEER)],
)
def test_a_generated_username_is_one_telegram_accepts(username: str) -> None:
    assert validate_username(username)
    assert username == username.lower()
    assert username.endswith("bot")
    assert len(username) <= MAX_USERNAME_LENGTH
    assert username[0].isalpha(), "Telegram requires a letter first"
    assert all(character.isalnum() or character == "_" for character in username)
    assert " " not in username and username.isascii()


def test_both_templates_fit_inside_thirty_two_characters() -> None:
    """The slug is 16 and the suffixes are 12 and 8. Checked, not assumed."""
    assert len(guard_username(SECRET, OWNER)) == 28
    assert len(contact_username(SECRET, PEER)) == 24


def test_a_leading_digit_is_mapped_to_a_letter() -> None:
    """Base32 emits `2`-`7`; Telegram refuses a username starting with one."""
    seen = {stable_slug(SECRET, PEER_NAMESPACE, index)[0] for index in range(2000)}
    assert all(character.isalpha() for character in seen), sorted(seen)


def test_an_unusable_username_is_refused_rather_than_shipped() -> None:
    for bad in ("Bad_Bot", "тест_bot", "abc", "no-dashes_bot", "1abc_bot", "abcd_xyz"):
        assert not validate_username(bad), bad
    with pytest.raises(InvalidUsernameError):
        ensure_username("nope")


# -------------------------------------------------------------- the secret


def test_the_naming_secret_is_created_once_and_never_rotated(tmp_path: Path) -> None:
    """Rotating it would rename the guardian and every contact bot at once."""
    first = load_or_create_naming_secret(tmp_path / "secrets")
    second = load_or_create_naming_secret(tmp_path / "secrets")

    assert first == second
    assert len(first) >= 32


def test_the_naming_secret_is_0600(tmp_path: Path) -> None:
    load_or_create_naming_secret(tmp_path / "secrets")
    path = tmp_path / "secrets" / "naming-secret"

    assert (path.stat().st_mode & 0o777) == 0o600
    assert (tmp_path / "secrets").stat().st_mode & 0o777 == 0o700


def test_a_second_bootstrap_reproduces_the_same_usernames(tmp_path: Path) -> None:
    """The end-to-end promise: re-running setup finds the bot it already made."""
    secret = load_or_create_naming_secret(tmp_path / "secrets")
    before = (guard_username(secret, OWNER), contact_username(secret, PEER))

    # A whole new process, reading the same file.
    again = load_or_create_naming_secret(tmp_path / "secrets")
    assert (guard_username(again, OWNER), contact_username(again, PEER)) == before


def test_two_installations_do_not_share_names(tmp_path: Path) -> None:
    """Keyed, not hashed: the same id on another host is a different name."""
    here = load_or_create_naming_secret(tmp_path / "one")
    there = load_or_create_naming_secret(tmp_path / "two")

    assert guard_username(here, OWNER) != guard_username(there, OWNER)


# ------------------------------------------------------------------ leakage


def test_the_raw_identifiers_never_appear_in_the_username() -> None:
    """The reason this is an HMAC and not `sha256(id)`."""
    guard = guard_username(SECRET, OWNER)
    contact = contact_username(SECRET, PEER)

    assert str(OWNER) not in guard
    assert str(PEER) not in contact
    # Nor in any base the digits could plausibly have been written in.
    for base in (hex(OWNER)[2:], oct(OWNER)[2:], hex(PEER)[2:], oct(PEER)[2:]):
        assert base.lower() not in guard + contact


def test_a_token_or_a_phone_is_not_an_input(monkeypatch: object) -> None:
    """Nothing that rotates may reach the name, or the name would rotate too."""
    phone = "+79001232051"
    token = "9000000003:AAHnQzZ5r4v6WcE2sTgP1kLm9xYbVdNfQwE"
    os.environ["TELEMAX_GUARDIAN_TOKEN"] = token

    username = guard_username(SECRET, OWNER)
    assert phone.strip("+") not in username
    assert token.split(":")[0] not in username
    # And the id alone reproduces it, so nothing else can have contributed.
    assert username == guard_username(SECRET, OWNER)
    os.environ.pop("TELEMAX_GUARDIAN_TOKEN", None)


def test_the_secret_is_what_makes_enumeration_useless() -> None:
    """Without the key, guessing an id gets you nothing — that is the point."""
    guessed = stable_slug(b"a-different-secret", PEER_NAMESPACE, PEER)
    assert guessed != stable_slug(SECRET, PEER_NAMESPACE, PEER)


def test_an_empty_naming_secret_is_refused_not_replaced(tmp_path: Path) -> None:
    """Replacing it renames the guardian and every V1 contact bot at once.

    That used to happen in silence. Only a V1 installation asks for this file at
    all now, and only to recognise bots it already has.
    """
    from bridge.provisioning.naming import (
        NAMING_SECRET_FILE,
        NamingSecretUnusableError,
    )

    secrets = tmp_path / "secrets"
    first = load_or_create_naming_secret(secrets)
    (secrets / NAMING_SECRET_FILE).write_bytes(b"")

    with pytest.raises(NamingSecretUnusableError):
        load_or_create_naming_secret(secrets)

    assert first, "the original was real"


def test_v2_naming_needs_no_secret_at_all(tmp_path: Path) -> None:
    """A clean machine: no file, no database, and the same names."""
    from bridge.provisioning.naming_v2 import (
        contact_bot_username_v2,
        guardian_bot_username_v2,
    )

    assert contact_bot_username_v2(100000001, 200000004).endswith("_max_bot")
    assert guardian_bot_username_v2(100000001, 200000002).endswith("_telemax_bot")
    assert not (tmp_path / "secrets").exists()
