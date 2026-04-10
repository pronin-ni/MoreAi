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
    FailurePolicy,
    PipelineContext,
    PipelineDefinition,
    PipelineRegistry,
    PipelineTrace,
    StageResult,
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
                    ctx, stage_def, request, remaining_ms, request_id,
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

            ctx.trace.status = "completed"
            ctx.trace.final_output = final_result.output
            ctx.trace.completed_at = time.monotonic()
            ctx.trace.total_duration_ms = (
                ctx.trace.completed_at - ctx.trace.started_at
            ) * 1000

            logger.info(
                "pipeline_completed",
                request_id=request_id,
                pipeline_id=pipeline_def.pipeline_id,
                total_duration_ms=str(round(ctx.trace.total_duration_ms, 1)),
                trace_id=ctx.trace.trace_id,
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

        except (BadRequestError, GatewayTimeoutError, InternalError):
            raise
        except Exception as exc:
            ctx.trace.status = "failed"
            ctx.trace.error_message = str(exc)
            ctx.trace.completed_at = time.monotonic()
            ctx.trace.total_duration_ms = (
                ctx.trace.completed_at - ctx.trace.started_at
            ) * 1000

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
        """Execute a single stage with retries."""
        stage_timeout_ms = stage_def.timeout_override_ms or remaining_ms
        max_retries = min(stage_def.max_retries, 3)  # bounded retry

        last_error: str | None = None
        last_error_type: str | None = None

        for attempt in range(max_retries + 1):
            trace = self._begin_stage_trace(stage_def, ctx.trace.trace_id)
            trace.retry_count = attempt

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
                result = await self._run_stage(
                    ctx, stage_def, request, stage_timeout_ms, trace, request_id,
                )
                trace.status = "completed"
                trace.duration_ms = result.duration_ms
                trace.completed_at = time.monotonic()
                trace.result_summary = result.output[:200]
                ctx.trace.stage_traces.append(trace)

                logger.info(
                    "stage_completed",
                    request_id=request_id,
                    pipeline_id=ctx.trace.pipeline_id,
                    stage_id=stage_def.stage_id,
                    role=stage_def.role.value,
                    provider=result.provider_id,
                    duration_ms=str(round(result.duration_ms, 1)),
                )

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
                trace.status = "failed"
                trace.error_message = last_error
                trace.completed_at = time.monotonic()

                if attempt == max_retries:
                    trace.duration_ms = (trace.completed_at - trace.started_at) * 1000
                    ctx.trace.stage_traces.append(trace)

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

        If the stage has a selection_policy, uses intelligent model selection
        with smart fallback: on failure, re-ranks candidates and picks next best.
        """
        stage_start = time.monotonic()
        trace.started_at = stage_start
        trace.status = "running"

        # Parse selection policy if intelligent selection is active
        policy = None
        if stage_def.uses_intelligent_selection:
            from app.intelligence.types import SelectionPolicy
            policy = SelectionPolicy(**(stage_def.selection_policy or {}))

        # Build stage prompt and messages (same regardless of model)
        stage_prompt = build_stage_prompt(
            stage_id=stage_def.stage_id,
            role=stage_def.role,
            original_request=ctx.original_user_input,
            input_mapping=stage_def.input_mapping,
            prompt_template=stage_def.prompt_template,
            context=ctx,
        )

        stage_messages = self._build_stage_messages(
            ctx, stage_def, stage_prompt, request,
        )

        # Execute with intelligent selection + fallback loop
        if stage_def.uses_intelligent_selection and policy:
            return await self._run_stage_with_fallback(
                ctx, stage_def, policy, request, stage_messages,
                stage_start, trace, request_id,
            )

        # Fallback to fixed target_model
        target_model = stage_def.target_model
        return await self._execute_stage_model(
            target_model, ctx, stage_def, request, stage_messages,
            stage_start, trace, request_id,
        )

    async def _run_stage_with_fallback(
        self,
        ctx: PipelineContext,
        stage_def,
        policy,
        request: ChatCompletionRequest,
        stage_messages: list[ChatMessage],
        stage_start: float,
        trace: StageTrace,
        request_id: str,
    ) -> StageResult:
        """Execute a stage with intelligent model selection and smart fallback.

        On failure, classifies the failure reason, applies adaptive score
        penalties, and re-ranks candidates before trying the next best.
        """
        from app.intelligence.selection import model_selector

        # Get previous stage model
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

        # Initial selection
        selection = model_selector.select_for_stage(
            stage_id=stage_def.stage_id,
            stage_role=stage_def.role,
            policy=policy,
            previous_stage_model=prev_model,
        )

        ctx.metadata[f"selection_trace:{stage_def.stage_id}"] = selection.to_dict()

        # Build mutable candidate list for fallback
        candidates = list(selection.all_candidates)
        excluded_ids: set[str] = set()
        failure_penalties: dict[str, dict[str, float]] = {}
        fallback_chain: list[dict] = []

        while True:
            # Re-rank candidates with current failure penalties
            if failure_penalties:
                re_ranked = model_selector._rank_candidates(
                    [{"model_id": c.model_id, "provider_id": c.provider_id,
                      "transport": c.transport, "canonical_id": c.canonical_id}
                     for c in candidates],
                    stage_def.role.value,
                    policy,
                    prev_model,
                    failure_penalties=failure_penalties,
                )
                # Update candidates with re-ranked results
                for c in candidates:
                    for r in re_ranked:
                        if r.model_id == c.model_id:
                            c.final_score = r.final_score
                            c.rank = r.rank
                            c.is_excluded = r.is_excluded
                            c.excluded_reason = r.excluded_reason
                            break

                # Re-sort by updated scores
                candidates.sort(key=lambda c: (c.is_excluded, c.final_score), reverse=True)
                # Re-assign ranks
                rank = 1
                for c in candidates:
                    if not c.is_excluded:
                        c.rank = rank
                        rank += 1
                    else:
                        c.rank = -1

            # Pick the best non-excluded candidate
            target_candidate = None
            for c in candidates:
                if c.model_id not in excluded_ids and not c.is_excluded:
                    target_candidate = c
                    break

            if target_candidate is None:
                # No more candidates available
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

            # Execute with this candidate
            result = await self._execute_stage_model(
                target_candidate.model_id, ctx, stage_def, request, stage_messages,
                stage_start, trace, request_id,
            )

            if result.success:
                # Record fallback chain in selection trace
                if fallback_chain:
                    ctx.metadata[f"selection_trace:{stage_def.stage_id}"]["fallback_chain"] = fallback_chain
                    ctx.metadata[f"selection_trace:{stage_def.stage_id}"]["fallback_count"] = len(fallback_chain)

                return result

            # Candidate failed — classify failure and apply adaptive penalty
            excluded_ids.add(target_candidate.model_id)
            failure_reason = self._classify_stage_failure(result)
            penalty = self._compute_failure_penalty(failure_reason)

            # Apply penalty to this candidate
            failure_penalties[target_candidate.model_id] = penalty

            # Record in global short-lived penalty cache
            try:
                from app.pipeline.observability.penalty_cache import global_penalty_cache
                global_penalty_cache.record_failure(
                    target_candidate.model_id,
                    reason=failure_reason,
                    penalty=sum(penalty.values()),
                )
            except Exception:
                pass  # Penalty cache is optional

            # Record in fallback chain
            fallback_chain.append({
                "failed_model": target_candidate.model_id,
                "failed_provider": target_candidate.provider_id,
                "reason": failure_reason,
                "penalty": penalty,
                "error_message": result.error_message,
            })

            logger.warning(
                "stage_candidate_failed_trying_next",
                request_id=request_id,
                stage_id=stage_def.stage_id,
                failed_model=target_candidate.model_id,
                failed_provider=target_candidate.provider_id,
                failure_reason=failure_reason,
                penalty=str(penalty),
                excluded_count=str(len(excluded_ids)),
            )

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
        if "not found" in error_msg or "unknown model" in error_msg or "unknown_model" in error_type:
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

        # Execute through existing routing (ChatProxyService)
        stage_request_id = f"{request_id}:stage:{stage_def.stage_id}"
        response = await chat_proxy_service.process_completion(stage_request, stage_request_id)

        # Extract output text
        output_text = ""
        if response.choices:
            output_text = response.choices[0].message.content

        duration_ms = (time.monotonic() - stage_start) * 1000

        result = StageResult(
            stage_id=stage_def.stage_id,
            role=stage_def.role,
            target_model=target_model,
            provider_id=getattr(response, "_provider", ""),
            output=output_text,
            success=True,
            duration_ms=duration_ms,
        )

        # Store in context for next stages
        ctx.stage_outputs[stage_def.stage_id] = result
        ctx.metadata[f"stage_provider:{stage_def.stage_id}"] = result.provider_id

        return result

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

    def _get_final_result(self, ctx: PipelineContext, pipeline_def: PipelineDefinition) -> StageResult:
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
            ctx, pipeline_def, request_id,
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


def initialize_pipelines() -> None:
    """Bootstrap the pipeline registry with built-in definitions."""
    register_builtin_pipelines(pipeline_registry)
    logger.info(
        "pipelines_initialized",
        pipeline_count=str(len(pipeline_registry.list_all())),
        enabled_count=str(len(pipeline_registry.list_enabled())),
    )
