"""The Telethon-shaped half of owner MTProto intake: reduce a message to plain
data, classify its media by MTProto attributes, and re-fetch by reference.

Kept apart from the normaliser so that one is framework-agnostic and this one
owns every Telethon type. Classification never trusts the bare
`MessageMediaDocument` constructor — voice, round video, video, audio and a plain
document all arrive as one, told apart only by their document attributes, exactly
as the live probe showed. It reuses `plan_upload` so the MAX-side kind is decided
in one place, the same as the Bot API path. No PyMax anywhere.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from telethon.extensions import markdown  # type: ignore[import-untyped]
from telethon.tl import types  # type: ignore[import-untyped]

from bridge.formatting import forward_header
from bridge.media.store import TempFiles
from bridge.media.upload import plan_upload
from bridge.routing.echo import media_echo_fingerprint, text_echo_fingerprint
from bridge.telegram.forwards import is_mtproto_forward, mtproto_forward_name
from bridge.telegram.mtproto_intake import OwnerMedia, OwnerMessage
from bridge.telegram.rich_message import rich_message_to_markdown

logger = logging.getLogger(__name__)


class MediaUnavailableError(Exception):
    """The owner-side message could not be turned back into a file."""


def _classify(message: Any) -> tuple[str, str] | None:
    """`(UploadKind.value, file_name)` for a media message, or None when there is
    no media this increment carries."""
    media = getattr(message, "media", None)
    if isinstance(media, types.MessageMediaPhoto):
        return plan_upload(photo=True, file_id="", file_name="photo.jpg").kind.value, "photo.jpg"
    if isinstance(media, types.MessageMediaDocument):
        attributes = getattr(getattr(media, "document", None), "attributes", None) or []
        name: str | None = None
        flags: dict[str, bool] = {}
        for attribute in attributes:
            if isinstance(attribute, types.DocumentAttributeFilename):
                name = attribute.file_name
            elif isinstance(attribute, types.DocumentAttributeSticker):
                flags["sticker"] = True
            elif isinstance(attribute, types.DocumentAttributeAudio):
                flags["voice" if attribute.voice else "audio"] = True
            elif isinstance(attribute, types.DocumentAttributeVideo):
                key = "video_note" if getattr(attribute, "round_message", False) else "video"
                flags[key] = True
        if not flags:
            flags["document"] = True
        kind = plan_upload(file_id="", file_name=name or "", **flags).kind.value
        return kind, name or f"{kind}.bin"
    return None


def owner_media(message: Any, *, account_id: int, peer_id: int) -> OwnerMedia | None:
    classified = _classify(message)
    if classified is None:
        return None
    kind, name = classified
    grouped = getattr(message, "grouped_id", None)
    reference = {
        "account_id": account_id,
        "peer": peer_id,
        "owner_message_id": int(message.id),
        "part_index": 0,
        "grouped_id": int(grouped) if grouped is not None else None,
        "kind": kind,
        "name": name,
    }
    return OwnerMedia(kind=kind, reference=reference)


def _echo_media_kind(message: Any) -> str | None:
    """The shared kind name for what a contact bot sent, from MTProto alone.

    Told apart by document attributes rather than by the constructor, the same
    way `_classify` does it — a voice note, a round video and a plain file all
    arrive as `MessageMediaDocument`. None for anything this increment will not
    bind on structure: a sticker (rebuilt on the way out, so it does not come
    back as what it was) or a media type nothing here recognises.
    """
    media = getattr(message, "media", None)
    if isinstance(media, types.MessageMediaPhoto):
        return "photo"
    if not isinstance(media, types.MessageMediaDocument):
        return None
    attributes = getattr(getattr(media, "document", None), "attributes", None) or []
    for attribute in attributes:
        if isinstance(attribute, types.DocumentAttributeSticker):
            return None
        if isinstance(attribute, types.DocumentAttributeAudio):
            return "voice" if attribute.voice else "audio"
        if isinstance(attribute, types.DocumentAttributeVideo):
            return "video_note" if getattr(attribute, "round_message", False) else "video"
    return "document"


def echo_fingerprint_of(message: Any) -> str | None:
    """What an incoming contact-bot message looks like, in canonical form.

    The other half of the comparison `claim_from_max` wrote down before sending:
    same vocabulary, same shape, computed from what actually arrived. Text is read
    raw rather than through the markdown unparser, because raw is what the sender
    passed to Telegram — the entities travel beside it and never re-escape it.

    None means "not something this increment binds": an album (grouped, its own
    increment) or a kind that does not survive the trip as itself.
    """
    if getattr(message, "grouped_id", None) is not None:
        return None
    text = getattr(message, "message", "") or ""
    if getattr(message, "media", None) is None:
        return text_echo_fingerprint(text) if text else None
    kind = _echo_media_kind(message)
    return media_echo_fingerprint(kind, caption=text) if kind is not None else None


def echo_album_part_of(message: Any) -> dict[str, Any] | None:
    """One part of an incoming album, described structurally. None if not one.

    The counterpart of `echo_fingerprint_of` for the shape it deliberately
    refuses: a grouped message, which cannot be matched on its own because the
    group is what was sent. What comes back is what the part *is* — its kind, the
    caption if this is the part carrying one, and the group it belongs to — and
    the caption's position is in there because Telegram puts it wherever the
    sender put it, which for an outgoing album is the head and for an incoming
    one was, in the live probe, the second of three.
    """
    grouped = getattr(message, "grouped_id", None)
    if grouped is None:
        return None
    caption = getattr(message, "message", "") or ""
    return {
        "grouped_id": int(grouped),
        "kind": _echo_media_kind(message),
        "caption": caption or None,
    }


def owner_message_from(
    message: Any,
    *,
    account_id: int,
    peer_id: int,
    forward_line: str | None = None,
) -> OwnerMessage:
    """Reduce a Telethon message to the plain `OwnerMessage` intake works with.

    Text and captions travel as markdown, produced by Telethon's own unparser —
    not a new formatter — so PyMax parses them the same way it does the Bot API
    path's markdown.

    `forward_line` is the whole `[28/07 09:14] ↪ Переслано от Аня` line, built
    by the caller: it holds the session that looks the author up against their
    own account, and the timestamp settings that decide whether a clock is drawn
    at all. This function stays synchronous and offline, and what it can read off
    the update by itself is only the fallback — that name is the author as the
    owner has them saved rather than as they call themselves, and it carries no
    stamp.
    """
    rich_text = rich_message_to_markdown(getattr(message, "rich_message", None))
    text = rich_text or markdown.unparse(message.message or "", message.entities or [])
    if is_mtproto_forward(message):
        # After the unparser, never before: it works from entity offsets over
        # the owner's own text, and a line put in front of it would move every
        # one of them. Plain rather than markdown for the same reason the Bot
        # API path keeps it plain — PyMax's markdown has no escape syntax.
        line = forward_line or forward_header(mtproto_forward_name(message))
        text = f"{line}{text}"
    grouped = getattr(message, "grouped_id", None)
    reply = getattr(message, "reply_to", None)
    reply_id = getattr(reply, "reply_to_msg_id", None) if reply is not None else None
    return OwnerMessage(
        account_id=account_id,
        peer_id=peer_id,
        message_id=int(message.id),
        text=text,
        media=owner_media(message, account_id=account_id, peer_id=peer_id),
        grouped_id=int(grouped) if grouped is not None else None,
        reply_to_message_id=int(reply_id) if reply_id else None,
        contact_vcard=owner_contact_vcard(message),
    )


def owner_contact_vcard(message: Any) -> str | None:
    """The vCard for a contact the owner shared, or None if this is not one.

    Telethon exposes a shared contact as `MessageMediaContact`, whose fields are
    the structured ones — `phone_number`, `first_name`, `last_name` — plus a
    `vcard` that is usually empty. The card is built from the structured fields,
    the same as the Bot API path, so MAX always gets a `TEL` and an `FN` to
    parse; a phone is present by construction on a contact media.
    """
    media = getattr(message, "media", None)
    if not isinstance(media, types.MessageMediaContact):
        return None
    name = " ".join(
        part
        for part in (getattr(media, "first_name", "") or "", getattr(media, "last_name", "") or "")
        if part.strip()
    ).strip() or "Контакт"
    phone = str(getattr(media, "phone_number", "") or "").strip()

    def esc(value: str) -> str:
        return (
            value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
        )

    return (
        "BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        f"FN:{esc(name)}\r\n"
        f"TEL;TYPE=CELL:{esc(phone)}\r\n"
        "END:VCARD"
    )


class MtprotoMediaSource:
    """Re-fetches a media job's parts over the owner session, by reference.

    The one place the session downloads bytes — used by the delivery worker on
    the first attempt and on every retry, so nothing perishable is stored. A
    deleted or unreachable source raises `MediaUnavailableError`, which the existing
    retry/FAILED policy turns into an honest outcome rather than a masked one.
    """

    def __init__(self, *, client_provider: Callable[[], Any], temp_files: TempFiles) -> None:
        self._client_provider = client_provider
        self._temp = temp_files

    async def fetch(
        self, stack: AsyncExitStack, ref: dict[str, Any]
    ) -> tuple[str, Path, str]:
        client = self._client_provider()
        if client is None:
            raise MediaUnavailableError("the owner session is not connected")
        message_id = int(ref["owner_message_id"])
        kind = str(ref["kind"])
        name = str(ref.get("name") or f"{kind}.bin")
        message = await client.get_messages(ref["peer"], ids=message_id)
        if message is None or getattr(message, "media", None) is None:
            raise MediaUnavailableError(f"owner message {message_id} is gone or carries no media")
        path = stack.enter_context(self._temp.reserve(suffix=Path(name).suffix))
        await client.download_media(message, file=str(path))
        return kind, path, name
