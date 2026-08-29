"""The console half of setup: everything that must happen before Telegram can.

Four things cannot be done from a chat, and this package is exactly those four:
the timezone nobody can detect reliably, the Telegram login that needs a code
typed by a human, the guardian bot that only a user account can create, and the
system service that keeps the whole thing running after the terminal closes.

Everything else — MAX, dialogs, status, restarts — happens in the guardian bot.
"""

from .timezones import (
    FALLBACK_TIMEZONE,
    RUSSIAN_TIMEZONES,
    TimezoneChoice,
    detect_system_timezone,
    is_known_timezone,
    offset_label,
    timezone_choices,
)

__all__ = [
    "FALLBACK_TIMEZONE",
    "RUSSIAN_TIMEZONES",
    "TimezoneChoice",
    "detect_system_timezone",
    "is_known_timezone",
    "offset_label",
    "timezone_choices",
]
