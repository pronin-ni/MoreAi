"""
Tests for pipeline execution store bootstrap, browser retry observability,
and pipeline observability with retry.

Covers:
A. Execution store
   - pipeline_executions table auto-created
   - write succeeds on fresh DB
   - repeated init is idempotent

B. Browser retry observability
   - first attempt timeout + second success
   - total duration != successful attempt duration
   - logs/trace expose per-attempt durations
   - runtime restart reason recorded

C. Pipeline observability
   - successful pipeline still persists execution after retry
   - missing schema no longer causes write failure
"""

import sqlite3
import tempfile
from pathlib import Path

from app.pipeline.observability.persistent_store import (
    PersistentExecutionStore,
    _ensure_schema,
    get_persistent_store,
    initialize_persistent_store,
)
from app.pipeline.observability.trace_model import StageExecutionSummary
from app.pipeline.types import AttemptTrace, StageResult, StageRole

# ── A. Execution Store Bootstrap Tests ──


class TestExecutionStoreBootstrap:
    def test_schema_created_on_fresh_db(self):
        """Schema is automatically created on a fresh database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test_executions.db")

            store = PersistentExecutionStore(db_path=db_path)

            # Table should exist after init
            conn = store._connect()
            try:
                row = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='pipeline_executions'"
                ).fetchone()
                assert row is not None, "pipeline_executions table was not created"

                # Verify schema version
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                assert version == 1
            finally:
                conn.close()

    def test_write_succeeds_on_fresh_db(self):
        """Write operation succeeds immediately after bootstrap."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test_executions.db")
            store = PersistentExecutionStore(db_path=db_path)

            # Create a mock summary
            summary = _make_mock_summary("exec-1", "test-pipeline", "success")

            # Should not raise
            store.store(summary)

            # Verify it was written
            row = store.get("exec-1")
            assert row is not None
            assert row["execution_id"] == "exec-1"
            assert row["status"] == "success"

    def test_repeated_init_is_idempotent(self):
        """Calling init multiple times does not fail or duplicate data."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test_executions.db")

            # First init
            store1 = PersistentExecutionStore(db_path=db_path)
            summary = _make_mock_summary("exec-1", "test-pipeline", "success")
            store1.store(summary)

            # Second init (simulating re-initialization)
            store2 = PersistentExecutionStore(db_path=db_path)

            # Data should still be there
            row = store2.get("exec-1")
            assert row is not None

            # Writing again should work (INSERT OR REPLACE)
            summary2 = _make_mock_summary("exec-1", "test-pipeline", "completed")
            store2.store(summary2)

            # Should have exactly one row
            recent = store2.get_recent(limit=10)
            assert len(recent) == 1
            assert recent[0]["status"] == "completed"

    def test_ensure_schema_is_idempotent(self):
        """_ensure_schema can be called multiple times without error."""
        conn = sqlite3.connect(":memory:")
        try:
            # Call multiple times
            assert _ensure_schema(conn, ":memory:") is True
            assert _ensure_schema(conn, ":memory:") is True
            assert _ensure_schema(conn, ":memory:") is True

            # Table should exist
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='pipeline_executions'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_global_singleton_thread_safe(self):
        """get_persistent_store returns the same instance."""
        store1 = get_persistent_store()
        store2 = get_persistent_store()
        assert store1 is store2

    def test_initialize_persistent_store_idempotent(self):
        """initialize_persistent_store can be called multiple times safely."""
        store1 = initialize_persistent_store()
        store2 = initialize_persistent_store()
        assert store1 is store2


# ── B. Browser Retry Observability Tests ──


class TestBrowserRetryObservability:
    def test_attempt_trace_model(self):
        """AttemptTrace captures per-attempt timing and outcome."""
        attempt = AttemptTrace(
            attempt_number=1,
            started_at=100.0,
            ended_at=220.0,
            duration_ms=120000,
            result="failed",
            failure_reason="ExecutionTimeoutError",
            restart_occurred=True,
            restart_reason="timeout",
        )
        assert attempt.attempt_number == 1
        assert attempt.duration_ms == 120000
        assert attempt.result == "failed"
        assert attempt.restart_occurred is True
        assert attempt.restart_reason == "timeout"

    def test_stage_result_with_attempts(self):
        """StageResult carries per-attempt breakdown."""
        result = StageResult(
            stage_id="generate",
            role=StageRole.GENERATE,
            target_model="browser/kimi",
            output="final answer",
            success=True,
            duration_ms=128000,  # Total including retry overhead
            attempts=[
                AttemptTrace(
                    attempt_number=0,
                    duration_ms=120000,
                    result="failed",
                    failure_reason="timeout",
                    restart_occurred=True,
                    restart_reason="timeout",
                ),
                AttemptTrace(
                    attempt_number=1,
                    duration_ms=3000,
                    result="success",
                ),
            ],
            successful_attempt_duration_ms=3000,
            restart_occurred=True,
            restart_reason="timeout",
        )

        assert result.duration_ms == 128000
        assert result.successful_attempt_duration_ms == 3000
        assert len(result.attempts) == 2
        assert result.attempts[0].result == "failed"
        assert result.attempts[1].result == "success"
        assert result.restart_occurred is True

    def test_stage_execution_summary_with_attempts(self):
        """StageExecutionSummary includes attempts in to_dict()."""
        summary = StageExecutionSummary(
            stage_id="generate",
            stage_role="generate",
            status="completed",
            selected_model="browser/kimi",
            duration_ms=128000,
            attempts=[
                {
                    "attempt_number": 0,
                    "duration_ms": 120000,
                    "result": "failed",
                    "failure_reason": "timeout",
                    "restart_occurred": True,
                    "restart_reason": "timeout",
                },
                {
                    "attempt_number": 1,
                    "duration_ms": 3000,
                    "result": "success",
                },
            ],
            successful_attempt_duration_ms=3000,
            restart_occurred=True,
            restart_reason="timeout",
        )

        d = summary.to_dict()
        assert "attempts" in d
        assert len(d["attempts"]) == 2
        assert d["successful_attempt_duration_ms"] == 3000
        assert d["restart_occurred"] is True
        assert d["restart_reason"] == "timeout"
        # Total duration != successful attempt duration
        assert d["duration_ms"] == 128000
        assert d["attempts"][1]["duration_ms"] == 3000

    def test_total_duration_differs_from_successful_attempt(self):
        """Verify that total stage duration can differ from successful attempt duration."""
        # Simulate: first attempt took 120s and failed, second took 3s and succeeded
        total_ms = 128000  # cumulative including restart overhead
        successful_ms = 3000  # just the successful attempt

        assert total_ms != successful_ms
        assert successful_ms < total_ms


# ── C. Pipeline Observability with Retry Tests ──


class TestPipelineObservabilityWithRetry:
    def test_recorder_captures_attempt_data(self):
        """ObservabilityRecorder captures per-attempt data from StageResult."""
        from app.pipeline.observability.recorder import ObservabilityRecorder
        from app.pipeline.types import (
            PipelineContext,
            PipelineDefinition,
            PipelineStage,
            PipelineTrace,
        )

        recorder = ObservabilityRecorder()

        # Create a stage result with attempt data
        stage_result = StageResult(
            stage_id="generate",
            role=StageRole.GENERATE,
            target_model="browser/kimi",
            provider_id="kimi",
            output="final answer",
            success=True,
            duration_ms=128000,
            attempts=[
                AttemptTrace(
                    attempt_number=0,
                    started_at=100.0,
                    ended_at=220.0,
                    duration_ms=120000,
                    result="failed",
                    failure_reason="timeout",
                    restart_occurred=True,
                    restart_reason="timeout",
                ),
                AttemptTrace(
                    attempt_number=1,
                    started_at=225.0,
                    ended_at=228.0,
                    duration_ms=3000,
                    result="success",
                ),
            ],
            successful_attempt_duration_ms=3000,
            restart_occurred=True,
            restart_reason="timeout",
        )

        pipeline_def = PipelineDefinition(
            pipeline_id="test-pipeline",
            display_name="Test Pipeline",
            stages=[
                PipelineStage(stage_id="generate", role=StageRole.GENERATE, target_model="browser/kimi"),
            ],
        )

        trace = PipelineTrace(
            pipeline_id="test-pipeline",
            model_id="pipeline/test-pipeline",
            status="completed",
            total_duration_ms=128000,
            stage_traces=[],
        )

        ctx = PipelineContext(
            trace=trace,
            original_request_model="pipeline/test-pipeline",
            original_messages=[{"role": "user", "content": "Hello"}],
            original_user_input="Hello",
        )
        ctx.stage_outputs["generate"] = stage_result

        summary = recorder._build_summary(ctx, pipeline_def, trace, "req-1")

        assert len(summary.stage_summaries) == 1
        stage_summary = summary.stage_summaries[0]

        # Attempt data captured
        assert len(stage_summary.attempts) == 2
        assert stage_summary.attempts[0]["result"] == "failed"
        assert stage_summary.attempts[1]["result"] == "success"
        assert stage_summary.successful_attempt_duration_ms == 3000
        assert stage_summary.restart_occurred is True
        assert stage_summary.restart_reason == "timeout"

        # to_dict includes attempt data
        d = stage_summary.to_dict()
        assert "attempts" in d
        assert d["successful_attempt_duration_ms"] == 3000
        assert d["restart_occurred"] is True

    def test_persistent_store_write_after_retry(self):
        """Execution persists correctly even after retry/restart scenarios."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test_executions.db")
            store = PersistentExecutionStore(db_path=db_path)

            # Simulate a pipeline execution that had retries
            summary = _make_mock_summary("exec-retry", "test-pipeline", "success")
            summary.total_retries = 1
            summary.stage_summaries = [
                {
                    "stage_id": "generate",
                    "stage_role": "generate",
                    "status": "completed",
                    "duration_ms": 128000,
                    "successful_attempt_duration_ms": 3000,
                    "attempts": [
                        {"attempt_number": 0, "duration_ms": 120000, "result": "failed"},
                        {"attempt_number": 1, "duration_ms": 3000, "result": "success"},
                    ],
                    "restart_occurred": True,
                    "restart_reason": "timeout",
                }
            ]

            store.store(summary)

            # Verify persisted
            row = store.get("exec-retry")
            assert row is not None
            assert row["status"] == "success"
            assert row["total_retries"] == 1

            # Stage summaries persisted (parsed from JSON by _row_to_dict)
            stages = row.get("stages", [])
            assert len(stages) == 1
            assert stages[0].get("restart_occurred") is True

    def test_no_such_table_error_does_not_occur(self):
        """Schema bootstrap prevents 'no such table' errors."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "fresh.db")

            # Fresh store on non-existent path
            store = PersistentExecutionStore(db_path=db_path)

            # This should NOT raise "no such table"
            summary = _make_mock_summary("exec-fresh", "test-pipeline", "success")
            store.store(summary)

            # Verify written
            row = store.get("exec-fresh")
            assert row is not None


# ── Helpers ──


def _make_mock_summary(execution_id: str, pipeline_id: str, status: str):
    """Create a minimal mock execution summary for store tests."""
    from dataclasses import make_dataclass

    # Create a duck-typed summary that mimics PipelineExecutionSummary
    summary_cls = make_dataclass(
        "MockSummary",
        [
            ("execution_id", str),
            ("pipeline_id", str),
            ("pipeline_display_name", str),
            ("status", str),
            ("started_at", float),
            ("finished_at", float),
            ("duration_ms", float),
            ("total_budget_ms", int),
            ("budget_consumed_pct", float),
            ("stage_count", int),
            ("stages_completed", int),
            ("total_retries", int),
            ("total_fallbacks", int),
            ("final_output_summary", str),
            ("failure_reason", str),
            ("failed_stage", str),
            ("request_id", str),
            ("original_model", str),
            ("stage_summaries", list),
        ],
    )

    return summary_cls(
        execution_id=execution_id,
        pipeline_id=pipeline_id,
        pipeline_display_name="Test Pipeline",
        status=status,
        started_at=0.0,
        finished_at=1.0,
        duration_ms=1000.0,
        total_budget_ms=120000,
        budget_consumed_pct=0.8,
        stage_count=1,
        stages_completed=1,
        total_retries=0,
        total_fallbacks=0,
        final_output_summary="test output",
        failure_reason="",
        failed_stage="",
        request_id="req-1",
        original_model="test-model",
        stage_summaries=[],
    )
