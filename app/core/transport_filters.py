"""
Centralized transport-level feature flag filtering.

Provides a single point of control for enabling/disabling
entire transport types (browser, api, agent) across the system.

When a transport is disabled:
- Models are excluded from unified_registry.list_models()
- Models are excluded from ModelSelector candidates
- Models are excluded from routing_engine
- Models are excluded from pipeline stage selection
- Discovery is skipped for that transport
- Models are excluded from scoring/intelligence
- Models do NOT appear in /v1/models
"""

from typing import TypeVar

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


def is_transport_enabled(transport: str) -> bool:
    """Check if a transport type is enabled."""
    return settings.transport_feature_flags.is_transport_enabled(transport)


def filter_models_by_transport(models: list[dict]) -> list[dict]:
    """Filter out models from disabled transports.

    Args:
        models: List of model dicts with 'transport' key.

    Returns:
        Filtered list containing only models from enabled transports.
    """
    if not models:
        return models

    filtered = []
    for m in models:
        transport = m.get("transport", "unknown")
        if is_transport_enabled(transport):
            filtered.append(m)
        else:
            logger.debug(
                "Skipping model due to disabled transport",
                model_id=m.get("id", "unknown"),
                transport=transport,
            )

    return filtered


def filter_strings_by_transport_prefix(strings: list[str]) -> list[str]:
    """Filter out strings that start with a disabled transport prefix.

    Used for filtering model IDs like "browser/...", "api/...", "agent/...".

    Args:
        strings: List of model ID strings.

    Returns:
        Filtered list containing only IDs from enabled transports.
    """
    if not strings:
        return strings

    filtered = []
    for s in strings:
        # Extract transport prefix (e.g., "browser" from "browser/qwen")
        transport = s.split("/")[0] if "/" in s else "unknown"
        if is_transport_enabled(transport):
            filtered.append(s)
        else:
            logger.debug(
                "Skipping model ID due to disabled transport",
                model_id=s,
                transport=transport,
            )

    return filtered


def log_startup_status() -> None:
    """Log transport feature flag status at startup."""
    flags = settings.transport_feature_flags

    browser_status = "ENABLED" if flags.browser_providers else "DISABLED"
    api_status = "ENABLED" if flags.api_providers else "DISABLED"
    agent_status = "ENABLED" if flags.agent_providers else "DISABLED"

    logger.info(
        "Transport feature flags",
        browser=browser_status,
        api=api_status,
        agent=agent_status,
    )

    # Log specific warnings for disabled transports
    if not flags.browser_providers:
        logger.warning(
            "Browser providers disabled via config (ENABLE_BROWSER_PROVIDERS=false) — "
            "all browser models will be excluded from selection, pipelines, and /v1/models"
        )
    if not flags.api_providers:
        logger.warning(
            "API providers disabled via config (ENABLE_API_PROVIDERS=false) — "
            "all API models will be excluded from selection, pipelines, and /v1/models"
        )
    if not flags.agent_providers:
        logger.warning(
            "Agent providers disabled via config (ENABLE_AGENT_PROVIDERS=false) — "
            "all agent models will be excluded from selection, pipelines, and /v1/models"
        )
