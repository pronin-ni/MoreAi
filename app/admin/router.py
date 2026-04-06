"""
Admin API router — /admin/* endpoints.

Auth: X-Admin-Token header with shared secret.
"""

import hashlib
import os
import time

import jinja2
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from app.admin.config_manager import config_manager
from app.admin.resolver import (
    resolve_all_effective,
    resolve_model_effective,
    resolve_provider_effective,
    resolve_routing_effective,
)
from app.admin.schemas import (
    AdminActionView,
    AdminHealthView,
    ModelPatch,
    ProviderPatch,
    RollbackHistoryEntry,
    RollbackRequest,
    RoutingPatch,
    SuccessResponse,
    SystemStatusView,
)
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

# ── Template ──


def _render_admin_page() -> str:
    """Render the admin SPA (Single Page App) template."""
    template_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(template_dir),
        autoescape=True,
    )
    template = env.get_template("admin.html")
    return template.render()


# ── Admin UI ──


@router.get("/", response_class=HTMLResponse)
async def admin_ui():
    """Serve the admin SPA — login + dashboard in one page."""
    return HTMLResponse(content=_render_admin_page())

# ── Auth ──


def _verify_admin_token(token: str) -> bool:
    """Verify admin token against configured secret."""
    admin_token = getattr(settings, "admin_token", None) or ""
    if not admin_token:
        return False  # No token configured — deny by default
    return hashlib.sha256(token.encode()).hexdigest() == hashlib.sha256(
        admin_token.encode()
    ).hexdigest()


async def require_admin(x_admin_token: str = Header(default="")):
    """Dependency that validates X-Admin-Token header."""
    if not x_admin_token:
        raise HTTPException(status_code=401, detail="Missing X-Admin-Token header")
    if not _verify_admin_token(x_admin_token):
        raise HTTPException(status_code=401, detail="Invalid admin token")


# ── Config ──


@router.get("/config/effective", response_model_exclude_none=True)
async def get_effective_config(_=Depends(require_admin)):
    """Get complete effective config with provenance (base + overrides)."""
    return resolve_all_effective()


@router.get("/config/history")
async def get_config_history(_=Depends(require_admin)):
    """Get historical config versions."""
    history = config_manager.get_history()
    return [RollbackHistoryEntry(**h) for h in history]


# ── Providers ──


@router.get("/providers")
async def list_providers(_=Depends(require_admin)):
    """List all providers with admin overrides and effective config."""

    all_providers = config_manager._known_providers or set()
    result = {}
    for pid in sorted(all_providers):
        effective = resolve_provider_effective(pid)
        result[pid] = effective

    return result


@router.get("/providers/{provider_id}/effective")
async def get_provider_effective(provider_id: str, _=Depends(require_admin)):
    """Get effective config for a single provider with provenance."""

    return resolve_provider_effective(provider_id)


@router.patch("/providers/{provider_id}")
async def update_provider(
    provider_id: str,
    patch: ProviderPatch,
    _=Depends(require_admin),
):
    """Update provider override (enable/disable, concurrency, priority)."""
    override = await config_manager.update_provider(
        provider_id,
        patch.model_dump(exclude_none=True),
    )
    return {"provider_id": provider_id, "override": override.to_dict()}


@router.delete("/providers/{provider_id}/override")
async def reset_provider_override(
    provider_id: str,
    _=Depends(require_admin),
):
    """Remove provider override → inherit from base."""
    await config_manager.reset_override("provider", provider_id)
    return SuccessResponse(message=f"Provider override reset for {provider_id}")


@router.post("/providers/{provider_id}/healthcheck")
async def healthcheck_provider(
    provider_id: str,
    _=Depends(require_admin),
):
    """Force immediate healthcheck for a provider."""
    from app.agents.registry import registry as agent_registry
    from app.browser.registry import registry as browser_registry
    from app.integrations.registry import api_registry

    if provider_id in browser_registry._providers:
        return {"provider_id": provider_id, "status": "browser — healthcheck via browser pool"}
    elif provider_id in api_registry._adapters:
        adapter = api_registry._adapters[provider_id]
        return {
            "provider_id": provider_id,
            "status": adapter.status.last_refresh_status,
            "available": adapter.status.available,
        }
    elif provider_id in agent_registry._providers:
        provider = agent_registry._providers[provider_id]
        available = await provider.is_available()
        return {"provider_id": provider_id, "available": available}
    else:
        return JSONResponse(
            status_code=404,
            content={"error": f"Provider not found: {provider_id}"},
        )


