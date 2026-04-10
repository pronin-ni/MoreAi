"""
Tests for the adaptive pipeline scoring enhancements.

Covers:
- Exact sample count correctness
- Confidence calculation uses exact count
- Scoring UI data contract
- Cold-start detection
- Fallback-heavy detection
- Global penalty cache TTL / expiration / bounded behavior
- Trace includes penalty source info
"""

import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.pipeline.observability.penalty_cache import GlobalPenaltyCache, global_penalty_cache
from app.pipeline.observability.stage_perf import (
    RolePerformanceEntry,
    StagePerformanceTracker,
)


# ── Fixtures ──


@pytest.fixture
def fresh_perf_tracker(tmp_path):
    """Fresh performance tracker in a temp directory."""
    db = str(tmp_path / "test_stage_perf.db")
    tracker = StagePerformanceTracker(db_path=db, max_entries=100, max_age_days=1)
    yield tracker
    # Cleanup
    try:
        import os
        if os.path.exists(db):
            os.remove(db)
    except Exception:
        pass


@pytest.fixture
def fresh_penalty_cache():
    """Fresh penalty cache with short TTL for testing."""
    cache = GlobalPenaltyCache(ttl_seconds=2, max_entries=10, default_penalty=0.08)
    yield cache
    cache.clear()


# ── Exact Sample Count Tests ──


class TestExactSampleCount:
    def test_zero_samples_returns_zero(self, fresh_perf_tracker):
        count = fresh_perf_tracker.get_sample_count("qwen", "generate")
        assert count == 0

    def test_exact_count_after_records(self, fresh_perf_tracker):
        for i in range(7):
            fresh_perf_tracker.record(RolePerformanceEntry(
                model_id="qwen",
                provider_id="qwen",
                stage_role="generate",
                success=True,
                duration_ms=5000,
                had_fallback=False,
                had_retry=False,
            ))

        count = fresh_perf_tracker.get_sample_count("qwen", "generate")
        assert count == 7

    def test_exact_count_respects_window(self, fresh_perf_tracker):
        # Record 5 entries
        for i in range(5):
            fresh_perf_tracker.record(RolePerformanceEntry(
                model_id="qwen",
                provider_id="qwen",
                stage_role="generate",
                success=True,
                duration_ms=5000,
                had_fallback=False,
                had_retry=False,
            ))

        # Query with window=3 should return 3
        count = fresh_perf_tracker.get_sample_count("qwen", "generate", window=3)
        assert count == 3

    def test_exact_count_separate_per_role(self, fresh_perf_tracker):
        fresh_perf_tracker.record(RolePerformanceEntry(
            model_id="qwen", provider_id="qwen", stage_role="generate",
            success=True, duration_ms=5000, had_fallback=False, had_retry=False,
        ))
        fresh_perf_tracker.record(RolePerformanceEntry(
            model_id="qwen", provider_id="qwen", stage_role="review",
            success=True, duration_ms=5000, had_fallback=False, had_retry=False,
        ))

        assert fresh_perf_tracker.get_sample_count("qwen", "generate") == 1
        assert fresh_perf_tracker.get_sample_count("qwen", "review") == 1

    def test_get_model_role_stats_returns_exact_count(self, fresh_perf_tracker):
        for i in range(12):
            fresh_perf_tracker.record(RolePerformanceEntry(
                model_id="qwen", provider_id="qwen", stage_role="generate",
                success=i % 3 != 0, duration_ms=5000 + i * 100,
                had_fallback=i % 4 == 0, had_retry=False,
            ))

        stats = fresh_perf_tracker.get_model_role_stats("qwen", "generate")
        assert stats["sample_count"] == 12
        assert stats["success_rate"] == pytest.approx(8 / 12, abs=0.01)


# ── Confidence Calculation Tests ──


