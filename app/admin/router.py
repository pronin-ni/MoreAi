"""
Admin API router — /admin/* endpoints.

Auth: X-Admin-Token header with shared secret.
"""

import hashlib
import os
import time

import httpx
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
    AnalyticsUsageView,
    APIKeyCreateRequest,
    APIKeySecretResponse,
    APIKeyView,
    CanaryRegisterRequest,
    CanaryUpdateRequest,
    CanaryView,
    ModelPatch,
    ProviderPatch,
    RollbackHistoryEntry,
    RollbackRequest,
    RoutingPatch,
    SandboxCompareRequest,
    SandboxCompareResponse,
    SandboxRunRequest,
    SandboxRunResponse,
    SuccessResponse,
    SystemStatusView,
    TenantCreateRequest,
    TenantUpdateRequest,
    TenantView,
    WebhookConfigRequest,
    WebhookConfigView,
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


# ── Sandbox ──


@router.post("/sandbox/run", response_model=SandboxRunResponse)
async def sandbox_run(
    req: SandboxRunRequest,
    _=Depends(require_admin),
):
    """Execute a test prompt against a specific model."""
    from app.services.sandbox_service import sandbox_service

    result = await sandbox_service.run_prompt(
        prompt=req.prompt,
        model_id=req.model_id,
        provider_id=req.provider_id,
        max_tokens=req.max_tokens,
        temperature=req.temperature,
    )
    return SandboxRunResponse(**result.__dict__)


@router.post("/sandbox/compare", response_model=SandboxCompareResponse)
async def sandbox_compare(
    req: SandboxCompareRequest,
    _=Depends(require_admin),
):
    """Execute the same prompt against multiple models for comparison."""
    from app.services.sandbox_service import sandbox_service

    compare = await sandbox_service.compare_providers(
        prompt=req.prompt,
        model_ids=req.model_ids,
        max_tokens=req.max_tokens,
        temperature=req.temperature,
        parallel=req.parallel,
    )
    fastest = compare.fastest_result
    return SandboxCompareResponse(
        prompt=compare.prompt,
        results=[SandboxRunResponse(**r.__dict__) for r in compare.results],
        total_duration_seconds=compare.total_duration_seconds,
        fastest_provider=fastest.provider_id if fastest else None,
        successful_count=compare.successful_count,
        failed_count=compare.failed_count,
    )


@router.get("/sandbox/models")
async def sandbox_list_models(_=Depends(require_admin)):
    """List all models available for sandbox testing."""
    from app.services.sandbox_service import sandbox_service

    return sandbox_service.get_available_models()


# ── Analytics ──


@router.get("/analytics/usage", response_model=AnalyticsUsageView)
async def analytics_usage(_=Depends(require_admin)):
    """Get usage analytics summary."""
    from app.services.analytics_service import usage_analytics

    return AnalyticsUsageView(**usage_analytics.export_all())


@router.get("/analytics/top-models")
async def analytics_top_models(limit: int = 20, _=Depends(require_admin)):
    """Get top models by request count."""
    from app.services.analytics_service import usage_analytics

    return usage_analytics.top_models(limit)


@router.get("/analytics/top-providers")
async def analytics_top_providers(limit: int = 20, _=Depends(require_admin)):
    """Get top providers by request count."""
    from app.services.analytics_service import usage_analytics

    return usage_analytics.top_providers(limit)


@router.get("/analytics/errors")
async def analytics_errors(limit: int = 50, _=Depends(require_admin)):
    """Get error breakdown."""
    from app.services.analytics_service import usage_analytics

    return usage_analytics.error_summary(limit)


@router.get("/analytics/fallbacks")
async def analytics_fallbacks(_=Depends(require_admin)):
    """Get fallback statistics."""
    from app.services.analytics_service import usage_analytics

    return usage_analytics.fallback_summary()


@router.post("/analytics/reset")
async def analytics_reset(_=Depends(require_admin)):
    """Reset all analytics data."""
    from app.services.analytics_service import usage_analytics

    usage_analytics.reset()
    return SuccessResponse(message="Analytics data reset")


# ── Canary / Experimental ──


