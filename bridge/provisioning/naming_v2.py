"""Bot usernames that two machines compute the same way, with no shared secret.

The V1 scheme keyed every username on `naming-secret` — 32 random bytes made
once per installation. It gave opaque names and it made the file load-bearing in
a way nobody chose: lose it and the guardian and every contact bot are renamed
at once, so a clean VPS with the same Telegram account and the same MAX account
could not work out what its own bots were called. Recovery depended on a file,
not on the identities the bots actually belong to.

V2 keys on the identities and nothing else:

    contact  = H("telemax-contact-bot-v2",  telegram_owner_id, max_peer_id)
    guardian = H("telemax-guardian-bot-v2", telegram_owner_id, max_owner_id)

Same accounts, any machine, same names. No secret, no installation id, no
hostname, no phone number, no contact name.

**What this costs, said plainly.** The digest is unkeyed, so anybody who can
guess a pair of ids can compute the username it produces. That is a real change
from V1: the id space is dense and the mapping is no longer one-way in practice.
What it buys is the only property that matters for recovery — a bot's name is a
function of who it is for, not of which disk it was created on. The names were
never secret in any useful sense anyway: they are public Telegram usernames, and
anybody who can see one can already resolve it.

**Framing.** The message is length-prefixed domain, then two fixed-width
big-endian u64s. Nothing is joined with `str` concatenation, so no pair of
inputs can be re-cut into another pair, and the two namespaces cannot collide
even where a Telegram id and a MAX id happen to be equal.

**Length.** Twenty base32 characters is 100 bits, above the 96-bit floor and
above V1's 80. `_max_bot` and `_telemax_bot` both fit inside Telegram's
thirty-two characters with it.

V1 stays in `naming` for one job only: computing the identity of a bridge made
before V2 whose username was never written down. A username that *is* written
down always wins over anything either scheme computes.
"""

from __future__ import annotations

import base64
import hashlib
from enum import StrEnum

from .naming import (
    _LEADING_DIGIT_MAP,
    CONTACT_SUFFIX,
    GUARD_SUFFIX,
    ensure_username,
)

#: Bumped only when the *shape* of the message changes. The domain strings carry
#: their own version too, which is belt and braces on purpose: this is a value
#: that can never be recomputed differently once bots exist under it.
SCHEME_VERSION = 1

#: Domain separation, and the whole reason a guardian and a contact bot cannot
#: land on one name even for identical numbers.
CONTACT_DOMAIN = "telemax-contact-bot-v2"
GUARDIAN_DOMAIN = "telemax-guardian-bot-v2"
# The Guardian must exist before MAX is connected: it is the surface where MAX
# login happens. V3 therefore keys the Guardian only on its Telegram owner. The
# contact-bot contract remains V2 and still includes both Telegram and MAX ids.
GUARDIAN_V3_DOMAIN = "telemax-guardian-bot-v3"

#: 20 base32 characters — 100 bits. Above the 96-bit floor, and both suffixes
#: still fit in Telegram's 32.
SLUG_LENGTH = 20

#: The widest an id may be. Telegram and MAX ids are far below this; the bound
#: exists so a wrong caller fails here rather than producing a truncated name.
MAX_ID = 2**64 - 1


class NamingVersion(StrEnum):
    """Which contract a bridge's username was minted under.

    Recorded, never recomputed. A bridge made under V1 keeps its V1 name for
    ever — renaming a bot is not a thing Telegram allows and would break every
    link the owner saved even if it were.
    """

    #: HMAC under `naming-secret`. Recoverable only from a persisted username.
    LEGACY = "v1-naming-secret"
    #: SHA-256 over the owner and peer ids. Recomputable anywhere.
    V2 = "v2-account-identity"
    #: Guardian-only contract: available before the MAX account is connected.
    V3 = "v3-telegram-owner"
    #: A guardian whose token was pasted in by hand, whose username is whatever
    #: the owner called it. Truth is what `getMe` says, and nothing else.
    ADOPTED = "adopted"


class IdentityError(ValueError):
    """An id that cannot be part of a username. A bug in the caller."""


