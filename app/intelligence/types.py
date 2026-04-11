"""
Core intelligence types.

Defines the data models for runtime stats, stage suitability,
capability tags, selection policy, and candidate ranking.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

# ── Capability Tags ──


class CapabilityTag(StrEnum):
    """Semantic tags describing model/provider characteristics."""

    FAST = "fast"
    STABLE = "stable"
    CREATIVE = "creative"
    REVIEW_STRONG = "review_strong"
    REASONING_STRONG = "reasoning_strong"
    CHEAP = "cheap"
    EXPERIMENTAL = "experimental"
    BROWSER_ONLY = "browser_only"
    API_PREFERRED = "api_preferred"
    LONG_CONTEXT = "long_context"
    CODE_STRONG = "code_strong"
    MULTILINGUAL = "multilingual"


# ── Stage Role Suitability ──


class StageRole(StrEnum):
    """Pipeline stage roles — mirrors pipeline.types.StageRole."""

    GENERATE = "generate"
    REVIEW = "review"
    CRITIQUE = "critique"
    REFINE = "refine"
    VERIFY = "verify"
    TRANSFORM = "transform"


# ── Runtime Model Stats ──


@dataclass(slots=True)
class ModelRuntimeStats:
    """Runtime statistics for a model/provider combination.

    Computed from analytics, health, and circuit breaker data.
    Not persisted — always reflects current window state.
    """

    model_id: str
    provider_id: str
    transport: str

    # Request counts (from analytics)
    request_count: int = 0
    success_count: int = 0
    error_count: int = 0
    fallback_count: int = 0

    # Latency (seconds, from analytics)
    avg_latency_s: float = 0.0
    p50_latency_s: float = 0.0
    p95_latency_s: float = 0.0

    # Derived scores
    success_rate: float = 1.0
    recent_success_rate: float = 1.0  # last N requests
    failure_rate: float = 0.0
    timeout_rate: float = 0.0
    fallback_rate: float = 0.0

    # Health (from healing system)
    health_score: float = 1.0  # 0.0-1.0 from selector health

    # Circuit breaker
    circuit_open: bool = False
    consecutive_failures: int = 0

    # Timing
    last_success_at: float = 0.0
    last_failure_at: float = 0.0

    # Stage-specific execution stats (optional)
    stage_stats: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def availability_score(self) -> float:
        """Combined availability: circuit breaker + health + success rate."""
        if self.circuit_open:
            return 0.0
        circuit_penalty = 1.0 if self.consecutive_failures == 0 else max(0.3, 1.0 - self.consecutive_failures * 0.15)
        return min(1.0, self.success_rate * 0.5 + self.health_score * 0.3 + circuit_penalty * 0.2)

    @property
    def latency_score(self) -> float:
        """Inverse latency score: 1.0 = fast, 0.0 = slow.

        Based on p50 latency: <5s = 1.0, 5-30s = linear decline, >60s = 0.0.
        """
        if self.p50_latency_s <= 0:
            return 1.0  # No data = assume fast
        if self.p50_latency_s <= 5:
            return 1.0
        if self.p50_latency_s >= 60:
            return 0.0
        return 1.0 - (self.p50_latency_s - 5) / 55.0

    @property
    def stability_score(self) -> float:
        """Stability: low variance in outcomes + low fallback rate.

        1.0 = very stable, 0.0 = unreliable.
        """
        if self.request_count < 3:
            return 0.5  # Insufficient data
        return max(0.0, 1.0 - self.failure_rate * 0.6 - self.fallback_rate * 0.4)


# ── Stage Suitability ──


@dataclass(slots=True)
class StageSuitability:
    """Per-model suitability scores for each pipeline stage role.

    Uses proxy metrics (not ground-truth quality):
    - generate: latency_score * availability_score + creativity bonus
    - review/reasoning: stability_score + reasoning tags
    - refine: combination of stability and availability
    - verify: stability_score weighted heavily
    """

    model_id: str
    generate_score: float = 0.5
    review_score: float = 0.5
    critique_score: float = 0.5
    refine_score: float = 0.5
    verify_score: float = 0.5
    transform_score: float = 0.5

    def for_role(self, role: StageRole | str) -> float:
        """Get suitability score for a specific stage role."""
        role_str = role.value if isinstance(role, StageRole) else role
        mapping: dict[str, float] = {
            "generate": self.generate_score,
            "review": self.review_score,
            "critique": self.critique_score,
            "refine": self.refine_score,
            "verify": self.verify_score,
            "transform": self.transform_score,
        }
        return mapping.get(role_str, 0.5)


# ── Selection Policy ──


class FallbackMode(StrEnum):
    """How to handle candidate unavailability."""

    NEXT_BEST = "next_best"
    FAIL = "fail"


class SelectionPolicy(BaseModel):
    """Model selection policy for a pipeline stage.

    Replaces hard-coded target_model with data-driven selection.
    """

    preferred_models: list[str] = Field(
        default_factory=list,
        description="Preferred model IDs in priority order",
    )
    preferred_tags: list[str] = Field(
        default_factory=list,
        description="Preferred capability tags",
    )
    avoid_tags: list[str] = Field(
        default_factory=list,
        description="Tags to avoid",
    )
    min_availability: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description="Minimum availability score to consider a candidate",
    )
    max_latency_s: float = Field(
        default=60.0,
        gt=0.0,
        description="Maximum acceptable p50 latency in seconds",
    )
    avoid_same_model_as_previous: bool = Field(
        default=False,
        description="Avoid using the same model as the previous stage",
    )
    fallback_mode: FallbackMode = Field(
        default=FallbackMode.NEXT_BEST,
        description="How to handle candidate unavailability",
    )
    allowed_transports: list[str] = Field(
        default_factory=list,
        description="Allowed transports: browser, api, agent. Empty = all allowed.",
    )
    excluded_models: list[str] = Field(
        default_factory=list,
        description="Explicitly excluded model IDs",
    )
    max_fallback_attempts: int = Field(
        default=2,
        ge=0,
        le=5,
        description="Max fallback attempts before giving up",
    )


# ── Candidate Ranking ──


@dataclass(slots=True)
class CandidateRanking:
    """Ranking result for a single candidate model.

    Includes scores and reasoning for why this candidate was ranked.
    Now includes full scoring breakdown for explainability.
    """

    model_id: str
    provider_id: str
    transport: str
    canonical_id: str

    # Component scores (0.0-1.0)
    availability_score: float = 0.0
    latency_score: float = 0.0
    stability_score: float = 0.0
    stage_suitability_score: float = 0.0
    tag_bonus_score: float = 0.0
    admin_bonus_score: float = 0.0

    # Scoring breakdown for explainability
    base_static_score: float = 0.0
    dynamic_adjustment: float = 0.0
    failure_penalty: float = 0.0
    penalty_reasons: list[str] = field(default_factory=list)
    performance_data: dict[str, Any] = field(default_factory=dict)

    # Final composite score
    final_score: float = 0.0
    rank: int = 0

    # Reasoning
    selected_reason: str = ""
    excluded_reason: str = ""

    # Flags
    is_excluded: bool = False
    is_fallback: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "provider_id": self.provider_id,
            "transport": self.transport,
            "canonical_id": self.canonical_id,
            "scores": {
                "availability": round(self.availability_score, 3),
                "latency": round(self.latency_score, 3),
                "stability": round(self.stability_score, 3),
                "stage_suitability": round(self.stage_suitability_score, 3),
                "tag_bonus": round(self.tag_bonus_score, 3),
                "admin_bonus": round(self.admin_bonus_score, 3),
            },
            "scoring_breakdown": {
                "base_static_score": round(self.base_static_score, 3),
                "dynamic_adjustment": round(self.dynamic_adjustment, 3),
                "failure_penalty": round(self.failure_penalty, 3),
                "penalty_reasons": self.penalty_reasons,
                "performance": self.performance_data,
            },
            "final_score": round(self.final_score, 3),
            "rank": self.rank,
            "selected_reason": self.selected_reason,
            "excluded_reason": self.excluded_reason,
            "is_excluded": self.is_excluded,
            "is_fallback": self.is_fallback,
        }


# ── Selection Trace ──


@dataclass
class SelectionTrace:
    """Trace of a model selection decision for a pipeline stage.

    Provides full visibility into why a candidate was chosen or rejected.
    """

    stage_id: str
    stage_role: str
    selection_time: float = field(default_factory=time.monotonic)

    # Candidates considered
    all_candidates: list[CandidateRanking] = field(default_factory=list)

    # Final selection
    selected_model: str = ""
    selected_provider: str = ""
    selected_transport: str = ""

    # Selection context
    previous_stage_model: str = ""
    selection_policy: dict[str, Any] = field(default_factory=dict)

    # Fallback info
    fallback_count: int = 0
    fallback_chain: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "stage_role": self.stage_role,
            "selection_time": self.selection_time,
            "selected_model": self.selected_model,
            "selected_provider": self.selected_provider,
            "selected_transport": self.selected_transport,
            "previous_stage_model": self.previous_stage_model,
            "all_candidates": [c.to_dict() for c in self.all_candidates],
            "fallback_count": self.fallback_count,
            "fallback_chain": self.fallback_chain,
        }


# ── Capability Tag Entry ──


@dataclass
class CapabilityEntry:
    """A capability tag with metadata."""

    tag: CapabilityTag
    description: str
    applies_to_models: list[str] = field(default_factory=list)
    applies_to_providers: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
