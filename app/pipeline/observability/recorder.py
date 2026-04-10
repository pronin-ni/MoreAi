"""
Observability recorder.

Converts raw PipelineTrace and execution context into
bounded PipelineExecutionSummary with stage explainability,
budget tracking, and failure analysis.

Integrates with the existing PipelineExecutor to record
enhanced traces after each execution.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.pipeline.observability.store import execution_store
from app.pipeline.observability.trace_model import (
    CandidateExplain,
    FailureAnalysis,
    PipelineExecutionSummary,
    StageExecutionSummary,
    StageSelectionExplain,
)
from app.pipeline.types import PipelineContext, PipelineDefinition, PipelineTrace, StageTrace

logger = get_logger(__name__)

# Summary length limits
_MAX_OUTPUT_SUMMARY = 500
_MAX_INPUT_SUMMARY = 300


class ObservabilityRecorder:
    """Converts raw pipeline execution data into bounded summaries.

    Called by PipelineExecutor after each execution to record
    enhanced traces with explainability and budget tracking.
    """

    def record_from_context(
        self,
        ctx: PipelineContext,
        pipeline_def: PipelineDefinition,
        request_id: str,
    ) -> PipelineExecutionSummary:
        """Build and store an execution summary from the pipeline context.

        This is the main entry point — called after pipeline completion or failure.
        Stores to both in-memory and persistent stores.
        Also records stage-level performance metrics.
        """
        trace = ctx.trace
        summary = self._build_summary(ctx, pipeline_def, trace, request_id)

        # Store in in-memory execution store
        execution_store.store(summary)

        # Store in persistent store
        try:
            from app.pipeline.observability.persistent_store import get_persistent_store
            get_persistent_store().store(summary)
        except Exception as exc:
            logger.debug("persistent_store_failed", execution_id=summary.execution_id, error=str(exc))

        # Record stage-level performance
        self._record_stage_performance(ctx, summary)

        return summary

    def _record_stage_performance(
        self,
        ctx: PipelineContext,
        summary: PipelineExecutionSummary,
    ) -> None:
        """Record per-stage performance metrics for future ranking."""
        try:
            from app.pipeline.observability.stage_perf import RolePerformanceEntry
            from app.pipeline.observability.stage_perf import stage_performance as perf_tracker

            for stage_summary in summary.stage_summaries:
                entry = RolePerformanceEntry(
                    model_id=stage_summary.selected_model or "",
                    provider_id=stage_summary.selected_provider or "",
                    stage_role=stage_summary.stage_role,
                    success=stage_summary.status == "completed",
                    duration_ms=stage_summary.duration_ms,
                    had_fallback=stage_summary.fallback_count > 0,
                    had_retry=stage_summary.retry_count > 0,
                    # Proxy for quality: non-empty output = higher quality
                    output_quality_hint=1.0 if stage_summary.output_summary else 0.0,
                )
                perf_tracker.record(entry)
        except Exception as exc:
            logger.debug("stage_perf_record_failed", error=str(exc))

    def build_failure_analysis(
        self,
        summary: PipelineExecutionSummary,
    ) -> FailureAnalysis:
        """Build a structured failure analysis from an execution summary."""
        analysis = FailureAnalysis(
            execution_id=summary.execution_id,
            pipeline_id=summary.pipeline_id,
            status=summary.status,
            failure_reason=summary.failure_reason,
            total_budget_ms=summary.total_budget_ms,
            time_elapsed_ms=summary.duration_ms,
            budget_exceeded=summary.budget_consumed_pct >= 100,
        )

        # Find the failed stage
        for stage in summary.stage_summaries:
            stage_result: dict[str, Any] = {
                "stage_id": stage.stage_id,
                "role": stage.stage_role,
                "status": stage.status,
                "model": stage.selected_model,
                "duration_ms": round(stage.duration_ms, 1),
            }
            analysis.stage_results.append(stage_result)

            if stage.status == "failed":
                analysis.failed_stage = stage.stage_id
                analysis.failed_stage_role = stage.stage_role
                analysis.failure_reason = stage.failure_reason
                analysis.error_type = stage.error_type
                analysis.retry_count = stage.retry_count
                analysis.fallback_count = stage.fallback_count

                # Classify root cause
                analysis.root_cause = self._classify_root_cause(stage)

        # Check if all stages completed (partial failure)
        completed = sum(1 for s in summary.stage_summaries if s.status == "completed")
        if completed > 0 and summary.status == "failed":
            analysis.root_cause = analysis.root_cause or "stage_failure"
            analysis.root_cause_detail = (
                f"{completed}/{summary.stage_count} stages completed before failure"
            )

        # Check candidate exhaustion
        for stage in summary.stage_summaries:
            if stage.selection_explain and stage.selection_explain.candidates_viable == 0:
                analysis.candidates_exhausted = True
                analysis.root_cause = "no_viable_candidates"
                analysis.root_cause_detail = (
                    f"Stage '{stage.stage_id}': no viable candidates available"
                )

        return analysis

    def _build_summary(
        self,
        ctx: PipelineContext,
        pipeline_def: PipelineDefinition,
        trace: PipelineTrace,
        request_id: str,
    ) -> PipelineExecutionSummary:
        """Build a PipelineExecutionSummary from raw trace data."""
        summary = PipelineExecutionSummary(
            execution_id=trace.trace_id,
            pipeline_id=pipeline_def.pipeline_id,
            pipeline_display_name=pipeline_def.display_name,
            status=self._map_status(trace.status),
            started_at=trace.started_at,
            finished_at=trace.completed_at,
            duration_ms=trace.total_duration_ms,
            total_budget_ms=pipeline_def.max_total_time_ms,
            budget_consumed_pct=(
                (trace.total_duration_ms / pipeline_def.max_total_time_ms * 100)
                if pipeline_def.max_total_time_ms > 0
                else 0
            ),
            stage_count=len(pipeline_def.stages),
            stages_completed=sum(
                1 for st in trace.stage_traces if st.status in ("completed", "skipped")
            ),
            total_retries=sum(st.retry_count for st in trace.stage_traces),
            total_fallbacks=0,  # Computed from selection explainability
            final_output_summary=self._bounded(trace.final_output, _MAX_OUTPUT_SUMMARY),
            failure_reason=trace.error_message or "",
            request_id=request_id,
            original_model=trace.original_request_model,
        )

        # Build stage summaries with explainability
        total_fallbacks = 0
        for i, stage_def in enumerate(pipeline_def.stages):
            st = trace.stage_traces[i] if i < len(trace.stage_traces) else None

            # Get selection explainability from context metadata
            selection_key = f"selection_trace:{stage_def.stage_id}"
            selection_data = ctx.metadata.get(selection_key, {})

            stage_summary = self._build_stage_summary(
                stage_def, st, selection_data, trace, pipeline_def, ctx,
            )

            total_fallbacks += stage_summary.fallback_count
            summary.stage_summaries.append(stage_summary)

        summary.total_fallbacks = total_fallbacks

        # If a stage failed, record which one
        if summary.status == "failed":
            for stage in summary.stage_summaries:
                if stage.status == "failed":
                    summary.failed_stage = stage.stage_id
                    break

        return summary

    def _build_stage_summary(
        self,
        stage_def,
        stage_trace: StageTrace | None,
        selection_data: dict[str, Any],
        pipeline_trace: PipelineTrace,
        pipeline_def: PipelineDefinition,
        ctx: PipelineContext,
    ) -> StageExecutionSummary:
        """Build a StageExecutionSummary with explainability and budget info."""
        # Get stage result from context
        stage_result = ctx.stage_outputs.get(stage_def.stage_id)

        status = "completed"
        failure_reason = ""
        error_type = ""

        if stage_result and not stage_result.success:
            status = "failed"
            failure_reason = stage_result.error_message or ""
            error_type = stage_result.error_type or ""
        elif stage_trace and stage_trace.status == "skipped":
            status = "skipped"
        elif stage_trace and stage_trace.status == "failed":
            status = "failed"
            failure_reason = stage_trace.error_message or ""
            error_type = ""

        # Model/provider info
        selected_model = ""
        selected_provider = ""
        selected_transport = ""

        if stage_result:
            selected_model = stage_result.target_model
            selected_provider = stage_result.provider_id
        elif stage_trace:
            selected_model = stage_trace.target_model
            selected_provider = stage_trace.provider_id

        # Timing
        duration_ms = 0.0
        if stage_result:
            duration_ms = stage_result.duration_ms
        elif stage_trace:
            duration_ms = stage_trace.duration_ms

        # Budget info
        budget_remaining = None
        if stage_trace and pipeline_trace.total_duration_ms > 0:
            elapsed = pipeline_trace.total_duration_ms
            budget_remaining = max(0, pipeline_def.max_total_time_ms - elapsed)

        # Build selection explainability
        selection_explain = self._build_selection_explain(
            stage_def, selection_data, selected_model, selected_provider,
        )

        # Count fallbacks from selection explain
        fallback_count = 0
        if selection_explain:
            fallback_count = selection_explain.fallback_count

        # Input/output summaries (bounded)
        input_summary = ""
        output_summary = ""

        if stage_result:
            output_summary = self._bounded(stage_result.output, _MAX_OUTPUT_SUMMARY)

        # Input summary from context
        prev_output = ctx.get_previous_output(stage_def.stage_id)
        if prev_output:
            input_summary = self._bounded(
                f"Previous stage output: {prev_output}",
                _MAX_INPUT_SUMMARY,
            )
        else:
            input_summary = self._bounded(ctx.original_user_input, _MAX_INPUT_SUMMARY)

        retry_count = stage_result.retry_count if stage_result else 0
        if stage_trace and stage_trace.retry_count > retry_count:
            retry_count = stage_trace.retry_count

        return StageExecutionSummary(
            stage_id=stage_def.stage_id,
            stage_role=stage_def.role.value,
            status=status,
            selected_model=selected_model,
            selected_provider=selected_provider,
            selected_transport=selected_transport,
            selection_explain=selection_explain,
            duration_ms=duration_ms,
            budget_remaining_ms=budget_remaining,
            retry_count=retry_count,
            fallback_count=fallback_count,
            input_summary=input_summary,
            output_summary=output_summary,
            failure_reason=failure_reason,
            error_type=error_type,
            stage_budget_ms=stage_def.timeout_override_ms,
            total_budget_ms=pipeline_def.max_total_time_ms,
        )

    def _build_selection_explain(
        self,
        stage_def,
        selection_data: dict[str, Any],
        selected_model: str,
        selected_provider: str,
    ) -> StageSelectionExplain | None:
        """Build selection explainability from selection trace data."""
        if not selection_data:
            return None

        all_candidates = selection_data.get("all_candidates", [])
        fallback_chain = selection_data.get("fallback_chain", [])

        # Build candidate explains
        candidate_explains: list[CandidateExplain] = []
        excluded_count = 0
        viable_count = 0

        for c in all_candidates[:20]:  # bounded
            is_excluded = c.get("is_excluded", False)
            is_selected = c.get("model_id") == selected_model
            is_fallback = c.get("is_fallback", False)

            if is_excluded:
                excluded_count += 1
            else:
                viable_count += 1

            candidate_explains.append(CandidateExplain(
                model_id=c.get("model_id", ""),
                provider_id=c.get("provider_id", ""),
                transport=c.get("transport", ""),
                rank=c.get("rank", 0),
                score=c.get("final_score", c.get("scores", {}).get("final", 0)),
                excluded=is_excluded,
                exclusion_reason=c.get("excluded_reason", ""),
                selected=is_selected,
                selection_reason=c.get("selected_reason", ""),
                is_fallback=is_fallback,
            ))

        # Determine selection reason
        selection_reason = "highest_ranked"
        for c in candidate_explains:
            if c.selected and c.selection_reason:
                selection_reason = c.selection_reason
                break
        if fallback_chain:
            selection_reason = f"fallback_after_{fallback_chain[-1].get('failed_model', 'unknown')}"

        return StageSelectionExplain(
            stage_id=stage_def.stage_id,
            stage_role=stage_def.role.value,
            policy_summary={
                "preferred_models": selection_data.get("selection_policy", {}).get("preferred_models", []),
                "avoid_tags": selection_data.get("selection_policy", {}).get("avoid_tags", []),
                "min_availability": selection_data.get("selection_policy", {}).get("min_availability", 0),
                "fallback_mode": selection_data.get("selection_policy", {}).get("fallback_mode", "next_best"),
            },
            candidates_considered=len(all_candidates),
            candidates_excluded=excluded_count,
            candidates_viable=viable_count,
            selected_model=selected_model,
            selected_provider=selected_provider,
            selection_reason=selection_reason,
            fallback_count=len(fallback_chain),
            fallback_chain=fallback_chain,
            candidate_details=candidate_explains,
        )

    def _classify_root_cause(self, stage: StageExecutionSummary) -> str:
        """Classify the root cause of a stage failure."""
        if not stage.failure_reason:
            return "unknown"

        reason_lower = stage.failure_reason.lower()
        error_type_lower = stage.error_type.lower()

        # Check for specific failure patterns
        if "timeout" in reason_lower or "gateway_timeout" in error_type_lower:
            return "timeout"
        if "circuit" in reason_lower:
            return "circuit_breaker"
        if "no viable" in reason_lower or "no available" in reason_lower:
            return "no_viable_candidates"
        if "unavailable" in reason_lower:
            return "model_unavailable"
        if "selection" in reason_lower:
            return "selection_failed"

        return "execution_error"

    def _map_status(self, trace_status: str) -> str:
        """Map PipelineTrace status to summary status."""
        mapping = {
            "completed": "success",
            "failed": "failed",
            "pending": "pending",
            "running": "running",
        }
        return mapping.get(trace_status, trace_status)

    def _bounded(self, text: str, max_len: int) -> str:
        """Bound text to max length with ellipsis."""
        if not text:
            return ""
        if len(text) <= max_len:
            return text
        return text[: max_len - 3] + "..."


# Global singleton
observability_recorder = ObservabilityRecorder()
