import asyncio
import time
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
from app.utils.openai_mapper import create_completion_response

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
            last_refresh_status="not_started",
            last_refresh_error=None,
            last_refresh_at=None,
            models_discovered_count=0,
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
            "last_refresh_status": self.status.last_refresh_status,
            "last_refresh_error": self.status.last_refresh_error,
            "last_refresh_at": self.status.last_refresh_at,
            "models_discovered_count": self.status.models_discovered_count,
        }

    async def stream_chat_completion(
        self,
        request: ChatCompletionRequest,
        canonical_model_id: str,
    ):
        raise NotImplementedError

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

    def _set_refresh_status(
        self,
        status: str,
        *,
        error: str | None = None,
        discovered_models: list[str] | None = None,
    ) -> None:
        self.status.last_refresh_status = status
        self.status.last_refresh_error = error
        self.status.last_refresh_at = time.time()
        if discovered_models is not None:
            self.status.discovered_models = discovered_models
            self.status.models_discovered_count = len(discovered_models)

    def _extract_prompt(self, request: ChatCompletionRequest) -> str:
        for message in reversed(request.messages):
            if message.role != "user":
                continue
            if isinstance(message.content, str):
                content = message.content.strip()
                if content:
                    return content
                continue

            parts: list[str] = []
            for item in message.content:
                if item.type == "text" and item.text:
                    text = item.text.strip()
                    if text:
                        parts.append(text)
            prompt = "\n".join(parts).strip()
            if prompt:
                return prompt

        return ""

    def _build_model_definitions(
        self,
        model_names: list[str],
        *,
        available: bool,
        fallback: bool = False,
        metadata_by_model: dict[str, dict[str, object]] | None = None,
    ) -> list[ModelDefinition]:
        metadata_by_model = metadata_by_model or {}
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
                    **({"fallback": True} if fallback else {}),
                    **metadata_by_model.get(model_name, {}),
                },
            )
            for model_name in model_names
        ]


class OpenAICompatibleIntegration(BaseIntegrationAdapter):
    async def discover_models(self) -> list[ModelDefinition]:
        if not self._is_configured():
            self._set_refresh_status("disabled", error=self.status.disabled_reason)
            return self._fallback_model_definitions(available=False)

        if not self.runtime_config.discover_models:
            self.status.models_probe_ok = False
            self._set_refresh_status("skipped")
            return self._fallback_model_definitions(available=True)

        base_url = self.runtime_config.base_url
        assert base_url is not None
        models_url = f"{base_url.rstrip('/')}/models"
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
            self._set_refresh_status("failed", error=str(exc))
            return self._fallback_model_definitions(available=False)

        discovered_models: list[str] = []
        for item in payload.get("data", []):
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            if isinstance(model_id, str) and model_id:
                discovered_models.append(model_id)
        if not discovered_models:
            self.status.disabled_reason = "models_probe_returned_no_models"
            self.status.models_probe_ok = False
            self._set_refresh_status("empty")
            return self._fallback_model_definitions(available=False)

        self.status.available = True
        self.status.models_probe_ok = True
        self.status.disabled_reason = None
        self._set_refresh_status("ok", discovered_models=discovered_models)
        return self._build_model_definitions(discovered_models, available=True)

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
        base_url = self.runtime_config.base_url
        assert base_url is not None
        url = f"{base_url.rstrip('/')}/chat/completions"
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
        self.status.models_discovered_count = len(fallback_models)
        return self._build_model_definitions(fallback_models, available=available, fallback=True)


class ClientBasedIntegration(OpenAICompatibleIntegration):
    async def discover_models(self) -> list[ModelDefinition]:
        if not self.runtime_config.base_url:
            self.status.disabled_reason = "missing_client_endpoint"
            self._set_refresh_status("disabled", error=self.status.disabled_reason)
            return self._fallback_model_definitions(available=False)
        return await super().discover_models()


