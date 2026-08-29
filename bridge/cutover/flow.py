"""`python -m bridge cutover-v2` — one irreversible move, behind two gates.

The shape follows from one fact: **the console cannot create a managed bot.**
Creation needs the manager bot to be polling and the owner to press Create in
Telegram's own dialog, so it belongs to the running guardian and nowhere else.

So this command does the half that is destructive and local, and hands the half
that is remote and creative to the guardian:

    preflight (read only)
      → GATE 1: the destructive inventory and the backup, then stop
      → backup, stop the service, purge, write the history floors, start
      → GATE 2: the exact V2 usernames, then stop
      → the owner creates them in the guardian, through the ordinary provisioning flow
        lifecycle, which already computes V2 names

Both gates are refusals by default. Without `--yes-destroy` nothing is deleted;
without `--yes-create` nothing is said about creating. The flags are separate
because the two decisions are separate: one loses local history, the other
spends slots on bots that cannot be deleted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)

GATE_ONE = """
────────────────────────────────────────────────────────────────────────
GATE 1 — необратимая очистка локального состояния

Всё перечисленное будет удалено из активной базы. Бэкап снят и проверен.

Откат — три команды, руками, и это намеренно: восстановление затирает
активную базу, и такому шагу нужен человек, а не флаг.

    systemctl --user stop telemax
    cp {database} data/bridge.db
    tar xzf {archive} -C .   # data/secrets, data/state, data/max-session, config, .env
    systemctl --user start telemax

Продолжить очистку: python -m bridge cutover-v2 --yes-destroy
────────────────────────────────────────────────────────────────────────
"""

GATE_TWO = """
────────────────────────────────────────────────────────────────────────
GATE 2 — необратимое создание managed-ботов

Managed-бота НЕЛЬЗЯ удалить: `deleteManagedBot` не существует. Каждый
занимает один слот из двадцати, навсегда.

Создание идёт в чате со стражем: /dialogs → выбрать контакты → «Готово».
Имена вычисляются по V2 и совпадут с показанными выше.
────────────────────────────────────────────────────────────────────────
"""

RETIRED = """
СТАРЫЕ БОТЫ ОСТАЮТСЯ В TELEGRAM.

Telemax не умеет их удалять — в Bot API нет `deleteManagedBot`, и притворяться
здесь было бы ложью. Они помечены RETIRED, ни один процесс их больше не
опрашивает, их токены забыты.

Удалить вручную: @BotFather → /mybots → выбрать → Delete Bot.
"""


class Phase(StrEnum):
    """Where a run got to. Written down so a failure names its own state."""

    PREFLIGHT = "preflight"
    BACKED_UP = "backed_up"
    STOPPED = "stopped"
    PURGED = "purged"
    FLOORED = "floored"
    STARTED = "started"
    DONE = "done"


@dataclass(frozen=True, slots=True)
class Outcome:
    phase: Phase
    detail: str = ""

    @property
    def complete(self) -> bool:
        return self.phase is Phase.DONE


def phase_file(data_dir: Path) -> Path:
    return data_dir / "state" / "cutover-v2.phase"


def record(data_dir: Path, phase: Phase, detail: str = "") -> None:
    """One line on disk saying how far this got.

    A cutover that dies halfway must not be guessed at afterwards. There is no
    resume: the phase is for a person deciding between finishing by hand and
    rolling back.
    """
    from bridge.config.writer import atomic_write_text

    atomic_write_text(phase_file(data_dir), f"{phase.value}\n{detail}\n")


def read_phase(data_dir: Path) -> Outcome | None:
    try:
        lines = phase_file(data_dir).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if not lines:
        return None
    try:
        return Outcome(phase=Phase(lines[0].strip()), detail=lines[1] if len(lines) > 1 else "")
    except ValueError:
        return None
