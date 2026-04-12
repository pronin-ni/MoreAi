from app.browser.execution.dispatcher import browser_dispatcher
from app.browser.registry import registry as browser_registry
from app.core.logging import get_logger
from app.core.metrics import (
    browser_execution_seconds,
    queue_wait_seconds,
)
from app.schemas.openai import ChatCompletionRequest, ChatCompletionResponse
from app.utils.message_parser import extract_last_user_message
from app.utils.openai_mapper import create_completion_response

logger = get_logger(__name__)


class BrowserCompletionService:
    async def process_completion(
        self,
        request: ChatCompletionRequest,
        request_id: str,
        canonical_model_id: str,
    ) -> ChatCompletionResponse:
        provider_class = browser_registry.get_provider_class(canonical_model_id)
        result = await browser_dispatcher.submit_and_wait(
            request_id,
            provider_id=provider_class.provider_id,
            canonical_model_id=canonical_model_id,
            message=extract_last_user_message(request.messages),
        )
        logger.info(
            "Browser task completed",
            request_id=request_id,
            model=canonical_model_id,
            queue_wait_seconds=result.queue_wait_seconds,
            execution_seconds=result.execution_seconds,
            retry_count=result.retry_count,
            restart_occurred=getattr(result, 'restart_occurred', False),
            restart_reason=getattr(result, 'restart_reason', ''),
        )

        # Record metrics
        queue_wait_seconds.observe(result.queue_wait_seconds)
        browser_execution_seconds.observe(
            result.execution_seconds,
            provider=provider_class.provider_id,
        )

        response = create_completion_response(model=canonical_model_id, content=result.content)
        # Attach browser-level attempt data for observability
        if hasattr(result, 'attempts') and result.attempts:
            response._browser_attempts = result.attempts
            response._browser_restart_occurred = result.restart_occurred
            response._browser_restart_reason = result.restart_reason

        return response


browser_completion_service = BrowserCompletionService()
