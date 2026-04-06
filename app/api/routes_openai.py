import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.browser.execution.dispatcher import browser_dispatcher
from app.core.diagnostics import get_full_diagnostics, get_recent_failures, get_recent_routing_decisions, record_routing_decision
from app.core.errors import APIError, BadRequestError, InternalError
from app.core.health import health_status, live_probe, ready_probe
from app.core.logging import bind_request_id, clear_request_id, get_logger
from app.core.metrics import (
    errors_total,
    queue_depth,
    queue_wait_seconds,
    registry_model_count,
    request_latency,
    requests_total,
    metrics as metrics_registry,
)
from app.registry.unified import unified_registry
from app.schemas.openai import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ErrorResponse,
    HealthResponse,
    ModelList,
)
from app.services.chat_proxy_service import service
from app.services.routing_engine import routing_engine
from app.utils.openai_mapper import create_model_list

logger = get_logger(__name__)

router = APIRouter()


@router.get("/live")
async def liveness_probe():
    """Liveness probe — process is alive."""
    return JSONResponse(content=live_probe())


@router.get("/ready")
async def readiness_probe():
    """Readiness probe — service is ready to accept traffic."""
    result = ready_probe()
    status_code = 200 if result["ready"] else 503
    return JSONResponse(content=result, status_code=status_code)


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    result = health_status()
    return HealthResponse(status=result["status"], version=result["version"])


@router.get("/metrics")
async def metrics_endpoint():
    """Prometheus-format metrics."""
    # Update gauges from runtime state
    try:
        snapshot = browser_dispatcher.get_health_snapshot()
        queue_depth.set(snapshot.queue_size)
        registry_model_count.set(len(unified_registry.list_models()))
    except Exception:
        pass
    return PlainTextResponse(content=metrics_registry.render())


@router.get("/v1/models", response_model=ModelList)
async def list_models() -> ModelList:
    logger.info("Listing available models")
    return create_model_list()


@router.get("/diagnostics/integrations")
async def list_integrations_diagnostics() -> dict:
    logger.info("Listing integration diagnostics")
    diagnostics = unified_registry.diagnostics()
    diagnostics["browser_execution"] = browser_dispatcher.diagnostics()
    return diagnostics


@router.get("/diagnostics/models")
async def list_models_diagnostics() -> dict:
    logger.info("Listing model diagnostics")
    return {"models": unified_registry.list_models()}


@router.post(
    "/v1/chat/completions",
    response_model=ChatCompletionResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Bad request error"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
        503: {"model": ErrorResponse, "description": "Service unavailable"},
        504: {"model": ErrorResponse, "description": "Gateway timeout"},
    },
)
async def create_chat_completion(
    request: Request,
    body: ChatCompletionRequest,
) -> ChatCompletionResponse:
    request_id = bind_request_id()
    started = time.monotonic()
    transport = "unknown"
    provider = "unknown"
    status_code = "2xx"

    try:
        if body.stream:
            raise BadRequestError(
                "Streaming is not supported yet. Set stream=false.",
                details={"stream": True},
            )

        logger.info(
            "Received chat completion request",
            request_id=request_id,
            model=body.model,
            message_count=len(body.messages),
        )

        response = await service.process_completion(body, request_id)
        transport = getattr(response, "_transport", "unknown")
        provider = getattr(response, "_provider", "unknown")
        status_code = "2xx"

        logger.info(
            "Chat completion response sent",
            request_id=request_id,
            response_id=response.id,
        )

        return response

    except APIError as e:
        status_code = str(e.status_code)[0] + "xx" if hasattr(e, "status_code") else "5xx"
        transport = getattr(e, "_transport", "unknown")
        provider = getattr(e, "_provider", "unknown")
        errors_total.inc(
            error_type=type(e).__name__,
            transport=transport,
            provider=provider,
        )
        raise
    except Exception as e:
        status_code = "5xx"
        errors_total.inc(
            error_type=type(e).__name__,
            transport=transport,
            provider=provider,
        )
        logger.exception(
            "Unexpected error in chat completion",
            request_id=request_id,
            error=str(e),
        )
        raise InternalError(
            f"Internal server error: {str(e)}",
            details={"request_id": request_id},
        ) from e
    finally:
        elapsed = time.monotonic() - started
        requests_total.inc(
            transport=transport,
            provider=provider,
            model=body.model if "body" in dir() else "unknown",
            status=status_code,
        )
        request_latency.observe(elapsed, transport=transport, provider=provider)
        clear_request_id()


# ── Enhanced diagnostics endpoints ──


@router.get("/diagnostics/status")
async def full_status():
    """Aggregated system status — providers, registry, workers, queue, config."""
    return get_full_diagnostics()


@router.get("/diagnostics/routing")
async def routing_diagnostics():
    """Recent routing decisions — why providers were chosen, fallbacks, rejections."""
    return {
        "recent_decisions": get_recent_routing_decisions(50),
    }


@router.get("/diagnostics/routing/plan")
async def routing_plan(model: str):
    """Get the routing plan for a specific model — candidates, chain, policy."""
    plan = routing_engine.plan(model)
    return plan.summary()


@router.get("/diagnostics/failures")
async def recent_failures():
    """Recent failures summary."""
    return {
        "recent_failures": get_recent_failures(20),
    }
