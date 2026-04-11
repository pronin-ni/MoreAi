"""
Scoring snapshot capture and trend analysis.

Captures periodic scoring snapshots across all models and roles.
Provides trend analysis: delta calculation, classification (improving/stable/declining/unstable),
and driver identification.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger
from app.pipeline.observability.scoring_history import (
    ScoringHistoryStore,
    ScoringSnapshot,
    scoring_history_store,
)

logger = get_logger(__name__)

# Trend windows (seconds)
WINDOW_1H = 3600
WINDOW_24H = 86400
WINDOW_7D = 604800

# Trend classification thresholds
_IMPROVING_DELTA = 0.05
_DECLINING_DELTA = -0.05
_UNSTABLE_STDDEV = 0.10


@dataclass(slots=True)
class TrendPoint:
    """A single trend data point for a metric over time."""

    metric: str  # e.g., "final_score", "success_rate", "fallback_rate", "avg_duration_ms"
    current_value: float
    previous_value: float
    delta: float
    delta_pct: float  # percentage change relative to previous_value
    trend: str  # "improving", "stable", "declining", "unstable"


@dataclass(slots=True)
class TrendSummary:
    """Trend summary for a model+provider+transport+role over a time window."""

    model_id: str
    provider_id: str
    transport: str
    role: str
    window_seconds: int
    window_label: str  # "1h", "24h", "7d"

    # Current state
    current_score: float = 0.0
    current_success_rate: float = 0.5
    current_fallback_rate: float = 0.0
    current_avg_duration_ms: float = 0.0
    current_sample_count: int = 0

    # Previous state (start of window)
    previous_score: float = 0.0
    previous_success_rate: float = 0.5
    previous_fallback_rate: float = 0.0
    previous_avg_duration_ms: float = 0.0

    # Deltas
    score_delta: float = 0.0
    success_rate_delta: float = 0.0
    fallback_rate_delta: float = 0.0
    duration_delta_ms: float = 0.0

    # Overall trend classification
    overall_trend: str = "stable"  # improving, stable, declining, unstable

    # Main driver of change
    main_driver: str = ""  # e.g., "success_rate_improved", "fallback_rate_worsened", etc.

    # Data quality
    data_points: int = 0
    has_enough_data: bool = False


class ScoringTrendAnalyzer:
    """Analyzes scoring history trends.

    Computes delta metrics and classifies trends for model+role combinations.
    """

    def __init__(
        self,
        store: ScoringHistoryStore | None = None,
    ) -> None:
        self._store = store or scoring_history_store

    def get_trend(
        self,
        model_id: str,
        provider_id: str = "",
        transport: str = "",
        role: str = "generate",
        window_seconds: int = WINDOW_24H,
    ) -> TrendSummary | None:
        """Get trend summary for a specific model+role over a time window.

        Args:
            model_id: The model to analyze.
            provider_id: Provider filter (empty = all providers).
            transport: Transport filter (empty = all transports).
            role: Stage role to analyze.
            window_seconds: Time window for comparison.

        Returns:
            TrendSummary or None if insufficient data.
        """
        history = self._store.get_history(
            model_id=model_id,
            role=role,
            window_seconds=window_seconds,
            limit=1000,
        )

        if len(history) < 2:
            return None

        # Filter by provider/transport if specified
        if provider_id:
            history = [h for h in history if h.provider_id == provider_id]
        if transport:
            history = [h for h in history if h.transport == transport]

        if len(history) < 2:
            return None

        # Current = most recent, Previous = oldest in window
        current = history[0]  # newest (DESC order)
        previous = history[-1]  # oldest

        window_label = self._format_window(window_seconds)

        summary = TrendSummary(
            model_id=model_id,
            provider_id=current.provider_id,
            transport=current.transport,
            role=role,
            window_seconds=window_seconds,
            window_label=window_label,
            current_score=current.final_score,
            current_success_rate=current.success_rate,
            current_fallback_rate=current.fallback_rate,
            current_avg_duration_ms=current.avg_duration_ms,
            current_sample_count=current.sample_count,
            previous_score=previous.final_score,
            previous_success_rate=previous.success_rate,
            previous_fallback_rate=previous.fallback_rate,
            previous_avg_duration_ms=previous.avg_duration_ms,
            score_delta=round(current.final_score - previous.final_score, 4),
            success_rate_delta=round(current.success_rate - previous.success_rate, 4),
            fallback_rate_delta=round(current.fallback_rate - previous.fallback_rate, 4),
            duration_delta_ms=round(current.avg_duration_ms - previous.avg_duration_ms, 1),
            data_points=len(history),
            has_enough_data=len(history) >= 3,
        )

        # Compute overall trend
        summary.overall_trend = self._classify_trend(summary, history)

        # Identify main driver
        summary.main_driver = self._identify_driver(summary)

        return summary

    def get_all_trends(
        self,
        role: str | None = None,
        window_seconds: int = WINDOW_24H,
    ) -> list[TrendSummary]:
        """Get trends for all models with history.

        Args:
            role: Filter by role (None = all roles).
            window_seconds: Time window.

        Returns:
            List of TrendSummary, sorted by score_delta descending.
        """
        models = self._store.get_distinct_models(role=role)

        results: list[TrendSummary] = []

        for mid in models:
            # We need provider/transport from history
            history = self._store.get_history(
                model_id=mid,
                role=role,
                window_seconds=window_seconds,
                limit=1,
            )
            if not history:
                continue

            latest = history[0]
            trend = self.get_trend(
                model_id=mid,
                provider_id=latest.provider_id,
                transport=latest.transport,
                role=latest.role,
                window_seconds=window_seconds,
            )
            if trend:
                results.append(trend)

        # Sort by score delta descending (top improvers first)
        results.sort(key=lambda t: t.score_delta, reverse=True)
        return results

    def get_top_improvers(
        self,
        role: str | None = None,
        window_seconds: int = WINDOW_24H,
        limit: int = 10,
    ) -> list[TrendSummary]:
        """Get top improving models."""
        trends = self.get_all_trends(role=role, window_seconds=window_seconds)
        return [t for t in trends if t.overall_trend == "improving"][:limit]

    def get_top_decliners(
        self,
        role: str | None = None,
        window_seconds: int = WINDOW_24H,
        limit: int = 10,
    ) -> list[TrendSummary]:
        """Get top declining models."""
        trends = self.get_all_trends(role=role, window_seconds=window_seconds)
        return [t for t in trends if t.overall_trend == "declining"][:limit]

    def get_unstable_models(
        self,
        role: str | None = None,
        window_seconds: int = WINDOW_24H,
        limit: int = 10,
    ) -> list[TrendSummary]:
        """Get models with unstable scoring."""
        trends = self.get_all_trends(role=role, window_seconds=window_seconds)
        return [t for t in trends if t.overall_trend == "unstable"][:limit]

    def capture_snapshot(self) -> int:
        """Capture a scoring snapshot for all current models and roles.

        Reads current scoring from the suitability scorer and writes to history.

        Returns:
            Number of snapshots written.
        """
        from app.agents.registry import registry as agent_registry
        from app.browser.registry import registry as browser_registry
        from app.integrations.registry import api_registry
        from app.intelligence.suitability import suitability_scorer
        from app.pipeline.observability.stage_perf import stage_performance as perf_tracker

        roles = ["generate", "review", "critique", "refine", "verify", "transform"]
        count = 0
        now = time.time()

        for reg, transport in [
            (browser_registry, "browser"),
            (api_registry, "api"),
            (agent_registry, "agent"),
        ]:
            for m in reg.list_models():
                if not m.get("enabled", True):
                    continue
                mid = m["id"]
                provider_id = m.get("provider_id", "")

                for role in roles:
                    try:
                        breakdown = suitability_scorer.compute_breakdown(
                            mid, provider_id, transport, role,
                        )
                        perf_stats = perf_tracker.get_model_role_stats(mid, role)

                        snapshot = ScoringSnapshot(
                            timestamp=now,
                            model_id=mid,
                            provider_id=provider_id,
                            transport=transport,
                            role=role,
                            final_score=round(breakdown.final_score, 4),
                            base_static_score=round(breakdown.base_static_score, 4),
                            dynamic_adjustment=round(breakdown.dynamic_adjustment, 4),
                            failure_penalty=round(breakdown.failure_penalty, 4),
                            success_rate=round(perf_stats.get("success_rate", 0.5), 4),
                            fallback_rate=round(perf_stats.get("fallback_rate", 0.0), 4),
                            avg_duration_ms=round(perf_stats.get("avg_duration_ms", 0.0), 1),
                            sample_count=perf_stats.get("sample_count", 0),
                            data_confidence=round(breakdown.data_confidence, 4),
                        )
                        self._store.record_snapshot(snapshot)
                        count += 1
                    except Exception as exc:
                        logger.debug(
                            "scoring_snapshot_failed",
                            model=mid,
                            provider=provider_id,
                            transport=transport,
                            role=role,
                            error=str(exc),
                        )

        if count > 0:
            logger.info(
                "scoring_snapshot_captured",
                snapshots=count,
            )
        return count

    # ── Internal classification logic ──

    def _classify_trend(
        self,
        summary: TrendSummary,
        history: list[ScoringSnapshot],
    ) -> str:
        """Classify trend as improving, stable, declining, or unstable.

        Uses score delta as the primary signal, with variance check for instability.
        """
        if not summary.has_enough_data:
            return "stable"  # Not enough data to classify

        # Check for instability: high variance in the score over time
        if len(history) >= 3:
            scores = [h.final_score for h in history]
            mean_score = sum(scores) / len(scores)
            variance = sum((s - mean_score) ** 2 for s in scores) / len(scores)
            stddev = variance ** 0.5
            if stddev > _UNSTABLE_STDDEV:
                return "unstable"

        # Primary classification by score delta
        if summary.score_delta >= _IMPROVING_DELTA:
            return "improving"
        if summary.score_delta <= _DECLINING_DELTA:
            return "declining"

        return "stable"

    def _identify_driver(self, summary: TrendSummary) -> str:
        """Identify the main driver of score change.

        Returns a human-readable string explaining what drove the change.
        """
        drivers: list[tuple[float, str]] = []

        # Success rate change (positive = good)
        sr_weight = 0.35
        drivers.append((summary.success_rate_delta * sr_weight, "success_rate"))

        # Fallback rate change (negative delta = good, positive = bad)
        fr_weight = 0.30
        drivers.append((-summary.fallback_rate_delta * fr_weight, "fallback_rate"))

        # Duration change (negative delta = faster = good)
        # Normalize: 1000ms = 0.1 score impact roughly
        dur_weight = 0.15
        dur_normalized = -summary.duration_delta_ms / 10000.0
        drivers.append((dur_normalized * dur_weight, "duration"))

        # Sort by absolute impact
        drivers.sort(key=lambda d: abs(d[0]), reverse=True)

        if not drivers:
            return "no_change"

        top_metric, impact = drivers[0]
        metric_name = impact

        if abs(top_metric) < 0.005:
            return "no_significant_change"

        if metric_name == "success_rate":
            return "success_rate_improved" if top_metric > 0 else "success_rate_worsened"
        if metric_name == "fallback_rate":
            return "fallback_rate_improved" if top_metric > 0 else "fallback_rate_worsened"
        if metric_name == "duration":
            return "duration_improved" if top_metric > 0 else "duration_worsened"

        return "scoring_adjustment"

    @staticmethod
    def _format_window(seconds: int) -> str:
        if seconds <= WINDOW_1H:
            return "1h"
        if seconds <= WINDOW_24H:
            return "24h"
        return "7d"


class SnapshotScheduler:
    """Manages periodic scoring snapshot capture.

    Provides a thread-safe bounded scheduler that captures snapshots
    at configurable intervals. Not a heavy cron — just a simple loop.
    """

    def __init__(
        self,
        analyzer: ScoringTrendAnalyzer | None = None,
        interval_seconds: int = 300,  # 5 minutes
    ) -> None:
        self._analyzer = analyzer or ScoringTrendAnalyzer()
        self._interval = interval_seconds
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_snapshot_time: float = 0.0
        self._last_snapshot_count: int = 0

    def start(self) -> None:
        """Start the background snapshot scheduler."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(
                target=self._run_loop,
                daemon=True,
                name="scoring-snapshot-scheduler",
            )
            self._thread.start()
            logger.info("scoring_snapshot_scheduler_started", interval_seconds=self._interval)

    def stop(self) -> None:
        """Stop the background scheduler."""
        with self._lock:
            self._running = False
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
            logger.info("scoring_snapshot_scheduler_stopped")

    def capture_now(self) -> int:
        """Trigger an immediate snapshot capture.

        Returns the number of snapshots recorded.
        """
        count = self._analyzer.capture_snapshot()
        with self._lock:
            self._last_snapshot_time = time.time()
            self._last_snapshot_count = count
        return count

    def get_status(self) -> dict[str, Any]:
        """Get scheduler status."""
        with self._lock:
            return {
                "running": self._running,
                "interval_seconds": self._interval,
                "last_snapshot_time": self._last_snapshot_time,
                "last_snapshot_count": self._last_snapshot_count,
            }

    def _run_loop(self) -> None:
        """Background snapshot loop."""
        while self._running:
            try:
                self.capture_now()
            except Exception as exc:
                logger.error("scoring_snapshot_schedule_error", error=str(exc))

            # Sleep in small increments so we can stop quickly
            for _ in range(self._interval):
                if not self._running:
                    break
                time.sleep(1)


# Global singletons
scoring_trend_analyzer = ScoringTrendAnalyzer()
snapshot_scheduler = SnapshotScheduler(analyzer=scoring_trend_analyzer)
