from app.browser.auth import auth_bootstrapper
from app.browser.registry import registry as browser_registry
from app.browser.session_pool import pool
from app.core.config import settings
from app.core.errors import BrowserError, InternalError, ProxyError
from app.core.logging import get_logger
from app.schemas.openai import ChatCompletionRequest, ChatCompletionResponse
from app.utils.message_parser import extract_last_user_message
from app.utils.openai_mapper import create_completion_response

logger = get_logger(__name__)


class BrowserCompletionService:
    def __init__(self, browser_pool):
        self.browser_pool = browser_pool

    async def process_completion(
        self,
        request: ChatCompletionRequest,
        request_id: str,
        canonical_model_id: str,
    ) -> ChatCompletionResponse:
        response_text = await self._send_to_browser_chat(
            canonical_model_id,
            extract_last_user_message(request.messages),
            request_id,
        )
        return create_completion_response(model=canonical_model_id, content=response_text)

    async def _send_to_browser_chat(self, model: str, message: str, request_id: str) -> str:
        last_error: ProxyError | None = None
        auth_state_invalidated = False

        provider_class = browser_registry.get_provider_class(model)
        provider_config = browser_registry.get_provider_config(model)

        for attempt in range(settings.retry_attempts + 1):
            try:
                await auth_bootstrapper.ensure_model_authenticated(model)

                async with self.browser_pool.acquire_session(model=model) as session:
                    provider = provider_class(
                        session.page, request_id, provider_config=provider_config
                    )
                    provider.set_request_id(request_id)

                    await provider.navigate_to_chat()
                    await provider.start_new_chat()
                    await provider.send_message(message)
                    return await provider.wait_for_response(
                        timeout=settings.response_timeout_seconds
                    )

            except BrowserError as exc:
                last_error = exc
                auth_wall_detected = False
                if "provider" in locals():
                    try:
                        await provider.save_debug_artifacts(str(exc))
                    except Exception:
                        logger.debug(
                            "Failed to save provider artifacts",
                            request_id=request_id,
                            model=model,
                        )
                    try:
                        auth_wall_detected = await provider.detect_login_required()
                    except Exception:
                        logger.debug(
                            "Failed to re-check provider login state",
                            request_id=request_id,
                            model=model,
                        )

                if (
                    provider_class.requires_auth
                    and auth_wall_detected
                    and not auth_state_invalidated
                ):
                    auth_bootstrapper.invalidate_model_storage_state(model)
                    auth_state_invalidated = True
                    logger.info(
                        "Invalidated provider auth state after login wall",
                        request_id=request_id,
                        model=model,
                    )
                    continue

                logger.warning(
                    "Browser error occurred",
                    request_id=request_id,
                    model=model,
                    attempt=attempt + 1,
                    error=str(exc),
                )

        if last_error:
            raise InternalError(
                f"Failed to process chat request after {settings.retry_attempts + 1} attempts: {last_error.message}",
                details=last_error.details,
            )

        raise InternalError("Unknown browser error occurred during chat processing")


browser_completion_service = BrowserCompletionService(browser_pool=pool)
