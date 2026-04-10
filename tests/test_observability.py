"""
Tests for pipeline observability layer.

Covers:
- Trace model creation and bounded summaries
- Stage selection explainability recording
- Execution store retention and filtering
- Observability recorder
- Failure analysis
- Admin endpoints
- Metrics increments
"""

import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.pipeline.observability.recorder import ObservabilityRecorder
from app.pipeline.observability.store import PipelineExecutionStore
from app.pipeline.observability.trace_model import (
    CandidateExplain,
    FailureAnalysis,
    PipelineExecutionSummary,
    StageExecutionSummary,
    StageSelectionExplain,
)
from app.pipeline.types import (
    PipelineContext,
    PipelineDefinition,
    PipelineStage,
    PipelineTrace,
    StageResult,
    StageRole,
    StageTrace,
)

# ── Fixtures ──


@pytest.fixture
def fresh_store():
    """Fresh execution store for each test."""
    s = PipelineExecutionStore(max_executions=10, max_per_pipeline=5)
    yield s
    s.clear()


@pytest.fixture
def recorder(fresh_store):
    """Recorder with fresh store."""
    r = ObservabilityRecorder()
    # Monkey-patch the recorder's internal import to use fresh store
    import app.pipeline.observability.recorder as rec_mod
    import app.pipeline.observability.store as store_mod
    orig_store = store_mod.execution_store
    store_mod.execution_store = fresh_store

    # Also patch the recorder module's import
    rec_mod.execution_store = fresh_store

    yield r
    store_mod.execution_store = orig_store
    rec_mod.execution_store = orig_store


@pytest.fixture
def sample_pipeline():
    return PipelineDefinition(
        pipeline_id="test-pipeline",
        display_name="Test Pipeline",
        stages=[
            PipelineStage(stage_id="generate", role=StageRole.GENERATE, target_model="qwen"),
            PipelineStage(stage_id="review", role=StageRole.REVIEW, target_model="glm"),
        ],
    )


# ── Trace Model Tests ──


class TestTraceModel:
    def test_candidate_explain_to_dict(self):
        explain = CandidateExplain(
            model_id="qwen",
            provider_id="qwen",
            transport="browser",
            rank=1,
            score=0.85,
            selected=True,
            selection_reason="highest_ranked",
        )
        d = explain.to_dict()
        assert d["model_id"] == "qwen"
        assert d["score"] == 0.85
        assert d["selected"] is True

    def test_stage_selection_explain_to_dict(self):
        explain = StageSelectionExplain(
            stage_id="generate",
            stage_role="generate",
            candidates_considered=3,
            candidates_viable=2,
            selected_model="qwen",
            selection_reason="highest_ranked",
            candidate_details=[
                CandidateExplain(model_id="qwen", provider_id="qwen", transport="browser", selected=True),
            ],
        )
        d = explain.to_dict()
        assert d["candidates_considered"] == 3
        assert len(d["candidate_details"]) == 1

    def test_stage_execution_summary_to_dict(self):
        summary = StageExecutionSummary(
            stage_id="generate",
            stage_role="generate",
            status="completed",
            selected_model="qwen",
            selected_provider="qwen",
            duration_ms=5000,
            retry_count=1,
            fallback_count=0,
            output_summary="Generated answer about Python...",
        )
        d = summary.to_dict()
        assert d["stage_id"] == "generate"
        assert d["status"] == "completed"
        assert d["duration_ms"] == 5000
        assert "output_summary" in d

    def test_stage_summary_bounded_output(self):
        summary = StageExecutionSummary(
            stage_id="test",
            stage_role="generate",
            status="completed",
        )
        long_text = "x" * 1000
        summary.output_summary = summary._bounded(long_text, 100)
        assert len(summary.output_summary) <= 100
        assert summary.output_summary.endswith("...")

    def test_pipeline_execution_summary_to_dict(self):
        summary = PipelineExecutionSummary(
            execution_id="abc123",
            pipeline_id="test",
            pipeline_display_name="Test Pipeline",
            status="success",
            duration_ms=15000,
            total_budget_ms=180000,
            budget_consumed_pct=8.3,
            stage_count=2,
            stages_completed=2,
            total_fallbacks=1,
        )
        d = summary.to_dict()
        assert d["execution_id"] == "abc123"
        assert d["status"] == "success"
        assert "stages" in d

    def test_pipeline_execution_summary_to_list_row(self):
        summary = PipelineExecutionSummary(
            execution_id="abc123",
            pipeline_id="test",
            status="success",
            duration_ms=15000,
            stage_count=2,
            stages_completed=2,
            started_at=time.monotonic(),
            finished_at=time.monotonic() + 15,
        )
        row = summary.to_list_row()
        assert "execution_id" in row
        assert "pipeline_id" in row
        assert "status" in row
        assert "duration_ms" in row
        assert "total_fallbacks" in row

    def test_failure_analysis_to_dict(self):
        analysis = FailureAnalysis(
            execution_id="abc123",
            pipeline_id="test",
            failed_stage="review",
            failed_stage_role="review",
            failure_reason="timeout",
            root_cause="timeout",
            retry_count=1,
            fallback_count=0,
            candidates_exhausted=False,
            budget_exceeded=False,
        )
        d = analysis.to_dict()
        assert d["execution_id"] == "abc123"
        assert d["root_cause"] == "timeout"
        assert d["candidates_exhausted"] is False


