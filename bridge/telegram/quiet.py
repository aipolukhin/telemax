"""History arrives without a sound.

An import pulls the last fifty messages of a MAX dialog into a brand-new chat.
Delivered the ordinary way, that is fifty notifications for a conversation the
owner has already read — in the MAX app, often years ago. The messages are
wanted; the pings are not.

`disable_notification` is per-message, which is the awkward part: the flag would
have to be threaded through `on_max_message`, the router, the media adapter and
every one of the dozen `send_*` calls underneath, and every one of them would
have to remember to pass it on. A single forgotten hop is a chat that pings.

So it is not threaded. A context variable says "this delivery is history" and an
outgoing-request middleware on the bot's own session stamps the flag onto
whatever send method comes past. The variable is set for the duration of one
import and reset after, so nothing needs turning back on — real-time delivery never
sees it, and a task spawned inside the import inherits it, which is more than
an argument would have managed.

Silent, not hidden: Telegram still counts the messages unread and still shows
the badge. Only the sound and the banner are suppressed. There is no Bot API
call that mutes a chat on the owner's behalf, so this is the whole of what can
be done — and it is what was wanted, since a chat muted for the import would
have to be unmuted afterwards by hand.

One gap, named rather than papered over: a send that fails during an import
goes to the outbox, and the retry worker is a different task that never entered
the block. That message pings when it lands. Threading the flag onto the queued
job would close it; it is not worth a schema change for the rare failed send in
a run the owner is watching happen.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

#: True while the current task is replaying history rather than carrying live
#: traffic. Read by the middleware below and by nothing else.
_QUIET: ContextVar[bool] = ContextVar("telemax_quiet_delivery", default=False)

#: The field to stamp. Absent from `getMe`, `getUpdates` and the rest, which is
#: how those are told apart from the sends without listing the sends.
FLAG = "disable_notification"


@contextmanager
def quietly() -> Iterator[None]:
    """Every send inside this block goes out silently."""
    token = _QUIET.set(True)
    try:
        yield
    finally:
        _QUIET.reset(token)


def is_quiet() -> bool:
    return _QUIET.get()


async def silence_history(
    make_request: Callable[[Any, Any], Awaitable[Any]],
    bot: Any,
    method: Any,
) -> Any:
    """aiogram outgoing middleware: mute sends made while replaying history.

    Only ever *sets* the flag. A caller that has already asked for silence keeps
    it, and a method with no such field is passed through untouched.
    """
    if _QUIET.get() and FLAG in type(method).model_fields:
        setattr(method, FLAG, True)
    return await make_request(bot, method)
