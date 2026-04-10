"""
Tests for quality-aware scoring.

Covers:
- Quality signal extraction (review, refine, generate)
- Quality score computation and explainability
- Quality metrics store (persistence, retention, rolling windows)
- Integration into suitability scoring (quality_adjustment)
- Cold-start behavior
- Admin API responses
"""

import contextlib
import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.pipeline.observability.quality_scoring import (
    QualityExtractor,
    QualityMetricsStore,
    QualitySignals,
)

# ── Fixtures ──


@pytest.fixture
def extractor():
    return QualityExtractor()


@pytest.fixture
def temp_quality_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = QualityMetricsStore(db_path=path, max_entries=100, max_age_days=30)
    yield store
    store.cleanup()
    with contextlib.suppress(OSError):
        os.unlink(path)


@pytest.fixture
def client():
    """Test client with mocked startup."""
    with (
        patch("app.main.browser_dispatcher.initialize", new=AsyncMock()),
        patch("app.main.browser_dispatcher.shutdown", new=AsyncMock()),
        patch("app.main.unified_registry.initialize", new=AsyncMock()),
    ):
        from app.main import app
        yield TestClient(app)


# ── Quality Signal Extraction Tests ──


class TestQualityExtraction:
    def test_extract_review_with_issues(self, extractor):
        text = (
            "The draft contains several **incorrect** assertions.\n"
            "1. The first claim is misleading.\n"
            "2. There is a critical error in the methodology.\n"
            "3. A major flaw exists in the conclusion.\n"
            "4. Minor wording could be improved in section 2.\n"
        )
        signals = extractor.extract(text, "review")

        assert signals.critical_count >= 1
        assert signals.major_count >= 1
        assert signals.minor_count >= 1
        assert signals.issue_count > 0
        assert signals.has_structure is True

    def test_extract_review_no_issues(self, extractor):
        text = "The draft looks solid. Well-written and clear."
        signals = extractor.extract(text, "review")

        assert signals.critical_count == 0
        assert signals.has_structure is False

    def test_extract_refine_with_changes(self, extractor):
        input_text = "Short draft with basic points."
        output_text = (
            "**Revised and Improved Version**\n\n"
            "This document has been restructured and corrected.\n"
            "The following changes were made:\n"
            "1. Added detail.\n"
            "2. Fixed errors.\n"
        )
        signals = extractor.extract(output_text, "refine", input_text)

        assert signals.rewrite_ratio > 0.0
        assert signals.has_structure is True

    def test_extract_generate_basic(self, extractor):
        text = "Here is a detailed explanation of the topic.\n\n" * 20
        signals = extractor.extract(text, "generate")

        assert signals.output_length > 500
        assert signals.confidence >= 0.7

    def test_extract_empty_output(self, extractor):
        signals = extractor.extract("", "generate")
        assert signals.output_length == 0
        assert signals.issue_count == 0

    def test_detect_structure_markdown(self, extractor):
        text = "# Heading\n\nSome content.\n\n## Subheading\n\n- List item\n- Another"
        assert extractor._detect_structure(text) is True

    def test_detect_structure_no_structure(self, extractor):
        text = "This is just a plain paragraph with no formatting or structure."
        assert extractor._detect_structure(text) is False

    def test_compute_rewrite_ratio_identical(self, extractor):
        text = "Same text.\nSame lines."
        ratio = extractor._compute_rewrite_ratio(text, text)
        assert ratio == 0.0

    def test_compute_rewrite_ratio_completely_different(self, extractor):
        ratio = extractor._compute_rewrite_ratio("Line A\nLine B", "Line X\nLine Y")
        assert ratio > 0.5

    def test_compute_rewrite_ratio_empty_input(self, extractor):
        ratio = extractor._compute_rewrite_ratio("", "New content")
        assert ratio == 1.0


# ── Quality Score Computation Tests ──


