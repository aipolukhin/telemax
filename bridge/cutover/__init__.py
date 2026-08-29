"""The one-time V2 cutover: destroy the legacy bots' local state, keep the accounts.

Split three ways on purpose. `inventory` only reads — it is the evidence both
owner gates are decided from. `backup` copies and then proves the copy. `purge`
is the only thing that removes anything, and it removes exactly the set the
inventory classified and the owner approved.
"""

from .backup import Backup, take, verify
from .floors import apply_floor, read_floors, write_floors
from .flow import Outcome, Phase, read_phase, record
from .inventory import CutoverInventory, LegacyBot, Planned, Role, TableScope, classify_tables
from .purge import Purged

__all__ = [
    "Backup",
    "CutoverInventory",
    "LegacyBot",
    "Outcome",
    "Phase",
    "Planned",
    "Purged",
    "Role",
    "TableScope",
    "apply_floor",
    "classify_tables",
    "read_floors",
    "read_phase",
    "record",
    "take",
    "verify",
    "write_floors",
]
