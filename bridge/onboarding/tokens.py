"""The one-time link that carries the owner from the terminal into Telegram.

A bot cannot open a private chat with somebody who has never pressed Start, so
the handoff has to be a link the owner taps. That link is public the moment it
exists — it travels through Telegram's servers, sits in Saved Messages, and may
be screenshotted — so it must be worth nothing to anybody else.

Four properties do that, and each is tested:

* **unguessable** — 32 bytes from `secrets`, not a counter, not a timestamp;
* **short-lived** — an hour is plenty to switch from SSH to a phone;
* **bound to one account** — a stranger holding the link still fails the owner
  check, so the token is a second lock, not the only one;
* **single use** — consumed on the first successful `/start`, so a link found
  later in a chat history opens nothing.

Only the SHA-256 of the token is ever written down. Somebody reading the state
file finds a hash, and a hash cannot be sent to a bot.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from enum import StrEnum

#: 256 bits of entropy, URL-safe. Long enough that brute force is not a threat
#: model, short enough for a `t.me/...?start=` link to stay tappable.
TOKEN_BYTES = 32

#: The owner is expected to walk from a terminal to a phone, not to next week.
DEFAULT_TTL_SECONDS = 3600


class Verdict(StrEnum):
    """Why a `/start` payload was accepted or refused."""

    OK = "ok"
    UNKNOWN = "unknown"
    EXPIRED = "expired"
    USED = "used"
    WRONG_OWNER = "wrong_owner"


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """The plaintext (handed out once) and the digest (written down)."""

    plaintext: str
    digest: str
    expires_at: int


def digest_of(token: str) -> str:
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def issue(*, ttl_seconds: int = DEFAULT_TTL_SECONDS, now: int | None = None) -> IssuedToken:
    plaintext = secrets.token_urlsafe(TOKEN_BYTES)
    moment = now if now is not None else int(time.time())
    return IssuedToken(
        plaintext=plaintext,
        digest=digest_of(plaintext),
        expires_at=moment + ttl_seconds,
    )


def verify(
    *,
    presented: str,
    expected_digest: str | None,
    expires_at: int | None,
    used_at: int | None,
    owner_user_id: int,
    sender_user_id: int,
    now: int | None = None,
) -> Verdict:
    """Check a `/start` payload. Order matters: identity first.

    The owner check comes before the token check so that a stranger holding a
    valid link learns nothing about whether it was valid — they get the same
    refusal either way.
    """
    if sender_user_id != owner_user_id:
        return Verdict.WRONG_OWNER
    if not expected_digest:
        return Verdict.UNKNOWN
    # Constant time: the digest is not secret, but comparing it in constant time
    # costs nothing and removes the question.
    if not hmac.compare_digest(digest_of(presented), expected_digest):
        return Verdict.UNKNOWN
    if used_at is not None:
        return Verdict.USED
    moment = now if now is not None else int(time.time())
    if expires_at is not None and moment > expires_at:
        return Verdict.EXPIRED
    return Verdict.OK


def deep_link(bot_username: str, token: str) -> str:
    return f"https://t.me/{bot_username.lstrip('@')}?start={token}"
