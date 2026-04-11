"""
Tests for execution trace completeness.

Covers:
- trace records full scoring breakdown per candidate
- trace contains fallback chain
- trace contains quality signals
- trace contains cross-stage signals
- execution detail endpoint fallbacks to persistent store
- execution summary endpoint
- studio execution details endpoint (user-facing)
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


# ── Studio Execution Details Tests ──


class TestStudioExecutionDetails:
    """Tests for /studio/executions/{id} endpoint and user-facing details."""

    def test_studio_execution_detail_not_found(self, client):
        resp = client.get("/studio/executions/nonexistent")
        assert resp.status_code == 200
        data = resp.json()
        assert "error" in data

    def test_studio_user_facing_details_structure(self):
        """Test the _build_user_facing_details helper produces correct shape."""
        from app.api.routes_studio import _build_user_facing_details

        raw_data = {
            "execution_id": "test-123",
            "pipeline_id": "generate-review-refine",
            "pipeline_display_name": "Generate → Review → Refine",
            "status": "success",
            "duration_ms": 12000,
            "total_fallbacks": 1,
            "total_retries": 0,
            "stages": [
                {
                    "stage_id": "draft",
                    "stage_role": "generate",
                    "status": "completed",
                    "selected_model": "qwen",
                    "selected_provider": "qwen-provider",
                    "selected_transport": "api",
                    "duration_ms": 3000,
                    "fallback_count": 0,
                    "retry_count": 0,
                    "selection_explain": {
                        "selected_model": "qwen",
                        "selected_provider": "qwen-provider",
                        "selected_transport": "api",
                        "fallback_chain": [],
                    },
                },
                {
                    "stage_id": "review",
                    "stage_role": "review",
                    "status": "completed",
                    "selected_model": "glm",
                    "selected_provider": "glm-provider",
                    "selected_transport": "api",
                    "duration_ms": 5000,
                    "fallback_count": 1,
                    "retry_count": 0,
                    "selection_explain": {
                        "selected_model": "glm",
                        "selected_provider": "glm-provider",
                        "selected_transport": "api",
                        "fallback_chain": [
                            {"failed_model": "qwen", "reason": "timeout", "penalty": 0.08},
                        ],
                    },
                },
                {
                    "stage_id": "refine",
                    "stage_role": "refine",
                    "status": "completed",
                    "selected_model": "qwen",
                    "selected_provider": "qwen-provider",
                    "selected_transport": "api",
                    "duration_ms": 4000,
                    "fallback_count": 0,
                    "retry_count": 0,
                    "selection_explain": {
                        "selected_model": "qwen",
                        "selected_provider": "qwen-provider",
                        "selected_transport": "api",
                        "fallback_chain": [],
                    },
                    "quality_score": 0.75,
                    "cross_stage": {
                        "review_actionability": 0.8,
                        "refine_effectiveness": 0.7,
                    },
                },
            ],
        }

        result = _build_user_facing_details(raw_data)

        # Top-level structure
        assert result["execution_id"] == "test-123"
        assert result["stage_count"] == 3
        assert result["total_fallbacks"] == 1
        assert "verdict" in result
        assert "stages" in result

        # Stage cards
        assert len(result["stages"]) == 3
        stage1 = result["stages"][0]
        assert stage1["role_label"] == "Draft"
        assert stage1["status_label"] == "Completed"
        assert stage1["explanation"] == "Draft generated"
        assert stage1["fallback_count"] == 0

        stage2 = result["stages"][1]
        assert stage2["fallback_count"] == 1
        assert len(stage2["fallbacks"]) == 1
        assert stage2["fallbacks"][0]["reason"] == "timed out"  # User-friendly
        assert stage2["explanation"] == "Answer reviewed (with fallback)"

        # Cross-stage
        assert "cross_stage" in result
        assert result["verdict"]["has_fallbacks"] is True

    def test_user_friendly_fallback_reason(self):
        """Test fallback reason translation."""
        from app.api.routes_studio import _user_friendly_fallback_reason

        assert _user_friendly_fallback_reason("timeout") == "timed out"
        assert _user_friendly_fallback_reason("circuit_breaker_open") == "temporarily unavailable"
        assert _user_friendly_fallback_reason("model_unavailable") == "unavailable"
        assert _user_friendly_fallback_reason("rate_limited") == "rate limited"
        assert _user_friendly_fallback_reason("") == "switched model"
        assert _user_friendly_fallback_reason("some_unknown_error") == "error occurred"
        assert _user_friendly_fallback_reason("internal_server_error") == "error occurred"

    def test_stage_explanation_text(self):
        """Test stage explanation text generation."""
        from app.api.routes_studio import _stage_explanation

        assert _stage_explanation("generate", "completed", 0) == "Draft generated"
        assert _stage_explanation("generate", "completed", 1) == "Draft generated (with fallback)"
        assert _stage_explanation("generate", "skipped", 0) == "Draft generated (skipped)"
        assert _stage_explanation("generate", "failed", 0) == "Draft generated — failed"
        assert _stage_explanation("unknown", "completed", 0) == "Unknown completed"

    def test_verdict_computation(self):
        """Test overall verdict computation."""
        from app.api.routes_studio import _compute_verdict

        # All completed, high quality
        stages = [
            {"status": "completed", "quality_score": 0.8},
            {"status": "completed", "quality_score": 0.75},
        ]
        verdict = _compute_verdict(stages, 0, {})
        assert verdict["label"] == "High confidence result"
        assert verdict["all_completed"] is True
        assert verdict["has_fallbacks"] is False

        # With fallbacks
        verdict_fb = _compute_verdict(stages, 2, {})
        assert verdict_fb["label"] == "Completed with fallbacks"

        # Low quality
        stages_low = [
            {"status": "completed", "quality_score": 0.3},
        ]
        verdict_low = _compute_verdict(stages_low, 0, {})
        assert verdict_low["label"] == "Result with low confidence"

