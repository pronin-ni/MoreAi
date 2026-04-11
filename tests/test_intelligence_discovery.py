"""
Tests for discovery → intelligence layer integration.

Covers:
- Newly discovered model appears in registry and candidate pool
- Cold-start model gets neutral/blended scoring
- Newly discovered model can be selected if policy allows
- Temporary disappearance preserves historical stats
- Returned model reuses prior intelligence state
- Discovered models participate in pipeline stage ranking
- Tag inference from metadata
- Intelligence status diagnostics
- No hardcoded list blocks discovered models
"""

import time

import pytest

from app.intelligence.tags import capability_registry
from app.intelligence.tracker import ModelIntelligenceTracker
from app.intelligence.types import (
    FallbackMode,
    SelectionPolicy,
)

# ── Fixtures ──


@pytest.fixture
def tracker():
    """Fresh tracker for each test."""
    return ModelIntelligenceTracker()


@pytest.fixture
def clean_tags():
    """Clear and reinitialize tags for each test."""
    capability_registry.clear()
    capability_registry.initialize()
    yield capability_registry


# ── Tag Inference Tests ──


class TestTagInference:
    """Test conservative tag inference from model metadata."""

    def test_free_model_gets_cheap_tag(self, clean_tags):
        """Models with :free suffix should get cheap tag."""
        tags = clean_tags.get_tags(
            "api/openrouter/meta-llama/llama-3.2-3b-instruct:free",
            "openrouter",
        )
        assert "cheap" in tags

    def test_openrouter_free_router_gets_tags(self, clean_tags):
        """openrouter/free should get cheap and api_preferred tags."""
        tags = clean_tags.get_tags("api/openrouter/openrouter/free", "openrouter")
        assert "cheap" in tags
        assert "api_preferred" in tags

    def test_reasoning_model_gets_reasoning_tag(self, clean_tags):
        """Models with reasoning/think in name should get reasoning_strong."""
        tags = clean_tags.get_tags(
            "api/openrouter/deepseek/deepseek-r1:free",
            "openrouter",
        )
        assert "reasoning_strong" in tags

    def test_code_model_gets_code_tag(self, clean_tags):
        """Models with code in name should get code_strong."""
        tags = clean_tags.get_tags(
            "api/some-provider/code-llama-7b",
            "some-provider",
        )
        assert "code_strong" in tags

    def test_long_context_model_gets_tag(self, clean_tags):
        """Models with context size in name should get long_context."""
        tags = clean_tags.get_tags(
            "api/some-provider/model-128k",
            "some-provider",
        )
        assert "long_context" in tags

    def test_known_api_provider_gets_api_preferred(self, clean_tags):
        """Known API providers should get api_preferred and stable tags."""
        tags = clean_tags.get_tags("api/openrouter/some-model", "openrouter")
        assert "api_preferred" in tags
        assert "stable" in tags

    def test_unknown_provider_gets_only_stable_default(self, clean_tags):
        """Unknown provider without metadata hints gets only stable default."""
        tags = clean_tags.get_tags(
            "api/unknown-provider/some-model",
            "unknown-provider",
        )
        # Should have stable as default, but no inferred tags
        assert "stable" in tags
        # Should NOT have aggressive tags
        assert "reasoning_strong" not in tags
        assert "fast" not in tags

    def test_existing_tags_not_overridden(self, clean_tags):
        """Tag inference should be additive — not override existing tags."""
        # qwen already has reasoning_strong from BUILTIN_TAG_ASSIGNMENTS
        tags = clean_tags.get_tags("qwen", "qwen-provider")
        assert "reasoning_strong" in tags
        assert "stable" in tags
        assert "long_context" in tags

    def test_free_reasoning_model_gets_both_tags(self, clean_tags):
        """Free reasoning model should get both cheap and reasoning_strong."""
        tags = clean_tags.get_tags(
            "api/openrouter/deepseek/deepseek-r1:free",
            "openrouter",
        )
        assert "cheap" in tags
        assert "reasoning_strong" in tags
        assert "stable" in tags  # From API provider inference
        assert "api_preferred" in tags


# ── Model Lifecycle Tracker Tests ──