# ── Execution Store Tests ──


class TestExecutionStore:
    def test_store_and_retrieve(self, fresh_store):
        summary = PipelineExecutionSummary(
            execution_id="e1",
            pipeline_id="p1",
            status="success",
            duration_ms=10000,
        )
        fresh_store.store(summary)
        result = fresh_store.get("e1")
        assert result is not None
        assert result.execution_id == "e1"

    def test_get_missing(self, fresh_store):
        assert fresh_store.get("nonexistent") is None

    def test_get_recent_returns_latest(self, fresh_store):
        for i in range(5):
            fresh_store.store(PipelineExecutionSummary(
                execution_id=f"e{i}",
                pipeline_id="p1",
                status="success",
            ))

        recent = fresh_store.get_recent(limit=3)
        assert len(recent) == 3
        # Most recent first
        assert recent[0].execution_id == "e4"

    def test_filter_by_pipeline(self, fresh_store):
        fresh_store.store(PipelineExecutionSummary(execution_id="e1", pipeline_id="p1", status="success"))
        fresh_store.store(PipelineExecutionSummary(execution_id="e2", pipeline_id="p2", status="success"))
        fresh_store.store(PipelineExecutionSummary(execution_id="e3", pipeline_id="p1", status="failed"))

        p1_execs = fresh_store.get_recent(pipeline_id="p1")
        assert len(p1_execs) == 2
        assert all(e.pipeline_id == "p1" for e in p1_execs)

    def test_filter_by_status(self, fresh_store):
        fresh_store.store(PipelineExecutionSummary(execution_id="e1", pipeline_id="p1", status="success"))
        fresh_store.store(PipelineExecutionSummary(execution_id="e2", pipeline_id="p1", status="failed"))

        failed = fresh_store.get_recent(status="failed")
        assert len(failed) == 1
        assert failed[0].status == "failed"

    def test_per_pipeline_bounded(self):
        store = PipelineExecutionStore(max_executions=100, max_per_pipeline=3)
        for i in range(10):
            store.store(PipelineExecutionSummary(
                execution_id=f"e{i}",
                pipeline_id="p1",
                status="success",
            ))

        p1_execs = store.get_by_pipeline("p1")
        assert len(p1_execs) == 3  # bounded to 3

    def test_global_bounded(self):
        store = PipelineExecutionStore(max_executions=5, max_per_pipeline=5)
        for i in range(10):
            store.store(PipelineExecutionSummary(
                execution_id=f"e{i}",
                pipeline_id=f"p{i}",
                status="success",
            ))

        recent = store.get_recent(limit=100)
        assert len(recent) == 5  # bounded to 5

    def test_get_stats(self, fresh_store):
        fresh_store.store(PipelineExecutionSummary(execution_id="e1", pipeline_id="p1", status="success"))
        fresh_store.store(PipelineExecutionSummary(execution_id="e2", pipeline_id="p1", status="failed"))
        fresh_store.store(PipelineExecutionSummary(execution_id="e3", pipeline_id="p2", status="success"))

        stats = fresh_store.get_stats()
        assert stats["total_stored"] == 3
        assert stats["by_status"]["success"] == 2
        assert stats["by_status"]["failed"] == 1
        assert stats["pipeline_count"] == 2

    def test_clear(self, fresh_store):
        fresh_store.store(PipelineExecutionSummary(execution_id="e1", pipeline_id="p1"))
        fresh_store.clear()
        assert fresh_store.get_stats()["total_stored"] == 0


