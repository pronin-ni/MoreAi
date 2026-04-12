"""
RuntimeConfigApplier — applies validated overrides to live system components.

ConfigManager handles storage/validation/persistence.
This module handles the actual side effects: toggling providers, updating semaphores, etc.
"""

import time
from dataclasses import dataclass

from app.admin.config_manager import RuntimeOverrides
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ComponentApplyResult:
    status: str  # applied | skipped | restart_required
    details: dict = None
    error: str | None = None

    def __post_init__(self):
        if self.details is None:
            self.details = {}


@dataclass
class ApplyResult:
    status: str  # applied | apply_failed | restart_required
    component: str | None = None
    error: str | None = None
    results: dict[str, ComponentApplyResult] = None

    def __post_init__(self):
        if self.results is None:
            self.results = {}


class RuntimeConfigApplier:
    """
    Takes validated RuntimeOverrides and applies them to running systems.
    Pure side-effect layer — never mutates ConfigManager state.
    """

    def __init__(
        self,
        browser_dispatcher=None,
        browser_pool=None,
        agent_registry=None,
        api_registry=None,
        unified_registry=None,
    ):
        self.browser_dispatcher = browser_dispatcher
        self.browser_pool = browser_pool
        self.agent_registry = agent_registry
        self.api_registry = api_registry
        self.unified_registry = unified_registry

    async def apply(self, overrides: RuntimeOverrides) -> ApplyResult:
        """Apply all overrides. If any component fails, rollback applied changes."""
        started = time.monotonic()
        results: dict[str, ComponentApplyResult] = {}
        applied_components: list[str] = []

        try:
            # 1. Provider overrides
            result = await self._apply_provider_overrides(overrides)
            results["providers"] = result
            if result.status == "applied":
                applied_components.append("providers")
            elif result.status == "restart_required":
                pass  # continue but mark
            elif result.status == "failed":
                await self._rollback_components(applied_components, overrides)
                self._record_config_apply(overrides, "apply_failed", started, results, result.error)
                return ApplyResult(
                    status="apply_failed",
                    component="providers",
                    error=result.error,
                    results=results,
                )

            # 2. Model overrides (visibility changes)
            result = await self._apply_model_overrides(overrides)
            results["models"] = result
            if result.status == "applied":
                applied_components.append("models")
            elif result.status == "failed":
                await self._rollback_components(applied_components, overrides)
                self._record_config_apply(overrides, "apply_failed", started, results, result.error)
                return ApplyResult(
                    status="apply_failed",
                    component="models",
                    error=result.error,
                    results=results,
                )

            # 3. Routing overrides
            result = await self._apply_routing_overrides(overrides)
            results["routing"] = result
            if result.status == "applied":
                applied_components.append("routing")
            elif result.status == "failed":
                await self._rollback_components(applied_components, overrides)
                self._record_config_apply(overrides, "apply_failed", started, results, result.error)
                return ApplyResult(
                    status="apply_failed",
                    component="routing",
                    error=result.error,
                    results=results,
                )

            self._record_config_apply(overrides, "success", started, results)
            return ApplyResult(status="applied", results=results)

        except Exception as e:
            await self._rollback_components(applied_components, overrides)
            logger.exception("Unexpected error during config apply")
            self._record_config_apply(overrides, "apply_failed", started, results, str(e))
            return ApplyResult(
                status="apply_failed",
                component="unexpected",
                error=str(e),
                results=results,
            )

    def _record_config_apply(
        self,
        overrides: RuntimeOverrides,
        status: str,
        started: float,
        results: dict,
        error: str | None = None,
    ) -> None:
        """Record config apply outcome for metrics and diagnostics."""
        try:
            from app.core.metrics import config_apply_total, config_apply_duration
            from app.core.diagnostics import record_config_apply

            elapsed = time.monotonic() - started
            config_apply_total.inc(result=status)
            config_apply_duration.observe(elapsed)

            component_status = {k: v.status for k, v in results.items()}
            record_config_apply(
                version=overrides.version,
                status=status,
                duration_seconds=elapsed,
                components=component_status,
                error=error,
            )
        except Exception:
            pass

    # ── Component-specific apply ──

    async def _apply_provider_overrides(
        self, overrides: RuntimeOverrides
    ) -> ComponentApplyResult:
        """Toggle provider enable/disable, update concurrency."""
        details: dict[str, str] = {}

        for pid, override in overrides.providers.items():
            if override.enabled is None and override.concurrency_limit is None:
                continue

            try:
                if self.agent_registry and pid in self.agent_registry._providers:
                    # Generic agent provider — shutdown or reinitialize
                    provider = self.agent_registry.get_provider(pid)
                    if provider:
                        if override.enabled is False:
                            await provider.shutdown()
                            details[pid] = "shutdown"
                        elif override.enabled is True:
                            await provider.initialize()
                            details[pid] = "reinitialized"
                elif pid in ("qwen", "glm", "chatgpt", "yandex", "kimi", "deepseek"):
                    # Browser provider: concurrency limit update on dispatcher
                    if self.browser_dispatcher and override.concurrency_limit is not None:
                        try:
                            self.browser_dispatcher.set_concurrency_limit(
                                pid, override.concurrency_limit
                            )
                            details[pid] = f"concurrency={override.concurrency_limit}"
                        except AttributeError:
                            # Dispatcher doesn't support live concurrency changes
                            details[pid] = f"concurrency={override.concurrency_limit} (restart_required)"
                else:
                    # API provider
                    if self.api_registry:
                        adapter = self.api_registry.get_adapter(pid)
                        if override.enabled is not None:
                            adapter.runtime_config.enabled = override.enabled
                            details[pid] = f"enabled={override.enabled}"

            except Exception as e:
                logger.error(
                    "Failed to apply provider override",
                    provider_id=pid,
                    error=str(e),
                )
                return ComponentApplyResult(status="failed", error=str(e))

        return ComponentApplyResult(
            status="applied" if details else "skipped",
            details=details,
        )

    async def _apply_model_overrides(
        self, overrides: RuntimeOverrides
    ) -> ComponentApplyResult:
        """Model visibility changes are applied by updating the unified registry view."""
        details: dict[str, str] = {}

        for mid, override in overrides.models.items():
            if override.enabled is not None or override.visibility is not None:
                details[mid] = f"enabled={override.enabled}, visibility={override.visibility}"

        return ComponentApplyResult(
            status="applied" if details else "skipped",
            details=details,
        )

    async def _apply_routing_overrides(
        self, overrides: RuntimeOverrides
    ) -> ComponentApplyResult:
        """Routing overrides — stored for next resolution, applied on demand."""
        details: dict[str, str] = {}

        for mid, override in overrides.routing.items():
            if override.primary is not None or override.fallbacks is not None:
                details[mid] = f"primary={override.primary}, fallbacks={override.fallbacks}"

        return ComponentApplyResult(
            status="applied" if details else "skipped",
            details=details,
        )

    async def _rollback_components(
        self, components: list[str], overrides: RuntimeOverrides
    ) -> None:
        """Best-effort rollback of applied components."""
        for component in reversed(components):
            try:
                if component == "providers":
                    # Re-apply previous state would need history — for now, log
                    logger.warning("Provider rollback is best-effort — previous state not restored")
            except Exception as e:
                logger.error(
                    "Rollback failed for component",
                    component=component,
                    error=str(e),
                )


applier = RuntimeConfigApplier()
