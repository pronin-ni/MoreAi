"""
Locator resolver — orchestrates primary → fallback → healing resolution.

This is the main entry point for self-healing selector resolution.
It:
1. Checks the runtime cache first
2. Tries primary selectors
3. Tries fallback selectors
4. If all fail and healing is enabled, invokes HealingEngine + ElementVerifier
5. Returns the first valid locator found
"""

from __future__ import annotations

import time

from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from app.browser.dom import baseline_store, dom_drift_telemetry
from app.browser.dom.baseline import DOMBaseline
from app.browser.dom.diff import diff_against_baseline
from app.browser.dom.store import DriftRecord
from app.browser.healing.element_verifier import ElementVerifier
from app.browser.healing.healing_engine import HealingEngine
from app.browser.healing.health import health_aggregator
from app.browser.healing.runtime_cache import healing_cache
from app.browser.healing.selector_profiles import SelectorProfile, build_provider_profiles
from app.browser.healing.telemetry import healing_telemetry
from app.core.logging import get_logger

logger = get_logger(__name__)


class LocatorResolver:
    """Resolves element locators with self-healing fallback."""

    PROMOTION_THRESHOLD = 5  # successes before promoting healed selector

    def __init__(
        self,
        page: Page,
        provider_id: str,
        *,
        healing_enabled: bool = True,
        timeout_ms: int = 2000,
        confidence_threshold: float = 0.6,
        max_healing_ms: float = 500.0,  # Max time spent in healing
        early_exit_confidence: float = 0.9,  # Accept immediately if above this
    ) -> None:
        self.page = page
        self.provider_id = provider_id
        self.healing_enabled = healing_enabled
        self.timeout_ms = timeout_ms
        self.confidence_threshold = confidence_threshold
        self.max_healing_ms = max_healing_ms
        self.early_exit_confidence = early_exit_confidence
        self._profiles = build_provider_profiles(provider_id)
        self._verifier = ElementVerifier()
        # Promotion tracking: {(provider_id, role, selector)} -> success_count
        self._promotion_tracker: dict[tuple[str, str, str], int] = {}
        # Promoted selectors: {(provider_id, role)} -> list of promoted selectors
        self._promoted_selectors: dict[tuple[str, str], list[str]] = {}

    def get_profile(self, role: str) -> SelectorProfile | None:
        """Get the selector profile for a role."""
        profile = self._profiles.get(role)
        if profile is None:
            return None

        # Add promoted selectors to the profile's primary list at runtime
        key = (self.provider_id, role)
        promoted = self._promoted_selectors.get(key, [])
        if promoted:
            # Create a new profile with promoted selectors prepended to primary
            from dataclasses import replace
            profile = replace(profile, primary=tuple(promoted) + profile.primary)

        return profile

    def _track_promotion(self, role: str, selector: str) -> None:
        """Track successful use of a healed selector for potential promotion."""
        key = (self.provider_id, role, selector)
        count = self._promotion_tracker.get(key, 0) + 1
        self._promotion_tracker[key] = count

        if count == self.PROMOTION_THRESHOLD:
            # Promote: add to primary list
            pkey = (self.provider_id, role)
            if pkey not in self._promoted_selectors:
                self._promoted_selectors[pkey] = []
            if selector not in self._promoted_selectors[pkey]:
                self._promoted_selectors[pkey].append(selector)
                logger.info(
                    "Selector promoted to primary",
                    provider_id=self.provider_id,
                    role=role,
                    selector=selector,
                    successes=count,
                )

    async def resolve(
        self,
        role: str,
        *,
        extra_selectors: list[str] | None = None,
        allow_healing: bool | None = None,
    ) -> Locator:
        """Resolve a locator for the given role.

        Order:
        1. Runtime cache check
        2. Primary selectors
        3. Fallback selectors
        4. Healing (if enabled and primary+fallback failed)

        Parameters
        ----------
        role : semantic role (message_input, send_button, …)
        extra_selectors : additional selectors to try before healing
        allow_healing : override instance healing_enabled

        Returns
        -------
        Locator for the found element.

        Raises
        ------
        LookupError if no valid element found.
        """
        profile = self._profiles.get(role)
        if profile is None:
            raise LookupError(f"No selector profile for role: {role}")

        allow = allow_healing if allow_healing is not None else self.healing_enabled

        # 1. Check runtime cache
        cached = healing_cache.get(self.provider_id, role)
        if cached is not None:
            try:
                cached_loc = self.page.locator(cached.selector)
                await cached_loc.wait_for(state="visible", timeout=1000)
                logger.debug(
                    "Using cached healed locator",
                    provider_id=self.provider_id,
                    role=role,
                    selector=cached.selector,
                )
                # Track for promotion (using cached selector counts as success)
                self._track_promotion(role, cached.selector)
                return cached_loc
            except Exception:
                healing_cache.invalidate(self.provider_id, role)
                logger.debug(
                    "Cached locator no longer valid, invalidated",
                    provider_id=self.provider_id,
                    role=role,
                )

        # 2. Try primary selectors
        primary_start = time.monotonic()
        if profile.primary:
            result = await self._try_selectors(
                profile.primary, profile, timeout_ms=self.timeout_ms
            )
            if result is not None:
                healing_telemetry.record_primary(self.provider_id, role, True)
                health_aggregator.update(self.provider_id, role, primary_success=True)
                elapsed = (time.monotonic() - primary_start) * 1000
                logger.info(
                    "Primary selector succeeded",
                    provider_id=self.provider_id,
                    role=role,
                    elapsed_ms=round(elapsed, 1),
                )
                # Capture baseline on primary success
                await self._maybe_capture_baseline(result, role, "primary_success")
                return result
            healing_telemetry.record_primary(self.provider_id, role, False)
            health_aggregator.update(self.provider_id, role, primary_success=False)

        # 3. Try fallback selectors
        fallback_start = time.monotonic()
        if profile.fallback:
            result = await self._try_selectors(
                profile.fallback, profile, timeout_ms=self.timeout_ms
            )
            if result is not None:
                healing_telemetry.record_fallback(self.provider_id, role, True)
                health_aggregator.update(self.provider_id, role, fallback_success=True)
                elapsed = (time.monotonic() - fallback_start) * 1000
                logger.info(
                    "Fallback selector succeeded",
                    provider_id=self.provider_id,
                    role=role,
                    elapsed_ms=round(elapsed, 1),
                )
                # Capture baseline on fallback success
                await self._maybe_capture_baseline(result, role, "fallback_success")
                return result
            healing_telemetry.record_fallback(self.provider_id, role, False)
            health_aggregator.update(self.provider_id, role, fallback_success=False)

        # 4. Try extra selectors
        if extra_selectors:
            result = await self._try_selectors(
                tuple(extra_selectors), profile, timeout_ms=self.timeout_ms
            )
            if result is not None:
                logger.info(
                    "Extra selector succeeded",
                    provider_id=self.provider_id,
                    role=role,
                )
                return result

        # 5. Healing
        if allow and profile.min_confidence > 0:
            return await self._heal(profile)

        raise LookupError(
            f"Element not found for role '{role}' on provider '{self.provider_id}'"
        )

    async def _try_selectors(
        self,
        selectors: tuple[str, ...],
        profile: SelectorProfile,
        *,
        timeout_ms: int,
    ) -> Locator | None:
        """Try a list of selectors, return the first visible one."""
        container = self.page
        if profile.container_selector:
            try:
                container = self.page.locator(profile.container_selector).first
                if not await container.is_visible(timeout=500):
                    container = self.page
            except Exception:
                container = self.page

        for selector in selectors:
            try:
                loc = self._resolve_selector(container, selector)
                await loc.wait_for(state="visible", timeout=timeout_ms)
                return loc
            except (PlaywrightTimeout, Exception):
                continue
        return None

    async def _heal(self, profile: SelectorProfile) -> Locator:
        """Attempt to heal by scanning DOM and verifying candidates.

        Performance guardrails:
        - max_healing_ms: total time budget for healing
        - early_exit_confidence: accept immediately if above threshold
        """
        start = time.monotonic()
        logger.info(
            "Healing invoked",
            provider_id=self.provider_id,
            role=profile.role,
            max_healing_ms=self.max_healing_ms,
        )

        engine = HealingEngine(self.page, self.provider_id)
        candidates = await engine.scan(profile, max_candidates=30)

        if not candidates:
            elapsed = (time.monotonic() - start) * 1000
            healing_telemetry.record_healing(
                self.provider_id,
                profile.role,
                success=False,
                confidence=0.0,
                elapsed_ms=round(elapsed, 1),
            )
            health_aggregator.update(
                self.provider_id, profile.role,
                healing_success=False, duration_ms=round(elapsed, 1),
            )
            raise LookupError(
                f"Healing failed: no candidates found for role '{profile.role}' "
                f"on provider '{self.provider_id}'"
            )

        best_result = None
        for candidate in candidates:
            # Performance guard: time budget
            elapsed_ms = (time.monotonic() - start) * 1000
            if elapsed_ms > self.max_healing_ms:
                logger.warning(
                    "Healing time budget exceeded",
                    provider_id=self.provider_id,
                    role=profile.role,
                    elapsed_ms=round(elapsed_ms, 1),
                    max_ms=self.max_healing_ms,
                )
                break

            try:
                verification = await self._verifier.verify(candidate.locator, profile)

                # Early exit: high confidence candidate
                if (
                    verification.is_valid
                    and verification.confidence >= self.early_exit_confidence
                ):
                    elapsed = (time.monotonic() - start) * 1000
                    elapsed_rounded = round(elapsed, 1)
                    logger.info(
                        "Healing early exit (high confidence)",
                        provider_id=self.provider_id,
                        role=profile.role,
                        confidence=verification.confidence,
                        elapsed_ms=round(elapsed, 1),
                    )
                    # Record and cache as usual
                    healing_telemetry.record_healing(
                        self.provider_id,
                        profile.role,
                        success=True,
                        confidence=verification.confidence,
                        elapsed_ms=elapsed_rounded,
                        candidate_info={
                            "selector": candidate.selector_used,
                            "score": candidate.score,
                            "verification_confidence": verification.confidence,
                            "reason": candidate.reason,
                            "details": verification.details,
                            "early_exit": True,
                        },
                    )
                    health_aggregator.update(
                        self.provider_id, profile.role,
                        healing_success=True,
                        confidence=verification.confidence,
                        duration_ms=elapsed_rounded,
                    )
                    healing_cache.put(
                        self.provider_id,
                        profile.role,
                        candidate.selector_used,
                        meta={
                            "confidence": verification.confidence,
                            "reason": candidate.reason,
                            "details": verification.details,
                            "early_exit": True,
                        },
                    )
                    # Track for promotion
                    self._track_promotion(profile.role, candidate.selector_used)
                    return verification.locator

                if verification.is_valid and verification.confidence >= profile.min_confidence:
                    elapsed = (time.monotonic() - start) * 1000
                    elapsed_rounded = round(elapsed, 1)
                    healing_telemetry.record_healing(
                        self.provider_id,
                        profile.role,
                        success=True,
                        confidence=verification.confidence,
                        elapsed_ms=elapsed_rounded,
                        candidate_info={
                            "selector": candidate.selector_used,
                            "score": candidate.score,
                            "verification_confidence": verification.confidence,
                            "reason": candidate.reason,
                            "details": verification.details,
                        },
                    )
                    health_aggregator.update(
                        self.provider_id, profile.role,
                        healing_success=True,
                        confidence=verification.confidence,
                        duration_ms=elapsed_rounded,
                    )

                    # Cache the successful locator
                    healing_cache.put(
                        self.provider_id,
                        profile.role,
                        candidate.selector_used,
                        meta={
                            "confidence": verification.confidence,
                            "reason": candidate.reason,
                            "details": verification.details,
                        },
                    )

                    logger.info(
                        "Healing succeeded",
                        provider_id=self.provider_id,
                        role=profile.role,
                        confidence=verification.confidence,
                        elapsed_ms=round(elapsed, 1),
                        selector=candidate.selector_used,
                    )

                    # Track for promotion
                    self._track_promotion(profile.role, candidate.selector_used)

                    # Capture baseline on healing success
                    await self._maybe_capture_baseline(
                        verification.locator, profile.role, "healing_success",
                        confidence=verification.confidence,
                    )

                    return verification.locator

                # Track best attempt
                if best_result is None or verification.confidence > best_result[0]:
                    best_result = (verification.confidence, candidate)
            except Exception:
                continue

        # No candidate passed verification
        elapsed = (time.monotonic() - start) * 1000
        best_conf = best_result[0] if best_result else 0.0
        elapsed_rounded = round(elapsed, 1)
        healing_telemetry.record_healing(
            self.provider_id,
            profile.role,
            success=False,
            confidence=best_conf,
            elapsed_ms=elapsed_rounded,
        )
        health_aggregator.update(
            self.provider_id, profile.role,
            healing_success=False,
            confidence=best_conf,
            duration_ms=elapsed_rounded,
        )

        raise LookupError(
            f"Healing failed: best candidate confidence {best_conf:.3f} "
            f"below threshold {profile.min_confidence:.3f} "
            f"for role '{profile.role}' on provider '{self.provider_id}'"
        )

    async def _maybe_capture_baseline(
        self,
        locator: Locator,
        role: str,
        reason: str,
        *,
        confidence: float = 0.0,
    ) -> None:
        """Capture DOM baseline if no baseline exists or if confidence is high.

        This is called after successful resolution (primary, fallback, healing).
        Baseline is captured only if:
        - No baseline exists yet, or
        - Confidence is high (>= 0.85) — indicates strong match
        """
        existing = baseline_store.get_baseline(self.provider_id, role)
        is_update = existing is not None

        # Only capture if no baseline exists OR confidence is very high
        if existing and confidence < 0.85:
            return

        try:
            baseline = await DOMBaseline.from_locator(
                locator=locator,
                provider_id=self.provider_id,
                role=role,
                selector=await locator.evaluate("el => { const s = el.tagName.toLowerCase(); const id = el.id ? '#' + el.id : ''; const cls = el.className ? '.' + el.className.split(' ')[0] : ''; return s + id + cls; }"),
                capture_reason=reason,
                confidence=confidence,
                version=existing.version + 1 if existing else 1,
            )

            if baseline_store.set_baseline(baseline, update_only_if_newer=True):
                dom_drift_telemetry.record_baseline_capture(
                    self.provider_id, role, is_update=is_update
                )
                dom_drift_telemetry.update_baseline_age(
                    self.provider_id, baseline.captured_at
                )

                # If updating, check for drift against old baseline
                if existing:
                    diff_result = diff_against_baseline(existing, baseline)
                    if diff_result.has_drift:
                        baseline_store.record_drift(DriftRecord(
                            provider_id=self.provider_id,
                            role=role,
                            timestamp=time.monotonic(),
                            diff_result=diff_result,
                            trigger=f"baseline_update_{reason}",
                        ))
                        dom_drift_telemetry.record_drift(
                            self.provider_id, role,
                            severity=diff_result.drift_severity,
                            reason=diff_result.human_summary,
                        )
        except Exception as exc:
            logger.debug(
                "Baseline capture failed",
                provider_id=self.provider_id,
                role=role,
                error=str(exc),
            )

    @staticmethod
    def _resolve_selector(container: Locator | Page, selector: str) -> Locator:
        """Resolve a selector string to a Playwright Locator."""
        if selector.startswith("role="):
            # Parse role=name format
            parts = selector.split("=", 1)
            role_name = parts[1] if len(parts) > 1 else ""
            if "[" in role_name and "]" in role_name:
                # role=textbox[name="..."]
                name_start = role_name.index("[name=") + 6
                name_end = role_name.rindex("]")
                name = role_name[name_start:name_end].strip('"')
                return container.get_by_role(parts[0], name=name)
            return container.get_by_role(role_name)
        elif selector.startswith("tag="):
            tag = selector[4:]
            return container.locator(tag)
        else:
            return container.locator(selector)


def create_resolver(
    page: Page,
    provider_id: str,
    *,
    healing_enabled: bool = True,
    timeout_ms: int = 2000,
    confidence_threshold: float = 0.6,
) -> LocatorResolver:
    """Factory function for LocatorResolver."""
    return LocatorResolver(
        page=page,
        provider_id=provider_id,
        healing_enabled=healing_enabled,
        timeout_ms=timeout_ms,
        confidence_threshold=confidence_threshold,
    )
