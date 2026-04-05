import time
from unittest.mock import MagicMock

import pytest

from app.core.errors import ServiceUnavailableError
from app.integrations.adapters import OllamaFreeAPIIntegration
from app.integrations.types import IntegrationDefinition, IntegrationRuntimeConfig
from app.schemas.openai import ChatCompletionRequest


def build_definition() -> IntegrationDefinition:
    return IntegrationDefinition(
        integration_id="ollamafreeapi",
        display_name="OllamaFreeAPI",
        integration_type="client_based",
        group="individual_client",
        source_type="client_based",
        base_url=None,
        api_key_requirement="none",
        enabled_by_default=True,
    )


class TestOllamaFreeAPIIntegration:
    @pytest.mark.asyncio
    async def test_discover_models_success(self):
        adapter = OllamaFreeAPIIntegration(
            build_definition(),
            IntegrationRuntimeConfig(enabled=True, base_url=None, api_key=None),
        )
        client = MagicMock()
        client.list_models.return_value = ["llama3.3:70b", "deepseek-r1:7b"]
        client.get_model_info.side_effect = [
            {"family": "deepseek", "size": "7b"},
            {"family": "llama", "size": "70b"},
        ]
        adapter._client = client

        models = await adapter.discover_models()

        assert [model.id for model in models] == [
            "api/ollamafreeapi/deepseek-r1:7b",
            "api/ollamafreeapi/llama3.3:70b",
        ]
        assert adapter.status.available is True
        assert adapter.status.models_probe_ok is True
        assert adapter.status.models_discovered_count == 2
        assert adapter.status.last_refresh_status == "ok"
        models_by_id = {model.id: model for model in models}
        assert (
            models_by_id["api/ollamafreeapi/deepseek-r1:7b"].metadata["model_info"]["family"]
            == "deepseek"
        )

    @pytest.mark.asyncio
    async def test_discover_models_gracefully_fails(self):
        adapter = OllamaFreeAPIIntegration(
            build_definition(),
            IntegrationRuntimeConfig(enabled=True, base_url=None, api_key=None),
        )
        client = MagicMock()
        client.list_models.side_effect = RuntimeError("upstream down")
        adapter._client = client

        models = await adapter.discover_models()

        assert models == []
        assert adapter.status.available is False
        assert adapter.status.last_refresh_status == "failed"
        assert adapter.status.last_refresh_error == "upstream down"

    @pytest.mark.asyncio
    async def test_chat_completion_uses_last_user_message(self):
        adapter = OllamaFreeAPIIntegration(
            build_definition(),
            IntegrationRuntimeConfig(enabled=True, base_url=None, api_key=None),
        )
        client = MagicMock()
        client.chat.return_value = "final answer"
        adapter._client = client

        completion = await adapter.create_chat_completion(
            ChatCompletionRequest(
                model="api/ollamafreeapi/llama3.3:70b",
                messages=[
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "first question"},
                    {"role": "assistant", "content": "partial reply"},
                    {"role": "user", "content": "final question"},
                ],
                temperature=0.3,
                top_p=0.8,
                max_tokens=128,
            ),
            canonical_model_id="api/ollamafreeapi/llama3.3:70b",
        )

        client.chat.assert_called_once_with(
            "final question",
            model="llama3.3:70b",
            temperature=0.3,
            top_p=0.8,
            num_predict=128,
        )
        assert completion.model == "api/ollamafreeapi/llama3.3:70b"
        assert completion.choices[0].message.content == "final answer"

    @pytest.mark.asyncio
    async def test_chat_completion_rejects_missing_user_prompt(self):
        adapter = OllamaFreeAPIIntegration(
            build_definition(),
            IntegrationRuntimeConfig(enabled=True, base_url=None, api_key=None),
        )
        adapter._client = MagicMock()

        with pytest.raises(ServiceUnavailableError) as exc_info:
            await adapter.create_chat_completion(
                ChatCompletionRequest(
                    model="api/ollamafreeapi/llama3.3:70b",
                    messages=[{"role": "assistant", "content": "hello"}],
                ),
                canonical_model_id="api/ollamafreeapi/llama3.3:70b",
            )

        assert "No user prompt found" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_chat_completion_reports_timeout_clearly(self):
        adapter = OllamaFreeAPIIntegration(
            build_definition(),
            IntegrationRuntimeConfig(enabled=True, base_url=None, api_key=None, timeout_seconds=1),
        )
        client = MagicMock()

        def slow_chat(*args, **kwargs):
            time.sleep(1.2)
            return "late"

        client.chat.side_effect = slow_chat
        adapter._client = client

        with pytest.raises(ServiceUnavailableError) as exc_info:
            await adapter.create_chat_completion(
                ChatCompletionRequest(
                    model="api/ollamafreeapi/llama3.3:70b",
                    messages=[{"role": "user", "content": "Hello"}],
                ),
                canonical_model_id="api/ollamafreeapi/llama3.3:70b",
            )

        assert "Timed out waiting for ollamafreeapi" in str(exc_info.value)
        assert exc_info.value.details["timeout_seconds"] == 1
