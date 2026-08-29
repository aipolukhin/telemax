"""Answering MAX's login prompts with messages typed into the guardian chat.

PyMax asks for the SMS code and the 2FA password through two providers it calls
from inside `client.start()`. Those providers are the whole seam: the console
implementation blocks on `input()`, and this one blocks on a future that a
Telegram handler resolves. Nothing else about the login changes.

The gateway holds a credential for exactly as long as it takes to hand it to
PyMax. It is never written to the state file, never logged, and never echoed —
the guardian deletes the message it came in on. What it cannot promise is that
the value never existed elsewhere: it travelled through Telegram, and saying
otherwise would be a lie the owner might rely on.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)

#: How long a question may stay unanswered before the login attempt is dropped.
#: A MAX code expires long before this; the limit exists so an abandoned attempt
#: does not hold a socket open forever.
PROMPT_TIMEOUT_SECONDS = 600.0


class Question(StrEnum):
    CODE = "code"
    PASSWORD = "password"  # noqa: S105 - the name of a question, not one


class AuthAbandoned(Exception):  # noqa: N818 - reads as the outcome it is
    """Nobody answered, or the owner cancelled. Ends the login attempt."""


@dataclass(frozen=True, slots=True)
class Prompt:
    """What to show the owner. `attempt` is 1 the first time, 2 after a refusal."""

    kind: Question
    attempt: int
    hint: str | None = None


class MaxAuthGateway:
    """A `SmsCodeProvider` and a `PasswordProvider` fed from a chat."""

    def __init__(
        self,
        ask: Callable[[Prompt], Awaitable[None]],
        *,
        timeout: float = PROMPT_TIMEOUT_SECONDS,
    ) -> None:
        self._ask = ask
        self._timeout = timeout
        self._pending: asyncio.Future[str] | None = None
        self._question: Question | None = None
        self._attempts: dict[Question, int] = {}

    # ------------------------------------------------------- the PyMax contract

    async def get_code(self, phone: str) -> str:
        """Called by PyMax once it has asked MAX to send a code."""
        return await self._collect(Question.CODE)

    async def get_password(self, hint: str | None = None) -> str:
        """Called only when the account has a second factor.

        PyMax retries this in a loop on a wrong password, which is exactly the
        behaviour wanted here: the owner corrects one value instead of starting
        the whole login again.
        """
        return await self._collect(Question.PASSWORD, hint=hint)

    # ----------------------------------------------------------- the chat side

    @property
    def waiting_for(self) -> Question | None:
        return self._question

    def submit(self, value: str) -> bool:
        """Hand an answer to whoever is waiting. False when nobody is."""
        pending, self._pending = self._pending, None
        self._question = None
        if pending is None or pending.done():
            return False
        pending.set_result(value.strip())
        return True

    def abandon(self, reason: str = "cancelled") -> None:
        """Fail the outstanding question, so `client.start()` unwinds."""
        pending, self._pending = self._pending, None
        self._question = None
        if pending is not None and not pending.done():
            pending.set_exception(AuthAbandoned(reason))

    # ---------------------------------------------------------------- plumbing

    async def _collect(self, question: Question, *, hint: str | None = None) -> str:
        attempt = self._attempts.get(question, 0) + 1
        self._attempts[question] = attempt

        loop = asyncio.get_running_loop()
        pending: asyncio.Future[str] = loop.create_future()
        self._pending = pending
        self._question = question

        # Ask after the future exists: an answer that arrives improbably fast
        # must have somewhere to land.
        await self._ask(Prompt(kind=question, attempt=attempt, hint=hint))

        try:
            return await asyncio.wait_for(pending, timeout=self._timeout)
        except TimeoutError as error:
            self._pending = None
            self._question = None
            raise AuthAbandoned("no answer in time") from error
