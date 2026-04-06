"""Tests for admin config manager, resolver, and applier."""

import pytest

from app.admin.config_manager import (
    ConfigManager,
    ModelOverride,
    ProviderOverride,
    RuntimeOverrides,
)
from app.admin.resolver import (
    get_field_policy,
    resolve_all_effective,
    resolve_model_effective,
    resolve_provider_effective,
)


class TestRuntimeOverrides:
    def test_empty_by_default(self):
        overrides = RuntimeOverrides()
        assert overrides.version == 0
        assert overrides.is_empty()

    def test_to_dict_and_from_dict(self):
        overrides = RuntimeOverrides()
        overrides.providers["qwen"] = ProviderOverride(enabled=False)
        overrides.version = 5

        data = overrides.to_dict()
        restored = RuntimeOverrides.from_dict(data)

        assert restored.version == 5
        assert restored.providers["qwen"].enabled is False

    def test_is_empty_with_overrides(self):
        overrides = RuntimeOverrides()
        overrides.providers["qwen"] = ProviderOverride(enabled=True)
        assert not overrides.is_empty()


class TestProviderOverride:
    def test_default_state(self):
        override = ProviderOverride()
        assert override.enabled is None
        assert override.state == "pending"
        assert override.error is None

    def test_to_dict_and_from_dict(self):
        override = ProviderOverride(
            enabled=False, concurrency_limit=10, priority=5
        )
        override.state = "applied"
        override.applied_at = 12345.0

        data = override.to_dict()
        restored = ProviderOverride.from_dict(data)

        assert restored.enabled is False
        assert restored.concurrency_limit == 10
        assert restored.priority == 5
        assert restored.state == "applied"


class TestModelOverride:
    def test_default_state(self):
        override = ModelOverride()
        assert override.enabled is None
        assert override.visibility is None
        assert override.state == "pending"

    def test_to_dict_and_from_dict(self):
        override = ModelOverride(
            enabled=False, visibility="hidden"
        )
        data = override.to_dict()
        restored = ModelOverride.from_dict(data)

        assert restored.enabled is False
        assert restored.visibility == "hidden"