# ── Observability Recorder Tests ──


class TestObservabilityRecorder:
    def test_record_from_context_creates_summary(self, recorder, fresh_store, sample_pipeline):
        trace = PipelineTrace(
            pipeline_id=sample_pipeline.pipeline_id,
            model_id="pipeline/test",
            status="completed",
            total_duration_ms=15000,
            final_output="Final answer text",
            stage_traces=[
                StageTrace(stage_id="generate", role="generate", target_model="qwen", provider_id="qwen", status="completed", duration_ms=5000),
                StageTrace(stage_id="review", role="review", target_model="glm", provider_id="glm", status="completed", duration_ms=10000),
            ],
        )
        ctx = PipelineContext(
            trace=trace,
            original_request_model="pipeline/test",
            original_messages=[{"role": "user", "content": "What is Python?"}],
            original_user_input="What is Python?",
        )
        ctx.stage_outputs["generate"] = StageResult(
            stage_id="generate", role=StageRole.GENERATE, target_model="qwen",
            provider_id="qwen", output="Draft answer", success=True, duration_ms=5000,
        )
        ctx.stage_outputs["review"] = StageResult(
            stage_id="review", role=StageRole.REVIEW, target_model="glm",
            provider_id="glm", output="Review complete", success=True, duration_ms=10000,
        )

        summary = recorder.record_from_context(ctx, sample_pipeline, "req-1")

        assert summary.execution_id == trace.trace_id
        assert summary.pipeline_id == sample_pipeline.pipeline_id
        assert summary.status == "success"
        assert summary.duration_ms == 15000
        assert summary.stage_count == 2
        assert summary.stages_completed == 2
        assert len(fresh_store.get_recent()) == 1

    def test_record_failed_pipeline(self, recorder, fresh_store, sample_pipeline):
        trace = PipelineTrace(
            pipeline_id=sample_pipeline.pipeline_id,
            model_id="pipeline/test",
            status="failed",
            total_duration_ms=8000,
            error_message="Stage review timed out",
            stage_traces=[
                StageTrace(stage_id="generate", role="generate", target_model="qwen", provider_id="qwen", status="completed", duration_ms=5000),
                StageTrace(stage_id="review", role="review", target_model="glm", provider_id="glm", status="failed", duration_ms=3000, error_message="timeout"),
            ],
        )
        ctx = PipelineContext(
            trace=trace,
            original_request_model="pipeline/test",
            original_messages=[{"role": "user", "content": "Q"}],
            original_user_input="Q",
        )
        ctx.stage_outputs["generate"] = StageResult(
            stage_id="generate", role=StageRole.GENERATE, target_model="qwen",
            provider_id="qwen", output="Draft", success=True, duration_ms=5000,
        )
        ctx.stage_outputs["review"] = StageResult(
            stage_id="review", role=StageRole.REVIEW, target_model="glm",
            provider_id="glm", output="", success=False,
            error_message="timeout", error_type="GatewayTimeoutError", duration_ms=3000,
        )

        summary = recorder.record_from_context(ctx, sample_pipeline, "req-1")
        assert summary.status == "failed"
        assert summary.failed_stage == "review"

    def test_bounded_output_summary(self, recorder, fresh_store, sample_pipeline):
        long_output = "x" * 2000
        trace = PipelineTrace(
            pipeline_id=sample_pipeline.pipeline_id,
            model_id="pipeline/test",
            status="completed",
            total_duration_ms=5000,
            final_output=long_output,
            stage_traces=[
                StageTrace(stage_id="generate", role="generate", target_model="qwen", provider_id="qwen", status="completed", duration_ms=5000),
            ],
        )
        ctx = PipelineContext(
            trace=trace,
            original_request_model="pipeline/test",
            original_messages=[{"role": "user", "content": "Q"}],
            original_user_input="Q",
        )
        ctx.stage_outputs["generate"] = StageResult(
            stage_id="generate", role=StageRole.GENERATE, target_model="qwen",
            provider_id="qwen", output=long_output, success=True, duration_ms=5000,
        )

        summary = recorder.record_from_context(ctx, sample_pipeline, "req-1")
        assert len(summary.final_output_summary) <= 500

    def test_selection_explainability_recorded(self, recorder, fresh_store, sample_pipeline):
        trace = PipelineTrace(
            pipeline_id=sample_pipeline.pipeline_id,
            model_id="pipeline/test",
            status="completed",
            total_duration_ms=10000,
            stage_traces=[
                StageTrace(stage_id="generate", role="generate", target_model="qwen", provider_id="qwen", status="completed", duration_ms=5000),
                StageTrace(stage_id="review", role="review", target_model="glm", provider_id="glm", status="completed", duration_ms=5000),
            ],
        )
        ctx = PipelineContext(
            trace=trace,
            original_request_model="pipeline/test",
            original_messages=[{"role": "user", "content": "Q"}],
            original_user_input="Q",
        )
        ctx.stage_outputs["generate"] = StageResult(
            stage_id="generate", role=StageRole.GENERATE, target_model="qwen",
            provider_id="qwen", output="Draft", success=True, duration_ms=5000,
        )
        ctx.stage_outputs["review"] = StageResult(
            stage_id="review", role=StageRole.REVIEW, target_model="glm",
            provider_id="glm", output="Review", success=True, duration_ms=5000,
        )
        # Simulate selection trace metadata
        ctx.metadata["selection_trace:generate"] = {
            "all_candidates": [
                {"model_id": "qwen", "provider_id": "qwen", "transport": "browser", "rank": 1, "final_score": 0.9, "selected_reason": "highest_ranked"},
                {"model_id": "glm", "provider_id": "glm", "transport": "browser", "rank": 2, "final_score": 0.8},
            ],
            "selected_model": "qwen",
            "selected_provider": "qwen",
            "fallback_chain": [],
        }

        summary = recorder.record_from_context(ctx, sample_pipeline, "req-1")
        generate_stage = next(s for s in summary.stage_summaries if s.stage_id == "generate")
        assert generate_stage.selection_explain is not None
        assert generate_stage.selection_explain.candidates_considered == 2
        assert generate_stage.selection_explain.selected_model == "qwen"

    def test_failure_analysis_built(self, recorder, fresh_store, sample_pipeline):
        trace = PipelineTrace(
            pipeline_id=sample_pipeline.pipeline_id,
            model_id="pipeline/test",
            status="failed",
            total_duration_ms=10000,
            error_message="Review stage failed",
            stage_traces=[
                StageTrace(stage_id="generate", role="generate", target_model="qwen", provider_id="qwen", status="completed", duration_ms=5000),
                StageTrace(stage_id="review", role="review", target_model="glm", provider_id="glm", status="failed", duration_ms=5000, error_message="timeout"),
            ],
        )
        ctx = PipelineContext(
            trace=trace,
            original_request_model="pipeline/test",
            original_messages=[{"role": "user", "content": "Q"}],
            original_user_input="Q",
        )
        ctx.stage_outputs["generate"] = StageResult(
            stage_id="generate", role=StageRole.GENERATE, target_model="qwen",
            provider_id="qwen", output="Draft", success=True, duration_ms=5000,
        )
        ctx.stage_outputs["review"] = StageResult(
            stage_id="review", role=StageRole.REVIEW, target_model="glm",
            provider_id="glm", output="", success=False,
            error_message="timeout", error_type="GatewayTimeoutError", duration_ms=5000,
        )

        summary = recorder.record_from_context(ctx, sample_pipeline, "req-1")
        analysis = recorder.build_failure_analysis(summary)

        assert analysis.failed_stage == "review"
        assert analysis.root_cause == "timeout"
        assert analysis.stage_results[0]["status"] == "completed"
        assert analysis.stage_results[1]["status"] == "failed"

    def test_root_cause_classification(self, recorder):
        # Timeout
        stage = StageExecutionSummary(stage_id="s1", stage_role="generate", status="failed",
                                      failure_reason="GatewayTimeoutError: timeout", error_type="GatewayTimeoutError")
        assert recorder._classify_root_cause(stage) == "timeout"

        # Circuit breaker
        stage2 = StageExecutionSummary(stage_id="s1", stage_role="generate", status="failed",
                                       failure_reason="circuit breaker open")
        assert recorder._classify_root_cause(stage2) == "circuit_breaker"

        # No viable candidates
        stage3 = StageExecutionSummary(stage_id="s1", stage_role="generate", status="failed",
                                       failure_reason="no viable candidates available")
        assert recorder._classify_root_cause(stage3) == "no_viable_candidates"

        # Unknown
        stage4 = StageExecutionSummary(stage_id="s1", stage_role="generate", status="failed",
                                       failure_reason="something weird happened")
        assert recorder._classify_root_cause(stage4) == "execution_error"