@router.get("/canary")
async def canary_list(_=Depends(require_admin)):
    """List all canary configurations."""
    from app.services.canary import canary_registry

    canaries = canary_registry.list_all()
    return [
        CanaryView(
            model_id=c.model_id,
            provider_id=c.provider_id,
            status=c.status.value,
            traffic_percentage=c.traffic_percentage,
            error_threshold=c.error_threshold,
            started_at=c.started_at,
            notes=c.notes,
            is_active=c.is_active(),
        )
        for c in canaries
    ]


@router.get("/canary/active")
async def canary_active(_=Depends(require_admin)):
    """List active canary configurations."""
    from app.services.canary import canary_registry

    canaries = canary_registry.list_active()
    return [
        CanaryView(
            model_id=c.model_id,
            provider_id=c.provider_id,
            status=c.status.value,
            traffic_percentage=c.traffic_percentage,
            error_threshold=c.error_threshold,
            started_at=c.started_at,
            notes=c.notes,
            is_active=c.is_active(),
        )
        for c in canaries
    ]


@router.get("/canary/history")
async def canary_history(model_id: str = "", limit: int = 50, _=Depends(require_admin)):
    """Get canary event history."""
    from app.services.canary import canary_registry

    mid = model_id if model_id else None
    return canary_registry.get_history(mid, limit)


@router.post("/canary", response_model=CanaryView)
async def canary_register(
    req: CanaryRegisterRequest,
    _=Depends(require_admin),
):
    """Register a new canary configuration."""
    from app.services.canary import CanaryStatus, canary_registry

    try:
        status = CanaryStatus(req.status)
    except ValueError:
        return JSONResponse(
            status_code=400,
            content={"error": f"Invalid status: {req.status}. Must be one of: {[s.value for s in CanaryStatus]}"},
        )

    config = canary_registry.register(
        model_id=req.model_id,
        provider_id=req.provider_id,
        status=status,
        traffic_percentage=req.traffic_percentage,
        error_threshold=req.error_threshold,
        notes=req.notes,
    )
    return CanaryView(
        model_id=config.model_id,
        provider_id=config.provider_id,
        status=config.status.value,
        traffic_percentage=config.traffic_percentage,
        error_threshold=config.error_threshold,
        started_at=config.started_at,
        notes=config.notes,
        is_active=config.is_active(),
    )


@router.patch("/canary/{model_id}", response_model=CanaryView)
async def canary_update(
    model_id: str,
    req: CanaryUpdateRequest,
    _=Depends(require_admin),
):
    """Update a canary configuration."""
    from app.services.canary import CanaryStatus, canary_registry

    status = CanaryStatus(req.status) if req.status else None
    config = canary_registry.update(
        model_id,
        status=status,
        traffic_percentage=req.traffic_percentage,
        error_threshold=req.error_threshold,
        notes=req.notes,
    )
    if config is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"Canary not found: {model_id}"},
        )
    return CanaryView(
        model_id=config.model_id,
        provider_id=config.provider_id,
        status=config.status.value,
        traffic_percentage=config.traffic_percentage,
        error_threshold=config.error_threshold,
        started_at=config.started_at,
        notes=config.notes,
        is_active=config.is_active(),
    )


@router.post("/canary/{model_id}/promote")
async def canary_promote_ga(model_id: str, _=Depends(require_admin)):
    """Promote a canary model to general availability."""
    from app.services.canary import canary_registry

    config = canary_registry.promote_to_ga(model_id)
    if config is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"Canary not found: {model_id}"},
        )
    return {
        "model_id": model_id,
        "status": "ga",
        "message": f"Promoted {model_id} to general availability",
    }


@router.post("/canary/{model_id}/rollback")
async def canary_rollback(model_id: str, _=Depends(require_admin)):
    """Rollback a canary model to draft status."""
    from app.services.canary import canary_registry

    config = canary_registry.rollback(model_id)
    if config is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"Canary not found: {model_id}"},
        )
    return {
        "model_id": model_id,
        "status": "draft",
        "message": f"Rolled back {model_id} to draft",
    }


@router.delete("/canary/{model_id}")
async def canary_remove(model_id: str, _=Depends(require_admin)):
    """Remove a canary configuration."""
    from app.services.canary import canary_registry

    removed = canary_registry.remove(model_id)
    if not removed:
        return JSONResponse(
            status_code=404,
            content={"error": f"Canary not found: {model_id}"},
        )
    return SuccessResponse(message=f"Canary removed for {model_id}")


# ── API Keys ──


