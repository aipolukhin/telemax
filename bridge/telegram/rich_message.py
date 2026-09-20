"""Render Telegram Premium Rich Messages into MAX-safe markdown.

Telegram's visual editor does not put its contents in ``Message.message``.
The ordinary text field is empty and the visible post lives in
``Message.rich_message.blocks`` as Instant-View style page blocks. Treating
that shape as an empty message loses the post before the durable queue can see
it, so this module reduces the block tree to the same markdown string the
ordinary owner-intake path already sends to MAX.

The renderer is deliberately text-first. Embedded rich-post media is named
with a visible placeholder and its caption rather than silently discarded;
ordinary Telegram photo/document messages continue through the native media
pipeline. Unknown wrappers are traversed structurally so a newer formatting
type keeps its words even before the bridge learns its decoration.
"""

from __future__ import annotations

from typing import Any

from telethon.tl import types  # type: ignore[import-untyped]


def _rich_text(value: Any) -> str:
    """Flatten one ``RichText`` node, preserving supported MAX formatting."""
    if value is None or isinstance(value, types.TextEmpty):
        return ""
    if isinstance(value, types.TextPlain):
        return str(value.text or "")
    if isinstance(value, types.TextConcat):
        return "".join(_rich_text(item) for item in (value.texts or []))
    if isinstance(value, types.TextCustomEmoji):
        return str(value.alt or "")
    if isinstance(value, types.TextMath):
        return str(value.source or "")
    if isinstance(value, types.TextImage):
        return "[Изображение]"

    inner = _rich_text(getattr(value, "text", None))
    if isinstance(value, types.TextBold):
        return f"**{inner}**"
    if isinstance(value, types.TextItalic):
        return f"_{inner}_"
    if isinstance(value, types.TextUnderline):
        return f"__{inner}__"
    if isinstance(value, types.TextStrike):
        return f"~~{inner}~~"
    if isinstance(value, types.TextFixed):
        return f"`{inner}`"
    if isinstance(value, types.TextUrl):
        return f"[{inner}]({value.url})" if value.url else inner
    if isinstance(value, types.TextEmail):
        return f"[{inner}](mailto:{value.email})" if value.email else inner
    if isinstance(value, types.TextPhone):
        return f"[{inner}](tel:{value.phone})" if value.phone else inner

    # Spoilers and semantic wrappers (mention, hashtag, date, marked text,
    # sub/superscript, auto-links) have no guaranteed MAX equivalent. Their
    # visible child is the lossless fallback and avoids inventing markup that
    # PyMax might reinterpret.
    if inner:
        return inner

    texts = getattr(value, "texts", None)
    if texts:
        return "".join(_rich_text(item) for item in texts)
    for field in ("alt", "source"):
        fallback = getattr(value, field, None)
        if fallback:
            return str(fallback)
    return ""


def _caption(value: Any) -> str:
    if value is None:
        return ""
    text = _rich_text(getattr(value, "text", None))
    credit = _rich_text(getattr(value, "credit", None))
    return " — ".join(part for part in (text, credit) if part)


def _indent(text: str, prefix: str, continuation: str) -> str:
    lines = text.splitlines() or [""]
    return "\n".join(
        f"{prefix if index == 0 else continuation}{line}"
        for index, line in enumerate(lines)
    )