class TestQualityScoring:
    def test_score_review_with_issues(self, extractor):
        signals = QualitySignals(
            issue_count=5, critical_count=1, major_count=2, minor_count=1,
            output_length=500, has_structure=True,
        )
        score = extractor.compute_quality_score(signals, "review")
        assert 0.0 < score <= 1.0
        # Issues found + structure = decent score
        assert score >= 0.5

    def test_score_review_no_issues(self, extractor):
        signals = QualitySignals(
            issue_count=0, output_length=200, has_structure=False,
        )
        score = extractor.compute_quality_score(signals, "review")
        # No issues found = lower score (may have missed things)
        assert score < 0.6

    def test_score_refine_moderate(self, extractor):
        signals = QualitySignals(
            rewrite_ratio=0.3, output_length=800, has_structure=True,
        )
        score = extractor.compute_quality_score(signals, "refine")
        # Moderate rewrite + structure = good
        assert score >= 0.5

    def test_score_refine_minimal_change(self, extractor):
        signals = QualitySignals(
            rewrite_ratio=0.02, output_length=500, has_structure=False,
        )
        score = extractor.compute_quality_score(signals, "refine")
        # Almost no change = low score
        assert score < 0.4

    def test_score_refine_heavy_rewrite(self, extractor):
        signals = QualitySignals(
            rewrite_ratio=0.95, output_length=600, has_structure=False,
        )
        score = extractor.compute_quality_score(signals, "refine")
        # Near-complete rewrite = low score (may have lost intent)
        assert score < 0.4

    def test_score_generate_good_output(self, extractor):
        signals = QualitySignals(
            output_length=1500, has_structure=True, confidence=0.8,
        )
        score = extractor.compute_quality_score(signals, "generate")
        assert score >= 0.5

    def test_score_generate_short_output(self, extractor):
        signals = QualitySignals(
            output_length=30, has_structure=False, confidence=0.3,
        )
        score = extractor.compute_quality_score(signals, "generate")
        assert score < 0.5

    def test_score_empty_output(self, extractor):
        signals = QualitySignals(output_length=0)
        score = extractor.compute_quality_score(signals, "generate")
        assert score == 0.0

    def test_score_bounded_0_to_1(self, extractor):
        # Test extremes
        for role in ["generate", "review", "refine", "critique", "verify"]:
            signals = QualitySignals(
                issue_count=100, rewrite_ratio=1.0, output_length=10000,
            )
            score = extractor.compute_quality_score(signals, role)
            assert 0.0 <= score <= 1.0, f"Score {score} out of bounds for {role}"

    def test_explain_score_review(self, extractor):
        signals = QualitySignals(
            issue_count=8, critical_count=2, major_count=3,
            output_length=600, has_structure=True,
        )
        explanation = extractor.explain_score(signals, 0.7, "review")
        assert "quality=0.70" in explanation
        assert "critical_issues" in explanation

    def test_explain_score_refine(self, extractor):
        signals = QualitySignals(rewrite_ratio=0.02, output_length=400)
        explanation = extractor.explain_score(signals, 0.15, "refine")
        assert "minimal_changes" in explanation


# ── Quality Metrics Store Tests ──