@router.post("/api-keys", response_model=APIKeySecretResponse)
async def create_api_key(
    req: APIKeyCreateRequest,
    _=Depends(require_admin),
):
    """Create a new API key. Secret is returned once and never stored."""
    from app.services.access_policy import AccessRole, access_policy

    try:
        role = AccessRole(req.role)
    except ValueError:
        return JSONResponse(
            status_code=400,
            content={"error": f"Invalid role: {req.role}. Must be one of: {[r.value for r in AccessRole]}"},
        )

    key_info, secret = access_policy.create_key(
        name=req.name,
        role=role,
        quotas=req.quotas,
        allowed_models=req.allowed_models,
        tenant_id=req.tenant_id,
        expires_at=req.expires_at,
        metadata=req.metadata,
    )
    return APIKeySecretResponse(
        key_info=APIKeyView(
            key_id=key_info["key_id"],
            name=key_info["name"],
            role=key_info["role"],
            quotas=key_info["quotas"],
            allowed_models=key_info["allowed_models"],
            tenant_id=key_info["tenant_id"],
            created_at=key_info["created_at"],
            expires_at=key_info.get("expires_at"),
            is_active=key_info["is_active"],
            last_used_at=key_info.get("last_used_at"),
            metadata=key_info.get("metadata", {}),
        ),
        secret=secret,
    )


@router.get("/api-keys")
async def list_api_keys(tenant_id: str = "", _=Depends(require_admin)):
    """List all API keys (secrets are never returned)."""
    from app.services.access_policy import access_policy

    tid = tenant_id if tenant_id else None
    keys = access_policy.list_keys(tenant_id=tid)
    return [
        {
            "key_id": k["key_id"],
            "name": k["name"],
            "role": k["role"],
            "quotas": k["quotas"],
            "allowed_models": k["allowed_models"],
            "tenant_id": k.get("tenant_id", ""),
            "created_at": k["created_at"],
            "expires_at": k.get("expires_at"),
            "is_active": k["is_active"],
            "last_used_at": k.get("last_used_at"),
            "metadata": k.get("metadata", {}),
        }
        for k in keys
    ]


@router.get("/api-keys/{key_id}")
async def get_api_key(key_id: str, _=Depends(require_admin)):
    """Get a single API key (secret not included)."""
    from app.services.access_policy import access_policy

    key = access_policy.get_key(key_id)
    if key is None:
        return JSONResponse(status_code=404, content={"error": f"Key not found: {key_id}"})
    return {
        "key_id": key["key_id"],
        "name": key["name"],
        "role": key["role"],
        "quotas": key["quotas"],
        "allowed_models": key["allowed_models"],
        "tenant_id": key.get("tenant_id", ""),
        "created_at": key["created_at"],
        "expires_at": key.get("expires_at"),
        "is_active": key["is_active"],
        "last_used_at": key.get("last_used_at"),
        "metadata": key.get("metadata", {}),
    }


@router.get("/api-keys/{key_id}/usage")
async def get_api_key_usage(key_id: str, _=Depends(require_admin)):
    """Get usage statistics for an API key."""
    from app.services.access_policy import access_policy

    return access_policy.get_usage(key_id)


@router.patch("/api-keys/{key_id}")
async def update_api_key(key_id: str, updates: dict, _=Depends(require_admin)):
    """Update an API key (role, quotas, allowed_models, active status, etc.)."""
    from app.services.access_policy import AccessRole, access_policy

    if "role" in updates:
        try:
            updates["role"] = AccessRole(updates["role"]).value
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"error": f"Invalid role: {updates['role']}"},
            )

    result = access_policy.update_key(key_id, updates)
    if result is None:
        return JSONResponse(status_code=404, content={"error": f"Key not found: {key_id}"})
    return result


@router.delete("/api-keys/{key_id}")
async def delete_api_key(key_id: str, _=Depends(require_admin)):
    """Delete an API key."""
    from app.services.access_policy import access_policy

    if not access_policy.delete_key(key_id):
        return JSONResponse(status_code=404, content={"error": f"Key not found: {key_id}"})
    return SuccessResponse(message=f"API key deleted: {key_id}")


# ── Tenants ──


