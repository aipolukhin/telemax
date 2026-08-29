"""Configuration models.

Every knob the bridge has lives here, and every one of them has a default that
is safe for a personal setup. Two rules hold across the whole file:

* **No secrets in the models.** A bridge names an environment variable that
  holds its bot token; the token itself is resolved later, into a `SecretStr`
  that never lands in a repr, a log line or the database.
* **Paths come from config, never from the code.** Defaults hang off
  `BRIDGE_DATA_DIR` so the same package runs from a checkout and from
  `/var/lib/max-bridge` without edits.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

# Bot API refuses to download anything bigger, so a larger value would only
# produce failures deep in the media pipeline instead of at startup.
TELEGRAM_DOWNLOAD_LIMIT_MB = 20


logger = logging.getLogger(__name__)


class Strict(BaseModel):
    """Reject unknown keys: a typo in YAML must fail loudly, not silently."""

    model_config = ConfigDict(extra="forbid", frozen=True)


#: Settings that used to exist and no longer do, per section. A typo must still
#: stop the service — that is what `extra="forbid"` is for — but a key the bridge
#: itself removed is not the owner's mistake, and refusing to start over one
#: turns an upgrade into an outage. They are dropped with a line in the log.
RETIRED: dict[str, tuple[str, ...]] = {
    # The inline reaction picker and its keyboard. Reactions enter on the owner's
    # puppet session now; there is nothing left for a bot to offer.
    "reactions": ("picker", "picker_emoji"),
}


def _drop_retired(section: str) -> Any:
    def drop(value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        stale = [key for key in RETIRED[section] if key in value]
        if stale:
            logger.warning(
                "%s: %s no longer exist and were ignored", section, ", ".join(sorted(stale))
            )
            value = {key: item for key, item in value.items() if key not in stale}
        return value

    return model_validator(mode="before")(drop)


def _coerce_off(value: object) -> object:
    """Let YAML's `off` mean the string "off".

    YAML 1.1 turns bare `off` into `False`, so `mode: off` — the most natural
    way to write it — would otherwise fail validation with a confusing message.
    `on` is accepted for symmetry only where a mode named "on" exists; here it
    never does, so `True` stays an error.
    """
    return "off" if value is False else value


class OutgoingTyping(StrEnum):
    """When the bridge sends `MSG_TYPING` into MAX.

    Telegram never tells a bot that the owner is typing (see ROADMAP 0.6), so
    this is always synthetic: it covers the seconds the bridge itself spends
    fetching and uploading, which is exactly when the contact sees silence.
    """

    OFF = "off"
    MEDIA_ONLY = "media_only"
    ALWAYS = "always"


class ReadReceiptStyle(StrEnum):
    """How a contact's read mark is rendered in Telegram.

    Bot API has no read receipts and cannot draw ✓✓, so the tick is our own
    markup. Which markup is possible depends on who owns the message:

    * `REACTION` (default) — 👀 on the last message the contact read, including
      the owner's own messages: a bot may react to those in a private chat even
      though it may not edit them (verified in compatibility tests). The closest thing to a
      tick that adds nothing to the conversation — which is not the same as
      making no sound: `setMessageReaction` takes no `disable_notification`, and
      a reaction lands on a message the owner wrote, so whether it notifies them
      is Telegram's call and the owner's notification settings, not ours.
    * `LINE` — one status message of the bot's own, kept at the bottom and
      edited in place: `✓ доставлено 14:10 · ✓✓ прочитано 14:12`. The only style
      that shows a *time*, and the only one sent silently, at the cost of an
      extra message in the chat.
    * `SUFFIX` — appends the tick to the message text. Works only where the bot
      owns the message, so it falls back to a reaction elsewhere.

    All three share one constraint: a bot holds a single reaction per message
    (`REACTIONS_TOO_MANY` on a second), so 👀 and the mirror of the contact's own
    reaction compete for the same slot on the same message.
    """

    LINE = "line"
    SUFFIX = "suffix"
    REACTION = "reaction"
    OFF = "off"


class AutoRead(StrEnum):
    """When the bridge marks a MAX chat as read on the owner's behalf.

    `ON_READ` is the honest one: Telegram tells the owner's own session which
    messages the owner has actually opened, and only those are marked. It needs
    the owner MTProto session; answering still counts as reading, so a reply
    marks under this mode too.

    `ON_DELIVERY` changes what the contact observes — they see the message read
    the moment it reaches Telegram, looked at or not — so it is not the default.
    """

    ON_READ = "on_read"
    ON_REPLY = "on_reply"
    ON_DELIVERY = "on_delivery"
    OFF = "off"


class ReactionStyle(StrEnum):
    NATIVE = "native"
    TEXT = "text"
    OFF = "off"


class ProvisioningMode(StrEnum):
    OFF = "off"
    GUARDIAN = "guardian"
    #: Managed Bots and nothing else: no account credential on disk. This is the
    #: production shape — `auto_mtproto` differs only by keeping a user session
    #: for the counts and the `/start`, which is useful while debugging.
    MANAGED = "managed"
    AUTO_MTPROTO = "auto_mtproto"


class UnknownChatPolicy(StrEnum):
    ASK = "ask"
    IGNORE = "ignore"
    AUTO = "auto"


class TimestampStyle(StrEnum):
    """How each delivered message is stamped with its MAX time.

    The time Telegram shows is when the *bridge* delivered a message, which is
    the same thing only while the bridge is up. After downtime the backfill
    arrives in one burst, and without a stamp an hour-old message looks current.

    `compact` prints `HH:MM`, and prepends `DD.MM` when the message is not from
    today — a date on every line is noise in a live conversation.
    """

    OFF = "off"
    COMPACT = "compact"
    FULL = "full"


class BridgeSource(StrEnum):
    """Where a bridge came from — YAML seed or runtime provisioning."""

    YAML = "yaml"
    GUARDIAN = "guardian"
    MTPROTO = "mtproto"


class PathsConfig(Strict):
    data_dir: Path = Path("./data")
    temp_dir: Path | None = None
    media_cache_dir: Path | None = None
    secrets_dir: Path | None = None
    db_name: str = "bridge.db"

    @property
    def resolved_temp_dir(self) -> Path:
        return self.temp_dir or self.data_dir / "tmp"

    @property
    def resolved_media_cache_dir(self) -> Path:
        return self.media_cache_dir or self.data_dir / "media-cache"

    @property
    def resolved_secrets_dir(self) -> Path:
        return self.secrets_dir or self.data_dir / "secrets"

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_name


class TelegramConfig(Strict):
    owner_user_id: int = Field(gt=0)
    # Telegram renders times in the *reader's* device timezone and never tells a
    # bot what that is, so the bridge has to be told. Unset means the host's own
    # timezone, which on a server is usually UTC and therefore usually wrong.
    timezone: str | None = None
    # Telegram's own timestamp is the delivery time, not the time the message
    # was written in MAX; the two differ after every reconnect backfill.
    message_timestamp: TimestampStyle = TimestampStyle.COMPACT

    _off_alias = field_validator("message_timestamp", mode="before")(_coerce_off)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str | None) -> str | None:
        """Fail at load time rather than print 1970 dates for a month."""
        if value is None:
            return None
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(
                f"unknown timezone {value!r} — use an IANA name like Europe/Moscow"
            ) from error
        return value


class NativeMediaConfig(Strict):
    """Whether voice messages and video notes are carried natively into MAX.

    The switches are independent so an operator can disable one media kind when
    an upstream compatibility change affects it without losing the other.
    """

    enabled: bool = True
    voice_enabled: bool = True
    circle_enabled: bool = True


class OwnPresence(StrEnum):
    """What the owner's MAX contacts see about the owner while the bridge runs.

    The bridge keeps one MAX socket open around the clock, and MAX reads a
    connected session as «В сети» — so before this switch existed, the owner was
    permanently online to every contact. What actually decides it is one field
    on the keepalive frame; `bridge.max_client.interactive` owns that behavior.

    * `mirror` (default) — online only while the owner is actually doing
      something through the bridge, then a last-seen that stops at that moment.
      The same thing their phone would show.
    * `offline` — never online. Last-seen freezes shortly after each connect and
      nothing the owner does moves it.
    * `online` — the old behaviour: permanently «В сети».
    """

    MIRROR = "mirror"
    OFFLINE = "offline"
    ONLINE = "online"


class MaxConfig(Strict):
    session_dir: Path | None = None
    session_name: str = "max-session.db"
    phone_env: str = "MAX_PHONE"
    native_media: NativeMediaConfig = NativeMediaConfig()
    own_presence: OwnPresence = OwnPresence.MIRROR
    # How long after the owner's last action the account keeps saying «В сети».
    # A mobile foreground session stops pinging when it is backgrounded, while a
    # reply typed in Telegram has no "and now I put the phone down" event.
    own_presence_idle_seconds: float = Field(default=90.0, ge=5, le=3600)
    # How often the tail of every bridged chat is re-read and pushed through the
    # ordinary dedup. The catch-up used to run at start-up and after a reconnect
    # only, so a missed push on a connection that never dropped would be
    # invisible until the next restart. Costs one history call per bridge per period and delivers
    # nothing that already has a claim. 0 turns it off.
    history_reconcile_seconds: float = Field(default=300.0, ge=0, le=86_400)


class MediaConfig(Strict):
    max_file_size_mb: int = Field(default=TELEGRAM_DOWNLOAD_LIMIT_MB, ge=1, le=2000)


class OwnMessagesConfig(Strict):
    """Whether a message the owner writes in the MAX app is carried into Telegram.

    Off by default, and on purpose. Telemax is first of all for people who use
    the bot *instead* of MAX, so a message typed in the MAX app is the exception,
    not the rule. Carrying it needs either a `Вы:` line on the bot's side of the
    chat, or — to place it as the owner's own on the right — Telegram Premium's
    Business connection. Both are more machinery than the common case wants, for
    a message the owner is unlikely to send at all.

    When this is off, such a message is simply not carried: not as the owner,
    not as a `Вы:` line. When on, the existing behaviour returns — placed as the
    owner's own through a business connection when one exists, and as a `Вы:`
    line otherwise. The owner can flip it from the guardian's home screen; this
    is only the default that flip starts from.
    """

    mirror: bool = False


class PresenceConfig(Strict):
    mirror_typing: bool = True
    outgoing_typing: OutgoingTyping = OutgoingTyping.MEDIA_ONLY
    read_receipt_style: ReadReceiptStyle = ReadReceiptStyle.REACTION
    read_receipt_suffix: str = " ✓✓"
    auto_read: AutoRead = AutoRead.ON_READ
    # Telegram's own indicator lasts about five seconds; refreshing faster than
    # that only burns rate limit.
    typing_refresh_seconds: float = Field(default=4.0, gt=0, le=30)
    # How long a MAX typing burst keeps the Telegram indicator alive without new
    # events — a contact who walks away mid-word should not type forever.
    typing_ttl_seconds: float = Field(default=30.0, gt=0, le=300)
    # Period for our own typing signal into MAX. Five seconds keeps the
    # indicator ahead of the ordinary cadence with room
    # for a slow round trip.
    max_typing_period_seconds: float = Field(default=5.0, gt=0, le=30)
    # A pinned line at the top of the bot chat saying whether the contact is
    # around. MAX reports last-seen; Telegram lets a bot pin and edit its own
    # message in a private chat.
    pin_contact_status: bool = True
    # Presence is chatty and every edit costs rate limit.
    pin_refresh_seconds: float = Field(default=60.0, ge=5, le=3600)

    _off_aliases = field_validator(
        "outgoing_typing", "read_receipt_style", "auto_read", mode="before"
    )(_coerce_off)


class ReactionsConfig(Strict):
    style: ReactionStyle = ReactionStyle.NATIVE
    # MAX only pushes a chat update when its own "last reaction" pointer moves,
    # so a reaction put on or taken off an older message may produce no event.
    # Polling recent reactions is the fallback for
    # those. 0 turns it off and leaves the bridge reacting to pushes alone.
    poll_seconds: float = Field(default=20.0, ge=0, le=600)
    # While a dialog is live, an interval this long reads as a delay; when it is
    # asleep, the same interval is one pointless call per dialog per tick. So a
    # dialog that has just seen something is asked about this often instead.
    poll_active_seconds: float = Field(default=3.0, ge=0.5, le=60)

    _off_aliases = field_validator("style", mode="before")(_coerce_off)
    _retired = _drop_retired("reactions")


class ProvisioningConfig(Strict):
    mode: ProvisioningMode = ProvisioningMode.OFF
    unknown_chat_policy: UnknownChatPolicy = UnknownChatPolicy.ASK
    guardian_bot_token_env: str | None = None
    pending_ttl_days: int = Field(default=7, ge=1, le=90)
    pending_max_messages: int = Field(default=200, ge=1, le=10_000)
    # Tokens handed out at runtime land here, 0600, one `NAME=value` per line.
    # The database only ever stores the variable name.
    secrets_file_name: str = "bots.env"
    # Creating a bot is the one thing Bot API cannot do, so `auto_mtproto` needs
    # the owner's own Telegram account: api_id and api_hash from my.telegram.org,
    # and the phone that account is registered to. All three come from the
    # environment — never from this file.
    mtproto_api_id_env: str = "TELEMAX_API_ID"
    mtproto_api_hash_env: str = "TELEMAX_API_HASH"
    mtproto_phone_env: str = "TELEMAX_PHONE"
    # Bot API cannot read the account's bot limit — only the app config knows it,
    # and only a user session can read that. Used to draw a count; Telegram's own
    # refusal at creation time is what actually enforces anything.
    assumed_bot_limit: int = Field(default=20, ge=1, le=100)
    # Whether contact bots are created by driving @BotFather over the owner's
    # own session instead of Telegram's managed-bots dialog. On by default
    # because the dialog's Create button is not reliable across clients. Falls back
    # to managed bots on its own when no session credentials are configured.
    use_owner_session: bool = True

    _off_aliases = field_validator("mode", mode="before")(_coerce_off)


class BridgeEntry(Strict):
    name: str = Field(min_length=1, max_length=64)
    max_chat_id: int
    telegram_bot_token_env: str = Field(min_length=1)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _plain_name(cls, value: str) -> str:
        # The name shows up in log lines, table keys and file names; keep it boring.
        if not all(char.isalnum() or char in "-_" for char in value):
            raise ValueError("bridge name may only contain letters, digits, '-' and '_'")
        return value


class AppConfig(Strict):
    paths: PathsConfig = PathsConfig()
    telegram: TelegramConfig
    max: MaxConfig = MaxConfig()
    media: MediaConfig = MediaConfig()
    own_messages: OwnMessagesConfig = OwnMessagesConfig()
    presence: PresenceConfig = PresenceConfig()
    reactions: ReactionsConfig = ReactionsConfig()
    provisioning: ProvisioningConfig = ProvisioningConfig()
    bridges: tuple[BridgeEntry, ...] = ()
    log_level: str = "INFO"

    @property
    def max_session_dir(self) -> Path:
        return self.max.session_dir or self.paths.data_dir / "max-session"

    @property
    def secrets_file(self) -> Path:
        return self.paths.resolved_secrets_dir / self.provisioning.secrets_file_name


class ResolvedBridge(Strict):
    """A bridge with its token filled in from the environment.

    `SecretStr` is what keeps the token out of tracebacks and log lines; the
    value is only unwrapped where a `Bot` is constructed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    max_chat_id: int
    token_env: str
    token: SecretStr
    source: BridgeSource = BridgeSource.YAML
    enabled: bool = True
