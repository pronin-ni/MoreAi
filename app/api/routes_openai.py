import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.browser.execution.dispatcher import browser_dispatcher
from app.core.config import settings
from app.core.diagnostics import (
    get_full_diagnostics,
    get_recent_failures,
    get_recent_routing_decisions,
)
from app.core.errors import APIError, BadRequestError, InternalError
from app.core.health import health_status, live_probe, ready_probe
from app.core.logging import bind_request_id, clear_request_id, get_logger
from app.core.metrics import (
    errors_total,
    queue_depth,
    registry_model_count,
    request_latency,
    requests_total,
)
from app.core.metrics import (
    metrics as metrics_registry,
)
from app.pipeline.diagnostics import pipeline_diagnostics
from app.pipeline.executor import pipeline_executor
from app.pipeline.types import pipeline_registry
from app.registry.unified import unified_registry
from app.schemas.openai import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ErrorResponse,
    HealthResponse,
    Model,
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
    model_list = create_model_list()

    # Append pipeline models if enabled
    if settings.pipeline.enabled:
        for pdef in pipeline_registry.list_enabled():
            model_list.data.append(
                Model(
                    id=pdef.model_id,
                    created=int(time.time()),
                    owned_by="moreai-pipeline",
                    pipeline_id=pdef.pipeline_id,
                    display_name=pdef.display_name,
                    description=pdef.description,
                    stage_count=len(pdef.stages),
                    object="model",
                ),
            )

    return model_list


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


@router.get("/diagnostics/agents")
async def list_agent_diagnostics() -> dict:
    """Detailed diagnostics for agent providers (OpenCode, Kilocode, etc.)."""
    from app.agents.registry import registry as agent_registry

    logger.info("Listing agent provider diagnostics")
    return agent_registry.diagnostics()


@router.get("/diagnostics/transports")
async def list_transport_status() -> dict:
    """Transport feature flag status and model counts.

    Shows which transport types (browser, api, agent) are enabled/disabled
    via config and how many models are available for each.
    """
    from app.agents.registry import registry as agent_registry
    from app.browser.registry import registry as browser_registry
    from app.core.config import settings
    from app.integrations.registry import api_registry

    flags = settings.transport_feature_flags

    # Count models per transport (before filtering)
    browser_count = len(browser_registry.list_models())
    api_count = len(api_registry.list_models())
    agent_count = len(agent_registry.list_models())

    # Count after filtering
    all_models = [
        *browser_registry.list_models(),
        *api_registry.list_models(),
        *agent_registry.list_models(),
    ]
    from app.core.transport_filters import filter_models_by_transport

    filtered_models = filter_models_by_transport(all_models)

    filtered_counts: dict[str, int] = {}
    for m in filtered_models:
        t = m.get("transport", "unknown")
        filtered_counts[t] = filtered_counts.get(t, 0) + 1

    return {
        "feature_flags": {
            "browser_providers": {
                "enabled": flags.browser_providers,
                "env_var": "ENABLE_BROWSER_PROVIDERS",
                "total_models": browser_count,
                "visible_models": filtered_counts.get("browser", 0),
                "status": "ENABLED" if flags.browser_providers else "DISABLED",
            },
            "api_providers": {
                "enabled": flags.api_providers,
                "env_var": "ENABLE_API_PROVIDERS",
                "total_models": api_count,
                "visible_models": filtered_counts.get("api", 0),
                "status": "ENABLED" if flags.api_providers else "DISABLED",
            },
            "agent_providers": {
                "enabled": flags.agent_providers,
                "env_var": "ENABLE_AGENT_PROVIDERS",
                "total_models": agent_count,
                "visible_models": filtered_counts.get("agent", 0),
                "status": "ENABLED" if flags.agent_providers else "DISABLED",
            },
        },
        "total_models": {
            "before_filtering": len(all_models),
            "after_filtering": len(filtered_models),
        },
    }


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

        # Check if this is a pipeline model
        if body.model.startswith("pipeline/") or pipeline_registry.is_pipeline_model(body.model):
            return await _execute_pipeline(body, request_id, started)

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


async def _execute_pipeline(
    body: ChatCompletionRequest,
    request_id: str,
    started: float,
) -> ChatCompletionResponse:
    """Execute a pipeline request and return the response."""
    # Check if pipelines are enabled
    if not settings.pipeline.enabled:
        raise BadRequestError(
            f"Pipeline model '{body.model}' requested but pipeline execution is disabled",
            details={"model": body.model},
        )

    # Resolve pipeline definition
    pipeline_def = pipeline_registry.get_by_model_id(body.model) or pipeline_registry.get(
        body.model.removeprefix("pipeline/")
    )

    if pipeline_def is None:
        raise BadRequestError(
            f"Unknown pipeline model: {body.model}",
            details={"model": body.model},
        )

    if not pipeline_def.enabled:
        raise BadRequestError(
            f"Pipeline '{pipeline_def.pipeline_id}' is disabled",
            details={"pipeline_id": pipeline_def.pipeline_id},
        )

    logger.info(
        "Executing pipeline",
        request_id=request_id,
        pipeline_id=pipeline_def.pipeline_id,
        model=body.model,
        message_count=str(len(body.messages)),
    )

    # Execute the pipeline
    response = await pipeline_executor.execute(pipeline_def, body, request_id)

    logger.info(
        "Pipeline execution completed",
        request_id=request_id,
        pipeline_id=pipeline_def.pipeline_id,
    )

    return response


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


