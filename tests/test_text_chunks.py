"""MAX text chunks are small, lossless and stable across a replay."""

from __future__ import annotations

import pytest

from bridge.routing.text_chunks import (
    MAX_TEXT_UTF16_LIMIT,
    split_max_text,
    text_part_source_key,
    utf16_units,
)


@pytest.mark.parametrize("length", [1, MAX_TEXT_UTF16_LIMIT])
def test_text_at_or_below_the_limit_stays_one_message(length: int) -> None:
    text = "я" * length
    assert split_max_text(text) == [text]


def test_one_character_over_the_limit_is_split_losslessly() -> None:
    text = "я" * (MAX_TEXT_UTF16_LIMIT + 1)
    chunks = split_max_text(text)

    assert [utf16_units(chunk) for chunk in chunks] == [MAX_TEXT_UTF16_LIMIT, 1]
    assert "".join(chunks) == text


def test_supplementary_characters_are_counted_as_two_utf16_units() -> None:
    text = "🙂" * 2_001
    chunks = split_max_text(text)

    assert [len(chunk) for chunk in chunks] == [2_000, 1]
    assert all(utf16_units(chunk) <= MAX_TEXT_UTF16_LIMIT for chunk in chunks)
    assert "".join(chunks) == text


def test_a_nearby_paragraph_boundary_is_preferred_and_preserved() -> None:
    text = "а" * 3_500 + "\n\n" + "б" * 700
    chunks = split_max_text(text)

    assert chunks[0].endswith("\n\n")
    assert "".join(chunks) == text
    assert all(utf16_units(chunk) <= MAX_TEXT_UTF16_LIMIT for chunk in chunks)


def test_part_keys_are_stable_and_single_text_keeps_its_old_key() -> None:
    assert text_part_source_key("tg:1:2", index=0, total=1) == "tg:1:2"
    assert text_part_source_key("tg:1:2", index=0, total=2) == "tg:1:2:text-part:1:2"
    assert text_part_source_key("tg:1:2", index=1, total=2) == "tg:1:2:text-part:2:2"


def test_an_impossible_limit_is_rejected() -> None:
    with pytest.raises(ValueError):
        split_max_text("🙂", limit=1)