@router.post("/tenants", response_model=TenantView)
async def create_tenant(
    req: TenantCreateRequest,
    _=Depends(require_admin),
):
    """Create a new tenant."""
    from app.services.access_policy import access_policy

    tenant = access_policy.create_tenant(
        tenant_id=req.tenant_id,
        name=req.name,
        allowed_models=req.allowed_models,
        hidden_models=req.hidden_models,
        budget_monthly_tokens=req.budget_monthly_tokens,
        budget_monthly_requests=req.budget_monthly_requests,
        metadata=req.metadata,
    )
    return TenantView(**tenant)


@router.get("/tenants")
async def list_tenants(_=Depends(require_admin)):
    """List all tenants."""
    from app.services.access_policy import access_policy

    tenants = access_policy.list_tenants()
    return [
        TenantView(
            tenant_id=t["tenant_id"],
            name=t["name"],
            allowed_models=t["allowed_models"],
            hidden_models=t["hidden_models"],
            budget_monthly_tokens=t["budget_monthly_tokens"],
            budget_monthly_requests=t["budget_monthly_requests"],
            created_at=t["created_at"],
            metadata=t["metadata"],
        )
        for t in tenants
    ]


@router.get("/tenants/{tenant_id}")
async def get_tenant(tenant_id: str, _=Depends(require_admin)):
    """Get a single tenant."""
    from app.services.access_policy import access_policy

    tenant = access_policy.get_tenant(tenant_id)
    if tenant is None:
        return JSONResponse(status_code=404, content={"error": f"Tenant not found: {tenant_id}"})
    return TenantView(**tenant)


@router.patch("/tenants/{tenant_id}")
async def update_tenant(tenant_id: str, req: TenantUpdateRequest, _=Depends(require_admin)):
    """Update a tenant."""
    from app.services.access_policy import access_policy

    updates = req.model_dump(exclude_none=True)
    result = access_policy.update_tenant(tenant_id, updates)
    if result is None:
        return JSONResponse(status_code=404, content={"error": f"Tenant not found: {tenant_id}"})
    return TenantView(**result)


@router.delete("/tenants/{tenant_id}")
async def delete_tenant(tenant_id: str, _=Depends(require_admin)):
    """Delete a tenant."""
    from app.services.access_policy import access_policy

    if not access_policy.delete_tenant(tenant_id):
        return JSONResponse(status_code=404, content={"error": f"Tenant not found: {tenant_id}"})
    return SuccessResponse(message=f"Tenant deleted: {tenant_id}")


# ── Webhooks ──


@router.get("/webhooks")
async def list_webhooks(_=Depends(require_admin)):
    """List all registered webhooks."""
    from app.services.webhooks import webhook_dispatcher

    return webhook_dispatcher.list_webhooks()


@router.post("/webhooks")
async def register_webhook(req: WebhookConfigRequest, _=Depends(require_admin)):
    """Register a new webhook endpoint."""
    from app.services.webhooks import WebhookConfig, WebhookEvent, webhook_dispatcher

    events = set()
    for event_str in req.events:
        try:
            events.add(WebhookEvent(event_str))
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"error": f"Invalid event: {event_str}"},
            )

    config = WebhookConfig(
        url=req.url,
        events=events,
        secret=req.secret,
        max_retries=req.max_retries,
        timeout_seconds=req.timeout_seconds,
    )
    webhook_dispatcher.register_webhook(config)
    return WebhookConfigView(
        url=config.url,
        events=[e.value for e in config.events],
        max_retries=config.max_retries,
        timeout_seconds=config.timeout_seconds,
    )


@router.delete("/webhooks")
async def unregister_webhook(url: str, _=Depends(require_admin)):
    """Remove a webhook endpoint."""
    from app.services.webhooks import webhook_dispatcher

    removed = webhook_dispatcher.unregister_webhook(url)
    if not removed:
        return JSONResponse(status_code=404, content={"error": f"Webhook not found: {url}"})
    return SuccessResponse(message=f"Webhook removed: {url}")


@router.post("/webhooks/test")
async def test_webhook(url: str, _=Depends(require_admin)):
    """Send a test webhook to verify connectivity."""

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url,
                json={"event": "test", "timestamp": time.time(), "payload": {"message": "Test webhook"}},
                headers={"Content-Type": "application/json"},
            )
        return {
            "url": url,
            "status": response.status_code,
            "ok": response.status_code < 400,
        }
    except Exception as exc:
        return {"url": url, "error": str(exc), "ok": False}


# ── System Maintenance ──


