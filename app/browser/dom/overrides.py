"""
Selector profile overrides layer.

Provides a controlled override mechanism for selector profiles:
- base profiles from code (app.browser.healing.selector_profiles)
- approved overrides from persistent storage
- effective profiles = merged result

Override precedence:
1. Approved override (from persistent store, operator-approved)
2. Base profile from code

This allows operator-reviewed selector changes without modifying source code.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from app.browser.dom.persistent_store import persistent_dom_store
from app.browser.healing.selector_profiles import SelectorProfile, build_provider_profiles
from app.core.logging import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)


class SelectorOverrideManager:
    """Manages selector profile overrides.

    Merges base profiles with approved overrides.
    """

    def get_effective_profile(
        self, provider_id: str, role: str
    ) -> SelectorProfile | None:
        """Get the effective selector profile for a provider+role.

        Returns merged profile with overrides applied, or None if not found.
        Override precedence:
        1. Approved override from persistent storage
        2. Base profile from code
        """
        base_profiles = build_provider_profiles(provider_id)
        base_profile = base_profiles.get(role)
        if base_profile is None:
            return None

        # Check for approved override
        override = persistent_dom_store.get_override(provider_id, role)
        if override and override.get("selector"):
            override_selector = override["selector"]
            logger.debug(
                "Applying selector override",
                provider_id=provider_id,
                role=role,
                override_selector=override_selector,
                source=override.get("source", "approved"),
            )

            # Prepend override selector to primary list
            new_primary = (override_selector,) + base_profile.primary
            return replace(base_profile, primary=new_primary)

        return base_profile

    def get_all_overrides(
        self, provider_id: str | None = None
    ) -> list[dict[str, str]]:
        """Get all active overrides."""
        return persistent_dom_store.get_overrides(provider_id)

    def reset_override(self, provider_id: str, role: str | None = None) -> int:
        """Remove override(s), reverting to base profile."""
        return persistent_dom_store.clear_override(provider_id, role)


selector_override_manager = SelectorOverrideManager()
