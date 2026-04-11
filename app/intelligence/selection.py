"""
Model selection engine for pipeline stages.

Implements:
- Candidate collection from all registries
- Ranking with weighted scoring
- Stage-aware selection with policy constraints
- Bounded fallback with full traceability
"""

from __future__ import annotations

from app.admin.config_manager import config_manager
from app.core.errors import ServiceUnavailableError
from app.core.logging import get_logger
from app.intelligence.stats import stats_aggregator
from app.intelligence.suitability import suitability_scorer
from app.intelligence.tags import capability_registry
from app.intelligence.types import (
    CandidateRanking,
    FallbackMode,
    SelectionPolicy,
    SelectionTrace,
    StageRole,
)
from app.registry.unified import unified_registry

logger = get_logger(__name__)

# ── Ranking weights ──
# Composite score formula

RANKING_WEIGHTS = {
    "availability": 0.25,
    "latency": 0.15,
    "stability": 0.15,
    "stage_suitability": 0.30,
    "tag_bonus": 0.10,
    "admin_bonus": 0.05,
}


class ModelSelector:
    """Selects the best model for a pipeline stage using runtime intelligence.

    Collects candidates, ranks them, and selects the best one
    based on availability, latency, stability, stage suitability,
    capability tags, and admin overrides.
    """

    def select_for_stage(
        self,
        stage_id: str,
        stage_role: StageRole | str,
        policy: SelectionPolicy,
        previous_stage_model: str = "",
    ) -> SelectionTrace:
        """Select the best model for a pipeline stage.

        Args:
            stage_id: The stage identifier.
            stage_role: The stage role (generate, review, etc.).
            policy: Selection policy constraints.
            previous_stage_model: Model used in the previous stage (for avoidance).

        Returns:
            SelectionTrace with full decision traceability.
        """
        role_str = stage_role.value if isinstance(stage_role, StageRole) else stage_role

        trace = SelectionTrace(
            stage_id=stage_id,
            stage_role=role_str,
            previous_stage_model=previous_stage_model,
            selection_policy=policy.model_dump(),
        )

        # Collect all candidates
        candidates = self._collect_candidates(policy)

        # Rank candidates
        ranked = self._rank_candidates(candidates, role_str, policy, previous_stage_model)

        trace.all_candidates = ranked

        # Filter out excluded
        viable = [c for c in ranked if not c.is_excluded]

        if not viable:
            logger.warning(
                "no_viable_candidates",
                stage_id=stage_id,
                role=role_str,
                total_candidates=str(len(ranked)),
            )
            raise ServiceUnavailableError(
                f"No viable candidates for stage '{stage_id}' (role: {role_str})",
                details={
                    "stage_id": stage_id,
                    "role": role_str,
                    "excluded_count": str(len(ranked) - len(viable)),
                },
            )

        # Select the best candidate
        best = viable[0]
        best.selected_reason = "highest_ranked"
        best.rank = 1

        trace.selected_model = best.model_id
        trace.selected_provider = best.provider_id
        trace.selected_transport = best.transport

        logger.info(
            "model_selected",
            stage_id=stage_id,
            role=role_str,
            model=best.model_id,
            provider=best.provider_id,
            score=str(round(best.final_score, 3)),
        )

        return trace

    def fallback(
        self,
        selection_trace: SelectionTrace,
        policy: SelectionPolicy,
        failed_model: str,
        failed_reason: str,
        stage_role: StageRole | str,
        previous_stage_model: str = "",
    ) -> SelectionTrace | None:
        """Perform fallback selection after a candidate fails.

        Returns a new SelectionTrace with the next best candidate,
        or None if no fallback is available.
        """
        if policy.fallback_mode == FallbackMode.FAIL:
            logger.warning(
                "fallback_disabled",
                failed_model=failed_model,
                reason=failed_reason,
            )
            return None

        if selection_trace.fallback_count >= policy.max_fallback_attempts:
            logger.warning(
                "fallback_attempts_exhausted",
                stage_id=selection_trace.stage_id,
                failed_model=failed_model,
                attempts=str(selection_trace.fallback_count),
            )
            return None

        # Find the next viable candidate from original ranking
        candidates = selection_trace.all_candidates
        excluded_models = {failed_model} | {
            entry["failed_model"] for entry in selection_trace.fallback_chain
        }

        next_candidate = None
        for c in candidates:
            if c.model_id in excluded_models or c.is_excluded:
                continue
            next_candidate = c
            break

        if next_candidate is None:
            logger.warning(
                "no_fallback_available",
                stage_id=selection_trace.stage_id,
                failed_model=failed_model,
            )
            return None

        # Record fallback
        selection_trace.fallback_count += 1
        selection_trace.fallback_chain.append({
            "failed_model": failed_model,
            "failed_provider": selection_trace.selected_provider,
            "reason": failed_reason,
            "fallback_to": next_candidate.model_id,
        })

        next_candidate.selected_reason = f"fallback_after_{failed_model}"
        next_candidate.is_fallback = True

        selection_trace.selected_model = next_candidate.model_id
        selection_trace.selected_provider = next_candidate.provider_id
        selection_trace.selected_transport = next_candidate.transport

        logger.info(
            "fallback_selected",
            stage_id=selection_trace.stage_id,
            from_model=failed_model,
            to_model=next_candidate.model_id,
            reason=failed_reason,
        )

        return selection_trace

    def _collect_candidates(
        self,
        policy: SelectionPolicy,
    ) -> list[dict[str, str]]:
        """Collect candidate models from all registries.

        Applies transport filter and exclusion list.
        """
        candidates: list[dict[str, str]] = []
        seen: set[str] = set()

        # Start with preferred models if specified
        for model_id in policy.preferred_models:
            resolved = self._resolve_model(model_id)
            if resolved and resolved["canonical_id"] not in seen:
                seen.add(resolved["canonical_id"])
                candidates.append(resolved)

        # Add all available models from registries
        for m in unified_registry.list_models():
            canonical_id = m["id"]
            if canonical_id in seen:
                continue

            # Apply transport filter
            if policy.allowed_transports:
                resolved = self._resolve_model(canonical_id)
                if resolved and resolved["transport"] not in policy.allowed_transports:
                    continue

            # Apply exclusion list
            if canonical_id in policy.excluded_models:
                continue

            seen.add(canonical_id)
            candidates.append({
                "model_id": canonical_id,
                "provider_id": m.get("provider_id", ""),
                "transport": m.get("transport", "browser"),
                "canonical_id": canonical_id,
            })

        # Sort: preferred models first
        if policy.preferred_models:
            preferred_set = set(policy.preferred_models)
            candidates.sort(
                key=lambda c: (0 if c["model_id"] in preferred_set else 1, c["model_id"]),
            )

        return candidates

    def _resolve_model(self, model_id: str) -> dict[str, str] | None:
        """Resolve a model ID to its provider and transport."""
        try:
            resolved = unified_registry.resolve_model(model_id)
            return {
                "model_id": model_id,
                "provider_id": resolved.provider_id,
                "transport": resolved.transport,
                "canonical_id": resolved.canonical_id,
            }
        except Exception:
            return None

    def _rank_candidates(
        self,
        candidates: list[dict[str, str]],
        role: str,
        policy: SelectionPolicy,
        previous_stage_model: str,
        failure_penalties: dict[str, dict[str, float]] | None = None,
    ) -> list[CandidateRanking]:
        """Rank candidates by composite score.

        Applies constraints:
        - min availability
        - max latency
        - avoid tags
        - avoid same model as previous stage
        - admin overrides

        Args:
            candidates: List of candidate model dicts.
            role: Stage role to score for.
            policy: Selection policy constraints.
            previous_stage_model: Model used in previous stage.
            failure_penalties: Optional {model_id: {reason: penalty}} for adaptive re-ranking.

        Returns:
            Ranked list of candidates with full scoring breakdown.
        """
        rankings: list[CandidateRanking] = []
        weights = RANKING_WEIGHTS
        failure_penalties = failure_penalties or {}

        for c in candidates:
            model_id = c["model_id"]
            provider_id = c["provider_id"]
            transport = c["transport"]

            ranking = CandidateRanking(
                model_id=model_id,
                provider_id=provider_id,
                transport=transport,
                canonical_id=c["canonical_id"],
            )

            # Get runtime stats
            stats = stats_aggregator.get_model_stats(model_id, provider_id, transport)
            ranking.availability_score = stats.availability_score
            ranking.latency_score = stats.latency_score
            ranking.stability_score = stats.stability_score

            # Stage suitability with scoring breakdown
            model_penalties = failure_penalties.get(model_id)
            breakdown = suitability_scorer.compute_breakdown(
                model_id, provider_id, transport, role, model_penalties,
            )

            ranking.stage_suitability_score = breakdown.final_score
            ranking.base_static_score = breakdown.base_static_score
            ranking.dynamic_adjustment = breakdown.dynamic_adjustment
            ranking.failure_penalty = breakdown.failure_penalty
            ranking.penalty_reasons = breakdown.penalty_reasons
            ranking.performance_data = {
                "success_rate": breakdown.performance_success_rate,
                "fallback_rate": breakdown.performance_fallback_rate,
                "sample_count": breakdown.performance_sample_count,
                "data_confidence": breakdown.data_confidence,
            }

            # Tag bonus
            tags = capability_registry.get_tags(model_id, provider_id)
            ranking.tag_bonus_score = self._compute_tag_bonus_for_ranking(tags, role)

            # Admin bonus from overrides
            ranking.admin_bonus_score = self._compute_admin_bonus(model_id, provider_id)

            # Check exclusions
            exclusion_reason = self._check_exclusions(
                model_id, provider_id, transport, policy,
                stats, tags, previous_stage_model,
            )

            if exclusion_reason:
                ranking.is_excluded = True
                ranking.excluded_reason = exclusion_reason
                rankings.append(ranking)
                continue

            # Compute final score with full breakdown
            ranking.final_score = (
                weights["availability"] * ranking.availability_score
                + weights["latency"] * ranking.latency_score
                + weights["stability"] * ranking.stability_score
                + weights["stage_suitability"] * ranking.stage_suitability_score
                + weights["tag_bonus"] * ranking.tag_bonus_score
                + weights["admin_bonus"] * ranking.admin_bonus_score
            )

            # Clamp final score
            ranking.final_score = min(1.0, max(0.0, ranking.final_score))

            rankings.append(ranking)

        # Sort by final score (descending), then by admin preference
        rankings.sort(key=lambda r: (r.is_excluded, r.final_score), reverse=True)

        # Re-assign ranks
        rank = 1
        for r in rankings:
            if not r.is_excluded:
                r.rank = rank
                rank += 1
            else:
                r.rank = -1

        return rankings

    def _compute_tag_bonus_for_ranking(
        self,
        tags: set[str],
        role: str,
    ) -> float:
        """Compute tag bonus for ranking (0.0-1.0)."""
        from app.intelligence.suitability import ROLE_TAG_BONUSES

        relevant_tags = ROLE_TAG_BONUSES.get(role, [])
        if not relevant_tags:
            return 0.5

        tags_lower = {t.lower() for t in tags}
        matching = sum(1 for t in relevant_tags if t.lower() in tags_lower)

        if matching == 0:
            return 0.3
        if matching >= len(relevant_tags):
            return 1.0
        return matching / len(relevant_tags)

    def _compute_admin_bonus(
        self,
        model_id: str,
        provider_id: str,
    ) -> float:
        """Compute admin override bonus (0.0-1.0).

        Returns 1.0 if force_provider, 0.7 if primary, 0.5 otherwise.
        """
        overrides = config_manager.overrides.models.get(model_id)
        if overrides and overrides.force_provider == provider_id:
            return 1.0

        routing_override = config_manager.overrides.routing.get(model_id)
        if routing_override and routing_override.primary == provider_id:
            return 0.7

        return 0.5  # Neutral default

    def _check_exclusions(
        self,
        model_id: str,
        provider_id: str,
        transport: str,
        policy: SelectionPolicy,
        stats,
        tags: set[str],
        previous_stage_model: str,
    ) -> str | None:
        """Check if a candidate should be excluded.

        Returns a reason string if excluded, None otherwise.
        """
        # Check availability threshold
        if stats.availability_score < policy.min_availability:
            return f"availability_too_low ({stats.availability_score:.2f} < {policy.min_availability})"

        # Check latency threshold
        if stats.p50_latency_s > policy.max_latency_s and stats.p50_latency_s > 0:
            return f"latency_too_high ({stats.p50_latency_s:.1f}s > {policy.max_latency_s}s)"

        # Check avoided tags
        for avoid_tag in policy.avoid_tags:
            if avoid_tag.lower() in {t.lower() for t in tags}:
                return f"avoided_tag ({avoid_tag})"

        # Check same model as previous stage
        if policy.avoid_same_model_as_previous and previous_stage_model and model_id == previous_stage_model:
            return f"same_as_previous ({previous_stage_model})"

        # Check circuit breaker
        if stats.circuit_open:
            return "circuit_breaker_open"

        # Check admin disabled
        model_override = config_manager.overrides.models.get(model_id)
        if model_override and model_override.enabled is False:
            return "admin_disabled"

        return None


# Global singleton
model_selector = ModelSelector()
