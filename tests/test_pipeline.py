"""
Tests for the pipeline subsystem.

Covers:
- Pipeline definition parsing and loading
- Stage execution order
- Input/output handoff
- Guardrail enforcement
- Timeout/failure handling
- Pipeline registry
- Prompt builder
- Diagnostics
"""

import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.errors import BadRequestError, InternalError
from app.pipeline.builtin_pipelines import (
    DRAFT_VERIFY_FINALIZE,
    GENERATE_CRITIQUE_REGENERATE,
    GENERATE_REVIEW_REFINE,
    register_builtin_pipelines,
)
from app.pipeline.diagnostics import PipelineDiagnostics
from app.pipeline.executor import PipelineExecutor
from app.pipeline.prompt_builder import build_stage_prompt
from app.pipeline.types import (
    FailurePolicy,
    InputMapping,
    OutputMode,
    PipelineContext,
    PipelineDefinition,
    PipelineRegistry,
    PipelineStage,
    PipelineTrace,
    StageResult,
    StageRole,
)
from app.schemas.openai import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Choice,
    Message,
    Usage,
)

# ── Fixtures ──


@pytest.fixture
def registry():
    """Fresh registry for each test."""
    r = PipelineRegistry()
    return r


@pytest.fixture
def executor(registry):
    return PipelineExecutor(registry=registry)


@pytest.fixture
def simple_pipeline():
    """Two-stage pipeline for testing."""
    return PipelineDefinition(
        pipeline_id="test-simple",
        display_name="Test Simple",
        description="Simple two-stage test pipeline",
        enabled=True,
        max_total_time_ms=60_000,
        max_stage_retries=1,
        stages=[
            PipelineStage(
                stage_id="generate",
                role=StageRole.GENERATE,
                target_model="qwen",
                output_mode=OutputMode.PLAIN_TEXT,
                failure_policy=FailurePolicy.FAIL_ALL,
                max_retries=0,
            ),
            PipelineStage(
                stage_id="refine",
                role=StageRole.REFINE,
                target_model="glm",
                input_mapping=InputMapping(
                    include_original_request=True,
                    include_previous_output=True,
                    custom_prompt_prefix="Improve this answer:\n{original_request}\n\nDraft: {previous_output}",
                ),
                output_mode=OutputMode.PLAIN_TEXT,
                failure_policy=FailurePolicy.FAIL_ALL,
                max_retries=0,
            ),
        ],
    )


@pytest.fixture
def sample_request():
    return ChatCompletionRequest(
        model="pipeline/test-simple",
        messages=[ChatMessage(role="user", content="What is Python?")],
    )


# ── Pipeline Definition Tests ──


class TestPipelineDefinition:
    def test_builtin_pipelines_register(self, registry):
        register_builtin_pipelines(registry)
        pipelines = registry.list_all()
        assert len(pipelines) == 3

        ids = {p.pipeline_id for p in pipelines}
        assert "generate-review-refine" in ids
        assert "generate-critique-regenerate" in ids
        assert "draft-verify-finalize" in ids

    def test_model_id_auto_generated(self):
        pdef = PipelineDefinition(
            pipeline_id="test-auto",
            display_name="Test",
            stages=[
                PipelineStage(
                    stage_id="s1",
                    role=StageRole.GENERATE,
                    target_model="qwen",
                ),
            ],
        )
        assert pdef.model_id == "pipeline/test-auto"

    def test_model_id_explicit(self):
        pdef = PipelineDefinition(
            pipeline_id="test-explicit",
            display_name="Test",
            model_id="custom/model-id",
            stages=[
                PipelineStage(
                    stage_id="s1",
                    role=StageRole.GENERATE,
                    target_model="qwen",
                ),
            ],
        )
        assert pdef.model_id == "custom/model-id"

    def test_max_stages_enforced_in_definition(self):
        # Pydantic validates max_length=3 — this is the primary guardrail
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            PipelineDefinition(
                pipeline_id="too-many",
                display_name="Too Many",
                stages=[
                    PipelineStage(stage_id=f"s{i}", role=StageRole.GENERATE, target_model="qwen")
                    for i in range(4)
                ],
            )


# ── Registry Tests ──