# ── Pipeline Admin endpoints ──


@router.get("/admin/pipelines")
async def list_pipelines():
    """List all registered pipelines with basic info."""
    pipelines = []
    for pdef in pipeline_registry.list_all():
        pipelines.append(
            {
                "pipeline_id": pdef.pipeline_id,
                "model_id": pdef.model_id,
                "display_name": pdef.display_name,
                "description": pdef.description,
                "enabled": pdef.enabled,
                "stage_count": len(pdef.stages),
                "stages": [
                    {
                        "stage_id": s.stage_id,
                        "role": s.role.value,
                        "target_model": s.target_model,
                        "failure_policy": s.failure_policy.value,
                        "max_retries": s.max_retries,
                    }
                    for s in pdef.stages
                ],
            }
        )
    return {"pipelines": pipelines, "total": len(pipelines)}


@router.get("/admin/pipelines/stats")
async def pipeline_stats():
    """Get aggregate pipeline execution statistics."""
    stats = pipeline_diagnostics.get_stats()
    return {"stats": stats}


@router.get("/admin/pipelines/traces")
async def pipeline_traces(limit: int = 20):
    """Get recent pipeline execution traces."""
    traces = pipeline_diagnostics.get_recent_traces(limit)
    return {
        "traces": [
            {
                "trace_id": t.trace_id,
                "pipeline_id": t.pipeline_id,
                "model_id": t.model_id,
                "status": t.status,
                "started_at": t.started_at,
                "completed_at": t.completed_at,
                "total_duration_ms": t.total_duration_ms,
                "stage_count": len(t.stage_traces),
                "error_message": t.error_message,
                "request_id": t.request_id,
            }
            for t in traces
        ],
        "total": len(traces),
    }


# ── Pipeline Execution endpoints (must come before {pipeline_id}) ──


@router.get("/admin/pipelines/executions")
async def list_executions(
    pipeline_id: str | None = None, status: str | None = None, limit: int = 20
):
    """List recent pipeline executions with filtering."""
    from app.pipeline.observability.store import execution_store

    executions = execution_store.get_recent(limit=limit, pipeline_id=pipeline_id, status=status)
    return {
        "executions": [e.to_list_row() for e in executions],
        "total": len(executions),
        "filters": {"pipeline_id": pipeline_id, "status": status},
    }


@router.get("/admin/pipelines/executions/{execution_id}")
async def get_execution_detail(execution_id: str):
    """Get detailed execution trace with stage explainability.

    Checks in-memory store first, then falls back to persistent store.
    """
    from app.pipeline.observability.recorder import observability_recorder
    from app.pipeline.observability.store import execution_store

    summary = execution_store.get(execution_id)

    # Fallback to persistent store if not in memory
    if summary is None:
        try:
            from app.pipeline.observability.persistent_store import get_persistent_store

            persistent = get_persistent_store()
            exec_data = persistent.get(execution_id)
            if exec_data is None:
                return JSONResponse(
                    status_code=404,
                    content={"error": f"Execution '{execution_id}' not found"},
                )
            return exec_data
        except Exception:
            return JSONResponse(
                status_code=404,
                content={"error": f"Execution '{execution_id}' not found"},
            )

    # Build failure analysis if applicable
    failure_analysis = None
    if summary.status == "failed":
        failure_analysis = observability_recorder.build_failure_analysis(summary)

    result = summary.to_dict()
    if failure_analysis:
        result["failure_analysis"] = failure_analysis.to_dict()

    return result


@router.get("/admin/pipelines/executions/{execution_id}/summary")
async def get_execution_summary(execution_id: str):
    """Get a compact summary of an execution (no full stage details)."""
    from app.pipeline.observability.store import execution_store

    summary = execution_store.get(execution_id)
    if summary is None:
        # Try persistent store
        try:
            from app.pipeline.observability.persistent_store import get_persistent_store

            persistent = get_persistent_store()
            exec_data = persistent.get(execution_id)
            if exec_data is None:
                return JSONResponse(status_code=404, content={"error": "not found"})
            # Return compact version
            return {
                "execution_id": exec_data.get("execution_id"),
                "pipeline_id": exec_data.get("pipeline_id"),
                "status": exec_data.get("status"),
                "duration_ms": exec_data.get("duration_ms"),
                "stage_count": exec_data.get("stage_count"),
                "stages_completed": exec_data.get("stages_completed"),
                "total_fallbacks": exec_data.get("total_fallbacks"),
                "started_at": exec_data.get("started_at"),
                "finished_at": exec_data.get("finished_at"),
            }
        except Exception:
            return JSONResponse(status_code=404, content={"error": "not found"})

    return summary.to_list_row()


@router.get("/admin/pipelines/executions/store/stats")
async def execution_store_stats():
    """Get execution store statistics."""
    from app.pipeline.observability.store import execution_store

    return execution_store.get_stats()


