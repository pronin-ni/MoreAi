from typing import TYPE_CHECKING

from app.browser.base import BrowserProvider

if TYPE_CHECKING:
    from app.browser.base import BrowserProvider


class ProviderRegistry:
    """Registry for browser automation providers."""

    def __init__(self):
        self._providers: dict[str, type[BrowserProvider]] = {}
        self._canonical_models: dict[str, str] = {}
        self._aliases: dict[str, str] = {}
        self._provider_models: dict[str, str] = {}
        self._provider_configs: dict[str, dict] = {}

    def register(
        self,
        provider_class: type[BrowserProvider],
        canonical_model_id: str,
        alias_ids: list[str] | None = None,
        config: dict | None = None,
    ) -> None:
        provider_id = provider_class.provider_id
        self._providers[provider_id] = provider_class
        self._canonical_models[canonical_model_id] = provider_id
        self._provider_models[provider_id] = canonical_model_id

        for alias_id in alias_ids or []:
            self._aliases[alias_id] = canonical_model_id

        self._provider_configs[provider_id] = config or {}

        from app.core.logging import get_logger

        logger = get_logger(__name__)
        logger.info(
            "Registered provider",
            provider_id=provider_id,
            canonical_model=canonical_model_id,
            aliases=alias_ids or [],
        )

    def resolve_model(self, model: str) -> str:
        canonical_model = self._aliases.get(model, model)
        if canonical_model not in self._canonical_models:
            from app.core.errors import BadRequestError

            raise BadRequestError(
                f"Unknown model: {model}. Available models: {self.available_model_names()}",
                details={
                    "requested_model": model,
                    "available_models": self.available_model_names(),
                },
            )
        return canonical_model

    def can_resolve_model(self, model: str) -> bool:
        try:
            self.resolve_model(model)
        except Exception:
            return False
        return True

    def available_model_names(self) -> list[str]:
        return sorted(list(self._canonical_models.keys()) + list(self._aliases.keys()))

    def get_provider_class(self, model: str) -> type[BrowserProvider]:
        provider_id = self._canonical_models[self.resolve_model(model)]

        provider_class = self._providers.get(provider_id)
        if not provider_class:
            from app.core.errors import InternalError

            raise InternalError(f"Provider not found: {provider_id}")

        return provider_class

    def get_provider_config(self, model: str) -> dict:
        canonical_model = self.resolve_model(model)
        provider_id = self._canonical_models.get(canonical_model)
        return self._provider_configs.get(provider_id, {})

    def get_canonical_model_id(self, provider_id: str) -> str:
        return self._provider_models[provider_id]

    def list_models(self) -> list[dict]:
        result = []
        for model_id, provider_id in self._canonical_models.items():
            provider_class = self._providers[provider_id]
            result.append(
                {
                    "id": model_id,
                    "provider_id": provider_id,
                    "display_name": provider_class.display_name,
                    "target_url": provider_class.target_url,
                    "transport": "browser",
                    "source_type": "browser",
                    "enabled": True,
                    "available": True,
                    "aliases": [
                        alias for alias, canonical in self._aliases.items() if canonical == model_id
                    ],
                }
            )
        return result


registry = ProviderRegistry()
