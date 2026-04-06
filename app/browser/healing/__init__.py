"""
Self-healing selectors for browser providers.

Re-exports the main healing components for easy importing.
"""

from app.browser.healing.element_verifier import ElementVerifier, VerificationResult
from app.browser.healing.healing_engine import HealingCandidate, HealingEngine
from app.browser.healing.locator_resolver import LocatorResolver, create_resolver
from app.browser.healing.runtime_cache import (
    CachedHealedLocator,
    RuntimeHealingCache,
    healing_cache,
)
from app.browser.healing.selector_profiles import (
    ASSISTANT_MESSAGE,
    CHAT_READY_INDICATOR,
    MESSAGE_INPUT,
    NEW_CHAT_BUTTON,
    SEND_BUTTON,
    SelectorProfile,
    build_provider_profiles,
)
from app.browser.healing.telemetry import HealingTelemetry, healing_telemetry

__all__ = [
    # Profiles
    "SelectorProfile",
    "build_provider_profiles",
    "MESSAGE_INPUT",
    "SEND_BUTTON",
    "ASSISTANT_MESSAGE",
    "NEW_CHAT_BUTTON",
    "CHAT_READY_INDICATOR",
    # Resolver
    "LocatorResolver",
    "create_resolver",
    # Verifier
    "ElementVerifier",
    "VerificationResult",
    # Healing engine
    "HealingEngine",
    "HealingCandidate",
    # Cache
    "RuntimeHealingCache",
    "CachedHealedLocator",
    "healing_cache",
    # Telemetry
    "HealingTelemetry",
    "healing_telemetry",
]
