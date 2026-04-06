"""
Prometheus-style metrics — built-in, no external dependencies.

Exposes /metrics in Prometheus text format.
Thread-safe, async-safe, zero external deps.
"""

import time
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


# ── Metric types ──


class Counter:
    """Monotonically increasing counter."""

    def __init__(self, name: str, help_text: str, label_names: list[str] | None = None):
        self.name = name
        self.help = help_text
        self.label_names = label_names or []
        self._values: dict[tuple, float] = defaultdict(float)
        self._lock = threading.Lock()

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = tuple(labels.get(l, "") for l in self.label_names)
        with self._lock:
            self._values[key] += amount

    def get(self, **labels: str) -> float:
        key = tuple(labels.get(l, "") for l in self.label_names)
        with self._lock:
            return self._values.get(key, 0.0)

    def _collect(self) -> list[tuple[tuple, float]]:
        with self._lock:
            return list(self._values.items())


class Gauge:
    """Value that can go up and down."""

    def __init__(self, name: str, help_text: str, label_names: list[str] | None = None):
        self.name = name
        self.help = help_text
        self.label_names = label_names or []
        self._values: dict[tuple, float] = defaultdict(float)
        self._lock = threading.Lock()

    def set(self, value: float, **labels: str) -> None:
        key = tuple(labels.get(l, "") for l in self.label_names)
        with self._lock:
            self._values[key] = value

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = tuple(labels.get(l, "") for l in self.label_names)
        with self._lock:
            self._values[key] += amount

    def dec(self, amount: float = 1.0, **labels: str) -> None:
        key = tuple(labels.get(l, "") for l in self.label_names)
        with self._lock:
            self._values[key] -= amount

    def get(self, **labels: str) -> float:
        key = tuple(labels.get(l, "") for l in self.label_names)
        with self._lock:
            return self._values.get(key, 0.0)

    def _collect(self) -> list[tuple[tuple, float]]:
        with self._lock:
            return list(self._values.items())


class Histogram:
    """Tracks value distribution with configurable buckets."""

    DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, float("inf"))

    def __init__(
        self,
        name: str,
        help_text: str,
        label_names: list[str] | None = None,
        buckets: tuple[float, ...] | None = None,
    ):
        self.name = name
        self.help = help_text
        self.label_names = label_names or []
        self.buckets = buckets or self.DEFAULT_BUCKETS
        self._lock = threading.Lock()
        # Per-label: {labels: {"buckets": {upper: count}, "sum": float, "count": int}}
        self._data: dict[tuple, dict[str, Any]] = defaultdict(
            lambda: {
                "buckets": {b: 0 for b in self.buckets},
                "sum": 0.0,
                "count": 0,
            }
        )

    def observe(self, value: float, **labels: str) -> None:
        key = tuple(labels.get(l, "") for l in self.label_names)
        with self._lock:
            data = self._data[key]
            data["sum"] += value
            data["count"] += 1
            for bucket in self.buckets:
                if value <= bucket:
                    data["buckets"][bucket] += 1

    def _collect(self) -> list[tuple[tuple, dict[str, Any]]]:
        with self._lock:
            return list(self._data.items())


# ── Registry ──


