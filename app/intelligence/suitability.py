"""
Stage-specific suitability scoring.

Scores models for each pipeline stage role using proxy metrics:
- generate: availability + latency + creativity bonus
- review/critique: stability + reasoning tag bonus
- refine: availability + stability
- verify: stability heavily weighted
- transform: general capability

Enhanced with dynamic stage-performance feedback:
- Rolling success rate, fallback rate, and duration per model+role
- Blended scoring: static priors + dynamic performance adjustment
- Cold-start handling: low sample count uses mostly static scoring
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.intelligence.stats import stats_aggregator
from app.intelligence.tags import capability_registry
from app.intelligence.types import (
    ModelRuntimeStats,
    StageRole,
    StageSuitability,
)

logger = get_logger(__name__)

# ── Scoring weights ──
# How much each factor contributes to stage suitability

WEIGHTS: dict[str, dict[str, float]] = {
    "generate": {
        "availability": 0.35,
        "latency": 0.30,
        "stability": 0.15,
        "tag_bonus": 0.20,
    },
    "review": {
        "availability": 0.20,
        "latency": 0.15,
        "stability": 0.35,
        "tag_bonus": 0.30,
    },
    "critique": {
        "availability": 0.20,
        "latency": 0.15,
        "stability": 0.35,
        "tag_bonus": 0.30,
    },
    "refine": {
        "availability": 0.30,
        "latency": 0.20,
        "stability": 0.30,
        "tag_bonus": 0.20,
    },
    "verify": {
        "availability": 0.20,
        "latency": 0.10,
        "stability": 0.50,
        "tag_bonus": 0.20,
    },
    "transform": {
        "availability": 0.30,
        "latency": 0.25,
        "stability": 0.25,
        "tag_bonus": 0.20,
    },
}

# Tags that boost suitability for specific roles
ROLE_TAG_BONUSES: dict[str, list[str]] = {
    "generate": ["creative", "fast", "reasoning_strong", "long_context"],
    "review": ["review_strong", "reasoning_strong", "stable"],
    "critique": ["review_strong", "reasoning_strong", "stable"],
    "refine": ["stable", "reasoning_strong", "creative"],
    "verify": ["stable", "review_strong", "reasoning_strong"],
    "transform": ["fast", "stable", "reasoning_strong"],
}

# ── Feedback loop parameters ──

# Minimum samples before dynamic data starts influencing the score
MIN_SAMPLES_FOR_DYNAMIC = 5

# Full window size for 100% dynamic weight
FULL_WINDOW = 100

# Max adjustment magnitude: performance data can shift the score by at most ±0.25
MAX_PERFORMANCE_ADJUSTMENT = 0.25


class ScoringBreakdown:
    """Transparent scoring breakdown for a model+role candidate.

    Shows how the final score was computed:
    base_static_score + dynamic_performance + quality_adjustment - failure_penalty = final_score
    """

    # Max influence of quality on final score (bounded)
    MAX_QUALITY_ADJUSTMENT = 0.15

    # Minimum samples before quality starts influencing score
    MIN_QUALITY_SAMPLES = 3

    def __init__(
        self,
        model_id: str,
        provider_id: str,
        role: str,
    ) -> None:
        self.model_id = model_id
        self.provider_id = provider_id
        self.role = role

        # Component scores (0.0-1.0)
        self.availability_score: float = 0.0
        self.latency_score: float = 0.0
        self.stability_score: float = 0.0
        self.tag_bonus_score: float = 0.0

        # Static base score (from weights + static metrics)
        self.base_static_score: float = 0.0

        # Dynamic performance adjustment (from stage_performance tracker)
        self.dynamic_adjustment: float = 0.0
        self.performance_success_rate: float = 0.5
        self.performance_fallback_rate: float = 0.0
        self.performance_sample_count: int = 0
        self.data_confidence: float = 0.0

        # Quality adjustment (from quality_metrics store)
        self.quality_score: float = 0.5
        self.quality_adjustment: float = 0.0
        self.quality_sample_count: int = 0
        self.quality_confidence: float = 0.0

        # Temporary failure penalty (from adaptive fallback)
        self.failure_penalty: float = 0.0
        self.penalty_reasons: list[str] = []

        # Staleness decay
        self.staleness_decay: float = 1.0  # Decay factor (1.0 = no decay, 0.3 = floor)
        self.last_activity_seconds_ago: float = 0.0
        self.staleness_label: str = "fresh"
        self.effective_confidence: float = 0.0  # Confidence after staleness reduction

        # Final score
        self.final_score: float = 0.0

    def compute(self) -> None:
        """Compute final score from components."""
        self.base_static_score = (
            self.availability_score * 0.35
            + self.latency_score * 0.30
            + self.stability_score * 0.15
            + self.tag_bonus_score * 0.20
        )

        self.final_score = (
            self.base_static_score
            + self.dynamic_adjustment
            + self.quality_adjustment
            - self.failure_penalty
        )
        self.final_score = min(1.0, max(0.0, self.final_score))

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "provider_id": self.provider_id,
            "role": self.role,
            "components": {
                "availability": round(self.availability_score, 3),
                "latency": round(self.latency_score, 3),
                "stability": round(self.stability_score, 3),
                "tag_bonus": round(self.tag_bonus_score, 3),
            },
            "base_static_score": round(self.base_static_score, 3),
            "dynamic_adjustment": round(self.dynamic_adjustment, 3),
            "performance": {
                "success_rate": round(self.performance_success_rate, 3),
                "fallback_rate": round(self.performance_fallback_rate, 3),
                "sample_count": self.performance_sample_count,
                "data_confidence": round(self.data_confidence, 3),
            },
            "quality": {
                "score": round(self.quality_score, 3),
                "adjustment": round(self.quality_adjustment, 3),
                "sample_count": self.quality_sample_count,
                "confidence": round(self.quality_confidence, 3),
            },
            "failure_penalty": round(self.failure_penalty, 3),
            "penalty_reasons": self.penalty_reasons,
            "staleness": {
                "decay_factor": round(self.staleness_decay, 4),
                "last_activity_seconds_ago": round(self.last_activity_seconds_ago, 1),
                "staleness_label": self.staleness_label,
                "effective_confidence": round(self.effective_confidence, 3),
            },
            "final_score": round(self.final_score, 3),
        }


class SuitabilityScorer:
    """Computes stage-specific suitability scores for models.

    Uses a hybrid approach:
    - Static priors from runtime stats (availability, latency, stability, tags)
    - Dynamic adjustment from actual stage performance history
    - Cold-start blending: low sample count → mostly static scoring
    """

    def compute_suitability(
        self,
        model_id: str,
        provider_id: str,
        transport: str,
    ) -> StageSuitability:
        """Compute suitability scores for all stage roles.

        Returns a StageSuitability with scores for each role.
        """
        stats = stats_aggregator.get_model_stats(model_id, provider_id, transport)
        tags = capability_registry.get_tags(model_id, provider_id)

        return StageSuitability(
            model_id=model_id,
            generate_score=self._score_for_role(stats, tags, "generate"),
            review_score=self._score_for_role(stats, tags, "review"),
            critique_score=self._score_for_role(stats, tags, "critique"),
            refine_score=self._score_for_role(stats, tags, "refine"),
            verify_score=self._score_for_role(stats, tags, "verify"),
            transform_score=self._score_for_role(stats, tags, "transform"),
        )

    def compute_for_role(
        self,
        model_id: str,
        provider_id: str,
        transport: str,
        role: StageRole | str,
    ) -> float:
        """Compute suitability score for a single stage role."""
        stats = stats_aggregator.get_model_stats(model_id, provider_id, transport)
        tags = capability_registry.get_tags(model_id, provider_id)
        role_str = role.value if isinstance(role, StageRole) else role
        return self._score_for_role(stats, tags, role_str)

    def compute_breakdown(
        self,
        model_id: str,
        provider_id: str,
        transport: str,
        role: StageRole | str,
        failure_penalties: dict[str, float] | None = None,
    ) -> ScoringBreakdown:
        """Compute full scoring breakdown for traceability.

        Args:
            model_id: The model to score.
            provider_id: The provider serving this model.
            transport: The transport type.
            role: The stage role.
            failure_penalties: Optional {reason: penalty_amount} from adaptive fallback.

        Returns:
            ScoringBreakdown with full transparency.
        """
        stats = stats_aggregator.get_model_stats(model_id, provider_id, transport)
        tags = capability_registry.get_tags(model_id, provider_id)
        role_str = role.value if isinstance(role, StageRole) else role

        breakdown = ScoringBreakdown(model_id, provider_id, role_str)
        breakdown.availability_score = stats.availability_score
        breakdown.latency_score = stats.latency_score
        breakdown.stability_score = stats.stability_score
        breakdown.tag_bonus_score = self._compute_tag_bonus(tags, role_str)

        # Apply staleness decay to component scores
        last_activity = _get_last_activity(model_id, provider_id)
        from app.intelligence.staleness import StalenessDecay
        staleness = StalenessDecay(last_activity)

        # Decay stability and availability toward neutral (latency and tags don't decay)
        breakdown.stability_score = staleness.apply(breakdown.stability_score)
        breakdown.availability_score = staleness.apply(breakdown.availability_score)

        # Record staleness info
        breakdown.staleness_decay = staleness.decay_factor_value
        breakdown.last_activity_seconds_ago = staleness.staleness_seconds
        breakdown.staleness_label = staleness.staleness_label

        # Compute base static score
        weights = WEIGHTS.get(role_str, WEIGHTS["generate"])
        breakdown.base_static_score = (
            weights["availability"] * breakdown.availability_score
            + weights["latency"] * breakdown.latency_score
            + weights["stability"] * breakdown.stability_score
            + weights["tag_bonus"] * breakdown.tag_bonus_score
        )

        # Dynamic performance adjustment
        dynamic_score = self._compute_dynamic_performance_score(model_id, role_str)
        confidence = dynamic_score["confidence"]
        raw_perf_score = dynamic_score["performance_score"]

        breakdown.performance_success_rate = dynamic_score["success_rate"]
        breakdown.performance_fallback_rate = dynamic_score["fallback_rate"]
        breakdown.performance_sample_count = dynamic_score["sample_count"]
        breakdown.data_confidence = confidence

        # Blend: base_static * (1 - confidence) + performance * confidence
        # Then scale adjustment to ±MAX_PERFORMANCE_ADJUSTMENT range
        blended_score = breakdown.base_static_score * (1 - confidence) + raw_perf_score * confidence
        breakdown.dynamic_adjustment = (blended_score - breakdown.base_static_score) * 0.5

        # Effective confidence after staleness reduction
        breakdown.effective_confidence = confidence * breakdown.staleness_decay

        # Apply failure penalties
        if failure_penalties:
            total_penalty = sum(failure_penalties.values())
            breakdown.failure_penalty = min(total_penalty, 0.5)  # Cap at 0.5
            breakdown.penalty_reasons = list(failure_penalties.keys())

        # Apply global recent penalty cache (if available)
        try:
            from app.pipeline.observability.penalty_cache import global_penalty_cache
            global_p = global_penalty_cache.get_penalty(model_id)
            if global_p["total_penalty"] > 0:
                breakdown.failure_penalty += global_p["total_penalty"]
                breakdown.failure_penalty = min(breakdown.failure_penalty, 0.5)
                for r in global_p["reasons"]:
                    breakdown.penalty_reasons.append(f"global:{r}")
        except Exception:
            pass  # Penalty cache is optional

        # Quality adjustment (from quality_metrics store)
        self._compute_quality_adjustment(breakdown)

        # Compute final
        breakdown.compute()

        return breakdown

    def _score_for_role(
        self,
        stats: ModelRuntimeStats,
        tags: set[str],
        role: str,
    ) -> float:
        """Score a model for a specific stage role.

        Hybrid scoring: static priors blended with dynamic performance data.
        Uses cold-start handling: low sample count → mostly static.
        """
        weights = WEIGHTS.get(role, WEIGHTS["generate"])

        availability = stats.availability_score
        latency = stats.latency_score
        stability = stats.stability_score
        tag_bonus = self._compute_tag_bonus(tags, role)

        base_score = (
            weights["availability"] * availability
            + weights["latency"] * latency
            + weights["stability"] * stability
            + weights["tag_bonus"] * tag_bonus
        )

        # Apply dynamic performance adjustment
        dynamic_result = self._compute_dynamic_performance_score(stats.model_id, role)
        confidence = dynamic_result["confidence"]
        perf_score = dynamic_result["performance_score"]

        # Blend: more dynamic weight as we have more data
        blended = base_score * (1 - confidence) + perf_score * confidence

        return min(1.0, max(0.0, blended))

    def _compute_dynamic_performance_score(
        self,
        model_id: str,
        role: str,
    ) -> dict:
        """Compute performance score from stage_performance data.

        Returns a dict with:
        - performance_score: 0.0-1.0 score from historical data
        - confidence: 0.0-1.0 how much to trust this data
        - success_rate: raw rolling success rate
        - fallback_rate: raw fallback rate
        - sample_count: exact number of data points from the rolling window

        Cold-start handling:
        - < MIN_SAMPLES_FOR_DYNAMIC samples: confidence ~10%
        - MIN_SAMPLES_FOR_DYNAMIC to FULL_WINDOW: linear increase
        - >= FULL_WINDOW samples: confidence ~70%
        """
        try:
            from app.pipeline.observability.stage_perf import stage_performance as perf_tracker
        except Exception:
            # Tracker not available — no dynamic data
            return {
                "performance_score": 0.5,
                "confidence": 0.0,
                "success_rate": 0.5,
                "fallback_rate": 0.0,
                "sample_count": 0,
            }

        success_rate = perf_tracker.get_success_rate(model_id, role, window=FULL_WINDOW)
        fallback_rate = perf_tracker.get_fallback_rate(model_id, role, window=FULL_WINDOW)

        # Exact sample count from the performance tracker
        sample_count = perf_tracker.get_sample_count(model_id, role, window=FULL_WINDOW)

        if sample_count == 0:
            # No data — return defaults
            return {
                "performance_score": 0.5,
                "confidence": 0.0,
                "success_rate": 0.5,
                "fallback_rate": 0.0,
                "sample_count": 0,
            }

        # Compute confidence based on exact sample count
        if sample_count < MIN_SAMPLES_FOR_DYNAMIC:
            confidence = 0.1  # Very low confidence, mostly static
        elif sample_count >= FULL_WINDOW:
            confidence = 0.7  # High confidence, mostly dynamic
        else:
            # Linear interpolation
            ratio = (sample_count - MIN_SAMPLES_FOR_DYNAMIC) / (FULL_WINDOW - MIN_SAMPLES_FOR_DYNAMIC)
            confidence = 0.1 + ratio * 0.6

        # Compute performance score from success rate and fallback rate
        # Success rate is the primary signal (0.0-1.0)
        # Fallback rate is a negative signal (higher = worse)
        perf_score = success_rate * 0.7 + (1.0 - fallback_rate) * 0.3

        return {
            "performance_score": min(1.0, max(0.0, perf_score)),
            "confidence": confidence,
            "success_rate": success_rate,
            "fallback_rate": fallback_rate,
            "sample_count": sample_count,
        }

    def _compute_tag_bonus(self, tags: set[str], role: str) -> float:
        """Compute tag bonus score for a role.

        Returns 0.0-1.0 based on how many relevant tags the model has.
        """
        relevant_tags = ROLE_TAG_BONUSES.get(role, [])
        if not relevant_tags:
            return 0.5

        tags_lower = {t.lower() for t in tags}
        matching = sum(1 for t in relevant_tags if t.lower() in tags_lower)

        if matching == 0:
            return 0.5  # Neutral — unknown models should compete fairly
        if matching >= len(relevant_tags):
            return 1.0  # All relevant tags = full bonus
        return matching / len(relevant_tags)

    def _compute_quality_adjustment(self, breakdown: ScoringBreakdown) -> None:
        """Compute quality adjustment from quality_metrics store.

        Quality score (0.0-1.0) is mapped to a bounded adjustment:
        - quality > 0.5 → positive adjustment (up to +MAX_QUALITY_ADJUSTMENT)
        - quality < 0.5 → negative adjustment (down to -MAX_QUALITY_ADJUSTMENT)
        - quality == 0.5 → no adjustment

        Cold-start: requires MIN_QUALITY_SAMPLES before quality influences scoring.
        Confidence scales linearly from 0 to 1 as samples increase.
        """
        try:
            from app.pipeline.observability.quality_scoring import (
                quality_metrics_store,
            )
        except Exception:
            return  # Quality store not available

        metrics = quality_metrics_store.get_quality_metrics(
            model_id=breakdown.model_id,
            role=breakdown.role,
            window=100,
        )

        breakdown.quality_score = metrics.avg_quality_score
        breakdown.quality_sample_count = metrics.sample_count

        # Cold-start: not enough quality data
        if metrics.sample_count < ScoringBreakdown.MIN_QUALITY_SAMPLES:
            breakdown.quality_confidence = 0.0
            breakdown.quality_adjustment = 0.0
            return

        # Confidence: linear scale from MIN_SAMPLES to 20 samples
        if metrics.sample_count >= 20:
            breakdown.quality_confidence = 1.0
        else:
            breakdown.quality_confidence = (
                (metrics.sample_count - ScoringBreakdown.MIN_QUALITY_SAMPLES)
                / (20 - ScoringBreakdown.MIN_QUALITY_SAMPLES)
            )

        # Adjustment: (quality - 0.5) * 2 * MAX_QUALITY_ADJUSTMENT * confidence
        # Maps quality 0.0→-MAX, 0.5→0, 1.0→+MAX
        raw_adjustment = (metrics.avg_quality_score - 0.5) * 2 * ScoringBreakdown.MAX_QUALITY_ADJUSTMENT
        breakdown.quality_adjustment = raw_adjustment * breakdown.quality_confidence

        # Clamp to bounds
        breakdown.quality_adjustment = max(
            -ScoringBreakdown.MAX_QUALITY_ADJUSTMENT,
            min(ScoringBreakdown.MAX_QUALITY_ADJUSTMENT,
                breakdown.quality_adjustment),
        )


# Global singleton
suitability_scorer = SuitabilityScorer()


def _get_last_activity(model_id: str, provider_id: str) -> float:
    """Get the most recent activity timestamp for a model+provider.

    Sources (in priority order):
    1. ModelLifecycleEntry.last_seen_at from intelligence tracker
    2. ModelRuntimeStats.last_success_at / last_failure_at (if populated)
    3. Default: 0.0 (triggers immediate staleness)

    Returns:
        Timestamp of last known activity, or 0.0 if unknown.
    """
    # Source 1: Intelligence tracker lifecycle
    try:
        from app.intelligence.tracker import model_intelligence_tracker
        entry = model_intelligence_tracker.get_entry(model_id)
        if entry and entry.last_seen_at > 0:
            return entry.last_seen_at
    except Exception:
        pass  # Tracker may not be initialized

    # Source 2: Runtime stats (may not be populated)
    try:
        from app.intelligence.stats import stats_aggregator
        stats = stats_aggregator.get_model_stats(model_id, provider_id, "api")
        if stats.last_success_at > 0:
            return stats.last_success_at
        if stats.last_failure_at > 0:
            return stats.last_failure_at
    except Exception:
        pass

    return 0.0
