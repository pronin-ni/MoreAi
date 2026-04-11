"""
Tests for OpenRouter API provider.

Covers:
- Config tests for OPENROUTER_ONLY_FREE, OPENROUTER_ENABLED
- Model discovery with :free suffix filtering
- Inclusion of openrouter/free router model
- Routing (canonical → upstream model mapping)
- Graceful missing API key handling
- Free badge in model registry service
"""

import pytest

from app.integrations.types import (
    IntegrationDefinition,
    IntegrationRuntimeConfig,
)

# ── Fixtures ──


def _make_openrouter_definition(**overrides) -> IntegrationDefinition:
    defaults = {
        "integration_id": "openrouter",
        "display_name": "OpenRouter",
        "integration_type": "openai_compatible",
        "group": "supported_api_route",
        "source_type": "external_api",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_requirement": "required",
        "enabled_by_default": True,
        "fallback_models": [],
    }
    defaults.update(overrides)
    return IntegrationDefinition(**defaults)


def _make_openrouter_runtime_config(
    enabled=True,
    api_key="test-key",
    base_url="https://openrouter.ai/api/v1",
    **overrides,
) -> IntegrationRuntimeConfig:
    return IntegrationRuntimeConfig(
        enabled=enabled,
        base_url=base_url,
        api_key=api_key,
        api_key_source="test",
        fallback_models=[],
        discover_models=True,
        timeout_seconds=10,
        retry_attempts=1,
        **overrides,
    )


# ── Config Tests ──


class TestOpenRouterConfig:
    """Verify OpenRouter settings resolve from env vars."""

    def test_default_settings(self):
        """Default OpenRouter settings should be sensible."""
        from app.core.config import settings

        assert settings.openrouter.enabled is True
        assert settings.openrouter.api_key is None
        assert settings.openrouter.base_url == "https://openrouter.ai/api/v1"
        assert settings.openrouter.only_free is False
        assert settings.openrouter.include_free_router is False
        assert settings.openrouter.discovery_on_startup is True

    def test_only_free_env_var(self, monkeypatch):
        """OPENROUTER_ONLY_FREE=true should be resolved."""
        monkeypatch.setenv("OPENROUTER_ONLY_FREE", "true")
        from app.core.config import OpenRouterSettings
        s = OpenRouterSettings()
        assert s.only_free is True

    def test_include_free_router_env_var(self, monkeypatch):
        """OPENROUTER_INCLUDE_FREE_ROUTER=true should be resolved."""
        monkeypatch.setenv("OPENROUTER_INCLUDE_FREE_ROUTER", "true")
        from app.core.config import OpenRouterSettings
        s = OpenRouterSettings()
        assert s.include_free_router is True

    def test_disabled_env_var(self, monkeypatch):
        """OPENROUTER_ENABLED=false should disable provider."""
        monkeypatch.setenv("OPENROUTER_ENABLED", "false")
        from app.core.config import OpenRouterSettings
        s = OpenRouterSettings()
        assert s.enabled is False


# ── OpenRouter Adapter Tests ──


class TestOpenRouterAdapter:
    """Test OpenRouterIntegration adapter behavior."""

    def test_is_free_model_with_suffix(self):
        """Models with :free suffix should be detected as free."""
        from app.integrations.adapters.openrouter import OpenRouterIntegration

        definition = _make_openrouter_definition()
        config = _make_openrouter_runtime_config(api_key="key")
        adapter = OpenRouterIntegration(definition, config)

        assert adapter._is_free_model("meta-llama/llama-3.2-3b-instruct:free") is True
        assert adapter._is_free_model("deepseek/deepseek-r1:free") is True
        # openrouter/free doesn't end with :free — it's handled separately in build_model_definitions
        assert adapter._is_free_model("openrouter/free") is False
        assert adapter._is_free_model("meta-llama/llama-3.1-8b-instruct") is False
        assert adapter._is_free_model("gpt-4o") is False

    def test_canonical_model_id(self):
        """Canonical ID should follow api/openrouter/<model> scheme."""
        from app.integrations.adapters.openrouter import OpenRouterIntegration

        definition = _make_openrouter_definition()
        config = _make_openrouter_runtime_config(api_key="key")
        adapter = OpenRouterIntegration(definition, config)

        assert adapter._canonical_model_id("openrouter/free") == "api/openrouter/openrouter/free"
        assert (
            adapter._canonical_model_id("meta-llama/llama-3.2-3b-instruct:free")
            == "api/openrouter/meta-llama/llama-3.2-3b-instruct:free"
        )

    def test_extract_upstream_model(self):
        """Upstream model should be extracted from canonical ID."""
        from app.integrations.adapters.openrouter import OpenRouterIntegration

        definition = _make_openrouter_definition()
        config = _make_openrouter_runtime_config(api_key="key")
        adapter = OpenRouterIntegration(definition, config)

        assert (
            adapter._extract_upstream_model("api/openrouter/openrouter/free")
            == "openrouter/free"
        )
        assert (
            adapter._extract_upstream_model("api/openrouter/meta-llama/llama-3.2-3b-instruct:free")
            == "meta-llama/llama-3.2-3b-instruct:free"
        )

    def test_not_configured_without_api_key(self):
        """Adapter should report not configured without API key."""
        from app.integrations.adapters.openrouter import OpenRouterIntegration

        definition = _make_openrouter_definition()
        config = _make_openrouter_runtime_config(api_key=None)
        adapter = OpenRouterIntegration(definition, config)

        assert adapter._is_configured() is False
        assert adapter.status.disabled_reason == "missing_api_key"

    def test_not_configured_when_disabled(self):
        """Adapter should report not configured when disabled."""
        from app.integrations.adapters.openrouter import OpenRouterIntegration

        definition = _make_openrouter_definition()
        config = _make_openrouter_runtime_config(enabled=False, api_key="key")
        adapter = OpenRouterIntegration(definition, config)

        assert adapter._is_configured() is False
        assert adapter.status.disabled_reason == "disabled_by_config"


