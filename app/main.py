from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import app.agents.opencode.provider  # noqa: F401
import app.browser.providers  # noqa: F401
from app.api.routes_openai import router as openai_router
from app.api.routes_ui import router as ui_router
from app.browser.execution.dispatcher import browser_dispatcher
from app.core.config import settings
from app.core.errors import APIError
from app.core.logging import configure_logging, get_logger
from app.registry.unified import unified_registry

configure_logging(settings.log_level)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting MoreAI Proxy service", version="0.1.0")

    await browser_dispatcher.initialize()
    logger.info("Browser dispatcher initialized")

    await unified_registry.initialize()
    logger.info("Unified registry initialized")

    yield

    logger.info("Shutting down MoreAI Proxy service")
    await browser_dispatcher.shutdown()
    logger.info("Browser dispatcher shutdown complete")

    # Shutdown managed OpenCode subprocess
    from app.agents.opencode.provider import provider as opencode_provider
    await opencode_provider.shutdown()
    logger.info("OpenCode provider shutdown complete")


app = FastAPI(
    title="MoreAI Proxy",
    description="OpenAI-compatible API proxy with browser automation for Qwen Chat",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(APIError)
async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "message": exc.detail.get("message", "Unknown error"),
            "type": exc.detail.get("type", "internal_error"),
            "details": exc.detail.get("details", {}),
        },
    )


app.include_router(openai_router)
app.include_router(ui_router)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/")
async def root():
    return {"message": "MoreAI Proxy is running", "version": "0.1.0"}