@router.get("/admin/pipelines/stage-performance")
async def get_stage_performance(model_id: str | None = None, stage_role: str | None = None):
    """Get stage-specific performance metrics per model."""
    from app.pipeline.observability.stage_perf import stage_performance

    if model_id and stage_role:
        return stage_performance.get_model_role_stats(model_id, stage_role)

    all_stats = stage_performance.get_all_model_roles()
    return {"stats": all_stats, "total": len(all_stats)}


@router.get("/admin/pipelines/stage-scoring")
async def get_stage_scoring(model_id: str | None = None, stage_role: str | None = None):
    """Get scoring breakdown for models in a stage role.

    Shows full scoring transparency:
    - base static score
    - dynamic performance adjustment
    - failure penalty
    - final score
    - cold_start / fallback_heavy / top_performer badges
    """
    from app.agents.registry import registry as agent_registry
    from app.browser.registry import registry as browser_registry
    from app.integrations.registry import api_registry
    from app.intelligence.suitability import suitability_scorer
    from app.intelligence.tags import capability_registry

    role = stage_role or "generate"

    # Collect all models with scoring
    scoring_results: list[dict] = []

    for reg, transport in [
        (browser_registry, "browser"),
        (api_registry, "api"),
        (agent_registry, "agent"),
    ]:
        for m in reg.list_models():
            if not m.get("enabled", True):
                continue
            mid = m["id"]
            provider_id = m.get("provider_id", "")

            # Filter if specific model requested
            if model_id and mid != model_id:
                continue

            # Get tags
            tags = capability_registry.get_tags(mid, provider_id)

            # Get performance stats
            try:
                from app.pipeline.observability.stage_perf import stage_performance as perf_tracker

                perf_stats = perf_tracker.get_model_role_stats(mid, role)
            except Exception:
                perf_stats = {
                    "sample_count": 0,
                    "success_rate": 0.5,
                    "fallback_rate": 0.0,
                    "avg_duration_ms": 0.0,
                }

            # Get scoring breakdown
            breakdown = suitability_scorer.compute_breakdown(
                mid,
                provider_id,
                transport,
                role,
            )

            sample_count = perf_stats.get("sample_count", 0)
            fallback_rate = perf_stats.get("fallback_rate", 0.0)
            success_rate = perf_stats.get("success_rate", 0.5)

            # Status flags
            cold_start = sample_count < 5
            fallback_heavy = fallback_rate > 0.2 and sample_count >= 5
            top_performer = success_rate >= 0.9 and sample_count >= 5

            scoring_results.append(
                {
                    "model_id": mid,
                    "provider_id": provider_id,
                    "transport": transport,
                    "role": role,
                    "final_score": round(breakdown.final_score, 3),
                    "base_static_score": round(breakdown.base_static_score, 3),
                    "dynamic_adjustment": round(breakdown.dynamic_adjustment, 3),
                    "failure_penalty": round(breakdown.failure_penalty, 3),
                    "penalty_reasons": breakdown.penalty_reasons,
                    "success_rate": round(success_rate, 3),
                    "fallback_rate": round(fallback_rate, 3),
                    "avg_duration_ms": round(perf_stats.get("avg_duration_ms", 0.0), 1),
                    "sample_count": sample_count,
                    "data_confidence": round(breakdown.data_confidence, 3),
                    "tags": sorted(tags),
                    "cold_start": cold_start,
                    "fallback_heavy": fallback_heavy,
                    "top_performer": top_performer,
                    # Quality metrics
                    "quality_score": round(breakdown.quality_score, 3),
                    "quality_adjustment": round(breakdown.quality_adjustment, 3),
                    "quality_sample_count": breakdown.quality_sample_count,
                    "quality_confidence": round(breakdown.quality_confidence, 3),
                    "high_quality": breakdown.quality_score >= 0.7
                    and breakdown.quality_sample_count >= 3,
                    "low_quality": breakdown.quality_score < 0.35
                    and breakdown.quality_sample_count >= 3,
                }
            )

    # Enrich with cross-stage data from quality store
    try:
        from app.pipeline.observability.quality_scoring import quality_metrics_store

        qs_summaries = quality_metrics_store.get_all_quality_summary(role=role)
        qs_map = {f"{s['model_id']}:{s.get('provider_id', '')}": s for s in qs_summaries}
        for s in scoring_results:
            key = f"{s['model_id']}:{s['provider_id']}"
            qs = qs_map.get(key, {})
            s["downstream_corrections"] = qs.get("avg_downstream_corrections", 0)
            s["review_actionability"] = qs.get("avg_review_actionability", 0.5)
            s["refine_effectiveness"] = qs.get("avg_refine_effectiveness", 0.5)
            s["final_improvement_score"] = qs.get("avg_final_improvement_score", 0.5)
    except Exception:
        for s in scoring_results:
            s["downstream_corrections"] = 0
            s["review_actionability"] = 0.5
            s["refine_effectiveness"] = 0.5
            s["final_improvement_score"] = 0.5

    # Sort by final score descending
    scoring_results.sort(key=lambda s: s["final_score"], reverse=True)

    return {
        "stage_role": role,
        "scoring": scoring_results,
        "total": len(scoring_results),
    }


