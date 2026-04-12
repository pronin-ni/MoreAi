"""
Tests for Agent Discovery Service.

Covers:
- startup discovery
- periodic refresh
- provider failure (last-known-good preserved)
- partial update
- atomic swap
- manual refresh
- graceful degradation when provider unavailable
"""

import asyncio

import pytest

from app.agents.registry import AgentModelDefinition
from app.agents.registry import registry as agent_registry
from app.services.agent_discovery import AgentDiscoveryService

# ── Fixtures ──


@pytest.fixture
def discovery_service():
    """Fresh discovery service for each test."""
    return AgentDiscoveryService(refresh_interval_seconds=600)


@pytest.fixture(autouse=True)
def _clean_agent_registry():
    """Save and restore agent registry state to avoid test pollution."""
    saved_providers = dict(agent_registry._providers)
    saved_models = dict(agent_registry._models)
    yield
    agent_registry._providers = saved_providers
    agent_registry._models = saved_models


@pytest.fixture
def mock_agent_provider():
    """Mock agent provider for testing."""
    from unittest.mock import AsyncMock

    provider = AsyncMock()
    provider.provider_id = "test-agent"
    provider._available = True
    provider._error = None
    provider._mode = "external"
    provider._models = []

    async def mock_discover():
        return [
            {
                "id": "agent/test-agent/test/model-1",
                "provider_id": "test-agent",
                "transport": "agent",
                "source_type": "test_agent_server",
                "enabled": True,
                "available": True,
            },
            {
                "id": "agent/test-agent/test/model-2",
                "provider_id": "test-agent",
                "transport": "agent",
                "source_type": "test_agent_server",
                "enabled": True,
                "available": True,
            },
        ]

    provider.discover_models = mock_discover
    return provider


# ── Startup Discovery Tests ──


class TestAgentDiscoveryStartup:
    def test_discover_all_empty_registry(self, discovery_service):
        """discover_all should return empty results when no providers registered."""
        result = asyncio.run(discovery_service.discover_all())
        assert result == {}

    def test_discover_all_with_provider(self, discovery_service, mock_agent_provider):
        """discover_all should refresh models for registered providers."""
        # Register the mock provider
        agent_registry._providers["test-agent"] = mock_agent_provider

        result = asyncio.run(discovery_service.discover_all())

        assert "test-agent" in result
        assert result["test-agent"]["status"] == "ok"
        assert result["test-agent"]["model_count"] == 2

        # Cleanup
        del agent_registry._providers["test-agent"]


# ── Provider Refresh Tests ──


class TestAgentProviderRefresh:
    def test_refresh_provider_success(self, discovery_service, mock_agent_provider):
        """refresh_provider should update models for a single provider."""
        agent_registry._providers["test-agent"] = mock_agent_provider

        result = asyncio.run(discovery_service.refresh_provider("test-agent"))

        assert result["status"] == "ok"
        assert result["model_count"] == 2
        assert len(result["added"]) == 2  # Both models are new

        # Cleanup
        del agent_registry._providers["test-agent"]

    def test_refresh_provider_not_found(self, discovery_service):
        """refresh_provider should return not_found for unknown provider."""
        result = asyncio.run(discovery_service.refresh_provider("nonexistent"))
        assert result["status"] == "not_found"

    def test_refresh_provider_unavailable(self, discovery_service):
        """refresh_provider should skip unavailable providers."""
        from unittest.mock import AsyncMock

        provider = AsyncMock()
        provider.provider_id = "test-agent"
        provider._available = False
        provider._error = "connection_failed"
        provider._mode = "external"
        provider._models = []

        agent_registry._providers["test-agent"] = provider

        result = asyncio.run(discovery_service.refresh_provider("test-agent"))

        assert result["status"] == "skipped"
        assert result["reason"] == "provider_unavailable"

        # Cleanup
        del agent_registry._providers["test-agent"]


# ── Last-Known-Good Preservation ──


class TestLastKnownGood:
    def test_discovery_failure_preserves_models(self, discovery_service):
        """If discovery fails, previous models should be preserved."""
        from unittest.mock import AsyncMock

        # Pre-populate models
        existing_model = AgentModelDefinition(
            id="agent/test-agent/test/existing",
            provider_id="test-agent",
            transport="agent",
            source_type="test_agent_server",
        )
        agent_registry._models[existing_model.id] = existing_model

        # Create failing provider
        provider = AsyncMock()
        provider.provider_id = "test-agent"
        provider._available = True
        provider._error = None
        provider.discover_models = AsyncMock(side_effect=RuntimeError("Connection failed"))

        agent_registry._providers["test-agent"] = provider

        result = asyncio.run(discovery_service.refresh_provider("test-agent"))

        assert result["status"] == "failed"
        # Existing model should still be in registry
        assert "agent/test-agent/test/existing" in agent_registry._models

        # Cleanup
        del agent_registry._providers["test-agent"]
        del agent_registry._models["agent/test-agent/test/existing"]


# ── Atomic Model Update Tests ──


class TestAtomicModelUpdate:
    def test_added_and_removed_models(self, discovery_service, mock_agent_provider):
        """Model update should track added and removed models."""
        # Pre-populate with an old model
        old_model = AgentModelDefinition(
            id="agent/test-agent/test/old-model",
            provider_id="test-agent",
            transport="agent",
            source_type="test_agent_server",
        )
        agent_registry._models[old_model.id] = old_model
        agent_registry._providers["test-agent"] = mock_agent_provider

        result = asyncio.run(discovery_service.refresh_provider("test-agent"))

        assert result["status"] == "ok"
        assert "agent/test-agent/test/old-model" in result["removed"]
        assert "agent/test-agent/test/model-1" in result["added"]
        assert "agent/test-agent/test/model-2" in result["added"]

        # Old model should be removed
        assert "agent/test-agent/test/old-model" not in agent_registry._models

        # Cleanup
        del agent_registry._providers["test-agent"]


# ── Background Loop Tests ──


class TestBackgroundLoop:
    @pytest.mark.asyncio
    async def test_start_creates_task(self, discovery_service):
        """start() should create a background task."""
        discovery_service.start()
        assert discovery_service._task is not None
        assert not discovery_service._task.done()
        await discovery_service.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self, discovery_service):
        """stop() should cancel the background task."""
        discovery_service.start()
        task = discovery_service._task
        await discovery_service.stop()
        assert discovery_service._task is None
        assert task.cancelled() or task.done()
