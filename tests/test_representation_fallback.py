"""A second creating call requires the server to have answered.

The rule this file exists to hold shut, in the exact shape it was broken: a
timeout that arrived *after* Telegram had accepted the content was read as a
refusal, and the same bytes went out again under a different method. Six
messages for three attachments, inside one attempt, with no retry involved.

Every test here therefore counts **calls**, not return values.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from bridge.max_client import AttachmentKind
from bridge.media.delivery import OutgoingMedia
from bridge.routing.delivery import UnconfirmedDeliveryError
from bridge.routing.media_adapter import RegistryMediaSender, check_album_receipt
from bridge.telegram.errors import (
    TelegramOutcome,
    TelegramTransportUnavailableError,
    classify_telegram,
    refused_group,
    refused_representation,
)


def bad_request(description: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=None, message=description)  # type: ignore[arg-type]


#: Every way Telegram can fail to answer. Each one may mean the message exists.
SILENCE: list[Any] = [
    TelegramNetworkError(method=None, message="Request timeout error"),  # type: ignore[arg-type]
    TelegramNetworkError(method=None, message="ClientOSError: reset"),  # type: ignore[arg-type]
    TelegramServerError(method=None, message="Bad Gateway"),  # type: ignore[arg-type]
    TimeoutError("no answer"),
    ConnectionResetError("reset"),
    OSError("broken pipe"),
    asyncio.CancelledError(),
]

SILENCE_IDS = ["timeout", "reset", "5xx", "TimeoutError", "ConnectionReset", "OSError", "cancelled"]


#: Stands in for the sticker object Telegram puts on a real sticker message.
A_STICKER = object()


class Sent:
    def __init__(self, message_id: int | None = 1, *, sticker: Any = A_STICKER) -> None:
        self.message_id = message_id
        self.media_group_id = "g1"
        self.sticker = sticker


class RecordingBot:
    """A Telegram that records every method it was asked for.

    `fail` maps a method name to what it raises the *first* time it is called,
    which is how "the second call must not happen" becomes an assertion on
    `calls` rather than on an exception type.
    """

    def __init__(self, fail: dict[str, BaseException] | None = None, **answers: Any) -> None:
        self.calls: list[str] = []
        self._fail = dict(fail or {})
        self._answers = answers
        self._next = 100

    def __getattr__(self, name: str) -> Any:
        if not name.startswith("send_"):
            raise AttributeError(name)

        async def call(**_: Any) -> Any:
            self.calls.append(name)
            error = self._fail.pop(name, None)
            if error is not None:
                raise error
            if name in self._answers:
                return self._answers[name]
            self._next += 1
            return Sent(self._next)

        return call


class Registry:
    def __init__(self, bot: Any) -> None:
        self._bot = bot

    def by_bot_id(self, bot_id: int) -> Any:
        if self._bot is None:
            return None
        return type("Live", (), {"bot": self._bot})()


@pytest.fixture
def media(tmp_path: Path) -> OutgoingMedia:
    path = tmp_path / "thing.jpg"
    path.write_bytes(b"x")
    return OutgoingMedia(kind=AttachmentKind.PHOTO, path=path, file_name="thing.jpg")


def album(tmp_path: Path, count: int = 3) -> list[OutgoingMedia]:
    items = []
    for index in range(count):
        path = tmp_path / f"part{index}.jpg"
        path.write_bytes(b"x")
        items.append(
            OutgoingMedia(
                kind=AttachmentKind.PHOTO,
                path=path,
                file_name=path.name,
                caption="hi" if index == 0 else None,
            )
        )
    return items


# ------------------------------------------------------------------- classifier


def test_silence_is_never_an_answer() -> None:
    for error in SILENCE:
        verdict = classify_telegram(error)
        assert verdict.outcome is TelegramOutcome.NO_ANSWER, error
        assert not verdict.answered
        assert not refused_representation(error)
        assert not refused_group(error)


def test_a_rate_limit_carries_its_own_wait() -> None:
    verdict = classify_telegram(TelegramRetryAfter(method=None, message="x", retry_after=30))  # type: ignore[arg-type]
    assert verdict.outcome is TelegramOutcome.RATE_LIMITED
    assert verdict.retry_after_ms == 30_000


def test_a_known_representation_refusal_is_named() -> None:
    for description in (
        "Bad Request: PHOTO_INVALID_DIMENSIONS",
        "Bad Request: IMAGE_PROCESS_FAILED",
        "Bad Request: STICKER_TGS_NOTGZIP",
        "Bad Request: VIDEO_FILE_INVALID",
        "Bad Request: wrong file identifier/HTTP URL specified",
    ):
        assert refused_representation(bad_request(description)), description


def test_an_unknown_bad_request_is_permanent_and_offers_no_second_form() -> None:
    for description in (
        "Bad Request: chat not found",
        "Bad Request: can't parse entities: unexpected end tag",
        "Bad Request: have no rights to send a message",
        "Bad Request: replied message not found",
        "Bad Request: something nobody has measured",
    ):
        verdict = classify_telegram(bad_request(description))
        assert verdict.outcome is TelegramOutcome.REFUSED, description
        assert not refused_representation(bad_request(description))
        assert not refused_group(bad_request(description))


def test_permission_errors_are_permanent() -> None:
    error = TelegramForbiddenError(method=None, message="bot was blocked by the user")  # type: ignore[arg-type]
    assert classify_telegram(error).outcome is TelegramOutcome.REFUSED


def test_a_missing_transport_is_proven_not_sent() -> None:
    verdict = classify_telegram(TelegramTransportUnavailableError("bot 1 is gone"))
    assert verdict.outcome is TelegramOutcome.NOT_SENT


def test_a_mutation_target_that_is_already_there_is_a_no_op() -> None:
    for description in (
        "Bad Request: message is not modified",
        "Bad Request: message to delete not found",
        "Bad Request: message to edit not found",
    ):
        assert classify_telegram(bad_request(description)).outcome is TelegramOutcome.NO_OP


# ----------------------------------------------------- silence never falls back

SINGLE_METHODS = [
    ("send_photo", "send_photo"),
    ("send_video", "send_video"),
    ("send_video_note", "send_video_note"),
    ("send_voice", "send_voice"),
    ("send_audio", "send_audio"),
    ("send_document", "send_document"),
    ("send_sticker", "send_sticker"),
]


@pytest.mark.parametrize(("method", "api"), SINGLE_METHODS)
@pytest.mark.parametrize("error", SILENCE, ids=SILENCE_IDS)
@pytest.mark.asyncio
async def test_silence_never_calls_a_second_method(
    method: str, api: str, error: BaseException, media: OutgoingMedia
) -> None:
    bot = RecordingBot(fail={api: error})
    sender = RegistryMediaSender(Registry(bot))  # type: ignore[arg-type]
    with pytest.raises((Exception, asyncio.CancelledError)):
        await getattr(sender, method)(1, 2, media, reply_to=None)
    assert bot.calls == [api], f"{method} reached a second creating method on {error!r}"


@pytest.mark.parametrize(("method", "api"), SINGLE_METHODS)
@pytest.mark.asyncio
async def test_an_unknown_bad_request_never_falls_back(
    method: str, api: str, media: OutgoingMedia
) -> None:
    bot = RecordingBot(fail={api: bad_request("Bad Request: chat not found")})
    sender = RegistryMediaSender(Registry(bot))  # type: ignore[arg-type]
    with pytest.raises(TelegramBadRequest):
        await getattr(sender, method)(1, 2, media, reply_to=None)
    assert bot.calls == [api]


@pytest.mark.parametrize(("method", "api"), SINGLE_METHODS)
@pytest.mark.asyncio
async def test_an_answer_without_an_id_is_unconfirmed_and_final(
    method: str, api: str, media: OutgoingMedia
) -> None:
    bot = RecordingBot(**{api: Sent(None)})
    sender = RegistryMediaSender(Registry(bot))  # type: ignore[arg-type]
    with pytest.raises(UnconfirmedDeliveryError):
        await getattr(sender, method)(1, 2, media, reply_to=None)
    assert bot.calls == [api], "an unnamed message must not be sent a second way"


@pytest.mark.asyncio
async def test_a_transport_that_is_gone_is_not_a_question(media: OutgoingMedia) -> None:
    sender = RegistryMediaSender(Registry(None))  # type: ignore[arg-type]
    with pytest.raises(TelegramTransportUnavailableError):
        await sender.send_photo(1, 2, media, reply_to=None)


# --------------------------------------------- one permitted fallback, and only one


@pytest.mark.parametrize(
    ("method", "api", "fallback"),
    [
        ("send_photo", "send_photo", "send_document"),
        ("send_video", "send_video", "send_document"),
        ("send_voice", "send_voice", "send_document"),
        ("send_audio", "send_audio", "send_document"),
        ("send_video_note", "send_video_note", "send_video"),
    ],
)
@pytest.mark.asyncio
async def test_a_representation_refusal_degrades_exactly_once(
    method: str, api: str, fallback: str, media: OutgoingMedia
) -> None:
    bot = RecordingBot(fail={api: bad_request("Bad Request: PHOTO_INVALID_DIMENSIONS")})
    sender = RegistryMediaSender(Registry(bot))  # type: ignore[arg-type]
    assert await getattr(sender, method)(1, 2, media, reply_to=None) > 0
    assert bot.calls == [api, fallback]


@pytest.mark.asyncio
async def test_a_sticker_degrades_through_a_photo_and_stops(media: OutgoingMedia) -> None:
    bot = RecordingBot(
        fail={
            "send_sticker": bad_request("Bad Request: STICKER_PNG_NOPNG"),
            "send_photo": bad_request("Bad Request: PHOTO_INVALID_DIMENSIONS"),
        }
    )
    sender = RegistryMediaSender(Registry(bot))  # type: ignore[arg-type]
    assert await sender.send_sticker(1, 2, media, reply_to=None) > 0
    assert bot.calls == ["send_sticker", "send_photo", "send_document"]


@pytest.mark.asyncio
async def test_a_failing_fallback_never_reaches_a_third_method(media: OutgoingMedia) -> None:
    bot = RecordingBot(
        fail={
            "send_photo": bad_request("Bad Request: PHOTO_INVALID_DIMENSIONS"),
            "send_document": bad_request("Bad Request: PHOTO_INVALID_DIMENSIONS"),
        }
    )
    sender = RegistryMediaSender(Registry(bot))  # type: ignore[arg-type]
    with pytest.raises(TelegramBadRequest):
        await sender.send_photo(1, 2, media, reply_to=None)
    assert bot.calls == ["send_photo", "send_document"]


@pytest.mark.asyncio
async def test_a_demoted_sticker_keeps_the_message_telegram_made(media: OutgoingMedia) -> None:
    """`ok: true` with no sticker on it: the message exists, as a document.

    This used to send the picture again as a photo — two copies of one image.
    """
    bot = RecordingBot(send_sticker=Sent(777, sticker=None))
    sender = RegistryMediaSender(Registry(bot))  # type: ignore[arg-type]
    assert await sender.send_sticker(1, 2, media, reply_to=None) == 777
    assert bot.calls == ["send_sticker"]


# ------------------------------------------------------------------- the album


@pytest.mark.parametrize("error", SILENCE, ids=SILENCE_IDS)
@pytest.mark.asyncio
async def test_album_silence_never_sends_the_parts_individually(
    error: BaseException, tmp_path: Path
) -> None:
    bot = RecordingBot(fail={"send_media_group": error})
    sender = RegistryMediaSender(Registry(bot))  # type: ignore[arg-type]
    with pytest.raises((Exception, asyncio.CancelledError)):
        await sender.send_album(1, 2, album(tmp_path), reply_to=None)
    assert bot.calls == ["send_media_group"], "the album was sent a second time, piece by piece"


@pytest.mark.asyncio
async def test_an_unknown_album_refusal_is_permanent(tmp_path: Path) -> None:
    bot = RecordingBot(fail={"send_media_group": bad_request("Bad Request: chat not found")})
    sender = RegistryMediaSender(Registry(bot))  # type: ignore[arg-type]
    with pytest.raises(TelegramBadRequest):
        await sender.send_album(1, 2, album(tmp_path), reply_to=None)
    assert bot.calls == ["send_media_group"]


@pytest.mark.asyncio
async def test_a_confirmed_group_refusal_decomposes_by_policy(tmp_path: Path) -> None:
    """The one documented case: Telegram answered that the *group* is wrong.

    An answer proves nothing was created, so the parts go out as a first attempt
    rather than a second one.
    """
    bot = RecordingBot(fail={"send_media_group": bad_request("Bad Request: MEDIA_GROUP_INVALID")})
    sender = RegistryMediaSender(Registry(bot))  # type: ignore[arg-type]
    receipt = await sender.send_album(1, 2, album(tmp_path), reply_to=None)
    assert bot.calls == ["send_media_group", "send_photo", "send_photo", "send_photo"]
    assert len(receipt.message_ids) == 3
    assert receipt.media_group_id is None


@pytest.mark.asyncio
async def test_a_full_album_is_one_request(tmp_path: Path) -> None:
    bot = RecordingBot(send_media_group=[Sent(501), Sent(502), Sent(503)])
    sender = RegistryMediaSender(Registry(bot))  # type: ignore[arg-type]
    receipt = await sender.send_album(1, 2, album(tmp_path), reply_to=None)
    assert bot.calls == ["send_media_group"]
    assert receipt.message_ids == (501, 502, 503)
    assert receipt.media_group_id == "g1"


@pytest.mark.asyncio
async def test_a_short_album_answer_is_unconfirmed(tmp_path: Path) -> None:
    bot = RecordingBot(send_media_group=[Sent(501), Sent(502)])
    sender = RegistryMediaSender(Registry(bot))  # type: ignore[arg-type]
    with pytest.raises(UnconfirmedDeliveryError):
        await sender.send_album(1, 2, album(tmp_path), reply_to=None)


@pytest.mark.asyncio
async def test_a_reordered_album_answer_is_unconfirmed(tmp_path: Path) -> None:
    bot = RecordingBot(send_media_group=[Sent(503), Sent(501), Sent(502)])
    sender = RegistryMediaSender(Registry(bot))  # type: ignore[arg-type]
    with pytest.raises(UnconfirmedDeliveryError):
        await sender.send_album(1, 2, album(tmp_path), reply_to=None)


# ------------------------------------------------------------- the receipt contract


def test_the_receipt_contract() -> None:
    check_album_receipt((1, 2, 3), expected=3)
    for bad, why in (
        ((1, 2), "short"),
        ((1, 2, 3, 4), "long"),
        ((1, 1, 2), "duplicate"),
        ((3, 1, 2), "reordered"),
        ((3, 2, 1), "descending"),
        ((0, 1, 2), "impossible id"),
    ):
        with pytest.raises(UnconfirmedDeliveryError):
            check_album_receipt(bad, expected=3)
        assert why


def test_the_receipt_contract_never_sorts() -> None:
    """A repair here would hide a transport that stopped behaving as measured."""
    with pytest.raises(UnconfirmedDeliveryError):
        check_album_receipt((903, 901, 902), expected=3)


# ------------------------------------------------------------- notices and cards


@pytest.mark.parametrize("error", SILENCE, ids=SILENCE_IDS)
@pytest.mark.asyncio
async def test_a_notice_is_a_creating_call_too(error: BaseException) -> None:
    bot = RecordingBot(fail={"send_message": error})
    sender = RegistryMediaSender(Registry(bot))  # type: ignore[arg-type]
    with pytest.raises((Exception, asyncio.CancelledError)):
        await sender.send_text(1, 2, "[фото: не удалось скачать]")
    assert bot.calls == ["send_message"]


@pytest.mark.asyncio
async def test_an_avatar_url_telegram_cannot_fetch_falls_back_to_a_card() -> None:
    bot = RecordingBot(
        fail={"send_photo": bad_request("Bad Request: failed to get HTTP URL content")}
    )
    sender = RegistryMediaSender(Registry(bot))  # type: ignore[arg-type]
    assert await sender.send_photo_url(1, 2, "https://x/y.jpg", caption="card") is None
    assert bot.calls == ["send_photo"]


@pytest.mark.parametrize("error", SILENCE, ids=SILENCE_IDS)
@pytest.mark.asyncio
async def test_an_avatar_timeout_is_not_a_missing_avatar(error: BaseException) -> None:
    bot = RecordingBot(fail={"send_photo": error})
    sender = RegistryMediaSender(Registry(bot))  # type: ignore[arg-type]
    with pytest.raises((Exception, asyncio.CancelledError)):
        await sender.send_photo_url(1, 2, "https://x/y.jpg", caption="card")
    assert bot.calls == ["send_photo"]