# ── Models ──


@router.get("/models")
async def list_models(q: str = "", _=Depends(require_admin)):
    """List all models with admin overrides and effective config."""
    from app.admin.config_manager import config_manager
    from app.agents.registry import registry as agent_registry
    from app.browser.registry import registry as browser_registry
    from app.integrations.registry import api_registry

    all_models = []

    # Browser models
    for m in browser_registry.list_models():
        model_id = m["id"]
        override = config_manager.overrides.models.get(model_id)
        enabled = override.enabled if override and override.enabled is not None else m["enabled"]
        visibility = override.visibility if override and override.visibility else "public"
        all_models.append(
            {
                "id": model_id,
                "canonical_id": model_id,
                "provider_id": m["provider_id"],
                "transport": m["transport"],
                "enabled": enabled,
                "available": m["available"],
                "visibility": visibility,
            }
        )

    # API models
    for m in api_registry.list_models():
        model_id = m["id"]
        override = config_manager.overrides.models.get(model_id)
        enabled = override.enabled if override and override.enabled is not None else m["enabled"]
        visibility = override.visibility if override and override.visibility else "public"
        all_models.append(
            {
                "id": model_id,
                "canonical_id": model_id,
                "provider_id": m["provider_id"],
                "transport": m["transport"],
                "enabled": enabled,
                "available": m["available"],
                "visibility": visibility,
            }
        )

    # Agent models
    for m in agent_registry.list_models():
        model_id = m["id"]
        override = config_manager.overrides.models.get(model_id)
        enabled = override.enabled if override and override.enabled is not None else m["enabled"]
        visibility = override.visibility if override and override.visibility else "public"
        all_models.append(
            {
                "id": model_id,
                "canonical_id": model_id,
                "provider_id": m["provider_id"],
                "transport": m["transport"],
                "enabled": enabled,
                "available": m["available"],
                "visibility": visibility,
                "source_kind": m.get("metadata", {}).get("source_kind"),
            }
        )

    if q:
        q_lower = q.lower()
        all_models = [
            m
            for m in all_models
            if q_lower in m["id"].lower() or q_lower in m["provider_id"].lower()
        ]

    all_models.sort(key=lambda m: m["id"])
    return all_models


@router.get("/models/{model_id:path}/effective")
async def get_model_effective(model_id: str, _=Depends(require_admin)):
    """Get effective config for a single model with provenance."""
    return resolve_model_effective(model_id)


@router.patch("/models/{model_id:path}")
async def update_model(
    model_id: str,
    patch: ModelPatch,
    _=Depends(require_admin),
):
    """Update model override (enable/disable, visibility, force_provider)."""
    override = await config_manager.update_model(
        model_id,
        patch.model_dump(exclude_none=True),
    )
    return {"model_id": model_id, "override": override.to_dict()}


@router.delete("/models/{model_id:path}/override")
async def reset_model_override(
    model_id: str,
    _=Depends(require_admin),
):
    """Remove model override → inherit from base."""
    await config_manager.reset_override("model", model_id)
    return SuccessResponse(message=f"Model override reset for {model_id}")


# ── Routing ──


@router.get("/routing")
async def list_routing(_=Depends(require_admin)):
    """List all routing overrides."""
    result = {}
    for mid in config_manager.overrides.routing:
        result[mid] = resolve_routing_effective(mid)
    return result


@router.get("/routing/{model_id}/effective")
async def get_routing_effective(model_id: str, _=Depends(require_admin)):
    """Get effective routing config for a single model."""
    return resolve_routing_effective(model_id)


