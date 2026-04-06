"""
Enhanced diagnostics — aggregated operational visibility.

Provides structured diagnostics data for:
- Provider status (all transports)
- Registry snapshot
- Worker pool state
- Queue stats
- Config apply state
- Recent failures summary
- Routing decision info
"""

import time
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


# ── Recent failures tracker ──

_recent_failures: list[dict[str, Any]] = []
_MAX_RECENT_FAILURES = 50


def record_failure(
    model: str,
    provider: str,
    transport: str,
    error_type: str,
    error_message: str,
    is_fallback: bool = False,
    fallback_to: str | None = None,
) -> None:
    """Record a failure for diagnostics visibility."""
    entry = {
        "timestamp": time.monotonic(),
        "wall_time": time.time(),
        "model": model,
        "provider": provider,
        "transport": transport,
        "error_type": error_type,
        "error_message": error_message,
        "is_fallback": is_fallback,
        "fallback_to": fallback_to,
    }
    _recent_failures.append(entry)
    # Trim old entries
    while len(_recent_failures) > _MAX_RECENT_FAILURES:
        _recent_failures.pop(0)


def get_recent_failures(limit: int = 10) -> list[dict[str, Any]]:
    """Get most recent failures, newest first."""
    return sorted(_recent_failures, key=lambda x: x["timestamp"], reverse=True)[:limit]


# ── Routing decision tracker ──

_routing_decisions: list[dict[str, Any]] = []
_MAX_ROUTING_DECISIONS = 100


def record_routing_decision(
    model: str,
    selected_provider: str,
    transport: str,
    routing_rule: str,  # default, force_provider, primary, fallback
    fallbacks_tried: list[str] | None = None,
    candidates_rejected: list[dict[str, str]] | None = None,
) -> None:
    """Record a routing decision for diagnostics."""
    entry = {
        "timestamp": time.monotonic(),
        "wall_time": time.time(),
        "model": model,
        "selected_provider": selected_provider,
        "transport": transport,
        "routing_rule": routing_rule,
        "fallbacks_tried": fallbacks_tried or [],
        "candidates_rejected": candidates_rejected or [],
    }
    _routing_decisions.append(entry)
    while len(_routing_decisions) > _MAX_ROUTING_DECISIONS:
        _routing_decisions.pop(0)


def get_recent_routing_decisions(limit: int = 20) -> list[dict[str, Any]]:
    """Get recent routing decisions, newest first."""
    return sorted(_routing_decisions, key=lambda x: x["timestamp"], reverse=True)[:limit]


# ── Config apply history ──

_config_apply_history: list[dict[str, Any]] = []
_MAX_CONFIG_HISTORY = 20


def record_config_apply(
    version: int,
    status: str,
    duration_seconds: float,
    components: dict[str, str],
    error: str | None = None,
) -> None:
    """Record a config apply attempt."""
    entry = {
        "timestamp": time.monotonic(),
        "wall_time": time.time(),
        "version": version,
        "status": status,
        "duration_seconds": duration_seconds,
        "components": components,
        "error": error,
    }
    _config_apply_history.append(entry)
    while len(_config_apply_history) > _MAX_CONFIG_HISTORY:
        _config_apply_history.pop(0)


def get_config_apply_history(limit: int = 10) -> list[dict[str, Any]]:
    return sorted(_config_apply_history, key=lambda x: x["timestamp"], reverse=True)[:limit]


# ── Aggregated diagnostics ──


def get_provider_status() -> list[dict[str, Any]]:
    """Aggregated status of all providers across all transports."""
    result: list[dict[str, Any]] = []

    # Browser providers
    try:
        from app.browser.registry import registry as browser_registry
        from app.browser.execution.dispatcher import browser_dispatcher

        for model_data in browser_registry.list_models():
            pid = model_data["provider_id"]
            # Check circuit breaker state
            circuit_state = "unknown"
            try:
                from app.browser.execution.workers import ProviderHealthController
                # Access via dispatcher
                health_ctrl = browser_dispatcher._health_controller
                if health_ctrl:
                    provider_stats = health_ctrl._provider_stats.get(pid)
                    if provider_stats:
                        if provider_stats.circuit_open_until and provider_stats.circuit_open_until > time.monotonic():
                            circuit_state = "open"
                        else:
                            circuit_state = "closed"
            except Exception:
                pass

            result.append({
                "provider_id": pid,
                "transport": "browser",
                "model_id": model_data["id"],
                "enabled": model_data.get("enabled", True),
                "available": model_data.get("available", True),
                "circuit_state": circuit_state,
            })
    except Exception as exc:
        logger.warning("Failed to collect browser provider status", error=str(exc))

    # API providers
    try:
        from app.integrations.registry import api_registry

        for model_data in api_registry.list_models():
            pid = model_data["provider_id"]
            result.append({
                "provider_id": pid,
                "transport": "api",
                "model_id": model_data["id"],
                "enabled": model_data.get("enabled", True),
                "available": model_data.get("available", True),
                "circuit_state": "n/a",
            })
    except Exception as exc:
        logger.warning("Failed to collect API provider status", error=str(exc))

    # Agent providers
    try:
        from app.agents.registry import registry as agent_registry

        for model_data in agent_registry.list_models():
            pid = model_data["provider_id"]
            result.append({
                "provider_id": pid,
                "transport": "agent",
                "model_id": model_data["id"],
                "enabled": model_data.get("enabled", True),
                "available": model_data.get("available", True),
                "circuit_state": "n/a",
            })
    except Exception as exc:
        logger.warning("Failed to collect agent provider status", error=str(exc))

    return result