def _u64(value: int, *, what: str) -> bytes:
    number = int(value)
    if number < 0 or number > MAX_ID:
        raise IdentityError(f"{what} is not a u64: {number}")
    return number.to_bytes(8, "big")


def digest(domain: str, first: int, second: int, *, what: tuple[str, str]) -> bytes:
    """`SHA-256(version || len(domain) || domain || u64be(a) || u64be(b))`.

    Length-prefixed and fixed-width throughout: two different pairs of inputs
    cannot produce one message, which plain concatenation of decimal strings
    would allow (`(1, 23)` and `(12, 3)`).
    """
    label = domain.encode("ascii")
    message = b"".join(
        (
            SCHEME_VERSION.to_bytes(1, "big"),
            len(label).to_bytes(2, "big"),
            label,
            _u64(first, what=what[0]),
            _u64(second, what=what[1]),
        )
    )
    return hashlib.sha256(message).digest()


def encode_username(raw: bytes, *, length: int = SLUG_LENGTH) -> str:
    """Base32, lowercase, cut to `length`, with a legal first character.

    Base32 emits `A-Z2-7`; lowercased, a leading `2`-`7` is a username Telegram
    refuses. The six digits map onto six fixed letters, which keeps the result
    deterministic and inside the same alphabet.
    """
    encoded = base64.b32encode(raw).decode("ascii").rstrip("=").lower()
    slug = encoded[:length]
    first = slug[0]
    if first in _LEADING_DIGIT_MAP:
        slug = _LEADING_DIGIT_MAP[first] + slug[1:]
    return slug


def contact_bot_username_v2(telegram_owner_user_id: int, max_peer_user_id: int) -> str:
    """The bot that carries one MAX contact, for one Telegram owner.

    Two owners bridging the same MAX person get different bots, because both ids
    are in the message. One owner bridging two people likewise.

    Nothing about the *contact* beyond their MAX id is in here: not their name,
    not their phone, not their avatar. Renaming somebody in MAX renames the bot
    and never its username.
    """
    return ensure_username(
        encode_username(
            digest(
                CONTACT_DOMAIN,
                telegram_owner_user_id,
                max_peer_user_id,
                what=("telegram owner id", "MAX peer id"),
            )
        )
        + CONTACT_SUFFIX
    )


def guardian_bot_username_v2(telegram_owner_user_id: int, max_owner_user_id: int) -> str:
    """The one bot that is not a bridge, for one pair of owner accounts.

    Both ids, because the guardian belongs to the *installation* rather than to
    either account alone: the same Telegram owner with a different MAX account
    is a different deployment and gets a different guardian.

    The product invariant this rests on is one MAX owner account per Telemax
    owner identity. A second MAX account under one Telegram owner would need a
    V3 with the peer's owner in the message; there is deliberately no room made
    for that here.
    """
    return ensure_username(
        encode_username(
            digest(
                GUARDIAN_DOMAIN,
                telegram_owner_user_id,
                max_owner_user_id,
                what=("telegram owner id", "MAX owner id"),
            )
        )
        + GUARD_SUFFIX
    )


def guardian_bot_username_v3(telegram_owner_user_id: int) -> str:
    """The first-install Guardian, derived before a MAX account exists.

    One Telegram owner gets one Guardian. MAX can then be linked, replaced or
    recovered inside that bot without changing the chat the owner already uses
    to control Telemax.
    """
    label = GUARDIAN_V3_DOMAIN.encode("ascii")
    message = b"".join(
        (
            SCHEME_VERSION.to_bytes(1, "big"),
            len(label).to_bytes(2, "big"),
            label,
            _u64(telegram_owner_user_id, what="telegram owner id"),
        )
    )
    return ensure_username(encode_username(hashlib.sha256(message).digest()) + GUARD_SUFFIX)


def names_a_contact_bot(username: str) -> bool:
    return username.lower().endswith(CONTACT_SUFFIX)


def names_a_guardian_bot(username: str) -> bool:
    return username.lower().endswith(GUARD_SUFFIX)
