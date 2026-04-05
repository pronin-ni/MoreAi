from dataclasses import dataclass

from app.registry.unified import unified_registry


@dataclass
class ModelViewModel:
    id: str
    display_name: str
    provider_id: str
    transport: str
    source_type: str
    enabled: bool
    available: bool
    aliases: list[str]

    @property
    def is_selectable(self) -> bool:
        return self.enabled and self.available

    @property
    def badge_type(self) -> str:
        if not self.available:
            return "unavailable"
        if not self.enabled:
            return "disabled"
        return self.transport


class ModelRegistryService:
    def list_models(self) -> list[ModelViewModel]:
        raw_models = unified_registry.list_models()
        result = []
        for m in raw_models:
            model_id = m["id"]
            if not model_id.startswith(("browser/", "api/")):
                continue

            display_name = model_id.split("/", 1)[-1].replace("-", " ").title()

            if model_id.startswith("browser/"):
                provider_id = m.get("provider_id", "")
                display_name = (
                    provider_id.replace("_", " ").title() if provider_id else display_name
                )
            else:
                provider_id = m.get("provider_id", "")
                if "/" in provider_id:
                    display_name = provider_id.split("/", 1)[-1].replace("-", " ").title()
                elif provider_id:
                    display_name = provider_id.replace("_", " ").title()

            aliases = m.get("aliases", [])

            result.append(
                ModelViewModel(
                    id=model_id,
                    display_name=display_name,
                    provider_id=provider_id,
                    transport=m.get("transport", "unknown"),
                    source_type=m.get("source_type", "unknown"),
                    enabled=m.get("enabled", True),
                    available=m.get("available", True),
                    aliases=aliases,
                )
            )

        return result

    def group_models(
        self,
    ) -> tuple[list[ModelViewModel], list[ModelViewModel]]:
        all_models = self.list_models()

        browser_models = [m for m in all_models if m.transport == "browser"]
        api_models = [m for m in all_models if m.transport == "api"]

        browser_models.sort(key=lambda m: m.display_name.lower())
        api_models.sort(key=lambda m: m.display_name.lower())

        return browser_models, api_models

    def filter_models(self, query: str) -> list[ModelViewModel]:
        all_models = self.list_models()

        if not query:
            return all_models

        q = query.lower()
        return [
            m
            for m in all_models
            if q in m.id.lower()
            or q in m.display_name.lower()
            or q in m.provider_id.lower()
            or any(q in alias.lower() for alias in m.aliases)
        ]


service = ModelRegistryService()
