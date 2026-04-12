from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import app.agents.opencode.provider  # noqa: F401
import app.browser.providers  # noqa: F401
from app.admin.config_manager import config_manager
from app.admin.observer import observer
from app.admin.router import router as admin_router
from app.api.rate_limit import RateLimitMiddleware
from app.api.routes_home import router as home_router
from app.api.routes_openai import router as openai_router
from app.api.routes_studio import router as studio_router
from app.api.routes_ui import router as ui_router
from app.browser.execution.dispatcher import browser_dispatcher
from app.browser.registry import registry as browser_registry
from app.core.config import settings
from app.core.errors import APIError
from app.core.logging import configure_logging, get_logger
from app.integrations.registry import api_registry
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

    # Register known providers/models with config manager for validation
    # Collect agent provider IDs dynamically from agent registry
    agent_provider_ids = set(agent_registry._providers.keys()) | set(
        p.provider_id for p in agent_registry._pending_providers
    )
    known_providers = set(browser_registry._providers) | set(api_registry._adapters) | agent_provider_ids
    known_models = set()
    for m in unified_registry.list_models():
        known_models.add(m["id"])
    config_manager.register_known_providers(known_providers)
    config_manager.register_known_models(known_models)
    logger.info(
        "Config manager registered known providers/models",
        providers=len(known_providers),
        models=len(known_models),
    )

    # Start admin observer (background task for config change propagation)
    await observer.start()

    # Start webhook dispatcher
    from app.services.webhooks import webhook_dispatcher
    await webhook_dispatcher.start()
    logger.info("Webhook dispatcher started")

    # Start proactive baseline refresher
    from app.browser.dom.refresh import baseline_refresher
    await baseline_refresher.start()
    logger.info("Baseline refresher started")

    # Initialize pipeline subsystem
    from app.pipeline.executor import initialize_pipelines
    initialize_pipelines()
    logger.info("Pipeline subsystem initialized")

    # Initialize intelligence subsystem (capability tags)
    from app.intelligence.tags import capability_registry
    capability_registry.initialize()
    logger.info("Model intelligence subsystem initialized")

    # Start scoring snapshot scheduler (background thread)
    from app.pipeline.observability.scoring_trends import snapshot_scheduler
    snapshot_scheduler.start()
    logger.info("Scoring snapshot scheduler started")

    # Start model discovery service (periodic refresh background task)
    from app.services.model_discovery import model_discovery_service
    await model_discovery_service.discover_all()
    model_discovery_service.start()
    logger.info("Model discovery service started")

    yield

    logger.info("Shutting down MoreAI Proxy service")

    # Stop baseline refresher
    from app.browser.dom.refresh import baseline_refresher
    await baseline_refresher.stop()
    logger.info("Baseline refresher stopped")

    # Stop webhook dispatcher
    from app.services.webhooks import webhook_dispatcher
    await webhook_dispatcher.stop()
    logger.info("Webhook dispatcher stopped")

    # Stop admin observer
    await observer.stop()
    logger.info("Admin observer stopped")

    # Stop scoring snapshot scheduler
    from app.pipeline.observability.scoring_trends import snapshot_scheduler
    snapshot_scheduler.stop()
    logger.info("Scoring snapshot scheduler stopped")

    # Stop model discovery service
    from app.services.model_discovery import model_discovery_service
    await model_discovery_service.stop()
    logger.info("Model discovery service stopped")

    await browser_dispatcher.shutdown()
    logger.info("Browser dispatcher shutdown complete")

    # Shutdown all managed agent providers dynamically
    from app.agents.registry import registry as agent_registry
    for provider_id, provider in list(agent_registry._providers.items()):
        if hasattr(provider, "shutdown"):
            try:
                await provider.shutdown()
                logger.info("Agent provider shutdown complete", provider_id=provider_id)
            except Exception as exc:
                logger.warning(
                    "Agent provider shutdown failed",
                    provider_id=provider_id,
                    error=str(exc),
                )


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

# Rate limiting middleware (must be after CORS)
app.add_middleware(
    RateLimitMiddleware,
    enabled=True,
    default_rpm=60,
    default_burst=10,
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


app.include_router(home_router)
app.include_router(openai_router)
app.include_router(ui_router)
app.include_router(studio_router)
app.include_router(admin_router)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