class TestPipelineRegistry:
    def test_register_and_get(self, registry):
        pdef = PipelineDefinition(
            pipeline_id="test",
            display_name="Test",
            stages=[
                PipelineStage(stage_id="s1", role=StageRole.GENERATE, target_model="qwen"),
            ],
        )
        registry.register(pdef)
        assert registry.get("test") == pdef

    def test_get_missing(self, registry):
        assert registry.get("nonexistent") is None

    def test_get_by_model_id(self, registry):
        pdef = PipelineDefinition(
            pipeline_id="test",
            display_name="Test",
            stages=[
                PipelineStage(stage_id="s1", role=StageRole.GENERATE, target_model="qwen"),
            ],
        )
        registry.register(pdef)
        assert registry.get_by_model_id("pipeline/test") == pdef

    def test_list_enabled(self, registry):
        p1 = PipelineDefinition(
            pipeline_id="enabled",
            display_name="Enabled",
            enabled=True,
            stages=[PipelineStage(stage_id="s1", role=StageRole.GENERATE, target_model="qwen")],
        )
        p2 = PipelineDefinition(
            pipeline_id="disabled",
            display_name="Disabled",
            enabled=False,
            stages=[PipelineStage(stage_id="s1", role=StageRole.GENERATE, target_model="qwen")],
        )
        registry.register(p1)
        registry.register(p2)

        enabled = registry.list_enabled()
        assert len(enabled) == 1
        assert enabled[0].pipeline_id == "enabled"

    def test_is_pipeline_model(self, registry):
        registry.register(
            PipelineDefinition(
                pipeline_id="test",
                display_name="Test",
                stages=[PipelineStage(stage_id="s1", role=StageRole.GENERATE, target_model="qwen")],
            )
        )
        assert registry.is_pipeline_model("pipeline/test") is True
        assert registry.is_pipeline_model("pipeline/unknown") is True  # prefix match
        assert registry.is_pipeline_model("qwen") is False

    def test_enable_disable(self, registry):
        pdef = PipelineDefinition(
            pipeline_id="test",
            display_name="Test",
            enabled=True,
            stages=[PipelineStage(stage_id="s1", role=StageRole.GENERATE, target_model="qwen")],
        )
        registry.register(pdef)

        assert registry.disable("test") is True
        assert registry.get("test").enabled is False

        assert registry.enable("test") is True
        assert registry.get("test").enabled is True

        assert registry.enable("nonexistent") is False

    def test_clear(self, registry):
        pdef = PipelineDefinition(
            pipeline_id="test",
            display_name="Test",
            stages=[PipelineStage(stage_id="s1", role=StageRole.GENERATE, target_model="qwen")],
        )
        registry.register(pdef)
        registry.clear()
        assert len(registry.list_all()) == 0


# ── Prompt Builder Tests ──


class TestPromptBuilder:
    def test_default_passthrough(self, registry):
        """Without custom template, should pass through original request for first stage."""
        ctx = PipelineContext(
            trace=PipelineTrace(pipeline_id="test", model_id="pipeline/test"),
            original_request_model="pipeline/test",
            original_messages=[{"role": "user", "content": "Hello"}],
            original_user_input="Hello",
        )

        prompt = build_stage_prompt(
            stage_id="s1",
            role=StageRole.GENERATE,
            original_request="Hello",
            input_mapping=InputMapping(),
            prompt_template=None,
            context=ctx,
        )
        assert prompt == "Hello"

    def test_custom_prefix_with_variables(self, registry):
        """Custom prompt prefix with variable substitution."""
        ctx = PipelineContext(
            trace=PipelineTrace(pipeline_id="test", model_id="pipeline/test"),
            original_request_model="pipeline/test",
            original_messages=[{"role": "user", "content": "What is X?"}],
            original_user_input="What is X?",
        )
        ctx.stage_outputs["draft"] = StageResult(
            stage_id="draft",
            role=StageRole.GENERATE,
            target_model="qwen",
            output="Draft answer here",
            success=True,
        )

        prompt = build_stage_prompt(
            stage_id="review",
            role=StageRole.REVIEW,
            original_request="What is X?",
            input_mapping=InputMapping(
                include_original_request=True,
                include_previous_output=True,
                custom_prompt_prefix="Review:\nRequest: {original_request}\nDraft: {draft_output}",
            ),
            prompt_template=None,
            context=ctx,
        )
        assert "Request: What is X?" in prompt
        assert "Draft: Draft answer here" in prompt

    def test_template_with_all_variables(self):
        """Template substitution works for all supported variables."""
        ctx = PipelineContext(
            trace=PipelineTrace(pipeline_id="test", model_id="pipeline/test"),
            original_request_model="pipeline/test",
            original_messages=[{"role": "user", "content": "Q"}],
            original_user_input="Q",
        )
        ctx.stage_outputs["draft"] = StageResult(
            stage_id="draft", role=StageRole.GENERATE, target_model="qwen", output="draft text", success=True,
        )
        ctx.stage_outputs["critique"] = StageResult(
            stage_id="critique", role=StageRole.CRITIQUE, target_model="glm", output="critique text", success=True,
        )

        template = "Q: {original_request}\nDraft: {draft_output}\nCritique: {critique_notes}"
        prompt = build_stage_prompt(
            stage_id="refine",
            role=StageRole.REFINE,
            original_request="Q",
            input_mapping=InputMapping(),
            prompt_template=template,
            context=ctx,
        )
        assert "Q: Q" in prompt
        assert "Draft: draft text" in prompt
        assert "Critique: critique text" in prompt


