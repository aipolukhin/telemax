"""The service that outlives the SSH session.

`nohup` and a stray `Popen` both work until the first reboot, the first crash,
or the first time the owner closes a laptop lid — and then the bridge is simply
gone, with nothing to say so. A systemd *user* unit is the smallest thing that
survives all three, needs no root, and can be asked whether it is actually
running.

One trap is worth naming: a user manager normally stops when the last session of
that user ends, so a service installed over SSH would die the moment the owner
disconnects. `loginctl enable-linger` is what prevents that, and it is the first
thing this module does.

Nothing here reports success it has not observed. `enable --now` returning zero
means systemd accepted the unit, not that the process stayed up; only
`is-active` says that, and it is polled.
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import bridge

DEFAULT_UNIT_NAME = "telemax.service"

#: Characters a systemd unit name may carry without escaping.
_INSTANCE_SAFE = re.compile(r"[^a-z0-9_-]+")


def unit_name(instance: str | None = None) -> str:
    """`telemax.service`, or `telemax-<instance>.service` for a second stand.

    Two installations on one host is a real case — a second phone number, a
    staging account — and without a distinct unit the second `setup` silently
    overwrites the first one's service file. So the instance is part of the
    name, and the default stays exactly what it always was.
    """
    if not instance:
        return DEFAULT_UNIT_NAME
    slug = _INSTANCE_SAFE.sub("-", instance.strip().lower()).strip("-")
    return f"telemax-{slug}.service" if slug else DEFAULT_UNIT_NAME


#: Kept for callers that only ever manage the single default installation.
UNIT_NAME = DEFAULT_UNIT_NAME

#: How long to keep asking whether the service came up. A cold start opens the
#: database, runs migrations and calls getMe; ten seconds is generous.
ACTIVE_TIMEOUT_SECONDS = 15.0
POLL_INTERVAL_SECONDS = 0.5

#: How long "active" has to *stay* true before it is believed.
#:
#: `Type=simple` marks a unit active the moment ExecStart is forked, before the
#: process has read its own config. A service that exits immediately therefore
#: looks perfectly healthy for a fraction of a second — measured on this
#: machine, `is-active` said `active` for a `bridge run` that had already
#: refused to start. Watching the restart counter over a few seconds is what
#: tells the two apart.
SETTLE_SECONDS = 4.0

#: The unit file holds paths, never credentials — those stay in .env at 0600.
UNIT_MODE = 0o644


class ServiceError(Exception):
    """The service could not be installed or would not start."""


@dataclass(frozen=True, slots=True)
class Completed:
    """Just enough of `subprocess.CompletedProcess` to be faked in a test."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner(Protocol):
    """How the manager reaches the outside world. Faked whole in tests."""

    def __call__(self, args: Sequence[str], *, timeout: float = ...) -> Completed: ...


