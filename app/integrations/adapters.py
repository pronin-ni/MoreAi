from typing import Any

import httpx

from app.core.errors import ServiceUnavailableError
from app.core.logging import get_logger
from app.integrations.types import (
    IntegrationDefinition,
    IntegrationRuntimeConfig,
    IntegrationStatus,
    ModelDefinition,
)
from app.schemas.openai import ChatCompletionRequest, ChatCompletionResponse

logger = get_logger(__name__)


class BaseIntegrationAdapter:
    transport = "api"

    def __init__(self, definition: IntegrationDefinition, runtime_config: IntegrationRuntimeConfig):
        self.definition = definition
        self.runtime_config = runtime_config
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
        )

    async def discover_models(self) -> list[ModelDefinition]:
        raise NotImplementedError

    async def create_chat_completion(
        self,
        request: ChatCompletionRequest,
        canonical_model_id: str,
    ) -> ChatCompletionResponse:
        raise NotImplementedError

    def diagnostics(self) -> dict[str, Any]:
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
        }

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
            return canonical_model_id[len(prefix) :]
        return canonical_model_id


class OpenAICompatibleIntegration(BaseIntegrationAdapter):
    async def discover_models(self) -> list[ModelDefinition]:
        if not self._is_configured():
            return self._fallback_model_definitions(available=False)

        if not self.runtime_config.discover_models:
            self.status.models_probe_ok = False
            return self._fallback_model_definitions(available=True)

        models_url = f"{self.runtime_config.base_url.rstrip('/')}/models"
        try:
            async with httpx.AsyncClient(timeout=self.runtime_config.timeout_seconds) as client:
                response = await client.get(models_url, headers=self._headers())
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            logger.warning(
                "Integration model discovery failed",
                integration_id=self.definition.integration_id,
                error=str(exc),
            )
            self.status.disabled_reason = f"models_probe_failed: {exc}"
            self.status.models_probe_ok = False
            return self._fallback_model_definitions(available=False)

        discovered_models = [
            item.get("id")
            for item in payload.get("data", [])
            if isinstance(item, dict) and item.get("id")
        ]
        if not discovered_models:
            self.status.disabled_reason = "models_probe_returned_no_models"
            self.status.models_probe_ok = False
            return self._fallback_model_definitions(available=False)

        self.status.available = True
        self.status.models_probe_ok = True
        self.status.disabled_reason = None
        self.status.discovered_models = discovered_models
        return [
            ModelDefinition(
                id=self._canonical_model_id(model_name),
                provider_id=self.definition.integration_id,
                transport="api",
                source_type=self.definition.source_type,
                enabled=True,
                available=True,
                metadata={
                    "base_url": self.runtime_config.base_url,
                    "notes": self.definition.notes,
                    "requires_api_key": self.definition.api_key_requirement == "required",
                },
            )
            for model_name in discovered_models
        ]

    async def create_chat_completion(
        self,
        request: ChatCompletionRequest,
        canonical_model_id: str,
    ) -> ChatCompletionResponse:
        if not self._is_configured():
            raise ServiceUnavailableError(
                f"Integration {self.definition.integration_id} is not configured",
                details=self.diagnostics(),
            )

        payload = request.model_copy(deep=True).model_dump(exclude_none=True)
        payload["model"] = self._extract_upstream_model(canonical_model_id)
        url = f"{self.runtime_config.base_url.rstrip('/')}/chat/completions"
        last_error: Exception | None = None

        for _ in range(self.runtime_config.retry_attempts + 1):
            try:
                async with httpx.AsyncClient(timeout=self.runtime_config.timeout_seconds) as client:
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

    def _fallback_model_definitions(self, available: bool) -> list[ModelDefinition]:
        fallback_models = self.runtime_config.fallback_models or self.definition.fallback_models
        self.status.available = available and bool(fallback_models)
        self.status.discovered_models = fallback_models
        return [
            ModelDefinition(
                id=self._canonical_model_id(model_name),
                provider_id=self.definition.integration_id,
                transport="api",
                source_type=self.definition.source_type,
                enabled=self.runtime_config.enabled,
                available=available,
                metadata={
                    "base_url": self.runtime_config.base_url,
                    "notes": self.definition.notes,
                    "requires_api_key": self.definition.api_key_requirement == "required",
                    "fallback": True,
                },
            )
            for model_name in fallback_models
        ]


class ClientBasedIntegration(OpenAICompatibleIntegration):
    async def discover_models(self) -> list[ModelDefinition]:
        if not self.runtime_config.base_url:
            self.status.disabled_reason = "missing_client_endpoint"
            return self._fallback_model_definitions(available=False)
        return await super().discover_models()