class MetricsRegistry:
    """Collects all metrics and renders Prometheus format."""

    def __init__(self):
        self._counters: list[Counter] = []
        self._gauges: list[Gauge] = []
        self._histograms: list[Histogram] = []

    def counter(self, name: str, help_text: str, label_names: list[str] | None = None) -> Counter:
        c = Counter(name, help_text, label_names)
        self._counters.append(c)
        return c

    def gauge(self, name: str, help_text: str, label_names: list[str] | None = None) -> Gauge:
        g = Gauge(name, help_text, label_names)
        self._gauges.append(g)
        return g

    def histogram(self, name: str, help_text: str, label_names: list[str] | None = None, buckets: tuple[float, ...] | None = None) -> Histogram:
        h = Histogram(name, help_text, label_names, buckets)
        self._histograms.append(h)
        return h

    def render(self) -> str:
        lines: list[str] = []

        for c in self._counters:
            lines.append(f"# HELP {c.name} {c.help}")
            lines.append(f"# TYPE {c.name} counter")
            for labels, value in c._collect():
                label_str = _format_labels(c.label_names, labels)
                lines.append(f"{c.name}{label_str} {value}")
            lines.append("")

        for g in self._gauges:
            lines.append(f"# HELP {g.name} {g.help}")
            lines.append(f"# TYPE {g.name} gauge")
            for labels, value in g._collect():
                label_str = _format_labels(g.label_names, labels)
                lines.append(f"{g.name}{label_str} {value}")
            lines.append("")

        for h in self._histograms:
            lines.append(f"# HELP {h.name} {h.help}")
            lines.append(f"# TYPE {h.name} histogram")
            for labels, data in h._collect():
                label_str_base = _format_labels(h.label_names, labels)
                for bucket_upper, count in sorted(data["buckets"].items()):
                    le = "+Inf" if bucket_upper == float("inf") else str(bucket_upper)
                    lines.append(f'{h.name}_bucket{{le="{le}"{", " if label_str_base else ""}{label_str_base.lstrip("{").rstrip("}") if label_str_base else ""}}} {count}')
                lines.append(f"{h.name}_sum{label_str_base} {data['sum']}")
                lines.append(f"{h.name}_count{label_str_base} {data['count']}")
            lines.append("")

        return "\n".join(lines)


def _format_labels(label_names: list[str], values: tuple) -> str:
    if not label_names:
        return ""
    pairs = []
    for name, val in zip(label_names, values):
        escaped = val.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        pairs.append(f'{name}="{escaped}"')
    return "{" + ",".join(pairs) + "}"


# ── Global instance ──

metrics = MetricsRegistry()

# ── Application metrics ──

# Request counters
requests_total = metrics.counter(
    "moreai_requests_total",
    "Total number of requests processed",
    label_names=["transport", "provider", "model", "status"],
)

request_latency = metrics.histogram(
    "moreai_request_latency_seconds",
    "Request processing latency in seconds",
    label_names=["transport", "provider"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, float("inf")),
)

# Error counters
errors_total = metrics.counter(
    "moreai_errors_total",
    "Total number of errors",
    label_names=["error_type", "transport", "provider"],
)

# Queue metrics
queue_depth = metrics.gauge(
    "moreai_queue_depth",
    "Current number of jobs in queue"
)

queue_wait_seconds = metrics.histogram(
    "moreai_queue_wait_seconds",
    "Time jobs spent waiting in queue",
    buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, float("inf")),
)

# Browser execution
browser_execution_seconds = metrics.histogram(
    "moreai_browser_execution_seconds",
    "Browser task execution duration",
    label_names=["provider"],
)

browser_active_workers = metrics.gauge(
    "moreai_browser_active_workers",
    "Number of active browser workers"
)

# Circuit breaker
circuit_breaker_state = metrics.gauge(
    "moreai_circuit_breaker_state",
    "Provider circuit breaker state (0=closed, 1=open)",
    label_names=["provider"],
)

# Fallback
fallback_total = metrics.counter(
    "moreai_fallback_total",
    "Number of fallback attempts",
    label_names=["from_provider", "to_provider", "reason"],
)

fallback_success = metrics.counter(
    "moreai_fallback_success_total",
    "Number of successful fallbacks",
    label_names=["from_provider", "to_provider"],
)

# Config apply
config_apply_total = metrics.counter(
    "moreai_config_apply_total",
    "Config apply attempts",
    label_names=["result"],  # success, failed, restart_required
)

config_apply_duration = metrics.histogram(
    "moreai_config_apply_seconds",
    "Config apply duration",
)

# Registry
registry_model_count = metrics.gauge(
    "moreai_registry_models",
    "Number of registered models",
    label_names=["transport"],
)

# Routing
routing_decision_total = metrics.counter(
    "moreai_routing_decisions_total",
    "Routing decision counts",
    label_names=["model", "selected_provider", "routing_rule"],  # routing_rule: default, force, primary, fallback
)