@router.post("/maintenance/cleanup")
async def cleanup_old_data(
    analytics_max_age_days: int = 30,
    quota_max_age_days: int = 7,
    _=Depends(require_admin),
):
    """Clean up old analytics and quota data."""
    from app.services.analytics_service import usage_analytics

    analytics_cleaned = usage_analytics.cleanup(max_age_seconds=analytics_max_age_days * 86400)

    try:
        from app.core.persistent_store import persistent_store
        quota_cleaned = persistent_store.cleanup_old_quota_usage(max_age_seconds=quota_max_age_days * 86400)
    except Exception:
        quota_cleaned = 0

    return {
        "analytics_events_cleaned": analytics_cleaned,
        "quota_records_cleaned": quota_cleaned,
    }


# ── Healing Diagnostics ──


@router.get("/healing/stats")
async def healing_stats(provider_id: str = "", _=Depends(require_admin)):
    """Get self-healing selector statistics with health scores."""
    from app.browser.healing.health import health_aggregator
    from app.browser.healing.telemetry import healing_telemetry

    pid = provider_id if provider_id else None
    telemetry_stats = healing_telemetry.get_stats(provider_id=pid)
    health_scores = health_aggregator.get_all(provider_id=pid)

    # Merge health scores into telemetry stats
    for ts in telemetry_stats:
        health = health_aggregator.get(ts["provider_id"], ts["role"])
        if health:
            ts["health_score"] = health.health_score
            ts["status"] = health.status

    return {
        "per_role": telemetry_stats,
        "health_scores": [h.to_dict() for h in health_scores],
    }


@router.get("/healing/snapshot")
async def healing_snapshot(_=Depends(require_admin)):
    """Get complete healing telemetry snapshot with health scores."""
    from app.browser.healing.health import health_aggregator
    from app.browser.healing.runtime_cache import healing_cache
    from app.browser.healing.telemetry import healing_telemetry

    return {
        "telemetry": healing_telemetry.snapshot(),
        "health_scores": [h.to_dict() for h in health_aggregator.get_all()],
        "cache": healing_cache.snapshot(),
        "cache_size": healing_cache.size,
    }


@router.get("/healing/candidates")
async def healing_candidates(limit: int = 20, _=Depends(require_admin)):
    """Get recent healing candidates with insights."""
    from app.browser.healing.telemetry import healing_telemetry

    return healing_telemetry.get_recent_candidates(limit)


@router.get("/healing/cache")
async def healing_cache_snapshot(_=Depends(require_admin)):
    """Get current healed locator cache."""
    from app.browser.healing.runtime_cache import healing_cache

    return healing_cache.snapshot()


@router.post("/healing/cache/clear")
async def healing_cache_clear(_=Depends(require_admin)):
    """Clear the healed locator cache."""
    from app.browser.healing.runtime_cache import healing_cache

    healing_cache.clear()
    return SuccessResponse(message="Healing cache cleared")


@router.get("/healing/health")
async def healing_health_summary(provider_id: str = "", _=Depends(require_admin)):
    """Get selector health summary with status indicators."""
    from app.browser.healing.health import health_aggregator

    scores = health_aggregator.get_all(provider_id=provider_id if provider_id else None)

    # Group by status
    healthy = [s.to_dict() for s in scores if s.status == "healthy"]
    degrading = [s.to_dict() for s in scores if s.status == "degrading"]
    broken = [s.to_dict() for s in scores if s.status == "broken"]

    return {
        "summary": {
            "total": len(scores),
            "healthy": len(healthy),
            "degrading": len(degrading),
            "broken": len(broken),
        },
        "healthy": healthy,
        "degrading": degrading,
        "broken": broken,
        "all": [s.to_dict() for s in scores],
    }


# ── Recon Diagnostics ──


@router.get("/recon/stats")
async def recon_stats(provider_id: str = "", _=Depends(require_admin)):
    """Get recon recovery statistics."""
    from app.browser.recon.telemetry import recon_telemetry

    pid = provider_id if provider_id else None
    return recon_telemetry.get_stats(provider_id=pid)


@router.get("/recon/snapshot")
async def recon_snapshot(_=Depends(require_admin)):
    """Get full recon telemetry snapshot."""
    from app.browser.recon.telemetry import recon_telemetry

    return recon_telemetry.snapshot()


@router.get("/recon/events")
async def recon_events(limit: int = 20, _=Depends(require_admin)):
    """Get recent recon events."""
    from app.browser.recon.telemetry import recon_telemetry

    return recon_telemetry.get_recent_events(limit)