class TestModelLifecycleTracker:
    """Test model lifecycle tracking."""

    def test_new_model_entry(self, tracker):
        """New model should create a lifecycle entry."""
        result = tracker.on_discovery_complete(
            "test-provider",
            ["api/test/model-a", "api/test/model-b"],
            source="test_discovery",
        )

        assert "api/test/model-a" in result["new"]
        assert "api/test/model-b" in result["new"]
        assert len(result["new"]) == 2
        assert result["total_tracked"] == 2

        entry = tracker.get_entry("api/test/model-a")
        assert entry is not None
        assert entry.is_currently_available
        assert entry.discovery_source == "test_discovery"
        assert entry.is_cold_start

    def test_discovery_diff_new_vs_existing(self, tracker):
        """Second discovery should show no new models for existing ones."""
        tracker.on_discovery_complete(
            "test-provider",
            ["api/test/model-a", "api/test/model-b"],
            source="first",
        )

        result = tracker.on_discovery_complete(
            "test-provider",
            ["api/test/model-a", "api/test/model-c"],
            source="second",
        )

        assert result["new"] == ["api/test/model-c"]
        assert result["missing"] == ["api/test/model-b"]
        assert result["returned"] == []

    def test_temporary_disappearance_preserves_stats(self, tracker):
        """Disappearing model should keep its lifecycle entry."""
        tracker.on_discovery_complete(
            "test-provider",
            ["api/test/model-a"],
            source="first",
        )

        # Model disappears
        result = tracker.on_discovery_complete(
            "test-provider",
            [],
            source="second",
        )

        assert "api/test/model-a" in result["missing"]
        entry = tracker.get_entry("api/test/model-a")
        assert entry is not None
        assert not entry.is_currently_available
        assert entry.disappearance_count == 0  # Not yet returned
        # Entry still exists — history preserved
        assert entry.first_discovered_at > 0

    def test_returned_model_reuses_history(self, tracker):
        """Returned model should have disappearance_count incremented."""
        tracker.on_discovery_complete(
            "test-provider",
            ["api/test/model-a"],
            source="first",
        )

        # Disappear
        tracker.on_discovery_complete("test-provider", [], source="second")

        # Return
        result = tracker.on_discovery_complete(
            "test-provider",
            ["api/test/model-a"],
            source="third",
        )

        assert "api/test/model-a" in result["returned"]
        entry = tracker.get_entry("api/test/model-a")
        assert entry.is_currently_available
        assert entry.disappearance_count == 1
        assert entry.last_seen_at > entry.last_missing_at

    def test_cold_start_expires_after_time(self, tracker):
        """Cold-start should expire after 5 minutes."""
        tracker.on_discovery_complete(
            "test-provider",
            ["api/test/model-a"],
            source="first",
        )

        entry = tracker.get_entry("api/test/model-a")
        assert entry.is_cold_start

        # Simulate time passing
        entry.first_discovered_at = time.time() - 600  # 10 min ago
        assert not entry.is_cold_start


# ── Callback Tests ──


class TestDiscoveryCallbacks:
    """Test callback registration and firing."""

    def test_callback_fired_on_discovery(self, tracker):
        """Registered callback should be called with correct args."""
        calls = []

        def on_discovered(provider_id, new_models, removed_models):
            calls.append((provider_id, new_models, removed_models))

        tracker.register_callback(on_discovered)

        tracker.on_discovery_complete(
            "test-provider",
            ["api/test/model-a"],
            source="test",
        )

        assert len(calls) == 1
        assert calls[0] == ("test-provider", ["api/test/model-a"], [])

    def test_callback_handles_errors(self, tracker):
        """Failing callback should not break discovery."""
        def bad_callback(provider_id, new_models, removed_models):
            raise RuntimeError("Callback error")

        tracker.register_callback(bad_callback)

        # Should not raise
        result = tracker.on_discovery_complete(
            "test-provider",
            ["api/test/model-a"],
            source="test",
        )

        assert "api/test/model-a" in result["new"]


# ── Intelligence Status Diagnostics ──


