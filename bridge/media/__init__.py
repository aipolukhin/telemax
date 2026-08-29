"""Media pipeline (the media core): MAX and Telegram attachments to local files and back."""

from .http import DownloadFailedError, HttpFetcher, WrongContentError
from .names import display_name, extension_for, sanitize_filename
from .pipeline import MediaPipeline, UnavailableMediaError
from .sniff import ContentKind, classify, detect_kind, is_acceptable
from .sources import MaxMediaSources, photo_url, pick_url
from .store import LocalFile, MediaTooLargeError, TempFiles

__all__ = [
    "ContentKind",
    "DownloadFailedError",
    "HttpFetcher",
    "LocalFile",
    "MaxMediaSources",
    "MediaPipeline",
    "MediaTooLargeError",
    "TempFiles",
    "UnavailableMediaError",
    "WrongContentError",
    "classify",
    "detect_kind",
    "display_name",
    "extension_for",
    "is_acceptable",
    "photo_url",
    "pick_url",
    "sanitize_filename",
]
