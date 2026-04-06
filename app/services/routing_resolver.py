"""
Routing resolver — applies admin routing overrides to the request path.

Resolves:
  - force_provider: redirect model to specific provider
  - primary: override the canonical provider for a model
  - fallbacks: ordered list of fallback provider IDs
  - max_retries: per-model retry limit (overrides global default)
  - timeout_override: per-model execution timeout (overrides global default)

Only fields that are safe to apply at runtime are wired. Unsupported fields
return their configured value but are noted in the resolution metadata.
"""

from dataclasses import dataclass, field

from app.admin.config_manager import config_manager
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class RoutingResolution:
    """Result of resolving a model_id through routing overrides."""

    # The model_id to actually use (may differ from requested if force_provider is set)
    effective_model_id: str

    # Provider to route to (may differ from registry default if force_provider/primary is set)
    provider_id: str | None = None

    # Ordered list of fallback provider IDs (empty means use registry default)
    fallbacks: list[str] = field(default_factory=list)

    # Max retries for this model (None means use global default)
    max_retries: int | None = None

    # Execution timeout override (None means use global default)
    timeout_override: int | None = None

    # Whether force_provider was applied
    force_applied: bool = False

    # Whether primary was applied
    primary_applied: bool = False

    # Warnings about fields that are configured but not yet fully wired
    warnings: list[str] = field(default_factory=list)


def resolve_routing(model_id: str, default_provider_id: str) -> RoutingResolution:
    """Resolve routing overrides for a model.

    Args:
        model_id: The requested model ID (e.g. "browser/qwen").
        default_provider_id: The provider ID from the registry (e.g. "qwen").

    Returns:
        RoutingResolution with effective routing parameters.
    """
    override = config_manager.overrides.models.get(model_id)
    routing_override = config_manager.overrides.routing.get(model_id)

    result = RoutingResolution(
        effective_model_id=model_id,
        provider_id=default_provider_id,
    )

    if not override and not routing_override:
        return result

    # ── force_provider: redirect model to a specific provider ──
    if override and override.force_provider:
        result.provider_id = override.force_provider
        result.force_applied = True
        result.effective_model_id = model_id  # model_id stays the same, provider changes
        logger.info(
            "Routing force_provider applied",
            model_id=model_id,
            force_provider=override.force_provider,
        )

    # ── primary: override the canonical provider ──
    if routing_override and routing_override.primary:
        result.provider_id = routing_override.primary
        result.primary_applied = True
        logger.info(
            "Routing primary applied",
            model_id=model_id,
            primary=routing_override.primary,
        )

    # ── fallbacks: ordered list of fallback providers ──
    if routing_override and routing_override.fallbacks:
        result.fallbacks = list(routing_override.fallbacks)

    # ── max_retries: per-model retry limit ──
    if routing_override and routing_override.max_retries is not None:
        result.max_retries = routing_override.max_retries

    # ── timeout_override: per-model execution timeout ──
    if routing_override and routing_override.timeout_override is not None:
        result.timeout_override = routing_override.timeout_override

    return result
