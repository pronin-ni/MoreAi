import asyncio
import time
from typing import Any

from app.core.logging import get_logger
from app.integrations.adapters import (
    ClientBasedIntegration,
    OllamaFreeAPIIntegration,
    OpenAICompatibleIntegration,
    OpenRouterIntegration,
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
        self._adapters: dict[str, Any] = {}
        self._models: dict[str, ModelDefinition] = {}
        self._cooldowns: dict[str, float] = {}
        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Snapshot-based refresh: build new state separately, then atomically swap."""
        logger.info("Starting API registry refresh")
        async with self._lock:
            # Build new state in isolation — current state stays live during discovery
            new_adapters: dict[str, Any] = {}
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

    async def refresh_provider(self, integration_id: str) -> dict:
        """Re-run discovery for a single integration and merge results atomically.

        Returns a dict with: integration_id, model_count, status, error (if any).
        Does NOT affect other integrations — last-known-good preserved on failure.
        """
        if integration_id not in self._definitions:
            return {"integration_id": integration_id, "status": "not_found", "model_count": 0}

        definition = self._definitions[integration_id]
        config_snapshot = load_integrations_config([definition])
        runtime_config = config_snapshot.by_integration[integration_id]
        adapter_cls = self._adapter_class_for(definition)
        adapter = adapter_cls(definition, runtime_config)

        try:
            model_definitions = await adapter.discover_models()
        except Exception as exc:
            logger.warning(
                "Provider refresh failed — keeping last-known-good",
                integration_id=integration_id,
                error=str(exc),
            )
            # Update adapter status in existing adapters (if present)
            if integration_id in self._adapters:
                old_adapter = self._adapters[integration_id]
                old_adapter.status.last_refresh_status = "failed"
                old_adapter.status.last_refresh_error = str(exc)
                old_adapter.status.last_refresh_at = time.time()
            return {
                "integration_id": integration_id,
                "status": "failed",
                "error": str(exc),
                "model_count": len([m for m in self._models.values() if m.provider_id == integration_id]),
            }

        # Atomic merge: build new models dict for this provider only
        async with self._lock:
            old_models_for_provider = {
                mid: m for mid, m in self._models.items() if m.provider_id == integration_id
            }
            # Remove old models for this provider
            new_models = {mid: m for mid, m in self._models.items() if m.provider_id != integration_id}
            # Add new models
            for model_def in model_definitions:
                new_models[model_def.id] = model_def

            # Update adapter
            self._adapters[integration_id] = adapter
            self._models = new_models

        # Compute diff
        old_ids = set(old_models_for_provider.keys())
        new_ids = {m.id for m in model_definitions}
        added = sorted(new_ids - old_ids)
        removed = sorted(old_ids - new_ids)

        if added or removed:
            logger.info(
                "Provider model diff",
                integration_id=integration_id,
                added=str(added),
                removed=str(removed),
                total_models=str(len(new_ids)),
            )

        return {
            "integration_id": integration_id,
            "status": "ok",
            "model_count": len(model_definitions),
            "added": added,
            "removed": removed,
        }

    def get_provider_status(self) -> list[dict]:
        """Return per-provider status for the discovery admin endpoint."""
        statuses: list[dict] = []

        # Providers with active adapters
        for integration_id, adapter in self._adapters.items():
            status_data = adapter.diagnostics()
            status_data["model_count"] = len(
                [m for m in self._models.values() if m.provider_id == integration_id]
            )
            status_data["rate_limited"] = self.is_rate_limited(integration_id)
            statuses.append(status_data)

        # Providers defined but not yet initialized
        for integration_id in self._definitions:
            if integration_id not in self._adapters:
                definition = self._definitions[integration_id]
                statuses.append({
                    "integration_id": integration_id,
                    "display_name": definition.display_name,
                    "integration_type": definition.integration_type,
                    "source_type": definition.source_type,
                    "transport": "api",
                    "enabled": False,
                    "available": False,
                    "status": "not_initialized",
                    "model_count": 0,
                    "discovered_models": [],
                    "models_discovered_count": 0,
                    "last_refresh_status": "not_started",
                    "last_refresh_error": None,
                    "last_refresh_at": None,
                    "rate_limited": False,
                })

        statuses.sort(key=lambda s: s["display_name"].lower())
        return statuses

    def _adapter_class_for(self, definition: IntegrationDefinition):
        if definition.integration_id == "ollamafreeapi":
            return OllamaFreeAPIIntegration
        if definition.integration_id == "openrouter":
            return OpenRouterIntegration
        if definition.integration_type == "client_based":
            return ClientBasedIntegration
        return OpenAICompatibleIntegration


api_registry = APIRegistry()