@router.get("/admin/pipelines/penalty-cache")
async def get_penalty_cache_status():
    """Get current global penalty cache status."""
    from app.pipeline.observability.penalty_cache import global_penalty_cache

    penalties = global_penalty_cache.get_all_penalties()
    return {
        "active_penalties": penalties,
        "total_tracked": len(penalties),
        "ttl_seconds": global_penalty_cache._ttl,
    }


@router.post("/admin/pipelines/penalty-cache/clear")
async def clear_penalty_cache():
    """Clear all cached penalties."""
    from app.pipeline.observability.penalty_cache import global_penalty_cache

    global_penalty_cache.clear()
    return {"status": "cleared"}


# ── Scoring History & Trend Analysis endpoints ──


@router.get("/admin/pipelines/scoring-history")
async def get_scoring_history(
    model_id: str | None = None,
    role: str | None = None,
    window: str | None = None,
    limit: int = 200,
):
    """Get scoring history time series.

    Query params:
    - model_id: Filter by model (optional).
    - role: Filter by stage role (optional).
    - window: Time window — '1h', '24h', '7d' (default '24h').
    - limit: Max data points (default 200).
    """
    from app.pipeline.observability.scoring_history import scoring_history_store

    window_map = {"1h": 3600, "24h": 86400, "7d": 604800}
    window_seconds = window_map.get(window or "24h", 86400)

    history = scoring_history_store.get_history(
        model_id=model_id,
        role=role,
        window_seconds=window_seconds,
        limit=limit,
    )

    points = [
        {
            "timestamp": s.timestamp,
            "model_id": s.model_id,
            "provider_id": s.provider_id,
            "transport": s.transport,
            "role": s.role,
            "final_score": round(s.final_score, 4),
            "base_static_score": round(s.base_static_score, 4),
            "dynamic_adjustment": round(s.dynamic_adjustment, 4),
            "failure_penalty": round(s.failure_penalty, 4),
            "success_rate": round(s.success_rate, 4),
            "fallback_rate": round(s.fallback_rate, 4),
            "avg_duration_ms": round(s.avg_duration_ms, 1),
            "sample_count": s.sample_count,
            "data_confidence": round(s.data_confidence, 4),
        }
        for s in history
    ]

    return {
        "history": points,
        "total": len(points),
        "window": window or "24h",
        "window_seconds": window_seconds,
    }


@router.get("/admin/pipelines/scoring-trends")
async def get_scoring_trends(
    role: str | None = None,
    window: str | None = None,
    limit: int = 50,
):
    """Get scoring trends for all models.

    Returns trend summaries including:
    - score / success_rate / fallback_rate / duration deltas
    - trend classification: improving, stable, declining, unstable
    - main driver identification

    Query params:
    - role: Filter by stage role (optional, default all roles).
    - window: Time window — '1h', '24h', '7d' (default '24h').
    - limit: Max results (default 50).
    """
    from app.pipeline.observability.scoring_trends import scoring_trend_analyzer

    window_map = {"1h": 3600, "24h": 86400, "7d": 604800}
    window_seconds = window_map.get(window or "24h", 86400)

    trends = scoring_trend_analyzer.get_all_trends(
        role=role,
        window_seconds=window_seconds,
    )

    trend_list = [
        {
            "model_id": t.model_id,
            "provider_id": t.provider_id,
            "transport": t.transport,
            "role": t.role,
            "window": t.window_label,
            "current_score": round(t.current_score, 4),
            "previous_score": round(t.previous_score, 4),
            "score_delta": round(t.score_delta, 4),
            "current_success_rate": round(t.current_success_rate, 4),
            "previous_success_rate": round(t.previous_success_rate, 4),
            "success_rate_delta": round(t.success_rate_delta, 4),
            "current_fallback_rate": round(t.current_fallback_rate, 4),
            "previous_fallback_rate": round(t.previous_fallback_rate, 4),
            "fallback_rate_delta": round(t.fallback_rate_delta, 4),
            "current_avg_duration_ms": round(t.current_avg_duration_ms, 1),
            "previous_avg_duration_ms": round(t.previous_avg_duration_ms, 1),
            "duration_delta_ms": round(t.duration_delta_ms, 1),
            "overall_trend": t.overall_trend,
            "main_driver": t.main_driver,
            "data_points": t.data_points,
            "has_enough_data": t.has_enough_data,
        }
        for t in trends[:limit]
    ]

    # Derive summary lists
    improvers = [t for t in trend_list if t["overall_trend"] == "improving"][:10]
    decliners = [t for t in trend_list if t["overall_trend"] == "declining"][:10]
    unstable = [t for t in trend_list if t["overall_trend"] == "unstable"][:10]

    return {
        "trends": trend_list,
        "total": len(trend_list),
        "window": window or "24h",
        "top_improvers": improvers,
        "top_decliners": decliners,
        "unstable_models": unstable,
    }


@router.post("/admin/pipelines/scoring-history/snapshot")
async def trigger_scoring_snapshot():
    """Trigger an immediate scoring snapshot capture.

    Captures current scoring state for all models and roles.
    """
    from app.pipeline.observability.scoring_trends import snapshot_scheduler

    count = snapshot_scheduler.capture_now()
    return {
        "status": "ok",
        "snapshots_recorded": count,
    }


