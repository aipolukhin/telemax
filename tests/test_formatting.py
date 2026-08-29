"""Formatting conversion — the ranges must survive both trips."""

from __future__ import annotations

from bridge.formatting import (
    elements_to_entities,
    entities_to_elements,
    entities_to_markdown,
    shift_entities,
)


def test_bold_and_link_map_both_ways() -> None:
    entities = [
        {"type": "bold", "offset": 0, "length": 4},
        {"type": "text_link", "offset": 5, "length": 3, "url": "https://x"},
    ]

    elements = entities_to_elements(entities)
    assert elements[0] == {"type": "STRONG", "from": 0, "length": 4}
    assert elements[1]["attributes"] == {"url": "https://x"}

    assert elements_to_entities(elements) == entities


def test_max_only_types_degrade_but_keep_the_text() -> None:
    assert elements_to_entities([{"type": "HEADING", "from": 0, "length": 3}])[0]["type"] == "bold"
    # A link without a url is not sendable to Telegram; the words remain.
    assert elements_to_entities([{"type": "LINK", "from": 0, "length": 3}]) == []


def test_telegram_only_types_are_dropped_not_faked() -> None:
    assert entities_to_elements([{"type": "spoiler", "offset": 0, "length": 3}]) == []
    assert entities_to_elements([{"type": "url", "offset": 0, "length": 3}]) == []


def test_offsets_are_utf16_so_emoji_do_not_shift_anything() -> None:
    """🔥 is two UTF-16 units; a naive slice would cut one character short."""
    text = "🔥 bold"
    markdown = entities_to_markdown(text, [{"type": "bold", "offset": 3, "length": 4}])
    assert markdown == "🔥 **bold**"


def test_plain_text_is_never_reinterpreted() -> None:
    """PyMax parses markdown out of anything it sends; untouched text stays untouched."""
    assert entities_to_markdown("2 * 2 = 4", []) == "2 * 2 = 4"


def test_links_render_as_markdown() -> None:
    rendered = entities_to_markdown(
        "see docs", [{"type": "text_link", "offset": 4, "length": 4, "url": "https://x"}]
    )
    assert rendered == "see [docs](https://x)"


def test_prefix_shifts_every_range() -> None:
    shifted = shift_entities([{"type": "bold", "offset": 0, "length": 2}], 4)
    assert shifted[0]["offset"] == 4
