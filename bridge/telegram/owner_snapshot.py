"""What the puppet session last saw of one of the owner's own messages.

`UpdateEditMessage` carries the message as it *is*, never what changed, and the
same constructor arrives for a text edit, for a reaction, and for both at once.
So the reading of an update is a subtraction: this snapshot against the one
before it. Two independent differences fall out — content and reactions — and
either, both, or neither may be non-empty.

The reaction half is an ordered **set**, not a value. Telegram has no "replace":
changing a reaction is adding the new one beside the old and taking the old one
off, so the middle state carries both.
`chosen_order` is what orders it, and it is also what identifies the owner's own
reactions.

MAX holds one reaction per message, so the set is projected onto a single
*representative* — the last one the owner chose. The projection is what decides
whether MAX is touched at all: taking off a reaction that was not the
representative changes the set and changes nothing MAX can show.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Final, Literal

#: A reaction the owner chose, tagged by what kind it is. An ordinary emoji and
#: a custom one are different things and are never allowed to compare equal: a
#: `document_id` names a sticker in somebody's pack, and turning it into a
#: nearby Unicode emoji would show the contact a reaction the owner never made.
ReactionKind = Literal["e", "c"]

#: The canonical empty set. Written out rather than computed so a row can be
#: seeded without importing the encoder.
EMPTY_CHOSEN: Final[str] = "[]"


@dataclass(frozen=True, slots=True, order=True)
class Chosen:
    """One of the owner's own reactions. `value` is an emoji or a document id."""

    kind: ReactionKind
    value: str

    @property
    def emoji(self) -> str | None:
        """The emoji, or None for a custom one — which has no emoji at all."""
        return self.value if self.kind == "e" else None


def content_fingerprint_of(message: Any) -> str:
    """A stable hash of what an edit of this message would actually carry.

    The markdown body, and only that. Deliberately **not** the forward line: it
    is drawn from the original author and never changes when the owner edits,
    and building it needs the session to look a name up — a comparison that has
    to work from a bare message must not depend on a network call.

    Media is out for the same reason it is out of the edit path: what the bridge
    carries for an edit is text, so a fingerprint that moved when a photo was
    swapped would ask for an edit that changes nothing.

    Same algorithm as `bridge.routing.owner_mutation.fingerprint`, duplicated
    rather than imported so this module stays free of the routing layer; a test
    holds the two together.
    """
    from telethon.extensions import markdown  # type: ignore[import-untyped]

    body = markdown.unparse(getattr(message, "message", "") or "", message.entities or [])
    return _hash(body)


def content_fingerprint_of_placement(text: str, entities: Any = None) -> str:
    """The same fingerprint, from what is *about to be* placed.

    A session gets no update for a message it sent itself, so a message the
    bridge places as the owner never reaches `content_fingerprint_of` — there is
    no `Message` object to read. Its baseline has to be written from the two
    things the placement was built out of instead, and they are the same two
    things Telegram will store: `message` and `entities`.

    That equality is the whole contract, and it holds only because the placement
    passes `parse_mode=None`. With a parse mode Telethon re-reads the text as
    markdown, the stored body is not the text that went in, and a baseline
    written from it would say the message is something it is not.

    `entities` are MTProto entities, already converted — the same list handed to
    Telethon, never the Bot API dicts.
    """
    from telethon.extensions import markdown

    return _hash(markdown.unparse(text or "", list(entities or [])))


def _hash(body: str) -> str:
    """Truncated SHA-256, the one place the algorithm is written down."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def chosen_of(message: Any) -> tuple[Chosen, ...]:
    """The owner's own reactions on a message, in the order they chose them.

    Only entries carrying a `chosen_order` are read, which is what keeps another
    person's reaction out of the owner's set. In a contact-bot dialog there is
    never one — bots cannot react — but a channel aggregate would otherwise walk
    straight in, and the rule costs nothing.
    """
    reactions = getattr(message, "reactions", None)
    results = getattr(reactions, "results", None) or []
    picked: list[tuple[int, Chosen]] = []
    for count in results:
        order = getattr(count, "chosen_order", None)
        if order is None:
            continue
        value = getattr(count, "reaction", None)
        emoticon = getattr(value, "emoticon", None)
        if emoticon:
            picked.append((int(order), Chosen("e", str(emoticon))))
            continue
        document_id = getattr(value, "document_id", None)
        if document_id is not None:
            picked.append((int(order), Chosen("c", str(document_id))))
        # Anything else (a paid reaction, a constructor we have not met) is not
        # a reaction this bridge can carry and is left out of the set rather
        # than encoded as something it is not.
    picked.sort(key=lambda item: item[0])
    return tuple(chosen for _, chosen in picked)


def encode_chosen(chosen: tuple[Chosen, ...]) -> str:
    """Canonical JSON: the same logical set is always the same bytes.

    Order is the owner's own choosing order and is meaning, not formatting — it
    decides the representative — so it is preserved rather than sorted away.
    The separators are pinned and `ensure_ascii` is off, so an emoji is one
    character in the row and two states can be compared as strings.
    """
    return json.dumps(
        [[item.kind, item.value] for item in chosen],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def decode_chosen(raw: str) -> tuple[Chosen, ...]:
    """Read a stored set back. A row we cannot parse is treated as unknown."""
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(parsed, list):
        return ()
    out: list[Chosen] = []
    for item in parsed:
        if isinstance(item, list) and len(item) == 2 and item[0] in ("e", "c"):
            out.append(Chosen(item[0], str(item[1])))
    return tuple(out)


def representative(chosen: tuple[Chosen, ...]) -> Chosen | None:
    """The one reaction MAX will show: the last the owner chose.

    None for an empty set, which is what a removal looks like once the last of
    them is gone.
    """
    return chosen[-1] if chosen else None


@dataclass(frozen=True, slots=True)
class OwnerSnapshot:
    """One reading of an owner message: identity, content and reactions.

    `pts` is the version of the update this was read from. A snapshot taken by
    fetching the message rather than by receiving an update carries `pts = 0` —
    it is a picture of the present, not a position in the update stream, and
    every real update is newer than it by construction.
    """

    account_id: int
    bot_id: int
    message_id: int
    content_fingerprint: str
    chosen: tuple[Chosen, ...]
    pts: int = 0

    @property
    def chosen_json(self) -> str:
        return encode_chosen(self.chosen)

    @property
    def representative(self) -> Chosen | None:
        return representative(self.chosen)