# ── Context Tests ──


class TestPipelineContext:
    def test_get_previous_output(self):
        ctx = PipelineContext(
            trace=PipelineTrace(pipeline_id="test", model_id="pipeline/test"),
            original_request_model="pipeline/test",
            original_messages=[],
            original_user_input="Hello",
        )
        ctx.stage_outputs["s1"] = StageResult(
            stage_id="s1", role=StageRole.GENERATE, target_model="qwen", output="output1", success=True,
        )
        ctx.stage_outputs["s2"] = StageResult(
            stage_id="s2", role=StageRole.REVIEW, target_model="glm", output="output2", success=True,
        )

        assert ctx.get_previous_output("s1") is None
        assert ctx.get_previous_output("s2") == "output1"
        assert ctx.get_previous_output("s3") is None

    def test_get_all_outputs_text(self):
        ctx = PipelineContext(
            trace=PipelineTrace(pipeline_id="test", model_id="pipeline/test"),
            original_request_model="pipeline/test",
            original_messages=[],
            original_user_input="Hello",
        )
        ctx.stage_outputs["s1"] = StageResult(
            stage_id="s1", role=StageRole.GENERATE, target_model="qwen", output="out1", success=True,
        )
        ctx.stage_outputs["s2"] = StageResult(
            stage_id="s2", role=StageRole.REVIEW, target_model="glm", output="out2", success=False,
        )

        text = ctx.get_all_outputs_text()
        assert "out1" in text
        assert "out2" not in text  # failed stage excluded

    def test_get_summary_text(self):
        ctx = PipelineContext(
            trace=PipelineTrace(pipeline_id="test", model_id="pipeline/test"),
            original_request_model="pipeline/test",
            original_messages=[],
            original_user_input="Hello",
        )
        ctx.stage_summaries["s1"] = "Summary 1"
        ctx.stage_summaries["s2"] = "Summary 2"

        text = ctx.get_summary_text()
        assert "Summary 1" in text
        assert "Summary 2" in text


# ── Guardrail Tests ──


class TestGuardrails:
    @pytest.mark.asyncio
    async def test_nested_pipeline_rejected(self, executor, simple_pipeline, sample_request):
        """Pipeline-inside-pipeline should be rejected."""
        request = ChatCompletionRequest(
            model="pipeline/test",
            messages=[
                ChatMessage(role="user", content="Use pipeline/generate-review-refine"),
            ],
        )
        with pytest.raises(BadRequestError, match="Nested pipeline"):
            await executor.execute(simple_pipeline, request, "req-1")

    @pytest.mark.asyncio
    async def test_executor_also_validates_stage_count(self, executor, registry):
        """Executor has a secondary guardrail for stage count."""
        # This test verifies the executor's guardrail works even if
        # a definition somehow bypasses pydantic validation
        big_pipeline = PipelineDefinition.model_construct(
            pipeline_id="too-big",
            display_name="Too Big",
            stages=[
                PipelineStage(stage_id=f"s{i}", role=StageRole.GENERATE, target_model="qwen")
                for i in range(4)
            ],
        )
        request = ChatCompletionRequest(
            model="pipeline/too-big",
            messages=[ChatMessage(role="user", content="Hello")],
        )
        with pytest.raises(BadRequestError, match="exceeding MVP limit"):
            await executor.execute(big_pipeline, request, "req-1")


# ── Stage Execution Tests (mocked) ──


