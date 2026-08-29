"""Compatibility adjustments applied before a PyMax session is opened.

MAX attachment payloads may omit optional fields while PyMax models declare
some of them required. Rejecting the full login response or dropping a message
is worse than preserving an unknown optional value, so attachment fields are
relaxed here. The tagged-union discriminator and message identity fields remain
required because delivery and deduplication cannot work without them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pydantic import BaseModel

logger = logging.getLogger(__name__)

_applied = False
#: Filled by the sweep, read back by `relaxed_fields` once there is a log to
#: write it to.
_relaxed: list[str] = []


#: The field that must stay required. It is the tagged union's discriminator —
#: `Field(alias="_type")` — and relaxing it would not make PyMax tolerant, it
#: would make it unable to tell a photo from a call.
DISCRIMINATOR = "type"


def relax_pymax_models() -> None:
    """Make attachment fields optional while preserving their discriminator.

    A failure is logged and swallowed so a PyMax release that already fixed or
    renamed these models can still start the bridge.
    """
    global _applied
    if _applied:
        return
    _applied = True

    for model in _attachment_models():
        for name, field in model.model_fields.items():
            if name == DISCRIMINATOR or not field.is_required():
                continue
            if _make_optional(model, name):
                _relaxed.append(f"{model.__name__}.{name}")

    if _relaxed:
        _rebuild_dependents()


def relaxed_fields() -> list[str]:
    """What the sweep took the requirement off, for whoever can still log it.

    Not logged where it happens. This runs at import — before `configure_logging`
    has put a handler on the root logger — so a line written here goes to nobody,
    which is the same invisibility the whole `previewData` outage was made of.
    The caller that owns a session logs it once the log exists.
    """
    return sorted(_relaxed)


def _attachment_models() -> list[type[BaseModel]]:
    """The members of PyMax's attachment union, whatever they are today.

    Read out of the union rather than imported one by one: the point of the
    sweep is that it covers the attachment PyMax adds next, and a hand-written
    list would cover exactly the ones we already knew about.
    """
    try:
        from typing import get_args

        from pymax.types.domain.message import KnownAttachment

        # `Annotated[A | B | ..., Field(discriminator=...)]` — the union first,
        # then its members.
        union = get_args(KnownAttachment)[0]
        return [model for model in get_args(union) if hasattr(model, "model_fields")]
    except Exception:
        logger.warning("could not read PyMax's attachment union", exc_info=True)
        return []


def typed_message_error(payload: dict[str, Any]) -> str | None:
    """Why PyMax's typed dispatch will drop this message frame, or None.

    `dispatch/resolvers.py::resolve_message` validates the whole frame payload
    against `Message` and answers a `ValidationError` with `logger.debug` and a
    `None`. So a message PyMax cannot type is not delivered, not retried, and —
    at the INFO level a bridge actually runs at — not logged either. The message
    simply does not exist for the process. Compatibility tests confirmed: a video note sat
    unseen for three hours on a connection that never dropped, and the same
    message arrived the moment history was re-read.

    Asking the same question here, before PyMax gets the frame, is what turns
    that silence into a line and a delivery. Answering `None` means PyMax is
    happy and nothing else should happen: the ordinary path is left alone.

    **Field names and messages only, never the input.** A payload carries CDN
    tokens; `include_input=True` would put them in the log.
    """
    try:
        from pydantic import ValidationError
        from pymax.types.domain import Message
    except Exception:  # pragma: no cover - PyMax shape changed
        logger.debug("cannot ask PyMax whether it can type a message", exc_info=True)
        return None

    try:
        Message.model_validate(payload)
    except ValidationError as error:
        reasons = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors(include_url=False, include_input=False)
        )
        return reasons[:500] or "refused without a reason"
    except Exception:  # noqa: BLE001 - not a refusal; PyMax will raise, loudly, on its own
        # Anything other than a `ValidationError` is a frame PyMax breaks on
        # rather than drops, and a break is already visible. Answering "typeable"
        # here leaves that path exactly as it was.
        return None
    return None


def _make_optional(model: type[BaseModel], field_name: str) -> bool:
    """Relax one required field to `<its type> | None`. Returns whether it changed.

    The field is looked up by its Python name; PyMax models are camelCase on the
    wire and snake_case in the class, so `contactId` is `contact_id` here.

    The type it already declares is kept and widened rather than replaced: a
    `videoId` that does arrive should still have to be a number. Optional is a
    statement about presence, not about what the field means.
    """
    field = model.model_fields.get(field_name)
    if field is None or not field.is_required():
        return False
    declared = field.annotation
    field.annotation = (declared | None) if declared is not None else None  # type: ignore[assignment]
    field.default = None
    model.model_rebuild(force=True)
    logger.debug("relaxed %s.%s", model.__name__, field_name)
    return True


def _rebuild_dependents() -> None:
    """Rebuild every loaded PyMax model, not just the one that changed.

    Pydantic compiles a model's validator once and caches it, inlining the
    schema of everything it contains. `Message`, `Chat` and `LoginResponse` all
    embed the attachment union, so relaxing the sticker alone changes nothing
    they can see — they keep validating against the schema they were built with.
    Rebuilding the sticker first and then everything else makes the new shape
    propagate outwards.
    """
    import sys

    from pydantic import BaseModel

    modules = [module for name, module in list(sys.modules.items())
               if name.startswith("pymax") and module is not None]
    seen: set[type] = set()
    for module in modules:
        for value in vars(module).values():
            if not isinstance(value, type) or value in seen:
                continue
            if not issubclass(value, BaseModel):
                continue
            seen.add(value)
            try:
                value.model_rebuild(force=True)
            except Exception:
                # A model that will not rebuild is one we were not using
                # anyway; the ones that matter are on the login path, and those
                # do rebuild.
                logger.debug("could not rebuild %s", value.__name__, exc_info=True)
