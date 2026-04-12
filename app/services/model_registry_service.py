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
    metadata: dict = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}

    @property
    def is_selectable(self) -> bool:
        return self.enabled and self.available

    @property
    def badge_type(self) -> str:
        if not self.available:
            return "unavailable"
        if not self.enabled:
            return "disabled"
        # Free models get a special badge
        if self.metadata.get("free"):
            return "free"
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
            model_metadata = {
                "free": m.get("free", False),
                "only_free_mode": m.get("only_free_mode", False),
            }

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
                    metadata=model_metadata,
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

        # Enrich agent models with provider status
        agent_models = self._enrich_agent_models(agent_models)

        browser_models.sort(key=lambda m: m.display_name.lower())
        api_models.sort(key=lambda m: m.display_name.lower())
        agent_models.sort(key=lambda m: m.display_name.lower())

        return browser_models, api_models, agent_models

    def _enrich_agent_models(self, agent_models: list[ModelViewModel]) -> list[ModelViewModel]:
        """Add provider status metadata to agent models."""
        try:
            from app.agents.registry import registry as agent_registry

            provider_status: dict[str, dict] = {}
            for pid, provider in agent_registry._providers.items():
                provider_status[pid] = {
                    "provider_available": getattr(provider, "_available", False),
                    "provider_mode": getattr(provider, "_mode", "unknown"),
                    "provider_error": getattr(provider, "_error", None),
                    "provider_model_count": len(getattr(provider, "_models", [])),
                }

            for model in agent_models:
                pid = model.provider_id
                status = provider_status.get(pid, {})
                model.metadata["provider_available"] = status.get("provider_available", False)
                model.metadata["provider_mode"] = status.get("provider_mode", "unknown")
                model.metadata["provider_error"] = status.get("provider_error")
        except Exception:
            # If agent registry is not available, skip enrichment
            pass

        return agent_models

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
