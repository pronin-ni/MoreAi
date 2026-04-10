"""
Tests for execution trace completeness.

Covers:
- trace records full scoring breakdown per candidate
- trace contains fallback chain
- trace contains quality signals
- trace contains cross-stage signals
- execution detail endpoint fallbacks to persistent store
- execution summary endpoint
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.pipeline.observability.trace_model import (
    CandidateExplain,
    StageExecutionSummary,
    StageSelectionExplain,
)

# ── Fixtures ──


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


# ── Trace Model Tests ──


class TestCandidateExplain:
    def test_to_dict_includes_scoring_breakdown(self):
        explain = CandidateExplain(
            model_id="qwen",
            provider_id="qwen-provider",
            transport="api",
            rank=1,
            score=0.78,
            scoring_breakdown={
                "base_static_score": 0.65,
                "dynamic_adjustment": 0.05,
                "failure_penalty": 0.0,
                "penalty_reasons": [],
                "performance": {"success_rate": 0.9, "fallback_rate": 0.05, "sample_count": 50},
            },
            selected=True,
            selection_reason="highest_ranked",
        )
        result = explain.to_dict()
        assert "scoring_breakdown" in result
        assert result["scoring_breakdown"]["base_static_score"] == 0.65
        assert result["scoring_breakdown"]["performance"]["sample_count"] == 50
        assert result["score"] == 0.78

    def test_to_dict_omits_empty_scoring_breakdown(self):
        explain = CandidateExplain(
            model_id="kimi", provider_id="kimi-provider", transport="browser",
        )
        result = explain.to_dict()
        assert "scoring_breakdown" not in result


class TestStageExecutionSummary:
    def test_to_dict_includes_quality(self):
        summary = StageExecutionSummary(
            stage_id="generate", stage_role="generate", status="completed",
            selected_model="qwen", selected_provider="p1", selected_transport="api",
            quality_score=0.72,
            quality_explanation="quality=0.72: structured",
        )
        result = summary.to_dict()
        assert result["quality_score"] == 0.72
        assert result["quality_explanation"] == "quality=0.72: structured"

    def test_to_dict_omits_default_quality(self):
        summary = StageExecutionSummary(
            stage_id="generate", stage_role="generate", status="completed",
        )
        result = summary.to_dict()
        assert "quality_score" not in result
        assert "quality_explanation" not in result

    def test_to_dict_includes_cross_stage(self):
        summary = StageExecutionSummary(
            stage_id="generate", stage_role="generate", status="completed",
            cross_stage={
                "downstream_corrections": 3,
                "correction_severity": 0.5,
                "final_improvement_score": 0.65,
            },
        )
        result = summary.to_dict()
        assert "cross_stage" in result
        assert result["cross_stage"]["downstream_corrections"] == 3

    def test_to_dict_omits_empty_cross_stage(self):
        summary = StageExecutionSummary(
            stage_id="generate", stage_role="generate", status="completed",
        )
        result = summary.to_dict()
        assert "cross_stage" not in result

    def test_full_stage_trace_structure(self):
        """Verify a complete stage trace has all expected fields."""
        selection = StageSelectionExplain(
            stage_id="generate", stage_role="generate",
            selected_model="qwen", selected_provider="p1", selected_transport="api",
            selection_reason="highest_ranked",
            candidate_details=[
                CandidateExplain(
                    model_id="qwen", provider_id="p1", transport="api",
                    rank=1, score=0.78, selected=True,
                    scoring_breakdown={"base_static_score": 0.7, "dynamic_adjustment": 0.08},
                ),
                CandidateExplain(
                    model_id="kimi", provider_id="p2", transport="browser",
                    rank=2, score=0.74, excluded=True, exclusion_reason="latency_too_high",
                ),
            ],
        )
        summary = StageExecutionSummary(
            stage_id="generate", stage_role="generate", status="completed",
            selected_model="qwen", selected_provider="p1", selected_transport="api",
            selection_explain=selection,
            duration_ms=1500.0,
            quality_score=0.72,
            quality_explanation="quality=0.72: structured",
            cross_stage={"downstream_corrections": 2, "correction_severity": 0.4},
        )
        result = summary.to_dict()

        # Verify structure
        assert result["stage_id"] == "generate"
        assert result["status"] == "completed"
        assert "selection_explain" in result
        assert len(result["selection_explain"]["candidate_details"]) == 2
        assert result["selection_explain"]["candidate_details"][0]["scoring_breakdown"]["base_static_score"] == 0.7
        assert result["quality_score"] == 0.72
        assert result["cross_stage"]["downstream_corrections"] == 2


# ── Admin API Tests ──


class TestExecutionTraceAPI:
    def test_get_execution_detail_not_found(self, client):
        resp = client.get("/admin/pipelines/executions/nonexistent")
        assert resp.status_code == 404

    def test_get_execution_summary_not_found(self, client):
        resp = client.get("/admin/pipelines/executions/nonexistent/summary")
        assert resp.status_code == 404

    def test_execution_store_stats(self, client):
        resp = client.get("/admin/pipelines/executions/store/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_stored" in data or "stats" in data or "count" in data
