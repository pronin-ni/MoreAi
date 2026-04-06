import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from app.agents.base import AgentProvider

if TYPE_CHECKING:
    pass


SourceKind = Literal["zen", "bundled_free", "plugin", "external_provider", "unknown"]


@dataclass(slots=True)
class AgentModelDefinition:
    id: str
    provider_id: str
    transport: str = "agent"
    source_type: str = "opencode_server"
    enabled: bool = True
    available: bool = True
    metadata: dict[str, Any] = None

    # Classification metadata
    discovered_from_provider: str = ""
    requires_auth: bool = False
    provider_connected: bool = False
    source_kind: SourceKind = "unknown"
    is_runtime_available: bool = False

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class AgentRegistry:
    """Registry for agent-based providers (e.g., OpenCode server)."""

    def __init__(self):
        self._providers: dict[str, AgentProvider] = {}
        self._models: dict[str, AgentModelDefinition] = {}
        self._initialized = False
        self._pending_providers: list[AgentProvider] = []
        self._lock = asyncio.Lock()

    def register(self, provider: AgentProvider, models: list[AgentModelDefinition]) -> None:
        self._providers[provider.provider_id] = provider
        for model in models:
            self._models[model.id] = model

    def register_pending(self, provider: AgentProvider) -> None:
        """Register a provider that will self-register models after initialization."""
        self._pending_providers.append(provider)

    async def initialize(self) -> None:
        """Initialize all registered and pending providers."""
        async with self._lock:
            # Initialize pending providers (they will self-register models)
            for provider in self._pending_providers:
                await provider.initialize()
            self._initialized = True

    def list_models(self) -> list[dict]:
        # Snapshot _models for consistent read during concurrent refresh
        models_snapshot = dict(self._models)
        return [
            {
                "id": model.id,
                "provider_id": model.provider_id,
                "transport": model.transport,
                "source_type": model.source_type,
                "enabled": model.enabled,
                "available": model.available,
                **model.metadata,
            }
            for model in models_snapshot.values()
        ]

    def can_resolve_model(self, model_name: str) -> bool:
        return model_name in self._models

    def resolve_model(self, model_name: str) -> dict:
        if model_name not in self._models:
            from app.core.errors import BadRequestError

            raise BadRequestError(
                f"Unknown agent model: {model_name}",
                details={
                    "requested_model": model_name,
                    "available_models": sorted(self._models.keys()),
                },
            )
        model = self._models[model_name]
        return {
            "requested_id": model_name,
            "canonical_id": model.id,
            "provider_id": model.provider_id,
            "transport": model.transport,
            "source_type": model.source_type,
            "execution_strategy": "agent_completion",
        }

    def get_provider(self, provider_id: str) -> AgentProvider:
        if provider_id not in self._providers:
            from app.core.errors import InternalError

            raise InternalError(f"Agent provider not found: {provider_id}")
        return self._providers[provider_id]

    def diagnostics(self) -> dict[str, Any]:
        return {
            "providers": {
                pid: provider.diagnostics() for pid, provider in self._providers.items()
            },
            "models": self.list_models(),
        }


registry = AgentRegistry()
