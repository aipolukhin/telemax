"""Formatting: MAX elements and Telegram entities are the same idea twice."""

from .elements import (
    SELF_EVIDENT,
    TO_MAX,
    TO_TELEGRAM,
    elements_to_entities,
    entities_to_elements,
    entities_to_markdown,
    shift_entities,
)
from .forwards import (
    FORWARD_ANONYMOUS,
    FORWARD_ARROW,
    FORWARD_FROM,
    forward_header,
    forward_prefix,
    utf16_length,
)
from .presentation import strip_presentation
from .stamps import format_clock, format_edit_mark, format_stamp

__all__ = [
    "FORWARD_ANONYMOUS",
    "FORWARD_ARROW",
    "FORWARD_FROM",
    "SELF_EVIDENT",
    "TO_MAX",
    "TO_TELEGRAM",
    "elements_to_entities",
    "entities_to_elements",
    "entities_to_markdown",
    "format_clock",
    "format_edit_mark",
    "format_stamp",
    "forward_header",
    "forward_prefix",
    "shift_entities",
    "strip_presentation",
    "utf16_length",
]
