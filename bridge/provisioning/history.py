"""Pulling the conversation that existed before the bridge did.

Never automatic, and never all of it. A MAX dialog can be years long, copying it
into Telegram is not obviously what the owner wants, and the only person who
knows is the owner — so the guardian asks, and this runs on a yes.

Two properties make asking twice safe. Delivery already dedups on the MAX
message id, so a second import writes nothing; and each bridge remembers the
newest id it has seen, so «Импортировать только новые» is a real option rather
than a re-run that quietly does the same work. The cursor is stored per bridge
and survives a restart, which is also what lets an import interrupted halfway
carry on instead of starting from the top.

One chat's failure never stops the others: a contact whose history MAX refuses
to serve is reported on its own line, and the rest of the import continues.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bridge.telegram.design import BROKEN, BUSY, DONE, TODO, esc, title

logger = logging.getLogger(__name__)

#: `onboarding.screens.BRIDGES` and `.MENU`, by value: `screens` imports the
#: provisioning package for the buttons that open it, and importing back would
#: be a cycle. `tests/test_guardian_dead_ends.py` asserts the two agree.
BRIDGES = "onb:bridges"
HOME = "onb:menu"

#: What "the recent part" means. A dialog can be years long; this is the tail.
DEFAULT_LIMIT = 50

#: What a *re-pull* means: everything MAX will serve, not a tail. The chat has
#: just been emptied, and bringing back a slice of what was removed would be a
#: deletion dressed as a refresh.
#:
#: `None` reaches `fetch_history` as "no limit" and pages until MAX stops giving
#: anything new. It was a number for one deploy, and the number did nothing:
#: MAX's `backward` was never passed, so a thousand and fifty both came back as
#: one default page of forty.
REPULL_LIMIT: int | None = None

PROMPT = (
    "Подтянуть предыдущую переписку из MAX?\n\n"
    f"Будут импортированы последние {DEFAULT_LIMIT} сообщений "
    "для каждого созданного моста."
)

ALREADY_IMPORTED = "История уже импортирована."


@dataclass(frozen=True, slots=True)
class ImportTarget:
    """One healthy bridge, and what to call it on screen."""

    max_chat_id: int
    title: str
    bridge_name: str


@dataclass(frozen=True, slots=True)
class ChatImport:
    """What one chat's import did."""

    delivered: int
    cursor: int | None = None


@dataclass(slots=True)
class ImportReport:
    """One line of the progress display, updated in place as it moves."""

    target: ImportTarget
    delivered: int = 0
    total: int | None = None
    done: bool = False
    error: str | None = None


#: `(done, total)` while a chat is being walked, so the display can count.
ChatProgress = Callable[[int, int], None]


class HistorySource(Protocol):
    """Where the old messages come from, and where they are delivered to."""

    async def import_chat(
        self,
        max_chat_id: int,
        *,
        limit: int | None,
        after: int | None,
        on_progress: ChatProgress | None = None,
    ) -> ChatImport: ...


class CursorStore(Protocol):
    """Per-bridge watermark of the newest MAX message already imported."""

    async def history_cursor(self, bridge_name: str) -> int | None: ...

    async def set_history_cursor(self, bridge_name: str, cursor: int) -> None: ...


#: Called after every change, so one message is edited rather than many sent.
Progress = Callable[[list[ImportReport]], Awaitable[None]]


def progress_text(reports: list[ImportReport], *, limit: int = DEFAULT_LIMIT) -> str:
    lines = []
    for report in reports:
        name = esc(report.target.title)
        if report.error:
            lines.append(f"{BROKEN} {name} — {esc(report.error)}")
        elif report.done:
            lines.append(f"{DONE} {name} · {report.delivered} сообщений")
        elif report.total is not None:
            lines.append(f"{BUSY} {name} · {report.delivered} из {report.total}")
        elif report.delivered or report.total == 0:
            lines.append(f"{BUSY} {name} · {report.delivered} из {limit}")
        else:
            lines.append(f"{TODO} {name}")
    return title("Импорт истории", *lines)


def summary_text(reports: list[ImportReport]) -> str:
    lines = []
    for report in reports:
        name = esc(report.target.title)
        if report.error:
            lines.append(f"{BROKEN} {name} — {esc(report.error)}")
        else:
            lines.append(f"{DONE} {name} · {report.delivered}")
    return title("История импортирована", *lines)


def summary_markup() -> InlineKeyboardMarkup:
    """The way on from the last frame of an import.

    There was none. `summary_text` was drawn with `reply_markup=None`, so the
    guardian's one message ended on a list of counts and no buttons, and the
    only escape was typing `/menu` — which nothing on the screen said.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Мосты", callback_data=BRIDGES)],
            [InlineKeyboardButton(text="← Панель", callback_data=HOME)],
        ]
    )


class HistoryImporter:
    """Walks a list of healthy bridges, pulling each one's tail."""

    def __init__(
        self,
        *,
        source: HistorySource,
        cursors: CursorStore,
        limit: int | None = DEFAULT_LIMIT,
        progress: Progress | None = None,
    ) -> None:
        self._source = source
        self._cursors = cursors
        self._limit = limit
        self._progress = progress

    async def run(
        self, targets: list[ImportTarget], *, only_new: bool = False
    ) -> list[ImportReport]:
        reports = [ImportReport(target=target) for target in targets]
        await self._draw(reports)

        for report in reports:
            after = await self._after(report.target, only_new=only_new)
            try:
                outcome = await self._one(report, after=after)
            except Exception as error:  # noqa: BLE001 - one chat, not the batch
                logger.warning(
                    "could not import history for %s: %s",
                    report.target.bridge_name,
                    type(error).__name__,
                )
                report.error = "не удалось прочитать историю"
                await self._draw(reports)
                continue

            report.delivered = outcome.delivered
            report.done = True
            if outcome.cursor is not None:
                # Written even when nothing was delivered: the cursor is about
                # what has been *seen*, not about what was new.
                await self._cursors.set_history_cursor(
                    report.target.bridge_name, outcome.cursor
                )
            await self._draw(reports)

        return reports

    async def already_imported(self, targets: list[ImportTarget]) -> bool:
        """True when every target already has a cursor — asked before importing."""
        for target in targets:
            if await self._cursors.history_cursor(target.bridge_name) is None:
                return False
        return bool(targets)

    async def _after(self, target: ImportTarget, *, only_new: bool) -> int | None:
        if not only_new:
            return None
        return await self._cursors.history_cursor(target.bridge_name)

    async def _one(self, report: ImportReport, *, after: int | None) -> ChatImport:
        def note(done: int, total: int) -> None:
            report.delivered = done
            report.total = total

        return await self._source.import_chat(
            report.target.max_chat_id,
            limit=self._limit,
            after=after,
            on_progress=note,
        )

    async def _draw(self, reports: list[ImportReport]) -> None:
        if self._progress is None:
            return
        try:
            await self._progress(reports)
        except Exception:
            logger.debug("could not draw the import progress", exc_info=True)
