"""Where onboarding is, written down so a restart does not lose the thread.

Two kinds of state live here and they are deliberately different:

* **where the deployment is** (`Stage`) — bootstrap done, bot up, MAX session
  valid, bridge running. This is durable and it is what the runtime consults on
  every start to decide whether to serve or to ask.
* **which question is outstanding** (`Step`) — waiting for a phone, a code, a
  2FA password. This survives a restart *as a step*, never as a value: the code
  itself belongs to a MAX auth attempt that died with the process, so
  `load()` rewinds any step that was mid-conversation back to the beginning.

Nothing secret is ever stored. Not the code, not the 2FA password, not the setup
token — only its SHA-256. The file is 0600 and written atomically, because a
truncated state file would strand the owner between "already set up" and "not
set up" with no way to tell which.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from pathlib import Path

from bridge.config.writer import atomic_write_text

logger = logging.getLogger(__name__)

STATE_FILE_NAME = "onboarding.json"


class Stage(StrEnum):
    """How far the deployment has got. Only ever moves forward."""

    BOOTSTRAP_CONFIGURED = "bootstrap_configured"
    BOT_RUNNING = "bot_running"
    MAX_ONBOARDING_PENDING = "max_onboarding_pending"
    MAX_SESSION_VALID = "max_session_valid"
    BRIDGE_RUNNING = "bridge_running"


class Step(StrEnum):
    """Which question the guardian is waiting on."""

    IDLE = "idle"
    #: The first question of all, and the only one that is not about MAX: the
    #: console cannot know what a phone's clock says, so it does not ask.
    WAITING_FOR_TIMEZONE = "waiting_for_timezone"
    #: "Use the number Telegram already has, or a different one?"
    WAITING_FOR_PHONE_CHOICE = "waiting_for_phone_choice"
    WAITING_FOR_PHONE = "waiting_for_phone"
    WAITING_FOR_CODE = "waiting_for_code"
    WAITING_FOR_2FA = "waiting_for_2fa"
    VALIDATING = "validating"
    SAVING = "saving"
    STARTING_BRIDGE = "starting_bridge"
    COMPLETED = "completed"


#: Steps that only exist inside a live MAX auth attempt. After a restart the
#: attempt is gone: the server's code is bound to a request nobody holds any
#: more, so asking for it again would fail in a way nobody could explain.
EPHEMERAL_STEPS = frozenset(
    {Step.WAITING_FOR_CODE, Step.WAITING_FOR_2FA, Step.VALIDATING, Step.SAVING}
)


@dataclass(frozen=True, slots=True)
class OnboardingRecord:
    """Everything that must outlive the process, and nothing that must not."""

    stage: Stage = Stage.BOOTSTRAP_CONFIGURED
    step: Step = Step.IDLE
    owner_user_id: int = 0
    guardian_username: str | None = None
    #: Which number the owner chose for MAX — `same` as Telegram's, or `other`.
    #: The number itself is never here: it lives in `.env` at 0600, once.
    max_phone_mode: str | None = None
    #: The owner's runtime override for carrying their own MAX messages into
    #: Telegram, set from the home screen. `None` means "follow the config
    #: default" (`own_messages.mirror`); a bool is an explicit choice that wins
    #: over it. Kept here because this is where owner-set preferences already
    #: live, atomically and at 0600.
    mirror_own_messages: bool | None = None
    # `owner_mtproto_intake_enabled` used to live here: a flag that made the
    # owner's MTProto session authoritative and, when false, let the Bot API
    # handlers carry the owner's messages instead. There is no second ingress any
    # more, so there is nothing for the flag to select. It is not kept as a dead
    # boolean — a toggle that changes nothing is worse than no toggle. Old state
    # files carrying it still load: `_read` keeps only known keys, so the value is
    # dropped on the next save without a migration.
    #: Which naming contract the guardian's username was minted under —
    #: `v2-account-identity` when it was derived from the two owner accounts,
    #: `adopted` when the owner pasted a token for a bot they had already made.
    #: Operational only: it changes nothing at run time and exists so
    #: reconciliation can tell a name it could recompute from one it could not.
    guardian_naming: str | None = None
    token_digest: str | None = None
    token_expires_at: int | None = None
    token_used_at: int | None = None
    #: The single status message the guardian keeps editing, so a restart edits
    #: it instead of posting a second one.
    status_chat_id: int | None = None
    status_message_id: int | None = None
    #: One message id per open user problem, so a notification about a MAX
    #: outage is edited as the outage evolves rather than re-sent beside itself.
    #: Kept here rather than in a new `alert_incidents` column because the map is
    #: about *presentation*: an entry that is lost costs one extra message, and
    #: a schema migration for that is a poor trade. Durable, atomic and 0600,
    #: exactly like the anchor's own id two lines above.
    notifications: dict[str, int] = field(default_factory=dict)
    #: Bumped whenever a screen that can *change* something is drawn, and again
    #: when its button is honoured. A tap carrying an older number belongs to a
    #: question that has already been answered, so it is refused rather than
    #: performed twice — the anchor is edited in place and the owner cannot tell
    #: by looking whether the confirmation they are staring at is still live.
    screen_revision: int = 0
    updated_at: int = 0

    @property
    def max_ready(self) -> bool:
        return self.stage in {Stage.MAX_SESSION_VALID, Stage.BRIDGE_RUNNING}


class StateStore:
    """Loads and saves one `OnboardingRecord`, atomically, at 0600."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._cached: OnboardingRecord | None = None

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> StateStore:
        return cls(data_dir / "state" / STATE_FILE_NAME)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> OnboardingRecord:
        """Read the record, rewinding anything that cannot survive a restart."""
        if self._cached is not None:
            return self._cached

        record = self._read()
        if record.step in EPHEMERAL_STEPS:
            logger.info(
                "onboarding was at %s when the process stopped; asking again from the start",
                record.step.value,
            )
            record = replace(record, step=Step.WAITING_FOR_PHONE)
        self._cached = record
        return record

    def _read(self) -> OnboardingRecord:
        if not self._path.exists():
            return OnboardingRecord()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A damaged state file must not stop the service: the worst case is
            # that the owner is asked to connect MAX again, and a valid MAX
            # session short-circuits that anyway.
            logger.warning("onboarding state file is unreadable; starting from scratch")
            return OnboardingRecord()

        if not isinstance(raw, dict):
            return OnboardingRecord()

        known = set(OnboardingRecord.__slots__)
        data = {key: value for key, value in raw.items() if key in known}
        try:
            # JSON has no enums: without this the record round-trips as plain
            # strings, every `is` comparison quietly fails, and the runtime
            # decides it has never been set up.
            if "stage" in data:
                data["stage"] = Stage(data["stage"])
            if "step" in data:
                data["step"] = Step(data["step"])
            if "notifications" in data:
                # JSON object keys are strings and the values came off disk.
                # A damaged entry costs one extra message, never an exception
                # inside the health loop.
                raw_map = data["notifications"]
                data["notifications"] = (
                    {
                        str(name): int(value)
                        for name, value in raw_map.items()
                        if isinstance(value, int)
                    }
                    if isinstance(raw_map, dict)
                    else {}
                )
            return OnboardingRecord(**data)
        except (TypeError, ValueError):
            logger.warning("onboarding state file does not match the schema; ignoring it")
            return OnboardingRecord()

    def save(self, record: OnboardingRecord) -> OnboardingRecord:
        stamped = replace(record, updated_at=int(time.time()))
        atomic_write_text(
            self._path, json.dumps(asdict(stamped), ensure_ascii=False, indent=2) + "\n"
        )
        self._cached = stamped
        return stamped

    def update(self, **changes: object) -> OnboardingRecord:
        """Change some fields, keeping every field somebody else has written.

        The base is re-read from disk rather than taken from `self._cached`, and
        that is the whole of this method's safety. `load()` answers from the
        cache — which is right for a reader — but a *writer* that trusts it
        writes back a record from before whatever anybody else has done since,
        and every field it does not name is silently rolled back.

        This was not a timing race, which is why it was invisible. There were
        simply two live stores over one file: the runtime's, held by the status
        board, the state machine and the onboarding router, and a second one
        built per alert by the notification centre. The first cached a record
        with no `notifications` key, and its very next write — a screen revision
        bumped by any confirmation screen — put that record back on disk. The
        map went, the recovery for the open incident was dropped for want of a
        message id, and the red notification stayed in the chat for ever.

        No lock is taken and none is needed *inside one process*: there is no
        `await` between this read and the write below, asyncio is single
        threaded, and nothing runs a store in a thread. The residual is a
        second *process* — `telemax setup` writes this file too and does not
        hold the data directory's `flock`. Running setup against a live Telemax
        can still lose a field, which is an operational rule rather than
        something this method can enforce.
        """
        current = self._read()
        if current.step in EPHEMERAL_STEPS:
            # `load()` rewinds these; a write must not resurrect one.
            current = replace(current, step=Step.WAITING_FOR_PHONE)
        return self.save(replace(current, **changes))  # type: ignore[arg-type]
