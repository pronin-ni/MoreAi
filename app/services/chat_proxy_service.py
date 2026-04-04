import asyncio
from app.browser.auth import google_auth_bootstrapper
from app.browser.session_pool import pool
from app.browser.registry import registry
from app.browser.base import BrowserProvider
from app.core.config import settings
from app.core.logging import get_logger
from app.core.errors import (
    BrowserError,
    ProxyError,
    InternalError,
)
from app.schemas.openai import ChatCompletionRequest
from app.utils.message_parser import extract_last_user_message

logger = get_logger(__name__)


class ChatProxyService:
    def __init__(self, browser_pool):
        self.browser_pool = browser_pool

    async def process_completion(self, request: ChatCompletionRequest, request_id: str) -> str:
        logger.info(
            "Processing chat completion request",
            request_id=request_id,
            model=request.model,
        )

        user_message = extract_last_user_message(request.messages)
        logger.debug("Extracted user message", content_length=len(user_message))

        response_text = await self._send_to_browser_chat(request.model, user_message, request_id)

        logger.info(
            "Chat completion processed successfully",
            request_id=request_id,
            response_length=len(response_text),
        )

        return response_text

    async def _send_to_browser_chat(self, model: str, message: str, request_id: str) -> str:
        last_error: ProxyError | None = None

        provider_class = registry.get_provider_class(model)
        provider_config = registry.get_provider_config(model)

        for attempt in range(settings.retry_attempts + 1):
            try:
                await google_auth_bootstrapper.ensure_model_authenticated(model)

                async with self.browser_pool.acquire_session(model=model) as session:
                    provider = provider_class(session.page, request_id, provider_config=provider_config)
                    provider.set_request_id(request_id)

                    await provider.navigate_to_chat()
                    await provider.start_new_chat()
                    await provider.send_message(message)
                    response = await provider.wait_for_response(
                        timeout=settings.response_timeout_seconds
                    )

                    return response

            except BrowserError as e:
                last_error = e
                if "provider" in locals():
                    try:
                        await provider.save_debug_artifacts(str(e))
                    except Exception:
                        logger.debug("Failed to save provider artifacts", request_id=request_id, model=model)
                logger.warning(
                    "Browser error occurred",
                    request_id=request_id,
                    model=model,
                    attempt=attempt + 1,
                    error=str(e),
                )

        if last_error:
            raise InternalError(
                f"Failed to process chat request after {settings.retry_attempts + 1} attempts: {last_error.message}",
                details=last_error.details,
            )

        raise InternalError("Unknown error occurred during chat processing")


service = ChatProxyService(browser_pool=pool)
