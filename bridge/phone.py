"""One definition of "that looks like a phone number".

Both halves of setup ask for one — the console for Telegram, the guardian bot
for MAX — and both must agree, or a number accepted in the terminal would be
rejected in the chat for no reason the owner can see.
"""

from __future__ import annotations

import re

#: `+` and 7–15 digits: the E.164 range, which is what both services want.
PHONE_PATTERN = re.compile(r"^\+\d{7,15}$")

_SEPARATORS = str.maketrans("", "", " -() ")


def normalize(value: str) -> str | None:
    """`+7 (900) 123-45-67` -> `+79001234567`, or None when it is not a phone.

    Normalising before validating matters: people paste numbers the way their
    address book shows them, and refusing that would be pedantry, not safety.
    """
    text = value.strip().translate(_SEPARATORS)
    if text.startswith("8") and len(text) == 11:
        # The Russian domestic form. MAX itself accepts only the international
        # one, so translating here saves an error nobody would understand.
        text = "+7" + text[1:]
    if not text.startswith("+"):
        text = "+" + text
    return text if PHONE_PATTERN.match(text) else None


def mask(value: str) -> str:
    """`+79001232051` -> `+7 ••• •••-20-51`.

    The owner has to recognise their own number to answer "use this one?", and
    nobody else has to be able to read it off a screenshot. Four digits is
    enough for the first and not enough for the second.
    """
    normalized = normalize(value) or value.strip()
    digits = "".join(character for character in normalized if character.isdigit())
    if len(digits) < 4:
        return "•" * len(digits)
    country = digits[0] if len(digits) == 11 else digits[: len(digits) - 10] or digits[0]
    tail = digits[-4:]
    return f"+{country} ••• •••-{tail[:2]}-{tail[2:]}"