class TestConfigManager:
    @pytest.fixture
    def mgr(self, tmp_path):
        config_path = str(tmp_path / "admin.json")
        mgr = ConfigManager(config_path=config_path)
        mgr.register_known_providers({"qwen", "glm", "opencode"})
        mgr.register_known_models({"browser/qwen", "api/g4f-groq/test"})
        return mgr

    @pytest.mark.asyncio
    async def test_update_provider_override(self, mgr):
        override = await mgr.update_provider(
            "qwen", {"enabled": False}
        )
        assert override.enabled is False
        assert override.state == "pending"
        assert mgr.overrides.providers["qwen"].enabled is False

    @pytest.mark.asyncio
    async def test_update_model_override(self, mgr):
        override = await mgr.update_model(
            "browser/qwen", {"visibility": "hidden"}
        )
        assert override.visibility == "hidden"
        assert mgr.overrides.models["browser/qwen"].visibility == "hidden"

    @pytest.mark.asyncio
    async def test_reset_override(self, mgr):
        await mgr.update_provider("qwen", {"enabled": False})
        assert "qwen" in mgr.overrides.providers

        await mgr.reset_override("provider", "qwen")
        assert "qwen" not in mgr.overrides.providers

    @pytest.mark.asyncio
    async def test_rollback(self, mgr):
        await mgr.update_provider("qwen", {"enabled": False})
        v1 = mgr.current_version
        await mgr.update_provider("qwen", {"enabled": True, "priority": 10})

        # Rollback to previous
        rolled_back = await mgr.rollback()
        assert rolled_back.providers["qwen"].enabled is False

    @pytest.mark.asyncio
    async def test_rollback_no_history(self, mgr):
        with pytest.raises(Exception):
            await mgr.rollback()

    @pytest.mark.asyncio
    async def test_persistence(self, mgr, tmp_path):
        await mgr.update_provider("qwen", {"enabled": False})

        # Create new manager from same path
        new_mgr = ConfigManager(config_path=str(tmp_path / "admin.json"))
        new_mgr.register_known_providers({"qwen", "glm", "opencode"})

        assert new_mgr.overrides.providers["qwen"].enabled is False

    @pytest.mark.asyncio
    async def test_version_bumps_on_update(self, mgr):
        v0 = mgr.current_version
        await mgr.update_provider("qwen", {"enabled": False})
        assert mgr.current_version > v0

    @pytest.mark.asyncio
    async def test_unknown_provider_rejected(self, mgr):
        with pytest.raises(Exception):
            await mgr.update_provider("nonexistent", {"enabled": False})

    @pytest.mark.asyncio
    async def test_concurrency_limit_validation(self, mgr):
        with pytest.raises(Exception):
            await mgr.update_provider("qwen", {"concurrency_limit": 0})

        with pytest.raises(Exception):
            await mgr.update_provider("qwen", {"concurrency_limit": 200})

    @pytest.mark.asyncio
    async def test_visibility_validation(self, mgr):
        with pytest.raises(Exception):
            await mgr.update_model(
                "browser/qwen", {"visibility": "invalid"}
            )

    @pytest.mark.asyncio
    async def test_history(self, mgr):
        await mgr.update_provider("qwen", {"enabled": False})
        await mgr.update_provider("qwen", {"enabled": True})

        history = mgr.get_history()
        assert len(history) >= 1

    @pytest.mark.asyncio
    async def test_get_version(self, mgr):
        await mgr.update_provider("qwen", {"enabled": False})
        await mgr.update_provider("qwen", {"enabled": True})

        # History stores state before each change
        history = mgr.get_history()
        assert len(history) >= 1

        # Get the first historical version (state before first change = empty)
        first = mgr.get_version(0)
        assert first is not None
        assert not first.providers  # empty state


class TestConfigResolver:
    @pytest.fixture
    def mgr(self, tmp_path):
        config_path = str(tmp_path / "admin.json")
        mgr = ConfigManager(config_path=config_path)
        mgr.register_known_providers({"qwen", "glm", "opencode"})
        mgr.register_known_models({"browser/qwen", "api/g4f-groq/test"})
        return mgr

    def test_field_policy_lookup(self):
        policy = get_field_policy("providers.qwen.enabled")
        assert policy is not None
        assert policy.category == "safe_live"

    def test_field_policy_wildcard(self):
        policy = get_field_policy("providers.glm.concurrency_limit")
        assert policy is not None
        assert policy.category == "conditional_live"

    def test_field_policy_not_found(self):
        policy = get_field_policy("unknown.field")
        assert policy is None

    def test_resolve_provider_effective_no_overrides(self, mgr):
        result = resolve_provider_effective("qwen")
        assert result["provider_id"] == "qwen"
        assert result["enabled"]["effective_value"] is True
        assert result["enabled"]["source"] == "base"

    @pytest.mark.asyncio
    async def test_resolve_provider_effective_with_override(self, mgr):
        from unittest.mock import patch

        await mgr.update_provider("qwen", {"enabled": False})

        with patch(
            "app.admin.resolver.config_manager", mgr
        ):
            result = resolve_provider_effective("qwen")
            assert result["enabled"]["effective_value"] is False
            assert result["enabled"]["source"] == "override"

    def test_resolve_model_effective_no_overrides(self, mgr):
        result = resolve_model_effective("browser/qwen")
        assert result["model_id"] == "browser/qwen"
        assert result["enabled"]["effective_value"] is True

    def test_resolve_all_effective(self, mgr):
        result = resolve_all_effective()
        assert "version" in result
        assert "providers" in result
        assert "models" in result
        assert "field_policy" in result
