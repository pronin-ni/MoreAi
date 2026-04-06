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
        from app.admin.config_manager import config_manager

        raw_models = unified_registry.list_models()
        overrides = config_manager.overrides.models
        result = []
        
        for m in raw_models:
            model_id = m["id"]

            # Apply admin overrides
            override = overrides.get(model_id)
            enabled = m.get("enabled", True)
            visibility = "public"
            
            if override:
                if override.enabled is not None:
                    enabled = override.enabled
                if override.visibility is not None:
                    visibility = override.visibility

            # Skip hidden models
            if visibility == "hidden":
                continue

            display_name = model_id.split("/", 1)[-1].replace("-", " ").title()
            provider_id = m.get("provider_id", "")

            if model_id.startswith("browser/"):
                display_name = (
                    provider_id.replace("_", " ").title() if provider_id else display_name
                )
            elif model_id.startswith("api/"):
                upstream_model = model_id.split("/", 2)[-1]
                if provider_id:
                    display_name = f"{provider_id.replace('_', ' ').title()} | {upstream_model}"
                else:
                    display_name = upstream_model
            elif model_id.startswith("agent/"):
                # agent/opencode/<provider_key>/<model_id> → show model_id
                parts = model_id.split("/", 3)
                if len(parts) >= 4:
                    display_name = parts[3].replace("-", " ").title()
                else:
                    display_name = model_id.split("/", 1)[-1].replace("-", " ").title()
            else:
                continue

            aliases = m.get("aliases", [])

            result.append(
                ModelViewModel(
                    id=model_id,
                    display_name=display_name,
                    provider_id=provider_id,
                    transport=m.get("transport", "unknown"),
                    source_type=m.get("source_type", "unknown"),
                    enabled=enabled,
                    available=m.get("available", True),
                    aliases=aliases,
                )
            )

        return result

    def group_models(
        self,
    ) -> tuple[list[ModelViewModel], list[ModelViewModel], list[ModelViewModel]]:
        all_models = self.list_models()

        browser_models = [m for m in all_models if m.transport == "browser"]
        api_models = [m for m in all_models if m.transport == "api"]
        agent_models = [m for m in all_models if m.transport == "agent"]

        browser_models.sort(key=lambda m: m.display_name.lower())
        api_models.sort(key=lambda m: m.display_name.lower())
        agent_models.sort(key=lambda m: m.display_name.lower())

        return browser_models, api_models, agent_models

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
