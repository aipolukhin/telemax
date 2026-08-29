"""Text formatting across the two protocols.

Both sides describe formatting the same way — a list of ranges over the text —
and, luckily, both count offsets in **UTF-16 code units**. So the ranges
translate one to one and an emoji in the middle of a sentence does not shift
anything.

MAX element types are taken from PyMax's own markdown formatter, which is the
closest thing to a specification that exists: `STRONG`, `EMPHASIZED`,
`UNDERLINE`, `STRIKETHROUGH`, `MONOSPACED`, `CODE`, `LINK`, `HEADING`.

What does not survive is named rather than silently dropped:

* Telegram spoilers and custom emoji have no MAX equivalent — the text stays,
  the effect goes;
* MAX headings have no Telegram entity — the line stays, the size goes.
"""

from __future__ import annotations

from typing import Any

# Telegram entity type -> MAX element type.
TO_MAX: dict[str, str] = {
    "bold": "STRONG",
    "italic": "EMPHASIZED",
    "underline": "UNDERLINE",
    "strikethrough": "STRIKETHROUGH",
    "code": "MONOSPACED",
    "pre": "CODE",
    "text_link": "LINK",
}

# MAX element type -> Telegram entity type.
TO_TELEGRAM: dict[str, str] = {
    "STRONG": "bold",
    "EMPHASIZED": "italic",
    "UNDERLINE": "underline",
    "STRIKETHROUGH": "strikethrough",
    "MONOSPACED": "code",
    "CODE": "pre",
    "LINK": "text_link",
    # A heading has no entity; bold is the closest thing that survives.
    "HEADING": "bold",
}

# Telegram marks these itself when it renders the text, so re-sending them as
# formatting would be redundant and sometimes wrong.
SELF_EVIDENT = {"url", "mention", "hashtag", "bot_command", "email", "phone_number"}


def _kind_of(value: object) -> str:
    """MAX element types arrive as strings, or as PyMax enums whose `str()` is
    `ElementType.STRONG` rather than `STRONG`."""
    if value is None:
        return ""
    return str(getattr(value, "value", value)).upper()


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        result: Any = dump(mode="python", by_alias=True, exclude_none=True)
        if isinstance(result, dict):
            return result
    return {}


def entities_to_elements(entities: Any) -> list[dict[str, Any]]:
    """Telegram entities -> MAX elements. Unknown types are dropped, text is not."""
    elements: list[dict[str, Any]] = []

    for entity in entities or []:
        raw = _as_dict(entity)
        kind = str(raw.get("type") or "")
        if kind in SELF_EVIDENT:
            continue

        mapped = TO_MAX.get(kind)
        if mapped is None:
            # Spoilers, custom emoji, blockquotes: the text is already in the
            # message, only the decoration is lost.
            continue

        element: dict[str, Any] = {
            "type": mapped,
            "from": int(raw.get("offset") or 0),
            "length": int(raw.get("length") or 0),
        }
        url = raw.get("url")
        if mapped == "LINK" and url:
            element["attributes"] = {"url": str(url)}
        elements.append(element)

    return elements


def elements_to_entities(elements: Any) -> list[dict[str, Any]]:
    """MAX elements -> Telegram entities, as plain dicts aiogram can validate."""
    entities: list[dict[str, Any]] = []

    for element in elements or []:
        raw = _as_dict(element)
        kind = _kind_of(raw.get("type"))
        mapped = TO_TELEGRAM.get(kind)
        if mapped is None:
            continue

        offset = raw.get("from") if raw.get("from") is not None else raw.get("from_")
        length = raw.get("length")
        if offset is None or not length:
            continue

        entity: dict[str, Any] = {
            "type": mapped,
            "offset": int(offset),
            "length": int(length),
        }
        if mapped == "text_link":
            url = _as_dict(raw.get("attributes")).get("url")
            if not url:
                # A link with no target is not a link; keep the words, drop the
                # entity rather than send Telegram something it will reject.
                continue
            entity["url"] = str(url)
        entities.append(entity)

    return entities


def shift_entities(entities: list[dict[str, Any]], offset: int) -> list[dict[str, Any]]:
    """Move every range along, for when a prefix is added to the text.

    The "Вы: " marker on the owner's own messages does exactly that, and without
    this the formatting would land four characters to the left.
    """
    if not offset:
        return entities
    return [dict(entity, offset=int(entity["offset"]) + offset) for entity in entities]


# PyMax parses markdown out of the text it sends, so this is how formatting
# reaches MAX. It has no escape syntax, which is a real hazard: a message that
# merely *contains* a marker is reinterpreted. Rendering is therefore only done
# when Telegram actually reported formatting — plain text is left alone.
_MARKDOWN: dict[str, tuple[str, str]] = {
    "bold": ("**", "**"),
    "italic": ("_", "_"),
    "underline": ("__", "__"),
    "strikethrough": ("~~", "~~"),
    "code": ("`", "`"),
    "pre": ("```", "```"),
}


def entities_to_markdown(text: str, entities: Any) -> str:
    """Render Telegram entities as the markdown PyMax will parse back.

    Offsets are UTF-16 code units on both sides, so the text is cut in that
    encoding and reassembled — slicing the Python string directly would drift
    on every emoji.
    """
    ranges = [_as_dict(entity) for entity in entities or []]
    ranges = [
        item
        for item in ranges
        if str(item.get("type")) in _MARKDOWN or str(item.get("type")) == "text_link"
    ]
    if not ranges:
        return text

    units = text.encode("utf-16-le")

    def cut(start: int, end: int) -> str:
        return units[start * 2 : end * 2].decode("utf-16-le")

    # Innermost first, so nesting survives.
    ranges.sort(key=lambda item: (int(item["offset"]), -int(item["length"])))

    out: list[str] = []
    cursor = 0
    for item in ranges:
        offset, length = int(item["offset"]), int(item["length"])
        if offset < cursor:
            continue  # overlapping ranges: keep the first, drop the rest
        out.append(cut(cursor, offset))
        body = cut(offset, offset + length)
        kind = str(item["type"])
        if kind == "text_link":
            out.append(f"[{body}]({item.get('url', '')})")
        else:
            opener, closer = _MARKDOWN[kind]
            out.append(f"{opener}{body}{closer}")
        cursor = offset + length

    out.append(cut(cursor, len(units) // 2))
    return "".join(out)