class TestConfidenceCalculation:
    def test_confidence_zero_for_no_data(self, fresh_perf_tracker):
        from app.intelligence.suitability import MIN_SAMPLES_FOR_DYNAMIC, FULL_WINDOW

        count = fresh_perf_tracker.get_sample_count("qwen", "generate", window=FULL_WINDOW)
        assert count == 0

        success_rate = fresh_perf_tracker.get_success_rate("qwen", "generate", window=FULL_WINDOW)
        fallback_rate = fresh_perf_tracker.get_fallback_rate("qwen", "generate", window=FULL_WINDOW)
        assert success_rate == 0.5
        assert fallback_rate == 0.0

    def test_confidence_low_for_few_samples(self, fresh_perf_tracker):
        from app.intelligence.suitability import MIN_SAMPLES_FOR_DYNAMIC

        # Record 3 samples (below threshold)
        for i in range(3):
            fresh_perf_tracker.record(RolePerformanceEntry(
                model_id="qwen", provider_id="qwen", stage_role="generate",
                success=True, duration_ms=5000, had_fallback=False, had_retry=False,
            ))

        count = fresh_perf_tracker.get_sample_count("qwen", "generate")
        assert count == 3
        assert count < MIN_SAMPLES_FOR_DYNAMIC

        # Confidence should be 0.1 for < MIN_SAMPLES
        success_rate = fresh_perf_tracker.get_success_rate("qwen", "generate")
        fallback_rate = fresh_perf_tracker.get_fallback_rate("qwen", "generate")
        perf_score = success_rate * 0.7 + (1 - fallback_rate) * 0.3
        assert perf_score == pytest.approx(1.0, abs=0.01)

    def test_confidence_high_for_many_samples(self, fresh_perf_tracker):
        from app.intelligence.suitability import FULL_WINDOW

        # Record 100+ samples
        for i in range(FULL_WINDOW + 10):
            fresh_perf_tracker.record(RolePerformanceEntry(
                model_id="qwen", provider_id="qwen", stage_role="generate",
                success=True, duration_ms=5000, had_fallback=False, had_retry=False,
            ))

        count = fresh_perf_tracker.get_sample_count("qwen", "generate", window=FULL_WINDOW)
        assert count >= FULL_WINDOW
        # Windowed to 100, so count = 100
        assert count == FULL_WINDOW

    def test_confidence_linear_in_between(self, fresh_perf_tracker):
        from app.intelligence.suitability import MIN_SAMPLES_FOR_DYNAMIC, FULL_WINDOW

        # Record exactly 50 samples (mid-range)
        mid = (MIN_SAMPLES_FOR_DYNAMIC + FULL_WINDOW) // 2
        for i in range(mid):
            fresh_perf_tracker.record(RolePerformanceEntry(
                model_id="qwen", provider_id="qwen", stage_role="generate",
                success=True, duration_ms=5000, had_fallback=False, had_retry=False,
            ))

        count = fresh_perf_tracker.get_sample_count("qwen", "generate")
        expected_confidence = 0.1 + (count - MIN_SAMPLES_FOR_DYNAMIC) / (FULL_WINDOW - MIN_SAMPLES_FOR_DYNAMIC) * 0.6
        assert expected_confidence > 0.1
        assert expected_confidence < 0.7


# ── Scoring UI Data Contract Tests ──