@router.patch("/routing/{model_id}")
async def update_routing(
    model_id: str,
    patch: RoutingPatch,
    _=Depends(require_admin),
):
    """Update routing override for a model."""
    override = await config_manager.update_routing(
        model_id,
        patch.model_dump(exclude_none=True),
    )
    return {"model_id": model_id, "override": override.to_dict()}


@router.delete("/routing/{model_id}")
async def reset_routing(
    model_id: str,
    _=Depends(require_admin),
):
    """Remove routing override for a model."""
    await config_manager.reset_override("routing", model_id)
    return SuccessResponse(message=f"Routing override reset for {model_id}")


# ── Rollback ──


@router.post("/rollback")
async def rollback_config(
    req: RollbackRequest | None = None,
    _=Depends(require_admin),
):
    """Rollback config to previous version."""
    version = req.version if req else None
    overrides = await config_manager.rollback(version)
    return {"version": overrides.version, "state": overrides.state}


@router.post("/rollback/last")
async def rollback_last_config(_=Depends(require_admin)):
    """Rollback to immediately previous version."""
    overrides = await config_manager.rollback()
    return {"version": overrides.version, "state": overrides.state}


# ── Actions ──


@router.get("/actions")
async def list_actions(_=Depends(require_admin)):
    """List available operational actions."""
    actions = [
        AdminActionView(
            id="provider/refresh-discovery",
            display_name="Refresh Discovery",
            description="Re-run model discovery for all providers",
            category="provider",
            parameters={},
            destructive=False,
            requires_restart=False,
        ),
        AdminActionView(
            id="agent/opencode/restart",
            display_name="Restart OpenCode",
            description="Restart managed OpenCode subprocess",
            category="agent",
            parameters={},
            destructive=True,
            requires_restart=False,
        ),
        AdminActionView(
            id="cache/clear",
            display_name="Clear Runtime Cache",
            description="Clear all runtime caches",
            category="cache",
            parameters={},
            destructive=False,
            requires_restart=False,
        ),
    ]
    return actions


@router.post("/actions/{action_id}")
async def execute_action(action_id: str, _=Depends(require_admin)):
    """Execute an operational action."""
    start = time.monotonic()

    match action_id:
        case "provider/refresh-discovery":
            # Re-initialize registries
            from app.registry.unified import unified_registry

            await unified_registry.initialize()
            return {
                "action_id": action_id,
                "status": "success",
                "details": {"message": "All registries re-initialized"},
                "duration_seconds": time.monotonic() - start,
            }

        case "agent/opencode/restart":
            from app.agents.opencode.provider import provider as opencode_provider

            await opencode_provider.shutdown()
            await opencode_provider.initialize()
            return {
                "action_id": action_id,
                "status": "success",
                "details": {
                    "available": opencode_provider._available,
                    "model_count": len(opencode_provider._models),
                },
                "duration_seconds": time.monotonic() - start,
            }

        case "cache/clear":
            # No explicit cache to clear yet — noop
            return {
                "action_id": action_id,
                "status": "success",
                "details": {"message": "No runtime caches to clear"},
                "duration_seconds": time.monotonic() - start,
            }

        case _:
            return JSONResponse(
                status_code=404,
                content={"error": f"Unknown action: {action_id}"},
            )


# ── Health / Status ──


@router.get("/health", response_model=AdminHealthView)
async def admin_health(_=Depends(require_admin)):
    """Admin health check."""
    return AdminHealthView(
        config_version=config_manager.current_version,
        config_state=config_manager.state,
        admin_configured=True,
    )


@router.get("/status", response_model=SystemStatusView)
async def system_status(_=Depends(require_admin)):
    """System status overview."""
    from app.agents.registry import registry as agent_registry
    from app.browser.registry import registry as browser_registry
    from app.integrations.registry import api_registry

    return SystemStatusView(
        version="0.1.0",
        uptime_seconds=time.monotonic(),  # approximate
        config_version=config_manager.current_version,
        config_state=config_manager.state,
        provider_count=len(browser_registry._providers) + len(api_registry._adapters),
        model_count=len(browser_registry.list_models()) + len(api_registry.list_models()),
        agent_model_count=len(agent_registry.list_models()),
        last_config_error=config_manager.overrides.error,
    )
