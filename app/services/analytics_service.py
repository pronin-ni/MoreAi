"""
Usage analytics service — aggregates from persistent store.

Provides simple, queryable summaries for:
- Top models by request count
- Top providers by request count
- Error rates by provider/model
- Fallback frequency
- Latency distributions
- Time-windowed aggregation (last hour, last day, last week)

Uses SQLite persistent store with in-memory cache for fast queries.
"""

from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


class UsageAnalytics:
    """Usage analytics aggregator backed by persistent storage.

    Writes events to SQLite for durability, and maintains in-memory
    counters for fast real-time queries.
    """

    def __init__(self, window_seconds: float = 3600) -> None:
        self._window = window_seconds  # in-memory window for fast queries
        self._lock = Lock()
        self._model_counters: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "provider_id": "",
                "transport": "",
                "request_count": 0,
                "success_count": 0,
                "error_count": 0,
                "fallback_count": 0,
                "latencies": [],
                "last_request_at": 0.0,
            }
        )
        self._provider_counters: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "transport": "",
                "request_count": 0,
                "success_count": 0,
                "error_count": 0,
                "fallback_received": 0,
                "fallback_sent": 0,
                "latencies": [],
                "circuit_open_count": 0,
            }
        )

    def record_request(
        self,
        *,
        model: str,
        provider: str,
        transport: str,
        status: str,
        latency_seconds: float = 0.0,
        error_type: str | None = None,
        is_fallback: bool = False,
        fallback_from: str | None = None,
        tenant_id: str = "",
    ) -> None:
        """Record a single request outcome."""
        now = time.time()
        event = {
            "ts": now,
            "model": model,
            "provider": provider,
            "transport": transport,
            "status": status,
            "latency_seconds": latency_seconds,
            "error_type": error_type,
            "is_fallback": is_fallback,
            "fallback_from": fallback_from,
            "tenant_id": tenant_id,
        }

        # Persist to SQLite
        try:
            from app.core.persistent_store import persistent_store
            persistent_store.record_analytics_event(event)
        except Exception:
            pass  # Analytics must never break the request flow

        # Update in-memory counters
        with self._lock:
            mc = self._model_counters[model]
            mc["provider_id"] = provider
            mc["transport"] = transport
            mc["request_count"] += 1
            mc["last_request_at"] = now
            if status == "success":
                mc["success_count"] += 1
            elif status == "error":
                mc["error_count"] += 1
            elif status == "fallback":
                mc["fallback_count"] += 1
            if latency_seconds > 0:
                mc["latencies"].append(latency_seconds)
                # Trim old latencies to prevent unbounded growth
                if len(mc["latencies"]) > 5000:
                    mc["latencies"] = mc["latencies"][-5000:]

            pc = self._provider_counters[provider]
            pc["transport"] = transport
            pc["request_count"] += 1
            if status == "success":
                pc["success_count"] += 1
            elif status == "error":
                pc["error_count"] += 1
            if latency_seconds > 0:
                pc["latencies"].append(latency_seconds)
                if len(pc["latencies"]) > 5000:
                    pc["latencies"] = pc["latencies"][-5000:]
            if is_fallback and fallback_from:
                pc["fallback_received"] += 1
                self._provider_counters[fallback_from]["fallback_sent"] += 1

    def record_circuit_open(self, provider: str) -> None:
        with self._lock:
            self._provider_counters[provider]["circuit_open_count"] += 1

    def top_models(self, limit: int = 20, since: float | None = None) -> list[dict[str, Any]]:
        """Return top models by request count."""
        if since is None:
            since = time.time() - self._window

        # Try persistent store for historical data
        try:
            from app.core.persistent_store import persistent_store
            results = persistent_store.aggregate_top_models(since=since, limit=limit)
            if results:
                return results
        except Exception:
            pass

        # Fallback to in-memory
        with self._lock:
            entries = []
            for model_id, mc in self._model_counters.items():
                latencies = sorted(mc["latencies"])
                p50 = _percentile(latencies, 50) if latencies else 0.0
                p95 = _percentile(latencies, 95) if latencies else 0.0
                entries.append({
                    "model_id": model_id,
                    "provider_id": mc["provider_id"],
                    "transport": mc["transport"],
                    "request_count": mc["request_count"],
                    "success_count": mc["success_count"],
                    "error_count": mc["error_count"],
                    "fallback_count": mc["fallback_count"],
                    "error_rate": _rate(mc["error_count"], mc["request_count"]),
                    "p50_latency_seconds": round(p50, 3),
                    "p95_latency_seconds": round(p95, 3),
                    "last_request_at": mc["last_request_at"],
                })
            entries.sort(key=lambda e: e["request_count"], reverse=True)
            return entries[:limit]

    def top_providers(self, limit: int = 20, since: float | None = None) -> list[dict[str, Any]]:
        """Return top providers by request count."""
        if since is None:
            since = time.time() - self._window

        try:
            from app.core.persistent_store import persistent_store
            results = persistent_store.aggregate_top_providers(since=since, limit=limit)
            if results:
                return results
        except Exception:
            pass

        with self._lock:
            entries = []
            for provider_id, pc in self._provider_counters.items():
                latencies = sorted(pc["latencies"])
                avg = sum(latencies) / len(latencies) if latencies else 0.0
                entries.append({
                    "provider_id": provider_id,
                    "transport": pc["transport"],
                    "request_count": pc["request_count"],
                    "success_count": pc["success_count"],
                    "error_count": pc["error_count"],
                    "error_rate": _rate(pc["error_count"], pc["request_count"]),
                    "fallback_received": pc["fallback_received"],
                    "fallback_sent": pc["fallback_sent"],
                    "avg_latency_seconds": round(avg, 3),
                    "circuit_open_count": pc["circuit_open_count"],
                })
            entries.sort(key=lambda e: e["request_count"], reverse=True)
            return entries[:limit]

    def error_summary(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return error breakdown by type/provider/model."""
        try:
            from app.core.persistent_store import persistent_store
            events = persistent_store.query_analytics(since=time.time() - self._window, limit=5000)
            error_counts: dict[tuple[str, str, str], int] = defaultdict(int)
            for event in events:
                if event["status"] == "error" and event.get("error_type"):
                    key = (event["error_type"], event["provider"], event["model"])
                    error_counts[key] += 1
            entries = [
                {"error_type": k[0], "provider_id": k[1], "model_id": k[2], "count": v}
                for k, v in error_counts.items()
            ]
            entries.sort(key=lambda e: e["count"], reverse=True)
            return entries[:limit]
        except Exception:
            return []

    def fallback_summary(self) -> dict[str, Any]:
        """Return fallback statistics."""
        try:
            from app.core.persistent_store import persistent_store
            events = persistent_store.query_analytics(since=time.time() - self._window, limit=5000)
            total_fallbacks = 0
            fallback_pairs: dict[tuple[str, str], int] = defaultdict(int)
            fallback_by_reason: dict[str, int] = defaultdict(int)

            for event in events:
                if event.get("is_fallback") and event.get("fallback_from"):
                    total_fallbacks += 1
                    pair = (event["fallback_from"], event["provider"])
                    fallback_pairs[pair] += 1
                    if event.get("error_type"):
                        fallback_by_reason[event["error_type"]] += 1

            top_pairs = [
                {"from_provider": k[0], "to_provider": k[1], "count": v}
                for k, v in sorted(fallback_pairs.items(), key=lambda x: x[1], reverse=True)[:10]
            ]

            return {
                "total_fallbacks": total_fallbacks,
                "top_fallback_pairs": top_pairs,
                "fallback_by_reason": dict(fallback_by_reason),
            }
        except Exception:
            return {"total_fallbacks": 0, "top_fallback_pairs": [], "fallback_by_reason": {}}

    def activity_timeline(self, bucket_seconds: int = 300) -> list[dict[str, Any]]:
        """Return request count over time in fixed-size buckets."""
        try:
            from app.core.persistent_store import persistent_store
            events = persistent_store.query_analytics(
                since=time.time() - 86400, limit=50000
            )
        except Exception:
            events = []

        now = time.time()
        buckets: dict[int, dict[str, int]] = defaultdict(
            lambda: {"requests": 0, "success": 0, "error": 0, "fallback": 0}
        )
        for event in events:
            age = now - event["ts"]
            if age > 86400:
                continue
            bucket_idx = int((now - age) // bucket_seconds)
            b = buckets[bucket_idx]
            b["requests"] += 1
            status = event["status"]
            if status in b:
                b[status] += 1

        return [
            {"bucket_ts": k * bucket_seconds, **v}
            for k, v in sorted(buckets.items())
        ]

    def export_all(self) -> dict[str, Any]:
        """Full analytics export."""
        return {
            "top_models": self.top_models(limit=50),
            "top_providers": self.top_providers(limit=50),
            "error_summary": self.error_summary(),
            "fallback_summary": self.fallback_summary(),
            "activity_timeline": self.activity_timeline(),
        }

    def reset(self) -> None:
        """Clear all in-memory analytics data."""
        with self._lock:
            self._model_counters.clear()
            self._provider_counters.clear()

    def cleanup(self, max_age_seconds: float = 86400 * 30) -> int:
        """Clean up old analytics data."""
        try:
            from app.core.persistent_store import persistent_store
            return persistent_store.cleanup_old_analytics(max_age_seconds)
        except Exception:
            return 0


def _rate(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 4)


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    idx = int(len(sorted_values) * pct / 100)
    idx = min(idx, len(sorted_values) - 1)
    return sorted_values[idx]


usage_analytics = UsageAnalytics()