# ── Model Discovery Tests ──


class TestOpenRouterModelDiscovery:
    """Test model discovery with free model filtering."""

    @pytest.mark.asyncio
    async def test_build_model_definitions_marks_free(self):
        """Free models should have free=true metadata."""
        from app.integrations.adapters.openrouter import OpenRouterIntegration

        definition = _make_openrouter_definition()
        config = _make_openrouter_runtime_config(api_key="key")
        adapter = OpenRouterIntegration(definition, config)

        models = adapter._build_model_definitions(
            ["meta-llama/llama-3.2-3b-instruct:free", "gpt-4o"],
            available=True,
            only_free=False,
        )

        free_model = [m for m in models if "llama-3.2-3b-instruct:free" in m.id][0]
        paid_model = [m for m in models if "gpt-4o" in m.id][0]

        assert free_model.metadata["free"] is True
        assert paid_model.metadata["free"] is False

    @pytest.mark.asyncio
    async def test_free_only_filtering(self):
        """Free-only mode should mark models with free=true metadata."""
        from app.integrations.adapters.openrouter import OpenRouterIntegration

        definition = _make_openrouter_definition()
        config = _make_openrouter_runtime_config(api_key="key")
        adapter = OpenRouterIntegration(definition, config)

        # _build_model_definitions marks free models, discover_models() does the filtering
        models = adapter._build_model_definitions(
            ["meta-llama/llama-3.2-3b-instruct:free", "gpt-4o", "deepseek/deepseek-r1:free"],
            available=True,
            only_free=True,
        )

        free_models = [m for m in models if m.metadata["free"] is True]
        paid_models = [m for m in models if m.metadata["free"] is False]

        assert len(free_models) == 2  # llama and deepseek
        assert len(paid_models) == 1  # gpt-4o

    @pytest.mark.asyncio
    async def test_openrouter_free_router_included(self):
        """openrouter/free should be included when requested."""
        from app.integrations.adapters.openrouter import (
            OPENROUTER_FREE_ROUTER_MODEL,
            OpenRouterIntegration,
        )

        definition = _make_openrouter_definition()
        config = _make_openrouter_runtime_config(api_key="key")
        adapter = OpenRouterIntegration(definition, config)

        models = adapter._build_model_definitions(
            [OPENROUTER_FREE_ROUTER_MODEL],
            available=True,
            only_free=False,
        )

        assert len(models) == 1
        assert models[0].metadata["free"] is True
        assert models[0].id == "api/openrouter/openrouter/free"

    @pytest.mark.asyncio
    async def test_fallback_models_free_only(self):
        """Fallback in free-only mode should filter to free models."""
        from app.integrations.adapters.openrouter import OpenRouterIntegration

        definition = _make_openrouter_definition(
            fallback_models=["meta-llama/llama-3.2-3b-instruct:free", "gpt-4o"],
        )
        config = _make_openrouter_runtime_config(api_key="key")
        adapter = OpenRouterIntegration(definition, config)

        models = adapter._fallback_model_definitions(available=True, only_free=True)

        # Only free fallback model should be included
        assert len(models) == 1
        assert "llama-3.2-3b-instruct:free" in models[0].id


