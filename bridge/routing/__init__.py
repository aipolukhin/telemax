"""Routing: the one-to-one mapping, dedup and the loop guard."""

from .adapters import (
    MaxTextSender,
    RegistryLookup,
    RegistryMutations,
    RegistrySender,
    build_forwarding_router,
)
from .max_mutation import DELETED_NOTICE
from .router import (
    OWN_MESSAGE_PREFIX,
    UNSUPPORTED_NOTICE,
    BridgeRouter,
    BridgeTarget,
)

__all__ = [
    "DELETED_NOTICE",
    "OWN_MESSAGE_PREFIX",
    "UNSUPPORTED_NOTICE",
    "BridgeRouter",
    "BridgeTarget",
    "MaxTextSender",
    "RegistryLookup",
    "RegistryMutations",
    "RegistrySender",
    "build_forwarding_router",
]
