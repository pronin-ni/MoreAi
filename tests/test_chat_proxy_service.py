from unittest.mock import AsyncMock, patch

import pytest

from app.integrations.types import ResolvedModel
from app.schemas.openai import ChatCompletionRequest, ChatCompletionResponse, Choice, Message, Usage
from app.services.chat_proxy_service import ChatProxyService


class TestChatProxyService:
    @pytest.mark.asyncio
    async def test_routes_ollamafreeapi_model_to_api_completion_service(self):
        service = ChatProxyService()
        request = ChatCompletionRequest(
            model="api/ollamafreeapi/llama3.3:70b",
            messages=[{"role": "user", "content": "Hello"}],
        )
        resolved = ResolvedModel(
            requested_id=request.model,
            canonical_id="api/ollamafreeapi/llama3.3:70b",
            provider_id="ollamafreeapi",
            transport="api",
            source_type="client_based",
            execution_strategy="api_completion",
        )
        expected = ChatCompletionResponse(
            id="chatcmpl-1",
            created=1,
            model="api/ollamafreeapi/llama3.3:70b",
            choices=[
                Choice(
                    index=0,
                    message=Message(role="assistant", content="hi"),
                    finish_reason="stop",
                )
            ],
            usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

        with (
            patch(
                "app.services.chat_proxy_service.unified_registry.resolve_model",
                return_value=resolved,
            ),
            patch(
                "app.services.chat_proxy_service.api_completion_service.process_completion",
                new=AsyncMock(return_value=expected),
            ) as mock_api_completion,
        ):
            response = await service.process_completion(request, request_id="req-1")

        mock_api_completion.assert_awaited_once_with(request, resolved)
        assert response.model == "api/ollamafreeapi/llama3.3:70b"

    @pytest.mark.asyncio
    async def test_routes_browser_model_to_browser_completion_service(self):
        service = ChatProxyService()
        request = ChatCompletionRequest(
            model="kimi",
            messages=[{"role": "user", "content": "Hello"}],
        )
        resolved = ResolvedModel(
            requested_id=request.model,
            canonical_id="browser/kimi",
            provider_id="kimi",
            transport="browser",
            source_type="browser",
            execution_strategy="browser_completion",
        )
        expected = ChatCompletionResponse(
            id="chatcmpl-2",
            created=1,
            model="browser/kimi",
            choices=[
                Choice(
                    index=0,
                    message=Message(role="assistant", content="browser hi"),
                    finish_reason="stop",
                )
            ],
            usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

        with (
            patch(
                "app.services.chat_proxy_service.unified_registry.resolve_model",
                return_value=resolved,
            ),
            patch(
                "app.services.chat_proxy_service.browser_completion_service.process_completion",
                new=AsyncMock(return_value=expected),
            ) as mock_browser_completion,
        ):
            response = await service.process_completion(request, request_id="req-2")

        mock_browser_completion.assert_awaited_once_with(request, "req-2", "browser/kimi")
        assert response.model == "browser/kimi"

    @pytest.mark.asyncio
    async def test_routes_agent_model_to_agent_completion_service(self):
        service = ChatProxyService()
        request = ChatCompletionRequest(
            model="agent/opencode/openai/gpt-4",
            messages=[{"role": "user", "content": "Hello"}],
        )
        resolved = ResolvedModel(
            requested_id=request.model,
            canonical_id="agent/opencode/openai/gpt-4",
            provider_id="opencode",
            transport="agent",
            source_type="opencode_server",
            execution_strategy="agent_completion",
        )
        expected = ChatCompletionResponse(
            id="chatcmpl-3",
            created=1,
            model="agent/opencode/openai/gpt-4",
            choices=[
                Choice(
                    index=0,
                    message=Message(role="assistant", content="agent hi"),
                    finish_reason="stop",
                )
            ],
            usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

        with (
            patch(
                "app.services.chat_proxy_service.unified_registry.resolve_model",
                return_value=resolved,
            ),
            patch(
                "app.services.chat_proxy_service.agent_completion_service.process_completion",
                new=AsyncMock(return_value=expected),
            ) as mock_agent_completion,
        ):
            response = await service.process_completion(request, request_id="req-3")

        mock_agent_completion.assert_awaited_once_with(
            request, "req-3", "agent/opencode/openai/gpt-4", "opencode"
        )
        assert response.model == "agent/opencode/openai/gpt-4"
