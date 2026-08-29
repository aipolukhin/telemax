"""Onboarding: everything the owner does after leaving the terminal.

The console can only get as far as a running guardian bot. From there the MAX
login, the configuration, the first start of the bridge and every later change
happen in a chat — because a chat is the one place the owner still has once the
SSH session is closed.
"""

from . import screens
from .board import StatusBoard
from .fsm import MaxLoginError, MaxOnboarding
from .maxauth import AuthAbandoned, MaxAuthGateway, Prompt, Question
from .router import build_onboarding_router
from .state import Stage, StateStore, Step
from .tokens import IssuedToken, Verdict, deep_link, digest_of, issue, verify

__all__ = [
    "AuthAbandoned",
    "IssuedToken",
    "MaxAuthGateway",
    "MaxLoginError",
    "MaxOnboarding",
    "Prompt",
    "Question",
    "Stage",
    "StateStore",
    "StatusBoard",
    "Step",
    "Verdict",
    "build_onboarding_router",
    "deep_link",
    "digest_of",
    "issue",
    "screens",
    "verify",
]