class TestQualityMetricsStore:
    def test_record_and_query(self, temp_quality_db):
        signals = QualitySignals(issue_count=3, output_length=500)
        temp_quality_db.record(
            model_id="m1", provider_id="p1", transport="api",
            role="review", quality_score=0.7, signals=signals,
        )

        metrics = temp_quality_db.get_quality_metrics("m1", "review")
        assert metrics.sample_count == 1
        assert metrics.avg_quality_score == 0.7

    def test_rolling_average(self, temp_quality_db):
        signals = QualitySignals(output_length=500)
        for score in [0.3, 0.5, 0.7, 0.9]:
            temp_quality_db.record(
                model_id="m1", provider_id="p1", transport="api",
                role="generate", quality_score=score, signals=signals,
            )

        metrics = temp_quality_db.get_quality_metrics("m1", "generate")
        assert metrics.sample_count == 4
        assert abs(metrics.avg_quality_score - 0.6) < 0.01

    def test_sample_count(self, temp_quality_db):
        signals = QualitySignals(output_length=500)
        for i in range(10):
            temp_quality_db.record(
                model_id="m1", provider_id="p1", transport="api",
                role="review", quality_score=0.5 + i * 0.05, signals=signals,
            )

        count = temp_quality_db.get_sample_count("m1", "review", window=100)
        assert count == 10

        count5 = temp_quality_db.get_sample_count("m1", "review", window=5)
        assert count5 == 5

    def test_retention_max_entries(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        store = None
        try:
            store = QualityMetricsStore(db_path=path, max_entries=5, max_age_days=365)
            signals = QualitySignals(output_length=500)
            for i in range(10):
                store.record(
                    model_id="m1", provider_id="p1", transport="api",
                    role="generate", quality_score=0.1 * i, signals=signals,
                )

            stats_row = store.get_quality_metrics("m1", "generate")
            assert stats_row.sample_count <= 5
        finally:
            if store:
                with contextlib.suppress(OSError):
                    os.unlink(path)

    def test_get_all_quality_summary(self, temp_quality_db):
        signals = QualitySignals(output_length=500)
        temp_quality_db.record(
            model_id="m1", provider_id="p1", transport="api",
            role="generate", quality_score=0.8, signals=signals,
        )
        temp_quality_db.record(
            model_id="m2", provider_id="p2", transport="browser",
            role="generate", quality_score=0.4, signals=signals,
        )

        summaries = temp_quality_db.get_all_quality_summary(role="generate")
        assert len(summaries) == 2
        # Sorted by avg_quality DESC
        assert summaries[0]["model_id"] == "m1"
        assert summaries[0]["avg_quality"] == 0.8

    def test_filter_by_role(self, temp_quality_db):
        signals = QualitySignals(output_length=500)
        temp_quality_db.record(
            model_id="m1", provider_id="p1", transport="api",
            role="generate", quality_score=0.7, signals=signals,
        )
        temp_quality_db.record(
            model_id="m1", provider_id="p1", transport="api",
            role="review", quality_score=0.5, signals=signals,
        )

        gen = temp_quality_db.get_all_quality_summary(role="generate")
        assert len(gen) == 1
        assert gen[0]["role"] == "generate"

    def test_stddev_computation(self, temp_quality_db):
        signals = QualitySignals(output_length=500)
        scores = [0.2, 0.5, 0.8]
        for s in scores:
            temp_quality_db.record(
                model_id="m1", provider_id="p1", transport="api",
                role="generate", quality_score=s, signals=signals,
            )

        metrics = temp_quality_db.get_quality_metrics("m1", "generate")
        assert metrics.quality_stddev > 0.0

    def test_empty_query_returns_defaults(self, temp_quality_db):
        metrics = temp_quality_db.get_quality_metrics("nonexistent", "generate")
        assert metrics.sample_count == 0
        assert metrics.avg_quality_score == 0.5


# ── Integration into Suitability Scoring ──


class TestQualityIntegration:
    def _populate_quality(self, store, model_id, role, score, count=5):
        """Helper to populate quality store with test data."""
        signals = QualitySignals(output_length=1000, has_structure=True)
        for _ in range(count):
            store.record(
                model_id=model_id, provider_id="p1", transport="api",
                role=role, quality_score=score, signals=signals,
            )

    def test_quality_adjustment_positive(self, temp_quality_db):
        """High quality score → positive adjustment."""
        self._populate_quality(temp_quality_db, "m1", "generate", 0.85, count=5)

        from app.intelligence.suitability import ScoringBreakdown

        with patch(
            "app.pipeline.observability.quality_scoring.quality_metrics_store",
            temp_quality_db,
        ):
            breakdown = ScoringBreakdown("m1", "p1", "generate")
            breakdown.availability_score = 0.7
            breakdown.latency_score = 0.6
            breakdown.stability_score = 0.5
            breakdown.tag_bonus_score = 0.5

            from app.intelligence.suitability import suitability_scorer
            suitability_scorer._compute_quality_adjustment(breakdown)

            assert breakdown.quality_sample_count == 5
            assert breakdown.quality_score == pytest.approx(0.85, abs=0.01)
            assert breakdown.quality_adjustment > 0  # Positive adjustment

    def test_quality_adjustment_negative(self, temp_quality_db):
        """Low quality score → negative adjustment."""
        self._populate_quality(temp_quality_db, "m1", "generate", 0.2, count=5)

        from app.intelligence.suitability import ScoringBreakdown

        with patch(
            "app.pipeline.observability.quality_scoring.quality_metrics_store",
            temp_quality_db,
        ):
            breakdown = ScoringBreakdown("m1", "p1", "generate")
            breakdown.availability_score = 0.7
            breakdown.latency_score = 0.6
            breakdown.stability_score = 0.5
            breakdown.tag_bonus_score = 0.5

            from app.intelligence.suitability import suitability_scorer
            suitability_scorer._compute_quality_adjustment(breakdown)

            assert breakdown.quality_adjustment < 0  # Negative adjustment

    def test_quality_cold_start_no_adjustment(self, temp_quality_db):
        """Not enough quality samples → no adjustment."""
        self._populate_quality(temp_quality_db, "m1", "generate", 0.9, count=1)

        from app.intelligence.suitability import ScoringBreakdown

        with patch(
            "app.pipeline.observability.quality_scoring.quality_metrics_store",
            temp_quality_db,
        ):
            breakdown = ScoringBreakdown("m1", "p1", "generate")
            breakdown.availability_score = 0.7
            breakdown.latency_score = 0.6
            breakdown.stability_score = 0.5
            breakdown.tag_bonus_score = 0.5

            from app.intelligence.suitability import suitability_scorer
            suitability_scorer._compute_quality_adjustment(breakdown)

            assert breakdown.quality_sample_count == 1
            assert breakdown.quality_adjustment == 0.0  # Cold start
            assert breakdown.quality_confidence == 0.0

    def test_quality_bounded_influence(self, temp_quality_db):
        """Quality adjustment is bounded by MAX_QUALITY_ADJUSTMENT."""
        from app.intelligence.suitability import ScoringBreakdown

        self._populate_quality(temp_quality_db, "m1", "generate", 1.0, count=20)

        with patch(
            "app.pipeline.observability.quality_scoring.quality_metrics_store",
            temp_quality_db,
        ):
            breakdown = ScoringBreakdown("m1", "p1", "generate")
            breakdown.availability_score = 0.5
            breakdown.latency_score = 0.5
            breakdown.stability_score = 0.5
            breakdown.tag_bonus_score = 0.5

            from app.intelligence.suitability import suitability_scorer
            suitability_scorer._compute_quality_adjustment(breakdown)

            assert breakdown.quality_adjustment <= ScoringBreakdown.MAX_QUALITY_ADJUSTMENT

    def test_quality_affects_final_score(self, temp_quality_db):
        """Quality adjustment should affect final_score."""
        from app.intelligence.suitability import ScoringBreakdown

        self._populate_quality(temp_quality_db, "m1", "generate", 0.9, count=10)

        with patch(
            "app.pipeline.observability.quality_scoring.quality_metrics_store",
            temp_quality_db,
        ):
            breakdown_high = ScoringBreakdown("m1", "p1", "generate")
            breakdown_high.availability_score = 0.6
            breakdown_high.latency_score = 0.6
            breakdown_high.stability_score = 0.6
            breakdown_high.tag_bonus_score = 0.5
            from app.intelligence.suitability import suitability_scorer
            suitability_scorer._compute_quality_adjustment(breakdown_high)
            breakdown_high.compute()

            # Low quality model
            self._populate_quality(
                temp_quality_db, "m2", "generate", 0.15, count=10,
            )

            breakdown_low = ScoringBreakdown("m2", "p1", "generate")
            breakdown_low.availability_score = 0.6
            breakdown_low.latency_score = 0.6
            breakdown_low.stability_score = 0.6
            breakdown_low.tag_bonus_score = 0.5
            suitability_scorer._compute_quality_adjustment(breakdown_low)
            breakdown_low.compute()

            # High quality should have higher final score (all else equal)
            assert breakdown_high.final_score > breakdown_low.final_score

    def test_scoring_breakdown_to_dict_includes_quality(self, temp_quality_db):
        """to_dict() should include quality section."""
        self._populate_quality(temp_quality_db, "m1", "generate", 0.7, count=5)

        from app.intelligence.suitability import ScoringBreakdown

        with patch(
            "app.pipeline.observability.quality_scoring.quality_metrics_store",
            temp_quality_db,
        ):
            breakdown = ScoringBreakdown("m1", "p1", "generate")
            breakdown.availability_score = 0.6
            breakdown.latency_score = 0.5
            breakdown.stability_score = 0.5
            breakdown.tag_bonus_score = 0.5
            from app.intelligence.suitability import suitability_scorer
            suitability_scorer._compute_quality_adjustment(breakdown)
            breakdown.compute()

            result = breakdown.to_dict()
            assert "quality" in result
            assert "score" in result["quality"]
            assert "adjustment" in result["quality"]
            assert "sample_count" in result["quality"]
            assert "confidence" in result["quality"]


# ── Admin API Tests ──


class TestQualityAPI:
    def test_get_stage_quality_empty(self, client):
        resp = client.get("/admin/pipelines/stage-quality")
        assert resp.status_code == 200
        data = resp.json()
        assert "quality" in data
        assert "top_quality" in data
        assert "low_quality" in data
        assert data["total"] == 0

    def test_get_stage_quality_with_role(self, client):
        resp = client.get("/admin/pipelines/stage-quality?role=review")
        assert resp.status_code == 200
        data = resp.json()
        assert "quality" in data
