"""
Proactive baseline refresh — actual working baseline recapture.

Uses standalone browser sessions (like recon/auth bootstrap) to safely
refresh baselines without interfering with the main worker pool.

For each provider:
1. Opens a standalone headless browser
2. Checks for blocking states (login wall, captcha, blocked)
3. Navigates to chat, attempts controlled baseline capture for each role
4. Compares with existing baseline, records drift if detected
5. Updates baseline store and telemetry
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeout

from app.browser.dom.baseline import DOMBaseline
from app.browser.dom.diff import diff_against_baseline
from app.browser.dom.persistent_store import persistent_dom_store
from app.browser.dom.store import DriftRecord, baseline_store
from app.browser.dom.telemetry import dom_drift_telemetry
from app.browser.registry import registry as browser_registry
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """Result of a single provider refresh."""

    provider_id: str
    status: str  # success, partial, failed, aborted
    duration_ms: float
    roles_attempted: list[str] = field(default_factory=list)
    roles_refreshed: list[str] = field(default_factory=list)
    baseline_updates: int = 0
    drift_detected: bool = False
    drift_summary: str = ""
    abort_reason: str = ""
    blocking_state: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "status": self.status,
            "duration_ms": round(self.duration_ms, 1),
            "roles_attempted": self.roles_attempted,
            "roles_refreshed": self.roles_refreshed,
            "baseline_updates": self.baseline_updates,
            "drift_detected": self.drift_detected,
            "drift_summary": self.drift_summary,
            "abort_reason": self.abort_reason,
            "blocking_state": self.blocking_state,
            "error": self.error,
        }


class BaselineRefresher:
    """Controlled periodic baseline refresh using standalone browser sessions."""

    def __init__(
        self,
        enabled: bool = True,
        interval_seconds: float = 3600.0,
        max_concurrent: int = 2,
        timeout_per_provider: float = 30.0,
        timeout_per_role: float = 10.0,
    ) -> None:
        self.enabled = enabled
        self.interval_seconds = interval_seconds
        self.max_concurrent = max_concurrent
        self.timeout_per_provider = timeout_per_provider
        self.timeout_per_role = timeout_per_role
        self._task: asyncio.Task | None = None
        self._running = False
        self._refresh_semaphore: asyncio.Semaphore | None = None
        self._recent_results: list[dict[str, Any]] = []
        self._max_recent = 100
        self._stats: dict[str, dict[str, Any]] = {}

    async def start(self) -> None:
        """Start the background refresh loop."""
        if self._running or not self.enabled:
            return

        self._running = True
        self._refresh_semaphore = asyncio.Semaphore(self.max_concurrent)
        self._task = asyncio.create_task(self._refresh_loop())
        logger.info(
            "Baseline refresher started",
            interval_seconds=self.interval_seconds,
            max_concurrent=self.max_concurrent,
        )

    async def stop(self) -> None:
        """Stop the background refresh loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Baseline refresher stopped")

    async def _refresh_loop(self) -> None:
        """Main refresh loop."""
        while self._running:
            try:
                await asyncio.sleep(self.interval_seconds)
                if not self._running:
                    break
                await self._run_refresh_cycle()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(
                    "Baseline refresh loop error",
                    error=str(exc),
                )
                await asyncio.sleep(60)

    async def _run_refresh_cycle(self) -> list[RefreshResult]:
        """Run a full refresh cycle for all providers."""
        logger.info("Starting proactive baseline refresh cycle")
        start = time.monotonic()

        results = []
        provider_ids = list(browser_registry._providers.keys())

        tasks = [
            self._refresh_provider(pid)
            for pid in provider_ids
        ]

        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        for result in gathered:
            if isinstance(result, Exception):
                logger.error("Provider refresh failed", error=str(result))
            elif isinstance(result, RefreshResult):
                results.append(result)
                self._record_result(result)

        elapsed = time.monotonic() - start
        logger.info(
            "Proactive baseline refresh cycle completed",
            elapsed_ms=round(elapsed * 1000, 1),
            providers_refreshed=sum(1 for r in results if r.status == "success"),
            providers_partial=sum(1 for r in results if r.status == "partial"),
            providers_failed=sum(1 for r in results if r.status in ("failed", "aborted")),
        )
        return results

    async def _refresh_provider(self, provider_id: str) -> RefreshResult:
        """Refresh baseline for a single provider."""
        if self._refresh_semaphore:
            async with self._refresh_semaphore:
                return await self._do_refresh_provider(provider_id)
        return await self._do_refresh_provider(provider_id)

    async def _do_refresh_provider(self, provider_id: str) -> RefreshResult:
        """Actual provider refresh logic."""
        start = time.monotonic()
        roles_attempted: list[str] = []
        roles_refreshed: list[str] = []
        baseline_updates = 0
        drift_detected = False
        drift_summary = ""
        blocking_state = ""

        try:
            # Get provider class and config
            provider_class = browser_registry.get_provider_class(provider_id)
            provider_config = browser_registry.get_provider_config(provider_id)

            # Check if provider requires auth and has storage state
            storage_state_path = provider_config.get("storage_state_path") or settings.auth_storage_state_path
            needs_auth = provider_class.requires_auth

            if needs_auth and storage_state_path:
                import os
                if not os.path.exists(storage_state_path):
                    return RefreshResult(
                        provider_id=provider_id,
                        status="aborted",
                        duration_ms=(time.monotonic() - start) * 1000,
                        abort_reason="auth storage state not available",
                    )

            # Launch standalone browser
            pw = await async_playwright().start()
            browser = None
            context = None
            page = None

            try:
                launch_kwargs = {
                    "headless": True,
                    "slow_mo": 0,
                }
                browser = await pw.chromium.launch(**launch_kwargs)

                context_kwargs = {
                    "viewport": {"width": 1280, "height": 720},
                    "ignore_https_errors": True,
                    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                }
                if storage_state_path:
                    import os
                    if os.path.exists(storage_state_path):
                        context_kwargs["storage_state"] = storage_state_path

                context = await browser.new_context(**context_kwargs)
                page = await context.new_page()

                # Inject anti-detection
                await page.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )

                # Instantiate provider
                provider = provider_class(page, request_id="refresh", provider_config=provider_config)

                # Navigate to chat
                await provider.navigate_to_chat()

                # Check blocking states
                blocking_state = await self._detect_blocking_state(provider, page)
                if blocking_state:
                    return RefreshResult(
                        provider_id=provider_id,
                        status="aborted",
                        duration_ms=(time.monotonic() - start) * 1000,
                        blocking_state=blocking_state,
                        abort_reason=f"blocking state detected: {blocking_state}",
                    )

                # Refresh each role
                roles = ["message_input", "send_button", "assistant_message", "new_chat_button"]
                for role in roles:
                    role_result = await self._refresh_role(
                        provider, page, provider_id, role, provider_config
                    )
                    roles_attempted.append(role)
                    if role_result:
                        roles_refreshed.append(role)
                        if role_result.get("updated"):
                            baseline_updates += 1
                        if role_result.get("drift"):
                            drift_detected = True
                            drift_summary = role_result.get("drift_summary", "")

            finally:
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass
                if context:
                    try:
                        await context.close()
                    except Exception:
                        pass
                if browser:
                    try:
                        await browser.close()
                    except Exception:
                        pass
                await pw.stop()

            status = "success" if baseline_updates > 0 else ("partial" if roles_attempted else "failed")
            return RefreshResult(
                provider_id=provider_id,
                status=status,
                duration_ms=(time.monotonic() - start) * 1000,
                roles_attempted=roles_attempted,
                roles_refreshed=roles_refreshed,
                baseline_updates=baseline_updates,
                drift_detected=drift_detected,
                drift_summary=drift_summary,
            )

        except Exception as exc:
            return RefreshResult(
                provider_id=provider_id,
                status="failed",
                duration_ms=(time.monotonic() - start) * 1000,
                error=str(exc),
            )

    async def _refresh_role(
        self,
        provider,
        page: Page,
        provider_id: str,
        role: str,
        provider_config: dict,
    ) -> dict[str, Any] | None:
        """Attempt to capture/update baseline for a single role.

        Returns dict with 'updated', 'drift', 'drift_summary' or None if role not applicable.
        """
        from app.browser.healing.selector_profiles import build_provider_profiles

        profiles = build_provider_profiles(provider_id)
        profile = profiles.get(role)
        if profile is None or profile.min_confidence == 0:
            return None

        try:
            # Try to find the element using the provider's normal flow
            # For message_input: start_new_chat should make it visible
            if role == "message_input":
                try:
                    await asyncio.wait_for(
                        page.locator("textarea, [role=textbox], [contenteditable]").first.wait_for(state="visible", timeout=5000),
                        timeout=self.timeout_per_role,
                    )
                except (TimeoutError, PlaywrightTimeout, Exception):
                    return None

                locator = page.locator("textarea, [role=textbox]").first
                try:
                    await locator.wait_for(state="visible", timeout=2000)
                except Exception:
                    return None

            elif role == "send_button":
                # For providers that use Enter key (yandex), skip
                if provider_id == "yandex":
                    return None
                locator = page.locator("button, [role=button]").first
                try:
                    await locator.wait_for(state="visible", timeout=2000)
                except Exception:
                    return None

            elif role == "assistant_message":
                # Can't capture without sending a message — skip
                return None

            elif role == "new_chat_button":
                locator = page.locator("button, a").first
                try:
                    await locator.wait_for(state="visible", timeout=2000)
                except Exception:
                    return None

            else:
                return None

            # Capture baseline
            selector_str = await locator.evaluate(
                "el => { const s = el.tagName.toLowerCase(); const id = el.id ? '#' + el.id : ''; const cls = el.className ? '.' + el.className.split(' ')[0] : ''; return s + id + cls; }"
            )
            baseline = await DOMBaseline.from_locator(
                locator=locator,
                provider_id=provider_id,
                role=role,
                selector=selector_str,
                capture_reason="proactive_refresh",
                confidence=0.9,  # High confidence — we found it cleanly
            )

            existing = baseline_store.get_baseline(provider_id, role)
            updated = False
            drift = False
            drift_summary = ""

            # Check for drift if baseline exists
            if existing:
                existing_baseline = DOMBaseline(
                    provider_id=existing["provider_id"],
                    role=existing["role"],
                    selector=existing["selector"],
                    tag_name=existing.get("tag_name", ""),
                    aria_role=existing.get("aria_role", ""),
                    placeholder=existing.get("placeholder", ""),
                    aria_label=existing.get("aria_label", ""),
                    text_summary=existing.get("text_summary", ""),
                    parent_tag=existing.get("parent_tag", ""),
                    sibling_count=existing.get("sibling_count", 0),
                    is_visible=bool(existing.get("is_visible", True)),
                    is_editable=bool(existing.get("is_editable", False)),
                    is_clickable=bool(existing.get("is_clickable", False)),
                    capture_reason=existing.get("capture_reason", ""),
                    confidence=existing.get("confidence", 0.0),
                    version=existing.get("version", 1),
                )
                diff_result = diff_against_baseline(existing_baseline, baseline)
                if diff_result.has_drift:
                    drift = True
                    drift_summary = diff_result.human_summary[:100]
                    baseline_store.record_drift(DriftRecord(
                        provider_id=provider_id,
                        role=role,
                        timestamp=time.monotonic(),
                        diff_result=diff_result,
                        trigger="proactive_refresh",
                    ))
                    persistent_dom_store.save_drift_event({
                        "provider_id": provider_id,
                        "role": role,
                        "trigger": "proactive_refresh",
                        "drift_severity": diff_result.drift_severity,
                        "drift_score": diff_result.drift_score,
                        "human_summary": diff_result.human_summary,
                        "diff_json": {"events_count": len(diff_result.drift_events)},
                    })

            # Update baseline
            baseline_data = baseline.to_dict()
            baseline_data["version"] = existing["version"] + 1 if existing else 1
            persistent_dom_store.save_baseline(baseline_data)
            baseline_store.set_baseline(baseline, update_only_if_newer=False)
            updated = True
            dom_drift_telemetry.record_baseline_capture(provider_id, role, is_update=bool(existing))
            dom_drift_telemetry.update_baseline_age(provider_id, baseline.captured_at)

            return {
                "updated": updated,
                "drift": drift,
                "drift_summary": drift_summary,
            }

        except Exception as exc:
            logger.debug(
                "Role refresh failed",
                provider_id=provider_id,
                role=role,
                error=str(exc),
            )
            return None

    async def _detect_blocking_state(self, provider, page: Page) -> str:
        """Detect login wall, captcha, blocked states."""
        try:
            if await provider.detect_login_required():
                return "login_wall"
        except Exception:
            pass

        # Check for captcha
        try:
            captcha_selectors = [
                '[class*="captcha"]',
                '[class*="g-recaptcha"]',
                '[class*="h-captcha"]',
                'iframe[src*="recaptcha"]',
                'iframe[src*="hcaptcha"]',
            ]
            for sel in captcha_selectors:
                if await page.locator(sel).first.is_visible(timeout=500):
                    return "captcha"
        except Exception:
            pass

        # Check for blocked/access denied
        try:
            body_text = await page.locator("body").inner_text()
            blocked_indicators = ["access denied", "blocked", "forbidden", "rate limit", "too many requests"]
            for indicator in blocked_indicators:
                if indicator in body_text.lower():
                    return f"blocked: {indicator}"
        except Exception:
            pass

        # Check for modal blockers
        try:
            modal_selectors = ['[class*="modal"]', '[class*="dialog"]', '[class*="overlay"]', '[role="dialog"]']
            for sel in modal_selectors:
                if await page.locator(sel).first.is_visible(timeout=500):
                    return f"modal: {sel}"
        except Exception:
            pass

        return ""

    def _record_result(self, result: RefreshResult) -> None:
        """Record refresh result for stats and recent events."""
        self._recent_results.append(result.to_dict())
        if len(self._recent_results) > self._max_recent:
            self._recent_results = self._recent_results[-self._max_recent:]

        # Update per-provider stats
        stats = self._stats.setdefault(result.provider_id, {
            "attempts": 0,
            "successes": 0,
            "failures": 0,
            "aborted": 0,
            "partials": 0,
            "total_duration_ms": 0.0,
            "last_result": "",
            "last_at": 0.0,
        })
        stats["attempts"] += 1
        if result.status == "success":
            stats["successes"] += 1
        elif result.status == "failed":
            stats["failures"] += 1
        elif result.status == "aborted":
            stats["aborted"] += 1
        elif result.status == "partial":
            stats["partials"] += 1
        stats["total_duration_ms"] += result.duration_ms
        stats["last_result"] = result.status
        stats["last_at"] = time.monotonic()

    def get_recent_results(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._recent_results[-limit:]

    def get_stats(self, provider_id: str | None = None) -> dict[str, Any]:
        if provider_id:
            return self._stats.get(provider_id, {})
        return dict(self._stats)

    def get_summary(self) -> dict[str, Any]:
        total_attempts = sum(s["attempts"] for s in self._stats.values())
        total_successes = sum(s["successes"] for s in self._stats.values())
        total_failures = sum(s["failures"] for s in self._stats.values())
        total_aborted = sum(s["aborted"] for s in self._stats.values())
        total_partials = sum(s["partials"] for s in self._stats.values())
        total_duration = sum(s["total_duration_ms"] for s in self._stats.values())

        return {
            "total_attempts": total_attempts,
            "total_successes": total_successes,
            "total_failures": total_failures,
            "total_aborted": total_aborted,
            "total_partials": total_partials,
            "avg_duration_ms": round(total_duration / max(total_attempts, 1), 1),
            "per_provider": self._stats,
            "recent_results": self._recent_results[-10:],
        }


baseline_refresher = BaselineRefresher(
    enabled=True,
    interval_seconds=3600.0,
    max_concurrent=2,
    timeout_per_provider=30.0,
    timeout_per_role=10.0,
)