def _list_item(item: Any, *, prefix: str) -> str:
    text = _rich_text(getattr(item, "text", None))
    if not text:
        text = _blocks(getattr(item, "blocks", None))
    if getattr(item, "checkbox", False):
        mark = "[x] " if getattr(item, "checked", False) else "[ ] "
        prefix = f"{prefix}{mark}"
    return _indent(text, prefix, " " * len(prefix)).rstrip()


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def _block(block: Any) -> str:
    if block is None or isinstance(block, (types.PageBlockAnchor, types.PageBlockUnsupported)):
        return ""
    if isinstance(block, types.PageBlockDivider):
        return "—"
    if isinstance(block, types.PageBlockMath):
        return str(block.source or "")

    if isinstance(
        block,
        (
            types.PageBlockTitle,
            types.PageBlockHeader,
            types.PageBlockSubheader,
            types.PageBlockHeading1,
            types.PageBlockHeading2,
            types.PageBlockHeading3,
            types.PageBlockHeading4,
            types.PageBlockHeading5,
            types.PageBlockHeading6,
        ),
    ):
        text = _rich_text(block.text)
        return f"**{text}**" if text else ""
    if isinstance(
        block,
        (
            types.PageBlockParagraph,
            types.PageBlockSubtitle,
            types.PageBlockKicker,
            types.PageBlockFooter,
            types.PageBlockThinking,
        ),
    ):
        return _rich_text(block.text)
    if isinstance(block, types.PageBlockAuthorDate):
        return _rich_text(block.author)
    if isinstance(block, types.PageBlockPreformatted):
        text = _rich_text(block.text)
        return f"```\n{text}\n```" if text else ""
    if isinstance(block, (types.PageBlockBlockquote, types.PageBlockPullquote)):
        body = _rich_text(block.text)
        caption = _rich_text(block.caption)
        joined = "\n".join(part for part in (body, caption) if part)
        return _quote(joined) if joined else ""
    if isinstance(block, types.PageBlockBlockquoteBlocks):
        body = _blocks(block.blocks)
        caption = _rich_text(block.caption)
        joined = "\n".join(part for part in (body, caption) if part)
        return _quote(joined) if joined else ""

    if isinstance(block, types.PageBlockList):
        return "\n".join(
            _list_item(item, prefix="• ") for item in (block.items or [])
        ).rstrip()
    if isinstance(block, types.PageBlockOrderedList):
        start = int(block.start or 1)
        rendered: list[str] = []
        for index, item in enumerate(block.items or []):
            number = getattr(item, "num", None)
            if not number:
                number = str(getattr(item, "value", None) or start + index)
            rendered.append(_list_item(item, prefix=f"{number}. "))
        return "\n".join(rendered).rstrip()

    if isinstance(block, types.PageBlockDetails):
        title = _rich_text(block.title)
        body = _blocks(block.blocks)
        return "\n\n".join(part for part in (f"**{title}**" if title else "", body) if part)
    if isinstance(block, types.PageBlockCover):
        return _block(block.cover)
    if isinstance(block, (types.PageBlockCollage, types.PageBlockSlideshow)):
        body = _blocks(block.items)
        caption = _caption(block.caption)
        return "\n\n".join(part for part in (body, caption) if part)
    if isinstance(block, types.PageBlockEmbedPost):
        body = _blocks(block.blocks)
        caption = _caption(block.caption)
        return "\n\n".join(part for part in (body, caption, block.url or "") if part)
    if isinstance(block, types.PageBlockEmbed):
        caption = _caption(block.caption)
        return "\n".join(part for part in (block.url or "", caption) if part)

    if isinstance(block, types.PageBlockTable):
        title = _rich_text(block.title)
        rows = [
            " | ".join(_rich_text(cell.text) for cell in (row.cells or []))
            for row in (block.rows or [])
        ]
        return "\n".join(part for part in (title, *rows) if part)
    if isinstance(block, types.PageBlockRelatedArticles):
        title = _rich_text(block.title)
        articles = [
            " — ".join(
                part
                for part in (
                    str(article.title or article.description or ""),
                    str(article.url or ""),
                )
                if part
            )
            for article in (block.articles or [])
        ]
        return "\n".join(part for part in (title, *articles) if part)

    media_labels = {
        types.PageBlockPhoto: "[Фото]",
        types.PageBlockVideo: "[Видео]",
        types.PageBlockAudio: "[Аудио]",
        types.PageBlockMap: "[Карта]",
    }
    for media_type, label in media_labels.items():
        if isinstance(block, media_type):
            caption = _caption(getattr(block, "caption", None))
            return " — ".join(part for part in (label, caption) if part)

    # Forward-compatible fallback for new container or textual page blocks.
    children = getattr(block, "blocks", None) or getattr(block, "items", None)
    if children:
        return _blocks(children)
    return _rich_text(getattr(block, "text", None))


def _blocks(blocks: Any) -> str:
    rendered = (_block(block).strip() for block in (blocks or []))
    return "\n\n".join(part for part in rendered if part)


def rich_message_to_markdown(value: Any) -> str:
    """Return the visible text of a Telegram ``RichMessage`` block tree."""
    return _blocks(getattr(value, "blocks", None)).strip()
