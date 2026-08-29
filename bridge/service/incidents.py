"""The four things an owner can do about a stuck message, and nothing more.

Guardian is a control plane, not an admin panel. What it owes the owner here is
narrow: see what did not arrive, see what might not have arrived, push one of
them again, or say "I checked, it is there". Anything past that belongs in
`/status` or in the runbook.

No message content appears in any of these answers. A job is named by its id,
its contact and why it stopped — never by what somebody wrote.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from bridge.storage import OutboxItem, OutboxRepository, OutboxState

logger = logging.getLogger(__name__)

NOTHING_FAILED = "Недоставленных сообщений нет."
NOTHING_AMBIGUOUS = "Неясных отправок нет."
NOTHING_ARCHIVED = "Архив пуст."
NEEDS_ID = "Укажите номер задачи, например: /retry 42"
UNKNOWN_JOB = "Такой задачи нет, либо её уже нельзя повторить."
RETRY_QUEUED = "Задача {job_id} снова в очереди."
RESOLVED_OK = "Задача {job_id} помечена решённой."
ARCHIVED_OK = "Задача {job_id} убрана в архив. История сохранена."
NOT_ARCHIVABLE = "Задачу {job_id} нельзя архивировать: она не failed и не ambiguous."
NEEDS_REASON = "Укажите причину: /archive 4 почему это неисправимо"
NOTHING_UNFINISHED = "Незавершённых мостов нет."
NEEDS_CHAT = "Укажите MAX-чат, например: /provretry 300000006"
UNKNOWN_ATTEMPT = "Такой попытки нет."
RESUMING = "Продолжаю мост для чата {chat_id}…"
NOT_AMBIGUOUS = "Задача {job_id} не в неясном состоянии — отмечать нечего."

#: Kinds, in words the owner uses rather than the queue's.
KIND_NAMES = {
    "max_to_tg_text": "текст MAX → Telegram",
    "max_to_tg_media": "вложение MAX → Telegram",
    "tg_to_max_text": "текст Telegram → MAX",
    "tg_to_max_media": "вложение Telegram → MAX",
}


def describe(item: OutboxItem) -> str:
    """One job, in one line, with nothing private in it."""
    kind = KIND_NAMES.get(item.kind, item.kind)
    reason = (item.last_error or "причина не записана").split("\n")[0][:120]
    return f"#{item.id} · {item.bridge_name} · {kind}\n   {reason}"


#: Answered when the worker is down: the queue lives inside it.
NOT_RUNNING = "Мост не запущен — очередь недоступна."


def build_incidents_router(
    source: Any, bridges: list[str] | None = None
) -> Router:
    """Commands for the jobs that need a person.

    `source` is either an `OutboxRepository` or the `GuardianContext` holding
    one. The context form is what production uses: these handlers are registered
    once on the guardian's dispatcher and the worker under them is rebuilt on
    every restart.
    """
    router = Router(name="incidents")

    def _queue() -> tuple[Any, list[str]]:
        if isinstance(source, OutboxRepository):
            return source, list(bridges or [])
        return getattr(source, "outbox", None), list(getattr(source, "bridge_names", ()))

    async def _collect(state: OutboxState) -> list[OutboxItem] | None:
        outbox, names = _queue()
        if outbox is None:
            return None
        found: list[OutboxItem] = []
        for name in names:
            found.extend(
                item for item in await outbox.needing_attention(name) if item.state is state
            )
        return found

    @router.message(Command("failed"))
    async def _failed(message: Message) -> None:
        items = await _collect(OutboxState.FAILED)
        if items is None:
            await message.answer(NOT_RUNNING)
            return
        if not items:
            await message.answer(NOTHING_FAILED)
            return
        lines = "\n".join(describe(item) for item in items[:20])
        await message.answer(
            f"Не доставлено: {len(items)}\n\n{lines}\n\nПовторить: /retry НОМЕР"
        )

    @router.message(Command("ambiguous"))
    async def _ambiguous(message: Message) -> None:
        items = await _collect(OutboxState.AMBIGUOUS)
        if items is None:
            await message.answer(NOT_RUNNING)
            return
        if not items:
            await message.answer(NOTHING_AMBIGUOUS)
            return
        lines = "\n".join(describe(item) for item in items[:20])
        await message.answer(
            f"Неясный результат: {len(items)}\n\n{lines}\n\n"
            "Проверьте в MAX. Дошло — /resolved НОМЕР, нет — /retry НОМЕР."
        )

    @router.message(Command("retry"))
    async def _retry(message: Message) -> None:
        job_id = _id_from(message)
        if job_id is None:
            await message.answer(NEEDS_ID)
            return
        outbox, _ = _queue()
        if outbox is None:
            await message.answer(NOT_RUNNING)
            return
        if await outbox.retry_now(job_id):
            await message.answer(RETRY_QUEUED.format(job_id=job_id))
        else:
            await message.answer(UNKNOWN_JOB)

    @router.message(Command("resolved"))
    async def _resolved(message: Message) -> None:
        job_id = _id_from(message)
        if job_id is None:
            await message.answer(NEEDS_ID)
            return
        outbox, _ = _queue()
        if outbox is None:
            await message.answer(NOT_RUNNING)
            return
        if await outbox.resolve(job_id):
            await message.answer(RESOLVED_OK.format(job_id=job_id))
        else:
            await message.answer(NOT_AMBIGUOUS.format(job_id=job_id))

    @router.message(Command("archive"))
    async def _archive(message: Message) -> None:
        """Set a dead job aside. History is kept; the incident count is not."""
        job_id = _id_from(message)
        if job_id is None:
            await message.answer(NEEDS_ID)
            return
        parts = (message.text or "").split(maxsplit=2)
        reason = parts[2].strip() if len(parts) > 2 else ""
        if not reason:
            await message.answer(NEEDS_REASON)
            return
        outbox, _ = _queue()
        if outbox is None:
            await message.answer(NOT_RUNNING)
            return
        if await outbox.archive(job_id, reason=reason):
            await message.answer(ARCHIVED_OK.format(job_id=job_id))
        else:
            await message.answer(NOT_ARCHIVABLE.format(job_id=job_id))

    @router.message(Command("archived"))
    async def _archived(message: Message) -> None:
        outbox, names = _queue()
        if outbox is None:
            await message.answer(NOT_RUNNING)
            return
        items: list[OutboxItem] = []
        for name in names:
            items.extend(await outbox.archived(name))
        if not items:
            await message.answer(NOTHING_ARCHIVED)
            return
        lines = "\n".join(describe(item) for item in items[:20])
        await message.answer(f"В архиве: {len(items)}\n\n{lines}")

    # ------------------------------------------------- unfinished provisioning

    @router.message(Command("attempts"))
    async def _attempts(message: Message) -> None:
        """Bridges that were being made and are not finished.

        The manual-cleanup inventory: the username a bot would have, the bot id
        if one is known, and the exact step it stopped at. That is everything an
        operator needs to find the bot in @BotFather, and it is all safe to show
        — no token, no phone, no message.
        """
        journal = getattr(source, "journal", None)
        if journal is None:
            await message.answer(NOT_RUNNING)
            return
        unfinished = journal.unfinished
        if not unfinished:
            await message.answer(NOTHING_UNFINISHED)
            return
        lines = "\n".join(
            f"#{entry.max_chat_id} · @{entry.expected_username} · {entry.state.value}"
            + (f" · бот {entry.telegram_bot_id}" if entry.telegram_bot_id else "")
            + (f"\n   {entry.error}" if entry.error else "")
            for entry in unfinished[:20]
        )
        await message.answer(
            f"Незавершённых мостов: {len(unfinished)}\n\n{lines}\n\n"
            "Повторить: /provretry ЧАТ · Отложить: /provabandon ЧАТ"
        )

    @router.message(Command("provretry"))
    async def _provretry(message: Message) -> None:
        chat_id = _id_from(message, signed=True)
        resume = getattr(source, "resume_attempt", None)
        if chat_id is None:
            await message.answer(NEEDS_CHAT)
            return
        if resume is None:
            await message.answer(NOT_RUNNING)
            return
        await message.answer(RESUMING.format(chat_id=chat_id))
        outcome = await resume(chat_id)
        await message.answer(outcome or UNKNOWN_ATTEMPT)

    @router.message(Command("provabandon"))
    async def _provabandon(message: Message) -> None:
        """Stop asking about an attempt. Nothing remote is deleted by this.

        There is no `deleteManagedBot`, so a bot that was created stays created —
        which is exactly why the entry is marked rather than removed: it is the
        only place its username is written down.
        """
        chat_id = _id_from(message, signed=True)
        journal = getattr(source, "journal", None)
        if chat_id is None:
            await message.answer(NEEDS_CHAT)
            return
        if journal is None:
            await message.answer(NOT_RUNNING)
            return
        entry = journal.get(chat_id)
        if entry is None:
            await message.answer(UNKNOWN_ATTEMPT)
            return
        from bridge.provisioning.journal import ItemState

        journal.note(chat_id, state=ItemState.ABANDONED)
        tail = f" · бот {entry.telegram_bot_id}" if entry.telegram_bot_id else ""
        await message.answer(
            f"Отложено. Бот @{entry.expected_username}{tail} остаётся в Telegram —"
            " удалить его можно только руками в @BotFather."
        )

    @router.callback_query(F.data.startswith("incident:"))
    async def _from_alert(query: object) -> None:
        """The buttons on an alert. Same four actions, one tap instead of typing."""
        data = getattr(query, "data", "") or ""
        answer = getattr(query, "answer", None)
        _, _, rest = data.partition(":")
        action, _, raw = rest.partition(":")
        if answer is None:
            return
        outbox, _ = _queue()
        if outbox is None:
            await answer(NOT_RUNNING, show_alert=True)
            return
        if action == "retry" and raw.isdigit():
            ok = await outbox.retry_now(int(raw))
            await answer(RETRY_QUEUED.format(job_id=raw) if ok else UNKNOWN_JOB, show_alert=True)
            return
        if action == "resolved" and raw.isdigit():
            ok = await outbox.resolve(int(raw))
            await answer(
                RESOLVED_OK.format(job_id=raw) if ok else NOT_AMBIGUOUS.format(job_id=raw),
                show_alert=True,
            )
            return
        await answer(UNKNOWN_JOB, show_alert=True)

    return router


def _id_from(message: Message, *, signed: bool = False) -> int | None:
    """The number after the command. ASCII digits only.

    `str.isdigit()` is true of `²`, which `int()` then refuses — and a
    `ValueError` raised inside a handler is a command that does nothing and says
    nothing.
    """
    parts = (message.text or "").split()
    if len(parts) < 2:
        return None
    raw = parts[1].lstrip("#")
    body = raw[1:] if signed and raw.startswith("-") else raw
    if not body or not body.isascii() or not body.isdigit():
        return None
    return int(raw)
