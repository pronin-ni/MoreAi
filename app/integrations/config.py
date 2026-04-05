import tomllib
import os
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings
from app.integrations.types import IntegrationDefinition, IntegrationRuntimeConfig


@dataclass(slots=True)
class IntegrationsConfigSnapshot:
    global_enabled: bool
    auto_discover_models: bool
    allow_fallback_models: bool
    timeout_seconds: int
    retry_attempts: int
    by_integration: dict[str, IntegrationRuntimeConfig]


def load_integrations_config(
    definitions: list[IntegrationDefinition],
) -> IntegrationsConfigSnapshot:
    raw = _load_toml_config(Path(settings.integrations_config_path))
    integrations_section = raw.get("integrations", {}) if isinstance(raw, dict) else {}

    by_integration: dict[str, IntegrationRuntimeConfig] = {}
    for definition in definitions:
        config_key = definition.integration_id.replace("-", "_")
        integration_overrides = integrations_section.get(config_key, {})
        if not isinstance(integration_overrides, dict):
            integration_overrides = {}

        base_enabled = settings.integrations_enabled and definition.enabled_by_default
        enabled = bool(integration_overrides.get("enabled", base_enabled))
        base_url = integration_overrides.get("base_url", definition.base_url)
        api_key, api_key_source = _resolve_api_key(definition.integration_id, integration_overrides)
        fallback_models = list(
            integration_overrides.get("fallback_models", definition.fallback_models)
        )
        discover_models = bool(
            integration_overrides.get("discover_models", settings.integrations_auto_discover_models)
        )
        timeout_seconds = int(
            integration_overrides.get(
                "timeout_seconds",
                settings.integrations_discovery_timeout_seconds,
            )
        )
        retry_attempts = int(
            integration_overrides.get(
                "retry_attempts",
                settings.integrations_retry_attempts,
            )
        )

        by_integration[definition.integration_id] = IntegrationRuntimeConfig(
            enabled=enabled,
            base_url=base_url,
            api_key=api_key,
            api_key_source=api_key_source,
            fallback_models=fallback_models,
            discover_models=discover_models,
            timeout_seconds=timeout_seconds,
            retry_attempts=retry_attempts,
        )

    return IntegrationsConfigSnapshot(
        global_enabled=settings.integrations_enabled,
        auto_discover_models=settings.integrations_auto_discover_models,
        allow_fallback_models=settings.integrations_allow_fallback_models,
        timeout_seconds=settings.integrations_discovery_timeout_seconds,
        retry_attempts=settings.integrations_retry_attempts,
        by_integration=by_integration,
    )


def _load_toml_config(path: Path) -> dict:
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _resolve_api_key(integration_id: str, integration_overrides: dict) -> tuple[str | None, str]:
    specific_env_name = f"INTEGRATION_{integration_id.replace('-', '_').upper()}_API_KEY"
    specific_env_value = os.getenv(specific_env_name)
    if specific_env_value:
        return specific_env_value, "integration_env"

    if integration_id.startswith("g4f-") and settings.g4f_api_key:
        return settings.g4f_api_key, "g4f_shared_env"

    toml_value = integration_overrides.get("api_key")
    if toml_value:
        return str(toml_value), "toml"

    return None, "none"
