from dataclasses import dataclass, field
from typing import Literal

ApiKeyRequirement = Literal["none", "required", "unknown"]
IntegrationType = Literal["openai_compatible", "client_based"]
TransportType = Literal["browser", "api"]
SourceType = Literal["browser", "g4f_openai", "g4f_client", "external_api", "client_based"]
DefinitionGroup = Literal[
    "ready_to_use_base_url",
    "supported_api_route",
    "individual_client",
]


@dataclass(slots=True)
class IntegrationDefinition:
    integration_id: str
    display_name: str
    integration_type: IntegrationType
    group: DefinitionGroup
    source_type: SourceType
    base_url: str | None
    api_key_requirement: ApiKeyRequirement
    notes: str = ""
    enabled_by_default: bool = False
    supports_models_probe: bool = True
    fallback_models: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    default_timeout_seconds: int | None = None


@dataclass(slots=True)
class ModelDefinition:
    id: str
    provider_id: str
    transport: TransportType
    source_type: SourceType
    enabled: bool
    available: bool
    alias_ids: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class ResolvedModel:
    requested_id: str
    canonical_id: str
    provider_id: str
    transport: TransportType
    source_type: SourceType
    execution_strategy: str


@dataclass(slots=True)
class ParsedReadyToUseData:
    base_urls: list[dict[str, str]]
    supported_api_routes: list[dict[str, str]]
    individual_clients: list[dict[str, str]]


@dataclass(slots=True)
class IntegrationRuntimeConfig:
    enabled: bool
    base_url: str | None
    api_key: str | None
    api_key_source: str = "none"
    fallback_models: list[str] = field(default_factory=list)
    discover_models: bool = True
    timeout_seconds: int = 10
    retry_attempts: int = 1


@dataclass(slots=True)
class IntegrationStatus:
    integration_id: str
    display_name: str
    integration_type: IntegrationType
    source_type: SourceType
    transport: TransportType
    enabled: bool
    available: bool
    api_key_requirement: ApiKeyRequirement
    requires_api_key: bool
    models_probe_ok: bool
    disabled_reason: str | None
    base_url: str | None
    discovered_models: list[str] = field(default_factory=list)
    last_refresh_status: str = "not_started"
    last_refresh_error: str | None = None
    last_refresh_at: float | None = None
    models_discovered_count: int = 0
