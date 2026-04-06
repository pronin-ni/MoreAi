"""
Routing resolver — backward compatibility layer.

Delegates to the centralized RoutingEngine while preserving the old API
for existing code that imports resolve_routing().
"""

from app.core.logging import get_logger
from app.services.routing_engine import routing_engine

logger = get_logger(__name__)


def resolve_routing(model_id: str, default_provider_id: str):
    """Resolve routing for a model — delegates to RoutingEngine.

    Returns an object with:
        - provider_id: the primary provider to use
        - fallbacks: list of fallback provider IDs
        - force_applied: whether force_provider was used
        - primary_applied: whether primary was used
        - max_retries: per-model retry limit (from override)
        - timeout_override: per-model timeout (from override)
    """
    plan = routing_engine.plan(model_id)

    # Build a compatibility result
    primary = plan.primary_provider

    return type("RoutingResult", (), {
        "effective_model_id": primary.canonical_model_id if primary else model_id,
        "provider_id": primary.provider_id if primary else default_provider_id,
        "fallbacks": [c.provider_id for c in plan.candidates[1:]],
        "max_retries": plan.policy.max_retries_per_provider,
        "timeout_override": plan.policy.timeout_override,
        "force_applied": plan.candidates[0].selection_rule == "force_provider" if plan.candidates else False,
        "primary_applied": plan.candidates[0].selection_rule == "primary" if plan.candidates else False,
        "warnings": [],
    })()
