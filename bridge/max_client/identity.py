"""Stable per-install device identity for the MAX session.

The identity is generated once, stored next to the session database and reused
across restarts. Each installation gets an independent random device id; no
value is derived from the owner's phone number or account identifiers.
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from pymax.api.session.enums import DeviceType
from pymax.api.session.payloads import MobileUserAgentPayload

from .state_files import ensure_backup, read_with_backup, write_atomic

logger = logging.getLogger(__name__)

#: The identity file, next to the session database.
IDENTITY_FILE = "identity.json"

#: Common Android profiles, each row `(deviceName, osVersion, screen, arch)`.
DEVICES: tuple[tuple[str, str, str, str], ...] = (
    ("Google Pixel 6", "Android 14", "411dpi 411dpi 1080x2400", "arm64-v8a"),
    ("Google Pixel 7", "Android 14", "416dpi 416dpi 1080x2400", "arm64-v8a"),
    ("Google Pixel 8", "Android 15", "428dpi 428dpi 1080x2400", "arm64-v8a"),
    ("samsung SM-A525F", "Android 13", "405dpi 405dpi 1080x2400", "arm64-v8a"),
    ("samsung SM-A546E", "Android 14", "405dpi 405dpi 1080x2340", "arm64-v8a"),
    ("samsung SM-S901B", "Android 14", "425dpi 425dpi 1080x2340", "arm64-v8a"),
    ("samsung SM-S911B", "Android 15", "425dpi 425dpi 1080x2340", "arm64-v8a"),
    ("Xiaomi 2201117TG", "Android 13", "395dpi 395dpi 1080x2400", "arm64-v8a"),
    ("Xiaomi 22101316G", "Android 14", "395dpi 395dpi 1080x2400", "arm64-v8a"),
    ("Xiaomi 23049PCD8G", "Android 14", "446dpi 446dpi 1220x2712", "arm64-v8a"),
    ("realme RMX3085", "Android 13", "409dpi 409dpi 1080x2400", "arm64-v8a"),
    ("OnePlus CPH2449", "Android 14", "451dpi 451dpi 1240x2772", "arm64-v8a"),
)

#: Used when the owner never told the bridge where they are.
FALLBACK_TIMEZONE = "Europe/Moscow"

#: The identity the bridge shipped as a module constant before this file existed.
#: An account created under it keeps it — see `load_or_create`.
LEGACY_IDENTITY = {
    "device_name": "Google Pixel 6",
    "os_version": "Android 14",
    "screen": "411dpi 411dpi 1080x2400",
    "arch": "arm64-v8a",
    "locale": "ru",
    "timezone": "Europe/Moscow",
}


@dataclass(frozen=True)
class MaxIdentity:
    """One phone, as this install will report it for as long as it exists."""

    device_name: str
    os_version: str
    screen: str
    arch: str
    device_id: str
    locale: str
    timezone: str

    def user_agent(self, *, app_version: str, build_number: int) -> MobileUserAgentPayload:
        """The handshake/login payload, with the version the caller pins."""
        return MobileUserAgentPayload(
            device_type=DeviceType.ANDROID,
            app_version=app_version,
            os_version=self.os_version,
            timezone=self.timezone,
            screen=self.screen,
            push_device_type="GCM",
            arch=self.arch,
            locale=self.locale,
            build_number=build_number,
            device_name=self.device_name,
            device_locale=self.locale,
        )

    def upload_user_agent(self, *, app_version: str) -> str:
        """The `User-Agent` header the app puts on a media upload.

        Derived from the same phone on purpose: the control socket and the upload
        POST describing different devices would be a contradiction nothing else
        explains.
        """
        return f"OKMessages/{app_version} ({self.os_version}; {self.device_name}; {self.screen})"


def reference_identity() -> MaxIdentity:
    """A deterministic identity for unit tests; real installs never use it."""
    return MaxIdentity(device_id="0" * 16, **LEGACY_IDENTITY)


def new_device_id() -> str:
    """A random per-install `deviceId`: 16 lowercase hex characters."""
    return secrets.token_hex(8)


def load_or_create(
    session_dir: Path,
    *,
    timezone: str | None = None,
    session_exists: bool = False,
    on_repair: Callable[[str], None] | None = None,
) -> MaxIdentity:
    """Read this install's identity, drawing one only when there is none to read.

    `timezone` is the owner's own IANA zone when the bridge knows it — a user in
    Novosibirsk reporting Moscow is a free inconsistency. It is only consulted
    when the identity is first created; changing it later does not re-fingerprint
    an account the server has already seen.

    `session_exists` says an account was logged in before this file existed. Such
    an install keeps the identity it has been showing all along (`LEGACY_IDENTITY`)
    rather than turning into a different phone on upgrade.

    **Recovery has one rule: never hand back an identity that is not on disk.**
    An unreadable primary falls back to the copy written beside it, and the copy
    is immediately promoted so the next start reads a good primary. If neither is
    usable, one replacement is drawn *and written* — once, not on every start.
    The old code drew a fresh `device_id` on every read of a broken file without
    ever repairing it, which turned a single torn write into an account whose
    `ANDROID_ID` changed at every restart, for ever.

    `on_repair` is called with a one-line reason whenever the file had to be
    recovered or replaced. A machine that stopped badly enough to need this is
    worth telling somebody about.
    """
    path = session_dir / IDENTITY_FILE
    stored, from_backup = read_with_backup(path, _parse)

    if stored is not None:
        if not from_backup:
            # An identity written before this file kept copies has none, and —
            # being valid — will never be rewritten, so it would never get one.
            # Seeding it touches only the copy; the primary keeps its bytes and
            # its mtime, and the account keeps reporting the same phone.
            ensure_backup(path)
        if from_backup:
            # The primary is gone or unreadable and the copy is good. Promote it
            # now: leaving the primary broken means doing this again next start,
            # and the copy is one bad write away from being the last one.
            _write(path, stored)
            _say_repaired(
                on_repair,
                f"the MAX identity file was unreadable and was restored from "
                f"{path.name}.bak; the account keeps the same device",
            )
        return stored

    if not path.exists() and not (path.with_name(path.name + ".bak")).exists():
        # Nothing to recover because nothing was ever written. The ordinary
        # first-run path, and the only one that may draw a new device.
        identity = _draw(timezone=timezone, session_exists=session_exists)
        _write(path, identity)
        return identity

    # Both copies exist and neither parses. Drawing a *new* phone here would look
    # like a stolen account, so the legacy identity is what gets written — the one
    # every install showed before this file existed. Written, not just returned:
    # that is the whole difference between recovering and drifting.
    identity = MaxIdentity(device_id=new_device_id(), **LEGACY_IDENTITY)
    _write(path, identity)
    _say_repaired(
        on_repair,
        "the MAX identity file and its copy were both unreadable; a replacement "
        "was written once and will not change again. If you have a backup of "
        "data/max-session, restoring it keeps the device this account was using.",
    )
    return identity


def _say_repaired(on_repair: Callable[[str], None] | None, reason: str) -> None:
    logger.error("%s", reason)
    if on_repair is not None:
        on_repair(reason)


def _draw(*, timezone: str | None, session_exists: bool) -> MaxIdentity:
    if session_exists:
        logger.info("keeping the identity this session was created with")
        return MaxIdentity(device_id=new_device_id(), **LEGACY_IDENTITY)

    device_name, os_version, screen, arch = secrets.choice(DEVICES)
    logger.info("drew a device identity for this install: %s", device_name)
    return MaxIdentity(
        device_id=new_device_id(),
        device_name=device_name,
        os_version=os_version,
        screen=screen,
        arch=arch,
        locale="ru",
        timezone=timezone or FALLBACK_TIMEZONE,
    )


def _parse(raw: str) -> MaxIdentity:
    """Every field, or nothing. A half-read identity is a different phone."""
    decoded = json.loads(raw)
    return MaxIdentity(**{key: decoded[key] for key in MaxIdentity.__dataclass_fields__})


def _write(path: Path, identity: MaxIdentity) -> None:
    try:
        write_atomic(path, json.dumps(asdict(identity), indent=2) + "\n")
    except OSError:
        # Not fatal on its own, but it means the next start may not find this
        # identity — worth a loud line rather than a debug one.
        logger.error("could not persist the MAX identity to %s", path, exc_info=True)
