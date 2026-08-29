"""Deterministic bot usernames: the same owner, the same contact, the same name.

Setup is re-runnable. A guardian bot re-created on a second bootstrap must land
on the *same* `@username` as the first one, or every link the owner saved points
at a bot that no longer exists — and a contact whose bot is rebuilt must keep the
username their chat is filed under. So the name cannot come from a counter, a
timestamp or a random suffix: it has to be a pure function of an identifier that
never changes.

The identifier itself must not leak. `sha256(telegram_id)` is deterministic and
completely reversible: Telegram ids are a dense integer space and MAX user ids
are worse, so a plain digest of either is a lookup table away from being the id
in clear. The name is therefore an **HMAC** under a key generated once per
installation (`naming-secret`, 0600, never rotated), with the namespace mixed
into the message so the guardian and a contact bot cannot collide even if the
two id spaces overlap.

Two shapes come out of it, both inside Telegram's 32-character limit:

    <16-char slug>_telemax_bot    the guardian, keyed on the Telegram owner id
    <16-char slug>_max_bot        a contact, keyed on the MAX user id

Display names are free to change — a contact renaming themselves in MAX renames
the *bot*, never its username.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

#: The keyed secret behind every generated username. Losing it loses the ability
#: to reproduce the names, which is why nothing here ever rewrites it.
NAMING_SECRET_FILE = "naming-secret"  # noqa: S105 - a file name, not a secret

#: 32 bytes is the HMAC-SHA256 block-equivalent key size; more would be hashed
#: down, less would weaken the keying.
NAMING_SECRET_BYTES = 32

#: Base32 of a SHA-256 digest, cut to 16 characters: 80 bits. Collisions are not
#: a concern at the scale of a personal address book, and the name stays short
#: enough that both suffixes fit.
SLUG_LENGTH = 16

#: Domain separation. The same integer as a Telegram id and as a MAX user id must
#: not produce the same slug.
GUARD_NAMESPACE = "telegram-owner"
PEER_NAMESPACE = "max-peer"

GUARD_SUFFIX = "_telemax_bot"
CONTACT_SUFFIX = "_max_bot"

#: What the guardian calls itself in the bot list. Only the username is
#: deterministic; this is prose and may change between versions.
GUARD_DISPLAY_NAME = "Telemax"

MIN_USERNAME_LENGTH = 5
MAX_USERNAME_LENGTH = 32

#: Telegram: letters, digits and underscores, starting with a letter, ending in
#: `bot` for a bot account. Checked rather than assumed — a username the code
#: generates but @BotFather refuses would strand provisioning halfway.
USERNAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{3,30}[a-z0-9]$")

#: Base32 emits `A-Z2-7`; lowercased, a leading `2`-`7` would be a username
#: Telegram refuses. Mapping them onto six fixed letters keeps the result
#: deterministic and inside the same alphabet.
_LEADING_DIGIT_MAP = {"2": "u", "3": "v", "4": "w", "5": "x", "6": "y", "7": "z"}


class InvalidUsernameError(ValueError):
    """A generated username would not survive @BotFather. A bug, not input."""


class NamingSecretUnusableError(RuntimeError):
    """The file exists and holds nothing. Refused rather than replaced.

    Generating a new one silently is the failure this module exists to prevent:
    it renames the guardian and every V1 contact bot at once, strands all of
    them, and looks like nothing happened. Only a V1 installation ever asks for
    this, and only to recognise bots it already has.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(
            f"{path} is empty. It is the only thing that can reproduce the names of"
            " bots created before V2 naming; restore it from a backup, or delete it"
            " deliberately to accept that those names are unrecoverable."
        )


def load_or_create_naming_secret(secrets_dir: Path) -> bytes:
    """The V1 HMAC key. **Legacy only.**

    V2 usernames are functions of the two owner accounts and never touch this
    file — a clean machine with the same Telegram and MAX accounts computes the
    same names with no secret at all. What still needs it is one thing:
    recomputing the name of a bot created *before* V2 whose username was never
    written down.

    An existing file always wins. An existing *empty* file is an error, not an
    invitation: replacing it renames the guardian and every V1 contact bot at
    once, which is exactly the outcome the rest of this module is written to
    avoid, and it used to happen in silence.
    """
    secrets_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(secrets_dir, stat.S_IRWXU)

    path = secrets_dir / NAMING_SECRET_FILE
    if path.exists():
        stored = path.read_bytes().strip()
        if stored:
            # Not logged, not returned in any message: this is the one value
            # from which every V1 username is reproducible.
            return stored
        raise NamingSecretUnusableError(path)

    created = base64.urlsafe_b64encode(secrets.token_bytes(NAMING_SECRET_BYTES))
    # Written through a private-mode open so the bytes are never briefly 0644.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(created)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return created


def stable_slug(
    naming_secret: bytes,
    namespace: str,
    raw_id: int | str,
    *,
    length: int = SLUG_LENGTH,
) -> str:
    """`HMAC(secret, "<namespace>:<id>")`, base32, lowercase, cut to `length`.

    Keyed, so the id cannot be recovered by enumeration; namespaced, so the two
    id spaces stay apart; and a pure function of its arguments, so the same
    contact keeps the same name forever.
    """
    payload = f"{namespace}:{raw_id}".encode()
    digest = hmac.new(naming_secret, payload, hashlib.sha256).digest()
    encoded = base64.b32encode(digest).decode("ascii").rstrip("=").lower()

    slug = encoded[:length]
    first = slug[0]
    if first in _LEADING_DIGIT_MAP:
        slug = _LEADING_DIGIT_MAP[first] + slug[1:]
    return slug


def guard_username(naming_secret: bytes, telegram_owner_id: int) -> str:
    """The guardian's username for this Telegram account, forever."""
    slug = stable_slug(naming_secret, GUARD_NAMESPACE, int(telegram_owner_id))
    return ensure_username(f"{slug}{GUARD_SUFFIX}")


def contact_username(naming_secret: bytes, max_peer_id: int) -> str:
    """One MAX contact's bot username, keyed on their permanent MAX user id.

    Deliberately *not* the chat id: a dialog can be recreated, and the person on
    the other end is the thing the bot is for.
    """
    slug = stable_slug(naming_secret, PEER_NAMESPACE, int(max_peer_id))
    return ensure_username(f"{slug}{CONTACT_SUFFIX}")


def validate_username(username: str) -> bool:
    """Everything Telegram requires of a bot username, checked in one place."""
    if not username or username != username.lower():
        return False
    if not MIN_USERNAME_LENGTH <= len(username) <= MAX_USERNAME_LENGTH:
        return False
    if not username.endswith("bot"):
        return False
    if not username.isascii():
        return False
    return bool(USERNAME_PATTERN.match(username))


def ensure_username(username: str) -> str:
    """`validate_username` as an assertion, for the generating side."""
    if not validate_username(username):
        raise InvalidUsernameError(f"generated username is not usable: {username!r}")
    return username


def guard_display_name() -> str:
    return GUARD_DISPLAY_NAME