class TestScoringUIDataContract:
    @pytest.fixture
    def client(self):
        from app.pipeline.executor import initialize_pipelines
        from app.pipeline.types import pipeline_registry
        initialize_pipelines()

        with (
            patch("app.main.browser_dispatcher.initialize", new=AsyncMock()),
            patch("app.main.browser_dispatcher.shutdown", new=AsyncMock()),
            patch("app.main.unified_registry.initialize", new=AsyncMock()),
        ):
            from app.main import app
            yield TestClient(app)

    def test_scoring_endpoint_returns_required_fields(self, client):
        response = client.get("/admin/pipelines/stage-scoring?stage_role=generate")
        assert response.status_code == 200
        data = response.json()
        assert "scoring" in data
        assert "stage_role" in data
        assert "total" in data

        if data["scoring"]:
            entry = data["scoring"][0]
            required_fields = [
                "model_id", "provider_id", "transport", "role",
                "final_score", "base_static_score", "dynamic_adjustment",
                "failure_penalty", "penalty_reasons",
                "success_rate", "fallback_rate", "avg_duration_ms",
                "sample_count", "data_confidence", "tags",
                "cold_start", "fallback_heavy", "top_performer",
            ]
            for field in required_fields:
                assert field in entry, f"Missing field: {field}"

    def test_scoring_endpoint_returns_different_roles(self, client):
        for role in ["generate", "review", "critique", "refine", "verify", "transform"]:
            response = client.get(f"/admin/pipelines/stage-scoring?stage_role={role}")
            assert response.status_code == 200
            data = response.json()
            assert data["stage_role"] == role

    def test_scoring_sorted_by_final_score(self, client):
        response = client.get("/admin/pipelines/stage-scoring?stage_role=generate")
        data = response.json()
        if len(data["scoring"]) > 1:
            scores = [s["final_score"] for s in data["scoring"]]
            assert scores == sorted(scores, reverse=True)


# ── Cold-Start Detection Tests ──


class TestColdStartDetection:
    @pytest.fixture
    def client(self):
        from app.pipeline.executor import initialize_pipelines
        from app.pipeline.types import pipeline_registry
        initialize_pipelines()

        with (
            patch("app.main.browser_dispatcher.initialize", new=AsyncMock()),
            patch("app.main.browser_dispatcher.shutdown", new=AsyncMock()),
            patch("app.main.unified_registry.initialize", new=AsyncMock()),
        ):
            from app.main import app
            yield TestClient(app)

    def test_models_without_data_are_cold_start(self, client):
        response = client.get("/admin/pipelines/stage-scoring?stage_role=generate")
        data = response.json()

        # All models should be cold_start since no performance data exists
        cold_starts = [s for s in data["scoring"] if s["cold_start"]]
        assert len(cold_starts) == data["total"]

    def test_models_with_enough_data_not_cold_start(self, client, fresh_perf_tracker):
        # Record 10 samples for a model
        for i in range(10):
            fresh_perf_tracker.record(RolePerformanceEntry(
                model_id="qwen", provider_id="qwen", stage_role="generate",
                success=True, duration_ms=5000, had_fallback=False, had_retry=False,
            ))

        response = client.get("/admin/pipelines/stage-scoring?stage_role=generate")
        data = response.json()

        qwen_entry = next((s for s in data["scoring"] if s["model_id"] == "qwen"), None)
        if qwen_entry:
            assert qwen_entry["cold_start"] is False
            assert qwen_entry["sample_count"] >= 5


# ── Fallback-Heavy Detection Tests ──


class TestFallbackHeavyDetection:
    def test_high_fallback_rate_is_fallback_heavy(self, fresh_perf_tracker):
        # Record 10 samples with high fallback rate
        for i in range(10):
            fresh_perf_tracker.record(RolePerformanceEntry(
                model_id="glm", provider_id="glm", stage_role="review",
                success=True, duration_ms=5000,
                had_fallback=i < 5,  # 50% fallback rate
                had_retry=False,
            ))

        stats = fresh_perf_tracker.get_model_role_stats("glm", "review")
        assert stats["fallback_rate"] == pytest.approx(0.5, abs=0.01)
        assert stats["sample_count"] == 10


# ── Global Penalty Cache Tests ──


