import asyncio
import time

from app.core.logging import get_logger
from app.integrations.adapters import (
    ClientBasedIntegration,
    OllamaFreeAPIIntegration,
    OpenAICompatibleIntegration,
)
from app.integrations.config import load_integrations_config
from app.integrations.definitions import READY_TO_USE_DEFINITIONS
from app.integrations.types import IntegrationDefinition, ModelDefinition, ResolvedModel

logger = get_logger(__name__)


class APIRegistry:
    def __init__(self):
        self._definitions: dict[str, IntegrationDefinition] = {
            definition.integration_id: definition for definition in READY_TO_USE_DEFINITIONS
        }
        self._adapters: dict[str, OpenAICompatibleIntegration] = {}
        self._models: dict[str, ModelDefinition] = {}
        self._cooldowns: dict[str, float] = {}
        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Snapshot-based refresh: build new state separately, then atomically swap."""
        logger.info("Starting API registry refresh")
        async with self._lock:
            # Build new state in isolation — current state stays live during discovery
            new_adapters: dict[str, OpenAICompatibleIntegration] = {}
            new_models: dict[str, ModelDefinition] = {}

            config_snapshot = load_integrations_config(list(self._definitions.values()))

            for definition in self._definitions.values():
                runtime_config = config_snapshot.by_integration[definition.integration_id]
                adapter_cls = self._adapter_class_for(definition)
                adapter = adapter_cls(definition, runtime_config)
                new_adapters[definition.integration_id] = adapter

                for model_definition in await adapter.discover_models():
                    new_models[model_definition.id] = model_definition

            # Preserve cooldowns from current state (rate limits survive refresh)
            new_cooldowns = dict(self._cooldowns)

            # Atomic swap — readers always see a complete, consistent snapshot
            old_model_count = len(self._models)
            self._adapters = new_adapters
            self._models = new_models
            self._cooldowns = new_cooldowns
            self._initialized = True

        logger.info(
            "API registry refresh complete",
            integrations=len(self._adapters),
            models=len(self._models),
            previous_models=old_model_count,
        )

    def list_models(self) -> list[dict]:
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
            for model in self._models.values()
        ]

    def can_resolve_model(self, model_name: str) -> bool:
        return model_name in self._models

    def resolve_model(self, model_name: str) -> ResolvedModel:
        if model_name not in self._models:
            from app.core.errors import BadRequestError

            raise BadRequestError(
                f"Unknown model: {model_name}",
                details={
                    "requested_model": model_name,
                    "available_models": sorted(self._models.keys()),
                },
            )
        model = self._models[model_name]
        return ResolvedModel(
            requested_id=model_name,
            canonical_id=model.id,
            provider_id=model.provider_id,
            transport=model.transport,
            source_type=model.source_type,
            execution_strategy="api_completion",
        )

    def get_adapter(self, provider_id: str) -> OpenAICompatibleIntegration:
        return self._adapters[provider_id]

    def mark_rate_limited(self, provider_id: str, cooldown_seconds: int) -> None:
        self._cooldowns[provider_id] = time.monotonic() + cooldown_seconds

    def is_rate_limited(self, provider_id: str) -> bool:
        until = self._cooldowns.get(provider_id)
        if until is None:
            return False
        if until <= time.monotonic():
            self._cooldowns.pop(provider_id, None)
            return False
        return True

    def find_fallback_model(
        self,
        canonical_model_id: str,
        exclude_provider_id: str,
    ) -> ResolvedModel | None:
        # Snapshot _models to avoid iteration during concurrent refresh
        models_snapshot = dict(self._models)
        upstream_model = canonical_model_id.split("/", 2)[-1]
        candidates: list[ModelDefinition] = []
        for model in models_snapshot.values():
            if model.provider_id == exclude_provider_id:
                continue
            if not model.enabled or not model.available:
                continue
            if self.is_rate_limited(model.provider_id):
                continue
            if model.id.split("/", 2)[-1] != upstream_model:
                continue
            candidates.append(model)

        candidates.sort(key=lambda item: (0 if item.provider_id == "g4f-hosted" else 1, item.id))
        if not candidates:
            return None

        chosen = candidates[0]
        return ResolvedModel(
            requested_id=canonical_model_id,
            canonical_id=chosen.id,
            provider_id=chosen.provider_id,
            transport=chosen.transport,
            source_type=chosen.source_type,
            execution_strategy="api_completion",
        )

    def diagnostics(self) -> list[dict]:
        diagnostics: list[dict] = []
        for adapter in self._adapters.values():
            item = adapter.diagnostics()
            item["rate_limited"] = self.is_rate_limited(adapter.definition.integration_id)
            diagnostics.append(item)
        return diagnostics

    def discovered_models(self) -> list[str]:
        return sorted(self._models.keys())

    def _adapter_class_for(self, definition: IntegrationDefinition):
        if definition.integration_id == "ollamafreeapi":
            return OllamaFreeAPIIntegration
        if definition.integration_type == "client_based":
            return ClientBasedIntegration
        return OpenAICompatibleIntegration


api_registry = APIRegistry()
