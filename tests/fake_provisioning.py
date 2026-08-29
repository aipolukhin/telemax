"""A Telegram account that owns bots, without a Telegram account.

Provisioning is the one part of this project that cannot be exercised for real:
creating a bot needs @BotFather, deleting one needs @BotFather, and both need a
live user session. So everything below stands in for that account and records
what was asked of it — which is what lets the rules that matter ("a foreign bot
is never deleted", "a replacement costs no slot", "one worker per bot") be
assertions rather than intentions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bridge.provisioning.batch import BridgeConflictError
from bridge.provisioning.mtproto import CreatedBot, OwnedBot
from bridge.provisioning.provisioner import (
    BotLimit,
    CreationLimitError,
    ForeignUsernameError,
    UsernameState,
)


@dataclass
class FakeProvisioner:
    """Stands in for `MtprotoProvisioner`, with the same safety rules."""

    owned: dict[str, int] = field(default_factory=dict)
    foreign: set[str] = field(default_factory=set)
    limit: int | None = 20
    premium: bool = False
    #: Extra bots the account owns that Telemax knows nothing about.
    strangers: int = 0
    #: Whether this provisioner can count the *account's* bots — true of the
    #: session path, false of Bot API alone.
    sees_the_account: bool = True

    deleted: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    started: list[str] = field(default_factory=list)
    releases: list[str] = field(default_factory=list)

    #: Usernames whose next creation should fail, and how.
    fail_create: dict[str, Exception] = field(default_factory=dict)
    #: Usernames Telegram refuses to release after deletion.
    never_released: set[str] = field(default_factory=set)
    next_bot_id: int = 500
    #: How many times anybody counted the account's bots. The number every screen
    #: shows has to come from one reading, and this is how a test proves it.
    owned_reads: int = 0

    async def list_owned_bots(self, *, refresh: bool = True) -> list[OwnedBot]:
        self.owned_reads += 1
        bots = [
            OwnedBot(bot_id=bot_id, username=name) for name, bot_id in self.owned.items()
        ]
        bots += [
            OwnedBot(bot_id=9000 + index, username=f"someone_else_{index}_bot")
            for index in range(self.strangers)
        ]
        return bots

    async def account_bot_count(self) -> int | None:
        """This fake stands in for a session-backed provisioner, which can see
        the whole account — `strangers` is exactly the bots it knows about and
        Telemax does not. `None` models the Bot API path, which cannot ask."""
        if not self.sees_the_account:
            return None
        # Not through `list_owned_bots`: `owned_reads` counts *snapshots*, and
        # one snapshot asking two different questions must still read as one.
        return len(self.owned) + self.strangers

    async def get_creation_limit(self) -> BotLimit:
        return BotLimit(value=self.limit, premium=self.premium)

    async def check_username(self, username: str) -> UsernameState:
        target = username.lower()
        if target in self.owned:
            return UsernameState.OWNED
        if target in self.foreign:
            return UsernameState.FOREIGN
        return UsernameState.FREE

    async def delete_owned_bot(self, username: str) -> None:
        target = username.lower()
        if target in self.foreign:
            raise ForeignUsernameError(target)
        if target not in self.owned:
            return
        self.owned.pop(target)
        self.deleted.append(target)

    async def wait_for_username_release(self, username: str, **_: Any) -> bool:
        target = username.lower()
        self.releases.append(target)
        return target not in self.never_released

    async def create_bot(self, *, name: str, username: str) -> CreatedBot:
        """Managed Bots semantics: an existing bot yields its token, unchanged.

        This is the behaviour the whole rework turns on — `getManagedBotToken`
        makes "the bot is already there" an answer rather than an obstacle.
        """
        target = username.lower()
        failure = self.fail_create.pop(target, None)
        if failure is not None:
            raise failure
        if target in self.foreign:
            raise ForeignUsernameError(target)
        if target in self.owned:
            self.reused.append(target)
            # Named, like both real implementations name it: a reuse the caller
            # cannot tell from a creation is a reuse that unbinds the bridge row.
            return CreatedBot(
                username=target,
                token=f"{self.owned[target]}:{'A' * 35}",
                bot_id=self.owned[target],
            )
        if self.limit is not None and len(await self.list_owned_bots()) >= self.limit:
            raise CreationLimitError("BOT_CREATE_LIMIT_EXCEEDED")
        self.next_bot_id += 1
        self.owned[target] = self.next_bot_id
        self.created.append(target)
        return CreatedBot(username=target, token=f"{self.next_bot_id}:{'A' * 35}")


    async def send_start(self, username: str, bot_id: int | None = None) -> None:
        self.started.append(username.lower())


@dataclass
class FakeGateway:
    """The live process, reduced to what provisioning does to it."""

    stopped: list[int] = field(default_factory=list)
    saved: dict[int, str] = field(default_factory=dict)
    started: list[int] = field(default_factory=list)
    running: set[int] = field(default_factory=set)
    unhealthy: set[int] = field(default_factory=set)
    fail_start: set[int] = field(default_factory=set)
    #: Who the caller said is on the other end, per chat. The by-number path has
    #: to pass this: a dialog with no messages yet cannot be asked.
    peers: dict[int, int | None] = field(default_factory=dict)
    #: Chats whose row has been promoted out of `provisioning`. The last step of
    #: activation, and the only one that makes a restart serve the bridge.
    active: set[int] = field(default_factory=set)
    #: Bindings the register cannot hold: chat id to the sentence that says why.
    conflicts: dict[int, str] = field(default_factory=dict)

    async def preflight(self, *, max_chat_id: int, username: str) -> None:
        reason = self.conflicts.get(max_chat_id)
        if reason is not None:
            raise BridgeConflictError(reason)

    async def stop_bridge(self, max_chat_id: int) -> None:
        self.stopped.append(max_chat_id)
        self.running.discard(max_chat_id)

    async def save_token(self, *, max_chat_id: int, username: str, token: str) -> str:
        token_env = f"TELEMAX_BOT_{username.upper().removesuffix('_MAX_BOT')}"
        self.saved[max_chat_id] = token_env
        return token_env

    async def start_worker(
        self,
        *,
        max_chat_id: int,
        username: str,
        token_env: str,
        title: str,
        max_user_id: int | None = None,
    ) -> str:
        if max_chat_id in self.fail_start:
            raise RuntimeError("polling refused")
        self.started.append(max_chat_id)
        self.peers[max_chat_id] = max_user_id
        self.running.add(max_chat_id)
        return username.removesuffix("_max_bot")

    async def is_healthy(self, max_chat_id: int) -> bool:
        return max_chat_id in self.running and max_chat_id not in self.unhealthy

    async def mark_active(self, max_chat_id: int) -> None:
        self.active.add(max_chat_id)


@dataclass
class FakeBridgeRepository:
    """Just enough of `BridgeRepository` for the picker and the importer."""

    records: dict[int, Any] = field(default_factory=dict)
    cursors: dict[str, int] = field(default_factory=dict)
    lifecycles: dict[str, dict[str, str | None]] = field(default_factory=dict)

    async def by_max_chat(self, max_chat_id: int) -> Any:
        return self.records.get(max_chat_id)

    async def active(self) -> list[Any]:
        return [
            record
            for record in self.records.values()
            if getattr(record, "state", None) is None
            or str(getattr(record.state, "value", record.state)) == "active"
        ]

    async def all(self) -> list[Any]:
        """Running or not. «Мосты» lists both — a disconnected bridge still has
        a bot, and that bot is the one worth deleting."""
        return list(self.records.values())

    async def set_lifecycle(self, bridge_name: str, **changes: str | None) -> None:
        self.lifecycles.setdefault(bridge_name, {}).update(changes)

    async def history_cursor(self, bridge_name: str) -> int | None:
        return self.cursors.get(bridge_name)

    async def set_history_cursor(self, bridge_name: str, cursor: int) -> None:
        self.cursors[bridge_name] = max(cursor, self.cursors.get(bridge_name, 0))


@dataclass
class FakeDialogPicker:
    """MAX's dialog list, already resolved to names."""

    listing: list[Any]

    async def options(self, *, exclude: set[int]) -> list[Any]:
        return [item for item in self.listing if item.max_chat_id not in exclude]

    async def dialog_with(self, max_user_id: int) -> Any:
        """The dialog with one person, however old — what adding by number asks."""
        return next(
            (item for item in self.listing if item.max_user_id == max_user_id), None
        )


