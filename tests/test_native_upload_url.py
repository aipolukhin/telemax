"""AU-2 F5 — the upload URL comes from the server, so it is input.

op82 answers with a URL and the bridge POSTs a private voice message to it. The
check that used to guard that was `"oneme.ru" not in host`, which is a substring
test: `https://au.oneme.ru.somewhere-else/up` passes it, and the recording goes
to whoever owns that domain. Nothing about that needs a hostile MAX — a wrong
answer from a compromised or simply confused server is enough.

So the URL is now pinned to what the stand actually captured: scheme, exact
host, default port, endpoint path, and no userinfo. Redirects are refused rather
than followed, because a 307 from the real host would re-send the body past the
check that was just made.

Every refusal has to reach the owner as a delivered message: a voice that
arrives as a file is a degradation, a voice that does not arrive is a bug.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_native_media_faults import Rig  # the real send path, faked socket

from bridge.max_client.native_state import UploaderUrlError, check_upload_url

GOOD_VOICE = "https://au.oneme.ru/uploadAudio?signatureToken=x&clientType=5"
GOOD_CIRCLE = "https://vu.oneme.ru/uploadVideo?signatureToken=x"


# --------------------------------------------------------------- the allowlist


def test_the_captured_hosts_pass() -> None:
    assert check_upload_url("voice", GOOD_VOICE) == "au.oneme.ru"
    assert check_upload_url("circle", GOOD_CIRCLE) == "vu.oneme.ru"


def test_an_explicit_default_port_passes() -> None:
    assert check_upload_url("voice", "https://au.oneme.ru:443/uploadAudio?x") == "au.oneme.ru"


@pytest.mark.parametrize(
    ("label", "url"),
    [
        # The one this exists for: reads as the right host, is not.
        ("lookalike suffix", "https://au.oneme.ru.somewhere-else/uploadAudio?x"),
        ("lookalike prefix", "https://evil-au.oneme.ru.co/uploadAudio?x"),
        # A subdomain of the right zone is still not the host we measured.
        ("unexpected subdomain", "https://cdn.au.oneme.ru/uploadAudio?x"),
        ("the other kind's host", "https://vu.oneme.ru/uploadAudio?x"),
        ("the OK CDN", "https://omu.okcdn.ru/upload.do?x"),
        # Plaintext would put a private recording on the wire in clear.
        ("http", "http://au.oneme.ru/uploadAudio?x"),
        ("no scheme", "au.oneme.ru/uploadAudio?x"),
        # `https://au.oneme.ru@elsewhere/` reads as the right host to a human.
        ("userinfo", "https://au.oneme.ru@elsewhere.example/uploadAudio?x"),
        ("userinfo with password", "https://au.oneme.ru:pw@elsewhere.example/uploadAudio?x"),
        ("odd port", "https://au.oneme.ru:8443/uploadAudio?x"),
        # The same host serves more than this endpoint.
        ("wrong path", "https://au.oneme.ru/uploadVideo?x"),
        ("empty path", "https://au.oneme.ru?x"),
        ("traversal in the path", "https://au.oneme.ru/uploadAudio/../else?x"),
    ],
)
def test_everything_else_is_refused(label: str, url: str) -> None:
    with pytest.raises(UploaderUrlError):
        check_upload_url("voice", url)


def test_a_kind_with_no_native_endpoint_is_refused() -> None:
    with pytest.raises(UploaderUrlError):
        check_upload_url("photo", GOOD_VOICE)


def test_the_refusal_never_carries_the_query() -> None:
    """The `signatureToken` lives in the query, and this text is logged and stored."""
    with pytest.raises(UploaderUrlError) as caught:
        check_upload_url("voice", "https://au.oneme.ru.evil.example/uploadAudio?signatureToken=s3cr")
    assert "s3cr" not in str(caught.value)
    assert "signatureToken" not in str(caught.value)


# ------------------------------------------------------------- the send path


@pytest.mark.parametrize(
    "url",
    [
        "https://au.oneme.ru.somewhere-else/uploadAudio?x",
        "http://au.oneme.ru/uploadAudio?x",
        "https://au.oneme.ru@elsewhere.example/uploadAudio?x",
        "https://au.oneme.ru:8443/uploadAudio?x",
        "https://au.oneme.ru/somethingElse?x",
        "https://omu.okcdn.ru/upload.do?x",
    ],
)
def test_a_refused_url_uploads_nothing_and_still_delivers(tmp_path: Path, url: str) -> None:
    """Refused before `read_bytes`, refused before the POST — and the owner's
    voice still arrives, as a file."""
    rig = Rig(tmp_path, slot={"info": {"url": url}, "token": "T"})

    assert rig.send() == 9  # the plain attachment went out
    assert rig.posts == []  # nothing was uploaded anywhere
    assert rig.op64_calls == 0
    assert len(rig.fallbacks) == 1  # exactly one, never two
    assert rig.state.status("voice").uploader_drift == 1


def test_a_refused_url_is_read_before_the_file_is(tmp_path: Path, monkeypatch: Any) -> None:
    """The order matters: a 30 MB read for a URL we are about to reject is work,
    and `read_bytes` is the step that puts the recording in memory."""
    reads: list[str] = []
    original = Path.read_bytes

    def watched(self: Path) -> bytes:
        reads.append(self.name)
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", watched)
    rig = Rig(tmp_path, slot={"info": {"url": "https://au.oneme.ru.evil.example/uploadAudio"}})

    assert rig.send() == 9
    assert reads == []


def test_a_missing_slot_names_the_keys_not_the_token(tmp_path: Path) -> None:
    """op82 answering with a URL but no token used to put the whole answer —
    signed URL included — into an exception that is logged and stored."""
    import asyncio

    from bridge.max_client.client import MaxMediaError

    rig = Rig(tmp_path, slot={"info": {"url": GOOD_VOICE}})  # a url, but no token
    source = tmp_path / "voice.ogg"
    source.write_bytes(b"x")

    with pytest.raises(MaxMediaError) as caught:
        asyncio.run(rig.client._send_native_media(1, "voice", source, "voice.ogg"))

    text = str(caught.value)
    assert "signatureToken" not in text
    assert "au.oneme.ru" not in text


# ---------------------------------------------------------------- redirects


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_an_answer_not_a_hop(tmp_path: Path, status: int) -> None:
    """aiohttp follows redirects by default, and following one would re-send the
    body to a host the allowlist never saw. Refused, and the message degrades."""
    from bridge.max_client.client import MaxMediaError

    rig = Rig(
        tmp_path,
        post_error=MaxMediaError(f"media upload answered {status}: the uploader redirected"),
    )

    assert rig.send() == 9
    assert rig.op64_calls == 0
    assert len(rig.fallbacks) == 1


def test_the_post_does_not_follow_redirects() -> None:
    """The flag itself, read off the production call: the test above models the
    outcome, this one pins the reason it happens."""
    import inspect

    from bridge.max_client.client import MaxClient

    source = inspect.getsource(MaxClient._post_media)
    assert "allow_redirects=False" in source
    # And nothing turns TLS verification off on the way past.
    assert "ssl=False" not in source
    assert "verify_ssl" not in source
