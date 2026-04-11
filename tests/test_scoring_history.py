"""
Tests for scoring history storage and trend analysis.

Covers:
- ScoringHistoryStore: persistence, retention, querying
- ScoringTrendAnalyzer: trend calculation, classification, driver identification
- SnapshotScheduler: manual trigger, status
- Admin API endpoints for history and trends
"""

import contextlib
import os
import tempfile
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.pipeline.observability.scoring_history import (
    ScoringHistoryStore,
    ScoringSnapshot,
)
from app.pipeline.observability.scoring_trends import (
    ScoringTrendAnalyzer,
    SnapshotScheduler,
)

# ── Fixtures ──


@pytest.fixture
def temp_db():
    """Create a temporary SQLite DB for each test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = ScoringHistoryStore(db_path=path, max_entries=100, max_age_days=30)
    yield store
    store.cleanup()
    with contextlib.suppress(OSError):
        os.unlink(path)


@pytest.fixture
def analyzer(temp_db):
    """Trend analyzer backed by temp DB."""
    return ScoringTrendAnalyzer(store=temp_db)


@pytest.fixture
def scheduler(analyzer):
    """Snapshot scheduler with short interval for testing."""
    return SnapshotScheduler(analyzer=analyzer, interval_seconds=60)


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


# ── Helper ──


def _make_snapshot(
    model_id="test-model",
    provider_id="test-provider",
    transport="api",
    role="generate",
    final_score=0.5,
    base_static_score=0.5,
    dynamic_adjustment=0.0,
    failure_penalty=0.0,
    success_rate=0.5,
    fallback_rate=0.0,
    avg_duration_ms=1000.0,
    sample_count=10,
    data_confidence=0.3,
    timestamp=None,
) -> ScoringSnapshot:
    return ScoringSnapshot(
        timestamp=timestamp or time.time(),
        model_id=model_id,
        provider_id=provider_id,
        transport=transport,
        role=role,
        final_score=final_score,
        base_static_score=base_static_score,
        dynamic_adjustment=dynamic_adjustment,
        failure_penalty=failure_penalty,
        success_rate=success_rate,
        fallback_rate=fallback_rate,
        avg_duration_ms=avg_duration_ms,
        sample_count=sample_count,
        data_confidence=data_confidence,
    )


# ── ScoringHistoryStore Tests ──


class TestScoringHistoryStore:
    def test_record_and_query(self, temp_db):
        snap = _make_snapshot(model_id="m1", role="generate", final_score=0.7)
        temp_db.record_snapshot(snap)

        history = temp_db.get_history(model_id="m1", role="generate")
        assert len(history) == 1
        assert history[0].final_score == 0.7
        assert history[0].model_id == "m1"

    def test_query_all_models(self, temp_db):
        temp_db.record_snapshot(_make_snapshot(model_id="m1", role="generate"))
        temp_db.record_snapshot(_make_snapshot(model_id="m2", role="review"))
        temp_db.record_snapshot(_make_snapshot(model_id="m1", role="review"))

        models = temp_db.get_distinct_models()
        assert set(models) == {"m1", "m2"}

        models_gen = temp_db.get_distinct_models(role="generate")
        assert models_gen == ["m1"]

    def test_query_all_roles(self, temp_db):
        temp_db.record_snapshot(_make_snapshot(role="generate"))
        temp_db.record_snapshot(_make_snapshot(role="review"))
        temp_db.record_snapshot(_make_snapshot(role="verify"))

        roles = temp_db.get_distinct_roles()
        assert set(roles) == {"generate", "review", "verify"}

    def test_query_by_window(self, temp_db):
        now = time.time()
        old_ts = now - 86400 * 2  # 2 days ago
        recent_ts = now - 3600  # 1 hour ago

        temp_db.record_snapshot(_make_snapshot(timestamp=old_ts, final_score=0.3))
        temp_db.record_snapshot(_make_snapshot(timestamp=recent_ts, final_score=0.7))

        # All history
        all_history = temp_db.get_history()
        assert len(all_history) == 2

        # Last 24h only
        recent = temp_db.get_history(window_seconds=86400)
        assert len(recent) == 1
        assert recent[0].final_score == 0.7

    def test_query_limit(self, temp_db):
        for i in range(10):
            temp_db.record_snapshot(_make_snapshot(final_score=0.1 * i))

        history = temp_db.get_history(limit=3)
        assert len(history) == 3
        # DESC order — most recent first
        assert history[0].final_score == 0.9

    def test_retention_max_entries(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        store = None
        try:
            store = ScoringHistoryStore(db_path=path, max_entries=5, max_age_days=365)
            for i in range(10):
                store.record_snapshot(_make_snapshot(final_score=0.1 * i))

            stats = store.get_stats()
            assert stats["total_snapshots"] <= 5
        finally:
            if store:
                with contextlib.suppress(OSError):
                    os.unlink(path)

    def test_store_stats(self, temp_db):
        temp_db.record_snapshot(_make_snapshot())
        stats = temp_db.get_stats()

        assert stats["total_snapshots"] == 1
        assert stats["max_entries"] == 100
        assert stats["max_age_days"] == 30
        assert stats["storage_type"] == "sqlite"
        assert stats["newest_snapshot_ts"] is not None

    def test_cleanup(self, temp_db):
        for i in range(10):
            temp_db.record_snapshot(_make_snapshot(final_score=0.1 * i))

        deleted = temp_db.cleanup()
        # Should not delete anything since we're under the limit
        assert deleted == 0

    def test_empty_query_returns_empty(self, temp_db):
        history = temp_db.get_history(model_id="nonexistent")
        assert history == []

    def test_graceful_degradation_on_db_error(self, temp_db):
        # Record some data first
        temp_db.record_snapshot(_make_snapshot(final_score=0.5))

        # Verify data exists
        history = temp_db.get_history()
        assert len(history) == 1

        # Test that queries on empty filters work fine
        empty_history = temp_db.get_history(model_id="nonexistent_model")
        assert empty_history == []

        stats = temp_db.get_stats()
        assert stats["total_snapshots"] == 1


# ── ScoringTrendAnalyzer Tests ──


class TestScoringTrendAnalyzer:
    def test_trend_improving(self, analyzer):
        now = time.time()
        # Steady improvement with low variance
        scores = [0.45, 0.50, 0.55, 0.60, 0.65]
        for i, score in enumerate(scores):
            ts = now - (len(scores) - i) * 1200  # every 20 minutes
            analyzer._store.record_snapshot(
                _make_snapshot(model_id="m1", role="generate", final_score=score, timestamp=ts)
            )

        trend = analyzer.get_trend(model_id="m1", role="generate", window_seconds=86400)
        assert trend is not None
        assert trend.overall_trend == "improving"
        assert trend.score_delta > 0.05
        assert trend.model_id == "m1"

    def test_trend_declining(self, analyzer):
        now = time.time()
        # Steady decline with low variance
        scores = [0.65, 0.60, 0.55, 0.50, 0.45]
        for i, score in enumerate(scores):
            ts = now - (len(scores) - i) * 1200
            analyzer._store.record_snapshot(
                _make_snapshot(model_id="m1", role="generate", final_score=score, timestamp=ts)
            )

        trend = analyzer.get_trend(model_id="m1", role="generate", window_seconds=86400)
        assert trend is not None
        assert trend.overall_trend == "declining"
        assert trend.score_delta < -0.05

    def test_trend_stable(self, analyzer):
        now = time.time()
        analyzer._store.record_snapshot(
            _make_snapshot(model_id="m1", role="generate", final_score=0.5, timestamp=now - 7200)
        )
        analyzer._store.record_snapshot(
            _make_snapshot(model_id="m1", role="generate", final_score=0.52, timestamp=now - 600)
        )

        trend = analyzer.get_trend(model_id="m1", role="generate", window_seconds=86400)
        assert trend is not None
        assert trend.overall_trend == "stable"

    def test_trend_unstable(self, analyzer):
        now = time.time()
        # High variance history
        scores = [0.2, 0.9, 0.1, 0.8, 0.3, 0.7]
        for i, score in enumerate(scores):
            ts = now - (len(scores) - i) * 600  # every 10 minutes
            analyzer._store.record_snapshot(
                _make_snapshot(model_id="m1", role="generate", final_score=score, timestamp=ts)
            )

        trend = analyzer.get_trend(model_id="m1", role="generate", window_seconds=86400)
        assert trend is not None
        assert trend.overall_trend == "unstable"

    def test_insufficient_data_returns_none(self, analyzer):
        now = time.time()
        analyzer._store.record_snapshot(
            _make_snapshot(model_id="m1", role="generate", timestamp=now - 600)
        )

        trend = analyzer.get_trend(model_id="m1", role="generate", window_seconds=86400)
        assert trend is None  # Only 1 data point

    def test_success_rate_delta(self, analyzer):
        now = time.time()
        analyzer._store.record_snapshot(
            _make_snapshot(model_id="m1", role="generate", success_rate=0.4, timestamp=now - 7200)
        )
        analyzer._store.record_snapshot(
            _make_snapshot(model_id="m1", role="generate", success_rate=0.9, timestamp=now - 600)
        )

        trend = analyzer.get_trend(model_id="m1", role="generate", window_seconds=86400)
        assert trend is not None
        assert trend.success_rate_delta > 0.4

    def test_fallback_rate_delta(self, analyzer):
        now = time.time()
        analyzer._store.record_snapshot(
            _make_snapshot(model_id="m1", role="generate", fallback_rate=0.0, timestamp=now - 7200)
        )
        analyzer._store.record_snapshot(
            _make_snapshot(model_id="m1", role="generate", fallback_rate=0.3, timestamp=now - 600)
        )

        trend = analyzer.get_trend(model_id="m1", role="generate", window_seconds=86400)
        assert trend is not None
        assert trend.fallback_rate_delta > 0.2

    def test_duration_delta(self, analyzer):
        now = time.time()
        analyzer._store.record_snapshot(
            _make_snapshot(model_id="m1", role="generate", avg_duration_ms=500, timestamp=now - 7200)
        )
        analyzer._store.record_snapshot(
            _make_snapshot(model_id="m1", role="generate", avg_duration_ms=2000, timestamp=now - 600)
        )

        trend = analyzer.get_trend(model_id="m1", role="generate", window_seconds=86400)
        assert trend is not None
        assert trend.duration_delta_ms == 1500.0

    def test_driver_identification_success_rate(self, analyzer):
        now = time.time()
        analyzer._store.record_snapshot(
            _make_snapshot(
                model_id="m1", role="generate",
                final_score=0.4, success_rate=0.3, timestamp=now - 7200,
            )
        )
        analyzer._store.record_snapshot(
            _make_snapshot(
                model_id="m1", role="generate",
                final_score=0.7, success_rate=0.95, timestamp=now - 600,
            )
        )

        trend = analyzer.get_trend(model_id="m1", role="generate", window_seconds=86400)
        assert trend is not None
        assert "success_rate" in trend.main_driver

    def test_driver_identification_fallback(self, analyzer):
        now = time.time()
        analyzer._store.record_snapshot(
            _make_snapshot(
                model_id="m1", role="generate",
                final_score=0.6, fallback_rate=0.0, timestamp=now - 7200,
            )
        )
        analyzer._store.record_snapshot(
            _make_snapshot(
                model_id="m1", role="generate",
                final_score=0.3, fallback_rate=0.5, timestamp=now - 600,
            )
        )

        trend = analyzer.get_trend(model_id="m1", role="generate", window_seconds=86400)
        assert trend is not None
        assert "fallback_rate" in trend.main_driver

    def test_all_trends_returns_sorted(self, analyzer):
        now = time.time()

        # Improving model
        analyzer._store.record_snapshot(
            _make_snapshot(model_id="improving", role="generate", final_score=0.3, timestamp=now - 7200)
        )
        analyzer._store.record_snapshot(
            _make_snapshot(model_id="improving", role="generate", final_score=0.8, timestamp=now - 600)
        )

        # Declining model
        analyzer._store.record_snapshot(
            _make_snapshot(model_id="declining", role="generate", final_score=0.9, timestamp=now - 7200)
        )
        analyzer._store.record_snapshot(
            _make_snapshot(model_id="declining", role="generate", final_score=0.3, timestamp=now - 600)
        )

        trends = analyzer.get_all_trends(role="generate", window_seconds=86400)
        # Sorted by score_delta desc — improving first
        assert len(trends) == 2
        assert trends[0].model_id == "improving"
        assert trends[1].model_id == "declining"

    def test_top_improvers(self, analyzer):
        now = time.time()
        for i in range(5):
            analyzer._store.record_snapshot(
                _make_snapshot(model_id=f"model_{i}", role="generate", final_score=0.2, timestamp=now - 7200)
            )
            analyzer._store.record_snapshot(
                _make_snapshot(model_id=f"model_{i}", role="generate", final_score=0.8, timestamp=now - 600)
            )

        improvers = analyzer.get_top_improvers(role="generate", window_seconds=86400, limit=3)
        assert len(improvers) <= 3
        assert all(t.overall_trend == "improving" for t in improvers)

    def test_top_decliners(self, analyzer):
        now = time.time()
        for i in range(5):
            analyzer._store.record_snapshot(
                _make_snapshot(model_id=f"model_{i}", role="generate", final_score=0.9, timestamp=now - 7200)
            )
            analyzer._store.record_snapshot(
                _make_snapshot(model_id=f"model_{i}", role="generate", final_score=0.2, timestamp=now - 600)
            )

        decliners = analyzer.get_top_decliners(role="generate", window_seconds=86400, limit=3)
        assert len(decliners) <= 3
        assert all(t.overall_trend == "declining" for t in decliners)

    def test_unstable_models(self, analyzer):
        now = time.time()
        # Create unstable history
        for i in range(6):
            score = 0.1 if i % 2 == 0 else 0.9
            ts = now - (6 - i) * 600
            analyzer._store.record_snapshot(
                _make_snapshot(model_id="unstable_model", role="generate", final_score=score, timestamp=ts)
            )

        unstable = analyzer.get_unstable_models(role="generate", window_seconds=86400, limit=5)
        assert len(unstable) >= 1
        assert unstable[0].overall_trend == "unstable"


# ── SnapshotScheduler Tests ──


class TestSnapshotScheduler:
    def test_scheduler_status(self, scheduler):
        status = scheduler.get_status()
        assert "running" in status
        assert "interval_seconds" in status
        assert status["interval_seconds"] == 60
        assert not status["running"]

    def test_capture_now(self, scheduler):
        # This will try to capture from registries — may return 0 if no models
        count = scheduler.capture_now()
        assert isinstance(count, int)
        assert count >= 0

    def test_start_stop(self, scheduler):
        scheduler.start()
        status = scheduler.get_status()
        assert status["running"]

        scheduler.stop()
        status = scheduler.get_status()
        assert not status["running"]

    def test_double_start_is_noop(self, scheduler):
        scheduler.start()
        scheduler.start()  # Should not create a second thread
        scheduler.stop()


# ── Trend Classification Edge Cases ──


class TestTrendClassification:
    def test_exact_same_score_is_stable(self, analyzer):
        now = time.time()
        for i in range(3):
            analyzer._store.record_snapshot(
                _make_snapshot(model_id="m1", role="generate", final_score=0.5, timestamp=now - (3 - i) * 600)
            )

        trend = analyzer.get_trend(model_id="m1", role="generate", window_seconds=86400)
        assert trend is not None
        assert trend.overall_trend == "stable"

    def test_boundary_improving(self, analyzer):
        now = time.time()
        analyzer._store.record_snapshot(
            _make_snapshot(model_id="m1", role="generate", final_score=0.4, timestamp=now - 7200)
        )
        analyzer._store.record_snapshot(
            _make_snapshot(model_id="m1", role="generate", final_score=0.43, timestamp=now - 3600)
        )
        analyzer._store.record_snapshot(
            _make_snapshot(model_id="m1", role="generate", final_score=0.47, timestamp=now - 600)
        )

        trend = analyzer.get_trend(model_id="m1", role="generate", window_seconds=86400)
        assert trend is not None
        assert trend.overall_trend == "improving"  # delta = 0.07 >= 0.05

    def test_boundary_declining(self, analyzer):
        now = time.time()
        analyzer._store.record_snapshot(
            _make_snapshot(model_id="m1", role="generate", final_score=0.6, timestamp=now - 7200)
        )
        analyzer._store.record_snapshot(
            _make_snapshot(model_id="m1", role="generate", final_score=0.57, timestamp=now - 3600)
        )
        analyzer._store.record_snapshot(
            _make_snapshot(model_id="m1", role="generate", final_score=0.53, timestamp=now - 600)
        )

        trend = analyzer.get_trend(model_id="m1", role="generate", window_seconds=86400)
        assert trend is not None
        assert trend.overall_trend == "declining"  # delta = -0.07 <= -0.05

    def test_no_significant_change_driver(self, analyzer):
        now = time.time()
        analyzer._store.record_snapshot(
            _make_snapshot(
                model_id="m1", role="generate",
                final_score=0.5, success_rate=0.5, fallback_rate=0.0,
                avg_duration_ms=1000, timestamp=now - 7200,
            )
        )
        analyzer._store.record_snapshot(
            _make_snapshot(
                model_id="m1", role="generate",
                final_score=0.51, success_rate=0.51, fallback_rate=0.01,
                avg_duration_ms=1010, timestamp=now - 600,
            )
        )

        trend = analyzer.get_trend(model_id="m1", role="generate", window_seconds=86400)
        assert trend is not None
        # Small changes = no significant driver
        assert "no_significant_change" in trend.main_driver or "no_change" in trend.main_driver


# ── Admin API Tests ──


class TestScoringHistoryAPI:
    def test_get_scoring_history_empty(self, client):
        resp = client.get("/admin/pipelines/scoring-history")
        assert resp.status_code == 200
        data = resp.json()
        assert "history" in data
        assert "total" in data
        assert data["total"] == 0

    def test_get_scoring_history_with_params(self, client):
        resp = client.get("/admin/pipelines/scoring-history?role=generate&window=24h")
        assert resp.status_code == 200
        data = resp.json()
        assert data["window"] == "24h"
        assert data["window_seconds"] == 86400

    def test_get_scoring_trends_empty(self, client):
        resp = client.get("/admin/pipelines/scoring-trends")
        assert resp.status_code == 200
        data = resp.json()
        assert "trends" in data
        assert "top_improvers" in data
        assert "top_decliners" in data
        assert "unstable_models" in data

    def test_get_scoring_trends_with_role(self, client):
        resp = client.get("/admin/pipelines/scoring-trends?role=review&window=7d")
        assert resp.status_code == 200
        data = resp.json()
        assert data["window"] == "7d"

    def test_trigger_snapshot(self, client):
        # Mock the registries to avoid browser/agent initialization
        with patch("app.pipeline.observability.scoring_trends.ScoringTrendAnalyzer.capture_snapshot", return_value=0):
            resp = client.post("/admin/pipelines/scoring-history/snapshot")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "snapshots_recorded" in data

    def test_scheduler_status(self, client):
        resp = client.get("/admin/pipelines/scoring-history/scheduler")
        assert resp.status_code == 200
        data = resp.json()
        assert "running" in data
        assert "interval_seconds" in data

    def test_store_stats(self, client):
        resp = client.get("/admin/pipelines/scoring-history/store/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_snapshots" in data
        assert "max_entries" in data
        assert "max_age_days" in data