@dataclass
class FakeHistorySource:
    """A MAX chat with a fixed tail, delivered through the usual dedup."""

    messages: dict[int, list[int]] = field(default_factory=dict)
    failures: set[int] = field(default_factory=set)
    calls: list[tuple[int, int | None]] = field(default_factory=list)

    async def import_chat(
        self,
        max_chat_id: int,
        *,
        limit: int,
        after: int | None,
        on_progress: Any = None,
    ) -> Any:
        from bridge.provisioning.history import ChatImport

        self.calls.append((max_chat_id, after))
        if max_chat_id in self.failures:
            raise RuntimeError("MAX refused the history")
        tail = self.messages.get(max_chat_id, [])[-limit:]
        wanted = [item for item in tail if after is None or item > after]
        for index, _ in enumerate(wanted, start=1):
            if on_progress is not None:
                on_progress(index, len(wanted))
        cursor = max(wanted) if wanted else after
        return ChatImport(delivered=len(wanted), cursor=cursor)


def fake_bridge_view(
    item: Any,
    *,
    state: Any = None,
    last_delivery_at: int | None = None,
    queued: int = 0,
    detail: str | None = None,
) -> Any:
    """A `BridgeView` from a `BridgeSummary`, for the screens that render one.

    The guardian's bridge screens take a view model now rather than a summary
    plus a block of the bridge bot's own `/status` text. Building one by hand in
    every test would be four lines of noise per assertion.
    """
    from bridge.onboarding.views import BridgeFacts, BridgeUiState, bridge_view

    return bridge_view(
        BridgeFacts(
            title=item.title,
            username=item.username,
            bridge_name=getattr(item, "bridge_name", item.username),
            max_chat_id=item.max_chat_id,
            bot_id=getattr(item, "bot_id", None),
            state=state
            or (
                BridgeUiState.ACTIVE
                if getattr(item, "running", True)
                else BridgeUiState.DISABLED
            ),
            queued=queued,
            last_delivery_at=last_delivery_at,
            detail=detail,
        ),
        now_ms=0,
    )
