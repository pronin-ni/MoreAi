"""
Tests for staleness decay in historical model intelligence.

Covers:
- Fresh model: no/low decay
- Stale model: reduced confidence
- Very stale model: approaches neutral behavior
- Returned model after long absence gets decayed historical influence
- Scoring breakdown includes staleness info
- Decay formula correctness
"""

import time

import pytest

from app.intelligence.staleness import (
    STALENESS_FLOOR,
    STALENESS_GRACE_SECONDS,
    STALENESS_HALF_LIFE,
    STALENESS_MAX_DECAY,
    STALENESS_NEUTRAL_TARGET,
    StalenessDecay,
    compute_decay,
)


# ── Decay Formula Tests ──


class TestStalenessDecayFormula:
    """Test the core staleness decay formula."""

    def test_fresh_model_no_decay(self):
        """Model with recent activity should have decay factor 1.0."""
        now = time.time()
        decay = StalenessDecay(last_activity_at=now - 100, now=now)
        assert decay.decay_factor() == 1.0
        assert decay.is_fresh()
        assert decay.staleness_label == "fresh"

    def test_within_grace_period(self):
        """Model within grace period should have no decay."""
        now = time.time()
        decay = StalenessDecay(last_activity_at=now - STALENESS_GRACE_SECONDS, now=now)
        assert decay.decay_factor() == 1.0

    def test_half_life_decay(self):
        """At half-life, decay factor should be approximately 0.5."""
        now = time.time()
        staleness = STALENESS_GRACE_SECONDS + STALENESS_HALF_LIFE
        decay = StalenessDecay(last_activity_at=now - staleness, now=now)
        assert 0.45 <= decay.decay_factor() <= 0.55
        assert decay.staleness_label == "stale"

    def test_max_decay_floor(self):
        """At max decay, factor should be at floor."""
        now = time.time()
        decay = StalenessDecay(last_activity_at=now - STALENESS_MAX_DECAY, now=now)
        assert decay.decay_factor() == STALENESS_FLOOR
        assert decay.is_very_stale()
        assert decay.staleness_label == "very_stale"

    def test_beyond_max_decay_stays_at_floor(self):
        """Beyond max decay, factor should stay at floor."""
        now = time.time()
        decay = StalenessDecay(last_activity_at=now - STALENESS_MAX_DECAY * 2, now=now)
        assert decay.decay_factor() == STALENESS_FLOOR

    def test_no_activity_defaults_to_max_stale(self):
        """Model with last_activity_at=0 should be at floor."""
        decay = StalenessDecay(last_activity_at=0.0)
        assert decay.decay_factor() == STALENESS_FLOOR
        assert decay.is_very_stale()

    def test_aging_label_between_grace_and_half_life(self):
        """Model between grace and half-life should be 'aging'."""
        now = time.time()
        staleness = STALENESS_GRACE_SECONDS + STALENESS_HALF_LIFE / 2
        decay = StalenessDecay(last_activity_at=now - staleness, now=now)
        assert decay.staleness_label == "aging"


# ── Decay Application Tests ──


class TestDecayApplication:
    """Test how decay is applied to scores."""

    def test_fresh_score_unchanged(self):
        """Fresh model (decay=1.0) should return score unchanged."""
        now = time.time()
        decay = StalenessDecay(last_activity_at=now - 100, now=now)
        result = decay.apply(0.8)
        assert result == 0.8

    def test_stale_score_decays_toward_neutral(self):
        """Stale model should have score decayed toward neutral target."""
        now = time.time()
        staleness = STALENESS_GRACE_SECONDS + STALENESS_HALF_LIFE
        decay = StalenessDecay(last_activity_at=now - staleness, now=now)
        factor = decay.decay_factor()

        # Score 0.9 with ~0.5 decay → 0.9 * 0.5 + 0.5 * 0.5 = 0.7
        result = decay.apply(0.9)
        expected = 0.9 * factor + STALENESS_NEUTRAL_TARGET * (1 - factor)
        assert abs(result - expected) < 0.01

    def test_low_score_stale_moves_toward_neutral(self):
        """Low-performing stale model should move toward neutral."""
        now = time.time()
        staleness = STALENESS_GRACE_SECONDS + STALENESS_HALF_LIFE
        decay = StalenessDecay(last_activity_at=now - staleness, now=now)
        factor = decay.decay_factor()

        # Score 0.2 with ~0.5 decay → 0.2 * 0.5 + 0.5 * 0.5 = 0.35
        result = decay.apply(0.2)
        expected = 0.2 * factor + STALENESS_NEUTRAL_TARGET * (1 - factor)
        assert abs(result - expected) < 0.01
        assert result > 0.2  # Moved toward neutral

    def test_floor_application(self):
        """At floor, score should be heavily weighted toward neutral."""
        now = time.time()
        decay = StalenessDecay(last_activity_at=now - STALENESS_MAX_DECAY, now=now)

        # Score 0.9 at floor 0.3 → 0.9 * 0.3 + 0.5 * 0.7 = 0.27 + 0.35 = 0.62
        result = decay.apply(0.9)
        expected = 0.9 * STALENESS_FLOOR + STALENESS_NEUTRAL_TARGET * (1 - STALENESS_FLOOR)
        assert abs(result - expected) < 0.01


# ── Scoring Integration Tests ──


class TestScoringIntegration:
    """Test staleness in scoring breakdown."""

    def test_compute_breakdown_includes_staleness(self):
        """Scoring breakdown should include staleness info."""
        from app.intelligence.suitability import suitability_scorer

        breakdown = suitability_scorer.compute_breakdown(
            "test-model", "test-provider", "api", "generate",
        )

        assert "staleness_decay" in vars(breakdown)
        assert breakdown.staleness_decay >= STALENESS_FLOOR
        assert breakdown.staleness_decay <= 1.0
        assert breakdown.staleness_label in ("fresh", "aging", "stale", "very_stale")

    def test_compute_breakdown_to_dict_has_staleness(self):
        """to_dict should include staleness section."""
        from app.intelligence.suitability import suitability_scorer

        breakdown = suitability_scorer.compute_breakdown(
            "test-model", "test-provider", "api", "generate",
        )
        d = breakdown.to_dict()

        assert "staleness" in d
        assert "decay_factor" in d["staleness"]
        assert "staleness_label" in d["staleness"]
        assert "effective_confidence" in d["staleness"]
        assert "last_activity_seconds_ago" in d["staleness"]

    def test_effective_confidence_reduced_by_staleness(self):
        """Effective confidence should be <= data confidence for stale models."""
        from app.intelligence.suitability import suitability_scorer

        breakdown = suitability_scorer.compute_breakdown(
            "test-model", "test-provider", "api", "generate",
        )

        # For a model with no activity data, staleness should reduce effective confidence
        if breakdown.staleness_decay < 1.0:
            assert breakdown.effective_confidence <= breakdown.data_confidence


# ── Convenience Function Tests ──


class TestConvenienceFunctions:
    def test_compute_decay_function(self):
        """Convenience function should work."""
        now = time.time()
        decay = compute_decay(now - 100, now)
        assert decay.decay_factor() == 1.0

    def test_compute_decay_stale(self):
        """Convenience function for stale model."""
        now = time.time()
        staleness = STALENESS_GRACE_SECONDS + STALENESS_HALF_LIFE
        decay = compute_decay(now - staleness, now)
        assert 0.45 <= decay.decay_factor() <= 0.55
