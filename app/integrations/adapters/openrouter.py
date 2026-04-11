"""
OpenRouter API integration adapter.

Extends OpenAICompatibleIntegration with:
- Free model filtering (runtime detection via :free suffix)
- Special openrouter/free router model injection
- Free-only mode diagnostics
- Graceful degradation when API key is missing

Namespace: api/openrouter/<model_id>
Examples:
  - api/openrouter/openrouter/free
  - api/openrouter/meta-llama/llama-3.2-3b-instruct:free
  - api/openrouter/deepseek/deepseek-r1:free
"""

from __future__ import annotations

import httpx

from app.core.errors import ServiceUnavailableError
from app.core.logging import get_logger
from app.integrations.types import ModelDefinition

logger = get_logger(__name__)

# Special router model ID (upstream, not canonical)
OPENROUTER_FREE_ROUTER_MODEL = "openrouter/free"


class OpenRouterIntegration:
    """OpenRouter API adapter with free model filtering.

    This class uses composition with OpenAICompatibleIntegration behavior.
    It is instantiated by the registry and must implement the same interface
    as BaseIntegrationAdapter subclasses.

    Uses the OpenAI-compatible /chat/completions endpoint for completions
    and /models endpoint for live model discovery.
    """

    transport = "api"

    def __init__(self, definition, runtime_config):
        self.definition = definition
        self.runtime_config = runtime_config
        # Initialize status like BaseIntegrationAdapter
        from app.integrations.types import IntegrationStatus
        self.status = IntegrationStatus(
            integration_id=definition.integration_id,
            display_name=definition.display_name,
            integration_type=definition.integration_type,
            source_type=definition.source_type,
            transport="api",
            enabled=runtime_config.enabled,
            available=False,
            api_key_requirement=definition.api_key_requirement,
            requires_api_key=definition.api_key_requirement == "required",
            models_probe_ok=False,
            disabled_reason=None,
            base_url=runtime_config.base_url,
            discovered_models=[],
            last_refresh_status="not_started",
            last_refresh_error=None,
            last_refresh_at=None,
            models_discovered_count=0,
        )

    # --- Base adapter methods ---

    def _is_configured(self) -> bool:
        if not self.runtime_config.enabled:
            self.status.disabled_reason = "disabled_by_config"
            return False
        if not self.runtime_config.base_url:
            self.status.disabled_reason = "missing_base_url"
            return False
        if self.definition.api_key_requirement == "required" and not self.runtime_config.api_key:
            self.status.disabled_reason = "missing_api_key"
            return False
        return True

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.runtime_config.api_key:
            headers["Authorization"] = f"Bearer {self.runtime_config.api_key}"
        return headers

    def _canonical_model_id(self, model_name: str) -> str:
        return f"api/{self.definition.integration_id}/{model_name}"

    def _extract_upstream_model(self, canonical_model_id: str) -> str:
        prefix = f"api/{self.definition.integration_id}/"
        if canonical_model_id.startswith(prefix):
            return canonical_model_id[len(prefix):]
        return canonical_model_id

    def _set_refresh_status(self, status: str, *, error: str | None = None, discovered_models: list[str] | None = None) -> None:
        import time as _time
        self.status.last_refresh_status = status
        self.status.last_refresh_error = error
        self.status.last_refresh_at = _time.time()
        if discovered_models is not None:
            self.status.discovered_models = discovered_models
            self.status.models_discovered_count = len(discovered_models)

    def _is_free_model(self, model_id: str) -> bool:
        """Determine if a model is free based on runtime catalog heuristics."""
        return model_id.endswith(":free")

    def _build_model_definitions(
        self,
        model_names: list[str],
        *,
        available: bool,
        fallback: bool = False,
        only_free: bool = False,
        metadata_by_model: dict[str, dict[str, object]] | None = None,
    ) -> list[ModelDefinition]:
        """Build ModelDefinition objects with free model metadata."""
        metadata_by_model = metadata_by_model or {}
        result: list[ModelDefinition] = []

        for model_name in model_names:
            is_free = self._is_free_model(model_name) or model_name == OPENROUTER_FREE_ROUTER_MODEL
            metadata = {
                "base_url": self.runtime_config.base_url,
                "notes": self.definition.notes,
                "requires_api_key": self.definition.api_key_requirement == "required",
                "free": is_free,
                "only_free_mode": only_free,
                **({"fallback": True} if fallback else {}),
                **metadata_by_model.get(model_name, {}),
            }

            result.append(
                ModelDefinition(
                    id=self._canonical_model_id(model_name),
                    provider_id=self.definition.integration_id,
                    transport="api",
                    source_type=self.definition.source_type,
                    enabled=self.runtime_config.enabled,
                    available=available,
                    metadata=metadata,
                )
            )

        return result

    def _fallback_model_definitions(
        self,
        available: bool,
        only_free: bool = False,
        include_free_router: bool = False,
    ) -> list[ModelDefinition]:
        """Fallback model definitions when discovery fails."""
        fallback_models = self.runtime_config.fallback_models or self.definition.fallback_models

        if only_free:
            free_fallbacks = [m for m in fallback_models if self._is_free_model(m)]
            if include_free_router and OPENROUTER_FREE_ROUTER_MODEL not in free_fallbacks:
                free_fallbacks.append(OPENROUTER_FREE_ROUTER_MODEL)
            fallback_models = free_fallbacks
        elif include_free_router:
            if OPENROUTER_FREE_ROUTER_MODEL not in fallback_models:
                fallback_models = [OPENROUTER_FREE_ROUTER_MODEL] + list(fallback_models)

        self.status.available = available and bool(fallback_models)
        self.status.discovered_models = fallback_models
        self.status.models_discovered_count = len(fallback_models)

        if not fallback_models and only_free:
            self.status.disabled_reason = (
                "openrouter_free_only_no_fallbacks: No free fallback models configured."
            )

        return self._build_model_definitions(
            fallback_models,
            available=available,
            fallback=True,
            only_free=only_free,
        )

    def diagnostics(self) -> dict:
        return {
            "integration_id": self.status.integration_id,
            "display_name": self.status.display_name,
            "integration_type": self.status.integration_type,
            "source_type": self.status.source_type,
            "transport": self.status.transport,
            "enabled": self.status.enabled,
            "available": self.status.available,
            "api_key_requirement": self.status.api_key_requirement,
            "requires_api_key": self.status.requires_api_key,
            "api_key_source": self.runtime_config.api_key_source,
            "models_probe_ok": self.status.models_probe_ok,
            "disabled_reason": self.status.disabled_reason,
            "base_url": self.status.base_url,
            "discovered_models": self.status.discovered_models,
            "last_refresh_status": self.status.last_refresh_status,
            "last_refresh_error": self.status.last_refresh_error,
            "last_refresh_at": self.status.last_refresh_at,
            "models_discovered_count": self.status.models_discovered_count,
            "free_only_mode": False,
        }

    async def discover_models(self) -> list[ModelDefinition]:
        """Discover models from OpenRouter API with free model filtering."""
        from app.core.config import settings as app_settings

        only_free = app_settings.openrouter.only_free
        include_free_router = app_settings.openrouter.include_free_router

        if not self._is_configured():
            self._set_refresh_status("disabled", error=self.status.disabled_reason)
            return []

        if not app_settings.openrouter.discovery_on_startup:
            self.status.models_probe_ok = False
            self._set_refresh_status("skipped")
            return self._fallback_model_definitions(available=True, only_free=only_free, include_free_router=include_free_router)

        discovered_models = await self._fetch_openrouter_models()
        if discovered_models is None:
            self.status.models_probe_ok = False
            return self._fallback_model_definitions(available=False, only_free=only_free, include_free_router=include_free_router)

        if only_free:
            free_models = [m for m in discovered_models if self._is_free_model(m)]
            if include_free_router and OPENROUTER_FREE_ROUTER_MODEL not in free_models:
                free_models.append(OPENROUTER_FREE_ROUTER_MODEL)

            if not free_models:
                self.status.available = False
                self.status.models_probe_ok = False
                self.status.disabled_reason = (
                    "openrouter_free_only_no_models: No free models found from OpenRouter API."
                )
                self._set_refresh_status("empty", discovered_models=[])
                return []

            self._set_refresh_status("ok", discovered_models=free_models)
            return self._build_model_definitions(free_models, available=True, only_free=True)
        else:
            if include_free_router and OPENROUTER_FREE_ROUTER_MODEL not in discovered_models:
                discovered_models.append(OPENROUTER_FREE_ROUTER_MODEL)

            self.status.available = True
            self.status.models_probe_ok = True
            self.status.disabled_reason = None
            self._set_refresh_status("ok", discovered_models=discovered_models)
            return self._build_model_definitions(discovered_models, available=True, only_free=False)

    async def _fetch_openrouter_models(self) -> list[str] | None:
        """Fetch models from OpenRouter /models endpoint."""
        base_url = self.runtime_config.base_url
        assert base_url is not None
        models_url = f"{base_url.rstrip('/')}/models"

        try:
            timeout = self.runtime_config.timeout_seconds
            headers = self._headers()
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(models_url, headers=headers)
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            logger.warning(
                "OpenRouter model discovery failed",
                integration_id=self.definition.integration_id,
                error=str(exc),
            )
            self.status.disabled_reason = f"models_probe_failed: {exc}"
            self.status.models_probe_ok = False
            self._set_refresh_status("failed", error=str(exc))
            return None

        discovered: list[str] = []
        for item in payload.get("data", []):
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            if isinstance(model_id, str) and model_id:
                discovered.append(model_id)

        if not discovered:
            self.status.disabled_reason = "models_probe_returned_no_models"
            self.status.models_probe_ok = False
            self._set_refresh_status("empty")
            return []

        return sorted(set(discovered))

    async def create_chat_completion(self, request, canonical_model_id):
        """Execute chat completion via OpenAI-compatible endpoint."""
        if not self._is_configured():
            raise ServiceUnavailableError(
                f"Integration {self.definition.integration_id} is not configured",
                details=self.diagnostics(),
            )

        payload = request.model_copy(deep=True).model_dump(exclude_none=True)
        payload["model"] = self._extract_upstream_model(canonical_model_id)
        base_url = self.runtime_config.base_url
        assert base_url is not None
        url = f"{base_url.rstrip('/')}/chat/completions"
        last_error: Exception | None = None

        for _ in range(self.runtime_config.retry_attempts + 1):
            try:
                timeout = self.runtime_config.timeout_seconds
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(url, json=payload, headers=self._headers())
                    if response.status_code == 429:
                        raise ServiceUnavailableError(
                            f"Upstream rate limit for {self.definition.integration_id}",
                            details={
                                "canonical_model_id": canonical_model_id,
                                "status_code": 429,
                                "rate_limited": True,
                                "provider_id": self.definition.integration_id,
                            },
                        )
                    response.raise_for_status()
                    raw_payload = response.json()
                from app.schemas.openai import ChatCompletionResponse
                parsed = ChatCompletionResponse.model_validate(raw_payload)
                return parsed.model_copy(update={"model": canonical_model_id})
            except ServiceUnavailableError:
                raise
            except Exception as exc:
                last_error = exc

        raise ServiceUnavailableError(
            f"Upstream completion failed for {self.definition.integration_id}",
            details={"canonical_model_id": canonical_model_id, "error": str(last_error)},
        )