@router.get("/admin/pipelines/scoring-history/scheduler")
async def get_snapshot_scheduler_status():
    """Get snapshot scheduler status."""
    from app.pipeline.observability.scoring_trends import snapshot_scheduler

    return snapshot_scheduler.get_status()


@router.get("/admin/pipelines/scoring-history/store/stats")
async def get_scoring_history_store_stats():
    """Get scoring history store statistics."""
    from app.pipeline.observability.scoring_history import scoring_history_store

    return scoring_history_store.get_stats()


# ── Stage Quality endpoints ──


@router.get("/admin/pipelines/stage-quality")
async def get_stage_quality(role: str | None = None):
    """Get quality metrics for all models per stage role.

    Returns:
    - Per-model quality scores, sample counts, issue counts
    - Top quality and low quality model lists
    - Quality trend data
    """
    from app.pipeline.observability.quality_scoring import quality_metrics_store

    summaries = quality_metrics_store.get_all_quality_summary(role=role, window=100)

    # Derive top/low quality lists
    top_quality = [s for s in summaries if s["sample_count"] >= 3][:10]
    low_quality = [s for s in summaries if s["sample_count"] >= 3 and s["avg_quality"] < 0.4][:10]

    # Determine quality stability (high stddev = unstable)
    for s in summaries:
        s["quality_label"] = (
            "high_quality"
            if s["avg_quality"] >= 0.7 and s["sample_count"] >= 3
            else "low_quality"
            if s["avg_quality"] < 0.35 and s["sample_count"] >= 3
            else "unstable_quality"
            if s.get("max_quality", 1) - s.get("min_quality", 0) > 0.5 and s["sample_count"] >= 3
            else "normal"
        )

    return {
        "quality": summaries,
        "total": len(summaries),
        "top_quality": top_quality,
        "low_quality": low_quality,
    }


@router.get("/admin/pipelines/stage-quality/{role}")
async def get_stage_quality_for_role(role: str):
    """Get quality breakdown for a specific stage role."""
    from fastapi.responses import RedirectResponse

    return RedirectResponse(url=f"/admin/pipelines/stage-quality?role={role}")


@router.get("/admin/pipelines/stage-quality/cross-stage")
async def get_cross_stage_quality(role: str | None = None, window: str | None = None):
    """Get cross-stage quality diagnostics.

    Returns downstream correction stats, review actionability,
    refine effectiveness, and top/bottom models by cross-stage quality.
    """
    from app.pipeline.observability.quality_scoring import quality_metrics_store

    summaries = quality_metrics_store.get_all_quality_summary(role=role, window=100)

    # Enrich with cross-stage labels
    for s in summaries:
        avg_dc = s.get("avg_downstream_corrections", 0)
        avg_ra = s.get("avg_review_actionability", 0.5)
        avg_re = s.get("avg_refine_effectiveness", 0.5)

        s["cross_stage_badges"] = []
        if avg_dc > 3:
            s["cross_stage_badges"].append("draft_often_corrected")
        if avg_ra >= 0.7:
            s["cross_stage_badges"].append("actionable_reviewer")
        elif avg_ra < 0.3 and s.get("avg_quality", 0.5) > 0.4:
            s["cross_stage_badges"].append("weak_reviewer")
        if avg_re >= 0.7:
            s["cross_stage_badges"].append("effective_refiner")
        elif avg_re < 0.3 and s.get("avg_quality", 0.5) > 0.4:
            s["cross_stage_badges"].append("overcorrected")

    # Top/bottom by cross-stage quality
    with_cross_data = [s for s in summaries if s.get("sample_count", 0) >= 3]
    top_cross = sorted(
        with_cross_data,
        key=lambda s: s.get("avg_review_actionability", 0) + s.get("avg_refine_effectiveness", 0),
        reverse=True,
    )[:10]
    bottom_cross = sorted(
        with_cross_data,
        key=lambda s: s.get("avg_review_actionability", 0) + s.get("avg_refine_effectiveness", 0),
    )[:10]

    return {
        "cross_stage": summaries,
        "total": len(summaries),
        "top_cross_stage_quality": top_cross,
        "bottom_cross_stage_quality": bottom_cross,
    }


@router.get("/admin/pipelines/stage-scoring/{role}")
async def get_scoring_for_role(role: str):
    """Get scoring breakdown for all models in a specific stage role."""
    from fastapi.responses import RedirectResponse

    return RedirectResponse(url=f"/admin/pipelines/stage-scoring?stage_role={role}")


@router.get("/admin/pipelines/stage-performance/trends")
async def get_stage_performance_trends():
    """Get stage performance trends."""
    from app.pipeline.observability.stage_perf import stage_performance

    all_stats = stage_performance.get_all_model_roles()
    top_performers = sorted(
        [s for s in all_stats if s.get("count", 0) >= 5],
        key=lambda s: s.get("success_rate", 0),
        reverse=True,
    )[:10]
    fallback_heavy = sorted(
        [s for s in all_stats if s.get("fallback_rate", 0) > 0.1],
        key=lambda s: s.get("fallback_rate", 0),
        reverse=True,
    )[:10]

    all_model_ids = set()
    from app.agents.registry import registry as agent_registry
    from app.browser.registry import registry as browser_registry
    from app.integrations.registry import api_registry

    for reg in [browser_registry, api_registry, agent_registry]:
        for m in reg.list_models():
            if m.get("enabled", True):
                all_model_ids.add(m["id"])

    tracked_models = {s["model_id"] for s in all_stats}
    cold_start = list(all_model_ids - tracked_models)

    return {
        "top_performers": top_performers,
        "fallback_heavy": fallback_heavy,
        "cold_start_models": cold_start,
        "total_tracked": len(tracked_models),
        "total_known_models": len(all_model_ids),
    }


