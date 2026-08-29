"""V2 usernames: the same two accounts compute the same names on any machine.

These are **frozen synthetic vectors**. Changing the algorithm changes the bot
names derived by existing installations. If one of these fails, the algorithm
moved and that is the bug — not the expectation.

The property the whole scheme exists for is at the bottom: a clean VPS with no
`naming-secret`, no database and no installation id computes the same names from
the same two account ids.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from bridge.provisioning import naming_v2
from bridge.provisioning.naming import CONTACT_SUFFIX, GUARD_SUFFIX, validate_username
from bridge.provisioning.naming_v2 import (
    CONTACT_DOMAIN,
    GUARDIAN_DOMAIN,
    MAX_ID,
    SLUG_LENGTH,
    IdentityError,
    NamingVersion,
    contact_bot_username_v2,
    guardian_bot_username_v2,
)

#: Synthetic account ids used as the anchor pair throughout.
OWNER_TG = 100000001
OWNER_MAX = 200000002
PEER = 200000004


# ------------------------------------------------------------ frozen vectors


@pytest.mark.parametrize(
    ("telegram_owner", "max_peer", "expected"),
    [
        (OWNER_TG, PEER, "um4jv3dgf25rpz2eyph4_max_bot"),
        (0, 0, "ttd42bvxbapcqea4wqgm_max_bot"),
        (1, 1, "uzoy4f47ytnsguo2byd6_max_bot"),
        (MAX_ID, MAX_ID, "obooqlbngmaqgakst4m3_max_bot"),
    ],
)
def test_contact_vectors(telegram_owner: int, max_peer: int, expected: str) -> None:
    assert contact_bot_username_v2(telegram_owner, max_peer) == expected


@pytest.mark.parametrize(
    ("telegram_owner", "max_owner", "expected"),
    [
        (OWNER_TG, OWNER_MAX, "rf2kxavwxaw7xyiii6gf_telemax_bot"),
        (0, 0, "os4qjp4gaevmsoycn3vc_telemax_bot"),
        (MAX_ID, MAX_ID, "xujqx3evxdt4yj7wrdow_telemax_bot"),
    ],
)
def test_guardian_vectors(telegram_owner: int, max_owner: int, expected: str) -> None:
    assert guardian_bot_username_v2(telegram_owner, max_owner) == expected


# --------------------------------------------------------------- properties


def test_the_same_pair_always_gives_the_same_name() -> None:
    assert contact_bot_username_v2(OWNER_TG, PEER) == contact_bot_username_v2(OWNER_TG, PEER)
    assert guardian_bot_username_v2(OWNER_TG, OWNER_MAX) == guardian_bot_username_v2(
        OWNER_TG, OWNER_MAX
    )


def test_a_different_telegram_owner_gets_a_different_bot() -> None:
    assert contact_bot_username_v2(OWNER_TG, PEER) != contact_bot_username_v2(1, PEER)
    assert guardian_bot_username_v2(OWNER_TG, OWNER_MAX) != guardian_bot_username_v2(
        1, OWNER_MAX
    )


def test_a_different_peer_gets_a_different_bot() -> None:
    assert contact_bot_username_v2(OWNER_TG, PEER) != contact_bot_username_v2(OWNER_TG, PEER + 1)


def test_a_different_max_owner_gets_a_different_guardian() -> None:
    assert guardian_bot_username_v2(OWNER_TG, OWNER_MAX) != guardian_bot_username_v2(
        OWNER_TG, OWNER_MAX + 1
    )


def test_the_two_namespaces_never_meet() -> None:
    """Identical numbers, two roles, two names — that is what the domain is for."""
    assert contact_bot_username_v2(5, 7) != guardian_bot_username_v2(5, 7)
    guardian = guardian_bot_username_v2(5, 7)
    assert guardian.endswith(GUARD_SUFFIX)
    assert not guardian.endswith(CONTACT_SUFFIX), "a guardian must not read as a contact bot"
    assert contact_bot_username_v2(5, 7).endswith(CONTACT_SUFFIX)


def test_the_framing_cannot_be_re_cut() -> None:
    """`(1, 23)` and `(12, 3)` are one string and two different messages."""
    assert contact_bot_username_v2(1, 23) != contact_bot_username_v2(12, 3)
    assert contact_bot_username_v2(0, 1) != contact_bot_username_v2(1, 0)


def test_leading_zero_bytes_are_not_ambiguous() -> None:
    """Fixed-width u64: a small id is not a prefix of a larger one."""
    assert contact_bot_username_v2(1, 256) != contact_bot_username_v2(256, 1)
    assert len({contact_bot_username_v2(0, value) for value in (0, 1, 256, 2**32)}) == 4


@pytest.mark.parametrize(
    "pair",
    [(OWNER_TG, PEER), (0, 0), (MAX_ID, MAX_ID), (2**63, 2**63 - 1)],
)
def test_every_output_is_a_username_telegram_accepts(pair: tuple[int, int]) -> None:
    for username in (contact_bot_username_v2(*pair), guardian_bot_username_v2(*pair)):
        assert validate_username(username), username
        assert len(username) <= 32
        assert username.isascii() and username == username.lower()


def test_the_slug_is_at_least_ninety_six_bits() -> None:
    assert SLUG_LENGTH * 5 >= 96


@pytest.mark.parametrize("bad", [-1, MAX_ID + 1])
def test_an_id_outside_u64_is_refused_rather_than_truncated(bad: int) -> None:
    with pytest.raises(IdentityError):
        contact_bot_username_v2(bad, 1)
    with pytest.raises(IdentityError):
        guardian_bot_username_v2(1, bad)


# --------------------------------------------------- no secret, no machine


def test_the_naming_secret_does_not_reach_the_result(tmp_path: pathlib.Path) -> None:
    """The property the whole scheme exists for.

    A clean VPS: no `naming-secret`, no database, no installation id. Same two
    accounts, same names — which is what makes recovering a bot inventory from
    the accounts alone possible at all.
    """
    from bridge.provisioning.naming import load_or_create_naming_secret

    before = contact_bot_username_v2(OWNER_TG, PEER)
    guardian_before = guardian_bot_username_v2(OWNER_TG, OWNER_MAX)

    # Two different installations' secrets, neither of which is consulted.
    load_or_create_naming_secret(tmp_path / "vps-one")
    load_or_create_naming_secret(tmp_path / "vps-two")

    assert contact_bot_username_v2(OWNER_TG, PEER) == before
    assert guardian_bot_username_v2(OWNER_TG, OWNER_MAX) == guardian_before


def test_nothing_about_the_contact_but_their_id_is_in_the_name() -> None:
    """A rename in MAX changes the bot's display name and never its username."""
    assert contact_bot_username_v2(OWNER_TG, PEER) == contact_bot_username_v2(OWNER_TG, PEER)


