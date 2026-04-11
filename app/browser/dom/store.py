"""
DOM Baseline Store — in-memory storage for DOM baselines + drift events.

Stores:
- DOMBaseline per (provider_id, role)
- Drift events with timestamp, severity, diff result
- Baseline version tracking for controlled updates
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from app.browser.dom.baseline import DOMBaseline
from app.browser.dom.diff import DOMDiffResult
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class DriftRecord:
    """A recorded drift event."""

    provider_id: str
    role: str
    timestamp: float
    diff_result: DOMDiffResult
    trigger: str = ""  # "healing_failure", "recon_failure", "baseline_check"

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "role": self.role,
            "timestamp": round(self.timestamp, 1),
            "diff_result": self.diff_result.to_dict(),
            "trigger": self.trigger,
        }


class BaselineStore:
    """Thread-safe in-memory store for DOM baselines and drift events."""

    def __init__(self, max_drift_events: int = 500) -> None:
        self._baselines: dict[tuple[str, str], DOMBaseline] = {}
        self._drift_events: list[DriftRecord] = []
        self._max_drift_events = max_drift_events

    def get_baseline(self, provider_id: str, role: str) -> DOMBaseline | None:
        return self._baselines.get((provider_id, role))

    def set_baseline(
        self,
        baseline: DOMBaseline,
        update_only_if_newer: bool = True,
    ) -> bool:
        """Store or update a baseline.

        If update_only_if_newer, only updates if the new baseline has
        higher version or is significantly different from existing.
        """
        key = (baseline.provider_id, baseline.role)
        existing = self._baselines.get(key)

        if existing and update_only_if_newer:
            # Don't update if version is lower
            if baseline.version < existing.version:
                return False
            # Don't update if same version and confidence is lower
            if baseline.version == existing.version and baseline.confidence < existing.confidence:
                return False

        # Increment version if updating
        if existing:
            new_baseline = replace(baseline, version=existing.version + 1)
            self._baselines[key] = new_baseline
            logger.info(
                "DOM baseline updated",
                provider_id=baseline.provider_id,
                role=baseline.role,
                version=new_baseline.version,
                reason=baseline.capture_reason,
            )
        else:
            self._baselines[key] = baseline
            logger.info(
                "DOM baseline captured",
                provider_id=baseline.provider_id,
                role=baseline.role,
                reason=baseline.capture_reason,
            )
        return True

    def record_drift(self, record: DriftRecord) -> None:
        self._drift_events.append(record)
        if len(self._drift_events) > self._max_drift_events:
            self._drift_events = self._drift_events[-self._max_drift_events:]

    def get_drift_events(
        self,
        provider_id: str | None = None,
        role: str | None = None,
        limit: int = 50,
    ) -> list[DriftRecord]:
        events = self._drift_events
        if provider_id:
            events = [e for e in events if e.provider_id == provider_id]
        if role:
            events = [e for e in events if e.role == role]
        return events[-limit:]

    def get_baselines(
        self, provider_id: str | None = None
    ) -> list[DOMBaseline]:
        baselines = list(self._baselines.values())
        if provider_id:
            baselines = [b for b in baselines if b.provider_id == provider_id]
        return baselines

    def clear_baseline(self, provider_id: str, role: str | None = None) -> int:
        """Remove baseline(s). Returns count removed."""
        if role:
            key = (provider_id, role)
            if key in self._baselines:
                del self._baselines[key]
                return 1
            return 0
        else:
            to_remove = [k for k in self._baselines if k[0] == provider_id]
            for k in to_remove:
                del self._baselines[k]
            return len(to_remove)

    def clear_all(self) -> None:
        self._baselines.clear()
        self._drift_events.clear()

    def summary(self) -> dict[str, Any]:
        """Aggregate summary for diagnostics."""
        baselines = list(self._baselines.values())
        drift_events = self._drift_events

        by_provider: dict[str, int] = {}
        for b in baselines:
            by_provider[b.provider_id] = by_provider.get(b.provider_id, 0) + 1

        recent_drift = [e.to_dict() for e in drift_events[-10:]]

        return {
            "total_baselines": len(baselines),
            "providers_with_baselines": by_provider,
            "total_drift_events": len(drift_events),
            "recent_drift": recent_drift,
        }


baseline_store = BaselineStore()
