"""
Pipeline Executor — sequential stage orchestration.

Executes pipeline stages one by one, using existing routing and
ChatProxyService as the execution foundation. Enforces guardrails,
collects traces, and returns the final OpenAI-compatible response.
"""

from __future__ import annotations

import time

from app.core.errors import (
    BadRequestError,
    GatewayTimeoutError,
    InternalError,
    ServiceUnavailableError,
)
from app.core.logging import get_logger
from app.core.metrics import (
    pipeline_duration_ms,
    pipeline_executions_total,
    pipeline_retries_total,
    pipeline_stage_duration_ms,
    pipeline_stage_executions_total,
    pipeline_stage_failures_total,
)
from app.pipeline.builtin_pipelines import register_builtin_pipelines
from app.pipeline.diagnostics import pipeline_diagnostics
from app.pipeline.prompt_builder import build_stage_prompt
from app.pipeline.types import (
    AttemptTrace,
    FailurePolicy,
    PipelineContext,
    PipelineDefinition,
    PipelineRegistry,
    PipelineTrace,
    StageResult,
    StageRole,
    StageTrace,
    pipeline_registry,
)
from app.schemas.openai import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Choice,
    Message,
    Usage,
)
from app.services.chat_proxy_service import service as chat_proxy_service

logger = get_logger(__name__)

# ── Guardrail Constants ──

MAX_STAGES_MVP = 3
MAX_NESTED_DEPTH = 1  # No pipeline-inside-pipeline


