"""
Runtime stats aggregator.

Collects and aggregates runtime statistics for models/providers from:
- usage_analytics (request counts, success rates, latencies)
- health_aggregator (selector health scores)
- circuit breaker state (consecutive failures, open/closed)

Provides a unified view: ModelRuntimeStats per model+provider.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.intelligence.types import ModelRuntimeStats

logger = get_logger(__name__)

# Recent window for "recent success rate" calculation
_RECENT_WINDOW = 10


class StatsAggregator:
    """Aggregates runtime stats from multiple data sources.

    Reads from existing analytics, health, and circuit breaker systems.
    Does not store its own data — always reflects current state of source systems.
    """

    def get_model_stats(
        self,
        model_id: str,
        provider_id: str,
        transport: str,
    ) -> ModelRuntimeStats:
        """Get runtime stats for a specific model+provider combination.

        Combines data from analytics, health, and circuit breaker.
        Returns sane defaults if data is unavailable.
        """
        stats = ModelRuntimeStats(
            model_id=model_id,
            provider_id=provider_id,
            transport=transport,
        )

        # Enrich from analytics
        self._enrich_from_analytics(stats)

        # Enrich from health aggregator
        self._enrich_from_health(stats)

        # Enrich from circuit breaker
        self._enrich_from_circuit_breaker(stats)

        return stats

    def get_all_model_stats(self) -> list[ModelRuntimeStats]:
        """Get runtime stats for all known models/providers.

        Collects candidates from all registries and builds stats.
        """
        from app.agents.registry import registry as agent_registry
        from app.browser.registry import registry as browser_registry
        from app.integrations.registry import api_registry

        all_stats: list[ModelRuntimeStats] = []

        # Browser models
        for m in browser_registry.list_models():
            if not m.get("enabled", True):
                continue
            stats = self.get_model_stats(
                model_id=m["id"],
                provider_id=m["provider_id"],
                transport="browser",
            )
            all_stats.append(stats)

        # API models
        for m in api_registry.list_models():
            if not m.get("enabled", True):
                continue
            stats = self.get_model_stats(
                model_id=m["id"],
                provider_id=m["provider_id"],
                transport="api",
            )
            all_stats.append(stats)

        # Agent models
        for m in agent_registry.list_models():
            if not m.get("enabled", True):
                continue
            stats = self.get_model_stats(
                model_id=m["id"],
                provider_id=m["provider_id"],
                transport="agent",
            )
            all_stats.append(stats)

        return all_stats

    def _enrich_from_analytics(self, stats: ModelRuntimeStats) -> None:
        """Enrich stats with data from usage_analytics."""
        try:
            from app.services.analytics_service import usage_analytics

            # Get provider-level analytics
            providers = usage_analytics.top_providers(50)
            provider_data = None
            for p in providers:
                if p.get("provider_id") == stats.provider_id:
                    provider_data = p
                    break

            if provider_data:
                stats.request_count = provider_data.get("request_count", 0)
                stats.success_count = provider_data.get("success_count", 0)
                stats.error_count = provider_data.get("error_count", 0)
                stats.fallback_count = provider_data.get("fallback_received", 0)

                # Calculate rates
                total = stats.request_count
                if total > 0:
                    stats.success_rate = stats.success_count / total
                    stats.failure_rate = stats.error_count / total
                    stats.fallback_rate = stats.fallback_count / total

                # Latency
                avg_lat = provider_data.get("avg_latency", 0)
                if avg_lat and avg_lat > 0:
                    stats.avg_latency_s = avg_lat
                    # Estimate p50/p95 from avg (rough approximation)
                    stats.p50_latency_s = avg_lat * 0.8
                    stats.p95_latency_s = avg_lat * 1.5

            # Get model-level analytics for stage-specific data
            models = usage_analytics.top_models(50)
            for m in models:
                if m.get("model_id") == stats.model_id:
                    # Update with model-level latencies if provider data missing
                    if stats.p50_latency_s == 0:
                        avg_lat = m.get("avg_latency", 0)
                        if avg_lat:
                            stats.p50_latency_s = avg_lat * 0.8
                            stats.p95_latency_s = avg_lat * 1.5
                            stats.avg_latency_s = avg_lat
                    break

        except Exception as exc:
            logger.debug("Failed to enrich from analytics", model=stats.model_id, error=str(exc))

    def _enrich_from_health(self, stats: ModelRuntimeStats) -> None:
        """Enrich stats with selector health scores from health_aggregator."""
        try:
            from app.browser.healing.health import health_aggregator

            if stats.transport != "browser":
                return

            degradation = health_aggregator.get_provider_degradation(stats.provider_id)
            if degradation is not None:
                stats.health_score = max(0.0, 1.0 - degradation)
            else:
                # No health data = assume healthy
                stats.health_score = 1.0

        except Exception as exc:
            logger.debug("Failed to enrich from health", provider=stats.provider_id, error=str(exc))

    def _enrich_from_circuit_breaker(self, stats: ModelRuntimeStats) -> None:
        """Enrich stats with circuit breaker state."""
        if stats.transport != "browser":
            return

        try:
            from app.browser.execution.dispatcher import browser_dispatcher

            pool = browser_dispatcher._pool
            health_ctrl = pool.provider_health
            snapshot = health_ctrl.snapshot()

            provider_snapshot = snapshot.get(stats.provider_id)
            if provider_snapshot:
                stats.consecutive_failures = provider_snapshot.get("consecutive_failures", 0)
                stats.circuit_open = provider_snapshot.get("is_open", False)

        except Exception as exc:
            logger.debug("Failed to enrich from circuit breaker", provider=stats.provider_id, error=str(exc))


# Global singleton
stats_aggregator = StatsAggregator()
