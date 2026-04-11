"""
Healing engine — DOM scanning and candidate ranking.

When primary and fallback selectors fail:
1. Scan the page for candidate elements
2. Score each candidate using heuristic matchers
3. Return top candidates for verification
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from playwright.async_api import Locator, Page

from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.browser.healing.selector_profiles import SelectorProfile

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class HealingCandidate:
    """A candidate element found during healing scan."""

    locator: Locator
    selector_used: str
    score: float  # 0.0 - 1.0
    reason: str


@dataclass(slots=True)
class HealingEngine:
    """Scans DOM and ranks candidate elements for healing.

    This engine does NOT decide which element to use — it only
    generates scored candidates. The LocatorResolver + ElementVerifier
    make the final decision.
    """

    def __init__(self, page: Page, provider_id: str) -> None:
        self.page = page
        self.provider_id = provider_id

    async def scan(
        self, profile: SelectorProfile, max_candidates: int = 20
    ) -> list[HealingCandidate]:
        """Scan the DOM for candidates matching the profile.

        Returns candidates sorted by score (highest first).
        """
        candidates: list[HealingCandidate] = []

        # 1. Scan by tag name (broad match)
        if profile.expected_tag:
            tag_candidates = await self._scan_by_tag(profile)
            candidates.extend(tag_candidates)

        # 2. Scan by role
        if profile.expected_role:
            role_candidates = await self._scan_by_role(profile)
            candidates.extend(role_candidates)

        # 3. Scan by aria attributes
        aria_candidates = await self._scan_by_aria(profile)
        candidates.extend(aria_candidates)

        # 4. Scan by class name heuristics
        class_candidates = await self._scan_by_class_heuristics(profile)
        candidates.extend(class_candidates)

        # 5. Scan by semantic keywords in text/attributes
        keyword_candidates = await self._scan_by_keywords(profile)
        candidates.extend(keyword_candidates)

        # 6. Broad scan: all interactive elements
        if not candidates:
            broad = await self._scan_all_interactive(profile)
            candidates.extend(broad)

        # Deduplicate by element handle
        seen = set()
        unique: list[HealingCandidate] = []
        for c in candidates:
            try:
                elem_id = await c.locator.evaluate("el => el.dataset.healingId || ''")
                if not elem_id:
                    # Assign a temporary ID for dedup
                    elem_id = str(id(c))
                if elem_id not in seen:
                    seen.add(elem_id)
                    unique.append(c)
            except Exception:
                unique.append(c)

        # Sort by score descending
        unique.sort(key=lambda c: c.score, reverse=True)

        # Limit
        result = unique[:max_candidates]

        logger.info(
            "Healing scan complete",
            role=profile.role,
            provider_id=self.provider_id,
            candidates_found=len(result),
            top_score=result[0].score if result else 0.0,
        )

        return result

    async def _scan_by_tag(self, profile: SelectorProfile) -> list[HealingCandidate]:
        """Find elements by tag name."""
        candidates: list[HealingCandidate] = []
        try:
            elements = await self.page.locator(profile.expected_tag).all()
            for el in elements:
                score = 0.4  # Base score for tag match
                score += await self._score_text_match(el, profile)
                score += await self._score_aria_match(el, profile)
                if score > 0.3:
                    candidates.append(
                        HealingCandidate(
                            locator=el,
                            selector_used=f"tag:{profile.expected_tag}",
                            score=min(score, 0.95),
                            reason=f"tag={profile.expected_tag}",
                        )
                    )
        except Exception:
            pass
        return candidates

    async def _scan_by_role(self, profile: SelectorProfile) -> list[HealingCandidate]:
        """Find elements by ARIA role."""
        candidates: list[HealingCandidate] = []
        try:
            role_locator = self.page.get_by_role(profile.expected_role)
            elements = await role_locator.all()
            for el in elements:
                score = 0.5  # Role match is strong
                score += await self._score_text_match(el, profile)
                score += await self._score_class_match(el, profile)
                if score > 0.4:
                    candidates.append(
                        HealingCandidate(
                            locator=el,
                            selector_used=f"role={profile.expected_role}",
                            score=min(score, 0.95),
                            reason=f"role={profile.expected_role}",
                        )
                    )
        except Exception:
            pass
        return candidates

    async def _scan_by_aria(self, profile: SelectorProfile) -> list[HealingCandidate]:
        """Find elements by aria-label or aria-describedby."""
        candidates: list[HealingCandidate] = []
        try:
            all_elements = await self.page.locator('[aria-label]').all()
            for el in all_elements:
                aria_label = (await el.get_attribute("aria-label") or "").lower()
                if profile.placeholder_hint and profile.placeholder_hint.lower() in aria_label:
                    score = 0.6
                    candidates.append(
                        HealingCandidate(
                            locator=el,
                            selector_used="aria-label",
                            score=score,
                            reason=f"aria-label match: {aria_label[:50]}",
                        )
                    )
        except Exception:
            pass
        return candidates

    async def _scan_by_class_heuristics(
        self, profile: SelectorProfile
    ) -> list[HealingCandidate]:
        """Find elements by class name patterns."""
        candidates: list[HealingCandidate] = []

        class_patterns = []
        if profile.role == "message_input":
            class_patterns = ["input", "editor", "composer", "textarea", "chat-input"]
        elif profile.role == "send_button":
            class_patterns = ["send", "submit", "send-btn", "compose-send"]
        elif profile.role == "assistant_message":
            class_patterns = ["assistant", "response", "reply", "message", "bot"]
        elif profile.role == "new_chat_button":
            class_patterns = ["new-chat", "new_chat", "newchat"]

        for pattern in class_patterns:
            try:
                elements = await self.page.locator(f'[class*="{pattern}"]').all()
                for el in elements[:10]:  # Limit per pattern
                    score = 0.35
                    score += await self._score_tag_match(el, profile)
                    score += await self._score_text_match(el, profile)
                    if score > 0.3:
                        candidates.append(
                            HealingCandidate(
                                locator=el,
                                selector_used=f'class*="{pattern}"',
                                score=min(score, 0.9),
                                reason=f'class pattern: {pattern}',
                            )
                        )
            except Exception:
                pass

        return candidates

    async def _scan_by_keywords(self, profile: SelectorProfile) -> list[HealingCandidate]:
        """Find elements by text/attribute keyword matching."""
        candidates: list[HealingCandidate] = []
        if not profile.semantic_keywords:
            return candidates

        try:
            # Scan all textareas and buttons
            for tag in ["textarea", "button", "input"]:
                elements = await self.page.locator(tag).all()
                for el in elements:
                    score = await self._score_keyword_match(el, profile)
                    if score > 0.5:
                        candidates.append(
                            HealingCandidate(
                                locator=el,
                                selector_used=f"keyword:{tag}",
                                score=min(score, 0.95),
                                reason="keyword match in text/attributes",
                            )
                        )
        except Exception:
            pass
        return candidates

    async def _scan_all_interactive(
        self, profile: SelectorProfile
    ) -> list[HealingCandidate]:
        """Broad scan: all interactive elements on page."""
        candidates: list[HealingCandidate] = []
        try:
            # Get all interactive elements
            all_interactive = await self.page.locator(
                "button, [role=button], textarea, input, [role=textbox], [contenteditable]"
            ).all()

            for el in all_interactive[:50]:  # Limit broad scan
                score = 0.1  # Very low base for broad scan
                score += await self._score_tag_match(el, profile)
                score += await self._score_text_match(el, profile)
                score += await self._score_aria_match(el, profile)
                score += await self._score_class_match(el, profile)
                score += await self._score_keyword_match(el, profile)

                if score > 0.4:
                    candidates.append(
                        HealingCandidate(
                            locator=el,
                            selector_used="broad:interactive",
                            score=min(score, 0.85),
                            reason="broad interactive element match",
                        )
                    )
        except Exception:
            pass
        return candidates

    # ── Scoring helpers ──

    async def _score_tag_match(
        self, locator: Locator, profile: SelectorProfile
    ) -> float:
        if not profile.expected_tag:
            return 0.0
        try:
            tag = (await locator.evaluate("el => el.tagName")).lower()
            if tag == profile.expected_tag.lower():
                return 0.2
            elif tag in ("div", "span") and profile.expected_tag in (
                "textarea",
                "button",
            ):
                return 0.05  # Div/span can sometimes wrap the real element
        except Exception:
            pass
        return 0.0

    async def _score_text_match(
        self, locator: Locator, profile: SelectorProfile
    ) -> float:
        if not profile.semantic_keywords:
            return 0.0
        try:
            text = (await locator.text_content() or "").lower()
            matches = sum(
                1 for kw in profile.semantic_keywords if kw.lower() in text
            )
            return min(matches * 0.1, 0.3)
        except Exception:
            return 0.0

    async def _score_aria_match(
        self, locator: Locator, profile: SelectorProfile
    ) -> float:
        score = 0.0
        try:
            aria_label = (
                await locator.get_attribute("aria-label") or ""
            ).lower()
            if profile.placeholder_hint and profile.placeholder_hint.lower() in aria_label:
                score += 0.15
            if profile.semantic_keywords:
                for kw in profile.semantic_keywords:
                    if kw.lower() in aria_label:
                        score += 0.1
        except Exception:
            pass
        return min(score, 0.3)

    async def _score_class_match(
        self, locator: Locator, profile: SelectorProfile
    ) -> float:
        try:
            class_attr = (await locator.get_attribute("class") or "").lower()
            if profile.role == "message_input" and any(
                p in class_attr
                for p in ["input", "editor", "composer", "textarea"]
            ):
                return 0.15
            if profile.role == "send_button" and any(
                p in class_attr for p in ["send", "submit", "compose"]
            ):
                return 0.15
        except Exception:
            pass
        return 0.0

    async def _score_keyword_match(
        self, locator: Locator, profile: SelectorProfile
    ) -> float:
        if not profile.semantic_keywords:
            return 0.0
        try:
            text = (await locator.text_content() or "").lower()
            placeholder = (
                await locator.get_attribute("placeholder") or ""
            ).lower()
            aria_label = (
                await locator.get_attribute("aria-label") or ""
            ).lower()
            combined = f"{text} {placeholder} {aria_label}"

            matches = sum(
                1 for kw in profile.semantic_keywords if kw.lower() in combined
            )
            return min(matches * 0.15, 0.45)
        except Exception:
            return 0.0
