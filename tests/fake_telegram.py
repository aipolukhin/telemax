"""A Telegram that never touches the network.

Every test in this project runs against this: no token, no account, no rate
limit. It records what was called so a test can assert on the *effect* of a
handler rather than on its internals.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from aiogram.exceptions import TelegramUnauthorizedError


def _snake_case(name: str) -> str:
    """`SendMessage` -> `send_message`, so both call paths record the same name."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


@dataclass(slots=True)
class Call:
    method: str
    kwargs: dict[str, Any]


class FakeSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeBot:
    """Stands in for `aiogram.Bot`.

    `updates` is the queue `get_updates` hands out, one batch per call; when it
    runs dry the call blocks forever, exactly like a real long poll with nothing
    to report.
    """

    def __init__(
        self,
        token: str,
        *,
        bot_id: int = 1,
        username: str = "test_bot",
        unauthorized: bool = False,
        **_: Any,
    ) -> None:
        self.token = token
        self.id = bot_id
        self.username = username
        self.session = FakeSession()
        self.calls: list[Call] = []
        self.updates: list[list[Any]] = []
        self._unauthorized = unauthorized
        # Two refusals worth simulating: a bot that may not write first, and a
        # message too old to edit. Both change what the caller must do next.
        self.send_fails = False
        self.edit_fails = False
        # Deleting somebody's message can be refused (too old, no rights); the
        # bridge has to say so without repeating what was in it.
        self.delete_fails = False

    async def get_me(self) -> Any:
        if self._unauthorized:
            raise TelegramUnauthorizedError(method=None, message="Unauthorized")  # type: ignore[arg-type]
        self.calls.append(Call("get_me", {}))
        return type("Me", (), {"id": self.id, "username": self.username})()

    async def get_updates(self, **kwargs: Any) -> list[Any]:
        self.calls.append(Call("get_updates", kwargs))
        if self.updates:
            return self.updates.pop(0)
        import asyncio

        await asyncio.Event().wait()  # a poll with nothing to report
        return []

    async def send_message(self, **kwargs: Any) -> Any:
        self.calls.append(Call("send_message", kwargs))
        if self.send_fails:
            raise RuntimeError("chat not found")
        return self._result_for("send_message", kwargs)

    async def edit_message_text(self, **kwargs: Any) -> Any:
        self.calls.append(Call("edit_message_text", kwargs))
        if self.edit_fails:
            raise RuntimeError("message can't be edited")
        return self._result_for("edit_message_text", kwargs)

    async def __call__(self, method: Any, request_timeout: int | None = None) -> Any:
        """aiogram calls the bot with a method object — `message.answer()` does.

        Recording here as well as on the named methods means a test sees the
        same call whichever way a handler chose to send it.
        """
        name = _snake_case(type(method).__name__)
        kwargs = method.model_dump(exclude_none=True)
        self.calls.append(Call(name, kwargs))
        if name == "delete_message" and self.delete_fails:
            raise RuntimeError("message can't be deleted")
        return self._result_for(name, kwargs)

    def _result_for(self, method: str, kwargs: dict[str, Any]) -> Any:
        if method in {"send_message", "edit_message_text", "edit_message_caption"}:
            from datetime import UTC, datetime

            from aiogram.types import Chat, Message

            return Message.model_construct(
                message_id=len(self.calls),
                date=datetime.now(tz=UTC),
                chat=Chat.model_construct(id=kwargs.get("chat_id", 0), type="private"),
                text=kwargs.get("text"),
            )
        return True

    def method_calls(self, method: str) -> list[dict[str, Any]]:
        return [call.kwargs for call in self.calls if call.method == method]


OWNER_ID = 111
STRANGER_ID = 222


def make_message(
    update_id: int, text: str, *, user_id: int = OWNER_ID, chat_id: int | None = None
) -> Any:
    """One text message, as an `Update` a dispatcher will accept."""
    from datetime import UTC, datetime

    from aiogram.types import Chat, Message, Update, User

    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(tz=UTC),
            chat=Chat(id=chat_id if chat_id is not None else user_id, type="private"),
            from_user=User(id=user_id, is_bot=False, first_name="Someone"),
            text=text,
        ),
    )


def make_contact_message(
    update_id: int,
    *,
    phone: str,
    first_name: str,
    last_name: str | None = None,
    user_id: int = OWNER_ID,
) -> Any:
    """An attached contact card — the clip-Контакт path of adding somebody."""
    from datetime import UTC, datetime

    from aiogram.types import Chat, Contact, Message, Update, User

    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(tz=UTC),
            chat=Chat(id=user_id, type="private"),
            from_user=User(id=user_id, is_bot=False, first_name="Someone"),
            contact=Contact(
                phone_number=phone, first_name=first_name, last_name=last_name
            ),
        ),
    )


def make_callback(
    update_id: int, data: str, *, user_id: int = OWNER_ID, chat_id: int | None = None
) -> Any:
    """A button press, carried by a message the bot itself sent."""
    from datetime import UTC, datetime

    from aiogram.types import CallbackQuery, Chat, Message, Update, User

    user = User(id=user_id, is_bot=False, first_name="Someone")
    carrier = Message(
        message_id=update_id,
        date=datetime.now(tz=UTC),
        chat=Chat(id=chat_id if chat_id is not None else user_id, type="private"),
        from_user=User(id=1, is_bot=True, first_name="Guardian"),
        text="…",
    )
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=str(update_id),
            from_user=user,
            chat_instance=str(update_id),
            message=carrier,
            data=data,
        ),
    )


@dataclass(slots=True)
class FakeBotFactory:
    """Hands out `FakeBot`s and remembers them, keyed by token."""

    bots: dict[str, FakeBot] = field(default_factory=dict)
    next_bot_id: int = 100
    unauthorized_tokens: set[str] = field(default_factory=set)
    bot_ids: dict[str, int] = field(default_factory=dict)

    def __call__(self, token: str, **kwargs: Any) -> FakeBot:
        bot_id = self.bot_ids.get(token, self.next_bot_id)
        if token not in self.bot_ids:
            self.next_bot_id += 1
        bot = FakeBot(
            token,
            bot_id=bot_id,
            username=f"bot{bot_id}",
            unauthorized=token in self.unauthorized_tokens,
        )
        self.bots[token] = bot
        return bot