def get_registry_snapshot() -> dict[str, Any]:
    """Snapshot of all registries."""
    try:
        from app.registry.unified import unified_registry
        diag = unified_registry.diagnostics()
        return {
            "total_models": len(unified_registry.list_models()),
            "browser_models": len(diag.get("browser_models", [])),
            "api_models": len(diag.get("api_models", [])),
            "agent_models": len(diag.get("agent_models", [])),
            "api_initialized": diag.get("api_initialized", False),
            "agent_initialized": diag.get("agent_initialized", False),
        }
    except Exception as exc:
        return {"error": str(exc)}


def get_worker_pool_state() -> dict[str, Any]:
    """Browser worker pool state."""
    try:
        from app.browser.execution.dispatcher import browser_dispatcher
        snapshot = browser_dispatcher.diagnostics()
        return {
            "active_workers": snapshot.get("active_workers"),
            "total_workers": snapshot.get("worker_pool_size"),
            "queue_size": snapshot.get("queue_size"),
            "queue_capacity": snapshot.get("queue_capacity"),
            "in_flight": snapshot.get("in_flight"),
            "completed_jobs": snapshot.get("completed_jobs"),
            "failed_jobs": snapshot.get("failed_jobs"),
            "cancelled_jobs": snapshot.get("cancelled_jobs"),
            "retry_count": snapshot.get("retry_count"),
            "worker_restart_count": snapshot.get("worker_restart_count"),
            "queue_oldest_age_seconds": snapshot.get("queue_oldest_age_seconds"),
            "state": snapshot.get("state"),
        }
    except Exception as exc:
        return {"error": str(exc)}


def get_queue_stats() -> dict[str, Any]:
    """Detailed queue statistics."""
    try:
        from app.browser.execution.dispatcher import browser_dispatcher

        q = browser_dispatcher._queue
        return {
            "current_size": q.qsize(),
            "max_size": q.maxsize(),
            "is_full": q.closed(),
            "is_empty": q.qsize() == 0,
        }
    except Exception as exc:
        return {"error": str(exc)}


def get_config_apply_state() -> dict[str, Any]:
    """Config apply state and history."""
    try:
        from app.admin.config_manager import config_manager

        return {
            "current_version": config_manager.current_version,
            "state": config_manager.state,
            "error": config_manager.overrides.error,
            "rollback_available": config_manager.overrides.rollback_available,
            "provider_override_count": len(config_manager.overrides.providers),
            "model_override_count": len(config_manager.overrides.models),
            "routing_override_count": len(config_manager.overrides.routing),
            "recent_applies": get_config_apply_history(),
        }
    except Exception as exc:
        return {"error": str(exc)}


def get_degraded_components() -> list[str]:
    """List components that are degraded or unavailable."""
    degraded: list[str] = []

    # Browser dispatcher
    try:
        from app.browser.execution.dispatcher import browser_dispatcher
        snapshot = browser_dispatcher.diagnostics()
        if snapshot.get("active_workers", 0) == 0:
            degraded.append("browser_dispatcher: no active workers")
    except Exception as exc:
        degraded.append(f"browser_dispatcher: unavailable ({exc})")

    # API registry
    try:
        from app.integrations.registry import api_registry
        if not api_registry._initialized:
            degraded.append("api_registry: not initialized")
        elif not api_registry.discovered_models():
            degraded.append("api_registry: no models discovered")
    except Exception as exc:
        degraded.append(f"api_registry: unavailable ({exc})")

    # Agent registry
    try:
        from app.agents.registry import registry as agent_registry
        if not agent_registry._initialized:
            degraded.append("agent_registry: not initialized")
    except Exception as exc:
        degraded.append(f"agent_registry: unavailable ({exc})")

    return degraded


def get_full_diagnostics() -> dict[str, Any]:
    """Complete diagnostics dump."""
    return {
        "provider_status": get_provider_status(),
        "registry_snapshot": get_registry_snapshot(),
        "worker_pool": get_worker_pool_state(),
        "queue_stats": get_queue_stats(),
        "config_apply": get_config_apply_state(),
        "degraded_components": get_degraded_components(),
        "recent_failures": get_recent_failures(10),
        "recent_routing_decisions": get_recent_routing_decisions(20),
        "generated_at": time.time(),
    }


def get_routing_plan(model_id: str) -> dict[str, Any]:
    """Get the routing plan for a specific model."""
    try:
        from app.services.routing_engine import routing_engine

        plan = routing_engine.plan(model_id)
        return plan.summary()
    except Exception as exc:
        return {"error": str(exc)}