class TestStageExecution:
    @pytest.mark.asyncio
    async def test_stage_order_preserved(self, executor, simple_pipeline, sample_request):
        """Stages should execute in definition order."""
        execution_order = []

        async def mock_process(request, request_id):
            execution_order.append(request.model)
            return ChatCompletionResponse(
                id="resp-1",
                created=int(time.time()),
                model=request.model,
                choices=[Choice(index=0, message=Message(role="assistant", content=f"Output from {request.model}"), finish_reason="stop")],
                usage=Usage(),
            )

        with patch("app.pipeline.executor.chat_proxy_service.process_completion", new=AsyncMock(side_effect=mock_process)):
            response = await executor.execute(simple_pipeline, sample_request, "req-1")

        assert execution_order == ["qwen", "glm"]
        assert response.model == "pipeline/test-simple"
        assert "Output from glm" in response.choices[0].message.content

    @pytest.mark.asyncio
    async def test_stage_failure_fail_all(self, executor, registry, sample_request):
        """FAIL_ALL policy should fail the whole pipeline."""
        failing_pipeline = PipelineDefinition(
            pipeline_id="failing",
            display_name="Failing",
            stages=[
                PipelineStage(
                    stage_id="s1",
                    role=StageRole.GENERATE,
                    target_model="qwen",
                    failure_policy=FailurePolicy.FAIL_ALL,
                ),
            ],
        )

        async def mock_fail(request, request_id):
            raise InternalError("Stage failed")

        with (
            patch("app.pipeline.executor.chat_proxy_service.process_completion", new=AsyncMock(side_effect=mock_fail)),
            pytest.raises(InternalError, match="Stage failed"),
        ):
            await executor.execute(failing_pipeline, sample_request, "req-1")

    @pytest.mark.asyncio
    async def test_stage_failure_skip(self, executor, registry, sample_request):
        """SKIP policy should continue pipeline after failed stage."""
        skip_pipeline_def = PipelineDefinition(
            pipeline_id="skip-test",
            display_name="Skip Test",
            stages=[
                PipelineStage(
                    stage_id="s1",
                    role=StageRole.GENERATE,
                    target_model="qwen",
                    failure_policy=FailurePolicy.SKIP,
                ),
                PipelineStage(
                    stage_id="s2",
                    role=StageRole.REFINE,
                    target_model="glm",
                ),
            ],
        )
        registry.register(skip_pipeline_def)

        call_count = 0

        async def mock_process(request, request_id):
            nonlocal call_count
            call_count += 1
            if request.model == "qwen":
                raise InternalError("s1 failed")
            return ChatCompletionResponse(
                id="resp-1",
                created=int(time.time()),
                model=request.model,
                choices=[Choice(index=0, message=Message(role="assistant", content="s2 output"), finish_reason="stop")],
                usage=Usage(),
            )

        with patch("app.pipeline.executor.chat_proxy_service.process_completion", new=AsyncMock(side_effect=mock_process)):
            response = await executor.execute(skip_pipeline_def, sample_request, "req-1")

        # s2 should have been called
        assert call_count == 2
        assert response.choices[0].message.content == "s2 output"


# ── Diagnostics Tests ──


class TestDiagnostics:
    def test_record_trace(self):
        diag = PipelineDiagnostics()
        trace = PipelineTrace(
            pipeline_id="test",
            model_id="pipeline/test",
            status="completed",
            total_duration_ms=5000,
        )
        diag.record(trace)

        stats = diag.get_stats()
        assert "test" in stats
        assert stats["test"]["executions"] == 1
        assert stats["test"]["success_count"] == 1
        assert stats["test"]["success_rate"] == 1.0

    def test_recent_traces(self):
        diag = PipelineDiagnostics(max_traces=5)
        for i in range(10):
            trace = PipelineTrace(
                pipeline_id="test",
                model_id="pipeline/test",
                status="completed",
                total_duration_ms=1000 * (i + 1),
            )
            diag.record(trace)

        recent = diag.get_recent_traces(3)
        assert len(recent) == 3

    def test_trace_by_id(self):
        diag = PipelineDiagnostics()
        trace = PipelineTrace(
            pipeline_id="test",
            model_id="pipeline/test",
            status="completed",
        )
        diag.record(trace)

        found = diag.get_trace(trace.trace_id)
        assert found is not None
        assert found.trace_id == trace.trace_id

    def test_traces_by_pipeline(self):
        diag = PipelineDiagnostics()
        for _ in range(5):
            diag.record(
                PipelineTrace(pipeline_id="p1", model_id="pipeline/p1", status="completed")
            )
        for _ in range(3):
            diag.record(
                PipelineTrace(pipeline_id="p2", model_id="pipeline/p2", status="completed")
            )

        p1_traces = diag.get_traces_by_pipeline("p1")
        assert len(p1_traces) == 5

        p2_traces = diag.get_traces_by_pipeline("p2")
        assert len(p2_traces) == 3

    def test_failure_stats(self):
        diag = PipelineDiagnostics()
        diag.record(
            PipelineTrace(pipeline_id="test", model_id="pipeline/test", status="completed")
        )
        diag.record(
            PipelineTrace(pipeline_id="test", model_id="pipeline/test", status="failed", error_message="error")
        )

        stats = diag.get_stats()
        assert stats["test"]["executions"] == 2
        assert stats["test"]["success_count"] == 1
        assert stats["test"]["failure_count"] == 1
        assert stats["test"]["success_rate"] == 0.5

    def test_clear(self):
        diag = PipelineDiagnostics()
        diag.record(PipelineTrace(pipeline_id="test", model_id="pipeline/test"))
        diag.clear()
        assert len(diag.get_recent_traces()) == 0
        assert diag.get_stats() == {}


