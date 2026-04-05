from unittest.mock import AsyncMock, patch

import pytest

from app.core.errors import ServiceUnavailableError
from app.integrations.types import ResolvedModel
from app.schemas.openai import ChatCompletionRequest, ChatCompletionResponse, Choice, Message, Usage
from app.services.api_completion_service import APICompletionService


class TestAPICompletionService:
    @pytest.mark.asyncio
    async def test_fallback_on_rate_limit(self):
        service = APICompletionService()
        request = ChatCompletionRequest(
            model="api/g4f-groq/llama-3.3-70b",
            messages=[{"role": "user", "content": "Hi"}],
        )
        primary = ResolvedModel(
            requested_id=request.model,
            canonical_id="api/g4f-groq/llama-3.3-70b",
            provider_id="g4f-groq",
            transport="api",
            source_type="g4f_openai",
            execution_strategy="api_completion",
        )
        fallback = ResolvedModel(
            requested_id=request.model,
            canonical_id="api/g4f-hosted/llama-3.3-70b",
            provider_id="g4f-hosted",
            transport="api",
            source_type="g4f_openai",
            execution_strategy="api_completion",
        )
        primary_adapter = AsyncMock()
        primary_adapter.create_chat_completion.side_effect = ServiceUnavailableError(
            "rate limited",
            details={"status_code": 429},
        )
        fallback_adapter = AsyncMock()
        fallback_adapter.create_chat_completion.return_value = ChatCompletionResponse(
            id="chatcmpl-1",
            created=1,
            model="api/g4f-hosted/llama-3.3-70b",
            choices=[
                Choice(
                    index=0, message=Message(role="assistant", content="ok"), finish_reason="stop"
                )
            ],
            usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
        )

        with (
            patch(
                "app.services.api_completion_service.api_registry.get_adapter",
                side_effect=[primary_adapter, fallback_adapter],
            ),
            patch(
                "app.services.api_completion_service.api_registry.mark_rate_limited"
            ) as mark_rate_limited,
            patch(
                "app.services.api_completion_service.api_registry.find_fallback_model",
                return_value=fallback,
            ),
        ):
            response = await service.process_completion(request, primary)

        mark_rate_limited.assert_called_once()
        assert response.model == "api/g4f-hosted/llama-3.3-70b"