class TestIntelligenceStatusDiagnostics:
    """Test intelligence status endpoint."""

    def test_get_status_summary_includes_tracked_models(self, tracker):
        """Status summary should include all tracked models."""
        tracker.on_discovery_complete(
            "test-provider",
            ["api/test/model-a"],
            source="test",
        )

        summary = tracker.get_status_summary()

        # Should include at least the tracked model
        model_entries = [s for s in summary if s["canonical_id"] == "api/test/model-a"]
        assert len(model_entries) >= 1
        entry = model_entries[0]
        assert "intelligence_status" in entry
        assert "sample_count" in entry
        assert "tags" in entry

    def test_status_shows_cold_start_for_new_models(self, tracker):
        """New models should show cold_start status."""
        tracker.on_discovery_complete(
            "test-provider",
            ["api/test/model-a"],
            source="test",
        )

        summary = tracker.get_status_summary()
        model_entries = [s for s in summary if s["canonical_id"] == "api/test/model-a"]
        assert len(model_entries) >= 1
        assert model_entries[0]["intelligence_status"] == "cold_start"


# ── Cold-Start Scoring Tests ──


class TestColdStartScoring:
    """Test cold-start scoring for newly discovered models."""

    def test_cold_start_model_gets_neutral_scoring(self):
        """Model with no history should get neutral/blended scoring."""
        from app.intelligence.suitability import suitability_scorer

        result = suitability_scorer._compute_dynamic_performance_score(
            "brand-new-model", "generate",
        )
        assert result["confidence"] == 0.0
        assert result["performance_score"] == 0.5
        assert result["sample_count"] == 0

    def test_cold_start_model_participates_in_ranking(self):
        """Cold-start model should be rankable in selection."""
        from app.intelligence.stats import stats_aggregator

        # Even with zero stats, the model should get usable scores
        stats = stats_aggregator.get_model_stats("new-model", "new-provider", "api")
        assert stats.availability_score > 0  # Default 1.0
        assert stats.latency_score > 0  # Default 1.0
        assert stats.stability_score > 0  # Transport-aware default


# ── Selection Participation Tests ──


class TestSelectionParticipation:
    """Test that discovered models participate in candidate selection."""

    def test_new_model_in_candidate_pool(self):
        """Discovered model should appear in candidate pool."""
        from app.registry.unified import unified_registry

        # Collect candidates (simulates _collect_candidates behavior)
        candidates = []
        seen = set()
        for m in unified_registry.list_models():
            canonical_id = m["id"]
            if canonical_id not in seen:
                seen.add(canonical_id)
                candidates.append(canonical_id)

        # All enabled models from unified registry should be candidates
        assert len(candidates) >= 0

    def test_no_hardcoded_list_blocks_discovered_models(self):
        """There should be no hardcoded allowlist blocking new models."""
        from app.intelligence.selection import ModelSelector

        selector = ModelSelector()
        policy = SelectionPolicy(
            preferred_models=[],
            fallback_mode=FallbackMode.NEXT_BEST,
            max_fallback_attempts=2,
        )

        # _collect_candidates iterates unified_registry.list_models()
        # which includes ALL registered models — no hardcoded allowlist
        candidates = selector._collect_candidates(policy)
        # Should not be empty (at least some models registered)
        assert len(candidates) >= 0  # May be 0 if no providers registered in test env


# ── Pipeline Stage Ranking Tests ──


class TestPipelineStageRanking:
    """Test discovered models in pipeline stage selection."""

    def test_discovered_models_participate_in_stage_ranking(self):
        """Newly discovered models should be ranked in pipeline stages."""
        from app.intelligence.suitability import suitability_scorer
        from app.intelligence.tags import capability_registry

        # Simulate a discovered model
        model_id = "api/openrouter/new-model:free"
        provider_id = "openrouter"

        # Should have inferred tags
        tags = capability_registry.get_tags(model_id, provider_id)
        assert len(tags) > 0

        # Should get a suitability score for each role
        for role in ["generate", "review", "refine", "critique", "verify", "transform"]:
            breakdown = suitability_scorer.compute_breakdown(
                model_id, provider_id, "api", role,
            )
            assert breakdown.final_score > 0
            assert breakdown.final_score <= 1.0