class PipelineExecutor:
    """Executes a pipeline definition stage by stage.

    Uses the existing ChatProxyService for each stage — does not bypass
    the current provider/routing architecture.
    """

    def __init__(self, registry: PipelineRegistry | None = None) -> None:
        self._registry = registry or pipeline_registry

    async def execute(
        self,
        pipeline_def: PipelineDefinition,
        request: ChatCompletionRequest,
        request_id: str,
    ) -> ChatCompletionResponse:
        """Execute a pipeline and return the final response.

        Args:
            pipeline_def: The pipeline to execute.
            request: The original user request.
            request_id: Unique request identifier for tracing.

        Returns:
            OpenAI-compatible ChatCompletionResponse with the final stage output.
        """
        # Guardrail: no nested pipelines
        self._validate_no_nested_pipelines(request)

        # Guardrail: stage count
        self._validate_stage_count(pipeline_def)

        # Guardrail: total timeout
        deadline_ms = pipeline_def.max_total_time_ms
        start_ms = time.monotonic() * 1000

        # Build execution context
        ctx = self._build_context(pipeline_def, request, request_id)

        logger.info(
            "pipeline_started",
            request_id=request_id,
            pipeline_id=pipeline_def.pipeline_id,
            stage_count=str(len(pipeline_def.stages)),
            trace_id=ctx.trace.trace_id,
        )

        # Pre-execute hook for special pipelines (e.g., search)
        if pipeline_def.pipeline_id == "search-answer":
            await _search_pipeline_prepare(ctx, request, request_id)

        try:
            # Execute stages sequentially
            for stage_def in pipeline_def.stages:
                elapsed_ms = (time.monotonic() * 1000) - start_ms
                remaining_ms = deadline_ms - elapsed_ms

                # Guardrail: total timeout exceeded
                if remaining_ms <= 0:
                    raise GatewayTimeoutError(
                        f"Pipeline {pipeline_def.pipeline_id} exceeded total time budget "
                        f"({pipeline_def.max_total_time_ms}ms)",
                        details={"pipeline_id": pipeline_def.pipeline_id, "elapsed_ms": elapsed_ms},
                    )

                result = await self._execute_stage(
                    ctx,
                    stage_def,
                    request,
                    remaining_ms,
                    request_id,
                )

                if not result.success:
                    # Handle failure based on policy
                    handled = await self._handle_stage_failure(ctx, stage_def, result, request_id)
                    if not handled:
                        # Fail the whole pipeline
                        ctx.trace.status = "failed"
                        ctx.trace.error_message = result.error_message
                        ctx.trace.completed_at = time.monotonic()
                        ctx.trace.total_duration_ms = (
                            ctx.trace.completed_at - ctx.trace.started_at
                        ) * 1000

                        logger.error(
                            "pipeline_failed",
                            request_id=request_id,
                            pipeline_id=pipeline_def.pipeline_id,
                            failed_stage=stage_def.stage_id,
                            error=result.error_message,
                            trace_id=ctx.trace.trace_id,
                        )

                        raise InternalError(
                            f"Pipeline {pipeline_def.pipeline_id} failed at stage "
                            f"'{stage_def.stage_id}': {result.error_message}",
                            details={
                                "pipeline_id": pipeline_def.pipeline_id,
                                "stage_id": stage_def.stage_id,
                                "error": result.error_message,
                                "trace_id": ctx.trace.trace_id,
                            },
                        )

            # All stages completed successfully
            final_result = self._get_final_result(ctx, pipeline_def)

            # Guard: check for meta-answer patterns, fallback to generate-only output
            final_output = self._validate_final_output(ctx, pipeline_def, final_result)
            if final_output != final_result.output:
                final_result = type(final_result)(output=final_output, success=True)

            ctx.trace.status = "completed"
            ctx.trace.final_output = final_result.output
            ctx.trace.completed_at = time.monotonic()
            ctx.trace.total_duration_ms = (ctx.trace.completed_at - ctx.trace.started_at) * 1000

            # Grounding observability for search pipeline
            grounding_failed = ctx.metadata.get("grounding_failed", False)
            grounding_pattern = ctx.metadata.get("grounding_pattern")
            search_ctx = ctx.metadata.get("search_context", {})

            logger.info(
                "pipeline_completed",
                request_id=request_id,
                pipeline_id=pipeline_def.pipeline_id,
                total_duration_ms=str(round(ctx.trace.total_duration_ms, 1)),
                trace_id=ctx.trace.trace_id,
                grounding_success=not grounding_failed,
                grounding_pattern=grounding_pattern,
                content_pages=search_ctx.get("filtered_pages", 0),
                total_text_length=search_ctx.get("total_text_length", 0),
            )

            # Record in diagnostics and metrics
            pipeline_diagnostics.record(ctx.trace)
            pipeline_executions_total.inc(pipeline_id=pipeline_def.pipeline_id, status="success")
            pipeline_duration_ms.observe(
                ctx.trace.total_duration_ms / 1000,
                pipeline_id=pipeline_def.pipeline_id,
                status="success",
            )

            # Record observability summary
            self._record_observability(ctx, pipeline_def, request_id)

            # Build OpenAI-compatible response
            return self._build_response(ctx, final_result, pipeline_def)

        except BadRequestError, GatewayTimeoutError, InternalError:
            raise
        except Exception as exc:
            ctx.trace.status = "failed"
            ctx.trace.error_message = str(exc)
            ctx.trace.completed_at = time.monotonic()
            ctx.trace.total_duration_ms = (ctx.trace.completed_at - ctx.trace.started_at) * 1000

            logger.exception(
                "pipeline_unexpected_error",
                request_id=request_id,
                pipeline_id=pipeline_def.pipeline_id,
                error=str(exc),
                trace_id=ctx.trace.trace_id,
            )

            raise InternalError(
                f"Pipeline {pipeline_def.pipeline_id} execution error: {exc}",
                details={"pipeline_id": pipeline_def.pipeline_id, "trace_id": ctx.trace.trace_id},
            ) from exc

    async def _execute_stage(
        self,
        ctx: PipelineContext,
        stage_def,
        request: ChatCompletionRequest,
        remaining_ms: float,
        request_id: str,
    ) -> StageResult:
        """Execute a single stage with retries and candidate fallback.

        For stages using intelligent selection:
        - Manages candidate exclusion state across retry iterations
        - Separates retry (same candidate, transient failures) from fallback (next candidate, terminal failures)
        - Only returns final failure after all viable candidates are exhausted

        For stages with fixed target_model:
        - Uses simple retry loop
        """
        stage_timeout_ms = stage_def.timeout_override_ms or remaining_ms
        max_retries = min(stage_def.max_retries, 3)  # bounded retry

        # For intelligent selection stages, manage candidate exclusion state here
        # so it persists across retry iterations
        excluded_ids: set[str] = set()
        failure_penalties: dict[str, dict[str, float]] = {}
        fallback_chain: list[dict] = []
        is_intelligent = stage_def.uses_intelligent_selection

        last_error: str | None = None
        last_error_type: str | None = None

        for attempt in range(max_retries + 1):
            trace = self._begin_stage_trace(stage_def, ctx.trace.trace_id)
            trace.retry_count = attempt

            attempt_start = time.monotonic()

            if attempt > 0:
                trace.status = "retried"
                logger.info(
                    "stage_retry",
                    request_id=request_id,
                    pipeline_id=ctx.trace.pipeline_id,
                    stage_id=stage_def.stage_id,
                    attempt=str(attempt + 1),
                )

            try:
                if is_intelligent:
                    result = await self._run_stage_with_candidate_selection(
                        ctx,
                        stage_def,
                        request,
                        stage_timeout_ms,
                        trace,
                        request_id,
                        excluded_ids,
                        failure_penalties,
                        fallback_chain,
                    )
                else:
                    result = await self._run_stage(
                        ctx,
                        stage_def,
                        request,
                        stage_timeout_ms,
                        trace,
                        request_id,
                    )

                trace.status = "completed"
                trace.duration_ms = result.duration_ms
                trace.completed_at = time.monotonic()
                trace.result_summary = result.output[:200]

                # Record attempt-level data
                attempt_duration_ms = (time.monotonic() - attempt_start) * 1000
                attempt_trace = AttemptTrace(
                    attempt_number=attempt,
                    started_at=attempt_start,
                    ended_at=trace.completed_at,
                    duration_ms=attempt_duration_ms,
                    result="success",
                    failure_reason="",
                    restart_occurred=getattr(result, "restart_occurred", False),
                    restart_reason=getattr(result, "restart_reason", ""),
                )
                trace.attempts.append(attempt_trace)
                if attempt == 0 and not result.restart_occurred:
                    # Single-attempt success
                    trace.successful_attempt_duration_ms = attempt_duration_ms
                else:
                    trace.successful_attempt_duration_ms = attempt_duration_ms
                    trace.restart_occurred = result.restart_occurred
                    trace.restart_reason = result.restart_reason

                ctx.trace.stage_traces.append(trace)

                log_fields: dict[str, str] = {
                    "request_id": request_id,
                    "pipeline_id": ctx.trace.pipeline_id,
                    "stage_id": stage_def.stage_id,
                    "role": stage_def.role.value,
                    "provider": result.provider_id,
                    "total_duration_ms": str(round(result.duration_ms, 1)),
                }
                if result.restart_occurred:
                    log_fields["restart_occurred"] = "true"
                    log_fields["restart_reason"] = result.restart_reason
                if result.successful_attempt_duration_ms > 0 and result.attempts:
                    log_fields["successful_attempt_duration_ms"] = str(
                        round(result.successful_attempt_duration_ms, 1)
                    )
                    log_fields["retry_count"] = str(len(result.attempts) - 1)

                logger.info("stage_completed", **log_fields)

                # Record stage metrics
                pipeline_stage_executions_total.inc(
                    pipeline_id=ctx.trace.pipeline_id,
                    stage_id=stage_def.stage_id,
                    stage_role=stage_def.role.value,
                    target_model=stage_def.target_model,
                    status="success",
                )
                pipeline_stage_duration_ms.observe(
                    result.duration_ms / 1000,
                    pipeline_id=ctx.trace.pipeline_id,
                    stage_id=stage_def.stage_id,
                    stage_role=stage_def.role.value,
                )
                if result.retry_count > 0:
                    pipeline_retries_total.inc(
                        pipeline_id=ctx.trace.pipeline_id,
                        stage_id=stage_def.stage_id,
                        amount=result.retry_count,
                    )
                return result

            except Exception as exc:
                last_error = str(exc)
                last_error_type = type(exc).__name__
                attempt_duration_ms = (time.monotonic() - attempt_start) * 1000
                trace.status = "failed"
                trace.error_message = last_error
                trace.completed_at = time.monotonic()

                # Record failed attempt
                failed_attempt_trace = AttemptTrace(
                    attempt_number=attempt,
                    started_at=attempt_start,
                    ended_at=trace.completed_at,
                    duration_ms=attempt_duration_ms,
                    result="failed",
                    failure_reason=last_error_type,
                )
                trace.attempts.append(failed_attempt_trace)

                if attempt == max_retries:
                    trace.duration_ms = (trace.completed_at - trace.started_at) * 1000
                    ctx.trace.stage_traces.append(trace)

                # For intelligent selection: update excluded set from the internal fallback loop
                # so it persists across retry iterations
                if is_intelligent:
                    sel_trace_data = ctx.metadata.get(f"selection_trace:{stage_def.stage_id}")
                    if sel_trace_data and excluded_ids:
                        logger.info(
                            "stage_retry_iteration",
                            request_id=request_id,
                            stage_id=stage_def.stage_id,
                            attempt=str(attempt + 1),
                            excluded_count=str(len(excluded_ids)),
                            excluded_models=str(excluded_ids),
                        )

        # All retries exhausted
        # Record failure metrics
        pipeline_stage_executions_total.inc(
            pipeline_id=ctx.trace.pipeline_id,
            stage_id=stage_def.stage_id,
            stage_role=stage_def.role.value,
            target_model=stage_def.target_model,
            status="failed",
        )
        pipeline_stage_failures_total.inc(
            pipeline_id=ctx.trace.pipeline_id,
            stage_id=stage_def.stage_id,
            stage_role=stage_def.role.value,
            error_type=last_error_type or "unknown",
        )

        # Record fallback chain in metadata
        if fallback_chain:
            ctx.metadata[f"selection_trace:{stage_def.stage_id}"] = {
                "fallback_chain": fallback_chain,
                "fallback_count": len(fallback_chain),
                "excluded_candidates": list(excluded_ids),
            }

        return StageResult(
            stage_id=stage_def.stage_id,
            role=stage_def.role,
            target_model=stage_def.target_model,
            output="",
            success=False,
            error_message=last_error,
            error_type=last_error_type,
            duration_ms=0,
            retry_count=max_retries,
        )

    async def _run_stage(
        self,
        ctx: PipelineContext,
        stage_def,
        request: ChatCompletionRequest,
        timeout_ms: float,
        trace: StageTrace,
        request_id: str,
    ) -> StageResult:
        """Run a single stage through the existing ChatProxyService.

        Uses the fixed target_model for this stage.
        """
        stage_start = time.monotonic()
        trace.started_at = stage_start
        trace.status = "running"

        # Build stage prompt and messages
        # For search-answer pipeline synthesize stage, use special prompt builder
        if ctx.trace.pipeline_id == "search-answer" and stage_def.stage_id == "synthesize":
            from app.pipeline.prompt_builder import build_search_stage_prompt

            search_results = ctx.metadata.get("search_results", [])
            search_content = ctx.metadata.get("search_content", {})
            search_skipped = ctx.metadata.get("search_skipped", False)

            # Get validation results from search
            search_context_meta = ctx.metadata.get("search_context", {})
            validation_result = search_context_meta.get("validation_result")
            content_pages = search_context_meta.get("filtered_pages", 0)
            total_text_length = search_context_meta.get("total_text_length", 0)

            # NEW: Get chunk context for prompt builder
            chunk_context = ctx.metadata.get("chunk_context")
            chunking_enabled = ctx.metadata.get("chunking_enabled", False)

            logger.info(
                "search_prompt_context",
                content_pages=content_pages,
                total_text_length=total_text_length,
                search_results_count=len(search_results),
                chunking_enabled=chunking_enabled,
                has_chunk_context=bool(chunk_context),
            )

            stage_prompt = build_search_stage_prompt(
                stage_id=stage_def.stage_id,
                original_request=ctx.original_user_input,
                search_results=search_results,
                search_content=search_content,
                search_skipped=search_skipped,
                validation_result=validation_result,
                retry_count=0,
                content_pages=content_pages,
                total_text_length=total_text_length,
                chunk_context=chunk_context,
            )
        else:
            stage_prompt = build_stage_prompt(
                stage_id=stage_def.stage_id,
                role=stage_def.role,
                original_request=ctx.original_user_input,
                input_mapping=stage_def.input_mapping,
                prompt_template=stage_def.prompt_template,
                context=ctx,
            )

        stage_messages = self._build_stage_messages(
            ctx,
            stage_def,
            stage_prompt,
            request,
        )

        target_model = stage_def.target_model
        return await self._execute_stage_model(
            target_model,
            ctx,
            stage_def,
            request,
            stage_messages,
            stage_start,
            trace,
            request_id,
        )

    async def _run_stage_with_candidate_selection(
        self,
        ctx: PipelineContext,
        stage_def,
        request: ChatCompletionRequest,
        timeout_ms: float,
        trace: StageTrace,
        request_id: str,
        excluded_ids: set[str],
        failure_penalties: dict[str, dict[str, float]],
        fallback_chain: list[dict],
    ) -> StageResult:
        """Execute a stage with intelligent candidate selection and smart fallback.

        This method manages its own candidate fallback loop:
        1. Select best non-excluded candidate
        2. Execute with that candidate
        3. On terminal failure: exclude candidate, re-rank, try next
        4. On retryable failure: retry same candidate (if caller retry budget allows)

        The caller (_execute_stage) tracks excluded candidates across retry iterations.

        Args:
            ctx: Pipeline context.
            stage_def: Stage definition.
            request: Original request.
            timeout_ms: Timeout for this stage.
            trace: Stage trace for tracking.
            request_id: Request identifier.
            excluded_ids: Set of already-excluded candidate model IDs (mutated).
            failure_penalties: Failure penalties for re-ranking (mutated).
            fallback_chain: Record of fallback attempts (mutated).

        Returns:
            StageResult on success, raises exception on failure.
        """
        from app.intelligence.selection import model_selector
        from app.intelligence.types import SelectionPolicy

        stage_start = time.monotonic()
        trace.started_at = stage_start
        trace.status = "running"

        # Parse selection policy
        policy = SelectionPolicy(**(stage_def.selection_policy or {}))

        # Get previous stage model for avoidance
        prev_output = ctx.get_previous_output(stage_def.stage_id)
        prev_model = ""
        if prev_output is not None:
            stage_ids = list(ctx.stage_outputs.keys())
            try:
                idx = stage_ids.index(stage_def.stage_id)
                if idx > 0:
                    prev_id = stage_ids[idx - 1]
                    prev_result = ctx.stage_outputs.get(prev_id)
                    if prev_result:
                        prev_model = prev_result.target_model
            except ValueError:
                pass

        # Get all candidates upfront
        initial_selection = model_selector.select_for_stage(
            stage_id=stage_def.stage_id,
            stage_role=stage_def.role,
            policy=policy,
            previous_stage_model=prev_model,
            excluded_ids=excluded_ids if excluded_ids else None,
        )
        candidates = list(initial_selection.all_candidates)

        # Fallback loop: try candidates until success or exhausted
        while True:
            # Re-rank candidates with current failure penalties
            if failure_penalties:
                re_ranked = model_selector._rank_candidates(
                    [
                        {
                            "model_id": c.model_id,
                            "provider_id": c.provider_id,
                            "transport": c.transport,
                            "canonical_id": c.canonical_id,
                        }
                        for c in candidates
                    ],
                    stage_def.role.value,
                    policy,
                    prev_model,
                    failure_penalties=failure_penalties,
                )
                for c in candidates:
                    for r in re_ranked:
                        if r.model_id == c.model_id:
                            c.final_score = r.final_score
                            c.rank = r.rank
                            c.is_excluded = r.is_excluded
                            c.excluded_reason = r.excluded_reason
                            break

                candidates.sort(key=lambda c: (c.is_excluded, c.final_score), reverse=True)
                rank = 1
                for c in candidates:
                    if not c.is_excluded:
                        c.rank = rank
                        rank += 1
                    else:
                        c.rank = -1

            # Apply runtime exclusions from caller
            for c in candidates:
                if c.model_id in excluded_ids:
                    c.is_excluded = True
                    if not c.excluded_reason:
                        c.excluded_reason = "runtime_excluded"

            # Pick the best non-excluded candidate
            target_candidate = None
            for c in candidates:
                if not c.is_excluded:
                    target_candidate = c
                    break

            if target_candidate is None:
                logger.warning(
                    "stage_no_candidates_left",
                    request_id=request_id,
                    stage_id=stage_def.stage_id,
                    excluded=str(excluded_ids),
                )
                raise ServiceUnavailableError(
                    f"No viable candidates for stage '{stage_def.stage_id}' "
                    f"after {' + '.join(excluded_ids)} failed",
                    details={"stage_id": stage_def.stage_id, "excluded": list(excluded_ids)},
                )

            # Check fallback attempts limit
            fallback_count = len(fallback_chain)
            if fallback_count > policy.max_fallback_attempts:
                logger.warning(
                    "stage_fallback_attempts_exhausted",
                    request_id=request_id,
                    stage_id=stage_def.stage_id,
                    attempts=str(fallback_count),
                )
                raise ServiceUnavailableError(
                    f"Stage '{stage_def.stage_id}' exceeded max fallback attempts "
                    f"({policy.max_fallback_attempts})",
                    details={"stage_id": stage_def.stage_id, "attempts": fallback_count},
                )

            # Record selection trace in context metadata (not in StageTrace which doesn't have these fields)
            ctx.metadata[f"selection_trace:{stage_def.stage_id}"] = {
                "selected_model": target_candidate.model_id,
                "selected_provider": target_candidate.provider_id,
                "selected_candidate": target_candidate.to_dict(),
                "all_candidates": [c.to_dict() for c in candidates],
            }

            logger.info(
                "stage_candidate_selected",
                request_id=request_id,
                stage_id=stage_def.stage_id,
                model=target_candidate.model_id,
                provider=target_candidate.provider_id,
                score=str(round(target_candidate.final_score, 3)),
                excluded_count=str(len(excluded_ids)),
            )

            # Build stage prompt and messages
            stage_prompt = build_stage_prompt(
                stage_id=stage_def.stage_id,
                role=stage_def.role,
                original_request=ctx.original_user_input,
                input_mapping=stage_def.input_mapping,
                prompt_template=stage_def.prompt_template,
                context=ctx,
            )

            stage_messages = self._build_stage_messages(
                ctx,
                stage_def,
                stage_prompt,
                request,
            )

            # Execute with the selected candidate
            try:
                # Build forced candidate for routing engine to use
                from app.services.routing_engine import CandidateProvider

                forced_candidate = CandidateProvider(
                    provider_id=target_candidate.provider_id,
                    transport=target_candidate.transport,
                    canonical_model_id=target_candidate.model_id,
                    enabled=True,
                    available=True,
                    visibility="public",
                    is_selected=True,
                    selection_rule="pipeline_selected",
                )

                logger.info(
                    "stage_executing_with_candidate",
                    request_id=request_id,
                    stage_id=stage_def.stage_id,
                    selected_model=target_candidate.model_id,
                    provider_id=target_candidate.provider_id,
                    transport=target_candidate.transport,
                )

                result = await self._execute_stage_model(
                    target_candidate.model_id,
                    ctx,
                    stage_def,
                    request,
                    stage_messages,
                    stage_start,
                    trace,
                    request_id,
                    forced_candidate=forced_candidate,
                )

                # Success
                if fallback_chain:
                    ctx.metadata[f"selection_trace:{stage_def.stage_id}"]["fallback_chain"] = (
                        fallback_chain
                    )
                    ctx.metadata[f"selection_trace:{stage_def.stage_id}"]["fallback_count"] = len(
                        fallback_chain
                    )

                return result

            except Exception as exc:
                # Candidate failed — classify and decide: retry vs exclude
                failure_reason = self._classify_exception(exc)
                is_terminal = self._is_terminal_failure(exc)

                if is_terminal:
                    # Exclude this candidate — won't be tried again
                    excluded_ids.add(target_candidate.model_id)
                    failure_penalties[target_candidate.model_id] = self._compute_failure_penalty(
                        failure_reason
                    )

                    # Record in global penalty cache (optional)
                    try:
                        from app.pipeline.observability.penalty_cache import global_penalty_cache

                        global_penalty_cache.record_failure(
                            target_candidate.model_id,
                            reason=failure_reason,
                            penalty=sum(failure_penalties[target_candidate.model_id].values()),
                        )
                    except Exception:
                        pass

                    fallback_chain.append(
                        {
                            "failed_model": target_candidate.model_id,
                            "failed_provider": target_candidate.provider_id,
                            "reason": failure_reason,
                            "penalty": failure_penalties[target_candidate.model_id],
                            "error_message": str(exc),
                        }
                    )

                    logger.warning(
                        "stage_candidate_failed_trying_next",
                        request_id=request_id,
                        stage_id=stage_def.stage_id,
                        failed_model=target_candidate.model_id,
                        failed_provider=target_candidate.provider_id,
                        failure_reason=failure_reason,
                        excluded_count=str(len(excluded_ids)),
                    )

                    # Continue loop to try next candidate
                    continue
                else:
                    # Retryable failure — re-raise for caller to handle retry
                    logger.info(
                        "stage_retryable_failure",
                        request_id=request_id,
                        stage_id=stage_def.stage_id,
                        model=target_candidate.model_id,
                        reason=failure_reason,
                    )
                    raise

    def _classify_stage_failure(self, result: StageResult) -> str:
        """Classify the reason for a stage failure.

        Returns a string reason code used for adaptive penalty calculation.
        """
        error_type = (result.error_type or "").lower()
        error_msg = (result.error_message or "").lower()

        if "timeout" in error_type or "timeout" in error_msg or "gateway_timeout" in error_type:
            return "timeout"
        if "circuit" in error_msg or "circuit" in error_type:
            return "circuit_breaker"
        if "unavailable" in error_msg or "service_unavailable" in error_type:
            return "service_unavailable"
        if (
            "not found" in error_msg
            or "unknown model" in error_msg
            or "unknown_model" in error_type
        ):
            return "model_not_found"
        if "internal" in error_type or "internal_error" in error_msg:
            return "provider_internal_error"
        return "execution_error"

    def _compute_failure_penalty(self, failure_reason: str) -> dict[str, float]:
        """Compute adaptive score penalty based on failure reason.

        Returns a dict of {penalty_reason: penalty_amount} to be applied
        to the candidate's stage_suitability score during re-ranking.

        Penalties are bounded and proportional to severity:
        - timeout: penalizes latency-heavy models (moderate penalty)
        - circuit_breaker: high penalty (reliability concern)
        - service_unavailable: high penalty (availability concern)
        - model_not_found: very high penalty (configuration issue)
        - provider_internal_error: moderate penalty (transient)
        - execution_error: moderate penalty (generic)
        """
        penalty_map: dict[str, dict[str, float]] = {
            "timeout": {"latency_penalty": 0.15},
            "circuit_breaker": {"reliability_penalty": 0.25},
            "service_unavailable": {"availability_penalty": 0.20},
            "model_not_found": {"configuration_penalty": 0.30},
            "provider_internal_error": {"reliability_penalty": 0.12},
            "execution_error": {"reliability_penalty": 0.10},
        }
        return penalty_map.get(failure_reason, {"reliability_penalty": 0.10})

    def _is_terminal_failure(self, exc: Exception) -> bool:
        """Determine if an exception means the candidate should be excluded.

        Terminal failures mean this candidate cannot succeed and should not be retried:
        - No available providers / service unavailable
        - available=false / excluded by availability filter
        - Missing auth / browser unavailable
        - Circuit breaker open
        - Model not found / misconfigured

        Retryable failures may succeed on retry:
        - Transient timeout
        - 429 / rate limit (if policy allows)
        - Temporary provider error
        """
        error_type = type(exc).__name__.lower()
        error_msg = str(exc).lower()

        # Terminal: service/model unavailability
        terminal_indicators = [
            "no available",
            "no viable",
            "service_unavailable",
            "unavailable",
            "available=false",
            "exceeded max fallback",
            "circuit_breaker_open",
            "not found",
            "unknown model",
            "missing auth",
            "browser unavailable",
            "no candidates",
        ]

        for indicator in terminal_indicators:
            if indicator in error_msg or indicator in error_type:
                return True

        # Check for ServiceUnavailableError specifically
        if isinstance(exc, ServiceUnavailableError):
            return True

        # Timeout is potentially retryable (not terminal by default)
        return not (
            "timeout" in error_msg or "gateway_timeout" in error_type or "timed out" in error_msg
        )

    def _classify_exception(self, exc: Exception) -> str:
        """Classify an exception into a reason code for logging/analytics."""
        error_type = type(exc).__name__.lower()
        error_msg = str(exc).lower()

        if "timeout" in error_msg or "timeout" in error_type or "gateway_timeout" in error_type:
            return "timeout"
        if "circuit" in error_msg or "circuit" in error_type:
            return "circuit_breaker"
        if (
            "no available" in error_msg
            or "no viable" in error_msg
            or "unavailable" in error_msg
            or "service_unavailable" in error_type
        ):
            return "service_unavailable"
        if "not found" in error_msg or "unknown model" in error_msg:
            return "model_not_found"
        if "auth" in error_msg or "login" in error_msg or "credential" in error_msg:
            return "auth_unavailable"
        if "rate" in error_msg or "429" in error_msg:
            return "rate_limited"
        if "internal" in error_type or "internal_error" in error_msg:
            return "provider_internal_error"
        return "execution_error"

    async def _execute_stage_model(
        self,
        target_model: str,
        ctx: PipelineContext,
        stage_def,
        request: ChatCompletionRequest,
        stage_messages: list[ChatMessage],
        stage_start: float,
        trace: StageTrace,
        request_id: str,
        forced_candidate=None,
    ) -> StageResult:
        """Execute a stage with a specific model through ChatProxyService."""
        # Create stage-specific request
        stage_request = ChatCompletionRequest(
            model=target_model,
            messages=stage_messages,
            temperature=request.temperature,
            top_p=request.top_p,
            max_tokens=request.max_tokens,
            stop=request.stop,
        )

        # Execute through ChatProxyService with forced candidate if provided
        stage_request_id = f"{request_id}:stage:{stage_def.stage_id}"
        response = await chat_proxy_service.process_completion(
            stage_request, stage_request_id, forced_candidate=forced_candidate
        )

        # Extract output text
        output_text = ""
        if response.choices:
            output_text = response.choices[0].message.content

        duration_ms = (time.monotonic() - stage_start) * 1000

        # Extract browser-level attempt data if present
        attempts: list[AttemptTrace] = []
        successful_attempt_duration_ms = 0.0
        restart_occurred = False
        restart_reason = ""

        if hasattr(response, "_browser_attempts") and response._browser_attempts:
            for a in response._browser_attempts:
                attempts.append(
                    AttemptTrace(
                        attempt_number=a.get("attempt_number", 0),
                        started_at=a.get("started_at", 0),
                        ended_at=a.get("ended_at", 0),
                        duration_ms=a.get("duration_ms", 0),
                        result=a.get("result", "unknown"),
                        failure_reason=a.get("failure_reason", ""),
                        restart_occurred=a.get("restart_occurred", False),
                        restart_reason=a.get("restart_reason", ""),
                    )
                )
            successful_attempt_duration_ms = attempts[-1].duration_ms if attempts else duration_ms
            restart_occurred = getattr(response, "_browser_restart_occurred", False)
            restart_reason = getattr(response, "_browser_restart_reason", "")

        result = StageResult(
            stage_id=stage_def.stage_id,
            role=stage_def.role,
            target_model=target_model,
            provider_id=getattr(response, "_provider", ""),
            output=output_text,
            success=True,
            duration_ms=duration_ms,
            attempts=attempts,
            successful_attempt_duration_ms=successful_attempt_duration_ms or duration_ms,
            restart_occurred=restart_occurred,
            restart_reason=restart_reason,
        )

        # Store in context for next stages
        ctx.stage_outputs[stage_def.stage_id] = result
        ctx.metadata[f"stage_provider:{stage_def.stage_id}"] = result.provider_id

        # Grounding failure detection for search-answer pipeline
        if ctx.trace.pipeline_id == "search-answer" and result.success:
            grounding_check = self._check_grounding_failure(
                result.output,
                ctx.metadata,
            )

            context_used = self._check_context_was_used(
                result.output,
                ctx.metadata,
            )

            keyword_ratio = grounding_check.get(
                "keyword_ratio", grounding_check.get("grounding_ratio", 0.0)
            )
            entity_ratio = grounding_check.get("entity_ratio", 0.0)

            answer_relevance = self._check_answer_relevance(
                result.output, ctx.original_user_input, ctx.metadata
            )

            query_coverage = answer_relevance.get("query_coverage", 0.0)
            chunking_stats = ctx.metadata.get("search_context", {}).get("chunking_stats", {})
            avg_chunk_score = chunking_stats.get("average_chunk_score", 0.5)

            from app.search.chunker import compute_confidence_score

            confidence = compute_confidence_score(
                keyword_ratio,
                query_coverage,
                avg_chunk_score,
            )

            early_exit_threshold = 0.75
            should_retry = (
                grounding_check["failed"] or not context_used or answer_relevance["failed"]
            )

            early_exit = not should_retry and confidence > early_exit_threshold

            if should_retry:
                reason = (
                    grounding_check["pattern"]
                    if grounding_check["failed"]
                    else (
                        "no_context_usage"
                        if not context_used
                        else answer_relevance.get("pattern", "unknown")
                    )
                )
                logger.warning(
                    "grounding_failure_detected",
                    model=result.model_id,
                    provider=result.provider_id,
                    pattern_matched=reason,
                    context_used=context_used,
                    keyword_ratio=keyword_ratio,
                    entity_ratio=entity_ratio,
                    answer_relevant=not answer_relevance.get("failed", False),
                    query_coverage=query_coverage,
                    confidence_score=confidence,
                    retry_reason=reason,
                    content_pages=ctx.metadata.get("search_context", {}).get("filtered_pages", 0),
                    chunking_enabled=ctx.metadata.get("chunking_enabled", False),
                )
                ctx.metadata["grounding_failed"] = True
                ctx.metadata["grounding_pattern"] = reason

                try:
                    from app.pipeline.observability.penalty_cache import global_penalty_cache

                    global_penalty_cache.record_failure(
                        model_id=result.model_id,
                        provider_id=result.provider_id,
                        failure_type="grounding_failure",
                        penalty={"grounding_penalty": 0.15},
                    )
                except Exception:
                    pass
            else:
                if early_exit:
                    logger.info(
                        "early_exit_high_confidence",
                        model=result.model_id,
                        provider=result.provider_id,
                        confidence_score=confidence,
                        keyword_ratio=keyword_ratio,
                        query_coverage=query_coverage,
                    )
                logger.info(
                    "grounding_success",
                    model=result.model_id,
                    provider=result.provider_id,
                    keyword_ratio=keyword_ratio,
                    entity_ratio=entity_ratio,
                    context_used=context_used,
                    confidence_score=confidence,
                    early_exit=early_exit,
                )

        return result

    def _check_grounding_failure(self, output: str, metadata: dict) -> dict:
        """Check if model failed to ground - used generic response instead of context.

        Enhanced to use chunk-specific validation with keyword + entity overlap.
        """
        from app.pipeline.prompt_builder import GROUNDING_FAILURE_PATTERNS
        from app.search.chunker import validate_chunk_grounding

        selected_chunks_data = metadata.get("selected_chunks_data", [])
        if selected_chunks_data:
            chunk_validation = validate_chunk_grounding(output, selected_chunks_data)
            if chunk_validation["failed"]:
                return {
                    "failed": True,
                    "pattern": chunk_validation["pattern"],
                    "keyword_ratio": chunk_validation.get("keyword_ratio", 0.0),
                    "entity_ratio": chunk_validation.get("entity_ratio", 0.0),
                }

        search_context = metadata.get("search_context", {})
        content_pages = search_context.get("filtered_pages", 0)
        total_text = search_context.get("total_text_length", 0)

        if content_pages < 2 or total_text < 1000:
            return {"failed": False, "pattern": None}

        output_lower = output.lower()
        for pattern in GROUNDING_FAILURE_PATTERNS:
            if pattern in output_lower:
                return {"failed": True, "pattern": pattern}

        return {"failed": False, "pattern": None}

    def _check_answer_relevance(self, output: str, query: str, metadata: dict) -> dict:
        """Check if answer is relevant to the original query.

        Validates answer contains key query terms and is not too short/generic.
        """
        from app.search.chunker import validate_answer_relevance

        if not output or not query:
            return {"failed": False, "pattern": None}

        result = validate_answer_relevance(output, query)

        if result["failed"]:
            logger.warning(
                "answer_relevance_failed",
                pattern=result.get("pattern"),
                query_coverage=result.get("query_coverage", 0),
                answer_length=len(output),
            )

        return result

    def _check_context_was_used(self, output: str, metadata: dict) -> bool:
        """Check if output actually uses provided context - checks for source references."""
        # We provided sources [1], [2], [3] in the prompt
        # If output contains these references, context was likely used
        output_lower = output.lower()

        # Check for common citation patterns
        citation_patterns = [
            "[1]",
            "[2]",
            "[3]",
            "source:",
            "according to",
            "as mentioned",
            "the article",
            "the page",
        ]

        has_citation = any(pattern in output_lower for pattern in citation_patterns)

        # Also check if output mentions any topic from the sources
        search_context = metadata.get("search_context", {})
        filtered_sources = search_context.get("filtered_sources", [])

        if filtered_sources and not has_citation:
            # If no citations and no references, likely didn't use context
            # But allow short answers (< 50 chars) to pass - might be partial
            if len(output) < 50:
                return True  # Short answer - could be valid
            return False

        return has_citation or len(output) > 50

    def _build_stage_messages(
        self,
        ctx: PipelineContext,
        stage_def,
        stage_prompt: str,
        original_request: ChatCompletionRequest,
    ) -> list[ChatMessage]:
        """Build the messages list for a stage request.

        For the first stage, pass through the original messages.
        For later stages, build messages from the prompt template output.
        """
        # Check if this is the first stage
        is_first_stage = len(ctx.stage_outputs) == 0

        if is_first_stage:
            # First stage: use original user messages
            return original_request.messages

        # Later stages: create a new user message from the built prompt
        return [ChatMessage(role="user", content=stage_prompt)]

    def _get_final_result(
        self, ctx: PipelineContext, pipeline_def: PipelineDefinition
    ) -> StageResult:
        """Get the final stage result — last successful stage output."""
        # Walk stages in order, return the last successful one
        last_success: StageResult | None = None
        for stage_def in pipeline_def.stages:
            result = ctx.stage_outputs.get(stage_def.stage_id)
            if result and result.success:
                last_success = result

        if last_success is None:
            raise InternalError(
                f"Pipeline {pipeline_def.pipeline_id} has no successful stages",
                details={"pipeline_id": pipeline_def.pipeline_id},
            )

        return last_success

    def _validate_final_output(
        self, ctx: PipelineContext, pipeline_def: PipelineDefinition, final_result: StageResult
    ) -> str:
        """Validate final output - fallback if meta-answer detected."""
        output = final_result.output
        if not output:
            return output

        # Check for meta-answer patterns
        lower_output = output.lower()
        meta_patterns = [
            "cannot evaluate",
            "no answer",
            "unable to determine",
            "cannot provide an answer",
            "i cannot determine",
            "can't evaluate",
            "no relevant information",
            "not enough information",
            "insufficient information",
            "i don't have enough",
            "without more information",
        ]

        is_meta = any(pattern in lower_output for pattern in meta_patterns)
        if not is_meta:
            return output

        # For search-answer pipeline: if refine stage has meta output, fallback to synthesize
        if pipeline_def.pipeline_id == "search-answer":
            synthesize_result = ctx.stage_outputs.get("synthesize")
            if synthesize_result and synthesize_result.success and synthesize_result.output:
                logger.info(
                    "pipeline_output_fallback",
                    pipeline_id=pipeline_def.pipeline_id,
                    reason="meta_answer_in_refine",
                    fallback_stage="synthesize",
                )
                return synthesize_result.output

        # Fallback to generate stage output if available
        for stage_def in pipeline_def.stages:
            if stage_def.role == StageRole.GENERATE:
                gen_result = ctx.stage_outputs.get(stage_def.stage_id)
                if gen_result and gen_result.success and gen_result.output:
                    logger.info(
                        "pipeline_output_fallback",
                        pipeline_id=pipeline_def.pipeline_id,
                        reason="meta_answer_detected",
                        original_length=len(output),
                        fallback_stage=stage_def.stage_id,
                    )
                    return gen_result.output

        return output

    def _build_response(
        self,
        ctx: PipelineContext,
        final_result: StageResult,
        pipeline_def: PipelineDefinition,
    ) -> ChatCompletionResponse:
        """Build an OpenAI-compatible ChatCompletionResponse from pipeline output."""
        import time as _time

        response_id = f"pipeline-{ctx.trace.trace_id}"
        created = int(_time.time())

        return ChatCompletionResponse(
            id=response_id,
            created=created,
            model=pipeline_def.model_id,
            choices=[
                Choice(
                    index=0,
                    message=Message(role="assistant", content=final_result.output),
                    finish_reason="stop",
                ),
            ],
            usage=Usage(),
        )

    def _build_context(
        self,
        pipeline_def: PipelineDefinition,
        request: ChatCompletionRequest,
        request_id: str,
    ) -> PipelineContext:
        """Build the initial execution context."""
        # Extract user input (last user message)
        user_input = ""
        for msg in reversed(request.messages):
            if msg.role == "user":
                if isinstance(msg.content, str):
                    user_input = msg.content
                else:
                    # List of content parts — extract text
                    for part in msg.content:
                        if hasattr(part, "text") and part.text:
                            user_input = part.text
                            break
                if user_input:
                    break

        # Serialize messages for context
        serialized_messages = []
        for msg in request.messages:
            if isinstance(msg.content, str):
                serialized_messages.append({"role": msg.role, "content": msg.content})
            else:
                # Complex content — simplify to text
                texts = []
                for part in msg.content:
                    if hasattr(part, "text") and part.text:
                        texts.append(part.text)
                serialized_messages.append({"role": msg.role, "content": "\n".join(texts)})

        trace = PipelineTrace(
            pipeline_id=pipeline_def.pipeline_id,
            model_id=pipeline_def.model_id,
            status="running",
            request_id=request_id,
            original_request_model=request.model,
        )

        return PipelineContext(
            trace=trace,
            original_request_model=request.model,
            original_messages=serialized_messages,
            original_user_input=user_input,
        )

    async def _handle_stage_failure(
        self,
        ctx: PipelineContext,
        stage_def,
        result: StageResult,
        request_id: str,
    ) -> bool:
        """Handle a stage failure based on its failure policy.

        Returns True if the pipeline should continue, False if it should fail.
        """
        policy = stage_def.failure_policy

        logger.error(
            "stage_failed",
            request_id=request_id,
            pipeline_id=ctx.trace.pipeline_id,
            stage_id=stage_def.stage_id,
            policy=policy.value,
            error=result.error_message,
        )

        if policy == FailurePolicy.FAIL_ALL:
            return False

        if policy == FailurePolicy.SKIP:
            logger.warning(
                "stage_skipped",
                request_id=request_id,
                pipeline_id=ctx.trace.pipeline_id,
                stage_id=stage_def.stage_id,
                error=result.error_message,
            )
            # Mark as skipped in context
            ctx.stage_outputs[stage_def.stage_id] = StageResult(
                stage_id=stage_def.stage_id,
                role=stage_def.role,
                target_model=stage_def.target_model,
                output="",
                success=False,
                error_message=result.error_message,
                error_type=result.error_type,
            )
            return True

        if policy == FailurePolicy.FALLBACK:
            # For MVP, treat fallback as skip — no complex fallback routing yet
            logger.warning(
                "stage_fallback_not_implemented_continuing",
                request_id=request_id,
                pipeline_id=ctx.trace.pipeline_id,
                stage_id=stage_def.stage_id,
            )
            ctx.stage_outputs[stage_def.stage_id] = StageResult(
                stage_id=stage_def.stage_id,
                role=stage_def.role,
                target_model=stage_def.target_model,
                output="",
                success=False,
                error_message=result.error_message,
                error_type=result.error_type,
            )
            return True

        return False

    def _validate_no_nested_pipelines(self, request: ChatCompletionRequest) -> None:
        """Guardrail: reject requests that try to nest pipelines."""
        if request.model.startswith("pipeline/"):
            # Check if any message mentions another pipeline model
            for msg in request.messages:
                content = msg.content if isinstance(msg.content, str) else ""
                if "pipeline/" in content.lower():
                    raise BadRequestError(
                        "Nested pipeline requests are not supported",
                        details={"reason": "no_recursive_pipelines"},
                    )

    def _validate_stage_count(self, pipeline_def: PipelineDefinition) -> None:
        """Guardrail: enforce max 3 stages for MVP."""
        if len(pipeline_def.stages) > MAX_STAGES_MVP:
            raise BadRequestError(
                f"Pipeline '{pipeline_def.pipeline_id}' has {len(pipeline_def.stages)} stages, "
                f"exceeding MVP limit of {MAX_STAGES_MVP}",
                details={
                    "pipeline_id": pipeline_def.pipeline_id,
                    "stage_count": len(pipeline_def.stages),
                    "max_allowed": MAX_STAGES_MVP,
                },
            )

    def _begin_stage_trace(self, stage_def, parent_trace_id: str) -> StageTrace:
        """Create a new StageTrace for tracking."""
        return StageTrace(
            stage_id=stage_def.stage_id,
            role=stage_def.role.value,
            target_model=stage_def.target_model,
        )

    def _record_observability(
        self,
        ctx: PipelineContext,
        pipeline_def: PipelineDefinition,
        request_id: str,
    ) -> None:
        """Record observability summary and metrics."""
        from app.core.metrics import (
            pipeline_partial_total,
            pipeline_rank_fallback_reason_total,
            pipeline_stage_fallback_total,
        )
        from app.pipeline.observability.recorder import observability_recorder

        # Build and store execution summary
        summary = observability_recorder.record_from_context(
            ctx,
            pipeline_def,
            request_id,
        )

        # Record partial success metric
        completed = sum(1 for s in summary.stage_summaries if s.status == "completed")
        if 0 < completed < summary.stage_count:
            pipeline_partial_total.inc(pipeline_id=pipeline_def.pipeline_id)

        # Record fallback metrics
        for stage in summary.stage_summaries:
            if stage.fallback_count > 0 and stage.selection_explain:
                for fb in stage.selection_explain.fallback_chain:
                    pipeline_stage_fallback_total.inc(
                        pipeline_id=pipeline_def.pipeline_id,
                        stage_id=stage.stage_id,
                        from_model=fb.get("failed_model", ""),
                        to_model=fb.get("fallback_to", ""),
                    )
                    pipeline_rank_fallback_reason_total.inc(
                        pipeline_id=pipeline_def.pipeline_id,
                        stage_id=stage.stage_id,
                        reason=fb.get("reason", "unknown"),
                    )

        # Log observability summary
        logger.info(
            "pipeline_observability_recorded",
            request_id=request_id,
            pipeline_id=pipeline_def.pipeline_id,
            execution_id=summary.execution_id,
            status=summary.status,
            duration_ms=str(round(summary.duration_ms, 1)),
            stages_completed=f"{summary.stages_completed}/{summary.stage_count}",
            fallbacks=str(summary.total_fallbacks),
        )


