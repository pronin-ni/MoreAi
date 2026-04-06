"""
Selector health scoring — aggregated metrics per provider+role.

Computes:
- primary_success_rate
- fallback_success_rate
- healing_usage_rate
- healing_success_rate
- avg_confidence
- failure_rate
- selector_health_score (0.0–1.0)
- status: healthy / degrading / broken
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SelectorHealthScore:
    """Aggregated health for a provider+role."""

    provider_id: str
    role: str
    primary_attempts: int = 0
    primary_successes: int = 0
    fallback_attempts: int = 0
    fallback_successes: int = 0
    healing_invoked: int = 0
    healing_successes: int = 0
    total_duration_ms: float = 0.0
    avg_confidence: float = 0.0
    best_confidence: float = 0.0
    worst_confidence: float = 1.0
    last_updated: float = 0.0

    @property
    def primary_success_rate(self) -> float:
        if self.primary_attempts == 0:
            return 1.0  # No attempts = assumed healthy
        return self.primary_successes / self.primary_attempts

    @property
    def fallback_success_rate(self) -> float:
        if self.fallback_attempts == 0:
            return 0.0
        return self.fallback_successes / self.fallback_attempts

    @property
    def healing_usage_rate(self) -> float:
        total = self.primary_attempts + self.fallback_attempts
        if total == 0:
            return 0.0
        return self.healing_invoked / total

    @property
    def healing_success_rate(self) -> float:
        if self.healing_invoked == 0:
            return 0.0
        return self.healing_successes / self.healing_invoked

    @property
    def failure_rate(self) -> float:
        total = self.primary_attempts + self.fallback_attempts
        if total == 0:
            return 0.0
        failures = (self.primary_attempts - self.primary_successes) + \
                   (self.fallback_attempts - self.fallback_successes)
        return failures / total

    @property
    def health_score(self) -> float:
        """Composite health score 0.0–1.0.

        Formula:
        - 50% weight: primary_success_rate (main signal)
        - 20% weight: (1 - healing_usage_rate) (less healing = better)
        - 15% weight: healing_success_rate (if healing used, is it reliable?)
        - 15% weight: avg_confidence (quality of recovered elements)
        """
        w1 = self.primary_success_rate * 0.50
        w2 = (1.0 - min(self.healing_usage_rate, 1.0)) * 0.20
        w3 = self.healing_success_rate * 0.15 if self.healing_invoked > 0 else 0.15
        w4 = self.avg_confidence * 0.15 if self.avg_confidence > 0 else 0.15
        return round(min(w1 + w2 + w3 + w4, 1.0), 3)

    @property
    def status(self) -> str:
        score = self.health_score
        if score >= 0.8:
            return "healthy"
        elif score >= 0.5:
            return "degrading"
        else:
            return "broken"

    def to_dict(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "role": self.role,
            "health_score": self.health_score,
            "status": self.status,
            "primary_success_rate": round(self.primary_success_rate, 3),
            "primary_attempts": self.primary_attempts,
            "fallback_success_rate": round(self.fallback_success_rate, 3),
            "fallback_attempts": self.fallback_attempts,
            "healing_usage_rate": round(self.healing_usage_rate, 3),
            "healing_success_rate": round(self.healing_success_rate, 3),
            "healing_invoked": self.healing_invoked,
            "failure_rate": round(self.failure_rate, 3),
            "avg_confidence": self.avg_confidence,
            "best_confidence": self.best_confidence,
            "worst_confidence": self.worst_confidence,
            "total_duration_ms": round(self.total_duration_ms, 1),
            "last_updated": round(self.last_updated, 1),
        }


class HealthAggregator:
    """Aggregates healing telemetry into health scores."""

    def __init__(self) -> None:
        self._scores: dict[tuple[str, str], SelectorHealthScore] = {}

    def update(
        self,
        provider_id: str,
        role: str,
        *,
        primary_success: bool | None = None,
        fallback_success: bool | None = None,
        healing_success: bool | None = None,
        confidence: float = 0.0,
        duration_ms: float = 0.0,
    ) -> None:
        key = (provider_id, role)
        if key not in self._scores:
            self._scores[key] = SelectorHealthScore(
                provider_id=provider_id,
                role=role,
                last_updated=time.monotonic(),
            )

        s = self._scores[key]
        s = self._apply_updates(s, primary_success, fallback_success, healing_success, confidence, duration_ms)
        self._scores[key] = s

    def get(self, provider_id: str, role: str) -> SelectorHealthScore | None:
        return self._scores.get((provider_id, role))

    def get_all(self, provider_id: str | None = None) -> list[SelectorHealthScore]:
        result = list(self._scores.values())
        if provider_id:
            result = [s for s in result if s.provider_id == provider_id]
        return result

    def get_provider_degradation(self, provider_id: str) -> float:
        """Return max degradation signal for a provider (0.0=healthy, 1.0=fully degraded)."""
        scores = self.get_all(provider_id)
        if not scores:
            return 0.0
        # Degradation = 1 - health_score
        return max(1.0 - s.health_score for s in scores)

    def clear(self) -> None:
        self._scores.clear()

    @staticmethod
    def _apply_updates(
        s: SelectorHealthScore,
        primary_success: bool | None,
        fallback_success: bool | None,
        healing_success: bool | None,
        confidence: float,
        duration_ms: float,
    ) -> SelectorHealthScore:
        updates: dict = {
            "last_updated": time.monotonic(),
        }
        if primary_success is not None:
            updates["primary_attempts"] = s.primary_attempts + 1
            updates["primary_successes"] = s.primary_successes + (1 if primary_success else 0)
        if fallback_success is not None:
            updates["fallback_attempts"] = s.fallback_attempts + 1
            updates["fallback_successes"] = s.fallback_successes + (1 if fallback_success else 0)
        if healing_success is not None:
            updates["healing_invoked"] = s.healing_invoked + 1
            updates["healing_successes"] = s.healing_successes + (1 if healing_success else 0)
        if confidence > 0:
            count = s.healing_successes + (1 if (healing_success is True) else 0)
            if count > 0:
                old_sum = s.avg_confidence * (count - (1 if healing_success is True else 0))
                new_avg = (old_sum + confidence) / count
                updates["avg_confidence"] = round(new_avg, 3)
                updates["best_confidence"] = max(s.best_confidence, confidence)
                updates["worst_confidence"] = min(s.worst_confidence, confidence)
        if duration_ms > 0:
            updates["total_duration_ms"] = s.total_duration_ms + duration_ms

        if updates:
            return SelectorHealthScore(
                provider_id=s.provider_id,
                role=s.role,
                primary_attempts=updates.get("primary_attempts", s.primary_attempts),
                primary_successes=updates.get("primary_successes", s.primary_successes),
                fallback_attempts=updates.get("fallback_attempts", s.fallback_attempts),
                fallback_successes=updates.get("fallback_successes", s.fallback_successes),
                healing_invoked=updates.get("healing_invoked", s.healing_invoked),
                healing_successes=updates.get("healing_successes", s.healing_successes),
                total_duration_ms=updates.get("total_duration_ms", s.total_duration_ms),
                avg_confidence=updates.get("avg_confidence", s.avg_confidence),
                best_confidence=updates.get("best_confidence", s.best_confidence),
                worst_confidence=updates.get("worst_confidence", s.worst_confidence),
                last_updated=updates["last_updated"],
            )
        return s


health_aggregator = HealthAggregator()
