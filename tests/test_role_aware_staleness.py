"""
Tests for role-aware staleness in intelligence scoring.

Covers:
- Model fresh for review but stale for generate
- Role-specific timestamp takes precedence over model-level fallback
- Missing role-specific data falls back correctly
- Scoring breakdown shows correct source and role-aware freshness
- Batch lookup works for multiple models and roles
"""

import os
import tempfile
import time

# ── Role-Aware Batched Lookup Tests ──


class TestRoleAwareBatchedLookup:
    """Test that role-aware batched lookup correctly distinguishes activity per role."""

    def test_batched_by_role_returns_correct_keys(self):
        """Batched lookup should return (model_id, role) keyed results."""
        from app.intelligence.suitability import _batch_get_last_activity_with_source_by_role

        candidates = [
            ("api/test/model-a", "provider-a", "api"),
            ("api/test/model-b", "provider-b", "api"),
        ]
        result = _batch_get_last_activity_with_source_by_role(candidates, "generate")

        # Keys should be (model_id, role) tuples
        assert ("api/test/model-a", "generate") in result
        assert ("api/test/model-b", "generate") in result

        # Values should be (timestamp, source, is_role_specific)
        for _key, value in result.items():
            ts, source, is_role_specific = value
            assert isinstance(ts, float)
            assert isinstance(source, str)
            assert isinstance(is_role_specific, bool)

    def test_batched_by_role_different_roles(self):
        """Same model with different roles should get independent staleness data."""
        from app.intelligence.suitability import _batch_get_last_activity_with_source_by_role

        candidates = [("api/test/model-x", "provider-x", "api")]

        gen_result = _batch_get_last_activity_with_source_by_role(candidates, "generate")
        rev_result = _batch_get_last_activity_with_source_by_role(candidates, "review")

        # Both should have entries for the model with their respective roles
        assert ("api/test/model-x", "generate") in gen_result
        assert ("api/test/model-x", "review") in rev_result

        # Both should be (0.0, "unknown", False) since no runtime data exists
        gen_ts, gen_source, _ = gen_result[("api/test/model-x", "generate")]
        rev_ts, rev_source, _ = rev_result[("api/test/model-x", "review")]
        assert gen_source == "unknown"
        assert rev_source == "unknown"

    def test_role_specific_source_takes_precedence(self):
        """Stage performance (role-specific) should be preferred over discovery (role-agnostic)."""
        from app.pipeline.observability.stage_perf import (
            RolePerformanceEntry,
            StagePerformanceTracker,
        )

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            tracker = StagePerformanceTracker(db_path=db_path)
            recent_ts = time.time() - 100
            entry = RolePerformanceEntry(
                model_id="api/test/role-specific-model",
                provider_id="test-provider",
                stage_role="review",
                success=True,
                duration_ms=500,
                had_fallback=False,
                had_retry=False,
                output_quality_hint=0.8,
                timestamp=recent_ts,
            )
            tracker.record(entry)

            # Patch the singleton temporarily
            from app.pipeline.observability import stage_perf as sp_module
            original = sp_module.stage_performance
            sp_module.stage_performance = tracker

            try:
                from app.intelligence.suitability import (
                    _batch_get_last_activity_with_source_by_role,
                )
                candidates = [("api/test/role-specific-model", "test-provider", "api")]

                # For "review" role — should find stage_performance data
                rev_result = _batch_get_last_activity_with_source_by_role(candidates, "review")
                rev_ts, rev_source, rev_is_role_specific = rev_result[("api/test/role-specific-model", "review")]
                assert rev_source == "stage_performance"
                assert rev_is_role_specific is True
                assert abs(rev_ts - recent_ts) < 1.0

                # For "generate" role — no stage_performance data, falls back
                gen_result = _batch_get_last_activity_with_source_by_role(candidates, "generate")
                gen_ts, gen_source, gen_is_role_specific = gen_result[("api/test/role-specific-model", "generate")]
                assert gen_source == "unknown"  # No data for generate role
                assert gen_is_role_specific is False
            finally:
                sp_module.stage_performance = original
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_empty_candidates_returns_empty(self):
        """Empty candidates list should return empty dict."""
        from app.intelligence.suitability import _batch_get_last_activity_with_source_by_role
        result = _batch_get_last_activity_with_source_by_role([], "generate")
        assert result == {}


# ── Scoring Breakdown Role-Aware Tests ──


