"""The MAX onboarding state machine.

Explicit states rather than a pile of handlers, for one reason: this
conversation can be interrupted at any point — by a wrong code, by a restart, by
the owner walking away — and "which question is outstanding" has to be a value
somebody can read, not something inferred from which handler happened to be
registered.

Three rules the code enforces:

* **a local failure rewinds one step, not the whole flow.** A wrong 2FA password
  asks for the password again; it does not throw away the code that worked.
* **nothing is confirmed twice.** A valid MAX session *is* the confirmation, so
  the configuration is written and the bridge started without another question.
* **credentials live in a future, never in the store.** After a restart the step
  is remembered; the value that was being typed is not, and cannot be.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from bridge.phone import mask as mask_phone
from bridge.phone import normalize as normalize_phone

from . import screens
from .maxauth import AuthAbandoned, MaxAuthGateway, Prompt, Question
from .screens import Screen
from .state import Stage, StateStore, Step

logger = logging.getLogger(__name__)

#: Runs the whole MAX login with the given phone, answering prompts through the
#: gateway. Returns when the session is valid; raises otherwise.
Connect = Callable[[str, MaxAuthGateway], Awaitable[None]]

#: Writes the phone and the finished configuration, atomically.
Persist = Callable[[str], Awaitable[None]]

#: Brings the bridge worker up on the running process.
Launch = Callable[[], Awaitable[None]]

#: Draws a screen: the guardian edits one message rather than sending many.
Show = Callable[[Screen], Awaitable[None]]

#: Reads the configured timezone, or None while it has not been chosen. Not a
#: stored value on this object: the config is the source of truth and it can be
#: rewritten under a running process.
TimezoneReader = Callable[[], str | None]

#: Writes an IANA timezone into the config, atomically, and reloads it.
TimezoneWriter = Callable[[str], Awaitable[None]]

#: The number the console logged Telegram in with, so MAX is not asked for one
#: the owner has already typed. Returns None when it cannot be read.
PhoneReader = Callable[[], str | None]


class MaxLoginError(Exception):
    """MAX refused the login. The message is short, sanitised and for a person."""


def _sanitise(error: BaseException) -> str:
    """A sentence, never a traceback, never a payload.

    Exception text from a protocol library can carry a phone number or a frame
    dump; the owner needs to know what to do next, and the log has the rest.
    """
    text = str(error).strip() or type(error).__name__
    lowered = text.lower()
    if "no token received" in lowered or "authentication failed" in lowered:
        return "MAX не принял код или пароль."
    if "timed out" in lowered or "timeout" in lowered:
        return "MAX не ответил вовремя."
    if "connect" in lowered or "network" in lowered or "socket" in lowered:
        return "MAX временно недоступен."
    return text[:120]


class MaxOnboarding:
    """Drives one owner through connecting MAX, and then gets out of the way."""

    def __init__(
        self,
        *,
        store: StateStore,
        show: Show,
        connect: Connect,
        persist: Persist,
        launch: Launch,
        timezone: str | None = None,
        timezone_reader: TimezoneReader | None = None,
        timezone_writer: TimezoneWriter | None = None,
        telegram_phone: PhoneReader | None = None,
    ) -> None:
        self._store = store
        self._show = show
        self._connect = connect
        self._persist = persist
        self._launch = launch
        self._timezone = timezone
        self._timezone_reader = timezone_reader
        self._timezone_writer = timezone_writer
        self._telegram_phone = telegram_phone or (lambda: None)

        self._gateway: MaxAuthGateway | None = None
        self._attempt: asyncio.Task[None] | None = None

    @property
    def timezone(self) -> str | None:
        """Whatever the config says right now, not what it said at start-up."""
        if self._timezone_reader is not None:
            return self._timezone_reader()
        return self._timezone

    # ------------------------------------------------------------------ status

    @property
    def step(self) -> Step:
        return self._store.load().step

    @property
    def finished(self) -> bool:
        return self._store.load().stage is Stage.BRIDGE_RUNNING

    @property
    def expects_secret(self) -> bool:
        """True while the owner's next message must not stay in the chat.

        A code and a 2FA password are the obvious cases; a phone number is here
        too. It is not a credential, but it identifies a person, and a chat
        history is exactly where it should not sit forever.
        """
        return self.step in {
            Step.WAITING_FOR_PHONE,
            Step.WAITING_FOR_CODE,
            Step.WAITING_FOR_2FA,
        }

    @property
    def expects_text(self) -> bool:
        return self.step in {
            Step.WAITING_FOR_PHONE,
            Step.WAITING_FOR_CODE,
            Step.WAITING_FOR_2FA,
        }

    # -------------------------------------------------------------- entry points

    async def offer(self) -> None:
        """First screen after `/start <token>`, or after a restart mid-flow."""
        record = self._store.load()
        if self.timezone is None:
            # Before the stage is even looked at: an unset zone is an unanswered
            # question whether or not the bridge is already carrying messages.
            # Everything after it produces timestamps, and a wrong zone is
            # invisible until somebody notices every message is an hour out.
            await self._ask_timezone()
            return
        if record.stage is Stage.BRIDGE_RUNNING:
            await self._show(screens.ready(timezone=self.timezone))
            return
        if record.step in {Step.WAITING_FOR_PHONE, Step.WAITING_FOR_CODE, Step.WAITING_FOR_2FA}:
            await self._show(screens.resume_offer())
            return
        await self._show(screens.welcome())

    async def begin(self) -> None:
        """The owner pressed «Начать подключение»."""
        await self._abandon_attempt()
        if self.timezone is None:
            await self._ask_timezone()
            return
        await self._ask_for_a_number()

    # ---------------------------------------------------------------- timezone

    async def _ask_timezone(self) -> None:
        """Ask, without pretending the deployment is less far along than it is.

        `Stage` only ever moves forward: rewinding a running bridge to
        MAX_ONBOARDING_PENDING because a zone is missing would make the answer
        lead back into the phone question, for an account already logged in.
        """
        if self._store.load().stage is not Stage.BRIDGE_RUNNING:
            self._store.update(
                stage=Stage.MAX_ONBOARDING_PENDING, step=Step.WAITING_FOR_TIMEZONE
            )
        else:
            self._store.update(step=Step.WAITING_FOR_TIMEZONE)
        await self._show(screens.timezone_offer())

    async def detect_timezone(self) -> None:
        """«Определить автоматически».

        The honest automatic answer available to a bot with no web surface: the
        host's own zone, which on a personal server the owner set themselves. A
        Mini App could read the *phone's* zone, but hosting one needs an HTTPS
        origin this project does not have, and a guess would be worse than a
        list — a wrong zone is invisible until every timestamp is an hour out.
        """
        from bridge.bootstrap.timezones import city_of, detect_system_timezone, offset_label

        detected = detect_system_timezone()
        if detected is None:
            await self._show(screens.timezone_undetectable())
            return
        await self._show(
            screens.confirm_timezone(
                detected, offset_label(detected), city=city_of(detected)
            )
        )

    async def offer_timezone_list(self) -> None:
        from bridge.bootstrap.timezones import RUSSIAN_TIMEZONES, offset_label

        choices = [
            (f"{city} · {offset_label(name)}", name) for city, name in RUSSIAN_TIMEZONES
        ]
        await self._show(screens.timezone_list(choices))

    async def set_timezone(self, name: str) -> bool:
        """Store an IANA name, having proved `zoneinfo` can build it.

        False means the value was refused. A tap carrying something `zoneinfo`
        does not know is either a stale button or a tampered callback, and
        writing it would make the config fail to load at the next restart.
        """
        from bridge.bootstrap.timezones import is_known_timezone

        if not is_known_timezone(name):
            logger.warning("refused an unknown timezone from a callback")
            await self._show(screens.timezone_undetectable())
            return False

        if self._timezone_writer is not None:
            try:
                await self._timezone_writer(name)
            except Exception as error:
                logger.exception("could not write the timezone")
                await self._show(screens.launch_failed(_sanitise(error)))
                return False
        self._timezone = name

        if self._store.load().stage is Stage.BRIDGE_RUNNING:
            await self._show(screens.ready(timezone=name))
            return True
        await self._ask_for_a_number()
        return True

    # ------------------------------------------------------------------- phone

    async def _ask_for_a_number(self) -> None:
        """Offer the Telegram number for MAX, or ask outright when there is none."""
        existing = self._telegram_phone()
        if not existing:
            self._store.update(stage=Stage.MAX_ONBOARDING_PENDING, step=Step.WAITING_FOR_PHONE)
            await self._show(screens.ask_max_phone())
            return
        self._store.update(
            stage=Stage.MAX_ONBOARDING_PENDING, step=Step.WAITING_FOR_PHONE_CHOICE
        )
        await self._show(screens.offer_telegram_phone(mask_phone(existing)))

    async def use_telegram_phone(self) -> bool:
        """«Использовать этот номер» — no second prompt, no second normalisation."""
        existing = self._telegram_phone()
        phone = normalize_phone(existing or "")
        if phone is None:
            self._store.update(step=Step.WAITING_FOR_PHONE)
            await self._show(screens.ask_max_phone())
            return False
        self._store.update(max_phone_mode="same")
        await self._start_login(phone)
        return True

    async def ask_other_phone(self) -> None:
        self._store.update(step=Step.WAITING_FOR_PHONE, max_phone_mode="other")
        await self._show(screens.ask_max_phone())

    async def cancel(self) -> None:
        await self._abandon_attempt()
        self._store.update(step=Step.IDLE)
        await self._show(screens.cancelled())

    async def retry(self) -> None:
        """Start the login again from the phone. MAX has no resend of its own:
        a fresh code needs a fresh request, which is what a new attempt is."""
        await self.begin()

    async def retry_launch(self) -> None:
        """The session is fine and only the bridge failed — try just that."""
        await self._finish()

    async def submit(self, text: str) -> bool:
        """Route a message from the owner. False when nothing was waiting."""
        step = self.step
        if step is Step.WAITING_FOR_PHONE:
            return await self._submit_phone(text)
        if step in {Step.WAITING_FOR_CODE, Step.WAITING_FOR_2FA}:
            gateway = self._gateway
            if gateway is None:
                # The attempt died while the owner was typing.
                await self._show(screens.login_failed("Попытка входа прервалась."))
                return True
            if not gateway.submit(text):
                return False
            self._store.update(step=Step.VALIDATING)
            await self._show(screens.validating())
            return True
        return False

    async def shutdown(self) -> None:
        await self._abandon_attempt()

    # ------------------------------------------------------------------ the flow

    async def _submit_phone(self, text: str) -> bool:
        phone = normalize_phone(text)
        if phone is None:
            await self._show(screens.ask_max_phone())
            return True
        await self._start_login(phone)
        return True

    async def _start_login(self, phone: str) -> None:
        """One entry into the MAX login, whichever number it came from."""
        gateway = MaxAuthGateway(self._on_prompt)
        self._gateway = gateway
        await self._show(screens.sending_code())
        self._attempt = asyncio.create_task(self._login(phone, gateway), name="max-onboarding")

    async def _on_prompt(self, prompt: Prompt) -> None:
        """PyMax needs something. Ask for it, and remember that we did."""
        if prompt.kind is Question.CODE:
            self._store.update(step=Step.WAITING_FOR_CODE)
            await self._show(screens.ask_code(again=prompt.attempt > 1))
            return
        self._store.update(step=Step.WAITING_FOR_2FA)
        await self._show(screens.ask_password(again=prompt.attempt > 1, hint=prompt.hint))

    async def _login(self, phone: str, gateway: MaxAuthGateway) -> None:
        try:
            await self._connect(phone, gateway)
        except asyncio.CancelledError:
            raise
        except AuthAbandoned:
            # Either the owner cancelled or nobody answered; both already have
            # a screen, or will get one from cancel().
            logger.info("MAX onboarding attempt was abandoned")
            return
        except Exception as error:  # noqa: BLE001 - every failure is the owner's to see
            logger.warning("MAX onboarding login failed: %s", type(error).__name__)
            self._store.update(step=Step.WAITING_FOR_PHONE)
            await self._show(screens.login_failed(_sanitise(error)))
            return
        finally:
            self._gateway = None

        # A valid session is the confirmation. Nothing else is asked.
        self._store.update(stage=Stage.MAX_SESSION_VALID, step=Step.SAVING)
        await self._show(screens.saving())
        try:
            await self._persist(phone)
        except Exception as error:
            logger.exception("could not write the configuration after MAX login")
            await self._show(screens.launch_failed(_sanitise(error)))
            return

        await self._finish()

    async def _finish(self) -> None:
        self._store.update(step=Step.STARTING_BRIDGE)
        await self._show(screens.starting())
        try:
            await self._launch()
        except Exception as error:
            logger.exception("the bridge worker did not start after onboarding")
            await self._show(screens.launch_failed(_sanitise(error)))
            return

        self._store.update(stage=Stage.BRIDGE_RUNNING, step=Step.COMPLETED)
        await self._show(screens.ready(timezone=self.timezone))

    async def _abandon_attempt(self) -> None:
        gateway, self._gateway = self._gateway, None
        if gateway is not None:
            gateway.abandon()

        attempt, self._attempt = self._attempt, None
        if attempt is not None and not attempt.done():
            attempt.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await attempt
