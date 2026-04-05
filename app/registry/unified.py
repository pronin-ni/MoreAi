import app.browser.providers  # noqa: F401

from app.browser.registry import registry as browser_registry
from app.integrations.registry import api_registry
from app.integrations.types import ResolvedModel


class UnifiedRegistry:
    async def initialize(self) -> None:
        await api_registry.initialize()

    def list_models(self) -> list[dict]:
        return browser_registry.list_models() + api_registry.list_models()

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
        return api_registry.resolve_model(model_name)

    def diagnostics(self) -> dict:
        browser_models = browser_registry.list_models()
        return {
            "browser": browser_models,
            "api_integrations": api_registry.diagnostics(),
            "api_models": api_registry.discovered_models(),
        }

    def model_names(self) -> list[str]:
        browser_names = [item["id"] for item in browser_registry.list_models()]
        browser_names.extend(browser_registry.available_model_names())
        browser_names.extend(api_registry.discovered_models())
        return sorted(set(browser_names))


unified_registry = UnifiedRegistry()