# ── Routing Tests ──


class TestOpenRouterRouting:
    """Test canonical → upstream model mapping."""

    def test_canonical_to_upstream_mapping(self):
        """Canonical ID should map to correct upstream model."""
        from app.integrations.adapters.openrouter import OpenRouterIntegration

        definition = _make_openrouter_definition()
        config = _make_openrouter_runtime_config(api_key="key")
        adapter = OpenRouterIntegration(definition, config)

        test_cases = [
            ("api/openrouter/openrouter/free", "openrouter/free"),
            ("api/openrouter/meta-llama/llama-3.2-3b-instruct:free", "meta-llama/llama-3.2-3b-instruct:free"),
            ("api/openrouter/deepseek/deepseek-r1:free", "deepseek/deepseek-r1:free"),
        ]

        for canonical, expected_upstream in test_cases:
            assert adapter._extract_upstream_model(canonical) == expected_upstream

    def test_registry_has_openrouter_definition(self):
        """OpenRouter should be registered in READY_TO_USE_DEFINITIONS."""
        from app.integrations.definitions import READY_TO_USE_DEFINITIONS

        or_def = [d for d in READY_TO_USE_DEFINITIONS if d.integration_id == "openrouter"]
        assert len(or_def) == 1
        assert or_def[0].base_url == "https://openrouter.ai/api/v1"
        assert or_def[0].api_key_requirement == "required"


# ── Graceful Degradation Tests ──


class TestOpenRouterGracefulDegradation:
    """Test graceful handling of missing API key and failures."""

    def test_missing_api_key_returns_empty(self):
        """Without API key, discover_models should return empty list."""
        import asyncio

        from app.integrations.adapters.openrouter import OpenRouterIntegration

        definition = _make_openrouter_definition()
        config = _make_openrouter_runtime_config(api_key=None)
        adapter = OpenRouterIntegration(definition, config)

        result = asyncio.run(adapter.discover_models())
        assert result == []
        assert adapter.status.disabled_reason == "missing_api_key"

    def test_disabled_provider_returns_empty(self):
        """When disabled, discover_models should return empty list."""
        import asyncio

        from app.integrations.adapters.openrouter import OpenRouterIntegration

        definition = _make_openrouter_definition()
        config = _make_openrouter_runtime_config(enabled=False, api_key="key")
        adapter = OpenRouterIntegration(definition, config)

        result = asyncio.run(adapter.discover_models())
        assert result == []

    def test_diagnostics_includes_free_only_mode(self):
        """Diagnostics should include free_only_mode flag."""
        from app.integrations.adapters.openrouter import OpenRouterIntegration

        definition = _make_openrouter_definition()
        config = _make_openrouter_runtime_config(api_key="key")
        adapter = OpenRouterIntegration(definition, config)

        diag = adapter.diagnostics()
        assert "free_only_mode" in diag


# ── Free Badge Tests ──


class TestOpenRouterFreeBadge:
    """Test free badge support in model registry service."""

    def test_free_model_gets_free_badge(self):
        """Models with free=true metadata should get 'free' badge."""
        from app.services.model_registry_service import ModelViewModel

        model = ModelViewModel(
            id="api/openrouter/meta-llama/llama-3.2-3b-instruct:free",
            display_name="Meta Llama | llama-3.2-3b-instruct:free",
            provider_id="openrouter",
            transport="api",
            source_type="external_api",
            enabled=True,
            available=True,
            aliases=[],
            metadata={"free": True},
        )

        assert model.badge_type == "free"

    def test_paid_model_gets_transport_badge(self):
        """Non-free models should get transport badge."""
        from app.services.model_registry_service import ModelViewModel

        model = ModelViewModel(
            id="api/openrouter/openai/gpt-4o",
            display_name="OpenAI | gpt-4o",
            provider_id="openrouter",
            transport="api",
            source_type="external_api",
            enabled=True,
            available=True,
            aliases=[],
            metadata={"free": False},
        )

        assert model.badge_type == "api"

    def test_unavailable_model_gets_unavailable_badge(self):
        """Unavailable models should get unavailable badge regardless of free status."""
        from app.services.model_registry_service import ModelViewModel

        model = ModelViewModel(
            id="api/openrouter/meta-llama/llama-3.2-3b-instruct:free",
            display_name="Meta Llama | llama-3.2-3b-instruct:free",
            provider_id="openrouter",
            transport="api",
            source_type="external_api",
            enabled=True,
            available=False,
            aliases=[],
            metadata={"free": True},
        )

        assert model.badge_type == "unavailable"