def run_command(args: Sequence[str], *, timeout: float = 30.0) -> Completed:
    try:
        finished = subprocess.run(  # noqa: S603 - fixed argv, never a shell
            list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return Completed(returncode=127, stderr=str(error))
    return Completed(
        returncode=finished.returncode,
        stdout=finished.stdout or "",
        stderr=finished.stderr or "",
    )


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    """What the unit needs to know, all of it discovered rather than assumed."""

    python: Path
    project_dir: Path
    config_path: Path
    #: None for the one-installation-per-host case, which is almost all of them.
    instance: str | None = None

    @property
    def unit_name(self) -> str:
        return unit_name(self.instance)

    @classmethod
    def detect(cls, config_path: Path, *, instance: str | None = None) -> ServiceSpec:
        """The interpreter running right now, and the checkout it came from.

        `sys.executable` is the venv's python by construction — the setup
        command is being run through it — so there is nothing to guess and
        nothing to hard-code.

        Deliberately *not* resolved: a venv's `bin/python` is a symlink to the
        system interpreter, and following it would produce a unit that starts a
        Python which cannot see a single one of the venv's packages.
        """
        return cls(
            python=Path(sys.executable),
            project_dir=Path(bridge.__file__).resolve().parent.parent,
            config_path=config_path.expanduser().resolve(),
            instance=instance,
        )


def unit_path(*, home: Path | None = None, instance: str | None = None) -> Path:
    base = home or Path.home()
    return base / ".config" / "systemd" / "user" / unit_name(instance)


def render_unit(spec: ServiceSpec) -> str:
    """The unit text. Paths are absolute and real; secrets are not here."""
    suffix = f" ({spec.instance})" if spec.instance else ""
    return f"""\
[Unit]
Description=Telemax Telegram-MAX Bridge{suffix}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={spec.project_dir}
Environment=BRIDGE_CONFIG={spec.config_path}
ExecStart={spec.python} -m bridge run
Restart=on-failure
RestartSec=5
# Shutdown writes down what it has already accepted before it lets go. systemd's
# default is 90s, which is usually enough and is not a promise — if SIGKILL
# lands before the queues are flushed, every durable-intake guarantee dies on
# the last step. Stated explicitly, and comfortably above the drain timeout.
TimeoutStopSec=60
# The bridge holds a MAX session and bot tokens; keep the blast radius small.
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
"""


class ServiceManager:
    """Installs, starts and inspects the user unit.

    The command runner is injected so the whole class can be tested without a
    systemd on the machine running the tests.
    """

    def __init__(
        self,
        *,
        runner: CommandRunner = run_command,
        home: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        instance: str | None = None,
    ) -> None:
        self._run = runner
        self._home = home
        self._clock = clock
        self._sleep = sleep
        # Every systemctl call this manager makes names *this* unit. A manager
        # built for one instance must not be able to stop another one's service.
        self._unit = unit_name(instance)
        self._instance = instance

    @property
    def unit(self) -> str:
        return self._unit

    # ----------------------------------------------------------------- probing

    def available(self) -> bool:
        """Can this host run user services at all?"""
        if shutil.which("systemctl") is None:
            return False
        return self._systemctl("show-environment").returncode == 0

    def properties(self, *names: str) -> dict[str, str]:
        result = self._systemctl(
            "show", self._unit, *(f"--property={name}" for name in names)
        )
        values: dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, _, value = line.partition("=")
            if key:
                values[key.strip()] = value.strip()
        return values

    def is_active(self) -> bool:
        """Active *and* running — not merely forked.

        `SubState` is the part that matters: a unit in `auto-restart` after a
        crash still reports `ActiveState=activating`, and one that forked a
        process which then died reports `running` only until it does.
        """
        state = self.properties("ActiveState", "SubState")
        return state.get("ActiveState") == "active" and state.get("SubState") == "running"

    def restart_count(self) -> int:
        raw = self.properties("NRestarts").get("NRestarts", "0")
        return int(raw) if raw.isdigit() else 0

    def stays_up(self, settle: float = SETTLE_SECONDS) -> bool:
        """Watch for a few seconds. A crash loop is not a running service."""
        before = self.restart_count()
        deadline = self._clock() + settle
        while self._clock() < deadline:
            self._sleep(POLL_INTERVAL_SECONDS)
            if not self.is_active() or self.restart_count() != before:
                return False
        return True

    def status_tail(self, lines: int = 5) -> str:
        """A few sanitised lines to explain a refusal. Never a raw traceback."""
        result = self._systemctl("status", self._unit, "--no-pager", f"--lines={lines}")
        from bridge.observability.redaction import redact

        text = (result.stdout or result.stderr or "").strip()
        return redact(text)[-400:]

    # -------------------------------------------------------------- installing

    def install(self, spec: ServiceSpec) -> Path:
        """Write the unit, enable it, start it, and prove it is running."""
        if not self.available():
            raise ServiceError(
                "systemctl --user недоступен на этой машине.\n"
                "Без него мост не переживёт закрытие SSH."
            )

        self.enable_linger()

        from bridge.config.writer import atomic_write_text

        path = unit_path(home=self._home, instance=self._instance)
        atomic_write_text(path, render_unit(spec), mode=UNIT_MODE)

        for command in (
            ("daemon-reload",),
            ("enable", self._unit),
            ("restart", self._unit),
        ):
            result = self._systemctl(*command)
            if result.returncode != 0:
                raise ServiceError(
                    f"systemctl --user {' '.join(command)} не сработал:\n"
                    f"{(result.stderr or result.stdout).strip()[:200]}"
                )

        if not self.wait_until_active():
            raise ServiceError(
                f"Сервис {self._unit} не поднялся.\n"
                + (self.status_tail() or "Причина неизвестна.")
            )
        if not self.stays_up():
            raise ServiceError(
                f"Сервис {self._unit} запустился и сразу упал.\n"
                + (self.status_tail() or "Причина неизвестна.")
            )
        return path

    def enable_linger(self) -> bool:
        """Let the user manager run with nobody logged in.

        Best effort on purpose: a machine where the owner is already lingering,
        or where `loginctl` is absent, is not a reason to abort — but a machine
        where it fails silently is exactly how "it stopped when I closed the
        terminal" happens, so the caller gets told.
        """
        if shutil.which("loginctl") is None:
            return False
        user = os.environ.get("USER") or getpass.getuser()
        return self._invoke("loginctl", "enable-linger", user).returncode == 0

    def wait_until_active(self, timeout: float = ACTIVE_TIMEOUT_SECONDS) -> bool:
        deadline = self._clock() + timeout
        while True:
            if self.is_active():
                return True
            if self._clock() >= deadline:
                return False
            self._sleep(POLL_INTERVAL_SECONDS)

    def restart(self) -> bool:
        if self._systemctl("restart", self._unit).returncode != 0:
            return False
        return self.wait_until_active() and self.stays_up()

    def stop(self) -> bool:
        """Stop the running runtime, leaving the unit installed.

        Needed before the guardian bot is deleted and recreated: the old token
        is long-polling right up until the process holding it goes away, and two
        guardians answering the same chat is a state with no honest recovery.
        Best effort — a unit that was never installed is already stopped.
        """
        self._systemctl("stop", self._unit)
        return not self.is_active()

    def uninstall(self) -> None:
        """Undo a half-finished install. Never raises: this is the rollback."""
        self._systemctl("disable", "--now", self._unit)
        unit_path(home=self._home, instance=self._instance).unlink(missing_ok=True)
        self._systemctl("daemon-reload")

    # ---------------------------------------------------------------- plumbing

    def _systemctl(self, *args: str) -> Completed:
        return self._invoke("systemctl", "--user", *args)

    def _invoke(self, *args: str) -> Completed:
        binary = shutil.which(args[0])
        if binary is None:
            return Completed(returncode=127, stderr=f"{args[0]} not found")
        return self._run((binary, *args[1:]))
