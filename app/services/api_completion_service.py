from app.core.config import settings
from app.core.errors import ServiceUnavailableError
from app.integrations.registry import api_registry
from app.integrations.types import ResolvedModel
from app.schemas.openai import ChatCompletionRequest, ChatCompletionResponse


class APICompletionService:
    async def process_completion(
        self,
        request: ChatCompletionRequest,
        resolved_model: ResolvedModel,
    ) -> ChatCompletionResponse:
        return await self._execute_with_fallback(request, resolved_model)

    async def _execute_with_fallback(
        self,
        request: ChatCompletionRequest,
        resolved_model: ResolvedModel,
    ) -> ChatCompletionResponse:
        adapter = api_registry.get_adapter(resolved_model.provider_id)
        try:
            return await adapter.create_chat_completion(request, resolved_model.canonical_id)
        except ServiceUnavailableError as exc:
            if exc.details.get("status_code") != 429:
                raise

            api_registry.mark_rate_limited(
                resolved_model.provider_id,
                settings.integrations_rate_limit_cooldown_seconds,
            )
            fallback = api_registry.find_fallback_model(
                resolved_model.canonical_id,
                exclude_provider_id=resolved_model.provider_id,
            )
            if fallback is None:
                raise

            fallback_request = request.model_copy(update={"model": fallback.canonical_id})
            fallback_adapter = api_registry.get_adapter(fallback.provider_id)
            return await fallback_adapter.create_chat_completion(
                fallback_request,
                fallback.canonical_id,
            )


api_completion_service = APICompletionService()
