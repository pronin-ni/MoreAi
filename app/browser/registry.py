from typing import TYPE_CHECKING
from app.browser.base import BrowserProvider

if TYPE_CHECKING:
    from app.browser.base import BrowserProvider


class ProviderRegistry:
    """Registry for browser automation providers."""

    def __init__(self):
        self._providers: dict[str, type["BrowserProvider"]] = {}
        self._model_to_provider: dict[str, str] = {}
        self._provider_configs: dict[str, dict] = {}

    def register(
        self,
        provider_class: type["BrowserProvider"],
        model_ids: list[str],
        config: dict | None = None,
    ) -> None:
        provider_id = provider_class.provider_id
        self._providers[provider_id] = provider_class

        for model_id in model_ids:
            self._model_to_provider[model_id] = provider_id

        self._provider_configs[provider_id] = config or {}

        from app.core.logging import get_logger

        logger = get_logger(__name__)
        logger.info(
            "Registered provider",
            provider_id=provider_id,
            models=model_ids,
        )

    def get_provider_class(self, model: str) -> type["BrowserProvider"]:
        provider_id = self._model_to_provider.get(model)
        if not provider_id:
            from app.core.errors import BadRequestError

            raise BadRequestError(
                f"Unknown model: {model}. Available models: {list(self._model_to_provider.keys())}",
                details={"requested_model": model, "available_models": list(self._model_to_provider.keys())},
            )

        provider_class = self._providers.get(provider_id)
        if not provider_class:
            from app.core.errors import InternalError

            raise InternalError(f"Provider not found: {provider_id}")

        return provider_class

    def get_provider_config(self, model: str) -> dict:
        provider_id = self._model_to_provider.get(model)
        return self._provider_configs.get(provider_id, {})

    def list_models(self) -> list[dict]:
        result = []
        for model_id, provider_id in self._model_to_provider.items():
            provider_class = self._providers[provider_id]
            result.append(
                {
                    "id": model_id,
                    "provider_id": provider_id,
                    "display_name": provider_class.display_name,
                    "target_url": provider_class.target_url,
                }
            )
        return result


registry = ProviderRegistry()