@router.get("/admin/pipelines/executions/persistent")
async def list_persistent_executions(
    pipeline_id: str | None = None, status: str | None = None, limit: int = 20
):
    """List recent executions from the persistent SQLite store."""
    from app.pipeline.observability.persistent_store import get_persistent_store

    store = get_persistent_store()
    executions = store.get_recent(limit=limit, pipeline_id=pipeline_id, status=status)
    return {
        "executions": executions,
        "total": len(executions),
        "persistent": True,
        "filters": {"pipeline_id": pipeline_id, "status": status},
    }


@router.post("/admin/pipelines/{pipeline_id}/run-test")
async def run_pipeline_test(pipeline_id: str, body: dict | None = None):
    """Run a diagnostic test execution of a pipeline."""
    import time as _time
    import uuid

    from app.pipeline.executor import pipeline_executor
    from app.pipeline.types import pipeline_registry
    from app.schemas.openai import ChatCompletionRequest, ChatMessage

    pipeline_def = pipeline_registry.get(pipeline_id)
    if pipeline_def is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"Pipeline '{pipeline_id}' not found"},
        )

    if not pipeline_def.enabled:
        return JSONResponse(
            status_code=400,
            content={"error": f"Pipeline '{pipeline_id}' is disabled"},
        )

    test_prompt = "Test prompt for pipeline diagnostics"
    if body and isinstance(body, dict):
        test_prompt = body.get("prompt", test_prompt)

    request_id = f"test-{uuid.uuid4().hex[:8]}"
    test_request = ChatCompletionRequest(
        model=pipeline_def.model_id,
        messages=[ChatMessage(role="user", content=test_prompt)],
    )

    started = _time.monotonic()
    try:
        response = await pipeline_executor.execute(pipeline_def, test_request, request_id)
        elapsed_ms = (_time.monotonic() - started) * 1000

        return {
            "status": "success",
            "pipeline_id": pipeline_id,
            "execution_id": response.id,
            "request_id": request_id,
            "duration_ms": round(elapsed_ms, 1),
            "output_preview": response.choices[0].message.content[:200] if response.choices else "",
        }

    except Exception as exc:
        elapsed_ms = (_time.monotonic() - started) * 1000
        return {
            "status": "failed",
            "pipeline_id": pipeline_id,
            "request_id": request_id,
            "duration_ms": round(elapsed_ms, 1),
            "error": str(exc),
        }


@router.post("/admin/pipelines/{pipeline_id}/run-sandbox")
async def run_pipeline_sandbox(pipeline_id: str, body: dict | None = None):
    """Dry-run a pipeline without calling providers."""
    from app.pipeline.types import pipeline_registry

    pipeline_def = pipeline_registry.get(pipeline_id)
    if pipeline_def is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"Pipeline '{pipeline_id}' not found"},
        )

    test_prompt = "Sandbox test prompt"
    if body and isinstance(body, dict):
        test_prompt = body.get("prompt", test_prompt)

    stages_result = []
    prev_model = ""

    for stage_def in pipeline_def.stages:
        stage_info = {
            "stage_id": stage_def.stage_id,
            "role": stage_def.role.value,
            "uses_intelligent_selection": stage_def.uses_intelligent_selection,
        }

        if stage_def.uses_intelligent_selection and stage_def.selection_policy:
            try:
                from app.intelligence.selection import model_selector
                from app.intelligence.types import SelectionPolicy

                policy = SelectionPolicy(**stage_def.selection_policy)
                selection = model_selector.select_for_stage(
                    stage_id=stage_def.stage_id,
                    stage_role=stage_def.role,
                    policy=policy,
                    previous_stage_model=prev_model,
                )

                stage_info["selected_model"] = selection.selected_model
                stage_info["selected_provider"] = selection.selected_provider
                stage_info["candidates"] = [c.to_dict() for c in selection.all_candidates[:10]]
                stage_info["candidates_considered"] = selection.candidates_considered
                stage_info["candidates_viable"] = selection.candidates_viable
                stage_info["candidates_excluded"] = selection.candidates_excluded
                prev_model = selection.selected_model

            except Exception as exc:
                stage_info["selection_error"] = str(exc)
        else:
            stage_info["selected_model"] = stage_def.target_model
            prev_model = stage_def.target_model

        stages_result.append(stage_info)

    return {
        "status": "sandbox_dry_run",
        "pipeline_id": pipeline_id,
        "pipeline_display_name": pipeline_def.display_name,
        "prompt": test_prompt,
        "stages": stages_result,
        "stage_count": len(stages_result),
    }


