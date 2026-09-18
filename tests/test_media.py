"""WP7 — the media core: names, magic bytes, size ceilings, temp files.

Nothing here touches the network. The fetcher takes a session factory, so the
tests hand it a fake that serves bytes from memory, which is enough to pin the
three properties that matter: the limit is enforced while streaming, an HTML
page is never delivered as media, and no temp file outlives its block.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from bridge.max_client import AttachmentKind, MaxAttachment
from bridge.media import (
    ContentKind,
    HttpFetcher,
    MaxMediaSources,
    MediaPipeline,
    MediaTooLargeError,
    TempFiles,
    UnavailableMediaError,
    classify,
    detect_kind,
    display_name,
    is_acceptable,
    pick_url,
    sanitize_filename,
)
from bridge.media.http import DownloadFailedError
from bridge.media.store import PREFIX

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
OGG = b"OggS" + b"0" * 64
MP4 = b"\x00\x00\x00\x20ftypavc1" + b"0" * 64
GENERIC_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00isommp42" + b"0" * 64
M4A = b"\x00\x00\x00\x20ftypM4A " + b"0" * 64
PDF = b"%PDF-1.7" + b"0" * 64
HTML = b"<!DOCTYPE html><html><body>token expired</body></html>"


# ------------------------------------------------------------------- names


def test_a_name_can_never_become_a_path() -> None:
    """The whole reason this module exists."""
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("/etc/shadow") == "shadow"
    assert sanitize_filename("..\\..\\windows\\system32\\cmd.exe") == "cmd.exe"
    assert "/" not in sanitize_filename("a/b/c.txt")
    assert sanitize_filename("..") == "file"
    assert sanitize_filename("") == "file"
    assert sanitize_filename(None) == "file"


def test_control_characters_and_length_are_trimmed() -> None:
    assert sanitize_filename("re\x00port\n.pdf") == "re_port_.pdf"

    long_name = "x" * 400 + ".pdf"
    trimmed = sanitize_filename(long_name)
    assert len(trimmed) <= 120
    assert trimmed.endswith(".pdf")


def test_display_name_adds_a_matching_extension() -> None:
    assert display_name(hint=None, fallback_stem="voice", content_type="audio/ogg") == "voice.ogg"
    assert display_name(hint="scan", fallback_stem="file", url="https://x/y/scan.pdf") == "scan.pdf"
    # A name that already has one is left alone.
    assert display_name(hint="report.pdf", fallback_stem="file", content_type="image/png") == (
        "report.pdf"
    )


# ------------------------------------------------------------------- sniffing


@pytest.mark.parametrize(
    ("payload", "kind"),
    [
        (JPEG, ContentKind.IMAGE),
        (PNG, ContentKind.IMAGE),
        (OGG, ContentKind.AUDIO),
        (MP4, ContentKind.VIDEO),
        (M4A, ContentKind.AUDIO),
        (PDF, ContentKind.DOCUMENT),
        (HTML, ContentKind.HTML),
    ],
)
def test_magic_bytes_beat_any_claim(payload: bytes, kind: ContentKind) -> None:
    assert detect_kind(payload) is kind
    # Even when the server insists otherwise.
    assert classify("application/octet-stream", payload) is kind


def test_html_is_never_acceptable() -> None:
    """An expired MAX token answers 200 with a page, not a file."""
    assert not is_acceptable(ContentKind.AUDIO, ContentKind.HTML)
    assert not is_acceptable(ContentKind.DOCUMENT, ContentKind.HTML)
    assert not is_acceptable(None, ContentKind.HTML)


def test_a_document_may_be_anything_but_a_photo_may_not() -> None:
    assert is_acceptable(ContentKind.DOCUMENT, ContentKind.IMAGE)
    assert is_acceptable(ContentKind.DOCUMENT, ContentKind.AUDIO)
    assert not is_acceptable(ContentKind.IMAGE, ContentKind.VIDEO)


def test_a_generic_iso_bmff_brand_is_not_assumed_to_be_video() -> None:
    """MAX serves music as AAC in an mp42 container and calls it octet-stream."""
    assert detect_kind(GENERIC_MP4) is ContentKind.UNKNOWN
    assert classify("application/octet-stream", GENERIC_MP4) is ContentKind.UNKNOWN
    assert is_acceptable(ContentKind.AUDIO, classify("application/octet-stream", GENERIC_MP4))


# ------------------------------------------------------------------- URL choice


def test_the_player_page_loses_to_the_mp4() -> None:
    """op83 answers with both; picking the page delivers HTML as a video."""
    answer = {
        "EXTERNAL": "https://m.ok.ru/video/123456",
        "MP4_720": "https://maxvd1.okcdn.ru/video.mp4?sig=abc",
        "thumbnail": "https://i.okcdn.ru/preview.jpg",
    }
    assert pick_url(answer, ContentKind.VIDEO) == "https://maxvd1.okcdn.ru/video.mp4?sig=abc"


def test_a_video_note_has_exactly_one_source() -> None:
    answer = {"MP4_480": "https://v.oneme.ru/videoMsg?cid=1&contentType=0"}
    assert pick_url(answer, ContentKind.VIDEO).startswith("https://v.oneme.ru/videoMsg")


def test_audio_prefers_the_track_over_the_waveform_image() -> None:
    answer = {"preview": "https://a.oneme.ru/wave.png", "url": "https://a.oneme.ru/audio.ogg"}
    assert pick_url(answer, ContentKind.AUDIO) == "https://a.oneme.ru/audio.ogg"


# ------------------------------------------------------------------- temp files


def test_temp_file_is_private_and_removed(tmp_path: Path) -> None:
    files = TempFiles(tmp_path / "tmp")
    with files.reserve(suffix=".ogg") as path:
        path.write_bytes(OGG)
        assert path.exists()
        assert path.name.startswith(PREFIX)
        assert (path.stat().st_mode & 0o777) == 0o600
        kept = path
    assert not kept.exists()


def test_temp_file_is_removed_even_when_the_block_raises(tmp_path: Path) -> None:
    files = TempFiles(tmp_path / "tmp")
    kept: Path | None = None
    with pytest.raises(RuntimeError):
        with files.reserve() as path:
            kept = path
            raise RuntimeError("delivery failed")
    assert kept is not None and not kept.exists()


def test_sweep_takes_orphans_and_leaves_strangers(tmp_path: Path) -> None:
    directory = tmp_path / "tmp"
    directory.mkdir()
    orphan = directory / f"{PREFIX}old.bin"
    orphan.write_bytes(b"x")
    stranger = directory / "somebody-elses.bin"
    stranger.write_bytes(b"x")
    import os
    import time

    old = time.time() - 24 * 3600
    os.utime(orphan, (old, old))
    os.utime(stranger, (old, old))

    assert TempFiles(directory).sweep() == 1
    assert not orphan.exists()
    assert stranger.exists()


# ------------------------------------------------------------------- fetching


@dataclass
class FakeResponse:
    body: bytes
    headers: dict[str, str]
    status: int = 200

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    @property
    def content(self) -> Any:
        body = self.body

        class Content:
            @staticmethod
            async def iter_chunked(size: int) -> Any:
                for start in range(0, len(body), size):
                    yield body[start : start + size]

        return Content()

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


@dataclass
class FakeSession:
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    status: int = 200
    requested: list[str] = field(default_factory=list)

    def __call__(self, **kwargs: Any) -> FakeSession:
        return self

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def get(self, url: str) -> FakeResponse:
        self.requested.append(url)
        return FakeResponse(self.body, self.headers, self.status)


def fetcher_for(session: FakeSession) -> HttpFetcher:
    return HttpFetcher(session_factory=session, attempts=1)  # type: ignore[arg-type]


async def test_download_writes_the_file_and_reports_its_type(tmp_path: Path) -> None:
    session = FakeSession(body=JPEG, headers={"Content-Type": "image/jpeg"})
    destination = tmp_path / "out.jpg"

    size, content_type = await fetcher_for(session).fetch(
        "https://x/y.jpg", destination, limit_bytes=1024, expected=ContentKind.IMAGE
    )

    assert size == len(JPEG)
    assert content_type == "image/jpeg"
    assert destination.read_bytes() == JPEG


async def test_a_declared_size_over_the_limit_is_refused_before_download(tmp_path: Path) -> None:
    session = FakeSession(body=JPEG, headers={"Content-Length": "99999999"})

    with pytest.raises(MediaTooLargeError):
        await fetcher_for(session).fetch(
            "https://x/y.jpg", tmp_path / "out.jpg", limit_bytes=1024
        )
    assert session.requested == ["https://x/y.jpg"]


async def test_the_limit_is_enforced_on_the_bytes_that_actually_arrive(tmp_path: Path) -> None:
    """`Content-Length` is a claim; the counter over the stream is the truth."""
    session = FakeSession(body=b"x" * 5000, headers={"Content-Length": "10"})

    with pytest.raises(MediaTooLargeError):
        await fetcher_for(session).fetch(
            "https://x/big.bin", tmp_path / "out.bin", limit_bytes=1024
        )


async def test_an_html_page_is_not_a_voice_message(tmp_path: Path) -> None:
    session = FakeSession(body=HTML, headers={"Content-Type": "audio/ogg"})

    with pytest.raises(Exception) as caught:
        await fetcher_for(session).fetch(
            "https://a.oneme.ru/expired", tmp_path / "out.ogg",
            limit_bytes=1024, expected=ContentKind.AUDIO,
        )
    assert "html" in str(caught.value).lower()


# ------------------------------------------------------------------- pipeline


@dataclass
class FakeProtocol:
    video: Any = None
    file: Any = None
    audio: Any = None
    calls: list[str] = field(default_factory=list)

    async def video_sources(self, chat_id: int, message_id: int, video_id: int) -> Any:
        self.calls.append("video")
        return self.video

    async def file_source(self, chat_id: int, message_id: int, file_id: int) -> Any:
        self.calls.append("file")
        return self.file

    async def audio_sources(
        self, chat_id: int, message_id: int, audio_id: int, token: str | None = None
    ) -> Any:
        self.calls.append("audio")
        return self.audio


def pipeline_for(tmp_path: Path, protocol: FakeProtocol, session: FakeSession) -> MediaPipeline:
    return MediaPipeline(
        sources=MaxMediaSources(protocol),
        temp_files=TempFiles(tmp_path / "tmp"),
        fetcher=fetcher_for(session),
        max_file_size_mb=1,
    )


async def test_a_photo_needs_no_protocol_round_trip(tmp_path: Path) -> None:
    protocol = FakeProtocol()
    session = FakeSession(body=JPEG, headers={"Content-Type": "image/jpeg"})
    attachment = MaxAttachment(
        kind=AttachmentKind.PHOTO, raw={"baseUrl": "https://cdn.oneme.ru/p/1"}
    )

    async with pipeline_for(tmp_path, protocol, session).fetch_from_max(
        attachment, chat_id=7, message_id=8
    ) as local:
        assert local.path.exists()
        assert local.display_name == "photo.jpg"
        assert local.size == len(JPEG)
    assert protocol.calls == []


async def test_a_voice_message_goes_through_opcode_301(tmp_path: Path) -> None:
    protocol = FakeProtocol(audio={"url": "https://a.oneme.ru/voice.ogg"})
    session = FakeSession(body=OGG, headers={"Content-Type": "audio/ogg"})
    attachment = MaxAttachment(
        kind=AttachmentKind.VOICE,
        duration_ms=1579,
        raw={"audioId": 2538376265838, "token": "t"},
    )

    async with pipeline_for(tmp_path, protocol, session).fetch_from_max(
        attachment, chat_id=7, message_id=8
    ) as local:
        assert local.display_name == "voice.ogg"
    assert protocol.calls == ["audio"]


async def test_max_music_in_a_generic_mp42_container_is_accepted(tmp_path: Path) -> None:
    """Regression for a real MAX track rejected as `expected audio, got video`."""
    protocol = FakeProtocol(file={"url": "https://fd.oneme.ru/getfile"})
    session = FakeSession(body=GENERIC_MP4, headers={"Content-Type": "application/octet-stream"})
    attachment = MaxAttachment(
        kind=AttachmentKind.MUSIC,
        file_name="track.m4a",
        raw={"fileId": 4_935_144_295},
    )

    async with pipeline_for(tmp_path, protocol, session).fetch_from_max(
        attachment, chat_id=7, message_id=8
    ) as local:
        assert local.display_name == "track.m4a"
        assert local.size == len(GENERIC_MP4)
    assert protocol.calls == ["file"]


async def test_unavailable_media_has_a_safe_durable_reason(tmp_path: Path) -> None:
    protocol = FakeProtocol(file={"url": "https://fd.oneme.ru/signed?token=secret"})
    session = FakeSession(body=HTML, headers={"Content-Type": "text/html"})
    attachment = MaxAttachment(kind=AttachmentKind.MUSIC, raw={"fileId": 9})

    with pytest.raises(UnavailableMediaError) as caught:
        async with pipeline_for(tmp_path, protocol, session).fetch_from_max(
            attachment, chat_id=7, message_id=8
        ):
            pass

    assert caught.value.code == "wrong_content"
    assert "secret" not in str(caught.value)


async def test_a_file_keeps_its_own_name_sanitised(tmp_path: Path) -> None:
    protocol = FakeProtocol(file={"url": "https://fu.oneme.ru/d/1"})
    session = FakeSession(body=PDF, headers={"Content-Type": "application/pdf"})
    attachment = MaxAttachment(
        kind=AttachmentKind.FILE, file_name="../../etc/passwd.pdf", raw={"fileId": 9}
    )

    files = TempFiles(tmp_path / "tmp")
    pipeline = MediaPipeline(
        sources=MaxMediaSources(protocol),
        temp_files=files,
        fetcher=fetcher_for(session),
        max_file_size_mb=1,
    )
    async with pipeline.fetch_from_max(attachment, chat_id=7, message_id=8) as local:
        assert local.display_name == "passwd.pdf"
        assert local.path.parent == (tmp_path / "tmp")


async def test_an_attachment_bigger_than_the_limit_is_refused_by_metadata(tmp_path: Path) -> None:
    protocol = FakeProtocol(file={"url": "https://fu.oneme.ru/d/1"})
    session = FakeSession(body=PDF)
    attachment = MaxAttachment(
        kind=AttachmentKind.FILE, size=50 * 1024 * 1024, raw={"fileId": 9}
    )

    with pytest.raises(MediaTooLargeError):
        async with pipeline_for(tmp_path, protocol, session).fetch_from_max(
            attachment, chat_id=7, message_id=8
        ):
            pass
    assert protocol.calls == [], "no point asking where a file we cannot take lives"


async def test_an_unresolvable_attachment_says_so(tmp_path: Path) -> None:
    protocol = FakeProtocol(video={})
    session = FakeSession(body=MP4)
    attachment = MaxAttachment(kind=AttachmentKind.VIDEO, raw={})

    with pytest.raises(UnavailableMediaError):
        async with pipeline_for(tmp_path, protocol, session).fetch_from_max(
            attachment, chat_id=7, message_id=8
        ):
            pass


async def test_the_temp_file_is_gone_after_delivery(tmp_path: Path) -> None:
    protocol = FakeProtocol(file={"url": "https://fu.oneme.ru/d/1"})
    session = FakeSession(body=PDF, headers={"Content-Type": "application/pdf"})
    attachment = MaxAttachment(kind=AttachmentKind.FILE, raw={"fileId": 9})

    kept: Path | None = None
    async with pipeline_for(tmp_path, protocol, session).fetch_from_max(
        attachment, chat_id=7, message_id=8
    ) as local:
        kept = local.path
    assert kept is not None and not kept.exists()


# ------------------------------------- two CDNs that want opposite headers


async def test_a_refused_user_agent_is_retried_bare(tmp_path: Path) -> None:
    """Measured on compatibility fixtures, both directions.

    `v.oneme.ru` (voice, circles) answers 403 without a browser user agent.
    `maxvd*.okcdn.ru`, where an ordinary video lives, answers 400 *with* one —
    its links carry `srcAg=UNKNOWN_ANDROID` and it holds the caller to that.
    Nothing in the URL says which is which, so a client refusal is retried
    without headers before the attachment is given up on.
    """
    seen: list[dict[str, str]] = []

    class Session:
        def __init__(self, **kwargs: Any) -> None:
            self.headers = dict(kwargs.get("headers") or {})

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        def get(self, url: str) -> Any:
            seen.append(self.headers)
            refuse = "User-Agent" in self.headers
            return _Response(refuse)

    fetcher = HttpFetcher(Session, attempts=1)  # type: ignore[arg-type]
    size, content_type = await fetcher.fetch(
        "https://maxvd558.okcdn.ru/?srcAg=UNKNOWN_ANDROID",
        tmp_path / "video.mp4",
        limit_bytes=20 * 1024 * 1024,
    )

    assert size > 0
    assert content_type == "video/mp4"
    assert len(seen) == 2, "it tried dressed, then bare"
    assert "User-Agent" in seen[0]
    assert "User-Agent" not in seen[1]


async def test_a_missing_file_is_not_retried_bare(tmp_path: Path) -> None:
    """A 404 is about the URL and fails identically without headers."""
    attempts: list[dict[str, str]] = []

    class Session:
        def __init__(self, **kwargs: Any) -> None:
            self.headers = dict(kwargs.get("headers") or {})

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        def get(self, url: str) -> Any:
            attempts.append(self.headers)
            return _Response(True, status=404)

    fetcher = HttpFetcher(Session, attempts=1)  # type: ignore[arg-type]
    with pytest.raises(DownloadFailedError):
        await fetcher.fetch(
            "https://example.invalid/gone.mp4",
            tmp_path / "gone.mp4",
            limit_bytes=1024,
        )

    assert len(attempts) == 1, "no wasted round trip"


class _Response:
    """Refuses when asked to, serves an mp4 otherwise."""

    def __init__(self, refuse: bool, *, status: int = 400) -> None:
        self._refuse = refuse
        self._status = status
        self.headers = {"Content-Type": "video/mp4"}
        self.content = _Body()

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def raise_for_status(self) -> None:
        if not self._refuse:
            return
        import aiohttp
        from yarl import URL

        info = aiohttp.RequestInfo(
            url=URL("https://example.invalid/"),
            method="GET",
            headers=aiohttp.typedefs.CIMultiDict(),  # type: ignore[attr-defined]
            real_url=URL("https://example.invalid/"),
        )
        raise aiohttp.ClientResponseError(
            request_info=info, history=(), status=self._status
        )


class _Body:
    async def iter_chunked(self, size: int) -> Any:
        # `\x00\x00\x00\x18ftypmp4` — enough for the sniffer to call it video.
        yield b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
