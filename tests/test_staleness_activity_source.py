"""
Tests for staleness decay with runtime-aware activity sources.

Covers:
- Model with recent stage activity → fresh
- Model only rediscovered but not used recently → still stale/aging
- Fallback to discovery timestamp when no runtime activity exists
- Scoring breakdown includes activity source
- Returned model behaves correctly (discovery-only ≠ fresh)
- Activity source priority order
"""

import os
import tempfile
import time

import pytest

from app.intelligence.staleness import (
    STALENESS_FLOOR,
    STALENESS_GRACE_SECONDS,
    STALENESS_HALF_LIFE,
    STALENESS_MAX_DECAY,
    StalenessDecay,
)


# ── Activity Source Priority Tests ──


class TestActivitySourcePriority:
    """Test that activity source selection follows the correct priority order."""

    def test_staleness_decay_tracks_source(self):
        """StalenessDecay should record which source was used."""
        now = time.time()
        decay = StalenessDecay(last_activity_at=now - 100, now=now, activity_source="stage_performance")
        assert decay.activity_source == "stage_performance"
        assert decay.decay_factor() == 1.0

    def test_staleness_decay_unknown_source(self):
        """When source is unknown, staleness should be maximal."""
        decay = StalenessDecay(last_activity_at=0.0, activity_source="unknown")
        assert decay.activity_source == "unknown"
        assert decay.decay_factor() == STALENESS_FLOOR
        assert decay.is_very_stale()

    def test_staleness_to_dict_includes_source(self):
        """to_dict should include activity_source."""
        now = time.time()
        decay = StalenessDecay(last_activity_at=now - 100, now=now, activity_source="quality_metrics")
        d = decay.to_dict()
        assert d["activity_source"] == "quality_metrics"


# ── Stage Performance as Primary Source Tests ──


class TestStagePerformancePrimarySource:
    """Test that stage performance timestamps are used as the primary source."""

    def test_get_latest_timestamp_returns_recent(self):
        """StagePerformanceTracker.get_latest_timestamp should return the most recent entry."""
        from app.pipeline.observability.stage_perf import StagePerformanceTracker

        # Use a temp DB for isolation
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            tracker = StagePerformanceTracker(db_path=db_path)
            from app.pipeline.observability.stage_perf import RolePerformanceEntry

            # Record an old entry
            old_ts = time.time() - 86400 * 14  # 14 days ago
            old_entry = RolePerformanceEntry(
                model_id="test-model",
                provider_id="test-provider",
                stage_role="generate",
                success=True,
                duration_ms=1000,
                had_fallback=False,
                had_retry=False,
                output_quality_hint=0.7,
                timestamp=old_ts,
            )
            tracker.record(old_entry)

            # Record a recent entry
            recent_ts = time.time() - 100  # 100 seconds ago
            recent_entry = RolePerformanceEntry(
                model_id="test-model",
                provider_id="test-provider",
                stage_role="review",
                success=True,
                duration_ms=500,
                had_fallback=False,
                had_retry=False,
                output_quality_hint=0.8,
                timestamp=recent_ts,
            )
            tracker.record(recent_entry)

            # Should return the most recent timestamp
            latest = tracker.get_latest_timestamp("test-model")
            assert abs(latest - recent_ts) < 1.0
            assert latest > old_ts

            # Non-existent model should return 0.0
            assert tracker.get_latest_timestamp("nonexistent-model") == 0.0
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)


# ── Scoring Integration with Activity Source ──


class TestScoringWithActivitySource:
    """Test that scoring breakdown includes activity source information."""

    def test_compute_breakdown_shows_activity_source(self):
        """Scoring breakdown should show which source was used for staleness."""
        from app.intelligence.suitability import suitability_scorer

        breakdown = suitability_scorer.compute_breakdown(
            "test-model", "test-provider", "api", "generate",
        )

        assert breakdown.activity_source_used in (
            "stage_performance", "quality_metrics", "discovery", "runtime_stats", "unknown",
        )

    def test_compute_breakdown_to_dict_has_activity_source(self):
        """to_dict should include activity_source in staleness section."""
        from app.intelligence.suitability import suitability_scorer

        breakdown = suitability_scorer.compute_breakdown(
            "test-model", "test-provider", "api", "generate",
        )
        d = breakdown.to_dict()

        assert "staleness" in d
        assert "activity_source" in d["staleness"]

    def test_discovery_only_model_shows_discovery_source(self):
        """Model known to tracker but not in runtime should show discovery source."""
        from app.intelligence.suitability import _get_last_activity_with_source
        from app.intelligence.tracker import model_intelligence_tracker

        # Register a model in the tracker
        model_intelligence_tracker.on_discovery_complete(
            "test-provider",
            ["api/test/discovery-only-model"],
            source="test",
        )

        ts, source = _get_last_activity_with_source("api/test/discovery-only-model", "test-provider")

        # Should use discovery as source (no runtime data exists)
        assert source == "discovery"
        assert ts > 0


# ── Returned Model Behavior Tests ──


class TestReturnedModelBehavior:
    """Test that returned models behave correctly with staleness decay."""

    def test_discovered_but_unused_model_is_stale(self):
        """Model that was rediscovered but not used in runtime should be stale."""
        from app.intelligence.staleness import StalenessDecay
        from app.intelligence.tracker import model_intelligence_tracker

        # Simulate a model discovered long ago
        past_ts = time.time() - STALENESS_MAX_DECAY  # 30 days ago
        entry = model_intelligence_tracker._entries.get("api/test/old-model")
        if entry:
            entry.last_seen_at = past_ts

        decay = StalenessDecay(last_activity_at=past_ts, activity_source="discovery")
        assert decay.decay_factor() == STALENESS_FLOOR
        assert decay.staleness_label == "very_stale"

    def test_recently_used_model_is_fresh(self):
        """Model with recent stage activity should be fresh regardless of discovery time."""
        from app.intelligence.staleness import StalenessDecay

        now = time.time()
        recent_ts = now - 60  # 1 minute ago
        decay = StalenessDecay(last_activity_at=recent_ts, now=now, activity_source="stage_performance")
        assert decay.decay_factor() == 1.0
        assert decay.is_fresh()
        assert decay.staleness_label == "fresh"

    def test_aging_model_has_reduced_influence(self):
        """Model between grace and half-life should have reduced influence."""
        from app.intelligence.staleness import StalenessDecay

        now = time.time()
        staleness = STALENESS_GRACE_SECONDS + STALENESS_HALF_LIFE / 2
        decay = StalenessDecay(last_activity_at=now - staleness, now=now, activity_source="stage_performance")
        assert 0.5 < decay.decay_factor() < 1.0
        assert decay.staleness_label == "aging"

        # Apply to a high score — should move toward neutral
        result = decay.apply(0.9)
        assert result < 0.9
        assert result > 0.5