# ── Singleton ──

pipeline_executor = PipelineExecutor()


async def _search_pipeline_prepare(
    ctx: PipelineContext, request: ChatCompletionRequest, request_id: str
) -> None:
    """Execute search before the search-answer pipeline stages.

    This runs BEFORE any pipeline stages. It performs the web search
    and stores results in the pipeline context metadata.
    """
    from app.core.config import settings
    from app.search.service import search_service

    if not settings.search.enabled:
        logger.warning("search_disabled_falling_back_to_llm")
        ctx.metadata["search_skipped"] = True
        ctx.metadata["search_error"] = "Search is disabled"
        return

    # Extract user query
    user_query = ctx.original_user_input
    if not user_query:
        ctx.metadata["search_skipped"] = True
        return

    logger.info("search_pipeline_starting", request_id=request_id, query=user_query[:100])

    try:
        # Perform search with content fetching
        search_context = await search_service.search(
            query=user_query,
            fetch_content=True,
        )

        # Store validation results for pipeline
        ctx.metadata["search_context"] = {
            "original_query": search_context.original_query,
            "expanded_queries": search_context.expanded_queries,
            "result_count": len(search_context.search_results),
            "content_pages": len(search_context.fetched_contents),
            "filtered_pages": len(search_context.filtered_contents),
            "total_text_length": search_context.total_text_length,
            "keywords_found": search_context.keywords_found,
            "validation_result": search_context.validation_result,
            "sources": list(search_context.fetched_contents.keys()),
            "filtered_sources": [p.url for p in search_context.filtered_contents],
            "filtering_stats": search_context.filtering_stats,
            "error": search_context.error,
        }

        # Store FILTERED content for prompt building (quality-controlled)
        if search_context.filtered_contents:
            ctx.metadata["search_content"] = {
                p.url: p.content for p in search_context.filtered_contents
            }
        else:
            # Fallback to raw content if filtering failed
            ctx.metadata["search_content"] = search_context.fetched_contents

        ctx.metadata["search_results"] = [
            {"title": r.title, "url": r.url, "snippet": r.snippet, "source": r.source}
            for r in search_context.search_results
        ]

        # Store validation for prompt builder
        ctx.metadata["search_validation"] = search_context.validation_result

        # FAIL-FAST: If insufficient search context, skip synthesis
        content_pages = len(search_context.fetched_contents)
        total_text = search_context.total_text_length
        context_valid = content_pages >= 2 and total_text >= 1500

        if not context_valid:
            ctx.metadata["search_skipped"] = True
            ctx.metadata["search_error"] = "insufficient_search_context"
            ctx.metadata["chunking_fallback"] = True
            ctx.metadata["fallback_reason"] = "insufficient_search_context"
            ctx.metadata["fallback_response"] = (
                "Не удалось получить достаточную информацию из поиска. Попробуйте позже."
            )
            logger.warning(
                "search_context_insufficient",
                pages=content_pages,
                text_length=total_text,
                context_valid=False,
            )
            return

        # NEW: Chunk-based relevance retrieval with quality control
        from app.search.chunker import MAX_CHUNK_CONTEXT_CHARS, process_chunks

        if search_context.filtered_contents:
            chunk_context, chunk_metadata, chunking_stats = process_chunks(
                query=user_query,
                pages=search_context.filtered_contents,
                top_k=5,
                max_per_url=2,
                max_context_chars=MAX_CHUNK_CONTEXT_CHARS,
            )

            ctx.metadata["search_context"]["chunking_stats"] = {
                "total_chunks_created": chunking_stats.total_chunks_created,
                "chunks_selected_top_k": chunking_stats.chunks_selected_top_k,
                "average_chunk_score": chunking_stats.average_chunk_score,
                "avg_keyword_overlap": chunking_stats.avg_keyword_overlap,
                "dropped_chunks_count": chunking_stats.dropped_chunks_count,
                "fallback_used": chunking_stats.fallback_used,
                "fallback_reason": chunking_stats.fallback_reason,
                "quality_zone": chunking_stats.quality_zone,
                "cross_source_boost_applied": chunking_stats.cross_source_boost_applied,
                "dedup_chunks_removed": chunking_stats.dedup_chunks_removed,
                "total_context_chars": chunking_stats.total_context_chars,
            }

            if chunk_context and not chunking_stats.fallback_used:
                ctx.metadata["chunk_context"] = chunk_context
                ctx.metadata["selected_chunks"] = chunk_metadata
                ctx.metadata["selected_chunks_data"] = [
                    {"chunk_id": c["chunk_id"], "url": c["url"], "chunk_text": ""}
                    for c in chunk_metadata
                ]
                ctx.metadata["chunking_enabled"] = True
                logger.info(
                    "chunking_success",
                    total_pages=chunking_stats.total_pages,
                    total_chunks=chunking_stats.total_chunks_created,
                    selected=chunking_stats.chunks_selected_top_k,
                    avg_score=chunking_stats.average_chunk_score,
                    avg_keyword_overlap=chunking_stats.avg_keyword_overlap,
                    quality_zone=chunking_stats.quality_zone,
                    context_chars=len(chunk_context),
                    cross_source_boost=chunking_stats.cross_source_boost_applied,
                )
            else:
                ctx.metadata["chunking_enabled"] = False
                ctx.metadata["chunking_fallback"] = True
                ctx.metadata["fallback_reason"] = chunking_stats.fallback_reason
                logger.warning(
                    "chunking_fallback_used",
                    reason=chunking_stats.fallback_reason,
                    quality_zone=chunking_stats.quality_zone,
                    total_chunks=chunking_stats.total_chunks_created,
                )
        else:
            ctx.metadata["chunking_enabled"] = False

        # Log comprehensive results
        logger.info(
            "search_pipeline_complete",
            request_id=request_id,
            query=user_query[:50],
            results=len(search_context.search_results),
            fetched_pages_count=content_pages,
            total_text_length=total_text,
            context_valid=context_valid,
            validation_result=search_context.validation_result,
            filtering_stats=search_context.filtering_stats,
            error=search_context.error,
        )

    except Exception as e:
        logger.error("search_pipeline_failed", request_id=request_id, error=str(e))
        # Don't fail the pipeline - just skip search
        ctx.metadata["search_skipped"] = True
        ctx.metadata["search_error"] = str(e)


class PipelineExecutor:
    """Bootstrap the pipeline registry with built-in definitions."""

    register_builtin_pipelines(pipeline_registry)
    logger.info(
        "pipelines_initialized",
        pipeline_count=str(len(pipeline_registry.list_all())),
        enabled_count=str(len(pipeline_registry.list_enabled())),
    )


def initialize_pipelines() -> None:
    """Initialize pipeline subsystem - called from main.py lifespan."""
    # Pipelines are already initialized at module load time
    # This function exists for backward compatibility and explicit initialization
    logger.info("pipelines_subsystem_ready")
