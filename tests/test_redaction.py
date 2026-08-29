"""WP13 — nothing secret reaches a log line."""

from __future__ import annotations

import io
import logging

from bridge.observability import MASK, configure_logging, redact

TOKEN = "123456789:AAdummy-token-value-that-is-long-enough"
PHONE = "+79161234567"


def test_bot_tokens_are_masked() -> None:
    assert TOKEN not in redact(f"starting bot with token {TOKEN}")
    assert MASK in redact(TOKEN)
    # Also inside an API URL, where aiohttp likes to log it.
    assert TOKEN not in redact(f"POST https://api.telegram.org/bot{TOKEN}/sendMessage")


def test_phone_keeps_only_its_tail() -> None:
    """Enough to tell two accounts apart, not enough to be a phone number."""
    line = redact(f"logging in as {PHONE}")
    assert PHONE not in line
    assert line.endswith("67")


def test_auth_codes_and_secret_fields_are_masked() -> None:
    assert "12345" not in redact("SMS code 12345 received")
    assert "s3cr3t-value" not in redact("session_token=s3cr3t-value")


def test_the_filter_covers_lazy_arguments() -> None:
    """`logger.info("token=%s", token)` is the shape this exists for."""
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)

    logging.getLogger("test").info("bot token is %s", TOKEN)
    logging.getLogger("test").info("phone %s", PHONE)

    output = stream.getvalue()
    assert TOKEN not in output
    assert PHONE not in output
    assert MASK in output


def test_tracebacks_are_redacted() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)

    try:
        raise RuntimeError(f"failed for {TOKEN}")
    except RuntimeError:
        logging.getLogger("test").exception("delivery failed")

    assert TOKEN not in stream.getvalue()
