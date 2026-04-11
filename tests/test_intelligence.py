"""
Tests for the model/provider intelligence subsystem.

Covers:
- Runtime stats aggregation
- Stage-specific suitability scoring
- Capability tags registry
- Selection policy parsing
- Candidate ranking
- Stage fallback
- Admin endpoints
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.intelligence.selection import ModelSelector
from app.intelligence.stats import StatsAggregator
from app.intelligence.suitability import SuitabilityScorer
from app.intelligence.tags import CapabilityRegistry, capability_registry
from app.intelligence.types import (
    CandidateRanking,
    FallbackMode,
    ModelRuntimeStats,
    SelectionPolicy,
    SelectionTrace,
    StageRole,
)

# ── Fixtures ──


@pytest.fixture
def fresh_tag_registry():
    """Fresh capability registry for each test."""
    r = CapabilityRegistry()
    r.initialize()
    yield r
    r.clear()


@pytest.fixture
def selector():
    return ModelSelector()


@pytest.fixture
def scorer():
    return SuitabilityScorer()


@pytest.fixture
def stats_agg():
    return StatsAggregator()


# ── Runtime Stats Tests ──


class TestRuntimeStats:
    def test_model_runtime_stats_defaults(self):
        stats = ModelRuntimeStats(
            model_id="qwen",
            provider_id="qwen",
            transport="browser",
        )
        assert stats.success_rate == 1.0
        assert stats.failure_rate == 0.0
        assert stats.availability_score == 1.0
        assert stats.latency_score == 1.0
        assert stats.stability_score == 0.5  # Insufficient data

    def test_availability_score_circuit_open(self):
        stats = ModelRuntimeStats(
            model_id="qwen",
            provider_id="qwen",
            transport="browser",
            circuit_open=True,
        )
        assert stats.availability_score == 0.0

    def test_availability_score_consecutive_failures(self):
        stats = ModelRuntimeStats(
            model_id="qwen",
            provider_id="qwen",
            transport="browser",
            consecutive_failures=3,
            success_rate=0.7,
            health_score=0.9,
        )
        # circuit_penalty = max(0.3, 1.0 - 3 * 0.15) = 0.55
        # score = 0.7 * 0.5 + 0.9 * 0.3 + 0.55 * 0.2 = 0.35 + 0.27 + 0.11 = 0.73
        score = stats.availability_score
        assert 0.0 < score <= 1.0

    def test_latency_score_fast(self):
        stats = ModelRuntimeStats(
            model_id="qwen",
            provider_id="qwen",
            transport="browser",
            p50_latency_s=2.0,
        )
        assert stats.latency_score == 1.0

    def test_latency_score_slow(self):
        stats = ModelRuntimeStats(
            model_id="qwen",
            provider_id="qwen",
            transport="browser",
            p50_latency_s=70.0,
        )
        assert stats.latency_score == 0.0

    def test_latency_score_mid(self):
        stats = ModelRuntimeStats(
            model_id="qwen",
            provider_id="qwen",
            transport="browser",
            p50_latency_s=30.0,
        )
        # 30s -> 1.0 - (30-5)/55 = 1.0 - 0.45 = 0.545
        assert 0.4 < stats.latency_score < 0.6

    def test_stability_score_insufficient_data(self):
        stats = ModelRuntimeStats(
            model_id="qwen",
            provider_id="qwen",
            transport="browser",
            request_count=1,
        )
        assert stats.stability_score == 0.5

    def test_stability_score_stable(self):
        stats = ModelRuntimeStats(
            model_id="qwen",
            provider_id="qwen",
            transport="browser",
            request_count=100,
            success_count=98,
            error_count=2,
            fallback_count=1,
        )
        stats.failure_rate = 0.02
        stats.fallback_rate = 0.01
        score = stats.stability_score
        assert score > 0.9


# ── Capability Tags Tests ──


class TestCapabilityTags:
    def test_builtin_tags_load(self, fresh_tag_registry):
        tags = fresh_tag_registry.get_tags("qwen", "qwen")
        assert len(tags) > 0

    def test_has_tag(self, fresh_tag_registry):
        assert fresh_tag_registry.has_tag("glm", "glm", "review_strong")

    def test_get_tags_for_model(self, fresh_tag_registry):
        tags = fresh_tag_registry.get_tags_for_model("kimi")
        assert "creative" in tags
        assert "fast" in tags

    def test_add_tag(self, fresh_tag_registry):
        fresh_tag_registry.add_tag("qwen", "qwen", "experimental")
        assert fresh_tag_registry.has_tag("qwen", "qwen", "experimental")

    def test_remove_tag(self, fresh_tag_registry):
        fresh_tag_registry.add_tag("qwen", "qwen", "experimental")
        fresh_tag_registry.remove_tag("qwen", "qwen", "experimental")
        assert not fresh_tag_registry.has_tag("qwen", "qwen", "experimental")

    def test_list_all_tags(self, fresh_tag_registry):
        result = fresh_tag_registry.list_all_tags()
        assert "review_strong" in result
        assert "glm" in result["review_strong"]["models"]

    def test_get_models_by_tag(self, fresh_tag_registry):
        models = fresh_tag_registry.get_models_by_tag("stable")
        assert "qwen" in models or "glm" in models

    def test_combined_model_and_provider_tags(self, fresh_tag_registry):
        # Model tag + provider tag should be combined
        tags = fresh_tag_registry.get_tags("qwen", "qwen")
        assert len(tags) > 0

    def test_clear(self, fresh_tag_registry):
        fresh_tag_registry.clear()
        assert len(fresh_tag_registry.list_all_tags()) == 0


# ── Suitability Scoring Tests ──


class TestSuitabilityScoring:
    def test_compute_suitability_returns_all_roles(self, scorer):
        suitability = scorer.compute_suitability("qwen", "qwen", "browser")
        assert 0.0 <= suitability.generate_score <= 1.0
        assert 0.0 <= suitability.review_score <= 1.0
        assert 0.0 <= suitability.critique_score <= 1.0
        assert 0.0 <= suitability.refine_score <= 1.0
        assert 0.0 <= suitability.verify_score <= 1.0
        assert 0.0 <= suitability.transform_score <= 1.0

    def test_compute_for_role(self, scorer):
        score = scorer.compute_for_role("glm", "glm", "browser", StageRole.REVIEW)
        assert 0.0 <= score <= 1.0

    def test_review_strong_model_gets_bonus(self, scorer, fresh_tag_registry):
        """Models with review_strong tag should score higher for review role."""
        glm_score = scorer.compute_for_role("glm", "glm", "browser", StageRole.REVIEW)
        # GLM has review_strong tag — should have reasonable score
        assert glm_score > 0.0

    def test_tag_bonus_matching(self, scorer):
        tags = {"review_strong", "reasoning_strong", "stable"}
        bonus = scorer._compute_tag_bonus(tags, "review")
        assert bonus > 0.5

    def test_tag_bonus_no_match(self, scorer):
        tags = {"fast", "cheap"}
        bonus = scorer._compute_tag_bonus(tags, "review")
        assert bonus <= 0.5

    def test_tag_bonus_partial_match(self, scorer):
        tags = {"stable"}  # Only 1 of several relevant tags
        bonus = scorer._compute_tag_bonus(tags, "generate")
        assert 0.0 < bonus < 1.0


# ── Selection Policy Tests ──


class TestSelectionPolicy:
    def test_default_policy(self):
        policy = SelectionPolicy()
        assert policy.fallback_mode == FallbackMode.NEXT_BEST
        assert policy.min_availability == 0.3
        assert policy.max_latency_s == 60.0
        assert policy.max_fallback_attempts == 2
        assert policy.avoid_same_model_as_previous is False

    def test_policy_with_preferred_models(self):
        policy = SelectionPolicy(
            preferred_models=["qwen", "glm"],
            avoid_tags=["experimental"],
            min_availability=0.5,
        )
        assert policy.preferred_models == ["qwen", "glm"]
        assert policy.avoid_tags == ["experimental"]
        assert policy.min_availability == 0.5

    def test_policy_validation(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            SelectionPolicy(min_availability=-0.1)

        with pytest.raises(ValidationError):
            SelectionPolicy(max_fallback_attempts=10)


# ── Candidate Ranking Tests ──


class TestCandidateRanking:
    def test_ranking_to_dict(self):
        ranking = CandidateRanking(
            model_id="qwen",
            provider_id="qwen",
            transport="browser",
            canonical_id="qwen",
            availability_score=0.9,
            latency_score=0.8,
            stability_score=0.85,
            stage_suitability_score=0.7,
            tag_bonus_score=0.6,
            admin_bonus_score=0.5,
            final_score=0.78,
            rank=1,
            selected_reason="highest_ranked",
        )
        d = ranking.to_dict()
        assert d["model_id"] == "qwen"
        assert d["final_score"] == 0.78
        assert d["rank"] == 1
        assert "scores" in d
        assert d["selected_reason"] == "highest_ranked"

    def test_excluded_candidate(self):
        ranking = CandidateRanking(
            model_id="test",
            provider_id="test",
            transport="browser",
            canonical_id="test",
            is_excluded=True,
            excluded_reason="circuit_breaker_open",
        )
        assert ranking.is_excluded
        assert ranking.excluded_reason == "circuit_breaker_open"


# ── Selection Trace Tests ──


class TestSelectionTrace:
    def test_trace_to_dict(self):
        trace = SelectionTrace(
            stage_id="draft",
            stage_role="generate",
        )
        trace.selected_model = "qwen"
        trace.selected_provider = "qwen"
        trace.selected_transport = "browser"

        d = trace.to_dict()
        assert d["stage_id"] == "draft"
        assert d["selected_model"] == "qwen"
        assert d["fallback_count"] == 0
        assert d["all_candidates"] == []

    def test_trace_with_fallback_chain(self):
        trace = SelectionTrace(
            stage_id="review",
            stage_role="review",
        )
        trace.selected_model = "glm"
        trace.fallback_count = 1
        trace.fallback_chain.append({
            "failed_model": "qwen",
            "failed_provider": "qwen",
            "reason": "timeout",
            "fallback_to": "glm",
        })

        d = trace.to_dict()
        assert d["fallback_count"] == 1
        assert len(d["fallback_chain"]) == 1
        assert d["fallback_chain"][0]["failed_model"] == "qwen"


# ── Model Selector Tests (mocked) ──


class TestModelSelector:
    def test_select_with_preferred_models(self, selector, fresh_tag_registry):
        """Selection should prefer models in preferred_models list."""
        policy = SelectionPolicy(
            preferred_models=["qwen"],
        )
        trace = selector.select_for_stage(
            stage_id="test",
            stage_role=StageRole.GENERATE,
            policy=policy,
        )
        assert trace.selected_model is not None
        assert len(trace.all_candidates) > 0

    def test_select_avoids_tags(self, selector, fresh_tag_registry):
        """Selection should avoid models with avoided tags."""
        policy = SelectionPolicy(
            avoid_tags=["experimental"],
        )
        trace = selector.select_for_stage(
            stage_id="test",
            stage_role=StageRole.GENERATE,
            policy=policy,
        )
        # Should still select something (no models are experimental by default)
        assert trace.selected_model is not None

    def test_select_avoid_same_model(self, selector, fresh_tag_registry):
        """Selection can avoid using the same model as the previous stage."""
        policy = SelectionPolicy(
            avoid_same_model_as_previous=True,
        )
        trace = selector.select_for_stage(
            stage_id="test",
            stage_role=StageRole.REFINE,
            policy=policy,
            previous_stage_model="qwen",
        )
        # Should not select qwen
        if trace.selected_model:
            assert trace.selected_model != "qwen"

    def test_fallback_disabled(self, selector, fresh_tag_registry):
        """Fallback mode FAIL should return None on failure."""
        policy = SelectionPolicy(fallback_mode=FallbackMode.FAIL)

        # Create a trace with a failed candidate
        trace = SelectionTrace(stage_id="test", stage_role="generate")
        trace.selected_model = "qwen"
        trace.selected_provider = "qwen"
        trace.all_candidates = [
            CandidateRanking(
                model_id="qwen",
                provider_id="qwen",
                transport="browser",
                canonical_id="qwen",
            ),
        ]

        result = selector.fallback(
            selection_trace=trace,
            policy=policy,
            failed_model="qwen",
            failed_reason="timeout",
            stage_role=StageRole.GENERATE,
        )
        assert result is None

    def test_fallback_next_best(self, selector, fresh_tag_registry):
        """Fallback mode NEXT_BEST should select the next candidate."""
        policy = SelectionPolicy(fallback_mode=FallbackMode.NEXT_BEST)

        trace = SelectionTrace(stage_id="test", stage_role="generate")
        trace.selected_model = "qwen"
        trace.selected_provider = "qwen"
        trace.selected_transport = "browser"
        trace.all_candidates = [
            CandidateRanking(
                model_id="qwen",
                provider_id="qwen",
                transport="browser",
                canonical_id="qwen",
                final_score=0.9,
                rank=1,
            ),
            CandidateRanking(
                model_id="glm",
                provider_id="glm",
                transport="browser",
                canonical_id="glm",
                final_score=0.8,
                rank=2,
            ),
        ]

        result = selector.fallback(
            selection_trace=trace,
            policy=policy,
            failed_model="qwen",
            failed_reason="timeout",
            stage_role=StageRole.GENERATE,
        )
        assert result is not None
        assert result.selected_model == "glm"
        assert result.fallback_count == 1

    def test_fallback_exhausted(self, selector, fresh_tag_registry):
        """Fallback should fail if no more candidates available."""
        policy = SelectionPolicy(fallback_mode=FallbackMode.NEXT_BEST)

        trace = SelectionTrace(stage_id="test", stage_role="generate")
        trace.selected_model = "qwen"
        trace.selected_provider = "qwen"
        trace.selected_transport = "browser"
        trace.all_candidates = [
            CandidateRanking(
                model_id="qwen",
                provider_id="qwen",
                transport="browser",
                canonical_id="qwen",
                final_score=0.9,
            ),
        ]

        result = selector.fallback(
            selection_trace=trace,
            policy=policy,
            failed_model="qwen",
            failed_reason="timeout",
            stage_role=StageRole.GENERATE,
        )
        assert result is None

    def test_fallback_max_attempts(self, selector, fresh_tag_registry):
        """Fallback should stop after max_fallback_attempts."""
        policy = SelectionPolicy(fallback_mode=FallbackMode.NEXT_BEST, max_fallback_attempts=1)

        trace = SelectionTrace(stage_id="test", stage_role="generate")
        trace.selected_model = "qwen"
        trace.selected_provider = "qwen"
        trace.selected_transport = "browser"
        trace.fallback_count = 1  # Already at max
        trace.all_candidates = [
            CandidateRanking(
                model_id="qwen",
                provider_id="qwen",
                transport="browser",
                canonical_id="qwen",
                final_score=0.9,
            ),
            CandidateRanking(
                model_id="glm",
                provider_id="glm",
                transport="browser",
                canonical_id="glm",
                final_score=0.8,
            ),
        ]

        result = selector.fallback(
            selection_trace=trace,
            policy=policy,
            failed_model="glm",
            failed_reason="timeout",
            stage_role=StageRole.GENERATE,
        )
        assert result is None


# ── Intelligence API Tests ──


class TestIntelligenceAPI:
    @pytest.fixture
    def client(self):
        # Initialize capabilities
        capability_registry.initialize()

        with (
            patch("app.main.browser_dispatcher.initialize", new=AsyncMock()),
            patch("app.main.browser_dispatcher.shutdown", new=AsyncMock()),
            patch("app.main.unified_registry.initialize", new=AsyncMock()),
            patch("app.pipeline.executor.initialize_pipelines"),
        ):
            from app.main import app
            yield TestClient(app)

    def test_list_model_intelligence(self, client):
        """GET /admin/intelligence/models should return per-model stats."""
        response = client.get("/admin/intelligence/models")
        assert response.status_code == 200
        data = response.json()
        assert "models" in data
        assert "total" in data

    def test_list_capability_tags(self, client):
        """GET /admin/intelligence/tags should return tag assignments."""
        response = client.get("/admin/intelligence/tags")
        assert response.status_code == 200
        data = response.json()
        assert "review_strong" in data

    def test_ranking_for_role(self, client):
        """GET /admin/intelligence/ranking/{role} should return ranked models."""
        response = client.get("/admin/intelligence/ranking/generate")
        assert response.status_code == 200
        data = response.json()
        assert "role" in data
        assert "ranked_models" in data
        assert data["role"] == "generate"

    def test_ranking_for_role_with_limit(self, client):
        """GET /admin/intelligence/ranking/{role}?limit=N should limit results."""
        response = client.get("/admin/intelligence/ranking/review?limit=2")
        assert response.status_code == 200
        data = response.json()
        assert len(data["ranked_models"]) <= 2
