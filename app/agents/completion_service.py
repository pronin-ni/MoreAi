from app.agents.registry import registry as agent_registry
from app.core.logging import get_logger
from app.schemas.openai import ChatCompletionRequest, ChatCompletionResponse
from app.utils.message_parser import extract_last_user_message
from app.utils.openai_mapper import create_completion_response

logger = get_logger(__name__)


class AgentCompletionService:
    async def process_completion(
        self,
        request: ChatCompletionRequest,
        request_id: str,
        canonical_model_id: str,
        provider_id: str,
    ) -> ChatCompletionResponse:
        """Process a chat completion request via an agent provider."""
        logger.info(
            "Processing agent completion",
            request_id=request_id,
            model=canonical_model_id,
            provider_id=provider_id,
        )

        # Get the provider from registry
        provider = agent_registry.get_provider(provider_id)

        # Extract the user message
        prompt = extract_last_user_message(request.messages)

        # Send the prompt and get response
        content = await provider.send_prompt(
            prompt=prompt,
            model=canonical_model_id,
            provider_id=provider_id,
            timeout=request.timeout if hasattr(request, "timeout") else None,
        )

        logger.info(
            "Agent completion completed",
            request_id=request_id,
            model=canonical_model_id,
        )

        # Create OpenAI-compatible response
        return create_completion_response(model=canonical_model_id, content=content)


agent_completion_service = AgentCompletionService()
