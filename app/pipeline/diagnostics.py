"""
Pipeline diagnostics — execution trace storage and querying.

Keeps a bounded in-memory buffer of recent pipeline traces
for admin visibility and debugging.
"""

from __future__ import annotations

import threading
from collections import deque

from app.core.logging import get_logger
from app.pipeline.types import PipelineTrace

logger = get_logger(__name__)

# Bounded buffer size — keeps only recent traces
_MAX_TRACES = 200


class PipelineDiagnostics:
    """Thread-safe store for recent pipeline execution traces."""

    def __init__(self, max_traces: int = _MAX_TRACES) -> None:
        self._traces: deque[PipelineTrace] = deque(maxlen=max_traces)
        self._lock = threading.Lock()

        # Aggregate stats
        self._executions_by_pipeline: dict[str, int] = {}
        self._success_by_pipeline: dict[str, int] = {}
        self._failure_by_pipeline: dict[str, int] = {}
        self._stage_failures: dict[str, dict[str, int]] = {}  # pipeline_id -> {stage_id: count}
        self._total_duration_ms: dict[str, float] = {}
        self._total_count: dict[str, int] = {}

    def record(self, trace: PipelineTrace) -> None:
        """Record a completed pipeline trace."""
        with self._lock:
            self._traces.append(trace)

            pid = trace.pipeline_id
            self._executions_by_pipeline[pid] = self._executions_by_pipeline.get(pid, 0) + 1

            if trace.status == "completed":
                self._success_by_pipeline[pid] = self._success_by_pipeline.get(pid, 0) + 1
            else:
                self._failure_by_pipeline[pid] = self._failure_by_pipeline.get(pid, 0) + 1
                # Record stage-level failures
                for st in trace.stage_traces:
                    if st.status in ("failed", "skipped"):
                        self._stage_failures.setdefault(pid, {})
                        self._stage_failures[pid][st.stage_id] = (
                            self._stage_failures[pid].get(st.stage_id, 0) + 1
                        )

            self._total_duration_ms[pid] = self._total_duration_ms.get(pid, 0.0) + trace.total_duration_ms
            self._total_count[pid] = self._total_count.get(pid, 0) + 1

    def get_recent_traces(self, limit: int = 20) -> list[PipelineTrace]:
        """Get the most recent pipeline traces."""
        with self._lock:
            return list(self._traces)[-limit:]

    def get_trace(self, trace_id: str) -> PipelineTrace | None:
        """Find a trace by ID."""
        with self._lock:
            for t in self._traces:
                if t.trace_id == trace_id:
                    return t
        return None

    def get_traces_by_pipeline(self, pipeline_id: str, limit: int = 10) -> list[PipelineTrace]:
        """Get recent traces for a specific pipeline."""
        with self._lock:
            result = []
            for t in reversed(self._traces):
                if t.pipeline_id == pipeline_id:
                    result.append(t)
                    if len(result) >= limit:
                        break
            return list(reversed(result))

    def get_stats(self) -> dict:
        """Get aggregate pipeline statistics."""
        with self._lock:
            stats: dict[str, dict] = {}
            for pid in self._executions_by_pipeline:
                total = self._total_count.get(pid, 0)
                success = self._success_by_pipeline.get(pid, 0)
                failures = self._failure_by_pipeline.get(pid, 0)
                avg_latency = (
                    round(self._total_duration_ms.get(pid, 0) / total, 1) if total > 0 else 0
                )
                stats[pid] = {
                    "executions": total,
                    "success_count": success,
                    "failure_count": failures,
                    "success_rate": round(success / total, 3) if total > 0 else 0,
                    "avg_latency_ms": avg_latency,
                    "stage_failures": dict(self._stage_failures.get(pid, {})),
                }
            return stats

    def clear(self) -> None:
        """Clear all traces and stats."""
        with self._lock:
            self._traces.clear()
            self._executions_by_pipeline.clear()
            self._success_by_pipeline.clear()
            self._failure_by_pipeline.clear()
            self._stage_failures.clear()
            self._total_duration_ms.clear()
            self._total_count.clear()


# Global singleton
pipeline_diagnostics = PipelineDiagnostics()
