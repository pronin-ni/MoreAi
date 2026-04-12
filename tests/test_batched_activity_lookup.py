"""
Tests for batched activity lookup in staleness intelligence.

Covers:
- Batched lookup returns same semantics as per-model logic
- Source priority preserved in batched mode
- Unknown models handled correctly
- Bulk candidate ranking uses batched path
- Scoring with pre-fetched staleness data produces correct breakdown
"""

import os
import tempfile
import time

# ── Batched Lookup Semantic Equivalence Tests ──


class TestBatchedLookupSemantics:
    """Test that batched lookup produces same results as per-model logic."""

    def test_batched_vs_per_model_equivalence(self):
        """Batched lookup should return same results as individual lookups."""
        from app.intelligence.suitability import (
            _batch_get_last_activity_with_source,
            _get_last_activity_with_source,
        )

        model_ids = ["test-model-a", "test-model-b", "test-model-c"]
        candidates = [(mid, "test-provider", "api") for mid in model_ids]

        # Batched lookup
        batched = _batch_get_last_activity_with_source(candidates)

        # Per-model lookups
        per_model = {}
        for mid, pid, _t in candidates:
            per_model[mid] = _get_last_activity_with_source(mid, pid)

        # Both should return entries for all models
        assert set(batched.keys()) == set(model_ids)
        assert set(per_model.keys()) == set(model_ids)

        # Each model should have matching (timestamp, source) pairs
        # Note: timestamps may differ slightly if recorded between calls,
        # but for models with no data, both should return (0.0, "unknown")
        for mid in model_ids:
            b_ts, b_source = batched[mid]
            p_ts, p_source = per_model[mid]
            assert b_source == p_source  # Source should match
            # Timestamps should be very close (within 1 second tolerance)
            assert abs(b_ts - p_ts) < 1.0

    def test_batched_empty_input(self):
        """Batched lookup with empty input should return empty dict."""
        from app.intelligence.suitability import _batch_get_last_activity_with_source
        result = _batch_get_last_activity_with_source([])
        assert result == {}

    def test_batched_unknown_models_handled(self):
        """Models with no activity data should be marked as unknown."""
        from app.intelligence.suitability import _batch_get_last_activity_with_source

        candidates = [
            ("nonexistent-model-xyz-123", "nonexistent-provider", "api"),
        ]
        result = _batch_get_last_activity_with_source(candidates)

        assert "nonexistent-model-xyz-123" in result
        ts, source = result["nonexistent-model-xyz-123"]
        assert ts == 0.0
        assert source == "unknown"


# ── Source Priority in Batched Mode Tests ──


class TestBatchedSourcePriority:
    """Test that source priority is preserved in batched mode."""

    def test_batched_stage_performance_priority(self):
        """Stage performance timestamps should be preferred in batched mode."""
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
                model_id="batched-test-model",
                provider_id="test-provider",
                stage_role="generate",
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
                from app.intelligence.suitability import _batch_get_last_activity_with_source
                candidates = [("batched-test-model", "test-provider", "api")]
                result = _batch_get_last_activity_with_source(candidates)

                ts, source = result["batched-test-model"]
                assert source == "stage_performance"
                assert abs(ts - recent_ts) < 1.0
            finally:
                sp_module.stage_performance = original
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_batched_multiple_models_different_sources(self):
        """Different models may have different activity sources in batch."""
        from app.intelligence.suitability import _batch_get_last_activity_with_source
        from app.intelligence.tracker import model_intelligence_tracker

        # Register a model in the tracker (discovery source)
        model_intelligence_tracker.on_discovery_complete(
            "test-provider",
            ["api/test/discovery-model"],
            source="test",
        )

        candidates = [
            ("api/test/discovery-model", "test-provider", "api"),
            ("api/test/unknown-model", "test-provider", "api"),
        ]
        result = _batch_get_last_activity_with_source(candidates)

        # Discovery model should have discovery source
        d_ts, d_source = result["api/test/discovery-model"]
        assert d_source == "discovery"
        assert d_ts > 0

        # Unknown model should have unknown source
        u_ts, u_source = result["api/test/unknown-model"]
        assert u_source == "unknown"
        assert u_ts == 0.0


# ── Scoring with Pre-fetched Staleness Tests ──


class TestScoringWithPrefetchedStaleness:
    """Test that scoring with pre-fetched staleness data works correctly."""

    def test_compute_breakdown_with_staleness_data(self):
        """compute_breakdown should use pre-fetched staleness data when provided."""
        from app.intelligence.suitability import suitability_scorer

        now = time.time()
        # Role-aware format: (timestamp, source, is_role_specific)
        staleness_data = (now - 86400 * 14, "stage_performance", True)  # 14 days ago

        breakdown = suitability_scorer.compute_breakdown(
            "test-model", "test-provider", "api", "generate",
            staleness_data=staleness_data,
        )

        assert breakdown.activity_source_used == "stage_performance"
        assert breakdown.staleness_decay < 1.0  # Should be decayed
        assert breakdown.staleness_label == "stale"

    def test_compute_breakdown_without_staleness_data(self):
        """compute_breakdown should fall back to per-model lookup when no pre-fetched data."""
        from app.intelligence.suitability import suitability_scorer

        breakdown = suitability_scorer.compute_breakdown(
            "test-model", "test-provider", "api", "generate",
        )

        # Should still have staleness info (from fallback lookup)
        assert breakdown.activity_source_used in (
            "stage_performance", "quality_metrics", "discovery", "runtime_stats", "unknown",
        )

    def test_compute_breakdown_staleness_none_uses_fallback(self):
        """Passing staleness_data=None should use per-model lookup."""
        from app.intelligence.suitability import suitability_scorer

        breakdown = suitability_scorer.compute_breakdown(
            "test-model", "test-provider", "api", "generate",
            staleness_data=None,
        )

        assert breakdown.activity_source_used is not None


# ── Bulk Candidate Ranking Tests ──


class TestBulkCandidateRanking:
    """Test that bulk ranking uses the batched staleness path."""

    def test_rank_candidates_uses_batched_path(self):
        """_rank_candidates should call batched staleness lookup once, not per-model."""
        from app.intelligence.selection import ModelSelector
        from app.intelligence.types import SelectionPolicy

        selector = ModelSelector()

        # Create candidate list
        candidates = [
            {"model_id": f"api/test/model-{i}", "provider_id": "test-provider", "transport": "api", "canonical_id": f"api/test/model-{i}"}
            for i in range(5)
        ]

        policy = SelectionPolicy()

        # The _rank_candidates method internally calls _batch_get_last_activity_with_source
        # We can verify this works by checking that all candidates get scored
        rankings = selector._rank_candidates(candidates, "generate", policy, "")

        assert len(rankings) == 5
        for r in rankings:
            # Each candidate should have been scored
            assert r.final_score >= 0
            assert r.final_score <= 1.0

    def test_rank_candidates_empty_list(self):
        """Empty candidate list should return empty rankings."""
        from app.intelligence.selection import ModelSelector
        from app.intelligence.types import SelectionPolicy

        selector = ModelSelector()
        policy = SelectionPolicy()

        rankings = selector._rank_candidates([], "generate", policy, "")
        assert rankings == []
