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


# ── Sandbox ──


class SandboxRunRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=10000)
    model_id: str
    provider_id: str | None = None
    max_tokens: int = Field(default=4096, ge=1, le=32000)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)


class SandboxRunResponse(BaseModel):
    model_id: str
    provider_id: str
    transport: str
    status: str
    content: str | None = None
    latency_seconds: float = 0.0
    error: str | None = None
    error_type: str | None = None
    route_info: dict[str, Any] = {}


class SandboxCompareRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=10000)
    model_ids: list[str] = Field(..., min_length=1, max_length=10)
    max_tokens: int = Field(default=4096, ge=1, le=32000)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    parallel: bool = True


class SandboxCompareResponse(BaseModel):
    prompt: str
    results: list[SandboxRunResponse]
    total_duration_seconds: float
    fastest_provider: str | None = None
    successful_count: int
    failed_count: int


# ── Analytics ──


class AnalyticsUsageView(BaseModel):
    top_models: list[dict[str, Any]]
    top_providers: list[dict[str, Any]]
    error_summary: list[dict[str, Any]]
    fallback_summary: dict[str, Any]
    activity_timeline: list[dict[str, Any]]


# ── Canary ──


class CanaryRegisterRequest(BaseModel):
    model_id: str
    provider_id: str
    status: str = Field(default="draft")  # draft, canary, rollout, ga, deprecated
    traffic_percentage: float = Field(default=0.0, ge=0.0, le=100.0)
    error_threshold: float = Field(default=0.1, ge=0.0, le=1.0)
    notes: str = ""


class CanaryUpdateRequest(BaseModel):
    status: str | None = None
    traffic_percentage: float | None = Field(None, ge=0.0, le=100.0)
    error_threshold: float | None = Field(None, ge=0.0, le=1.0)
    notes: str | None = None


class CanaryView(BaseModel):
    model_id: str
    provider_id: str
    status: str
    traffic_percentage: float
    error_threshold: float
    started_at: float
    notes: str
    is_active: bool


# ── API Keys ──


class APIKeyCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    role: str = Field(default="user")
    quotas: dict[str, Any] = {}
    allowed_models: list[str] = []
    tenant_id: str = ""
    expires_at: float | None = None
    metadata: dict[str, Any] = {}


class APIKeyView(BaseModel):
    key_id: str
    name: str
    role: str
    quotas: dict[str, Any] = {}
    allowed_models: list[str] = []
    tenant_id: str
    created_at: float
    expires_at: float | None = None
    is_active: bool
    last_used_at: float | None = None
    metadata: dict[str, Any] = {}


class APIKeySecretResponse(BaseModel):
    key_info: APIKeyView
    secret: str  # Only shown once!


# ── Tenants ──


class TenantCreateRequest(BaseModel):
    tenant_id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=100)
    allowed_models: list[str] = []
    hidden_models: list[str] = []
    budget_monthly_tokens: int = 0
    budget_monthly_requests: int = 0
    metadata: dict[str, Any] = {}


class TenantUpdateRequest(BaseModel):
    name: str | None = None
    allowed_models: list[str] | None = None
    hidden_models: list[str] | None = None
    budget_monthly_tokens: int | None = None
    budget_monthly_requests: int | None = None
    metadata: dict[str, Any] | None = None


class TenantView(BaseModel):
    tenant_id: str
    name: str
    allowed_models: list[str]
    hidden_models: list[str]
    budget_monthly_tokens: int
    budget_monthly_requests: int
    created_at: float
    metadata: dict[str, Any]


# ── Webhooks ──


class WebhookConfigRequest(BaseModel):
    url: str = Field(..., min_length=1)
    events: list[str] = []
    secret: str | None = None
    max_retries: int = 3
    timeout_seconds: float = 10.0


class WebhookConfigView(BaseModel):
    url: str
    events: list[str]
    max_retries: int
    timeout_seconds: float
