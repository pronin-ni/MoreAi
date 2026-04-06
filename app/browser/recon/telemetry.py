"""
Recon telemetry — metrics and logs for auto-recon recovery.

Tracks:
- recon_attempts_total
- recon_success_total
- recon_failure_total
- recon_by_provider
- recon_trigger_reason
- recon_duration_ms
- actions_performed
- replay_success/failure
- recovered_roles
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ReconStats:
    """Per-provider recon statistics."""

    provider_id: str
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    partials: int = 0  # candidates found but replay failed
    total_duration_ms: float = 0.0
    trigger_reasons: dict[str, int] = field(default_factory=dict)
    actions_histogram: dict[str, int] = field(default_factory=dict)
    last_recovery_actions: list[str] = field(default_factory=list)
    last_recovery_reason: str = ""
    last_recovery_recovered: bool = False

    @property
    def success_rate(self) -> float:
        if self.attempts == 0:
            return 0.0
        return self.successes / self.attempts

    @property
    def avg_duration_ms(self) -> float:
        if self.attempts == 0:
            return 0.0
        return self.total_duration_ms / self.attempts


class ReconTelemetry:
    """In-process telemetry for recon recovery."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stats: dict[str, ReconStats] = {}
        self._recent_events: list[dict[str, Any]] = []

    def _get_stats(self, provider_id: str) -> ReconStats:
        if provider_id not in self._stats:
            self._stats[provider_id] = ReconStats(provider_id=provider_id)
        return self._stats[provider_id]

    def record_attempt(
        self,
        provider_id: str,
        failed_action: str,
        error_type: str,
    ) -> None:
        with self._lock:
            s = self._get_stats(provider_id)
            s.attempts += 1
            reason = f"{error_type}:{failed_action}"
            s.trigger_reasons[reason] = s.trigger_reasons.get(reason, 0) + 1

    def record_success(
        self,
        provider_id: str,
        failed_action: str,
        duration_ms: float,
        actions: list[str],
        candidates_count: int,
        trigger_reason: str = "",
        blocking_state: str | None = None,
        replay_succeeded: bool = True,
        recovered_roles: list[str] | None = None,
    ) -> None:
        with self._lock:
            s = self._get_stats(provider_id)
            s.successes += 1
            s.total_duration_ms += duration_ms
            s.last_recovery_actions = list(actions)
            s.last_recovery_reason = "replay succeeded"
            s.last_recovery_recovered = True
            for a in actions:
                s.actions_histogram[a] = s.actions_histogram.get(a, 0) + 1

            self._recent_events.append({
                "ts": time.monotonic(),
                "provider_id": provider_id,
                "action": failed_action,
                "result": "success",
                "duration_ms": round(duration_ms, 1),
                "actions": actions,
                "candidates_count": candidates_count,
                "trigger_reason": trigger_reason,
                "blocking_state": blocking_state,
                "replay_succeeded": replay_succeeded,
                "recovered_roles": recovered_roles or [],
            })
            self._trim_events()

    def record_partial(
        self,
        provider_id: str,
        failed_action: str,
        duration_ms: float,
        reason: str,
    ) -> None:
        with self._lock:
            s = self._get_stats(provider_id)
            s.partials += 1
            s.total_duration_ms += duration_ms
            s.last_recovery_reason = reason
            s.last_recovery_recovered = False

            self._recent_events.append({
                "ts": time.monotonic(),
                "provider_id": provider_id,
                "action": failed_action,
                "result": "partial",
                "duration_ms": round(duration_ms, 1),
                "reason": reason,
            })
            self._trim_events()

    def record_failure(
        self,
        provider_id: str,
        failed_action: str,
        duration_ms: float,
        reason: str,
    ) -> None:
        with self._lock:
            s = self._get_stats(provider_id)
            s.failures += 1
            s.total_duration_ms += duration_ms
            s.last_recovery_reason = reason
            s.last_recovery_recovered = False

            self._recent_events.append({
                "ts": time.monotonic(),
                "provider_id": provider_id,
                "action": failed_action,
                "result": "failure",
                "duration_ms": round(duration_ms, 1),
                "reason": reason,
            })
            self._trim_events()

    def _trim_events(self) -> None:
        if len(self._recent_events) > 200:
            self._recent_events = self._recent_events[-200:]

    def get_stats(self, provider_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            providers = list(self._stats.values())
            if provider_id:
                providers = [s for s in providers if s.provider_id == provider_id]
            return [
                {
                    "provider_id": s.provider_id,
                    "attempts": s.attempts,
                    "successes": s.successes,
                    "failures": s.failures,
                    "partials": s.partials,
                    "success_rate": round(s.success_rate, 3),
                    "avg_duration_ms": round(s.avg_duration_ms, 1),
                    "trigger_reasons": dict(s.trigger_reasons),
                    "actions_histogram": dict(s.actions_histogram),
                    "last_recovery_actions": s.last_recovery_actions,
                    "last_recovery_reason": s.last_recovery_reason,
                    "last_recovery_recovered": s.last_recovery_recovered,
                }
                for s in providers
            ]

    def get_recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return self._recent_events[-limit:]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            total_attempts = sum(s.attempts for s in self._stats.values())
            total_successes = sum(s.successes for s in self._stats.values())
            total_failures = sum(s.failures for s in self._stats.values())
            total_partials = sum(s.partials for s in self._stats.values())

            return {
                "total_attempts": total_attempts,
                "total_successes": total_successes,
                "total_failures": total_failures,
                "total_partials": total_partials,
                "overall_success_rate": round(
                    total_successes / max(total_attempts, 1), 3
                ),
                "per_provider": self.get_stats(),
                "recent_events": self._recent_events[-10:],
            }

    def clear(self) -> None:
        with self._lock:
            self._stats.clear()
            self._recent_events.clear()


recon_telemetry = ReconTelemetry()
