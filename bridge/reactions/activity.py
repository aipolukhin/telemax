"""Which dialogs are worth asking about often.

Reactions have to be polled: MAX announces one on the newest message and nothing
else (see `sync.on_chat_reaction`). A single interval cannot be right for both
cases — short enough to feel immediate while somebody is typing is also short
enough to keep asking about a dialog nobody has touched in a week.

So a dialog is "warm" for a couple of minutes after anything happens in it, and
the poller asks warm dialogs often and the rest rarely. The signal is deliberately
crude: any chat update at all counts, because a reaction *is* a chat update.
"""

from __future__ import annotations

import time

#: How long anything that happens keeps a dialog on the fast cadence.
WARM_SECONDS = 120.0


class DialogActivity:
    """Remembers when each dialog was last heard from."""

    def __init__(self, warm_seconds: float = WARM_SECONDS) -> None:
        self._warm_seconds = warm_seconds
        self._seen: dict[int, float] = {}

    def touch(self, max_chat_id: int) -> None:
        self._seen[max_chat_id] = time.monotonic()

    def is_warm(self, max_chat_id: int) -> bool:
        last = self._seen.get(max_chat_id)
        return last is not None and time.monotonic() - last < self._warm_seconds
