"""Who originally wrote a message the owner forwarded into the bridge.

Two transports carry the same fact in two shapes. Bot API says `forward_origin`,
a union of four cases since Bot API 7.0; MTProto says `fwd_from`, whose fields
are peers rather than names. Both are read by attribute rather than by type, so
neither aiogram nor Telethon is imported here and the reader cannot be broken by
a library upgrade renaming a model.

What comes out is a name or None. None is a real answer — Telegram hides the
origin of a forward from a user who forbade being linked — and it is rendered as
the anonymous form rather than guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _person(first: Any, last: Any) -> str:
    return " ".join(part for part in (_text(first), _text(last)) if part)


def bot_api_forward_origin(message: Any) -> str | None:
    """The name behind aiogram's `forward_origin`, or None if there is none.

    Returning None both for "not a forward" and for "a forward that names
    nobody" would lose the distinction, so callers check `forward_origin`
    themselves; this only answers *who*.
    """
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return None

    # MessageOriginUser: an ordinary person who allows being linked.
    user = getattr(origin, "sender_user", None)
    if user is not None:
        name = _text(getattr(user, "full_name", None)) or _person(
            getattr(user, "first_name", None), getattr(user, "last_name", None)
        )
        return name or None

    # MessageOriginHiddenUser: a person who does not. Telegram sends the name
    # they had at the time and nothing else — no id, no link.
    hidden = _text(getattr(origin, "sender_user_name", None))
    if hidden:
        return hidden

    # MessageOriginChat: a group posting under its own title.
    chat = getattr(origin, "sender_chat", None) or getattr(origin, "chat", None)
    if chat is not None:
        title = _text(getattr(chat, "title", None)) or _text(getattr(chat, "full_name", None))
        signature = _text(getattr(origin, "author_signature", None))
        if title and signature:
            # MessageOriginChannel with a signed post: both halves are what the
            # reader in Telegram would have seen.
            return f"{title} ({signature})"
        return title or None

    return None


def is_bot_api_forward(message: Any) -> bool:
    return getattr(message, "forward_origin", None) is not None


def _date_ms(value: Any) -> int | None:
    """A Telegram date as milliseconds, whether it arrived typed or as a number.

    Bot API dates are seconds; aiogram and Telethon both hand them over as
    `datetime`. MAX counts in milliseconds everywhere, and so does every stamp
    in this bridge, so the conversion belongs here rather than at each caller.
    """
    if value is None:
        return None
    timestamp = getattr(value, "timestamp", None)
    if callable(timestamp):
        try:
            return int(timestamp() * 1000)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        return int(value) * 1000
    except (TypeError, ValueError):
        return None


def bot_api_forward_date(message: Any) -> int | None:
    """When the forwarded message was originally written, in milliseconds."""
    origin = getattr(message, "forward_origin", None)
    return _date_ms(getattr(origin, "date", None)) if origin is not None else None


def mtproto_forward_date(message: Any) -> int | None:
    """As `bot_api_forward_date`, off a Telethon message."""
    fwd = getattr(message, "forward", None)
    if fwd is None:
        fwd = getattr(message, "fwd_from", None)
    return _date_ms(getattr(fwd, "date", None)) if fwd is not None else None


def forward_peer_id(message: Any) -> int | None:
    """The id of the account the forwarded message was written by.

    A forward names its author as a peer, not as a name, and the peer is the
    only part of it worth trusting: the name that rides along in the update is
    the author *as this account has them saved*, down to an address-book rename.
    Asking Telegram about the id gets the account's own profile back.

    None for a sender who forbade being linked — there is deliberately no id to
    look up then, only the name they had at the time.
    """
    fwd = getattr(message, "forward", None)
    if fwd is None:
        fwd = getattr(message, "fwd_from", None)
    if fwd is None:
        return None

    peer = getattr(fwd, "from_id", None)
    for attribute in ("user_id", "channel_id", "chat_id"):
        value = getattr(peer, attribute, None)
        if value is not None:
            return int(value)
    return None


def name_of_entity(entity: Any) -> str | None:
    """A Telegram user or channel, as one name. None when it has none."""
    if entity is None:
        return None
    title = _text(getattr(entity, "title", None))
    if title:
        return title
    person = _person(getattr(entity, "first_name", None), getattr(entity, "last_name", None))
    if person:
        return person
    username = _text(getattr(entity, "username", None))
    return f"@{username}" if username else None


@dataclass(frozen=True, slots=True)
class ForwardAuthor:
    """Who wrote a forwarded message, in the two parts the header needs.

    `username` is the half that is genuinely theirs — nobody can rename it from
    the outside — so it is both the safe identifier and the link target.
    """

    name: str | None = None
    username: str | None = None

    def __bool__(self) -> bool:
        return bool(self.name or self.username)


def author_of_entity(entity: Any) -> ForwardAuthor:
    """A resolved user or channel — never under the owner's label for them.

    For anyone in the owner's address book, every name Telegram will hand this
    session is the one the owner filed them under. There is no authoritative
    profile-name field in a forward header: clients resolve the peer locally.

    So a contact contributes their username and nothing else — the owner cannot
    rename that, and the header renders it as the address itself. Anyone the
    owner has *not* saved is already carrying their own name, and a channel
    title is the same for everybody, so both are used as they are.
    """
    if entity is None:
        return ForwardAuthor()
    username = _text(getattr(entity, "username", None)) or None
    if getattr(entity, "contact", False):
        return ForwardAuthor(username=username)
    return ForwardAuthor(name=name_of_entity(entity), username=username)


def mtproto_forward_name(message: Any, *, names: dict[int, str] | None = None) -> str | None:
    """The name behind a Telethon forward.

    **The fallback, not the answer.** What this reads is whatever the update
    happened to carry, which is the author as *this* account has them saved —
    an address-book rename included. The name that belongs on a forward is the
    one the author's own account holds, so the intake fetches it by id and
    passes it in; this is what is left when that lookup finds nothing.

    `message.forward` before `message.fwd_from`: the former is Telethon's
    wrapper and carries the entities Telegram shipped inside the update, the
    latter is the raw `MessageFwdHeader` where the author is a bare peer and
    nothing else. `from_name` is only set for a *hidden* sender.

    `names` stays as the last resort for a peer nothing else resolved; an
    unresolved peer is an anonymous forward, never an id the owner cannot read.
    """
    fwd = getattr(message, "forward", None)
    if fwd is None:
        fwd = getattr(message, "fwd_from", None)
    if fwd is None:
        return None

    # Telethon resolves these from the update's own entity list; a channel post
    # is a `chat`, a person is a `sender`.
    for holder in (getattr(fwd, "chat", None), getattr(fwd, "sender", None)):
        if holder is None:
            continue
        name = _text(getattr(holder, "title", None)) or _person(
            getattr(holder, "first_name", None), getattr(holder, "last_name", None)
        )
        if name:
            return name

    # Set only for a sender who forbade being linked — Telegram sends the name
    # they had at the time and nothing else.
    named = _text(getattr(fwd, "from_name", None))
    if named:
        return named

    peer = getattr(fwd, "from_id", None)
    peer_id = None
    for attribute in ("user_id", "channel_id", "chat_id"):
        value = getattr(peer, attribute, None)
        if value is not None:
            peer_id = int(value)
            break
    if peer_id is not None and names:
        resolved = _text(names.get(peer_id))
        if resolved:
            return resolved

    post_author = _text(getattr(fwd, "post_author", None))
    return post_author or None


def is_mtproto_forward(message: Any) -> bool:
    return (getattr(message, "forward", None) or getattr(message, "fwd_from", None)) is not None


def bot_api_forward_author(message: Any) -> ForwardAuthor:
    """The author of a Bot API forward, name and username together.

    A bot has no address book, so what it is told about a person *is* their
    profile — the reason this side of the bridge is the one that can answer at
    all. A channel origin has a title and a public link of the same shape, so it
    travels the same way.
    """
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return ForwardAuthor()
    user = getattr(origin, "sender_user", None)
    if user is not None:
        return ForwardAuthor(
            name=name_of_entity(user),
            username=_text(getattr(user, "username", None)) or None,
        )
    chat = getattr(origin, "sender_chat", None) or getattr(origin, "chat", None)
    username = _text(getattr(chat, "username", None)) or None if chat is not None else None
    return ForwardAuthor(name=bot_api_forward_origin(message), username=username)
