"""Provisioning: a MAX contact becomes a bridge, deterministically, on the fly."""

from .avatar import AvatarUnusableError, to_profile_jpeg
from .batch import BridgeGateway, ProvisioningBatch
from .business import (
    BusinessConnectionRecord,
    read_record,
    record_business_connection,
    use_state_dir,
)
from .byphone import ContactDirectory, ContactDraft, Resolution, Verdict
from .capacity import Capacity, PeerBotStatus, PeerPlan, classify
from .coordinator import ContactKey, ProvisioningCoordinator
from .flow import BridgeSummary, DialogFlow
from .guardian import (
    GuardianContext,
    announce_markup,
    announce_text,
    build_guardian_router,
)
from .history import HistoryImporter, HistorySource, ImportTarget
from .journal import ItemState, JournalEntry, ProvisioningJournal
from .naming import (
    guard_username,
    load_or_create_naming_secret,
    stable_slug,
    validate_username,
)
from .naming_v2 import (
    NamingVersion,
    contact_bot_username_v2,
    guardian_bot_username_v2,
)
from .owned import (
    BotApiOwnedBots,
    OwnerMustOpenChatError,
    RepositoryKnownBots,
    start_link,
)
from .picker import DialogOption, DialogPicker
from .profile import BotProfileSync, signature_of
from .profile_adapter import TelegramProfileAdapter
from .provisioner import (
    BotLimit,
    BotProvisioner,
    ForeignUsernameError,
    MtprotoProvisioner,
    UsernameState,
)
from .runtime import BridgeReplayer, Guardian
from .secrets import ContactBotSecretStore
from .selection import Selection, picker_markup, picker_text
from .service import (
    TOKEN_PATTERN,
    PendingAnnouncement,
    Provisioner,
    ProvisioningError,
)

__all__ = [
    "TOKEN_PATTERN",
    "AvatarUnusableError",
    "BotApiOwnedBots",
    "BotLimit",
    "BotProfileSync",
    "BotProvisioner",
    "BridgeGateway",
    "BridgeReplayer",
    "BridgeSummary",
    "BusinessConnectionRecord",
    "Capacity",
    "ContactBotSecretStore",
    "ContactDirectory",
    "ContactDraft",
    "ContactKey",
    "DialogFlow",
    "DialogOption",
    "DialogPicker",
    "ForeignUsernameError",
    "Guardian",
    "GuardianContext",
    "HistoryImporter",
    "HistorySource",
    "ImportTarget",
    "ItemState",
    "JournalEntry",
    "MtprotoProvisioner",
    "NamingVersion",
    "OwnerMustOpenChatError",
    "PeerBotStatus",
    "PeerPlan",
    "PendingAnnouncement",
    "Provisioner",
    "ProvisioningBatch",
    "ProvisioningCoordinator",
    "ProvisioningError",
    "ProvisioningJournal",
    "RepositoryKnownBots",
    "Resolution",
    "Selection",
    "TelegramProfileAdapter",
    "UsernameState",
    "Verdict",
    "announce_markup",
    "announce_text",
    "build_guardian_router",
    "classify",
    "contact_bot_username_v2",
    "guard_username",
    "guardian_bot_username_v2",
    "load_or_create_naming_secret",
    "picker_markup",
    "picker_text",
    "read_record",
    "record_business_connection",
    "signature_of",
    "stable_slug",
    "start_link",
    "to_profile_jpeg",
    "use_state_dir",
    "validate_username",
]