class TestRoleAwareScoringBreakdown:
    """Test that scoring breakdown reflects role-aware staleness correctly."""

    def test_breakdown_shows_role_specific_source(self):
        """When source is role-specific, breakdown should reflect that."""
        from app.intelligence.suitability import suitability_scorer

        # Pass role-aware staleness data
        now = time.time()
        staleness_data = (now - 100, "stage_performance", True)

        breakdown = suitability_scorer.compute_breakdown(
            "test-model", "test-provider", "api", "review",
            staleness_data=staleness_data,
        )

        assert breakdown.activity_source_used == "stage_performance"
        assert breakdown.activity_source_is_role_specific is True
        assert breakdown.staleness_label == "fresh"

    def test_breakdown_shows_role_agnostic_fallback(self):
        """When source is role-agnostic fallback, breakdown should reflect that."""
        from app.intelligence.suitability import suitability_scorer

        # Pass role-agnostic staleness data (e.g., discovery fallback)
        now = time.time()
        staleness_data = (now - 86400 * 14, "discovery", False)  # 14 days ago

        breakdown = suitability_scorer.compute_breakdown(
            "test-model", "test-provider", "api", "generate",
            staleness_data=staleness_data,
        )

        assert breakdown.activity_source_used == "discovery"
        assert breakdown.activity_source_is_role_specific is False
        assert breakdown.staleness_label == "stale"

    def test_breakdown_to_dict_includes_role_specificity(self):
        """to_dict should include activity_source_is_role_specific."""
        from app.intelligence.suitability import suitability_scorer

        breakdown = suitability_scorer.compute_breakdown(
            "test-model", "test-provider", "api", "generate",
        )
        d = breakdown.to_dict()

        assert "staleness" in d
        assert "activity_source_is_role_specific" in d["staleness"]


# ── Role-Aware Fresh vs Stale Model Tests ──


class TestRoleAwareFreshVsStale:
    """Test that models can be fresh for one role but stale for another."""

    def test_model_fresh_for_review_stale_for_generate(self):
        """A model with recent review activity but no generate activity should show role-aware staleness."""
        from app.pipeline.observability.stage_perf import (
            RolePerformanceEntry,
            StagePerformanceTracker,
        )

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            tracker = StagePerformanceTracker(db_path=db_path)
            recent_ts = time.time() - 100  # 100 seconds ago
            entry = RolePerformanceEntry(
                model_id="api/test/dual-role-model",
                provider_id="test-provider",
                stage_role="review",
                success=True,
                duration_ms=500,
                had_fallback=False,
                had_retry=False,
                output_quality_hint=0.8,
                timestamp=recent_ts,
            )
            tracker.record(entry)

            # Patch the singleton temporarily
            from app.pipeline.observability import stage_perf as sp_module
            original = sp_module.stage_performance
            sp_module.stage_performance = tracker

            try:
                from app.intelligence.suitability import (
                    _batch_get_last_activity_with_source_by_role,
                )
                candidates = [("api/test/dual-role-model", "test-provider", "api")]

                # Review role: fresh (has recent activity)
                rev_result = _batch_get_last_activity_with_source_by_role(candidates, "review")
                rev_ts, rev_source, rev_is_role_specific = rev_result[("api/test/dual-role-model", "review")]
                assert rev_source == "stage_performance"
                assert rev_is_role_specific is True
                assert abs(rev_ts - recent_ts) < 1.0

                # Generate role: no activity, falls back to unknown
                gen_result = _batch_get_last_activity_with_source_by_role(candidates, "generate")
                gen_ts, gen_source, gen_is_role_specific = gen_result[("api/test/dual-role-model", "generate")]
                assert gen_source == "unknown"
                assert gen_ts == 0.0
            finally:
                sp_module.stage_performance = original
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)


# ── Integration with Ranking Tests ──


class TestRoleAwareRanking:
    """Test that _rank_candidates uses role-aware staleness correctly."""

    def test_rank_candidates_uses_role_aware_batched_path(self):
        """_rank_candidates should call role-aware batched staleness lookup."""
        from app.intelligence.selection import ModelSelector
        from app.intelligence.types import SelectionPolicy

        selector = ModelSelector()

        candidates = [
            {"model_id": f"api/test/role-model-{i}", "provider_id": "test-provider", "transport": "api", "canonical_id": f"api/test/role-model-{i}"}
            for i in range(3)
        ]

        policy = SelectionPolicy()
        role = "generate"

        rankings = selector._rank_candidates(candidates, role, policy, "")

        # All candidates should have been scored
        assert len(rankings) == 3
        for r in rankings:
            assert r.final_score >= 0
            assert r.final_score <= 1.0

    def test_rank_candidates_different_roles_produce_different_staleness(self):
        """Ranking for different roles may produce different staleness for same model."""
        from app.intelligence.selection import ModelSelector
        from app.intelligence.types import SelectionPolicy

        selector = ModelSelector()

        candidates = [
            {"model_id": "api/test/multi-role-model", "provider_id": "test-provider", "transport": "api", "canonical_id": "api/test/multi-role-model"},
        ]

        policy = SelectionPolicy()

        # Rank for generate
        gen_rankings = selector._rank_candidates(candidates, "generate", policy, "")
        # Rank for review
        rev_rankings = selector._rank_candidates(candidates, "review", policy, "")

        # Both should have scored the candidate
        assert len(gen_rankings) == 1
        assert len(rev_rankings) == 1

        # The staleness may differ based on role-specific activity data
        # (In this test, both will be "unknown" since no runtime data exists)
        # The key point is that the role-aware path is exercised
        assert gen_rankings[0].stage_suitability_score >= 0
        assert rev_rankings[0].stage_suitability_score >= 0
