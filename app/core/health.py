"""
Health probes — /live, /ready, /health.

/live  — process is alive (always 200 if we respond)
/ready — service is ready to accept traffic (checks all runtime components)
/health — extended health/status with per-component detail
"""

import time

from app.core.logging import get_logger

logger = get_logger(__name__)

# ── Component health checks ──


def _check_browser_dispatcher() -> dict:
    """Check that the browser dispatcher is operational."""
    try:
        from app.browser.execution.dispatcher import browser_dispatcher

        snapshot = browser_dispatcher.get_health_snapshot()
        return {
            "status": "healthy" if snapshot.active_workers > 0 else "degraded",
            "active_workers": snapshot.active_workers,
            "queue_size": snapshot.queue_size,
            "queue_capacity": snapshot.queue_capacity,
            "in_flight": snapshot.in_flight,
            "failed_jobs": snapshot.failed_jobs,
            "retry_count": snapshot.retry_count,
        }
    except Exception as exc:
        logger.warning("Browser dispatcher health check failed", error=str(exc))
        return {"status": "unavailable", "error": str(exc)}


def _check_api_registry() -> dict:
    """Check that the API registry is initialized and has models."""
    try:
        from app.integrations.registry import api_registry

        if not api_registry._initialized:
            return {"status": "initializing"}
        models = api_registry.discovered_models()
        return {
            "status": "healthy" if models else "degraded",
            "model_count": len(models),
        }
    except Exception as exc:
        logger.warning("API registry health check failed", error=str(exc))
        return {"status": "unavailable", "error": str(exc)}


def _check_agent_registry() -> dict:
    """Check that the agent registry is initialized."""
    try:
        from app.agents.registry import registry as agent_registry

        if not agent_registry._initialized:
            return {"status": "initializing"}
        models = agent_registry.list_models()
        return {
            "status": "healthy" if models else "degraded",
            "model_count": len(models),
        }
    except Exception as exc:
        logger.warning("Agent registry health check failed", error=str(exc))
        return {"status": "unavailable", "error": str(exc)}


def _check_config_apply() -> dict:
    """Check that the config manager and applier are operational."""
    try:
        from app.admin.config_manager import config_manager

        return {
            "status": "healthy",
            "version": config_manager.current_version,
            "state": config_manager.state,
        }
    except Exception as exc:
        logger.warning("Config manager health check failed", error=str(exc))
        return {"status": "unavailable", "error": str(exc)}


# ── Public API ──


def live_probe() -> dict:
    """Liveness probe — process is alive."""
    return {"status": "alive"}


def ready_probe() -> dict:
    """Readiness probe — all runtime components are ready."""
    components = {
        "browser_dispatcher": _check_browser_dispatcher(),
        "api_registry": _check_api_registry(),
        "agent_registry": _check_agent_registry(),
        "config_apply": _check_config_apply(),
    }

    # Ready if no component is "unavailable"
    overall_ready = all(
        c["status"] not in ("unavailable",) for c in components.values()
    )

    return {
        "ready": overall_ready,
        "components": components,
    }


def health_status() -> dict:
    """Extended health with per-component detail and uptime."""
    components = {
        "browser_dispatcher": _check_browser_dispatcher(),
        "api_registry": _check_api_registry(),
        "agent_registry": _check_agent_registry(),
        "config_apply": _check_config_apply(),
    }

    # Compute overall health
    unavailable = [k for k, v in components.items() if v["status"] == "unavailable"]
    degraded = [k for k, v in components.items() if v["status"] == "degraded"]

    if unavailable:
        overall = "unhealthy"
    elif degraded:
        overall = "degraded"
    else:
        overall = "healthy"

    return {
        "status": overall,
        "version": "0.1.0",
        "uptime_seconds": time.monotonic(),
        "components": components,
        "summary": {
            "unavailable_count": len(unavailable),
            "degraded_count": len(degraded),
            "unavailable": unavailable,
            "degraded": degraded,
        },
    }
