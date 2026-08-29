"""Which timezone to stamp messages in — chosen from a list, never typed.

The old prompt asked for a free-text IANA name, which is the one question a
person cannot reliably answer about their own machine: `+3`, `MSK`, `UTC+3` and
`Moscow` are all wrong, and each of them printed 1970 dates or crashed the
loader. So the answer is a menu, the host's own zone is the default, and the
only thing ever written to the config is the IANA name.

Offsets are computed, never stored: `Europe/Moscow` was UTC+04:00 between 2011
and 2014, and a hard-coded table is a lie waiting for the next time a government
changes its mind.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: Offered when the host has nothing valid to suggest. MAX is a Russian service
#: and its users are overwhelmingly in this zone.
FALLBACK_TIMEZONE = "Europe/Moscow"

#: The zones Russia actually uses, west to east. Enough for the owner of a MAX
#: account; anybody outside them has a system zone, which is offered first.
RUSSIAN_TIMEZONES: tuple[tuple[str, str], ...] = (
    ("Калининград", "Europe/Kaliningrad"),
    ("Москва", "Europe/Moscow"),
    ("Самара", "Europe/Samara"),
    ("Екатеринбург", "Asia/Yekaterinburg"),
    ("Омск", "Asia/Omsk"),
    ("Красноярск", "Asia/Krasnoyarsk"),
    ("Иркутск", "Asia/Irkutsk"),
    ("Якутск", "Asia/Yakutsk"),
    ("Владивосток", "Asia/Vladivostok"),
    ("Магадан", "Asia/Magadan"),
    ("Камчатка", "Asia/Kamchatka"),
)

SYSTEM_LABEL = "Системный"


@dataclass(frozen=True, slots=True)
class TimezoneChoice:
    """One line in the menu. `value` is all that reaches the config."""

    label: str
    value: str
    is_system: bool = False


def is_known_timezone(name: str | None) -> bool:
    """True when `zoneinfo` can actually build this zone."""
    if not name:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def offset_label(name: str, *, at: datetime | None = None) -> str:
    """`UTC+03:00`, computed now — DST and history included."""
    zone = ZoneInfo(name)
    if at is None:
        moment = datetime.now(zone)
    else:
        moment = (at if at.tzinfo else at.replace(tzinfo=UTC)).astimezone(zone)
    offset = moment.utcoffset()
    if offset is None:  # pragma: no cover - a zone always has one
        return "UTC+00:00"

    total = int(offset.total_seconds())
    sign = "-" if total < 0 else "+"
    hours, remainder = divmod(abs(total), 3600)
    minutes = remainder // 60
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def detect_system_timezone() -> str | None:
    """The host's own zone, or None when it cannot be established.

    `tzlocal` already reads `TZ`, `/etc/localtime` and `/etc/timezone` in the
    right order; what it does not do is promise the answer is a zone `zoneinfo`
    knows, so that is checked here. A wrong guess would print every timestamp in
    the wrong hour for months, which is much worse than asking.
    """
    try:
        from tzlocal import get_localzone_name
    except ImportError:  # pragma: no cover - declared dependency
        return None

    try:
        name = get_localzone_name()
    except Exception:  # noqa: BLE001 - any failure means the same thing
        return None

    return name if is_known_timezone(name) else None


def describe(name: str, *, prefix: str | None = None) -> str:
    """`Москва — Europe/Moscow (UTC+03:00)`."""
    head = f"{prefix} — " if prefix else ""
    return f"{head}{name} ({offset_label(name)})"


def city_of(name: str) -> str:
    """The name a person would use: `Europe/Moscow` -> `Москва`.

    Falls back to the last segment of the IANA name with the underscores taken
    out, which is wrong in no case worth a table — `Asia/Novosibirsk` reads as
    «Novosibirsk», not as a mystery.
    """
    for city, zone in RUSSIAN_TIMEZONES:
        if zone == name:
            return city
    return name.rsplit("/", 1)[-1].replace("_", " ")


def human_label(name: str) -> str:
    """`Москва · UTC+03:00` — the headline form, with no IANA name in it."""
    return f"{city_of(name)} · {offset_label(name)}"


def timezone_choices(system: str | None) -> list[TimezoneChoice]:
    """The menu: the host's zone first and selected, then Russia, no repeats."""
    choices: list[TimezoneChoice] = []
    seen: set[str] = set()

    if is_known_timezone(system) and system is not None:
        choices.append(
            TimezoneChoice(
                label=describe(system, prefix=SYSTEM_LABEL), value=system, is_system=True
            )
        )
        seen.add(system)

    for city, name in RUSSIAN_TIMEZONES:
        if name in seen or not is_known_timezone(name):
            continue
        choices.append(TimezoneChoice(label=describe(name, prefix=city), value=name))
        seen.add(name)

    if not choices:  # pragma: no cover - only with no tzdata at all
        choices.append(
            TimezoneChoice(label=describe(FALLBACK_TIMEZONE), value=FALLBACK_TIMEZONE)
        )
    return choices
