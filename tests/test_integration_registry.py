import pytest

from app.integrations.registry import APIRegistry
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

        await registry.initialize()

        assert registry.can_resolve_model("api/g4f-auto/default") is True


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
