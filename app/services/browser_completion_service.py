from app.browser.execution.dispatcher import browser_dispatcher
from app.browser.registry import registry as browser_registry
from app.core.logging import get_logger
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
        )
        return create_completion_response(model=canonical_model_id, content=result.content)


browser_completion_service = BrowserCompletionService()
