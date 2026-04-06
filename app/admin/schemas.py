"""
Admin API Pydantic schemas for request/response validation.
"""

from typing import Any

from pydantic import BaseModel, Field

# ── Common ──


class SuccessResponse(BaseModel):
    ok: bool = True
    message: str


class ErrorResponse(BaseModel):
    error: str
    details: dict[str, Any] | None = None


# ── Provider ──


class ProviderPatch(BaseModel):
    enabled: bool | None = None
    concurrency_limit: int | None = Field(None, ge=1, le=100)
    priority: int | None = None


class ProviderEffectiveView(BaseModel):
    provider_id: str
    enabled: dict[str, Any]
    concurrency_limit: dict[str, Any] | None = None
    priority: dict[str, Any]
    override_state: str
    override_error: str | None = None
    override_applied_at: float | None = None


class ProviderStatusView(BaseModel):
    id: str
    display_name: str
    transport: str
    enabled: bool
    available: bool
    error: str | None
    model_count: int
    models: list[str]


# ── Model ──


class ModelPatch(BaseModel):
    enabled: bool | None = None
    visibility: str | None = None
    force_provider: str | None = None


class ModelEffectiveView(BaseModel):
    model_id: str
    enabled: dict[str, Any]
    visibility: dict[str, Any]
    force_provider: dict[str, Any] | None = None
    override_state: str
    override_error: str | None = None


class ModelStatusView(BaseModel):
    id: str
    canonical_id: str
    provider_id: str
    transport: str
    enabled: bool
    available: bool
    visibility: str
    source_kind: str | None = None


# ── Routing ──


class RoutingPatch(BaseModel):
    primary: str | None = None
    fallbacks: list[str] | None = None
    max_retries: int | None = Field(None, ge=0, le=10)
    timeout_override: int | None = Field(None, ge=1)


class RoutingEffectiveView(BaseModel):
    model_id: str
    primary: dict[str, Any]
    fallbacks: dict[str, Any]
    max_retries: dict[str, Any]
    timeout_override: dict[str, Any] | None = None


# ── Config / Effective ──


class EffectiveConfigView(BaseModel):
    version: int
    updated_at: float
    state: str
    providers: dict[str, dict[str, Any]]
    models: dict[str, dict[str, Any]]
    routing: dict[str, dict[str, Any]]
    field_policy: dict[str, dict[str, Any]]


# ── Rollback ──


class RollbackRequest(BaseModel):
    version: int | None = None


class RollbackHistoryEntry(BaseModel):
    version: int
    updated_at: float
    state: str
    provider_count: int
    model_count: int


# ── Actions ──


class AdminActionView(BaseModel):
    id: str
    display_name: str
    description: str
    category: str
    parameters: dict[str, Any]
    destructive: bool
    requires_restart: bool


class ActionExecutionResult(BaseModel):
    action_id: str
    status: str  # success | failed | partial
    details: dict[str, Any] = {}
    error: str | None = None
    duration_seconds: float | None = None


# ── Health / Status ──


class AdminHealthView(BaseModel):
    status: str = "healthy"
    config_version: int
    config_state: str
    admin_configured: bool


class SystemStatusView(BaseModel):
    version: str
    uptime_seconds: float
    config_version: int
    config_state: str
    provider_count: int
    model_count: int
    agent_model_count: int
    last_config_error: str | None