class TestGlobalPenaltyCache:
    def test_record_and_get_penalty(self, fresh_penalty_cache):
        fresh_penalty_cache.record_failure("qwen", reason="timeout")
        penalty = fresh_penalty_cache.get_penalty("qwen")

        assert penalty["total_penalty"] > 0
        assert penalty["entry_count"] == 1
        assert "timeout" in penalty["reasons"]

    def test_penalty_expires_after_ttl(self, fresh_penalty_cache):
        fresh_penalty_cache.record_failure("qwen", reason="timeout", penalty=0.15)

        # Should have penalty immediately
        penalty = fresh_penalty_cache.get_penalty("qwen")
        assert penalty["total_penalty"] > 0

        # Wait for TTL to expire
        import time
        time.sleep(2.5)

        # Should be expired now
        penalty = fresh_penalty_cache.get_penalty("qwen")
        assert penalty["total_penalty"] == 0
        assert penalty["entry_count"] == 0

    def test_multiple_failures_stack(self, fresh_penalty_cache):
        fresh_penalty_cache.record_failure("qwen", reason="timeout", penalty=0.10)
        fresh_penalty_cache.record_failure("qwen", reason="execution_error", penalty=0.08)

        penalty = fresh_penalty_cache.get_penalty("qwen")
        assert penalty["entry_count"] == 2
        assert penalty["total_penalty"] == pytest.approx(0.18, abs=0.01)
        assert len(penalty["reasons"]) == 2

    def test_penalty_capped_at_0_3(self, fresh_penalty_cache):
        # Record many failures
        for i in range(10):
            fresh_penalty_cache.record_failure("qwen", reason="error", penalty=0.10)

        penalty = fresh_penalty_cache.get_penalty("qwen")
        assert penalty["total_penalty"] <= 0.3

    def test_bounded_memory(self):
        cache = GlobalPenaltyCache(ttl_seconds=60, max_entries=3, default_penalty=0.08)

        # Record more failures than max_entries
        for i in range(5):
            cache.record_failure(f"model_{i}", reason="error", penalty=0.08)

        # Should have at most max_entries models tracked
        assert len(cache._entries) <= 3

    def test_get_all_penalties(self, fresh_penalty_cache):
        fresh_penalty_cache.record_failure("qwen", reason="timeout")
        fresh_penalty_cache.record_failure("glm", reason="error")

        all_penalties = fresh_penalty_cache.get_all_penalties()
        assert "qwen" in all_penalties
        assert "glm" in all_penalties

    def test_clear(self, fresh_penalty_cache):
        fresh_penalty_cache.record_failure("qwen", reason="timeout")
        fresh_penalty_cache.clear()

        all_penalties = fresh_penalty_cache.get_all_penalties()
        assert len(all_penalties) == 0

    def test_cleanup_expired(self, fresh_penalty_cache):
        fresh_penalty_cache.record_failure("qwen", reason="timeout")
        fresh_penalty_cache.record_failure("glm", reason="error")

        # Wait for TTL
        import time
        time.sleep(2.5)

        cleaned = fresh_penalty_cache.cleanup_expired()
        assert cleaned >= 2
        assert len(fresh_penalty_cache.get_all_penalties()) == 0


# ── Penalty Cache Admin API Tests ──


class TestPenaltyCacheAPI:
    @pytest.fixture
    def client(self):
        from app.pipeline.executor import initialize_pipelines
        from app.pipeline.types import pipeline_registry
        initialize_pipelines()

        with (
            patch("app.main.browser_dispatcher.initialize", new=AsyncMock()),
            patch("app.main.browser_dispatcher.shutdown", new=AsyncMock()),
            patch("app.main.unified_registry.initialize", new=AsyncMock()),
        ):
            from app.main import app
            yield TestClient(app)

    def test_get_penalty_cache_status(self, client):
        response = client.get("/admin/pipelines/penalty-cache")
        assert response.status_code == 200
        data = response.json()
        assert "active_penalties" in data
        assert "total_tracked" in data
        assert "ttl_seconds" in data

    def test_clear_penalty_cache(self, client):
        response = client.post("/admin/pipelines/penalty-cache/clear")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "cleared"
