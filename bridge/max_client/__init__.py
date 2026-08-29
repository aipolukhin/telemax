"""MAX client: the only module that knows PyMax exists."""

from .pymax_compat import relax_pymax_models

# Before anything can import a PyMax model, let alone open a session: the login
# answer is parsed against these models, and a custom sticker in the chat list
# makes the stock one refuse to parse at all. See `pymax_compat`.
relax_pymax_models()

from .client import (  # noqa: E402
    MaxClient,
    MaxClientError,
    MaxMediaError,
    MaxNativeRejectedError,
    MaxUnconfirmedSendError,
)
from .events import (  # noqa: E402
    AttachmentKind,
    ChatReaction,
    IncomingMaxMessage,
    MaxAttachment,
    MaxContact,
    MaxForward,
    MessageDeleted,
    MessageReactions,
    PresenceUpdate,
    ReactionUpdate,
    ReadMark,
    TypingSignal,
    chat_reaction_from,
    enum_text,
    normalize_attachment,
    normalize_contact,
    normalize_message,
    normalize_presence,
    reaction_from,
    reactions_by_message,
)
from .interactive import InteractivePings, PresenceMode  # noqa: E402
from .native_state import (  # noqa: E402
    NATIVE_KINDS,
    KindStatus,
    NativeMediaState,
    classify_native_error,
)
from .opcodes import Opcode, TypingKind  # noqa: E402
from .session import ensure_session_dir, harden_session_files  # noqa: E402

__all__ = [
    "NATIVE_KINDS",
    "AttachmentKind",
    "ChatReaction",
    "IncomingMaxMessage",
    "InteractivePings",
    "KindStatus",
    "MaxAttachment",
    "MaxClient",
    "MaxClientError",
    "MaxContact",
    "MaxForward",
    "MaxMediaError",
    "MaxNativeRejectedError",
    "MaxUnconfirmedSendError",
    "MessageDeleted",
    "MessageReactions",
    "NativeMediaState",
    "Opcode",
    "PresenceMode",
    "PresenceUpdate",
    "ReactionUpdate",
    "ReadMark",
    "TypingKind",
    "TypingSignal",
    "chat_reaction_from",
    "classify_native_error",
    "ensure_session_dir",
    "enum_text",
    "harden_session_files",
    "normalize_attachment",
    "normalize_contact",
    "normalize_message",
    "normalize_presence",
    "reaction_from",
    "reactions_by_message",
]
