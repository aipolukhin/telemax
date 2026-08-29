"""MAX protocol operations the bridge calls directly.

PyMax exposes most of the protocol through typed methods, but three things it
gets wrong or does not offer at all:

* `MSG_TYPING` has no typed wrapper — the outgoing indicator does not exist in
  PyMax 2.3.1 at all.
* the reaction payloads type `messageId` as a string, and the server answers
  `proto.payload: Expected number` and then closes the connection.

Everything else goes through PyMax.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum


class Opcode(IntEnum):
    #: `CONTACT_PRESENCE`. Answers `{"presence": {"<user id>": {"seen", "status"}}}`
    #: for the ids asked about, or for every contact when asked for none.
    CONTACT_PRESENCE = 35
    #: `CHAT_MARK`. Our own read mark, and the contact's second tick: the server
    #: forwards it as `NOTIF_MARK` and the other side's app reads a message as
    #: read when its time is at or before the mark. PyMax has `read_message`, but
    #: it always sends `mark = now`, which would tell the contact that everything
    #: written up to this second has been read — see `read_frame`.
    CHAT_MARK = 50
    MSG_SEND = 64
    MSG_TYPING = 65
    #: `MSG_EDIT`, in both directions: the bridge sends it to change a message,
    #: and the server pushes it when one was changed. Watched for the same reason
    #: as `NOTIF_MESSAGE` — PyMax types both through the same model, and drops
    #: both the same way when it refuses.
    MSG_EDIT = 67
    #: Hands out an upload slot for a sticker image on `iusmile.oneme.ru`, a host
    #: of its own. Unlike the video one this is synchronous: the token is good
    #: straight away, with no `op65` intent and no wait for `NOTIF_ATTACH`.
    STICKER_UPLOAD = 81
    #: Hands out an upload slot for a voice message or a video note — the second
    #: media pipeline PyMax has no wrapper for (its enum stops at VIDEO_UPLOAD=82
    #: for ordinary video). Same opcode, told apart by `type` in the request:
    #: `2` answers a voice URL on `au.oneme.ru/uploadAudio`, `1` a note URL on
    #: `vu.oneme.ru/uploadVideo`. Answer is `{info: {url}, videoId, token}` — the
    #: `token` is what the `MSG_SEND` attach carries.
    MEDIA_UPLOAD = 82
    #: Turns an uploaded image into a sticker and answers with its id. That id is
    #: newly *created*, `authorType: USER` — it is not a lookup.
    STICKER_CREATE = 193
    #: Where a video can be downloaded from. PyMax has `get_video_by_id`, but its
    #: model requires a `cache` field that a video note's answer does not carry,
    #: so a circle cannot be read through the typed method at all.
    VIDEO_PLAY = 83
    FILE_DOWNLOAD = 88
    #: `NOTIF_MESSAGE`. A new message in a chat — the frame the whole bridge
    #: exists for. Watched, not intercepted: PyMax delivers it fine when its
    #: typed `Message` accepts the payload, and drops the frame **in silence**
    #: when it does not (`dispatch/resolvers.py` answers a `ValidationError`
    #: with `logger.debug` and a `None`). `_rescue_untyped_message` carries
    #: exactly that second case.
    NOTIF_MESSAGE = 128
    #: Pushed by the server when a message's reactions change. Parsed from the
    #: raw frame: PyMax's model declares `messageId` a string and the server
    #: sends a number, so its typed dispatch raises before we ever see it.
    REACTION_UPDATE = 155
    #: `NOTIF_CHAT`. A whole-chat update, pushed for changes that are not a new
    #: message. Watched because a reaction from the other side arrives here and
    #: not as 155.
    NOTIF_CHAT = 135
    MSG_REACTION = 178
    MSG_CANCEL_REACTION = 179
    #: `messageIds` is a list, so one call answers for a window of messages —
    #: the only way to learn about a reaction 135 did not name.
    MSG_GET_REACTIONS = 180
    #: `audioGetSources`. Not in PyMax; the only way to fetch a voice message.
    AUDIO_PLAY = 301


def presence_frame(user_ids: list[int]) -> dict[str, object]:
    """`contactIds` is the key that filters; the others are ignored (verified)."""
    return {"contactIds": [int(user_id) for user_id in user_ids]}


#: A MAX message id carries its own timestamp: the low 16 bits are a counter and
#: the rest is the send time in milliseconds.
MESSAGE_ID_TIME_SHIFT = 16
#: Sanity window for a time read out of an id, so a differently-shaped id cannot
#: turn into a mark somewhere in 1970 or in the next century.
_PLAUSIBLE_MS = (1_500_000_000_000, 4_000_000_000_000)


def message_time_ms(message_id: int) -> int | None:
    """When a MAX message was sent, read out of its own id."""
    moment = int(message_id) >> MESSAGE_ID_TIME_SHIFT
    low, high = _PLAUSIBLE_MS
    return moment if low <= moment <= high else None


def read_frame(chat_id: int, message_id: int, *, mark: int) -> dict[str, object]:
    """`CHAT_MARK` the way the MAX app itself sends it.

    `mark` is a *watermark over message times*, not an id: the other side counts
    everything up to it as read. The app sends the time of the message actually
    read, so that is what this takes — passing `now` instead, as PyMax's typed
    method does, would acknowledge messages the owner has not opened yet.
    """
    return {
        "type": "READ_MESSAGE",
        "chatId": int(chat_id),
        "messageId": int(message_id),
        "mark": int(mark),
    }


def video_sources_frame(chat_id: int, message_id: int, video_id: int) -> dict[str, object]:
    return {"chatId": int(chat_id), "messageId": int(message_id), "videoId": int(video_id)}


def file_download_frame(chat_id: int, message_id: int, file_id: int) -> dict[str, object]:
    return {"chatId": int(chat_id), "messageId": int(message_id), "fileId": int(file_id)}


def audio_sources_frame(
    chat_id: int, message_id: int, audio_id: int, token: str | None = None
) -> dict[str, object]:
    """Voice download. `token` comes with the attachment and is accepted as a string.

    Shape taken from the web client and confirmed against maxgram; the ids are
    numbers, and a string `messageId` is what makes other MAX opcodes answer
    `proto.payload` and hang up.
    """
    frame: dict[str, object] = {
        "chatId": int(chat_id),
        "messageId": int(message_id),
        "audioId": int(audio_id),
    }
    if token:
        frame["token"] = str(token)
    return frame


def sticker_message_frame(chat_id: int, sticker_id: int, *, text: str = "") -> dict[str, object]:
    """`MSG_SEND` carrying a sticker attach (verified in integration tests).

    PyMax cannot build this: its typed `send_message` validates `attachments`
    against a union that has no sticker in it. `cid` is the client's own id for
    the message, which MAX echoes back so a send can be recognised.

    **No reply link.** The shape of `link` in a hand-built frame has never been
    verified, and a wrong field here does not degrade — the server answers
    `proto.payload` and drops the connection. So a sticker sent as a reply
    arrives as a plain sticker; losing the quote beats losing the session.
    """
    from time import time

    return {
        "chatId": int(chat_id),
        "message": {
            "text": text,
            "cid": int(time() * 1000),
            "elements": [],
            "attaches": [{"_type": "STICKER", "stickerId": int(sticker_id)}],
        },
        "notify": False,
    }


class MediaUploadType(IntEnum):
    """`type` in a `MEDIA_UPLOAD` request — which second-pipeline URL to hand out.

    `2` requests a voice upload and `1` requests a video-note upload.
    `uploaderType` is a separate field and is always `1` for this path.
    """

    VIDEO = 1  # a video note (circle)
    AUDIO = 2  # a voice message


def media_upload_frame(media_type: MediaUploadType) -> dict[str, object]:
    """`MEDIA_UPLOAD` request. `count` is 1 — one slot per note, as the app asks."""
    return {"uploaderType": 1, "type": int(media_type), "count": 1}


#: Both attach builders in the app guard their two optional fields — `m60.a()`
#: (AUDIO) and `pzh.a()` (VIDEO) write `wave` only when the array is non-empty and
#: `duration` only when it is positive, so a note the recorder produced nothing for
#: goes out with no key at all rather than an empty one. **We cannot copy that.**
#: Tried against the live server from a fresh token (compatibility test): dropping either
#: key answers `errors.process.attachment.video.not.supported` — the same wall the
#: bridge used to hit — while an all-zero 80-bar `wave` is accepted. The app gets
#: away with it because its other branch sends an `audioId`/`videoId` for media the
#: server already holds; a token-based send has to carry both fields. So they are
#: unconditional here, and `_send_native_media` refuses to reach op64 without them.
def voice_attach(*, duration_ms: int, wave: bytes, token: str) -> dict[str, object]:
    """The `AUDIO` attach the app puts in a voice `MSG_SEND` (compatibility fixture).

    `wave` is raw bytes (msgpack `bin`, one amplitude per bar); PyMax's codec
    packs `bytes` as `bin` under `use_bin_type=True`, which is what the wire
    showed. `duration` is milliseconds.
    """
    return {"_type": "AUDIO", "duration": int(duration_ms), "wave": wave, "token": str(token)}


def circle_attach(*, duration_ms: int, wave: bytes, token: str) -> dict[str, object]:
    """The `VIDEO` attach for a video note. `videoType: 1` is the circle marker —
    the one field that separates a note from an ordinary video attach."""
    return {
        "_type": "VIDEO",
        "videoType": 1,
        "duration": int(duration_ms),
        "wave": wave,
        "token": str(token),
    }


def native_media_frame(
    chat_id: int, attach: dict[str, object], *, reply_to: int | None = None
) -> dict[str, object]:
    """`MSG_SEND` carrying a native voice/circle attach.

    Same envelope every other hand-built send uses — `chatId`, a `message` with a
    negative millisecond `cid` the server echoes back, and `notify`. The captured
    frame targeted a self-chat by `userId`; a bridge send always has a `chatId`,
    which the server accepts the same way it does for stickers and contacts.
    """
    from time import time

    message: dict[str, object] = {"cid": -int(time() * 1000), "attaches": [attach]}
    if reply_to:
        message["link"] = {"type": "REPLY", "messageId": int(reply_to)}
    return {"chatId": int(chat_id), "message": message, "notify": True}


class TypingKind(StrEnum):
    """Values `MSG_TYPING` accepts.

    Verified Compatibility tests: these five are taken, the field is optional, and
    anything outside the enum (`VIDEO_MESSAGE`, for one) is answered with
    `proto.payload` and a dropped connection. Never send a guess.
    """

    TEXT = "TEXT"
    PHOTO = "PHOTO"
    VIDEO = "VIDEO"
    AUDIO = "AUDIO"
    FILE = "FILE"


def typing_frame(chat_id: int, kind: TypingKind = TypingKind.TEXT) -> dict[str, object]:
    return {"chatId": chat_id, "type": kind.value}


def add_reaction_frame(chat_id: int, message_id: int, emoji: str) -> dict[str, object]:
    """`messageId` must be a number here — see the module docstring."""
    return {
        "chatId": chat_id,
        "messageId": int(message_id),
        "reaction": {"reactionType": "EMOJI", "id": emoji},
    }


def remove_reaction_frame(chat_id: int, message_id: int) -> dict[str, object]:
    return {"chatId": chat_id, "messageId": int(message_id)}


def get_reactions_frame(chat_id: int, message_ids: list[int]) -> dict[str, object]:
    """Numbers again: PyMax types these as strings and the server refuses."""
    return {"chatId": chat_id, "messageIds": [int(message_id) for message_id in message_ids]}
