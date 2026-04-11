"""
Pipeline execution store — bounded recent history.

Stores recent pipeline execution summaries with retention policy,
filtering by pipeline_id and status, and bounded memory usage.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any

from app.core.logging import get_logger
from app.pipeline.observability.trace_model import PipelineExecutionSummary

logger = get_logger(__name__)

# Default retention limits
_MAX_EXECUTIONS = 100
_MAX_PER_PIPELINE = 30


class PipelineExecutionStore:
    """Bounded in-memory store for recent pipeline executions.

    Supports filtering by pipeline_id and status, with
    per-pipeline and global retention limits.
    """

    def __init__(
        self,
        max_executions: int = _MAX_EXECUTIONS,
        max_per_pipeline: int = _MAX_PER_PIPELINE,
    ) -> None:
        self._max_executions = max_executions
        self._max_per_pipeline = max_per_pipeline
        self._executions: deque[PipelineExecutionSummary] = deque(maxlen=max_executions)
        self._by_pipeline: dict[str, deque[PipelineExecutionSummary]] = {}
        self._lock = threading.Lock()

    def store(self, summary: PipelineExecutionSummary) -> None:
        """Store a completed pipeline execution summary."""
        with self._lock:
            # Add to global list
            self._executions.append(summary)

            # Add to per-pipeline list (bounded)
            pid = summary.pipeline_id
            if pid not in self._by_pipeline:
                self._by_pipeline[pid] = deque(maxlen=self._max_per_pipeline)
            self._by_pipeline[pid].append(summary)

        logger.debug(
            "execution_stored",
            execution_id=summary.execution_id,
            pipeline_id=pid,
            status=summary.status,
        )

    def get_recent(
        self,
        limit: int = 20,
        pipeline_id: str | None = None,
        status: str | None = None,
    ) -> list[PipelineExecutionSummary]:
        """Get recent execution summaries with optional filtering."""
        with self._lock:
            results = list(self._executions)

        # Apply filters
        if pipeline_id:
            results = [e for e in results if e.pipeline_id == pipeline_id]
        if status:
            results = [e for e in results if e.status == status]

        # Return most recent first, limited
        results.reverse()
        return results[:limit]

    def get_by_pipeline(
        self,
        pipeline_id: str,
        limit: int = 10,
        status: str | None = None,
    ) -> list[PipelineExecutionSummary]:
        """Get recent executions for a specific pipeline."""
        with self._lock:
            per_pipeline = list(self._by_pipeline.get(pipeline_id, []))

        if status:
            per_pipeline = [e for e in per_pipeline if e.status == status]

        per_pipeline.reverse()
        return per_pipeline[:limit]

    def get(self, execution_id: str) -> PipelineExecutionSummary | None:
        """Get a specific execution by ID."""
        with self._lock:
            for e in self._executions:
                if e.execution_id == execution_id:
                    return e
        return None

    def get_stats(self) -> dict[str, Any]:
        """Get aggregate store statistics."""
        with self._lock:
            total = len(self._executions)
            by_status: dict[str, int] = {}
            by_pipeline: dict[str, int] = {}

            for e in self._executions:
                by_status[e.status] = by_status.get(e.status, 0) + 1
                by_pipeline[e.pipeline_id] = by_pipeline.get(e.pipeline_id, 0) + 1

            return {
                "total_stored": total,
                "max_capacity": self._max_executions,
                "by_status": by_status,
                "by_pipeline": by_pipeline,
                "pipeline_count": len(by_pipeline),
            }

    def clear(self) -> None:
        """Clear all stored executions."""
        with self._lock:
            self._executions.clear()
            self._by_pipeline.clear()


# Global singleton
execution_store = PipelineExecutionStore()
