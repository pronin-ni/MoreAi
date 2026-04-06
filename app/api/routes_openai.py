from fastapi import APIRouter, Request

from app.browser.execution.dispatcher import browser_dispatcher
from app.core.errors import APIError, BadRequestError, InternalError
from app.core.logging import bind_request_id, clear_request_id, get_logger
from app.registry.unified import unified_registry
from app.schemas.openai import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ErrorResponse,
    HealthResponse,
    ModelList,
)
from app.services.chat_proxy_service import service
from app.utils.openai_mapper import create_model_list

logger = get_logger(__name__)

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    return HealthResponse(status="healthy", version="0.1.0")


@router.get("/v1/models", response_model=ModelList)
async def list_models() -> ModelList:
    logger.info("Listing available models")
    return create_model_list()


@router.get("/diagnostics/integrations")
async def list_integrations_diagnostics() -> dict:
    logger.info("Listing integration diagnostics")
    diagnostics = unified_registry.diagnostics()
    diagnostics["browser_execution"] = browser_dispatcher.diagnostics()
    return diagnostics


@router.get("/diagnostics/models")
async def list_models_diagnostics() -> dict:
    logger.info("Listing model diagnostics")
    return {"models": unified_registry.list_models()}


@router.post(
    "/v1/chat/completions",
    response_model=ChatCompletionResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Bad request error"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
        503: {"model": ErrorResponse, "description": "Service unavailable"},
        504: {"model": ErrorResponse, "description": "Gateway timeout"},
    },
)
async def create_chat_completion(
    request: Request,
    body: ChatCompletionRequest,
) -> ChatCompletionResponse:
    request_id = bind_request_id()

    try:
        if body.stream:
            raise BadRequestError(
                "Streaming is not supported yet. Set stream=false.",
                details={"stream": True},
            )

        logger.info(
            "Received chat completion request",
            request_id=request_id,
            model=body.model,
            message_count=len(body.messages),
        )

        response = await service.process_completion(body, request_id)

        logger.info(
            "Chat completion response sent",
            request_id=request_id,
            response_id=response.id,
        )

        return response

    except APIError:
        raise
    except Exception as e:
        logger.exception(
            "Unexpected error in chat completion",
            request_id=request_id,
            error=str(e),
        )
        raise InternalError(
            f"Internal server error: {str(e)}",
            details={"request_id": request_id},
        ) from e
    finally:
        clear_request_id()
