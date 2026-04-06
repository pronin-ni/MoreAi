"""Quality telemetry hooks for browser providers.

Provides a lightweight, in-process metrics collector that tracks:
- Selector failures (how often each selector misses)
- Auth failures (session invalidations, login wall detections)
- Response timing (time to first response, generation duration)
- Timeout frequency (how often generation times out)

The collector is intentionally simple — no Prometheus dependency here.
Metrics can be exported to the existing Prometheus layer in
``chat_proxy_service.py`` when needed.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class SelectorMetric:
    name: str
    provider_id: str
    attempts: int = 0
    successes: int = 0
    failures: int = 0

    @property
    def failure_rate(self) -> float:
        if self.attempts == 0:
            return 0.0
        return self.failures / self.attempts


@dataclass
class ProviderTiming:
    provider_id: str
    response_times: list[float] = field(default_factory=list)
    timeout_count: int = 0
    total_requests: int = 0

    @property
    def avg_response_seconds(self) -> float:
        if not self.response_times:
            return 0.0
        return sum(self.response_times) / len(self.response_times)

    @property
    def timeout_rate(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.timeout_count / self.total_requests


@dataclass
class AuthMetric:
    provider_id: str
    session_invalidations: int = 0
    login_wall_detections: int = 0
    auth_bootstrap_errors: int = 0


class BrowserTelemetry:
    """In-process telemetry collector for browser providers.

    Thread-safe singleton accessible via ``browser_telemetry`` global.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._selector_metrics: dict[tuple[str, str], SelectorMetric] = {}
        self._timings: dict[str, ProviderTiming] = {}
        self._auth_metrics: dict[str, AuthMetric] = {}

    # ---- Selector metrics ----

    def record_selector_attempt(self, provider_id: str, selector_name: str, success: bool) -> None:
        key = (provider_id, selector_name)
        with self._lock:
            if key not in self._selector_metrics:
                self._selector_metrics[key] = SelectorMetric(
                    name=selector_name, provider_id=provider_id,
                )
            m = self._selector_metrics[key]
            m.attempts += 1
            if success:
                m.successes += 1
            else:
                m.failures += 1

    def get_selector_metrics(self, provider_id: str) -> list[SelectorMetric]:
        with self._lock:
            return [
                m for m in self._selector_metrics.values()
                if m.provider_id == provider_id
            ]

    # ---- Timing metrics ----

    def start_request(self, provider_id: str) -> float:
        """Record the start of a request. Returns a token for ``end_request``."""
        return time.monotonic()

    def end_request(
        self,
        provider_id: str,
        start_token: float,
        *,
        timed_out: bool = False,
    ) -> float:
        """Record the end of a request. Returns elapsed seconds."""
        elapsed = time.monotonic() - start_token
        with self._lock:
            if provider_id not in self._timings:
                self._timings[provider_id] = ProviderTiming(provider_id=provider_id)
            t = self._timings[provider_id]
            t.total_requests += 1
            if not timed_out:
                t.response_times.append(elapsed)
            else:
                t.timeout_count += 1
        return elapsed

    def get_timing(self, provider_id: str) -> ProviderTiming | None:
        with self._lock:
            return self._timings.get(provider_id)

    # ---- Auth metrics ----

    def record_session_invalidation(self, provider_id: str) -> None:
        with self._lock:
            if provider_id not in self._auth_metrics:
                self._auth_metrics[provider_id] = AuthMetric(provider_id=provider_id)
            self._auth_metrics[provider_id].session_invalidations += 1

    def record_login_wall(self, provider_id: str) -> None:
        with self._lock:
            if provider_id not in self._auth_metrics:
                self._auth_metrics[provider_id] = AuthMetric(provider_id=provider_id)
            self._auth_metrics[provider_id].login_wall_detections += 1

    def record_auth_bootstrap_error(self, provider_id: str) -> None:
        with self._lock:
            if provider_id not in self._auth_metrics:
                self._auth_metrics[provider_id] = AuthMetric(provider_id=provider_id)
            self._auth_metrics[provider_id].auth_bootstrap_errors += 1

    def get_auth_metric(self, provider_id: str) -> AuthMetric | None:
        with self._lock:
            return self._auth_metrics.get(provider_id)

    # ---- Export ----

    def export_all(self) -> dict[str, Any]:
        with self._lock:
            result: dict[str, Any] = {}

            # Selector metrics by provider
            selector_by_provider: dict[str, list[dict]] = defaultdict(list)
            for _key, m in self._selector_metrics.items():
                selector_by_provider[m.provider_id].append({
                    "selector": m.name,
                    "attempts": m.attempts,
                    "successes": m.successes,
                    "failures": m.failures,
                    "failure_rate": round(m.failure_rate, 3),
                })
            result["selector_metrics"] = dict(selector_by_provider)

            # Timing metrics
            timings = {}
            for pid, t in self._timings.items():
                timings[pid] = {
                    "total_requests": t.total_requests,
                    "timeout_count": t.timeout_count,
                    "avg_response_seconds": round(t.avg_response_seconds, 3),
                    "timeout_rate": round(t.timeout_rate, 3),
                    "response_times_count": len(t.response_times),
                }
            result["timing_metrics"] = timings

            # Auth metrics
            auth = {}
            for pid, a in self._auth_metrics.items():
                auth[pid] = {
                    "session_invalidations": a.session_invalidations,
                    "login_wall_detections": a.login_wall_detections,
                    "auth_bootstrap_errors": a.auth_bootstrap_errors,
                }
            result["auth_metrics"] = auth

            return result


browser_telemetry = BrowserTelemetry()
