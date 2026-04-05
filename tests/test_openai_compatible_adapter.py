from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.errors import ServiceUnavailableError
from app.integrations.adapters import OpenAICompatibleIntegration
from app.integrations.types import IntegrationDefinition, IntegrationRuntimeConfig
from app.schemas.openai import ChatCompletionRequest


def build_definition(api_key_requirement: str = "none") -> IntegrationDefinition:
    return IntegrationDefinition(
        integration_id="g4f-groq",
        display_name="G4F Groq",
        integration_type="openai_compatible",
        group="ready_to_use_base_url",
        source_type="g4f_openai",
        base_url="https://g4f.space/api/groq",
        api_key_requirement=api_key_requirement,
        enabled_by_default=True,
    )


class TestOpenAICompatibleAdapter:
    @pytest.mark.asyncio
    async def test_discover_models_success(self):
        adapter = OpenAICompatibleIntegration(
            build_definition(),
            IntegrationRuntimeConfig(
                enabled=True,
                base_url="https://g4f.space/api/groq",
                api_key=None,
            ),
        )
        response = MagicMock()
        response.json.return_value = {"data": [{"id": "llama-3.3-70b"}]}
        response.raise_for_status.return_value = None

        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=response)):
            models = await adapter.discover_models()

        assert [model.id for model in models] == ["api/g4f-groq/llama-3.3-70b"]
        assert adapter.status.available is True

    @pytest.mark.asyncio
    async def test_discover_models_falls_back_when_probe_fails(self):
        adapter = OpenAICompatibleIntegration(
            build_definition(),
            IntegrationRuntimeConfig(
                enabled=True,
                base_url="https://g4f.space/api/groq",
                api_key=None,
                fallback_models=["fallback-model"],
            ),
        )

        with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=RuntimeError("boom"))):
            models = await adapter.discover_models()

        assert [model.id for model in models] == ["api/g4f-groq/fallback-model"]
        assert adapter.status.models_probe_ok is False

    @pytest.mark.asyncio
    async def test_disabled_without_required_api_key(self):
        adapter = OpenAICompatibleIntegration(
            build_definition(api_key_requirement="required"),
            IntegrationRuntimeConfig(
                enabled=True,
                base_url="https://g4f.space/v1",
                api_key=None,
            ),
        )

        models = await adapter.discover_models()

        assert models == []
        assert adapter.status.disabled_reason == "missing_api_key"

    @pytest.mark.asyncio
    async def test_chat_completion_forwarding(self):
        adapter = OpenAICompatibleIntegration(
            build_definition(),
            IntegrationRuntimeConfig(
                enabled=True,
                base_url="https://g4f.space/api/groq",
                api_key=None,
            ),
        )
        response = MagicMock()
        response.json.return_value = {
            "id": "chatcmpl-upstream",
            "object": "chat.completion",
            "created": 1,
            "model": "llama-3.3-70b",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hello"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        response.raise_for_status.return_value = None

        with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)):
            completion = await adapter.create_chat_completion(
                ChatCompletionRequest(
                    model="api/g4f-groq/llama-3.3-70b",
                    messages=[{"role": "user", "content": "Hi"}],
                ),
                canonical_model_id="api/g4f-groq/llama-3.3-70b",
            )

        assert completion.model == "api/g4f-groq/llama-3.3-70b"
        assert completion.choices[0].message.content == "hello"

    @pytest.mark.asyncio
    async def test_chat_completion_marks_rate_limit(self):
        adapter = OpenAICompatibleIntegration(
            build_definition(),
            IntegrationRuntimeConfig(
                enabled=True,
                base_url="https://g4f.space/api/groq",
                api_key="shared-token",
                api_key_source="g4f_shared_env",
            ),
        )
        response = MagicMock()
        response.status_code = 429

        with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)):
            with pytest.raises(ServiceUnavailableError) as exc_info:
                await adapter.create_chat_completion(
                    ChatCompletionRequest(
                        model="api/g4f-groq/llama-3.3-70b",
                        messages=[{"role": "user", "content": "Hi"}],
                    ),
                    canonical_model_id="api/g4f-groq/llama-3.3-70b",
                )

        assert exc_info.value.details["status_code"] == 429
