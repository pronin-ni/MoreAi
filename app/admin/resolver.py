"""
ConfigResolver — computes effective config from BaseSettings + RuntimeOverrides.

Provides provenance: for each field, shows base_value, override_value, effective_value, source.
"""

from typing import Any

from app.admin.config_manager import config_manager
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


# ── Field policy metadata ──


class ConfigFieldPolicy:
    """Metadata about a config field's update safety."""

    def __init__(
        self,
        category: str,  # safe_live | conditional_live | restart_only
        description: str,
        requires_restart: bool = False,
        reason: str | None = None,
        caveat: str | None = None,
        min_val: float | None = None,
        max_val: float | None = None,
        enum_values: list[str] | None = None,
        field_type: str = "str",
    ):
        self.category = category
        self.description = description
        self.requires_restart = requires_restart
        self.reason = reason
        self.caveat = caveat
        self.min_val = min_val
        self.max_val = max_val
        self.enum_values = enum_values
        self.field_type = field_type


CONFIG_FIELD_POLICY: dict[str, ConfigFieldPolicy] = {
    # Provider fields
    "providers.*.enabled": ConfigFieldPolicy(
        category="safe_live",
        field_type="bool",
        description="Enable/disable provider. In-flight requests complete normally.",
    ),
    "providers.*.concurrency_limit": ConfigFieldPolicy(
        category="conditional_live",
        field_type="int",
        description="Max concurrent sessions. New tasks use new limit; in-flight tasks unchanged.",
        caveat="Transient over-capacity possible for in-flight tasks",
        min_val=1,
        max_val=100,
    ),
    "providers.*.priority": ConfigFieldPolicy(
        category="safe_live",
        field_type="int",
        description="Provider priority for routing. Applied to next routing decision.",
    ),
    # Model fields
    "models.*.enabled": ConfigFieldPolicy(
        category="safe_live",
        field_type="bool",
        description="Enable/disable model. In-flight calls with this model complete normally.",
    ),
    "models.*.visibility": ConfigFieldPolicy(
        category="safe_live",
        field_type="str",
        description="Whether model appears in /v1/models listing.",
        enum_values=["public", "hidden", "experimental"],
    ),
    "models.*.force_provider": ConfigFieldPolicy(
        category="safe_live",
        field_type="str",
        description="Pin model to specific provider (null = auto).",
    ),
    # Routing fields
    "routing.*.primary": ConfigFieldPolicy(
        category="safe_live",
        field_type="str",
        description="Primary provider for this model.",
    ),
    "routing.*.fallbacks": ConfigFieldPolicy(
        category="safe_live",
        field_type="list",
        description="Fallback provider chain.",
    ),
    "routing.*.max_retries": ConfigFieldPolicy(
        category="safe_live",
        field_type="int",
        description="Max retry attempts.",
        min_val=0,
        max_val=10,
    ),
    # Queue fields (restart-only for now, could be conditional_live later)
    "browser_pool_size": ConfigFieldPolicy(
        category="restart_only",
        field_type="int",
        description="Total browser worker pool size.",
        requires_restart=True,
        reason="Workers are created at startup",
        min_val=1,
        max_val=50,
    ),
}


def get_field_policy(field_path: str) -> ConfigFieldPolicy | None:
    """Look up policy for a field path, supporting wildcards."""
    # Exact match
    if field_path in CONFIG_FIELD_POLICY:
        return CONFIG_FIELD_POLICY[field_path]

    # Wildcard match
    for pattern, policy in CONFIG_FIELD_POLICY.items():
        pattern_parts = pattern.split(".")
        path_parts = field_path.split(".")
        if len(pattern_parts) != len(path_parts):
            continue
        if all(
            p == "*" or p == q for p, q in zip(pattern_parts, path_parts, strict=False)
        ):
            return policy

    return None


# ── Effective config view ──


def _field_view(
    base_value: Any,
    override_value: Any,
    field_path: str,
) -> dict[str, Any]:
    """Build a single field view with provenance."""
    effective = override_value if override_value is not None else base_value
    source = "override" if override_value is not None else "base"
    policy = get_field_policy(field_path)

    result: dict[str, Any] = {
        "base_value": base_value,
        "override_value": override_value,
        "effective_value": effective,
        "source": source,
    }

    if policy:
        result["requires_restart"] = policy.requires_restart
        result["category"] = policy.category
        if policy.caveat:
            result["caveat"] = policy.caveat

    return result


