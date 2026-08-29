"""Downloading a URL to disk, with a hard ceiling and honest failures.

MAX hands out CDN links; Telegram hands out `getFile` links. Both are plain
HTTPS, and both need the same three guarantees:

* **the size limit is enforced while streaming**, not after. `Content-Length` is
  a hint an attacker or a misconfigured CDN can lie about, so the counter that
  matters is the one over the bytes actually written;
* **the first bytes are inspected**, because a 200 with an HTML body is how an
  expired MAX token looks (see `sniff`);
* **a failure leaves nothing behind** — the caller's `TempFiles.reserve` block
  removes the file, and this module never returns a path it did not fill.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import aiohttp

from .sniff import SNIFF_SIZE, ContentKind, classify, is_acceptable
from .store import MediaTooLargeError

logger = logging.getLogger(__name__)

CHUNK_SIZE = 64 * 1024
REQUEST_TIMEOUT_SECONDS = 120
ATTEMPTS = 3

#: MAX CDNs answer 403 to a request without a browser-ish user agent.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Mobile Safari/537.36"
    ),
    "Accept": "*/*",
}

#: And the video CDN answers 400 to a request *with* one. Measured, both ways:
#: `v.oneme.ru` (voice, circles) needs the agent above; `maxvd*.okcdn.ru`, which
#: is where an ordinary video lives, refuses it — its links carry
#: `srcAg=UNKNOWN_ANDROID` and it holds the caller to that. Sending nothing is
#: what both accept, so a client refusal is retried bare before giving up.
BARE_HEADERS = {"Accept": "*/*"}

#: Statuses where another attempt cannot help: the URL or the token is wrong.
_FINAL_STATUSES = frozenset({400, 401, 403, 404, 410, 451})


class DownloadFailedError(Exception):
    """The URL did not yield a usable file."""


class WrongContentError(DownloadFailedError):
    """The bytes are not what the attachment claimed — usually an HTML page."""

    def __init__(self, expected: ContentKind | None, detected: ContentKind) -> None:
        self.expected = expected
        self.detected = detected
        super().__init__(f"expected {expected or 'media'}, got {detected}")


class HttpFetcher:
    """Fetches URLs into files the caller already reserved."""

    def __init__(
        self,
        session_factory: type[aiohttp.ClientSession] | None = None,
        *,
        attempts: int = ATTEMPTS,
    ) -> None:
        self._session_factory = session_factory or aiohttp.ClientSession
        self._attempts = attempts

    async def fetch(
        self,
        url: str,
        destination: Path,
        *,
        limit_bytes: int,
        expected: ContentKind | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, str | None]:
        """Download `url` into `destination`. Returns (size, content type).

        Raises `MediaTooLargeError` when the ceiling is crossed, `WrongContentError` when
        the body is not the kind of thing that was asked for, and
        `DownloadFailedError` for everything else.
        """
        last_error: Exception | None = None

        # Two header sets, tried in order, because the two CDNs MAX serves media
        # from want opposite things and the URL does not say which is which.
        for outfit in (DEFAULT_HEADERS, BARE_HEADERS):
            for attempt in range(1, self._attempts + 1):
                try:
                    return await self._attempt(
                        url,
                        destination,
                        limit_bytes=limit_bytes,
                        expected=expected,
                        headers={**outfit, **(headers or {})},
                    )
                except (MediaTooLargeError, WrongContentError):
                    # Neither gets better on a retry: the file is too big, or
                    # the server is serving something else entirely.
                    raise
                except Exception as error:  # noqa: BLE001 - classified by `_retryable`
                    last_error = error
                    if not _retryable(error) or attempt == self._attempts:
                        break
                    delay = min(2 ** (attempt - 1), 8)
                    logger.debug("download attempt %s failed, retrying in %ss", attempt, delay)
                    await asyncio.sleep(delay)

            if not _worth_undressing(last_error):
                break
            logger.debug("the server refused our user agent; retrying without one")

        raise DownloadFailedError(str(last_error) or "download failed") from last_error

    async def _attempt(
        self,
        url: str,
        destination: Path,
        *,
        limit_bytes: int,
        expected: ContentKind | None,
        headers: dict[str, str],
    ) -> tuple[int, str | None]:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        written = 0
        head = b""

        async with self._session_factory(timeout=timeout, headers=headers) as session:
            async with session.get(url) as response:
                response.raise_for_status()
                content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()

                declared = response.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > limit_bytes:
                    # Cheap rejection before a single byte is written; the loop
                    # below still counts, because this header is only a claim.
                    raise MediaTooLargeError(limit_bytes, int(declared))

                with destination.open("wb") as sink:
                    async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > limit_bytes:
                            raise MediaTooLargeError(limit_bytes)
                        if len(head) < SNIFF_SIZE:
                            head += chunk[: SNIFF_SIZE - len(head)]
                        sink.write(chunk)

        if written == 0:
            raise DownloadFailedError("the server returned an empty body")

        detected = classify(content_type or None, head)
        if not is_acceptable(expected, detected):
            raise WrongContentError(expected, detected)

        return written, content_type or None


def _worth_undressing(error: Exception | None) -> bool:
    """Whether a refusal looks like one the user agent itself caused.

    Only client statuses, and only the ones a CDN uses to say "not for you".
    A 404 is about the URL and would fail identically bare; a timeout is about
    the network. Retrying either without headers is a wasted round trip.
    """
    return isinstance(error, aiohttp.ClientResponseError) and error.status in {400, 403}


def _retryable(error: Exception) -> bool:
    if isinstance(error, aiohttp.ClientResponseError):
        return error.status not in _FINAL_STATUSES
    return isinstance(error, (aiohttp.ClientError, TimeoutError, OSError))