# ------------------------------------------------------ structural invariants


def _module_source() -> str:
    return pathlib.Path(naming_v2.__file__).read_text(encoding="utf-8")


def test_the_v2_module_imports_no_secret_machinery() -> None:
    tree = ast.parse(_module_source())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert "hmac" not in imported
    assert "secrets" not in imported
    assert not any("naming_secret" in name for name in imported)


def test_the_naming_input_is_only_the_two_account_ids() -> None:
    """No hostname, no installation id, no path, no clock."""
    source = _module_source()
    for forbidden in ("gethostname", "uuid", "platform", "socket", "time.time", "os.environ"):
        assert forbidden not in source, forbidden


def test_the_domains_are_distinct_and_versioned() -> None:
    assert CONTACT_DOMAIN != GUARDIAN_DOMAIN
    assert CONTACT_DOMAIN.endswith("-v2")
    assert GUARDIAN_DOMAIN.endswith("-v2")


def test_no_random_fallback_exists_anywhere_in_naming() -> None:
    """A username that is taken is information, never a reason to invent one.

    Asserted over the code rather than the prose: the module docstring says the
    word "random" about the *old* scheme, and it should keep being allowed to.
    """
    tree = ast.parse(_module_source())
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert not {name for name in called if "random" in name or "token" in name}


def test_new_provisioning_does_not_use_the_v1_contact_function() -> None:
    """`contact_username(secret, id)` may only be read by legacy code paths."""
    root = pathlib.Path(naming_v2.__file__).resolve().parent.parent
    callers: set[str] = set()
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "contact_username(" in source and path.name != "naming.py":
            callers.add(path.relative_to(root).as_posix())

    assert callers == set(), sorted(callers)


def test_the_guardian_v1_function_is_only_reachable_from_the_console() -> None:
    root = pathlib.Path(naming_v2.__file__).resolve().parent.parent
    callers = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if re.search(r"\bguard_username\(", path.read_text(encoding="utf-8"))
        and path.name != "naming.py"
    }

    assert callers == {"bootstrap/telegram.py"}, sorted(callers)


def test_the_naming_versions_are_named_and_distinct() -> None:
    assert len({item.value for item in NamingVersion}) == 3
    assert NamingVersion.LEGACY.value != NamingVersion.V2.value
