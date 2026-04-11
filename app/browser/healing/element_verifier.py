"""
Element verifier — validates recovered candidate elements.

Checks:
- visibility
- enabled/editable/clickable state
- expected tag/role
- contextual position
- role matching
- confidence scoring
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.core.logging import get_logger

if TYPE_CHECKING:
    from playwright.async_api import Locator

    from app.browser.healing.selector_profiles import SelectorProfile

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Result of element verification."""

    is_valid: bool
    confidence: float  # 0.0 - 1.0
    locator: Locator
    details: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def passes_threshold(self) -> bool:
        return self.is_valid and self.confidence >= 0.5


class ElementVerifier:
    """Verifies that a recovered candidate element matches the expected profile."""

    async def verify(
        self,
        locator: Locator,
        profile: SelectorProfile,
    ) -> VerificationResult:
        """Verify a candidate element against a profile.

        Returns VerificationResult with confidence score.
        """
        details: dict[str, str] = {}
        errors: list[str] = []
        score = 0.0
        max_score = 0.0

        # 1. Visibility check (required if profile says so)
        max_score += 20
        if profile.must_be_visible:
            try:
                is_visible = await locator.is_visible(timeout=1000)
                if is_visible:
                    score += 20
                    details["visible"] = "yes"
                else:
                    errors.append("not visible")
                    return VerificationResult(
                        is_valid=False,
                        confidence=0.0,
                        locator=locator,
                        details=details,
                        errors=errors,
                    )
            except Exception as exc:
                errors.append(f"visibility check failed: {exc}")
                return VerificationResult(
                    is_valid=False,
                    confidence=0.0,
                    locator=locator,
                    details=details,
                    errors=errors,
                )
        else:
            score += 20  # Not required, give points

        # 2. Tag name check
        max_score += 15
        if profile.expected_tag:
            try:
                tag = (await locator.evaluate("el => el.tagName")).lower()
                details["actual_tag"] = tag
                if tag == profile.expected_tag.lower():
                    score += 15
                    details["tag_match"] = "exact"
                elif self._tag_is_related(tag, profile.expected_tag):
                    score += 10
                    details["tag_match"] = "related"
                else:
                    errors.append(f"tag mismatch: {tag} != {profile.expected_tag}")
                    score += 0
            except Exception as exc:
                errors.append(f"tag check failed: {exc}")
                score += 5  # Partial credit
        else:
            score += 15  # Not required

        # 3. Role check
        max_score += 15
        if profile.expected_role:
            try:
                role = await locator.evaluate(
                    "el => el.getAttribute('role') || ''"
                )
                details["actual_role"] = role or "(none)"
                if role.lower() == profile.expected_role.lower():
                    score += 15
                    details["role_match"] = "exact"
                elif profile.expected_role in (role or ""):
                    score += 10
                    details["role_match"] = "partial"
                else:
                    score += 5  # No role attr but might still work
            except Exception:
                score += 5
        else:
            score += 15

        # 4. Editable/clickable check
        max_score += 15
        if profile.is_editable:
            try:
                disabled = await locator.evaluate("el => el.disabled")
                readonly = await locator.evaluate(
                    "el => el.readOnly || el.getAttribute('aria-readonly') === 'true'"
                )
                is_editable = not disabled and not readonly
                details["editable"] = str(is_editable)
                if is_editable:
                    score += 15
                else:
                    errors.append("not editable")
                    score += 0
            except Exception:
                score += 5
        elif profile.is_clickable:
            try:
                disabled = await locator.evaluate("el => el.disabled")
                aria_disabled = await locator.evaluate(
                    "el => el.getAttribute('aria-disabled')"
                )
                is_clickable = not disabled and aria_disabled != "true"
                details["clickable"] = str(is_clickable)
                if is_clickable:
                    score += 15
                else:
                    errors.append("not clickable")
                    score += 0
            except Exception:
                score += 5
        else:
            score += 15

        # 5. Negative keywords check (disqualifier)
        max_score += 10
        if profile.negative_keywords:
            try:
                text = (await locator.text_content() or "").lower()
                aria_label = (
                    await locator.get_attribute("aria-label") or ""
                ).lower()
                placeholder = (
                    await locator.get_attribute("placeholder") or ""
                ).lower()
                combined = f"{text} {aria_label} {placeholder}"

                has_negative = any(
                    kw.lower() in combined for kw in profile.negative_keywords
                )
                if has_negative:
                    errors.append("negative keyword found")
                    score += 0
                else:
                    score += 10
                    details["negative_kw"] = "clean"
            except Exception:
                score += 5
        else:
            score += 10

        # 6. Semantic keywords bonus
        max_score += 10
        if profile.semantic_keywords:
            try:
                text = (await locator.text_content() or "").lower()
                aria_label = (
                    await locator.get_attribute("aria-label") or ""
                ).lower()
                placeholder = (
                    await locator.get_attribute("placeholder") or ""
                ).lower()
                class_attr = (
                    await locator.get_attribute("class") or ""
                ).lower()
                combined = f"{text} {aria_label} {placeholder} {class_attr}"

                matched = sum(
                    1
                    for kw in profile.semantic_keywords
                    if kw.lower() in combined
                )
                if matched > 0:
                    bonus = min(10, matched * 5)
                    score += bonus
                    details["semantic_kw_matched"] = str(matched)
            except Exception:
                pass
        else:
            score += 10

        # 7. Expected attributes check
        max_score += 15
        if profile.expected_attributes:
            matched_attrs = 0
            total_attrs = len(profile.expected_attributes)
            for attr_name, attr_value in profile.expected_attributes.items():
                try:
                    actual = await locator.get_attribute(attr_name) or ""
                    if attr_value.lower() in actual.lower():
                        matched_attrs += 1
                except Exception:
                    pass
            if total_attrs > 0:
                attr_score = (matched_attrs / total_attrs) * 15
                score += attr_score
                details["attr_match"] = f"{matched_attrs}/{total_attrs}"
        else:
            score += 15

        # Calculate final confidence
        confidence = score / max_score if max_score > 0 else 0.0
        is_valid = confidence >= profile.min_confidence and not errors

        result = VerificationResult(
            is_valid=is_valid,
            confidence=round(confidence, 3),
            locator=locator,
            details=details,
            errors=errors,
        )

        logger.debug(
            "Element verification result",
            role=profile.role,
            confidence=result.confidence,
            is_valid=result.is_valid,
            details=result.details,
            errors=result.errors,
        )

        return result

    @staticmethod
    def _tag_is_related(actual: str, expected: str) -> bool:
        """Check if actual tag is semantically related to expected."""
        related = {
            "textarea": {"input", "div", "span"},
            "input": {"textarea", "div"},
            "button": {"a", "div", "span", "img"},
            "div": {"section", "article", "span"},
            "a": {"button", "span"},
        }
        return actual in related.get(expected, set())
