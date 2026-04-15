import importlib
import pkgutil

# Dynamically discover and import all agent provider packages
import app.agents
import app.browser.providers  # noqa: F401

for _finder, agent_pkg_name, _ispkg in pkgutil.iter_modules(app.agents.__path__, app.agents.__name__ + "."):
    if agent_pkg_name.endswith(".provider"):
        try:
            importlib.import_module(agent_pkg_name)
        except Exception as exc:
            from app.core.logging import get_logger
            get_logger(__name__).warning(
                "Failed to import agent provider package",
                package=agent_pkg_name,
                error=str(exc),
            )

from app.agents.registry import registry as agent_registry
from app.browser.registry import registry as browser_registry
from app.core.logging import get_logger
from app.core.transport_filters import filter_models_by_transport
from app.integrations.registry import api_registry
from app.integrations.types import ResolvedModel

logger = get_logger(__name__)


class UnifiedRegistry:
    async def initialize(self) -> None:
        from app.core.transport_filters import is_transport_enabled, log_startup_status

        # Log transport feature flag status
        log_startup_status()

        # Skip initialization for disabled transports
        if is_transport_enabled("api"):
            await api_registry.initialize()
        if is_transport_enabled("agent"):
            await agent_registry.initialize()

    def list_models(self) -> list[dict]:
        models = (
            browser_registry.list_models()
            + api_registry.list_models()
            + agent_registry.list_models()
        )
        return filter_models_by_transport(models)

    def resolve_model(self, model_name: str) -> ResolvedModel:
        if browser_registry.can_resolve_model(model_name):
            canonical_model = browser_registry.resolve_model(model_name)
            provider_class = browser_registry.get_provider_class(canonical_model)
            return ResolvedModel(
                requested_id=model_name,
                canonical_id=canonical_model,
                provider_id=provider_class.provider_id,
                transport="browser",
                source_type="browser",
                execution_strategy="browser_completion",
            )

        if agent_registry.can_resolve_model(model_name):
            resolved = agent_registry.resolve_model(model_name)
            return ResolvedModel(
                requested_id=resolved["requested_id"],
                canonical_id=resolved["canonical_id"],
                provider_id=resolved["provider_id"],
                transport=resolved["transport"],
                source_type=resolved["source_type"],
                execution_strategy=resolved["execution_strategy"],
            )

        return api_registry.resolve_model(model_name)

    def diagnostics(self) -> dict:
        browser_models = browser_registry.list_models()
        return {
            "browser": browser_models,
            "api_integrations": api_registry.diagnostics(),
            "api_models": api_registry.discovered_models(),
            "agent_providers": agent_registry.diagnostics(),
            "agent_models": agent_registry.list_models(),
        }

    def model_names(self) -> list[str]:
        from app.core.transport_filters import filter_strings_by_transport_prefix

        browser_names = [item["id"] for item in browser_registry.list_models()]
        browser_names.extend(browser_registry.available_model_names())
        browser_names.extend(api_registry.discovered_models())
        browser_names.extend(agent_registry.list_models())
        filtered = filter_strings_by_transport_prefix(sorted(set(browser_names)))
        return filtered


unified_registry = UnifiedRegistry()