@router.get("/admin/pipelines/{pipeline_id}")
async def get_pipeline(pipeline_id: str):
    """Get detailed info for a specific pipeline."""
    pdef = pipeline_registry.get(pipeline_id)
    if pdef is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"Pipeline '{pipeline_id}' not found"},
        )

    return {
        "pipeline_id": pdef.pipeline_id,
        "model_id": pdef.model_id,
        "display_name": pdef.display_name,
        "description": pdef.description,
        "enabled": pdef.enabled,
        "max_total_time_ms": pdef.max_total_time_ms,
        "max_stage_retries": pdef.max_stage_retries,
        "stages": [
            {
                "stage_id": s.stage_id,
                "role": s.role.value,
                "target_model": s.target_model,
                "input_mapping": s.input_mapping.model_dump(),
                "output_mode": s.output_mode.value,
                "failure_policy": s.failure_policy.value,
                "max_retries": s.max_retries,
                "prompt_template": s.prompt_template,
            }
            for s in pdef.stages
        ],
        "metadata": pdef.metadata,
    }


@router.post("/admin/pipelines/{pipeline_id}/enable")
async def enable_pipeline(pipeline_id: str):
    """Enable a pipeline."""
    pdef = pipeline_registry.get(pipeline_id)
    if pdef is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"Pipeline '{pipeline_id}' not found"},
        )

    pipeline_registry.enable(pipeline_id)
    return {"pipeline_id": pipeline_id, "enabled": True}


@router.post("/admin/pipelines/{pipeline_id}/disable")
async def disable_pipeline(pipeline_id: str):
    """Disable a pipeline."""
    pdef = pipeline_registry.get(pipeline_id)
    if pdef is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"Pipeline '{pipeline_id}' not found"},
        )

    pipeline_registry.disable(pipeline_id)
    return {"pipeline_id": pipeline_id, "enabled": False}


@router.get("/admin/pipelines/traces/{trace_id}")
async def get_pipeline_trace(trace_id: str):
    """Get a detailed pipeline execution trace."""
    trace = pipeline_diagnostics.get_trace(trace_id)
    if trace is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"Trace '{trace_id}' not found"},
        )

    return {
        "trace_id": trace.trace_id,
        "pipeline_id": trace.pipeline_id,
        "model_id": trace.model_id,
        "status": trace.status,
        "started_at": trace.started_at,
        "completed_at": trace.completed_at,
        "total_duration_ms": trace.total_duration_ms,
        "final_output": trace.final_output[:500] if trace.final_output else "",
        "error_message": trace.error_message,
        "request_id": trace.request_id,
        "original_request_model": trace.original_request_model,
        "stage_traces": [
            {
                "stage_id": st.stage_id,
                "role": st.role,
                "target_model": st.target_model,
                "provider_id": st.provider_id,
                "status": st.status,
                "duration_ms": st.duration_ms,
                "retry_count": st.retry_count,
                "error_message": st.error_message,
                "result_summary": st.result_summary,
            }
            for st in trace.stage_traces
        ],
    }


# ── Model Intelligence endpoints ──


@router.get("/admin/intelligence/models")
async def list_model_intelligence():
    """List per-model intelligence: availability, stability, suitability, ranking."""
    from app.intelligence.stats import stats_aggregator
    from app.intelligence.suitability import suitability_scorer
    from app.intelligence.tags import capability_registry

    all_stats = stats_aggregator.get_all_model_stats()
    models = []

    for stats in all_stats:
        suitability = suitability_scorer.compute_suitability(
            stats.model_id,
            stats.provider_id,
            stats.transport,
        )
        tags = capability_registry.get_tags(stats.model_id, stats.provider_id)

        models.append(
            {
                "model_id": stats.model_id,
                "provider_id": stats.provider_id,
                "transport": stats.transport,
                "availability": {
                    "score": round(stats.availability_score, 3),
                    "success_rate": round(stats.success_rate, 3),
                    "failure_rate": round(stats.failure_rate, 3),
                    "circuit_open": stats.circuit_open,
                    "consecutive_failures": stats.consecutive_failures,
                    "health_score": round(stats.health_score, 3),
                },
                "latency": {
                    "avg_s": round(stats.avg_latency_s, 2),
                    "p50_s": round(stats.p50_latency_s, 2),
                    "p95_s": round(stats.p95_latency_s, 2),
                    "score": round(stats.latency_score, 3),
                },
                "stability_score": round(stats.stability_score, 3),
                "capability_tags": sorted(tags),
                "stage_suitability": {
                    "generate": round(suitability.generate_score, 3),
                    "review": round(suitability.review_score, 3),
                    "critique": round(suitability.critique_score, 3),
                    "refine": round(suitability.refine_score, 3),
                    "verify": round(suitability.verify_score, 3),
                    "transform": round(suitability.transform_score, 3),
                },
                "recommended_roles": _get_recommended_roles(suitability),
                "request_count": stats.request_count,
                "fallback_count": stats.fallback_count,
            }
        )

    return {"models": models, "total": len(models)}


@router.get("/admin/intelligence/tags")
async def list_capability_tags():
    """List all capability tags and their assignments."""
    from app.intelligence.tags import capability_registry

    return capability_registry.list_all_tags()


