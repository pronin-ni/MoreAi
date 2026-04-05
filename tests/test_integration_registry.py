import pytest

from app.integrations.registry import APIRegistry
from app.integrations.types import ModelDefinition
from app.registry.unified import UnifiedRegistry


class TestAPIRegistry:
    @pytest.mark.asyncio
    async def test_initialize_creates_known_g4f_auto_fallback_model(self, monkeypatch):
        registry = APIRegistry()

        async def fake_discover(self):
            if self.definition.integration_id == "g4f-auto":
                return self._fallback_model_definitions(available=True)
            return []

        monkeypatch.setattr(
            "app.integrations.adapters.OpenAICompatibleIntegration.discover_models",
            fake_discover,
        )
        monkeypatch.setattr(
            "app.integrations.adapters.ClientBasedIntegration.discover_models",
            fake_discover,
        )
        monkeypatch.setattr(
            "app.integrations.adapters.OllamaFreeAPIIntegration.discover_models",
            fake_discover,
        )

        await registry.initialize()

        assert registry.can_resolve_model("api/g4f-auto/default") is True

    @pytest.mark.asyncio
    async def test_initialize_registers_ollamafreeapi_models(self, monkeypatch):
        registry = APIRegistry()

        async def fake_openai_discover(self):
            return []

        async def fake_ollama_discover(self):
            if self.definition.integration_id != "ollamafreeapi":
                return []
            return [
                ModelDefinition(
                    id="api/ollamafreeapi/llama3.3:70b",
                    provider_id="ollamafreeapi",
                    transport="api",
                    source_type="client_based",
                    enabled=True,
                    available=True,
                )
            ]

        monkeypatch.setattr(
            "app.integrations.adapters.OpenAICompatibleIntegration.discover_models",
            fake_openai_discover,
        )
        monkeypatch.setattr(
            "app.integrations.adapters.ClientBasedIntegration.discover_models",
            fake_openai_discover,
        )
        monkeypatch.setattr(
            "app.integrations.adapters.OllamaFreeAPIIntegration.discover_models",
            fake_ollama_discover,
        )

        await registry.initialize()

        assert registry.can_resolve_model("api/ollamafreeapi/llama3.3:70b") is True
        resolved = registry.resolve_model("api/ollamafreeapi/llama3.3:70b")
        assert resolved.provider_id == "ollamafreeapi"
        assert resolved.transport == "api"


class TestUnifiedRegistry:
    def test_browser_alias_resolves_to_canonical(self):
        unified = UnifiedRegistry()
        resolved = unified.resolve_model("qwen")

        assert resolved.canonical_id == "browser/qwen"
        assert resolved.transport == "browser"

    def test_browser_canonical_resolves_directly(self):
        unified = UnifiedRegistry()
        resolved = unified.resolve_model("browser/deepseek")

        assert resolved.canonical_id == "browser/deepseek"
        assert resolved.provider_id == "deepseek"
