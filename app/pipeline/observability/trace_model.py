"""
Enhanced trace model for pipeline observability.

Provides PipelineExecutionSummary, StageSelectionExplain,
and execution rendering utilities. Complements existing
PipelineTrace and StageTrace in pipeline/types.py.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# ── Stage Selection Explain ──


@dataclass
class CandidateExplain:
    """Why a candidate was considered or excluded."""

    model_id: str
    provider_id: str
    transport: str
    rank: int = 0
    score: float = 0.0

    # Inclusion/exclusion
    excluded: bool = False
    exclusion_reason: str = ""  # e.g. "circuit_breaker_open", "latency_too_high"

    # Selection
    selected: bool = False
    selection_reason: str = ""  # e.g. "highest_ranked", "fallback_after_qwen"
    is_fallback: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "provider_id": self.provider_id,
            "transport": self.transport,
            "rank": self.rank,
            "score": round(self.score, 3),
            "excluded": self.excluded,
            "exclusion_reason": self.exclusion_reason,
            "selected": self.selected,
            "selection_reason": self.selection_reason,
            "is_fallback": self.is_fallback,
        }


@dataclass
class StageSelectionExplain:
    """Full explainability for model selection at a pipeline stage."""

    stage_id: str
    stage_role: str
    policy_summary: dict[str, Any] = field(default_factory=dict)
    candidates_considered: int = 0
    candidates_excluded: int = 0
    candidates_viable: int = 0
    selected_model: str = ""
    selected_provider: str = ""
    selected_transport: str = ""
    selection_reason: str = ""
    fallback_count: int = 0
    fallback_chain: list[dict[str, str]] = field(default_factory=list)
    candidate_details: list[CandidateExplain] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "stage_role": self.stage_role,
            "policy_summary": self.policy_summary,
            "candidates_considered": self.candidates_considered,
            "candidates_excluded": self.candidates_excluded,
            "candidates_viable": self.candidates_viable,
            "selected_model": self.selected_model,
            "selected_provider": self.selected_provider,
            "selected_transport": self.selected_transport,
            "selection_reason": self.selection_reason,
            "fallback_count": self.fallback_count,
            "fallback_chain": self.fallback_chain,
            "candidate_details": [c.to_dict() for c in self.candidate_details[:20]],  # bounded
        }


# ── Stage Execution Summary ──


@dataclass
class StageExecutionSummary:
    """Bounded summary of a stage execution for admin UI and traces.

    Does NOT store full raw outputs — only concise summaries.
    """

    stage_id: str
    stage_role: str
    status: str  # completed, failed, skipped

    # Selection
    selected_model: str = ""
    selected_provider: str = ""
    selected_transport: str = ""
    selection_explain: StageSelectionExplain | None = None

    # Timing
    started_at: float = 0
    completed_at: float = 0
    duration_ms: float = 0
    budget_remaining_ms: float | None = None

    # Retry/fallback
    retry_count: int = 0
    fallback_count: int = 0

    # Concise summaries (bounded to 500 chars)
    input_summary: str = ""
    output_summary: str = ""

    # Failure info
    failure_reason: str = ""
    error_type: str = ""

    # Budget info
    stage_budget_ms: int | None = None
    total_budget_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "stage_id": self.stage_id,
            "stage_role": self.stage_role,
            "status": self.status,
            "selected_model": self.selected_model,
            "selected_provider": self.selected_provider,
            "selected_transport": self.selected_transport,
            "duration_ms": round(self.duration_ms, 1),
            "retry_count": self.retry_count,
            "fallback_count": self.fallback_count,
            "input_summary": self.input_summary,
            "output_summary": self.output_summary,
        }

        if self.budget_remaining_ms is not None:
            result["budget_remaining_ms"] = round(self.budget_remaining_ms, 1)
        if self.stage_budget_ms is not None:
            result["stage_budget_ms"] = self.stage_budget_ms
        if self.total_budget_ms is not None:
            result["total_budget_ms"] = self.total_budget_ms
        if self.failure_reason:
            result["failure_reason"] = self.failure_reason
            result["error_type"] = self.error_type
        if self.selection_explain:
            result["selection_explain"] = self.selection_explain.to_dict()

        return result

    @staticmethod
    def _bounded(text: str, max_len: int = 500) -> str:
        if not text:
            return ""
        if len(text) <= max_len:
            return text
        return text[: max_len - 3] + "..."


# ── Pipeline Execution Summary ──


@dataclass
class PipelineExecutionSummary:
    """Top-level summary of a pipeline execution for admin UI and history.

    Bounded and suitable for list views, recent history, and dashboards.
    Full detail available via PipelineExecutionTrace.
    """

    execution_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    pipeline_id: str = ""
    pipeline_display_name: str = ""
    status: str = "pending"  # success, partial, failed, aborted

    # Timing
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float = 0
    duration_ms: float = 0

    # Budget
    total_budget_ms: int = 0
    budget_consumed_pct: float = 0

    # Counts
    stage_count: int = 0
    stages_completed: int = 0
    total_retries: int = 0
    total_fallbacks: int = 0

    # Result
    final_output_summary: str = ""
    failure_reason: str = ""
    failed_stage: str = ""

    # Stage summaries
    stage_summaries: list[StageExecutionSummary] = field(default_factory=list)

    # Metadata
    request_id: str = ""
    original_model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "pipeline_id": self.pipeline_id,
            "pipeline_display_name": self.pipeline_display_name,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": round(self.duration_ms, 1),
            "total_budget_ms": self.total_budget_ms,
            "budget_consumed_pct": round(self.budget_consumed_pct, 1),
            "stage_count": self.stage_count,
            "stages_completed": self.stages_completed,
            "total_retries": self.total_retries,
            "total_fallbacks": self.total_fallbacks,
            "final_output_summary": self.final_output_summary,
            "failure_reason": self.failure_reason,
            "failed_stage": self.failed_stage,
            "request_id": self.request_id,
            "original_model": self.original_model,
            "stages": [s.to_dict() for s in self.stage_summaries],
        }

    def to_list_row(self) -> dict[str, Any]:
        """Compact row for table/list views."""
        return {
            "execution_id": self.execution_id,
            "pipeline_id": self.pipeline_id,
            "status": self.status,
            "duration_ms": round(self.duration_ms, 1),
            "stage_count": self.stage_count,
            "stages_completed": self.stages_completed,
            "total_fallbacks": self.total_fallbacks,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


# ── Failure Analysis ──


@dataclass
class FailureAnalysis:
    """Structured failure analysis for a pipeline execution."""

    execution_id: str = ""
    pipeline_id: str = ""
    status: str = "failed"

    # Where it failed
    failed_stage: str = ""
    failed_stage_role: str = ""
    failure_reason: str = ""
    error_type: str = ""

    # Recovery attempts
    retry_count: int = 0
    fallback_count: int = 0
    candidates_exhausted: bool = False

    # Root cause classification
    root_cause: str = ""  # model_unavailable, timeout, circuit_breaker, selection_failed, execution_error
    root_cause_detail: str = ""

    # Budget context
    total_budget_ms: int = 0
    time_elapsed_ms: float = 0
    budget_exceeded: bool = False

    # Stage states
    stage_results: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "pipeline_id": self.pipeline_id,
            "status": self.status,
            "failed_stage": self.failed_stage,
            "failed_stage_role": self.failed_stage_role,
            "failure_reason": self.failure_reason,
            "error_type": self.error_type,
            "retry_count": self.retry_count,
            "fallback_count": self.fallback_count,
            "candidates_exhausted": self.candidates_exhausted,
            "root_cause": self.root_cause,
            "root_cause_detail": self.root_cause_detail,
            "budget_exceeded": self.budget_exceeded,
            "total_budget_ms": self.total_budget_ms,
            "time_elapsed_ms": round(self.time_elapsed_ms, 1),
            "stage_results": self.stage_results,
        }
