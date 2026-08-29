"""Numbers and timestamps in the words the owner would have used.

`Последняя доставка: 1700000003000` was on the bridge card for months. It is the
value the code stores, and it is an answer to a question nobody asked: the owner
wants to know whether messages are moving, and «только что» says that in two
words where thirteen digits say nothing at all.

Three rules:

* **relative while it is still relative.** Under a minute and a half is «только
  что»; under an hour is «8 мин назад». Past that a clock time is more useful
  than a count of minutes nobody is going to do arithmetic on.
* **the owner's day, not UTC.** «сегодня в 08:18» has to agree with the clock on
  the phone, so everything here takes the configured zone. Telegram never tells
  a bot what the reader's zone is — that is the whole reason the guardian asks
  for it during onboarding.
* **Russian plurals are three-way.** `1 сообщение`, `2 сообщения`,
  `5 сообщений`. Getting this wrong is the kind of thing that makes a personal
  tool read like a machine.
"""

from __future__ import annotations

from datetime import datetime, tzinfo

__all__ = ["JUST_NOW", "count", "duration", "plural", "queue_words", "when"]

#: Short month names in the genitive, which is the case a date lands in.
_MONTHS = (
    "янв",
    "фев",
    "мар",
    "апр",
    "мая",
    "июн",
    "июл",
    "авг",
    "сен",
    "окт",
    "ноя",
    "дек",
)

#: Anything fresher than this reads as «только что». A minute and a half rather
#: than a minute: a delivery stamped 61 seconds ago is not news.
_JUST_NOW_MS = 90_000

JUST_NOW = "только что"


def plural(number: int, one: str, few: str, many: str) -> str:
    """The right of three Russian forms for `number`.

    11–14 are the exception every naive implementation gets wrong: they take the
    `many` form despite ending in 1–4.
    """
    number = abs(number)
    if number % 100 // 10 == 1:
        return many
    last = number % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def count(number: int, one: str, few: str, many: str) -> str:
    """`3 сообщения` — the number and its noun, agreeing."""
    return f"{number} {plural(number, one, few, many)}"


def when(stamp_ms: int | None, *, now_ms: int, tz: tzinfo | None = None) -> str | None:
    """A moment, as somebody looking at their phone would say it.

    None in, None out: «последняя доставка» has no answer before the first one,
    and the screen leaves the line out rather than inventing a zero.
    """
    if stamp_ms is None:
        return None
    delta = max(0, now_ms - stamp_ms)
    if delta < _JUST_NOW_MS:
        return JUST_NOW
    minutes = delta // 60_000
    if minutes < 60:
        return f"{minutes} мин назад"

    then = datetime.fromtimestamp(stamp_ms / 1000, tz)
    today = datetime.fromtimestamp(now_ms / 1000, tz).date()
    days = (today - then.date()).days
    if days <= 0:
        return f"сегодня в {then:%H:%M}"
    if days == 1:
        return f"вчера в {then:%H:%M}"
    return f"{then.day} {_MONTHS[then.month - 1]} в {then:%H:%M}"


def duration(span_ms: int | None) -> str | None:
    """How long something has been going on: «16 мин», «3 ч», «2 дн»."""
    if span_ms is None or span_ms < 0:
        return None
    minutes = span_ms // 60_000
    if minutes < 1:
        return "меньше минуты"
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч"
    return count(hours // 24, "день", "дня", "дней")


def queue_words(queued: int) -> str:
    """The queue, in one phrase. «пусто» is a real answer and the common one."""
    if queued <= 0:
        return "пусто"
    return count(queued, "сообщение", "сообщения", "сообщений")
