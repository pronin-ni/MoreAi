"""
Model selection engine for pipeline stages.

Implements:
- Candidate collection from all registries
- Ranking with weighted scoring
- Stage-aware selection with policy constraints
- Bounded fallback with full traceability
- Multi-armed bandit exploration vs exploitation
- Objective-based selection (fast/balanced/quality/deep)
"""

from __future__ import annotations

import random

from app.admin.config_manager import config_manager
from app.core.config import settings
from app.core.errors import ServiceUnavailableError
from app.core.logging import get_logger
from app.core.transport_filters import is_transport_enabled
from app.intelligence.stats import stats_aggregator
from app.intelligence.suitability import (
    _batch_get_last_activity_with_source_by_role,
    suitability_scorer,
)
from app.intelligence.tags import capability_registry
from app.intelligence.tracker import model_intelligence_tracker
from app.intelligence.types import (
    SELECTION_MODE_WEIGHTS,
    CandidateRanking,
    FallbackMode,
    SelectionMode,
    SelectionPolicy,
    SelectionTrace,
    StageRole,
)
from app.registry.unified import unified_registry

logger = get_logger(__name__)

# ── Ranking weights (base weights, will be adjusted by selection mode) ──
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

    Uses multi-armed bandit approach:
    - EXPLORATION_RATE (default 20%): select from cold-start/novel models
    - EXPLOITATION (80%): select from best-ranked established models

    Also applies objective-based weights (fast/balanced/quality/deep).
    """

    def select_for_stage(
        self,
        stage_id: str,
        stage_role: StageRole | str,
        policy: SelectionPolicy,
        previous_stage_model: str = "",
        excluded_ids: set[str] | None = None,
    ) -> SelectionTrace:
        """Select the best model for a pipeline stage.

        Args:
            stage_id: The stage identifier.
            stage_role: The stage role (generate, review, etc.).
            policy: Selection policy constraints.
            previous_stage_model: Model used in the previous stage (for avoidance).
            excluded_ids: Set of model IDs to exclude (from previous failures).

        Returns:
            SelectionTrace with full decision traceability.
        """
        role_str = stage_role.value if isinstance(stage_role, StageRole) else stage_role
        excluded_ids = excluded_ids or set()

        # Determine if we should explore (bandit) or exploit
        is_exploration = self._should_explore(policy)
        is_exploration_mode = policy.selection_mode == SelectionMode.EXPLORE

        trace = SelectionTrace(
            stage_id=stage_id,
            stage_role=role_str,
            previous_stage_model=previous_stage_model,
            selection_policy=policy.model_dump(),
            is_exploration=is_exploration or is_exploration_mode,
        )

        # Collect candidates
        if is_exploration or is_exploration_mode:
            candidates = self._get_exploration_candidates(policy)
            logger.info(
                "exploration_selection",
                stage_id=stage_id,
                role=role_str,
                candidate_count=len(candidates),
                mode=str(policy.selection_mode),
            )
        else:
            candidates = self._collect_candidates(policy)

        # Rank candidates with objective-based weights
        ranked = self._rank_candidates(candidates, role_str, policy, previous_stage_model)

        trace.all_candidates = ranked

        # Apply runtime exclusions (on top of policy-based exclusions from _rank_candidates)
        for c in ranked:
            if c.model_id in excluded_ids:
                c.is_excluded = True
                if not c.excluded_reason:
                    c.excluded_reason = "runtime_excluded"

        # Filter out excluded
        viable = [c for c in ranked if not c.is_excluded]

        if not viable:
            logger.warning(
                "no_viable_candidates",
                stage_id=stage_id,
                role=role_str,
                total_candidates=str(len(ranked)),
                excluded_ids=str(excluded_ids),
            )
            raise ServiceUnavailableError(
                f"No viable candidates for stage '{stage_id}' (role: {role_str})",
                details={
                    "stage_id": stage_id,
                    "role": role_str,
                    "excluded_count": str(len(ranked) - len(viable)),
                    "excluded_ids": list(excluded_ids),
                },
            )

        # Select the best candidate
        best = viable[0]
        best.selected_reason = "highest_ranked"
        best.rank = 1

        trace.selected_model = best.model_id
        trace.selected_provider = best.provider_id
        trace.selected_transport = best.transport
        trace.selected_candidate = best  # Store full ranking object

        # Update selection reason with objective info
        if is_exploration or is_exploration_mode:
            trace.selection_reason = "exploration_cold_start"
        else:
            trace.selection_reason = self._get_selection_reason(best, policy)

        logger.info(
            "model_selected",
            stage_id=stage_id,
            role=role_str,
            model=best.model_id,
            provider=best.provider_id,
            score=str(round(best.final_score, 3)),
            is_exploration=str(trace.is_exploration),
        )

        return trace

    def _should_explore(self, policy: SelectionPolicy) -> bool:
        """Decide using multi-armed bandit: explore or exploit."""
        # Force exploration mode from policy
        if policy.selection_mode == SelectionMode.EXPLORE:
            return True

        # Bandit exploration: random chance based on EXPLORATION_RATE
        rate = settings.pipeline.exploration_rate
        return random.random() < rate

    def _get_exploration_candidates(
        self,
        policy: SelectionPolicy,
    ) -> list[dict[str, str]]:
        """Get candidates for exploration: cold-start / novel models."""
        candidates: list[dict[str, str]] = []
        seen: set[str] = set()

        # Get all models from registry
        for m in unified_registry.list_models():
            canonical_id = m["id"]
            if canonical_id in seen:
                continue

            # Check transport filter
            model_transport = m.get("transport", "browser")
            if not is_transport_enabled(model_transport):
                continue

            # Check policy filters
            if policy.allowed_transports and model_transport not in policy.allowed_transports:
                continue

            if canonical_id in policy.excluded_models:
                continue

            # Check if this is a cold-start model
            lifecycle = model_intelligence_tracker.get_entry(canonical_id)
            sample_count = 0

            # Get sample count from stats
            try:
                stats = stats_aggregator.get_model_stats(
                    canonical_id,
                    provider_id=m.get("provider_id", ""),
                    transport=model_transport,
                )
                sample_count = stats.request_count
            except Exception:
                pass

            is_cold = lifecycle is None or lifecycle.get_is_cold_start(sample_count)
            if not is_cold:
                # In exploration mode, we want cold-start models
                # But if there are none, fall back to all models sorted by "novelty"
                pass

            seen.add(canonical_id)
            candidates.append(
                {
                    "model_id": canonical_id,
                    "provider_id": m.get("provider_id", ""),
                    "transport": model_transport,
                    "canonical_id": canonical_id,
                }
            )

        # Sort: cold-start first, then by cheapness/availability
        cold_start_first = []
        regular = []
        for c in candidates:
            # Check if cold-start
            lifecycle = model_intelligence_tracker.get_entry(c["model_id"])
            if lifecycle and lifecycle.get_is_cold_start(0):
                cold_start_first.append(c)
            else:
                regular.append(c)

        cold_start_first.sort(key=lambda c: c["model_id"])
        regular.sort(key=lambda c: c["model_id"])

        return cold_start_first + regular

    def _get_selection_reason(self, candidate: CandidateRanking, policy: SelectionPolicy) -> str:
        """Generate human-readable reason for selection based on objective."""
        mode = policy.selection_mode

        if mode == SelectionMode.FAST:
            return "low_latency"
        elif mode == SelectionMode.BALANCED:
            return "balanced_score"
        elif mode == SelectionMode.QUALITY:
            return "high_quality"
        elif mode == SelectionMode.DEEP:
            return "deep_reasoning"
        else:
            return "highest_ranked"

    def _get_mode_weights(self, mode: SelectionMode) -> dict[str, float]:
        """Get objective-based weights for a selection mode."""
        return SELECTION_MODE_WEIGHTS.get(mode, SELECTION_MODE_WEIGHTS[SelectionMode.BALANCED])

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
        selection_trace.fallback_chain.append(
            {
                "failed_model": failed_model,
                "failed_provider": selection_trace.selected_provider,
                "reason": failed_reason,
                "fallback_to": next_candidate.model_id,
            }
        )

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

            # Apply transport filter: skip if transport is disabled globally
            model_transport = m.get("transport", "browser")
            if not is_transport_enabled(model_transport):
                logger.debug(
                    "Skipping model due to disabled transport",
                    model_id=canonical_id,
                    transport=model_transport,
                )
                continue

            # Apply transport filter: check against policy allowed_transports
            if policy.allowed_transports:
                resolved = self._resolve_model(canonical_id)
                if resolved and resolved["transport"] not in policy.allowed_transports:
                    continue

            # Apply exclusion list
            if canonical_id in policy.excluded_models:
                continue

            seen.add(canonical_id)
            candidates.append(
                {
                    "model_id": canonical_id,
                    "provider_id": m.get("provider_id", ""),
                    "transport": model_transport,
                    "canonical_id": canonical_id,
                }
            )

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

        Uses batched staleness lookup to avoid per-candidate SQLite queries.

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

        # Batch role-aware staleness lookup once for all candidates
        staleness_map = _batch_get_last_activity_with_source_by_role(
            [(c["model_id"], c["provider_id"], c["transport"]) for c in candidates],
            role,
        )

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

            # Add bandit dynamic score
            try:
                from app.intelligence.bandit import bandit_model

                bandit_score = bandit_model.compute_bandit_score(model_id, provider_id)
                ranking.final_score = ranking.final_score * 0.7 + bandit_score * 0.3
                logger.info(
                    "bandit_score_integrated",
                    model=model_id,
                    provider=provider_id,
                    bandit_score=bandit_score,
                    combined=ranking.final_score,
                )
            except Exception:
                pass

            # Stage suitability with scoring breakdown (uses role-aware pre-fetched staleness data)
            model_penalties = failure_penalties.get(model_id)
            staleness_data = staleness_map.get((model_id, role))
            breakdown = suitability_scorer.compute_breakdown(
                model_id,
                provider_id,
                transport,
                role,
                model_penalties,
                staleness_data=staleness_data,
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
                model_id,
                provider_id,
                transport,
                policy,
                stats,
                tags,
                previous_stage_model,
            )

            if exclusion_reason:
                ranking.is_excluded = True
                ranking.excluded_reason = exclusion_reason
                rankings.append(ranking)
                continue

            # Apply objective-based weights from selection mode
            mode_weights = self._get_mode_weights(policy.selection_mode)

            # Compute final score with full breakdown and objective weights
            # Base composite: availability + latency + stability + suitability + tags + admin
            base_score = (
                weights["availability"] * ranking.availability_score
                + weights["latency"] * ranking.latency_score
                + weights["stability"] * ranking.stability_score
                + weights["stage_suitability"] * ranking.stage_suitability_score
                + weights["tag_bonus"] * ranking.tag_bonus_score
                + weights["admin_bonus"] * ranking.admin_bonus_score
            )

            # Apply objective-based adjustment (latency/success/quality weights)
            objective_score = (
                mode_weights["latency"] * ranking.latency_score
                + mode_weights["success_rate"] * ranking.availability_score
                + mode_weights["quality"] * ranking.stage_suitability_score
            )

            # Combine: 70% base score + 30% objective-adjusted score
            ranking.final_score = base_score * 0.7 + objective_score * 0.3

            # For EXPLORE mode: add novelty bonus for cold-start models
            if policy.selection_mode == SelectionMode.EXPLORE:
                novelty_bonus = mode_weights.get("novelty", 0.0)
                # Check if this model is cold-start
                lifecycle = model_intelligence_tracker.get_entry(model_id)
                if lifecycle and lifecycle.get_is_cold_start(stats.request_count):
                    ranking.final_score += novelty_bonus * 0.5

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
            return (
                f"availability_too_low ({stats.availability_score:.2f} < {policy.min_availability})"
            )

        # Check latency threshold
        if stats.p50_latency_s > policy.max_latency_s and stats.p50_latency_s > 0:
            return f"latency_too_high ({stats.p50_latency_s:.1f}s > {policy.max_latency_s}s)"

        # Check avoided tags
        for avoid_tag in policy.avoid_tags:
            if avoid_tag.lower() in {t.lower() for t in tags}:
                return f"avoided_tag ({avoid_tag})"

        # Check same model as previous stage
        if (
            policy.avoid_same_model_as_previous
            and previous_stage_model
            and model_id == previous_stage_model
        ):
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
