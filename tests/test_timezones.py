"""The timezone — the one setting nothing can detect, and never typed by hand.

Every test here exists because the old free-text prompt accepted an answer that
was wrong in a way nobody noticed for weeks: `+3`, `MSK`, `Moscow`. The question
has since moved out of the console entirely — a terminal on a server cannot know
what the owner's own clock says — so what is left here is the zone arithmetic
and the list the guardian offers. The conversation itself is tested in
`test_onboarding_timezone.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from bridge.bootstrap import timezones
from bridge.bootstrap.timezones import (
    FALLBACK_TIMEZONE,
    RUSSIAN_TIMEZONES,
    detect_system_timezone,
    is_known_timezone,
    offset_label,
    timezone_choices,
)
from bridge.config.writer import write_timezone

WINTER = datetime(2026, 1, 15, tzinfo=UTC)


def test_the_system_zone_is_first_and_selected_by_default() -> None:
    """Enter must accept the host's own zone: it is right almost every time."""
    choices = timezone_choices("Asia/Yekaterinburg")

    assert choices[0].value == "Asia/Yekaterinburg"
    assert choices[0].is_system
    assert choices[0].label.startswith("Системный — Asia/Yekaterinburg")


def test_an_undetectable_zone_still_offers_a_usable_list() -> None:
    """MAX is a Russian service; guessing UTC would be wrong for most owners."""
    choices = timezone_choices(None)

    assert FALLBACK_TIMEZONE in [choice.value for choice in choices]
    assert all(not choice.is_system for choice in choices)


def test_an_invalid_system_zone_is_discarded() -> None:
    """`tzlocal` promises a string, not a zone `zoneinfo` can build."""
    assert not is_known_timezone("UTC+3")
    assert not is_known_timezone("Moscow")
    assert not is_known_timezone("")

    choices = timezone_choices("Mars/Olympus")
    assert all(not choice.is_system for choice in choices)
    assert choices[0].value == "Europe/Kaliningrad", "the Russian list starts as usual"


def test_the_system_zone_is_not_offered_twice() -> None:
    choices = timezone_choices("Europe/Moscow")
    values = [choice.value for choice in choices]

    assert values.count("Europe/Moscow") == 1
    assert choices[0].is_system


def test_a_positive_offset_is_formatted_as_utc_plus() -> None:
    assert offset_label("Europe/Moscow", at=WINTER) == "UTC+03:00"
    assert offset_label("Asia/Kolkata", at=WINTER) == "UTC+05:30", "half-hour zones exist"


def test_a_negative_offset_keeps_its_sign() -> None:
    assert offset_label("America/New_York", at=WINTER) == "UTC-05:00"


def test_only_the_iana_name_reaches_the_config(tmp_path: Path) -> None:
    """The label is for the menu. The config gets a name `zoneinfo` knows."""
    config = tmp_path / "config.yaml"
    config.write_text("telegram:\n  owner_user_id: 1\n", encoding="utf-8")

    choices = timezone_choices("Europe/Moscow")
    picked = choices[1]
    assert "—" in picked.label and "UTC" in picked.label

    assert write_timezone(config, picked.value)
    text = config.read_text(encoding="utf-8")
    assert f"timezone: {picked.value}" in text
    assert "—" not in text and "UTC+" not in text


def test_the_console_no_longer_knows_how_to_ask() -> None:
    """The regression this whole module exists for, one step further on.

    The zone is not merely un-typeable now: the console has no timezone step at
    all, so there is no prompt left to answer wrongly.
    """
    from bridge.bootstrap import flow

    assert not hasattr(flow, "choose_timezone")
    assert "timezone" not in str(flow.bootstrap.__doc__ or "")


def test_every_offered_zone_is_real() -> None:
    for choice in timezone_choices(None):
        assert is_known_timezone(choice.value), choice.value


def test_detection_either_answers_or_admits_it_cannot() -> None:
    """A wrong guess is worse than no guess: it is silently wrong for months."""
    detected = detect_system_timezone()
    assert detected is None or is_known_timezone(detected)


@pytest.mark.parametrize("city, name", RUSSIAN_TIMEZONES)
def test_each_russian_zone_shows_a_city_and_an_offset(city: str, name: str) -> None:
    label = timezones.describe(name, prefix=city)
    assert label.startswith(f"{city} — {name} (UTC")