@router.post("/recon/clear")
async def recon_clear(_=Depends(require_admin)):
    """Clear recon telemetry data."""
    from app.browser.recon.telemetry import recon_telemetry

    recon_telemetry.clear()
    return SuccessResponse(message="Recon telemetry cleared")


@router.post("/recon/run/{provider_id}")
async def recon_manual_run(provider_id: str, _=Depends(require_admin)):
    """Manual recon trigger for diagnostics.

    Runs a controlled recon/health probe against the specified provider
    without an actual user request. Useful for diagnostics after UI changes.
    """
    import asyncio

    from app.browser.registry import registry as browser_registry
    from app.browser.recon.manager import ReconManager, ReconResult

    if provider_id not in browser_registry._providers:
        return JSONResponse(
            status_code=404,
            content={"error": f"Provider not found: {provider_id}"},
        )

    # This is a diagnostic-only tool — we can't run recon without a real page.
    # Instead, return the current policy + stats for this provider.
    from app.browser.recon.policy import recon_policy
    from app.browser.recon.telemetry import recon_telemetry

    stats = recon_telemetry.get_stats(provider_id=provider_id)
    policy = recon_policy.to_dict()

    return {
        "provider_id": provider_id,
        "note": "Manual recon requires a live browser page — returning current policy and stats instead",
        "policy": policy,
        "current_stats": stats,
        "health_aggregator": _get_provider_health_summary(provider_id),
    }


# ── DOM Baseline / Diff Diagnostics ──


@router.get("/dom-baseline/providers")
async def dom_baseline_providers(_=Depends(require_admin)):
    """List providers that have DOM baselines captured."""
    from app.browser.dom import baseline_store

    baselines = baseline_store.get_baselines()
    by_provider: dict[str, list[str]] = {}
    for b in baselines:
        if b.provider_id not in by_provider:
            by_provider[b.provider_id] = []
        by_provider[b.provider_id].append(b.role)

    return {
        "providers": by_provider,
        "total_baselines": len(baselines),
        "summary": baseline_store.summary(),
    }


@router.get("/dom-baseline/{provider_id}")
async def dom_baseline_provider(provider_id: str, role: str = "", _=Depends(require_admin)):
    """Get DOM baselines for a specific provider."""
    from app.browser.dom import baseline_store

    baselines = baseline_store.get_baselines(provider_id)
    if role:
        baselines = [b for b in baselines if b.role == role]

    return {
        "provider_id": provider_id,
        "baselines": [b.to_dict() for b in baselines],
    }


@router.get("/dom-diff/recent")
async def dom_diff_recent(limit: int = 20, _=Depends(require_admin)):
    """Get recent DOM drift events."""
    from app.browser.dom import baseline_store

    events = baseline_store.get_drift_events(limit=limit)
    return {
        "total_events": len(events),
        "events": [e.to_dict() for e in events],
    }


@router.post("/dom-baseline/clear/{provider_id}")
async def dom_baseline_clear(
    provider_id: str,
    role: str = "",
    _=Depends(require_admin),
):
    """Clear DOM baseline(s) for a provider."""
    from app.browser.dom import baseline_store

    removed = baseline_store.clear_baseline(provider_id, role if role else None)
    return SuccessResponse(
        message=f"Cleared {removed} baseline(s) for {provider_id}"
        + (f" role={role}" if role else "")
    )


@router.post("/dom-baseline/clear/all")
async def dom_baseline_clear_all(_=Depends(require_admin)):
    """Clear ALL DOM baselines and drift events."""
    from app.browser.dom import baseline_store
    from app.browser.dom.telemetry import dom_drift_telemetry

    baseline_store.clear_all()
    dom_drift_telemetry.clear()
    return SuccessResponse(message="All DOM baselines and drift events cleared")


@router.get("/dom-drift/stats")
async def dom_drift_stats(provider_id: str = "", _=Depends(require_admin)):
    """Get DOM drift statistics."""
    from app.browser.dom.telemetry import dom_drift_telemetry

    pid = provider_id if provider_id else None
    return {
        "snapshot": dom_drift_telemetry.snapshot(),
        "per_provider": dom_drift_telemetry.get_stats(pid),
        "recent_events": dom_drift_telemetry.get_recent_drift_events(),
    }


