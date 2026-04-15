import time
import uuid

from app.schemas.openai import (
    ChatCompletionResponse,
    Choice,
    Message,
    Model,
    ModelList,
    Usage,
)


def generate_completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:8]}"


def create_completion_response(
    model: str,
    content: str,
) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id=generate_completion_id(),
        object="chat.completion",
        created=int(time.time()),
        model=model,
        choices=[
            Choice(
                index=0,
                message=Message(
                    role="assistant",
                    content=content,
                ),
                finish_reason="stop",
            )
        ],
        usage=Usage(
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
        ),
    )


def create_model_list() -> ModelList:
    from app.admin.config_manager import config_manager
    from app.registry.unified import unified_registry

    models = unified_registry.list_models()
    overrides = config_manager.overrides.models

    filtered_models = []
    for m in models:
        model_id = m["id"]
        enabled = m.get("enabled", True)
        available = m.get("available", True)

        # Apply visibility override
        override = overrides.get(model_id)
        if override:
            if override.enabled is not None:
                enabled = override.enabled
            if override.visibility is not None and override.visibility == "hidden":
                continue

        # Skip disabled models
        if not enabled:
            continue

        # Skip unavailable models
        if not available:
            continue

        filtered_models.append(
            Model(
                id=model_id,
                object="model",
                created=int(time.time()),
                owned_by=m["provider_id"],
                provider_id=m["provider_id"],
                transport=m["transport"],
                source_type=m["source_type"],
                enabled=enabled,
                available=available,
            )
        )

    return ModelList(
        object="list",
        data=filtered_models,
    )
