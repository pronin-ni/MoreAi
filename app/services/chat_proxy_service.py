from app.core.config import settings
from app.core.logging import get_logger
from app.core.errors import InternalError
from app.registry.unified import unified_registry
from app.schemas.openai import ChatCompletionRequest, ChatCompletionResponse
from app.services.api_completion_service import api_completion_service
from app.services.browser_completion_service import browser_completion_service

logger = get_logger(__name__)


class ChatProxyService:
    async def process_completion(
        self,
        request: ChatCompletionRequest,
        request_id: str,
    ) -> ChatCompletionResponse:
        logger.info(
            "Processing chat completion request",
            request_id=request_id,
            model=request.model,
        )

        resolved_model = unified_registry.resolve_model(request.model)

        logger.info(
            "Resolved model for completion",
            request_id=request_id,
            requested_model=request.model,
            canonical_model=resolved_model.canonical_id,
            transport=resolved_model.transport,
            provider_id=resolved_model.provider_id,
        )

        if resolved_model.transport == "browser":
            return await browser_completion_service.process_completion(
                request,
                request_id,
                resolved_model.canonical_id,
            )
        if resolved_model.transport == "api":
            return await api_completion_service.process_completion(request, resolved_model)

        raise InternalError(
            f"Unsupported transport for model {resolved_model.canonical_id}",
            details={"transport": resolved_model.transport},
        )


service = ChatProxyService()