class OllamaFreeAPIIntegration(BaseIntegrationAdapter):
    def __init__(self, definition: IntegrationDefinition, runtime_config: IntegrationRuntimeConfig):
        super().__init__(definition, runtime_config)
        self._client: Any | None = None

    def _is_configured(self) -> bool:
        if not self.runtime_config.enabled:
            self.status.disabled_reason = "disabled_by_config"
            return False
        return True

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from ollamafreeapi import OllamaFreeAPI  # type: ignore[import-not-found]
            except ImportError as exc:
                raise ServiceUnavailableError(
                    "OllamaFreeAPI dependency is not installed",
                    details={"provider_id": self.definition.integration_id, "error": str(exc)},
                ) from exc
            self._client = OllamaFreeAPI()
        return self._client

    async def _run_client_call(self, func, *args, **kwargs):
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(func, *args, **kwargs),
                timeout=self.runtime_config.timeout_seconds,
            )
        except TimeoutError as exc:
            raise ServiceUnavailableError(
                f"Timed out waiting for {self.definition.integration_id}",
                details={
                    "provider_id": self.definition.integration_id,
                    "timeout_seconds": self.runtime_config.timeout_seconds,
                },
            ) from exc

    async def discover_models(self) -> list[ModelDefinition]:
        if not self._is_configured():
            self._set_refresh_status("disabled", error=self.status.disabled_reason)
            return []

        if not self.runtime_config.discover_models:
            self.status.available = False
            self.status.models_probe_ok = False
            self._set_refresh_status("skipped")
            return []

        try:
            client = self._get_client()
            discovered_models = await self._run_client_call(client.list_models)
        except ServiceUnavailableError as exc:
            self.status.available = False
            self.status.models_probe_ok = False
            self.status.disabled_reason = str(exc)
            self._set_refresh_status("failed", error=str(exc))
            return []
        except Exception as exc:
            logger.warning(
                "OllamaFreeAPI model discovery failed",
                integration_id=self.definition.integration_id,
                error=str(exc),
            )
            self.status.available = False
            self.status.models_probe_ok = False
            self.status.disabled_reason = f"models_probe_failed: {exc}"
            self._set_refresh_status("failed", error=str(exc))
            return []

        normalized_models = sorted(
            {str(model).strip() for model in discovered_models if str(model).strip()}
        )
        if not normalized_models:
            self.status.available = False
            self.status.models_probe_ok = False
            self.status.disabled_reason = "models_probe_returned_no_models"
            self._set_refresh_status("empty")
            return []

        metadata_by_model: dict[str, dict[str, object]] = {}
        for model_name in normalized_models:
            try:
                model_info = await self._run_client_call(client.get_model_info, model_name)
            except Exception:
                continue
            if isinstance(model_info, dict):
                metadata_by_model[model_name] = {"model_info": model_info}

        self.status.available = True
        self.status.models_probe_ok = True
        self.status.disabled_reason = None
        self._set_refresh_status("ok", discovered_models=normalized_models)
        return self._build_model_definitions(
            normalized_models,
            available=True,
            metadata_by_model=metadata_by_model,
        )

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

        prompt = self._extract_prompt(request)
        if not prompt:
            raise ServiceUnavailableError(
                "No user prompt found for OllamaFreeAPI request",
                details={"canonical_model_id": canonical_model_id},
            )

        upstream_model = self._extract_upstream_model(canonical_model_id)
        kwargs = {
            "temperature": request.temperature,
            "top_p": request.top_p,
            "num_predict": request.max_tokens,
            "stop": request.stop,
        }
        filtered_kwargs = {key: value for key, value in kwargs.items() if value is not None}

        try:
            client = self._get_client()
            content = await self._run_client_call(
                client.chat,
                prompt,
                model=upstream_model,
                **filtered_kwargs,
            )
        except ServiceUnavailableError:
            raise
        except Exception as exc:
            raise ServiceUnavailableError(
                f"Upstream completion failed for {self.definition.integration_id}",
                details={"canonical_model_id": canonical_model_id, "error": str(exc)},
            ) from exc

        return create_completion_response(canonical_model_id, str(content))

    async def stream_chat_completion(
        self,
        request: ChatCompletionRequest,
        canonical_model_id: str,
    ):
        if not self._is_configured():
            raise ServiceUnavailableError(
                f"Integration {self.definition.integration_id} is not configured",
                details=self.diagnostics(),
            )

        prompt = self._extract_prompt(request)
        upstream_model = self._extract_upstream_model(canonical_model_id)
        client = self._get_client()
        kwargs = {
            "temperature": request.temperature,
            "top_p": request.top_p,
            "num_predict": request.max_tokens,
            "stop": request.stop,
        }
        filtered_kwargs = {key: value for key, value in kwargs.items() if value is not None}

        return await self._run_client_call(
            client.stream_chat,
            prompt,
            model=upstream_model,
            **filtered_kwargs,
        )
