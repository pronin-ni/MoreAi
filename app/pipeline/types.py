"""
Core pipeline types and data models.

Defines the declarative types used to describe pipeline definitions,
stages, execution context, results, and traces.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

# ── Stage Role ──


class StageRole(StrEnum):
    """The purpose of a pipeline stage."""

    GENERATE = "generate"
    REVIEW = "review"
    CRITIQUE = "critique"
    REFINE = "refine"
    VERIFY = "verify"
    TRANSFORM = "transform"


# ── Failure Policy ──


class FailurePolicy(StrEnum):
    """What to do when a stage fails."""

    FAIL_ALL = "fail_all"
    SKIP = "skip"
    FALLBACK = "fallback"


# ── Output Mode ──


class OutputMode(StrEnum):
    """How a stage returns its output."""

    PLAIN_TEXT = "plain_text"
    STRUCTURED = "structured"


# ── Input Mapping ──


class InputMapping(BaseModel):
    """Defines what data a stage receives as input."""

    include_original_request: bool = True
    include_previous_output: bool = True
    include_all_outputs: bool = False
    include_stage_summaries: bool = False
    custom_prompt_prefix: str | None = None
    custom_prompt_suffix: str | None = None


# ── Selection Policy (imported from intelligence) ──
# Forward reference to avoid circular import.
# Actual type: app.intelligence.types.SelectionPolicy


# ── Pipeline Stage Definition ──


class PipelineStage(BaseModel):
    """Declarative definition of a single pipeline stage."""

    stage_id: str = Field(..., description="Unique identifier within the pipeline, e.g. 'draft'")
    role: StageRole = Field(..., description="Purpose of this stage")
    target_model: str = Field(
        default="",
        description="Model ID to route this stage to (e.g. 'qwen', 'glm', 'browser/kimi'). "
        "Ignored if selection_policy is provided.",
    )
    input_mapping: InputMapping = Field(default_factory=InputMapping)
    output_mode: OutputMode = Field(default=OutputMode.PLAIN_TEXT)
    timeout_override_ms: int | None = Field(default=None, ge=100)
    failure_policy: FailurePolicy = Field(default=FailurePolicy.FAIL_ALL)
    max_retries: int = Field(default=0, ge=0, le=3)
    prompt_template: str | None = Field(
        default=None,
        description="Optional template that receives {original_request}, {previous_output}, "
        "{critique_notes}, etc.",
    )
    selection_policy: dict[str, Any] | None = Field(
        default=None,
        description="Data-driven model selection policy. If provided, overrides target_model. "
        "Fields: preferred_models, preferred_tags, avoid_tags, min_availability, "
        "max_latency_s, avoid_same_model_as_previous, fallback_mode, allowed_transports, "
        "excluded_models, max_fallback_attempts.",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def uses_intelligent_selection(self) -> bool:
        """True if this stage uses data-driven model selection."""
        return self.selection_policy is not None and bool(self.selection_policy)


# ── Pipeline Definition ──


class PipelineDefinition(BaseModel):
    """Declarative definition of an entire pipeline."""

    pipeline_id: str = Field(..., description="Canonical ID, e.g. 'generate-review-refine'")
    display_name: str = Field(..., description="Human-readable name")
    description: str = Field(default="", description="What this pipeline does")
    enabled: bool = Field(default=True)
    stages: list[PipelineStage] = Field(..., min_length=1, max_length=3)
    max_total_time_ms: int = Field(default=120_000, ge=10_000)
    max_stage_retries: int = Field(default=1, ge=0, le=3)
    model_id: str = Field(
        default="",
        description="OpenAI-compatible model ID, e.g. 'pipeline/generate-review-refine'",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, __context) -> None:
        if not self.model_id:
            object.__setattr__(self, "model_id", f"pipeline/{self.pipeline_id}")


# ── Attempt Trace ──


@dataclass
class AttemptTrace:
    """Trace of a single execution attempt within a stage.

    Captures per-attempt timing and outcome so that total stage
    duration can be distinguished from the successful attempt's
    own duration.
    """

    attempt_number: int = 0
    started_at: float = 0
    ended_at: float = 0
    duration_ms: float = 0
    result: str = "pending"  # success, failed, timeout, restarted
    failure_reason: str = ""
    restart_occurred: bool = False
    restart_reason: str = ""


# ── Stage Result ──


class StageResult(BaseModel):
    """The outcome of executing a single stage."""

    stage_id: str
    role: StageRole
    target_model: str
    provider_id: str = ""
    output: str = ""
    success: bool = True
    error_message: str | None = None
    error_type: str | None = None
    duration_ms: float = 0
    retry_count: int = 0
    artifacts: dict[str, Any] = Field(default_factory=dict)

    # Per-attempt breakdown (populated when retries occur)
    attempts: list[AttemptTrace] = Field(default_factory=list)
    # Duration of the successful attempt (0 if no success)
    successful_attempt_duration_ms: float = 0
    # Whether a runtime restart occurred during this stage
    restart_occurred: bool = False
    restart_reason: str = ""


# ── Stage Trace ──


class StageTrace(BaseModel):
    """Detailed trace of a single stage execution."""

    stage_id: str
    role: str
    target_model: str
    provider_id: str = ""
    status: str = "pending"  # pending | running | completed | failed | skipped | retried
    started_at: float = 0
    completed_at: float = 0
    duration_ms: float = 0
    retry_count: int = 0
    error_message: str | None = None
    result_summary: str = ""

    # Per-attempt breakdown
    attempts: list[AttemptTrace] = Field(default_factory=list)
    # Duration of the successful attempt
    successful_attempt_duration_ms: float = 0
    # Runtime restart info
    restart_occurred: bool = False
    restart_reason: str = ""


# ── Pipeline Trace ──


class PipelineTrace(BaseModel):
    """Full execution trace for a pipeline run."""

    trace_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    pipeline_id: str
    model_id: str
    status: str = "pending"  # pending | running | completed | failed
    started_at: float = Field(default_factory=time.monotonic)
    completed_at: float = 0
    total_duration_ms: float = 0
    stage_traces: list[StageTrace] = Field(default_factory=list)
    final_output: str = ""
    error_message: str | None = None
    request_id: str = ""
    original_request_model: str = ""


# ── Pipeline Context ──


@dataclass
class PipelineContext:
    """Mutable execution context passed between stages.

    Contains the original request, stage outputs, and metadata.
    Designed for controlled handoff — not raw DOM dumping.
    """

    trace: PipelineTrace
    original_request_model: str
    original_messages: list[dict[str, Any]]  # serialized ChatMessage list
    original_user_input: str  # last user message text
    stage_outputs: dict[str, StageResult] = field(default_factory=dict)
    stage_summaries: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    memory_context: str = ""  # conversation context for memory-enabled sessions

    def get_previous_output(self, current_stage_id: str) -> str | None:
        """Get the output of the stage immediately before the current one."""
        stage_ids = list(self.stage_outputs.keys())
        try:
            idx = stage_ids.index(current_stage_id)
        except ValueError:
            return None
        if idx == 0:
            return None
        prev_id = stage_ids[idx - 1]
        result = self.stage_outputs.get(prev_id)
        if result and result.success:
            return result.output
        return None

    def get_all_outputs_text(self) -> str:
        """Concatenate all successful stage outputs."""
        parts = []
        for sid, result in self.stage_outputs.items():
            if result.success and result.output:
                parts.append(f"[{sid} ({result.role.value})]:\n{result.output}")
        return "\n\n".join(parts)

    def get_summary_text(self) -> str:
        """Get concise summaries from all stages."""
        parts = []
        for sid, summary in self.stage_summaries.items():
            if summary:
                parts.append(f"[{sid}]: {summary}")
        return "\n".join(parts)


# ── Pipeline Registry ──


class PipelineRegistry:
    """In-memory registry for pipeline definitions.

    Stores built-in and dynamically loaded pipelines.
    Thread-safe for read-heavy workloads (pipelines are defined at startup).
    """

    def __init__(self) -> None:
        self._pipelines: dict[str, PipelineDefinition] = {}

    def register(self, definition: PipelineDefinition) -> None:
        """Register a pipeline definition."""
        self._pipelines[definition.pipeline_id] = definition

    def get(self, pipeline_id: str) -> PipelineDefinition | None:
        """Get a pipeline definition by ID."""
        return self._pipelines.get(pipeline_id)

    def get_by_model_id(self, model_id: str) -> PipelineDefinition | None:
        """Find a pipeline by its OpenAI-compatible model ID."""
        for p in self._pipelines.values():
            if p.model_id == model_id:
                return p
        return None

    def list_all(self) -> list[PipelineDefinition]:
        """List all registered pipelines."""
        return list(self._pipelines.values())

    def list_enabled(self) -> list[PipelineDefinition]:
        """List only enabled pipelines."""
        return [p for p in self._pipelines.values() if p.enabled]

    def is_pipeline_model(self, model_id: str) -> bool:
        """Check if a model ID refers to a pipeline."""
        return model_id.startswith("pipeline/") or self.get_by_model_id(model_id) is not None

    def enable(self, pipeline_id: str) -> bool:
        """Enable a pipeline. Returns True if found."""
        p = self._pipelines.get(pipeline_id)
        if p is None:
            return False
        object.__setattr__(p, "enabled", True)
        return True

    def disable(self, pipeline_id: str) -> bool:
        """Disable a pipeline. Returns True if found."""
        p = self._pipelines.get(pipeline_id)
        if p is None:
            return False
        object.__setattr__(p, "enabled", False)
        return True

    def clear(self) -> None:
        """Remove all pipeline definitions."""
        self._pipelines.clear()


# Global singleton
pipeline_registry = PipelineRegistry()
