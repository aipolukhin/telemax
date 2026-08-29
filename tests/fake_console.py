"""A console that answers from a script and remembers everything it printed.

The point of most of these tests is *what setup did not do* — did not ask about
MAX, did not print a credential, did not leave a config behind after a Ctrl+C —
and all three are questions about this transcript.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from bridge.bootstrap.ui import SetupCancelled


@dataclass
class FakeUi:
    """Duck-compatible with `bridge.bootstrap.ui.Ui`."""

    #: Answers by question text, matched on a prefix so tests stay readable.
    answers: dict[str, str] = field(default_factory=dict)
    #: Which value `select` should return; None means the first choice.
    selects: dict[str, str] = field(default_factory=dict)
    #: Questions that should raise, as if the owner pressed Ctrl+C.
    cancel_on: set[str] = field(default_factory=set)
    #: Whether prompts are available; an optional step skips when this is False.
    interactive: bool = True

    printed: list[str] = field(default_factory=list)
    asked: list[str] = field(default_factory=list)
    hidden: list[str] = field(default_factory=list)
    choices_shown: list[list[tuple[str, str]]] = field(default_factory=list)

    # ------------------------------------------------------------------ output

    def banner(self) -> None:
        self.printed.append("<banner>")

    def step(self, number: int, total: int, title: str) -> None:
        self.printed.append(f"Шаг {number} из {total} · {title}")

    def ok(self, text: str) -> None:
        self.printed.append(f"✓ {text}")

    def warn(self, text: str) -> None:
        self.printed.append(f"! {text}")

    def fail(self, text: str) -> None:
        self.printed.append(f"✗ {text}")

    def note(self, text: str) -> None:
        self.printed.append(text)

    def panel(self, title: str) -> None:
        self.printed.append(f"[{title}]")

    def plain(self, text: str = "") -> None:
        self.printed.append(text)

    @contextmanager
    def working(self, text: str) -> Iterator[None]:
        self.printed.append(f"… {text}")
        yield

    # ------------------------------------------------------------------- input

    async def select(self, question: str, choices: list[tuple[str, str]]) -> str:
        self.asked.append(question)
        self.choices_shown.append(choices)
        self._maybe_cancel(question)
        return self.selects.get(question, choices[0][1])

    async def ask(
        self, question: str, *, validate: Callable[[str], bool | str] | None = None
    ) -> str:
        self.asked.append(question)
        self._maybe_cancel(question)
        return self._answer(question, validate)

    async def secret(
        self, question: str, *, validate: Callable[[str], bool | str] | None = None
    ) -> str:
        self.asked.append(question)
        self.hidden.append(question)
        self._maybe_cancel(question)
        return self._answer(question, validate)

    async def confirm(self, question: str, *, default: bool = True) -> bool:
        self.asked.append(question)
        self._maybe_cancel(question)
        return default

    # ---------------------------------------------------------------- plumbing

    def _maybe_cancel(self, question: str) -> None:
        for prefix in self.cancel_on:
            if question.startswith(prefix):
                raise SetupCancelled

    def _answer(
        self, question: str, validate: Callable[[str], bool | str] | None
    ) -> str:
        for prefix, value in self.answers.items():
            if question.startswith(prefix):
                if validate is not None:
                    verdict = validate(value)
                    if verdict is not True:
                        raise AssertionError(f"scripted answer rejected: {verdict}")
                return value
        raise AssertionError(f"setup asked something unscripted: {question!r}")

    # ------------------------------------------------------------- assertions

    @property
    def transcript(self) -> str:
        return "\n".join(self.printed)
