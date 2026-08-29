"""How many more bots this account may have, and what each choice costs.

Telegram caps how many bots one account can own. The cap is not ours to guess —
it depends on the account, on Premium, and on whatever the server decides — so
everything here treats "unknown" as a first-class answer rather than defaulting
to a number that would eventually be wrong.

The arithmetic that matters is the *net* cost of a selection. A contact whose
deterministic bot already exists and is about to be rebuilt is deleted and
created again: it occupies one slot before and one slot after, so it needs no
free slot at all. Counting it as new is the difference between an owner being
told they have seven slots and being told they have four, and the second number
would be a lie that stops them connecting people they can connect.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from enum import StrEnum

from .provisioner import BotLimit, UsernameState

#: Where a snapshot came from, for the one case that matters: a number read from
#: Telegram and a number assumed from configuration are not the same claim, and
#: the difference is why the picker and the home screen used to disagree.
SOURCE_TELEGRAM = "telegram"
#: Counted from this install's own bridges, because nothing could be asked. Was
#: being reported as `SOURCE_TELEGRAM` regardless, which is how a number that
#: knows about four bots was printed as a statement about an account holding six.
SOURCE_LOCAL = "local"
SOURCE_UNKNOWN = "unknown"

#: How long a snapshot is worth showing without asking again. Bots can be made
#: and deleted from a phone, so anything older than this is redrawn from live
#: numbers before it is used for a decision.
FRESH_SECONDS = 60.0


class PeerBotStatus(StrEnum):
    """What stands between a MAX contact and their deterministic bot."""

    #: Nothing exists under that username. Costs one free slot.
    NOT_CREATED = "not_created"
    #: Telegram confirms the owner owns it. Its token is fetched with
    #: `getManagedBotToken` and the bot is reused as it stands — nothing is
    #: deleted, nothing is recreated, and it costs no free slot.
    OWNED_REPLACEABLE = "owned_replaceable"
    #: Somebody else holds the name. Not selectable, and nothing is deleted.
    FOREIGN_USERNAME_COLLISION = "foreign_username_collision"
    #: A bot at our own name that this guardian cannot manage — made by hand in
    #: @BotFather rather than through the managed-bots flow. Not selectable, and
    #: the remedy is not the collision one.
    NOT_MANAGEABLE = "not_manageable"
    #: Local state claims the bot; Telegram does not. Telegram wins — the bot is
    #: created afresh and the stored token is not reused.
    LOCAL_STATE_MISMATCH = "local_state_mismatch"


#: Which statuses may be chosen at all, and what each one costs in free slots.
_SLOT_COST = {
    PeerBotStatus.NOT_CREATED: 1,
    PeerBotStatus.LOCAL_STATE_MISMATCH: 1,
    PeerBotStatus.OWNED_REPLACEABLE: 0,
}


def classify(state: UsernameState, *, locally_claimed: bool) -> PeerBotStatus:
    """Turn what Telegram says about a username into what the flow may do."""
    if state is UsernameState.OWNED:
        return PeerBotStatus.OWNED_REPLACEABLE
    if state is UsernameState.UNMANAGEABLE:
        return PeerBotStatus.NOT_MANAGEABLE
    if state is UsernameState.FOREIGN:
        return PeerBotStatus.FOREIGN_USERNAME_COLLISION
    return (
        PeerBotStatus.LOCAL_STATE_MISMATCH if locally_claimed else PeerBotStatus.NOT_CREATED
    )


def slot_cost(status: PeerBotStatus) -> int:
    """New slots consumed by provisioning this contact. Foreign never gets here."""
    return _SLOT_COST.get(status, 1)


def selectable(status: PeerBotStatus) -> bool:
    return status not in {
        PeerBotStatus.FOREIGN_USERNAME_COLLISION,
        PeerBotStatus.NOT_MANAGEABLE,
    }


@dataclass(frozen=True, slots=True)
class PeerPlan:
    """One MAX contact, and everything the picker needs to draw and price it."""

    max_chat_id: int
    max_peer_id: int | None
    title: str
    expected_username: str | None
    status: PeerBotStatus
    last_activity: int = 0

    @property
    def selectable(self) -> bool:
        # A contact MAX never told us the user id of has no stable identifier to
        # hash, and a bot named after a chat id would be renamed by MAX itself.
        return self.expected_username is not None and selectable(self.status)

    @property
    def slot_cost(self) -> int:
        return slot_cost(self.status)

    @property
    def is_replacement(self) -> bool:
        """Already exists, so provisioning it reuses rather than creates."""
        return self.status is PeerBotStatus.OWNED_REPLACEABLE


@dataclass(frozen=True, slots=True)
class Capacity:
    """The four numbers the picker shows, and the one rule it enforces.

    One snapshot serves every screen that mentions bots. That is the whole point
    of `checked_at` and `source`: the home screen used to count them itself, on
    its own schedule, which is how the owner was shown «свободно 39» above a
    provisioning run that stopped on a limit.
    """

    limit: BotLimit
    owned_bot_count: int
    plans: tuple[PeerPlan, ...] = ()
    selected: frozenset[int] = field(default_factory=frozenset)
    #: `time.time()` when these numbers were read, never when they were drawn.
    checked_at: float = 0.0
    source: str = SOURCE_UNKNOWN

    @property
    def bot_limit(self) -> int | None:
        return self.limit.value

    @property
    def counts_only_ours(self) -> bool:
        """Whether `owned_bot_count` is this install's bots rather than the account's.

        True on the Bot API path, which is production: there is no way to list
        the bots an account owns, so the count is the bridges this install
        created plus the guardian. A bot made by hand in @BotFather, or one left
        behind by an earlier install, is not in it — and still occupies a slot.

        Not a detail to hide. The owner counted six bots in their account
        against a screen reading «5 из 20» and reported it as a bug; the number
        was right about what it counts and wrong about what it looked like.
        """
        return self.source != SOURCE_TELEGRAM

    def is_fresh(self, *, now: float | None = None, within: float = FRESH_SECONDS) -> bool:
        """Whether this is recent enough to show without asking Telegram again."""
        if not self.checked_at:
            return False
        return (time.time() if now is None else now) - self.checked_at <= within

    @property
    def free_new_slots(self) -> int | None:
        """`limit - owned`, or None when Telegram never said what the limit is."""
        if self.limit.value is None:
            return None
        return max(0, self.limit.value - self.owned_bot_count)

    @property
    def selected_new_count(self) -> int:
        return sum(plan.slot_cost for plan in self.plans if plan.max_chat_id in self.selected)

    @property
    def replacement_count(self) -> int:
        return sum(
            1
            for plan in self.plans
            if plan.max_chat_id in self.selected and plan.is_replacement
        )

    @property
    def at_limit(self) -> bool:
        return self.free_new_slots == 0

    def plan_for(self, max_chat_id: int) -> PeerPlan | None:
        return next((plan for plan in self.plans if plan.max_chat_id == max_chat_id), None)

    def can_select(self, max_chat_id: int) -> bool:
        """Whether adding this contact to the selection is allowed *right now*.

        A replacement is always allowed: it gives a slot back before it takes
        one. A new bot needs a free slot, and when the limit is unknown there is
        no honest way to say there is one.
        """
        plan = self.plan_for(max_chat_id)
        if plan is None or not plan.selectable:
            return False
        if max_chat_id in self.selected:
            return True
        if plan.slot_cost == 0:
            return True
        free = self.free_new_slots
        if free is None:
            return False
        return self.selected_new_count + plan.slot_cost <= free

    def with_selection(self, selected: frozenset[int] | set[int]) -> Capacity:
        """`replace`, so a field added later keeps travelling with the snapshot."""
        return replace(self, selected=frozenset(selected))

    def with_plans(self, plans: tuple[PeerPlan, ...]) -> Capacity:
        return replace(self, plans=plans)
