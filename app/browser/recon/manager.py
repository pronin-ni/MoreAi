"""
Recon manager — auto-recon recovery orchestration.

When a provider fails with a recon-eligible error, this manager:
1. Captures a runtime snapshot (URL, title, DOM context)
2. Detects blocking states (login wall, modals, overlays)
3. Re-scans critical roles via HealingEngine
4. Attempts recovery actions (focus input, new chat, soft reload)
5. Replays the failed action once

Guardrails:
- max 1 recon attempt per failure
- max time budget (configurable, default 3s)
- max 1 page reload
- max 1 deep DOM scan
- max 1 action replay
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from app.browser.healing.healing_engine import HealingEngine
from app.browser.healing.health import health_aggregator
from app.browser.healing.runtime_cache import healing_cache
from app.browser.healing.selector_profiles import build_provider_profiles
from app.browser.recon.policy import ReconPolicy, recon_policy
from app.browser.recon.telemetry import recon_telemetry
from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.browser.base import BrowserProvider

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ReconResult:
    """Result of a recon recovery attempt."""

    recovered: bool
    reason: str
    actions_performed: list[str] = field(default_factory=list)
    candidates_found: dict[str, Any] = field(default_factory=dict)
    blocking_state: str | None = None
    duration_ms: float = 0.0
    replay_succeeded: bool = False


@dataclass(frozen=True, slots=True)
class PageSnapshot:
    """Lightweight page state snapshot for recon."""

    url: str
    title: str
    has_textarea: bool
    has_button: bool
    textarea_count: int
    button_count: bool
    body_text_preview: str


class ReconManager:
    """Orchestrates auto-recon recovery for browser providers.

    Uses ReconPolicy for all decisions (eligibility, budget, actions, stops).
    """

    def __init__(
        self,
        provider: BrowserProvider,
        page: Page,
        request_id: str | None = None,
        *,
        policy: ReconPolicy | None = None,
    ) -> None:
        self.provider = provider
        self.page = page
        self.request_id = request_id
        self.policy = policy or recon_policy
        self._actions: list[str] = []
        self._start_time = time.monotonic()
        self._dom_scans_used = 0
        self._reloads_used = 0
        self._replays_used = 0

    @property
    def _elapsed_ms(self) -> float:
        return (time.monotonic() - self._start_time) * 1000

    @property
    def _budget_exceeded(self) -> bool:
        return self._elapsed_ms > self.policy.budget.max_time_ms

    @property
    def _can_scan(self) -> bool:
        return self._dom_scans_used < self.policy.budget.max_dom_scans and not self._budget_exceeded

    @property
    def _can_reload(self) -> bool:
        return (
            self._reloads_used < self.policy.budget.max_page_reloads
            and self.policy.action_policy.allow_soft_reload
            and not self._budget_exceeded
        )

    @property
    def _can_replay(self) -> bool:
        return self._replays_used < self.policy.budget.max_replay_attempts and not self._budget_exceeded

    async def run_recovery(
        self,
        *,
        failed_action: str,
        failed_error_type: str,
        failed_error_message: str,
        replay_fn: Callable[..., Awaitable[Any]],
        replay_args: tuple = (),
    ) -> ReconResult:
        """Execute the full recon recovery pipeline.

        Parameters
        ----------
        failed_action : human-readable action name ("send_message", "wait_for_response")
        failed_error_type : exception class name
        failed_error_message : str(exception)
        replay_fn : async function to replay after recovery
        replay_args : args to pass to replay_fn

        Returns
        -------
        ReconResult with recovery status and details.
        """
        logger.info(
            "Recon recovery started",
            provider_id=self.provider.provider_id,
            failed_action=failed_action,
            error_type=failed_error_type,
        )

        recon_telemetry.record_attempt(
            provider_id=self.provider.provider_id,
            failed_action=failed_action,
            error_type=failed_error_type,
        )

        try:
            # Step 1: Capture snapshot
            await self._capture_snapshot()
            self._actions.append("snapshot_captured")

            # Step 2: Detect blocking states
            blocking = await self._detect_blocking_state()
            if blocking:
                logger.info(
                    "Recon detected blocking state",
                    provider_id=self.provider.provider_id,
                    blocking_state=blocking,
                )
                # Check stop conditions via policy
                should_abort, abort_reason = self.policy.should_abort(blocking)
                if should_abort:
                    return self._result(
                        recovered=False,
                        reason=f"abort: {abort_reason}",
                        blocking_state=blocking,
                    )

            # Step 3: Re-scan critical roles via HealingEngine
            candidates = await self._rescan_critical_roles()
            self._actions.append("critical_roles_rescanned")

            if candidates:
                self._actions.append(f"candidates_found: {len(candidates)}")
                logger.info(
                    "Recon found candidates",
                    provider_id=self.provider.provider_id,
                    roles=list(candidates.keys()),
                )

            # Step 4: Recovery actions
            recovery_ok = await self._attempt_recovery_actions(blocking)
            if recovery_ok:
                self._actions.append("recovery_actions_succeeded")

            # Step 5: Replay failed action
            replay_ok = await self._replay_action(replay_fn, replay_args)
            if replay_ok:
                self._actions.append("replay_succeeded")
                duration = self._elapsed_ms
                recon_telemetry.record_success(
                    provider_id=self.provider.provider_id,
                    failed_action=failed_action,
                    duration_ms=round(duration, 1),
                    actions=self._actions,
                    candidates_count=sum(len(v) for v in candidates.values()) if candidates else 0,
                    trigger_reason=f"{failed_error_type}:{failed_action}",
                    blocking_state=blocking,
                    replay_succeeded=True,
                    recovered_roles=list(candidates.keys()) if candidates else [],
                )
                health_aggregator.record_recon(
                    self.provider.provider_id,
                    success=True,
                    duration_ms=round(duration, 1),
                )
                return self._result(
                    recovered=True,
                    reason="replay succeeded after recon",
                    candidates_found=candidates,
                    blocking_state=blocking,
                    replay_succeeded=True,
                )

            # Replay failed — check if we at least found candidates
            if candidates:
                duration = self._elapsed_ms
                recon_telemetry.record_partial(
                    provider_id=self.provider.provider_id,
                    failed_action=failed_action,
                    duration_ms=round(duration, 1),
                    reason="replay failed but candidates found",
                )
                health_aggregator.record_recon(
                    self.provider.provider_id,
                    success=False,
                    duration_ms=round(duration, 1),
                )
                return self._result(
                    recovered=False,
                    reason="replay failed but recon found viable candidates",
                    candidates_found=candidates,
                    blocking_state=blocking,
                )

            duration = self._elapsed_ms
            recon_telemetry.record_failure(
                provider_id=self.provider.provider_id,
                failed_action=failed_action,
                duration_ms=round(duration, 1),
                reason="no viable candidates and replay failed",
            )
            health_aggregator.record_recon(
                self.provider.provider_id,
                success=False,
                duration_ms=round(duration, 1),
            )
            return self._result(
                recovered=False,
                reason="recon failed: no viable candidates and replay failed",
                blocking_state=blocking,
            )

        except Exception as exc:
            duration = self._elapsed_ms
            recon_telemetry.record_failure(
                provider_id=self.provider.provider_id,
                failed_action=failed_action,
                duration_ms=round(duration, 1),
                reason=f"recon error: {exc}",
            )
            health_aggregator.record_recon(
                self.provider.provider_id,
                success=False,
                duration_ms=round(duration, 1),
            )
            logger.exception(
                "Recon recovery error",
                provider_id=self.provider.provider_id,
                error=str(exc),
            )
            return self._result(
                recovered=False,
                reason=f"recon error: {exc}",
            )

    # ── Step 1: Capture snapshot ──

    async def _capture_snapshot(self) -> PageSnapshot:
        """Capture lightweight page state snapshot."""
        try:
            url = self.page.url
            title = await self.page.title()
        except Exception:
            url = "unknown"
            title = "unknown"

        textarea_count = 0
        button_count = 0
        has_textarea = False
        has_button = False
        body_preview = ""

        try:
            textareas = await self.page.locator("textarea").all()
            textarea_count = len(textareas)
            has_textarea = textarea_count > 0
        except Exception:
            pass

        try:
            buttons = await self.page.locator("button, [role=button]").all()
            button_count = len(buttons)
            has_button = button_count > 0
        except Exception:
            pass

        try:
            body_text = await self.page.locator("body").inner_text()
            body_preview = body_text[:200] if body_text else ""
        except Exception:
            pass

        return PageSnapshot(
            url=url,
            title=title,
            has_textarea=has_textarea,
            has_button=has_button,
            textarea_count=textarea_count,
            button_count=button_count,
            body_text_preview=body_preview,
        )

    # ── Step 2: Detect blocking states ──

    async def _detect_blocking_state(self) -> str | None:
        """Detect login walls, modals, overlays, stale chat state."""
        # Check login wall
        try:
            if await self.provider.detect_login_required():
                return "login_wall"
        except Exception:
            pass

        # Check for modal/dialog overlays
        modal_selectors = [
            '[class*="modal"]',
            '[class*="dialog"]',
            '[class*="overlay"]',
            '[role="dialog"]',
            '[role="alertdialog"]',
        ]
        for sel in modal_selectors:
            try:
                modal = self.page.locator(sel).first
                if await modal.is_visible(timeout=500):
                    return f"modal: {sel}"
            except Exception:
                pass

        # Check for cookie/consent banners
        banner_selectors = [
            '[class*="cookie"]',
            '[class*="consent"]',
            '[class*="banner"]',
        ]
        for sel in banner_selectors:
            try:
                banner = self.page.locator(sel).first
                if await banner.is_visible(timeout=500):
                    return f"banner: {sel}"
            except Exception:
                pass

        # Check for stale/empty chat (no input at all)
        try:
            textareas = await self.page.locator("textarea").all()
            if len(textareas) == 0:
                # Check if there are any interactive elements
                interactive = await self.page.locator(
                    "textarea, button, input, [contenteditable]"
                ).all()
                if len(interactive) == 0:
                    return "stale_page"
        except Exception:
            pass

        return None

    # ── Step 3: Re-scan critical roles ──

    async def _rescan_critical_roles(self) -> dict[str, list[dict[str, Any]]]:
        """Re-scan critical element roles using HealingEngine.

        Returns dict of role -> list of candidate info.
        Respects policy budget (max_dom_scans, candidate_limit).
        """
        candidates: dict[str, list[dict[str, Any]]] = {}
        profiles = build_provider_profiles(self.provider.provider_id)
        candidate_limit = self.policy.budget.candidate_limit

        for role in ["message_input", "send_button", "assistant_message", "new_chat_button"]:
            profile = profiles.get(role)
            if profile is None:
                continue
            if profile.min_confidence == 0:
                continue  # Skip disabled roles (e.g., yandex send_button)

            if not self._can_scan:
                break

            self._dom_scans_used += 1
            engine = HealingEngine(self.page, self.provider.provider_id)
            try:
                found = await engine.scan(profile, max_candidates=candidate_limit)
                if found:
                    candidates[role] = [
                        {
                            "selector": c.selector_used,
                            "score": c.score,
                            "reason": c.reason,
                        }
                        for c in found[:3]  # Top 3 only
                    ]
                    # Update runtime cache with top candidate
                    top = found[0]
                    healing_cache.put(
                        self.provider.provider_id,
                        role,
                        top.selector_used,
                        meta={
                            "source": "recon",
                            "score": top.score,
                            "reason": top.reason,
                        },
                    )
            except Exception:
                continue

        return candidates

    # ── Step 4: Recovery actions ──

    async def _attempt_recovery_actions(self, blocking_state: str | None) -> bool:
        """Attempt recovery actions based on detected state.

        Respects action_policy.
        Returns True if at least one action succeeded.
        """
        any_ok = False

        # If we detected a banner, try to close it
        if blocking_state and "banner" in blocking_state and self.policy.is_action_allowed("banner_close"):
            try:
                close_btn = self.page.get_by_role("button", name="Accept all").first
                if await close_btn.is_visible(timeout=1000):
                    await close_btn.click()
                    await asyncio.sleep(0.5)
                    any_ok = True
                    self._actions.append("banner_closed")
            except Exception:
                pass

        # Try to focus the chat area
        if not self._budget_exceeded and self.policy.is_action_allowed("input_focus"):
            try:
                textareas = await self.page.locator("textarea").all()
                if textareas:
                    await textareas[0].focus()
                    any_ok = True
                    self._actions.append("input_focused")
            except Exception:
                pass

        # Soft page reload (respects budget and policy)
        if not any_ok and self._can_reload:
            try:
                self._reloads_used += 1
                await self.page.reload(wait_until="domcontentloaded", timeout=10000)
                await asyncio.sleep(1)
                any_ok = True
                self._actions.append("page_reloaded")
            except PlaywrightTimeout:
                logger.debug("Recon reload timed out")
            except Exception:
                pass

        return any_ok

    # ── Step 5: Replay ──

    async def _replay_action(
        self,
        replay_fn: Callable[..., Awaitable[Any]],
        replay_args: tuple = (),
    ) -> bool:
        """Replay the failed action once. Respects policy budget."""
        if not self._can_replay:
            return False

        self._replays_used += 1
        try:
            await replay_fn(*replay_args)
            return True
        except Exception as exc:
            logger.debug(
                "Recon replay failed",
                provider_id=self.provider.provider_id,
                error=str(exc),
            )
            return False

    # ── Helpers ──

    def _result(
        self,
        recovered: bool,
        reason: str,
        actions_performed: list[str] | None = None,
        candidates_found: dict[str, Any] | None = None,
        blocking_state: str | None = None,
        replay_succeeded: bool = False,
    ) -> ReconResult:
        return ReconResult(
            recovered=recovered,
            reason=reason,
            actions_performed=actions_performed or list(self._actions),
            candidates_found=candidates_found or {},
            blocking_state=blocking_state,
            duration_ms=round(self._elapsed_ms, 1),
            replay_succeeded=replay_succeeded,
        )


async def attempt_recon_recovery(
    provider: BrowserProvider,
    page: Page,
    *,
    request_id: str | None = None,
    failed_action: str,
    failed_error_type: str,
    failed_error_message: str,
    replay_fn: Callable[..., Awaitable[Any]],
    replay_args: tuple = (),
    policy: ReconPolicy | None = None,
) -> ReconResult:
    """Convenience function for recon recovery.

    Creates a ReconManager and runs the recovery pipeline.
    Uses the global recon policy by default.
    """
    effective_policy = policy or recon_policy

    # Check if recon is enabled
    if not effective_policy.enabled:
        return ReconResult(
            recovered=False,
            reason="recon disabled by policy",
            duration_ms=0.0,
        )

    # Check eligibility
    eligible, reason = effective_policy.is_eligible(failed_error_type, failed_error_message)
    if not eligible:
        return ReconResult(
            recovered=False,
            reason=f"not recon-eligible: {reason}",
            duration_ms=0.0,
        )

    manager = ReconManager(
        provider=provider,
        page=page,
        request_id=request_id,
        policy=effective_policy,
    )
    return await manager.run_recovery(
        failed_action=failed_action,
        failed_error_type=failed_error_type,
        failed_error_message=failed_error_message,
        replay_fn=replay_fn,
        replay_args=replay_args,
    )
