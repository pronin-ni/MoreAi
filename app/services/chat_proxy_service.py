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
from app.services.routing_resolver import resolve_routing

logger = get_logger(__name__)


class ChatProxyService:
    async def process_completion(
        self,
        request: ChatCompletionRequest,
        request_id: str,
    ) -> ChatCompletionResponse:
        async with trace_span("chat_completion", model=request.model, request_id=request_id) as span:
            span.add_event("request_received", message_count=str(len(request.messages)))

            resolved_model = unified_registry.resolve_model(request.model)
            transport = resolved_model.transport
            provider_id = resolved_model.provider_id

            span.set_attribute("transport", transport)
            span.set_attribute("provider_id", provider_id)
            span.add_event("model_resolved", canonical_id=resolved_model.canonical_id)

            # Apply routing overrides
            routing = resolve_routing(
                request.model,
                default_provider_id=provider_id,
            )

            # Record routing decision
            rule = "default"
            if routing.force_applied:
                rule = "force_provider"
            elif routing.primary_applied:
                rule = "primary"

            routing_decision_total.inc(
                model=request.model,
                selected_provider=routing.provider_id or provider_id,
                routing_rule=rule,
            )

            record_routing_decision(
                model=request.model,
                selected_provider=routing.provider_id or provider_id,
                transport=transport,
                routing_rule=rule,
            )

            span.set_attribute("routing_rule", rule)
            if routing.fallbacks:
                span.set_attribute("fallbacks", ",".join(routing.fallbacks))
            span.add_event("routing_decided", rule=rule)

            # Record circuit breaker states
            try:
                from app.browser.execution.dispatcher import browser_dispatcher
                health_ctrl = browser_dispatcher._health_controller
                if health_ctrl:
                    for pid, stats in health_ctrl._provider_stats.items():
                        is_open = stats.circuit_open_until is not None and stats.circuit_open_until > 0
                        circuit_breaker_state.set(1 if is_open else 0, provider=pid)
            except Exception:
                pass

            # Record worker count
            try:
                from app.browser.execution.dispatcher import browser_dispatcher
                snapshot = browser_dispatcher.get_health_snapshot()
                browser_active_workers.set(snapshot.active_workers)
            except Exception:
                pass

            # Build the provider chain: primary first, then fallbacks
            provider_chain = [routing.provider_id or provider_id] + routing.fallbacks

            last_error = None
            fallbacks_tried = []

            for idx, chain_provider_id in enumerate(provider_chain):
                try:
                    if transport == "browser":
                        response = await browser_completion_service.process_completion(
                            request, request_id, resolved_model.canonical_id,
                        )
                        response._transport = transport
                        response._provider = provider_id
                        span.add_event("completion_success", provider=provider_id)
                        return response

                    if transport == "api":
                        api_model = replace(resolved_model, provider_id=chain_provider_id)
                        response = await api_completion_service.process_completion(request, api_model)
                        response._transport = transport
                        response._provider = chain_provider_id
                        span.add_event("completion_success", provider=chain_provider_id)
                        return response

                    if transport == "agent":
                        response = await agent_completion_service.process_completion(
                            request, request_id, resolved_model.canonical_id, chain_provider_id,
                        )
                        response._transport = transport
                        response._provider = chain_provider_id
                        span.add_event("completion_success", provider=chain_provider_id)
                        return response

                except (ServiceUnavailableError, Exception) as exc:
                    last_error = exc
                    fallbacks_tried.append(chain_provider_id)

                    if idx < len(provider_chain) - 1:
                        next_provider = provider_chain[idx + 1]
                        fallback_total.inc(
                            from_provider=chain_provider_id,
                            to_provider=next_provider,
                            reason=str(type(exc).__name__),
                        )
                        record_routing_decision(
                            model=request.model,
                            selected_provider=next_provider,
                            transport=transport,
                            routing_rule="fallback",
                            fallbacks_tried=fallbacks_tried,
                        )

                        span.add_event(
                            "fallback_triggered",
                            from_provider=chain_provider_id,
                            to_provider=next_provider,
                            error=type(exc).__name__,
                        )

                        logger.warning(
                            "Provider failed, trying fallback",
                            request_id=request_id,
                            model=request.model,
                            failed_provider=chain_provider_id,
                            next_provider=next_provider,
                            error=str(exc),
                        )
                        continue

                    # All providers exhausted — record failure
                    record_failure(
                        model=request.model,
                        provider=chain_provider_id,
                        transport=transport,
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                        is_fallback=len(fallbacks_tried) > 1,
                    )
                    span.set_status("error", str(exc))
                    raise

            if last_error is not None:
                span.set_status("error", str(last_error))
                raise last_error

            span.set_status("error", "unsupported_transport")
            raise InternalError(
                f"Unsupported transport for model {resolved_model.canonical_id}",
                details={"transport": transport},
            )


service = ChatProxyService()
