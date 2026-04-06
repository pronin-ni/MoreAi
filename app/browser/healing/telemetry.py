"""
Healing-specific telemetry.

Tracks:
- primary selector success/failure
- fallback selector success/failure
- healing invocations
- healing success/failure with confidence scores
- chosen candidate info
- time spent in healing
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class RoleStats:
    """Per-provider per-role healing statistics."""

    provider_id: str
    role: str
    primary_attempts: int = 0
    primary_successes: int = 0
    fallback_attempts: int = 0
    fallback_successes: int = 0
    healing_invoked: int = 0
    healing_successes: int = 0
    healing_failures: int = 0
    total_healing_ms: float = 0.0
    best_confidence: float = 0.0
    worst_confidence: float = 1.0
    avg_confidence: float = 0.0
    _confidence_sum: float = 0.0
    _confidence_count: int = 0

    def record_primary(self, success: bool) -> None:
        self.primary_attempts += 1
        if success:
            self.primary_successes += 1

    def record_fallback(self, success: bool) -> None:
        self.fallback_attempts += 1
        if success:
            self.fallback_successes += 1

    def record_healing(
        self, success: bool, confidence: float, elapsed_ms: float
    ) -> None:
        self.healing_invoked += 1
        self.total_healing_ms += elapsed_ms
        if success:
            self.healing_successes += 1
            self._confidence_sum += confidence
            self._confidence_count += 1
            self.avg_confidence = round(
                self._confidence_sum / self._confidence_count, 3
            )
            self.best_confidence = max(self.best_confidence, confidence)
            self.worst_confidence = min(self.worst_confidence, confidence)
        else:
            self.healing_failures += 1

    @property
    def primary_success_rate(self) -> float:
        if self.primary_attempts == 0:
            return 1.0
        return self.primary_successes / self.primary_attempts

    @property
    def fallback_success_rate(self) -> float:
        if self.fallback_attempts == 0:
            return 0.0
        return self.fallback_successes / self.fallback_attempts

    @property
    def healing_success_rate(self) -> float:
        if self.healing_invoked == 0:
            return 0.0
        return self.healing_successes / self.healing_invoked


class HealingTelemetry:
    """In-process telemetry for self-healing selectors."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stats: dict[tuple[str, str], RoleStats] = {}
        self._last_candidates: list[dict[str, Any]] = []

    def record_primary(
        self, provider_id: str, role: str, success: bool
    ) -> None:
        with self._lock:
            key = (provider_id, role)
            if key not in self._stats:
                self._stats[key] = RoleStats(
                    provider_id=provider_id, role=role
                )
            self._stats[key].record_primary(success)

    def record_fallback(
        self, provider_id: str, role: str, success: bool
    ) -> None:
        with self._lock:
            key = (provider_id, role)
            if key not in self._stats:
                self._stats[key] = RoleStats(
                    provider_id=provider_id, role=role
                )
            self._stats[key].record_fallback(success)

    def record_healing(
        self,
        provider_id: str,
        role: str,
        success: bool,
        confidence: float,
        elapsed_ms: float,
        candidate_info: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            key = (provider_id, role)
            if key not in self._stats:
                self._stats[key] = RoleStats(
                    provider_id=provider_id, role=role
                )
            self._stats[key].record_healing(success, confidence, elapsed_ms)

            if candidate_info:
                candidate_info["provider_id"] = provider_id
                candidate_info["role"] = role
                candidate_info["success"] = success
                candidate_info["timestamp"] = time.monotonic()
                self._last_candidates.append(candidate_info)
                # Keep last 100
                if len(self._last_candidates) > 100:
                    self._last_candidates = self._last_candidates[-100:]

    def get_stats(
        self, provider_id: str | None = None, role: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            result = []
            for _key, s in self._stats.items():
                if provider_id and s.provider_id != provider_id:
                    continue
                if role and s.role != role:
                    continue
                result.append(
                    {
                        "provider_id": s.provider_id,
                        "role": s.role,
                        "primary_attempts": s.primary_attempts,
                        "primary_success_rate": round(s.primary_success_rate, 3),
                        "fallback_attempts": s.fallback_attempts,
                        "fallback_success_rate": round(s.fallback_success_rate, 3),
                        "healing_invoked": s.healing_invoked,
                        "healing_success_rate": round(s.healing_success_rate, 3),
                        "avg_healing_ms": round(
                            s.total_healing_ms / max(s.healing_invoked, 1), 1
                        ),
                        "avg_confidence": s.avg_confidence,
                        "best_confidence": s.best_confidence,
                        "worst_confidence": s.worst_confidence,
                    }
                )
            return result

    def get_recent_candidates(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return self._last_candidates[-limit:]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            total_invoked = sum(s.healing_invoked for s in self._stats.values())
            total_success = sum(s.healing_successes for s in self._stats.values())
            # Inline per_role stats to avoid nested lock acquisition
            per_role = []
            for _key, s in self._stats.items():
                per_role.append(
                    {
                        "provider_id": s.provider_id,
                        "role": s.role,
                        "primary_attempts": s.primary_attempts,
                        "primary_success_rate": round(s.primary_success_rate, 3),
                        "fallback_attempts": s.fallback_attempts,
                        "fallback_success_rate": round(s.fallback_success_rate, 3),
                        "healing_invoked": s.healing_invoked,
                        "healing_success_rate": round(s.healing_success_rate, 3),
                        "avg_healing_ms": round(
                            s.total_healing_ms / max(s.healing_invoked, 1), 1
                        ),
                        "avg_confidence": s.avg_confidence,
                        "best_confidence": s.best_confidence,
                        "worst_confidence": s.worst_confidence,
                    }
                )
            return {
                "total_healing_invocations": total_invoked,
                "total_healing_successes": total_success,
                "overall_success_rate": round(
                    total_success / max(total_invoked, 1), 3
                ),
                "per_role": per_role,
                "recent_candidates": self._last_candidates[-10:],
            }


healing_telemetry = HealingTelemetry()