# ── Selector Maintenance ──


@router.get("/selector-suggestions")
async def list_suggestions(
    provider_id: str = "",
    status: str = "",
    _=Depends(require_admin),
):
    """List selector maintenance suggestions."""
    from app.browser.dom.suggestions import suggestion_engine

    pid = provider_id if provider_id else None
    st = status if status else None
    suggestions = suggestion_engine.get_pending_suggestions(provider_id=pid)
    if st and st != "pending":
        from app.browser.dom.persistent_store import persistent_dom_store
        suggestions = persistent_dom_store.get_suggestions(status=st, provider_id=pid)

    return {
        "count": len(suggestions),
        "suggestions": suggestions,
    }


@router.get("/selector-suggestions/{suggestion_id}")
async def get_suggestion(suggestion_id: int, _=Depends(require_admin)):
    """Get a single suggestion with details."""
    from app.browser.dom.persistent_store import persistent_dom_store

    suggestion = persistent_dom_store.get_suggestion(suggestion_id)
    if not suggestion:
        return JSONResponse(status_code=404, content={"error": "Suggestion not found"})

    # Get drift history for this provider+role
    from app.browser.dom.persistent_store import persistent_dom_store as store
    drift_events = store.get_drift_events(
        provider_id=suggestion["provider_id"],
        limit=10,
    )

    return {
        "suggestion": suggestion,
        "drift_history": drift_events,
    }


@router.post("/selector-suggestions/{suggestion_id}/approve")
async def approve_suggestion(
    suggestion_id: int,
    override_selector: str = "",
    _=Depends(require_admin),
):
    """Approve a suggestion and create an override."""
    from app.browser.dom.suggestions import suggestion_engine

    result = suggestion_engine.approve_suggestion(suggestion_id, override_selector)
    if not result:
        return JSONResponse(status_code=404, content={"error": "Suggestion not found"})

    return SuccessResponse(message=f"Suggestion {suggestion_id} approved")


@router.post("/selector-suggestions/{suggestion_id}/reject")
async def reject_suggestion(suggestion_id: int, _=Depends(require_admin)):
    """Reject a suggestion."""
    from app.browser.dom.persistent_store import persistent_dom_store

    result = persistent_dom_store.update_suggestion(suggestion_id, {"status": "rejected"})
    if not result:
        return JSONResponse(status_code=404, content={"error": "Suggestion not found"})

    return SuccessResponse(message=f"Suggestion {suggestion_id} rejected")


@router.post("/selector-suggestions/{suggestion_id}/dismiss")
async def dismiss_suggestion(suggestion_id: int, _=Depends(require_admin)):
    """Dismiss a suggestion."""
    from app.browser.dom.persistent_store import persistent_dom_store

    result = persistent_dom_store.update_suggestion(suggestion_id, {"status": "dismissed"})
    if not result:
        return JSONResponse(status_code=404, content={"error": "Suggestion not found"})

    return SuccessResponse(message=f"Suggestion {suggestion_id} dismissed")


@router.get("/selector-overrides")
async def list_overrides(provider_id: str = "", _=Depends(require_admin)):
    """List active selector overrides."""
    from app.browser.dom.overrides import selector_override_manager

    pid = provider_id if provider_id else None
    return selector_override_manager.get_all_overrides(pid)


@router.post("/selector-overrides/reset/{provider_id}")
async def reset_override(
    provider_id: str,
    role: str = "",
    _=Depends(require_admin),
):
    """Remove selector override(s) for a provider, reverting to base profile."""
    from app.browser.dom.overrides import selector_override_manager

    removed = selector_override_manager.reset_override(provider_id, role if role else None)
    return SuccessResponse(
        message=f"Reset {removed} override(s) for {provider_id}"
        + (f" role={role}" if role else "")
    )


def _get_provider_health_summary(provider_id: str) -> dict:
    """Get health summary for a provider."""
    try:
        from app.browser.healing.health import health_aggregator

        scores = health_aggregator.get_all(provider_id)
        return {
            "roles": [
                {
                    "role": s.role,
                    "health_score": s.health_score,
                    "status": s.status,
                    "recon_pressure": s.recon_pressure_score,
                    "recovered_recently": s.recovered_recently,
                }
                for s in scores
            ],
            "degradation": health_aggregator.get_provider_degradation(provider_id),
        }
    except Exception:
        return {}
