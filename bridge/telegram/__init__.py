"""Telegram side: shared dispatcher, one bot per bridge, owner-only."""

from . import design
from .app import build_commands_router, build_dispatcher
from .errors import (
    TelegramOutcome,
    TelegramTransportUnavailableError,
    TelegramVerdict,
    classify_telegram,
    is_no_op,
    refused_group,
    refused_representation,
)
from .forwards import (
    ForwardAuthor,
    author_of_entity,
    bot_api_forward_author,
    bot_api_forward_date,
    bot_api_forward_origin,
    forward_peer_id,
    is_bot_api_forward,
    is_mtproto_forward,
    mtproto_forward_date,
    mtproto_forward_name,
    name_of_entity,
)
from .owner import STRANGER_REPLY, OwnerOnlyMiddleware
from .registry import DEFAULT_ALLOWED_UPDATES, BotRegistry, BridgeRegistryError, LiveBridge
from .runner import BotIdentity, BotRunner

__all__ = [
    "DEFAULT_ALLOWED_UPDATES",
    "STRANGER_REPLY",
    "BotIdentity",
    "BotRegistry",
    "BotRunner",
    "BridgeRegistryError",
    "ForwardAuthor",
    "LiveBridge",
    "OwnerOnlyMiddleware",
    "TelegramOutcome",
    "TelegramTransportUnavailableError",
    "TelegramVerdict",
    "author_of_entity",
    "bot_api_forward_author",
    "bot_api_forward_date",
    "bot_api_forward_origin",
    "build_commands_router",
    "build_dispatcher",
    "classify_telegram",
    "design",
    "forward_peer_id",
    "is_bot_api_forward",
    "is_mtproto_forward",
    "is_no_op",
    "mtproto_forward_date",
    "mtproto_forward_name",
    "name_of_entity",
    "refused_group",
    "refused_representation",
]
