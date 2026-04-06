"""
Chat Proxy Service — routes requests through the centralized routing engine.

Uses RoutingEngine to build a provider chain, then executes it with
retry/fallback policy separation.
"""

from dataclasses import replace

from app.agents.completion_service import agent_completion_service
from app.core.diagnostics import record_failure, record_routing_decision
from app.core.logging import get_logger
from app.core.errors import InternalError, ServiceUnavailableError
from app.core.metrics import (
    browser_execution_seconds,
    browser_active_workers,
    errors_total,
    fallback_total,
    fallback_success,
    requests_total,
    routing_decision_total,
    circuit_breaker_state,
    queue_wait_seconds,
    request_latency,
)
from app.core.tracing import trace_span
from app.registry.unified import unified_registry
from app.schemas.openai import ChatCompletionRequest, ChatCompletionResponse
from app.services.api_completion_service import api_completion_service
from app.services.browser_completion_service import browser_completion_service
from app.services.routing_engine import routing_engine

logger = get_logger(__name__)


class ChatProxyService:
    async def process_completion(
        self,
        request: ChatCompletionRequest,
        request_id: str,
    ) -> ChatCompletionResponse:
        async with trace_span("chat_completion", model=request.model, request_id=request_id) as span:
            span.add_event("request_received", message_count=str(len(request.messages)))

            # Step 1: Build routing plan
            plan = routing_engine.plan(request.model)

            # Record routing decision metrics
            rule = plan.candidates[0].selection_rule if plan.candidates else "none"
            primary = plan.primary_provider

            routing_decision_total.inc(
                model=request.model,
                selected_provider=primary.provider_id if primary else "none",
                routing_rule=rule,
            )

            record_routing_decision(
                model=request.model,
                selected_provider=primary.provider_id if primary else "none",
                transport=primary.transport if primary else "none",
                routing_rule=rule,
                fallbacks_tried=[],
                candidates_rejected=[
                    {"provider_id": c.provider_id, "transport": c.transport, "reason": c.reason}
                    for c in plan.all_candidates
                    if not c.is_selected
                ],
            )

            span.set_attribute("routing_rule", rule)
            span.set_attribute("candidate_count", str(len(plan.candidates)))
            span.add_event("routing_plan_built", chain=" → ".join(
                f"{c.transport}/{c.provider_id}" for c in plan.candidates
            ))

            # Record circuit breaker states
            self._record_circuit_breaker_states()

            # Record worker count
            self._record_worker_count()

            # Step 2: Execute the provider chain
            if not plan.candidates:
                span.set_status("error", "no available providers")
                raise ServiceUnavailableError(
                    f"No available providers for model {request.model}",
                    details={"model": request.model, "decision": plan.decision_trace},
                )

            last_error = None

            for attempt_idx, candidate in enumerate(plan.candidates):
                # Retry loop for this provider
                max_retries = plan.policy.max_retries_per_provider
                for retry_idx in range(max_retries + 1):
                    try:
                        if retry_idx > 0:
                            span.add_event(
                                "provider_retry",
                                provider=candidate.provider_id,
                                attempt=str(retry_idx + 1),
                            )

                        response = await self._execute_candidate(candidate, request, request_id)

                        # Record success metrics
                        if attempt_idx > 0:
                            fallback_success.inc(
                                from_provider=plan.candidates[0].provider_id,
                                to_provider=candidate.provider_id,
                            )

                        span.add_event("completion_success", provider=candidate.provider_id)
                        response._transport = candidate.transport
                        response._provider = candidate.provider_id
                        return response

                    except (ServiceUnavailableError, Exception) as exc:
                        last_error = exc

                        if retry_idx < max_retries:
                            # Same provider retry
                            span.add_event(
                                "provider_retry_scheduled",
                                provider=candidate.provider_id,
                                next_attempt=str(retry_idx + 2),
                            )
                            continue

                        # Move to next provider (fallback)
                        if attempt_idx < len(plan.candidates) - 1:
                            next_candidate = plan.candidates[attempt_idx + 1]
                            fallback_total.inc(
                                from_provider=candidate.provider_id,
                                to_provider=next_candidate.provider_id,
                                reason=type(exc).__name__,
                            )
                            span.add_event(
                                "fallback_triggered",
                                from_provider=candidate.provider_id,
                                to_provider=next_candidate.provider_id,
                                error=type(exc).__name__,
                            )
                            logger.warning(
                                "Provider failed, trying fallback",
                                request_id=request_id,
                                model=request.model,
                                failed_provider=candidate.provider_id,
                                failed_transport=candidate.transport,
                                next_provider=next_candidate.provider_id,
                                next_transport=next_candidate.transport,
                                error=str(exc),
                            )
                            break  # Move to next candidate
                        else:
                            # All providers exhausted
                            record_failure(
                                model=request.model,
                                provider=candidate.provider_id,
                                transport=candidate.transport,
                                error_type=type(exc).__name__,
                                error_message=str(exc),
                                is_fallback=attempt_idx > 0,
                            )
                            span.set_status("error", str(exc))
                            raise

            if last_error is not None:
                span.set_status("error", str(last_error))
                raise last_error

            span.set_status("error", "unexpected_end_of_chain")
            raise InternalError(
                f"Unexpected end of provider chain for model {request.model}",
                details={"model": request.model},
            )

    async def _execute_candidate(
        self,
        candidate,
        request: ChatCompletionRequest,
        request_id: str,
    ) -> ChatCompletionResponse:
        """Execute a single candidate provider."""
        if candidate.transport == "browser":
            return await browser_completion_service.process_completion(
                request, request_id, candidate.canonical_model_id,
            )
        if candidate.transport == "api":
            # Resolve the API model properly from the registry
            resolved_model = unified_registry.resolve_model(candidate.canonical_model_id)
            return await api_completion_service.process_completion(
                request, resolved_model
            )
        if candidate.transport == "agent":
            return await agent_completion_service.process_completion(
                request, request_id, candidate.canonical_model_id, candidate.provider_id,
            )

        raise InternalError(
            f"Unsupported transport: {candidate.transport}",
            details={"transport": candidate.transport},
        )

    def _record_circuit_breaker_states(self):
        """Record circuit breaker states for metrics."""
        try:
            from app.browser.execution.dispatcher import browser_dispatcher
            pool = browser_dispatcher._pool
            health_ctrl = pool.provider_health
            if health_ctrl:
                for pid, opened_until in health_ctrl._opened_until.items():
                    is_open = opened_until is not None and opened_until > 0
                    circuit_breaker_state.set(1 if is_open else 0, provider=pid)
        except Exception:
            pass

    def _record_worker_count(self):
        """Record active worker count for metrics."""
        try:
            from app.browser.execution.dispatcher import browser_dispatcher
            snapshot = browser_dispatcher.diagnostics()
            browser_active_workers.set(snapshot.get("active_workers", 0))
        except Exception:
            pass


service = ChatProxyService()
