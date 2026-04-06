from unittest.mock import AsyncMock, patch

import pytest

from app.browser.execution.models import BrowserJobResult
from app.schemas.openai import ChatCompletionRequest
from app.services.browser_completion_service import BrowserCompletionService


class TestBrowserCompletionService:
    @pytest.mark.asyncio
    async def test_process_completion_uses_dispatcher(self):
        service = BrowserCompletionService()
        request = ChatCompletionRequest(
            model="kimi",
            messages=[{"role": "user", "content": "Hello from browser"}],
        )
        dispatcher_result = BrowserJobResult(
            content="browser response",
            started_at=1.0,
            finished_at=2.0,
            queue_wait_seconds=0.2,
            execution_seconds=0.8,
            retry_count=0,
        )

        with (
            patch(
                "app.services.browser_completion_service.browser_dispatcher.submit_and_wait",
                new=AsyncMock(return_value=dispatcher_result),
            ) as mock_submit,
            patch(
                "app.services.browser_completion_service.browser_registry.get_provider_class"
            ) as mock_provider,
        ):
            mock_provider.return_value.provider_id = "kimi"
            response = await service.process_completion(
                request=request,
                request_id="req-123",
                canonical_model_id="browser/kimi",
            )

        mock_submit.assert_awaited_once_with(
            "req-123",
            provider_id="kimi",
            canonical_model_id="browser/kimi",
            message="Hello from browser",
        )
        assert response.model == "browser/kimi"
        assert response.choices[0].message.content == "browser response"
