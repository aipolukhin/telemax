"""The console surface of `setup`, and the only place that knows about a screen.

Everything here is shaped by one assumption: the person running this is on a
phone, over SSH, in a terminal about 45 columns wide. That rules out tables,
side-by-side layouts and long lines, and it is why each step prints three or
four short lines and then gets out of the way.

Three behaviours are load-bearing rather than decorative:

* **Ctrl+C is an answer, not a crash.** `questionary` returns None when the
  prompt is interrupted; every wrapper here turns that into `SetupCancelled`, so
  the caller can roll back and the owner never sees a traceback.
* **a failed answer costs one line, not a screen.** Validation happens inside
  the prompt, so a mistyped api_id is corrected in place instead of leaving a
  scrollback full of red.
* **prompts are awaited, not run.** `questionary`'s `.ask()` starts its own
  event loop, which raises `asyncio.run() cannot be called from a running event
  loop` the moment it is used inside an async setup — so everything here uses
  `ask_async`. The spinner is stopped for the duration of a prompt, because Rich
  and prompt_toolkit both want to own the cursor.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import questionary
from rich.console import Console
from rich.panel import Panel
from rich.status import Status
from rich.text import Text

from bridge.phone import normalize as normalize_phone

#: Wide enough for `Екатеринбург — Asia/Yekaterinburg (UTC+05:00)`, narrow
#: enough for a phone in landscape.
WIDTH = 46


class SetupCancelled(Exception):  # noqa: N818 - a decision, not an error
    """The owner interrupted setup. Not an error — a decision."""


def valid_api_id(value: str) -> bool | str:
    text = value.strip()
    if not text.isdigit() or int(text) <= 0:
        return "api_id — это число с my.telegram.org, например 1234567."
    return True


def valid_phone(value: str) -> bool | str:
    # Same rule as the bot uses, so a number accepted here is accepted there.
    if normalize_phone(value) is None:
        return "Номер в международном виде: +79001234567."
    return True


def valid_nonempty(value: str) -> bool | str:
    return True if value.strip() else "Пустой ответ не подойдёт."


class Ui:
    """Everything `setup` prints or asks.

    `interactive=False` is for tests and for a non-tty: the prompts refuse
    rather than block forever on a pipe.
    """

    def __init__(self, console: Console | None = None, *, interactive: bool = True) -> None:
        self._console = console or Console(width=WIDTH, highlight=False)
        self._interactive = interactive
        self._status: Status | None = None

    @property
    def interactive(self) -> bool:
        """Whether prompts can be shown, so an optional step can skip on a pipe."""
        return self._interactive

    # ------------------------------------------------------------------ output

    def banner(self) -> None:
        title = Text("TELEMAX SETUP", style="bold")
        title.append("\nМост Telegram ↔ MAX", style="dim")
        title.justify = "center"
        self._console.print(Panel(title, width=WIDTH, border_style="cyan"))

    def step(self, number: int, total: int, title: str) -> None:
        self._console.print()
        self._console.print(f"[bold]Шаг {number} из {total}[/bold] · {title}")
        self._console.print()

    def ok(self, text: str) -> None:
        self._console.print(f"[green]✓[/green] {text}")

    def warn(self, text: str) -> None:
        self._console.print(f"[yellow]![/yellow] {text}")

    def fail(self, text: str) -> None:
        self._console.print(f"[red]✗[/red] {text}")

    def note(self, text: str) -> None:
        self._console.print(f"[dim]{text}[/dim]")

    def panel(self, title: str) -> None:
        heading = Text(title, style="bold")
        heading.justify = "center"
        self._console.print()
        self._console.print(Panel(heading, width=WIDTH, border_style="green"))
        self._console.print()

    def plain(self, text: str = "") -> None:
        self._console.print(text)

    @contextmanager
    def working(self, text: str) -> Iterator[None]:
        """A spinner for the seconds a network call takes.

        Without it the screen simply stops for ten seconds during `sign_in`,
        which reads as a hang.
        """
        if not self._interactive:
            yield
            return
        status = self._console.status(f"[cyan]{text}[/cyan]", spinner="dots")
        status.start()
        self._status = status
        try:
            yield
        finally:
            self._status = None
            status.stop()

    # ------------------------------------------------------------------- input

    async def select(self, question: str, choices: list[tuple[str, str]]) -> str:
        """Pick one of `choices`, given as `(label, value)`. First is default."""
        options = [questionary.Choice(title=label, value=value) for label, value in choices]
        return str(
            await self._prompt(
                questionary.select,
                question,
                choices=options,
                default=options[0],
                instruction=" ",
            )
        )

    async def ask(
        self, question: str, *, validate: Callable[[str], bool | str] | None = None
    ) -> str:
        answer = await self._prompt(questionary.text, question, validate=validate)
        return str(answer).strip()

    async def secret(
        self, question: str, *, validate: Callable[[str], bool | str] | None = None
    ) -> str:
        """Hidden input. The value is never echoed back, here or later."""
        answer = await self._prompt(questionary.password, question, validate=validate)
        return str(answer).strip()

    async def confirm(self, question: str, *, default: bool = True) -> bool:
        return bool(await self._prompt(questionary.confirm, question, default=default))

    async def _prompt(self, factory: Any, question: str, **kwargs: Any) -> Any:
        if not self._interactive:
            raise SetupCancelled("setup needs a terminal it can ask questions in")

        # Rich is drawing a spinner on the same line prompt_toolkit is about to
        # take over. One of them has to let go.
        status, self._status = self._status, None
        if status is not None:
            status.stop()
        try:
            # `kbi_msg=""`: the caller says what a cancellation means here, and
            # questionary's own "Cancelled by user" arrives before it.
            answer = await factory(question, qmark="?", **kwargs).ask_async(kbi_msg="")
        except (EOFError, KeyboardInterrupt) as error:
            raise SetupCancelled from error
        finally:
            if status is not None:
                status.start()
                self._status = status

        if answer is None:
            raise SetupCancelled
        return answer