# ── Admin API Tests ──


class TestObservabilityAPI:
    @pytest.fixture
    def client(self):
        from app.pipeline.executor import initialize_pipelines
        initialize_pipelines()

        with (
            patch("app.main.browser_dispatcher.initialize", new=AsyncMock()),
            patch("app.main.browser_dispatcher.shutdown", new=AsyncMock()),
            patch("app.main.unified_registry.initialize", new=AsyncMock()),
        ):
            from app.main import app
            yield TestClient(app)

    def test_list_executions(self, client):
        response = client.get("/admin/pipelines/executions")
        assert response.status_code == 200
        data = response.json()
        assert "executions" in data
        assert "total" in data

    def test_list_executions_with_filters(self, client):
        response = client.get("/admin/pipelines/executions?status=success&pipeline_id=generate-review-refine")
        assert response.status_code == 200
        data = response.json()
        assert "filters" in data
        assert data["filters"]["status"] == "success"

    def test_get_execution_detail_not_found(self, client):
        response = client.get("/admin/pipelines/executions/nonexistent")
        assert response.status_code == 404

    def test_execution_store_stats(self, client):
        response = client.get("/admin/pipelines/executions/store/stats")
        assert response.status_code == 200
        data = response.json()
        assert "total_stored" in data

    def test_run_pipeline_test(self, client):
        """Run a test execution (will likely fail without real providers, but endpoint should work)."""
        response = client.post("/admin/pipelines/generate-review-refine/run-test")
        # Either success or failure is fine — the endpoint should respond
        assert response.status_code in (200, 400)

    def test_run_pipeline_test_with_prompt(self, client):
        response = client.post(
            "/admin/pipelines/generate-review-refine/run-test",
            json={"prompt": "Test prompt for diagnostics"},
        )
        assert response.status_code in (200, 400)

    def test_run_pipeline_test_unknown_pipeline(self, client):
        response = client.post("/admin/pipelines/nonexistent-pipeline/run-test")
        assert response.status_code == 404

    def test_run_pipeline_test_disabled_pipeline(self, client):
        # First disable the pipeline
        client.post("/admin/pipelines/generate-review-refine/disable")
        response = client.post("/admin/pipelines/generate-review-refine/run-test")
        assert response.status_code == 400
        # Re-enable for other tests
        client.post("/admin/pipelines/generate-review-refine/enable")
