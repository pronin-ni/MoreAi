"""
DOM drift telemetry — metrics for baseline capture and drift detection.

Tracks:
- baseline_capture_total
- baseline_update_total
- drift_detected_total
- drift_by_provider
- drift_by_role
- drift_severity
- baseline_age
- last_drift_reason
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DriftStats:
    """Per-provider drift statistics."""

    provider_id: str
    baseline_capture_count: int = 0
    baseline_update_count: int = 0
    drift_detected_count: int = 0
    drift_severity_counts: dict[str, int] = field(default_factory=lambda: {"high": 0, "medium": 0, "low": 0})
    drift_by_role: dict[str, int] = field(default_factory=dict)
    last_drift_reason: str = ""
    last_drift_at: float = 0.0
    baseline_age_seconds: float = 0.0  # time since last baseline capture

    @property
    def drift_high_rate(self) -> float:
        total = self.drift_detected_count
        if total == 0:
            return 0.0
        return self.drift_severity_counts.get("high", 0) / total

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "baseline_capture_count": self.baseline_capture_count,
            "baseline_update_count": self.baseline_update_count,
            "drift_detected_count": self.drift_detected_count,
            "drift_severity_counts": dict(self.drift_severity_counts),
            "drift_high_rate": round(self.drift_high_rate, 3),
            "drift_by_role": dict(self.drift_by_role),
            "last_drift_reason": self.last_drift_reason,
            "last_drift_at": round(self.last_drift_at, 1) if self.last_drift_at else 0,
            "baseline_age_seconds": round(self.baseline_age_seconds, 1),
        }


class DOMDriftTelemetry:
    """In-process telemetry for DOM baseline and drift."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stats: dict[str, DriftStats] = {}
        self._recent_drift_events: list[dict[str, Any]] = []

    def record_baseline_capture(
        self, provider_id: str, role: str, is_update: bool = False
    ) -> None:
        with self._lock:
            s = self._get_stats(provider_id)
            if is_update:
                s.baseline_update_count += 1
            else:
                s.baseline_capture_count += 1

    def record_drift(
        self,
        provider_id: str,
        role: str,
        severity: str,
        reason: str,
    ) -> None:
        with self._lock:
            s = self._get_stats(provider_id)
            s.drift_detected_count += 1
            s.drift_severity_counts[severity] = s.drift_severity_counts.get(severity, 0) + 1
            s.drift_by_role[role] = s.drift_by_role.get(role, 0) + 1
            s.last_drift_reason = reason
            s.last_drift_at = time.monotonic()

            self._recent_drift_events.append({
                "provider_id": provider_id,
                "role": role,
                "severity": severity,
                "reason": reason,
                "timestamp": time.monotonic(),
            })
            if len(self._recent_drift_events) > 200:
                self._recent_drift_events = self._recent_drift_events[-200:]

    def update_baseline_age(self, provider_id: str, captured_at: float) -> None:
        with self._lock:
            s = self._get_stats(provider_id)
            s.baseline_age_seconds = time.monotonic() - captured_at

    def _get_stats(self, provider_id: str) -> DriftStats:
        if provider_id not in self._stats:
            self._stats[provider_id] = DriftStats(provider_id=provider_id)
        return self._stats[provider_id]

    def get_stats(self, provider_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            stats = list(self._stats.values())
            if provider_id:
                stats = [s for s in stats if s.provider_id == provider_id]
            return [s.to_dict() for s in stats]

    def get_recent_drift_events(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return self._recent_drift_events[-limit:]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            total_captures = sum(s.baseline_capture_count for s in self._stats.values())
            total_updates = sum(s.baseline_update_count for s in self._stats.values())
            total_drifts = sum(s.drift_detected_count for s in self._stats.values())

            return {
                "total_baseline_captures": total_captures,
                "total_baseline_updates": total_updates,
                "total_drift_events": total_drifts,
                "per_provider": self.get_stats(),
                "recent_drift_events": self._recent_drift_events[-10:],
            }

    def clear(self) -> None:
        with self._lock:
            self._stats.clear()
            self._recent_drift_events.clear()


dom_drift_telemetry = DOMDriftTelemetry()