def resolve_provider_effective(provider_id: str) -> dict[str, Any]:
    """Return effective config for a single provider with provenance."""
    base = _base_for_provider(provider_id)
    override = config_manager.overrides.providers.get(provider_id)

    return {
        "provider_id": provider_id,
        "enabled": _field_view(
            base.get("enabled", True),
            override.enabled if override else None,
            f"providers.{provider_id}.enabled",
        ),
        "concurrency_limit": _field_view(
            base.get("concurrency_limit"),
            override.concurrency_limit if override else None,
            f"providers.{provider_id}.concurrency_limit",
        ),
        "priority": _field_view(
            base.get("priority", 0),
            override.priority if override else None,
            f"providers.{provider_id}.priority",
        ),
        "override_state": override.state if override else "none",
        "override_error": override.error if override and override.error else None,
        "override_applied_at": override.applied_at if override else None,
    }


def resolve_model_effective(model_id: str) -> dict[str, Any]:
    """Return effective config for a single model with provenance."""
    override = config_manager.overrides.models.get(model_id)

    return {
        "model_id": model_id,
        "enabled": _field_view(
            True,
            override.enabled if override else None,
            f"models.{model_id}.enabled",
        ),
        "visibility": _field_view(
            "public",
            override.visibility if override else None,
            f"models.{model_id}.visibility",
        ),
        "force_provider": _field_view(
            None,
            override.force_provider if override else None,
            f"models.{model_id}.force_provider",
        ),
        "override_state": override.state if override else "none",
        "override_error": override.error if override and override.error else None,
    }


def resolve_routing_effective(model_id: str) -> dict[str, Any]:
    """Return effective routing config for a single model."""
    override = config_manager.overrides.routing.get(model_id)

    return {
        "model_id": model_id,
        "primary": _field_view(
            None,
            override.primary if override else None,
            f"routing.{model_id}.primary",
        ),
        "fallbacks": _field_view(
            None,
            override.fallbacks if override else None,
            f"routing.{model_id}.fallbacks",
        ),
        "max_retries": _field_view(
            1,
            override.max_retries if override else None,
            f"routing.{model_id}.max_retries",
        ),
        "timeout_override": _field_view(
            None,
            override.timeout_override if override else None,
            f"routing.{model_id}.timeout_override",
        ),
    }


def resolve_all_effective() -> dict[str, Any]:
    """Return complete effective config snapshot."""
    all_provider_ids = config_manager._known_providers or set()
    all_model_ids = config_manager._known_models or set()

    return {
        "version": config_manager.overrides.version,
        "updated_at": config_manager.overrides.updated_at,
        "state": config_manager.overrides.state,
        "providers": {
            pid: resolve_provider_effective(pid) for pid in sorted(all_provider_ids)
        },
        "models": {
            mid: resolve_model_effective(mid) for mid in sorted(all_model_ids)
        },
        "routing": {
            mid: resolve_routing_effective(mid)
            for mid in sorted(config_manager.overrides.routing.keys())
        },
        "field_policy": {
            path: {
                "category": p.category,
                "description": p.description,
                "requires_restart": p.requires_restart,
                "field_type": p.field_type,
                **({"min": p.min_val} if p.min_val is not None else {}),
                **({"max": p.max_val} if p.max_val is not None else {}),
                **({"enum": p.enum_values} if p.enum_values else {}),
                **({"caveat": p.caveat} if p.caveat else {}),
                **({"reason": p.reason} if p.reason else {}),
            }
            for path, p in CONFIG_FIELD_POLICY.items()
        },
    }


def _base_for_provider(provider_id: str) -> dict[str, Any]:
    """Get base settings for a provider from Settings."""
    provider_id_lower = provider_id.lower()

    base: dict[str, Any] = {"enabled": True}

    # Map provider IDs to their Settings
    browser_providers = {
        "qwen", "glm", "chatgpt", "yandex", "kimi", "deepseek",
    }
    if provider_id_lower in browser_providers:
        base["concurrency_limit"] = settings.browser_pool_size
        base["priority"] = 0
    elif provider_id_lower in ("opencode",):
        base["concurrency_limit"] = None  # no concurrency limit for agent
        base["priority"] = 0
    else:
        # API providers
        base["concurrency_limit"] = None
        base["priority"] = 0

    return base


class ConfigResolverCompat:
    """Compatibility layer for admin config resolution."""

    @staticmethod
    def resolve_provider_effective(provider_id: str) -> dict[str, Any]:
        return resolve_provider_effective(provider_id)

    @staticmethod
    def resolve_model_effective(model_id: str) -> dict[str, Any]:
        return resolve_model_effective(model_id)

    @staticmethod
    def resolve_routing_effective(model_id: str) -> dict[str, Any]:
        return resolve_routing_effective(model_id)

    @staticmethod
    def resolve_all_effective() -> dict[str, Any]:
        return resolve_all_effective()


resolver = ConfigResolverCompat()
