"""Keeping secrets out of the logs.

A personal bridge logs to journald on a machine the owner controls, which is
exactly the sort of place people stop being careful. Three things must never
appear there, and each has burned somebody before:

* **bot tokens** — the owner pastes one into a Telegram chat during
  provisioning, and it travels through code that logs its arguments;
* **the phone number and the MAX auth code** — enough to take over the account;
* **the setup token** — whoever holds it can start onboarding, so it is masked
  in the deep link and in the `/start` that carries it;
* **message text** — the whole point of the bridge is that it is private.

The filter runs on the logging handler, so it catches messages from libraries
too, not only from code that remembered to be careful.
"""

from __future__ import annotations

import logging
import re
from re import Pattern

MASK = "***"

# `123456789:AA...` — a Telegram bot token. Matched anywhere in the line,
# including inside a repr or a URL.
TOKEN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")

# api.telegram.org/bot<token>/method — the token is in the path.
TOKEN_IN_URL = re.compile(r"(/bot)\d{6,12}:[A-Za-z0-9_-]{30,}")

# A phone number with a country code. Keeps the last two digits so two accounts
# can still be told apart in a log.
PHONE = re.compile(r"\+\d{6,13}(\d{2})\b")

# The MAX login code, however it is labelled.
AUTH_CODE = re.compile(r"((?:code|код|otp|sms)\W{0,3})\d{4,8}", re.IGNORECASE)

# Session and auth tokens in key=value or JSON form. `hash` is in the list for
# `api_hash`, which is an account credential and not a digest.
SECRET_FIELD = re.compile(
    r"((?:token|session|secret|password|passwd|auth|cookie|api_hash|hash)\w*\W{0,3})"
    r"[\"']?([A-Za-z0-9_\-.]{8,})",
    re.IGNORECASE,
)

# The one-time setup payload, in the deep link and in a `/start` command alike.
# It is the key to onboarding, so it is masked wherever it appears.
SETUP_TOKEN = re.compile(r"((?:\?start=|/start\s+))[A-Za-z0-9_-]{16,}")

# `Authorization: Bearer ...` and friends.
BEARER = re.compile(r"(bearer\s+)[A-Za-z0-9_\-.]{8,}", re.IGNORECASE)

_RULES: tuple[tuple[Pattern[str], str], ...] = (
    (TOKEN_IN_URL, r"\1" + MASK),
    (TOKEN, MASK),
    (PHONE, "+" + MASK + r"\1"),
    (AUTH_CODE, r"\1" + MASK),
    (SETUP_TOKEN, r"\1" + MASK),
    (BEARER, r"\1" + MASK),
    (SECRET_FIELD, r"\1" + MASK),
)


def redact(text: str) -> str:
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    """Rewrites a record's message before any handler formats it.

    Arguments are redacted too: `logger.info("token=%s", token)` is the shape
    this exists for, and only touching `record.msg` would miss it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)

        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: redact(value) if isinstance(value, str) else value
                    for key, value in record.args.items()
                }
            else:
                record.args = tuple(
                    redact(value) if isinstance(value, str) else value for value in record.args
                )

        if record.exc_text:
            record.exc_text = redact(record.exc_text)

        return True
