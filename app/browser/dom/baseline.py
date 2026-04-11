"""
DOM Baseline model — lightweight structural fingerprints for key UI roles.

For each (provider_id, role) pair stores:
- selector used
- tag name
- role / aria role
- important attributes
- placeholder / aria-label / text summary
- container path summary
- sibling/parent context hints
- visibility/editability/clickability expectations

Does NOT store full HTML dumps — only structured metadata.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from playwright.async_api import Locator


@dataclass(frozen=True, slots=True)
class DOMBaseline:
    """Lightweight structural DOM fingerprint for a UI element role."""

    provider_id: str
    role: str
    # Selector that resolved this element
    selector: str
    # Structural attributes
    tag_name: str = ""
    aria_role: str = ""
    # Content hints
    placeholder: str = ""
    aria_label: str = ""
    text_summary: str = ""  # First 100 chars of text
    # Container context
    container_selector: str = ""
    parent_tag: str = ""
    sibling_count: int = 0
    # State expectations
    is_visible: bool = True
    is_editable: bool = False
    is_clickable: bool = False
    # Metadata
    captured_at: float = 0.0
    capture_reason: str = ""  # "primary_success", "healing_success", "recon_success"
    confidence: float = 0.0
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "role": self.role,
            "selector": self.selector,
            "tag_name": self.tag_name,
            "aria_role": self.aria_role,
            "placeholder": self.placeholder,
            "aria_label": self.aria_label,
            "text_summary": self.text_summary,
            "container_selector": self.container_selector,
            "parent_tag": self.parent_tag,
            "sibling_count": self.sibling_count,
            "is_visible": self.is_visible,
            "is_editable": self.is_editable,
            "is_clickable": self.is_clickable,
            "captured_at": round(self.captured_at, 1),
            "capture_reason": self.capture_reason,
            "confidence": self.confidence,
            "version": self.version,
        }

    @classmethod
    async def from_locator(
        cls,
        locator: Locator,
        provider_id: str,
        role: str,
        selector: str = "",
        capture_reason: str = "",
        confidence: float = 0.0,
        version: int = 1,
    ) -> DOMBaseline:
        """Capture baseline from a Playwright Locator."""
        tag_name = ""
        aria_role = ""
        placeholder = ""
        aria_label = ""
        text_summary = ""
        parent_tag = ""
        sibling_count = 0
        is_visible = False
        is_editable = False
        is_clickable = False

        try:
            is_visible = await locator.is_visible(timeout=1000)
        except Exception:
            pass

        try:
            tag_name = (await locator.evaluate("el => el.tagName")).lower()
        except Exception:
            pass

        try:
            aria_role = await locator.get_attribute("role") or ""
        except Exception:
            pass

        try:
            placeholder = await locator.get_attribute("placeholder") or ""
        except Exception:
            pass

        try:
            aria_label = await locator.get_attribute("aria-label") or ""
        except Exception:
            pass

        try:
            text_content = await locator.text_content() or ""
            text_summary = text_content.strip()[:100]
        except Exception:
            pass

        try:
            is_editable = not await locator.evaluate(
                "el => el.disabled || el.readOnly"
            )
        except Exception:
            pass

        try:
            is_clickable = tag_name == "button" or aria_role == "button"
            disabled = await locator.evaluate("el => el.disabled")
            aria_disabled = await locator.get_attribute("aria-disabled")
            if disabled or aria_disabled == "true":
                is_clickable = False
        except Exception:
            pass

        # Container context
        try:
            parent = await locator.evaluate(
                "el => el.parentElement ? el.parentElement.tagName.toLowerCase() : ''"
            )
            parent_tag = parent or ""
        except Exception:
            pass

        try:
            siblings = await locator.evaluate(
                "el => el.parentElement ? el.parentElement.children.length : 0"
            )
            sibling_count = siblings or 0
        except Exception:
            pass

        return cls(
            provider_id=provider_id,
            role=role,
            selector=selector,
            tag_name=tag_name,
            aria_role=aria_role,
            placeholder=placeholder,
            aria_label=aria_label,
            text_summary=text_summary,
            parent_tag=parent_tag,
            sibling_count=sibling_count,
            is_visible=is_visible,
            is_editable=is_editable,
            is_clickable=is_clickable,
            captured_at=time.monotonic(),
            capture_reason=capture_reason,
            confidence=confidence,
            version=version,
        )