# ── Built-in Pipeline Tests ──


class TestBuiltInPipelines:
    def test_generate_review_refine_structure(self):
        pdef = GENERATE_REVIEW_REFINE
        assert len(pdef.stages) == 3
        assert pdef.stages[0].role == StageRole.GENERATE
        assert pdef.stages[1].role == StageRole.REVIEW
        assert pdef.stages[2].role == StageRole.REFINE

    def test_generate_critique_regenerate_structure(self):
        pdef = GENERATE_CRITIQUE_REGENERATE
        assert len(pdef.stages) == 3
        assert pdef.stages[0].role == StageRole.GENERATE
        assert pdef.stages[1].role == StageRole.CRITIQUE
        assert pdef.stages[2].role == StageRole.GENERATE

    def test_draft_verify_finalize_structure(self):
        pdef = DRAFT_VERIFY_FINALIZE
        assert len(pdef.stages) == 3
        assert pdef.stages[0].role == StageRole.GENERATE
        assert pdef.stages[1].role == StageRole.VERIFY
        assert pdef.stages[2].role == StageRole.REFINE

    def test_all_have_review_stage_with_critique(self):
        """Pipelines with a review/critique stage should have proper input mapping."""
        for pdef in [GENERATE_REVIEW_REFINE, GENERATE_CRITIQUE_REGENERATE]:
            review_stage = next(s for s in pdef.stages if s.role in (StageRole.REVIEW, StageRole.CRITIQUE))
            assert review_stage.input_mapping.include_original_request is True
            assert review_stage.input_mapping.include_previous_output is True
            assert review_stage.input_mapping.custom_prompt_prefix is not None


# ── API Integration Tests ──


class TestPipelineAPI:
    @pytest.fixture
    def client(self):
        # Initialize pipelines before creating the client
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

    def test_list_models_includes_pipelines(self, client):
        """Pipeline models should appear in /v1/models."""
        with patch("app.api.routes_openai.create_model_list") as mock_list:
            from app.utils.openai_mapper import create_model_list as real_create
            mock_list.return_value = real_create()

            response = client.get("/v1/models")
            assert response.status_code == 200
            data = response.json()
            # Pipelines should be listed
            model_ids = [m["id"] for m in data["data"]]
            assert any(m.startswith("pipeline/") for m in model_ids)

    def test_admin_list_pipelines(self, client):
        """GET /admin/pipelines should return pipeline definitions."""
        response = client.get("/admin/pipelines")
        assert response.status_code == 200
        data = response.json()
        assert "pipelines" in data
        assert data["total"] >= 3

    def test_admin_get_pipeline_detail(self, client):
        """GET /admin/pipelines/{id} should return detailed info."""
        response = client.get("/admin/pipelines/generate-review-refine")
        assert response.status_code == 200
        data = response.json()
        assert data["pipeline_id"] == "generate-review-refine"
        assert len(data["stages"]) == 3

    def test_admin_enable_disable_pipeline(self, client):
        """POST enable/disable should toggle pipeline state."""
        # Disable
        response = client.post("/admin/pipelines/generate-review-refine/disable")
        assert response.status_code == 200
        assert response.json()["enabled"] is False

        # Enable
        response = client.post("/admin/pipelines/generate-review-refine/enable")
        assert response.status_code == 200
        assert response.json()["enabled"] is True

    def test_admin_get_unknown_pipeline(self, client):
        """GET unknown pipeline should return 404."""
        response = client.get("/admin/pipelines/nonexistent")
        assert response.status_code == 404

    def test_admin_pipeline_stats(self, client):
        """GET /admin/pipelines/stats should return stats."""
        response = client.get("/admin/pipelines/stats")
        assert response.status_code == 200
        assert "stats" in response.json()

    def test_admin_traces(self, client):
        """GET /admin/pipelines/traces should return traces."""
        response = client.get("/admin/pipelines/traces")
        assert response.status_code == 200
        assert "traces" in response.json()
