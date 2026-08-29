"""Retry policy: how long to wait, and when to stop waiting.

Two rules, both from the roadmap and both deliberate:

* the delay is capped, so a night-long outage does not turn into a week-long one;
* the number of attempts is capped too. Nothing in this project retries forever —
  a message that cannot be delivered becomes visible in `/status` instead of
  quietly cycling.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

MS_PER_SECOND = 1000


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    initial_ms: int = 1_000
    max_ms: int = 300_000  # five minutes
    factor: float = 2.0
    max_attempts: int = 12
    # Without jitter every queue in the process retries in lockstep after a
    # network blip, which is the shape of a self-inflicted rate limit.
    jitter: float = 0.2

    def delay_ms(self, attempt: int, *, rng: random.Random | None = None) -> int:
        """Delay before attempt number `attempt` (1 = the first retry)."""
        step = self.initial_ms * (self.factor ** max(0, attempt - 1))
        capped = min(step, float(self.max_ms))
        spread = capped * self.jitter
        source = rng or random
        return int(capped - spread + source.random() * 2 * spread)

    def exhausted(self, attempts: int) -> bool:
        return attempts >= self.max_attempts


DEFAULT_POLICY = BackoffPolicy()