@router.get("/admin/intelligence/ranking/{role}")
async def get_ranking_for_role(role: str, limit: int = 10):
    """Get ranked list of models for a specific stage role."""
    from app.intelligence.stats import stats_aggregator
    from app.intelligence.suitability import suitability_scorer

    # Collect all candidates
    all_stats = stats_aggregator.get_all_model_stats()
    ranked = []

    for stats in all_stats:
        stage_score = suitability_scorer.compute_for_role(
            stats.model_id,
            stats.provider_id,
            stats.transport,
            role,
        )

        ranked.append(
            {
                "model_id": stats.model_id,
                "provider_id": stats.provider_id,
                "transport": stats.transport,
                "stage_score": round(stage_score, 3),
                "availability_score": round(stats.availability_score, 3),
                "final_score": round(stage_score * 0.6 + stats.availability_score * 0.4, 3),
            }
        )

    # Sort by final score
    ranked.sort(key=lambda r: r["final_score"], reverse=True)

    return {
        "role": role,
        "ranked_models": ranked[:limit],
        "total_candidates": len(ranked),
    }


@router.get("/admin/models/exploration")
async def get_exploration_status():
    """Get exploration status for all tracked models.

    Shows:
    - Which models are in cold-start state
    - Exploration attempts count
    - Success rate
    - Whether they participate in selection
    """
    from app.intelligence.stats import stats_aggregator
    from app.intelligence.tracker import model_intelligence_tracker

    all_entries = model_intelligence_tracker.get_all_entries()
    all_stats = stats_aggregator.get_all_model_stats()

    # Build stats lookup
    stats_lookup = {(s.model_id, s.provider_id, s.transport): s for s in all_stats}

    cold_start_models = []
    established_models = []

    for entry in all_entries:
        # Get runtime stats
        provider_id = entry.last_provider or ""
        transport = _resolve_transport(entry.canonical_id)
        stats = stats_lookup.get((entry.canonical_id, provider_id, transport))

        sample_count = stats.request_count if stats else 0
        success_rate = stats.success_rate if stats else 0.0

        model_info = {
            "model_id": entry.canonical_id,
            "is_cold_start": entry.is_cold_start,
            "exploration_attempts": entry.exploration_attempts,
            "successful_explorations": entry.successful_explorations,
            "sample_count": sample_count,
            "success_rate": round(success_rate, 3),
            "is_currently_available": entry.is_currently_available,
            "participates_in_selection": entry.is_cold_start or entry.is_currently_available,
        }

        if entry.is_cold_start:
            cold_start_models.append(model_info)
        else:
            established_models.append(model_info)

    # Sort by exploration attempts (most explored first)
    cold_start_models.sort(key=lambda m: m["exploration_attempts"], reverse=True)
    established_models.sort(key=lambda m: m["sample_count"], reverse=True)

    return {
        "exploration_rate": settings.pipeline.exploration_rate,
        "cold_start_threshold": settings.pipeline.cold_start_threshold,
        "exploration_min_successes": settings.pipeline.exploration_min_successes,
        "cold_start_models": cold_start_models,
        "established_models": established_models,
        "total_tracked": len(all_entries),
    }


def _get_recommended_roles(suitability) -> list[str]:
    """Get recommended stage roles for a model based on suitability scores."""
    thresholds = {
        "generate": suitability.generate_score,
        "review": suitability.review_score,
        "critique": suitability.critique_score,
        "refine": suitability.refine_score,
        "verify": suitability.verify_score,
        "transform": suitability.transform_score,
    }

    sorted_roles = sorted(thresholds.items(), key=lambda x: x[1], reverse=True)
    return [role for role, score in sorted_roles[:2] if score >= 0.6]


def _resolve_transport(canonical_id: str) -> str:
    """Resolve transport type from canonical model ID."""
    if canonical_id.startswith("browser/"):
        return "browser"
    if canonical_id.startswith("agent/"):
        return "agent"
    if canonical_id.startswith("api/"):
        return "api"
    return "api"


@router.get("/admin/search/status")
async def get_search_status():
    """Get search system status.

    Returns:
        - Configuration (providers, settings)
        - Provider health status
        - Last errors from providers
        - Cache statistics
    """
    from app.core.config import settings
    from app.search.cache import page_cache, search_cache
    from app.search.router import search_router

    # Get provider health
    health = await search_router.health_check_all()

    # Get provider errors
    errors = search_router.get_provider_errors()

    # Get cache stats
    search_cache_stats = search_cache.stats
    page_cache_stats = page_cache.stats

    return {
        "enabled": settings.search.enabled,
        "config": {
            "providers": settings.search.providers.split(","),
            "searxng_base_url": settings.search.searxng_base_url,
            "timeout": settings.search.timeout,
            "max_results": settings.search.max_results,
            "max_queries": settings.search.max_queries,
            "fetch_max_pages": settings.search.fetch_max_pages,
        },
        "providers": {
            "configured": [p.provider_id for p in search_router.providers],
            "health": health,
            "errors": {
                provider: {"error_type": e.error_type, "message": e.message}
                for provider, e in errors.items()
            },
        },
        "cache": {
            "search": search_cache_stats,
            "page": page_cache_stats,
        },
    }
