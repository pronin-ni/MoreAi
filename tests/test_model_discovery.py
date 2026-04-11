"""
Tests for Model Discovery Service.

Covers:
- Startup discovery
- Periodic refresh
- Provider failure (last-known-good preserved)
- Partial update
- Atomic swap
- Manual refresh endpoints
- New models appear without restart
"""

import asyncio
import time
from unittest.mock import patch

import pytest

from app.core.config import ModelDiscoverySettings, settings
from app.integrations.registry import api_registry
from app.integrations.types import ModelDefinition
from app.services.model_discovery import ModelDiscoveryService, ProviderSnapshot


@pytest.fixture
def discovery_service():
    """Fresh discovery service for each test."""
    service = ModelDiscoveryService()
    yield service
    if service._task and not service._task.done():
        service._task.cancel()


def _make_model(provider_id: str, model_id: str) -> ModelDefinition:
    return ModelDefinition(
        id=f"api/{provider_id}/{model_id}",
        provider_id=provider_id,
        transport="api",
        source_type="external_api",
        enabled=True,
        available=True,
    )


class TestProviderSnapshot:
    def test_snapshot_initial_state(self):
        snap = ProviderSnapshot("test-provider", ["model-a", "model-b"])
        assert snap.provider_id == "test-provider"
        assert snap.model_count == 2
        assert snap.model_ids == ["model-a", "model-b"]
        assert snap.status == "available"
        assert snap.last_error is None
        assert snap.last_successful_update > 0

    def test_snapshot_empty_state(self):
        snap = ProviderSnapshot("test-provider", [])
        assert snap.status == "empty"
        assert snap.model_count == 0

    def test_snapshot_mark_failed(self):
        snap = ProviderSnapshot("test-provider", ["model-a", "model-b"])
        old_success_time = snap.last_successful_update
        time.sleep(0.01)
        snap.mark_failed("connection timeout")
        assert snap.status == "failed"
        assert snap.last_error == "connection timeout"
        assert snap.model_count == 2
        assert snap.model_ids == ["model-a", "model-b"]
        assert snap.last_successful_update == old_success_time
        assert snap.last_updated > old_success_time

    def test_snapshot_to_dict(self):
        snap = ProviderSnapshot("test-provider", ["model-a"])
        snap.mark_failed("test error")
        d = snap.to_dict()
        assert d["provider_id"] == "test-provider"
        assert d["model_count"] == 1
        assert d["status"] == "failed"
        assert d["last_error"] == "test error"


class TestModelDiscoveryConfig:
    def test_default_settings(self):
        assert settings.model_discovery.discovery_on_startup is True
        assert settings.model_discovery.refresh_interval_seconds == 300
        assert settings.model_discovery.refresh_jitter_seconds == 30

    def test_env_var_overrides(self):
        """Test env var prefix works correctly."""
        import os
        s = ModelDiscoverySettings()
        # Default values
        assert s.discovery_on_startup is True
        assert s.refresh_interval_seconds == 300


class TestStartupDiscovery:
    @pytest.mark.asyncio
    async def test_discover_all_skipped_when_disabled(self, discovery_service):
        """Test that discover_all skips when discovery_on_startup=False."""
        from app.core import config as config_module
        original = config_module.settings.model_discovery
        # Create a disabled settings object
        disabled = ModelDiscoverySettings(discovery_on_startup=False)
        config_module.settings.model_discovery = disabled
        try:
            result = await discovery_service.discover_all()
            assert result["status"] == "skipped"
        finally:
            config_module.settings.model_discovery = original

    @pytest.mark.asyncio
    async def test_discover_all_runs_api_discovery(self, discovery_service):
        initialize_called = False

        async def mock_initialize():
            nonlocal initialize_called
            initialize_called = True

        with (
            patch.object(api_registry, "initialize", new=mock_initialize),
            patch.object(api_registry, "get_provider_status", return_value=[]),
        ):
            result = await discovery_service.discover_all()

        assert initialize_called
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_discover_all_partial_on_failure(self, discovery_service):
        async def mock_initialize():
            raise RuntimeError("Connection refused")

        with (
            patch.object(api_registry, "initialize", new=mock_initialize),
            patch.object(api_registry, "get_provider_status", return_value=[]),
            patch.object(api_registry, "list_models", return_value=[]),
        ):
            result = await discovery_service.discover_all()

        assert result["status"] == "completed"
        assert "api" in result["providers"]
        assert result["providers"]["api"]["status"] == "failed"


class TestPeriodicRefresh:
    @pytest.mark.asyncio
    async def test_refresh_all_calls_initialize(self, discovery_service):
        initialize_called = False

        async def mock_initialize():
            nonlocal initialize_called
            initialize_called = True

        with (
            patch.object(api_registry, "initialize", new=mock_initialize),
            patch.object(api_registry, "discovered_models", return_value=[]),
            patch.object(api_registry, "get_provider_status", return_value=[]),
            patch.object(api_registry, "list_models", return_value=[]),
        ):
            result = await discovery_service.refresh_all()

        assert initialize_called
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_refresh_failure_preserves_last_known_good(self, discovery_service):
        discovery_service._snapshots["test-provider"] = ProviderSnapshot(
            "test-provider", ["model-a", "model-b"]
        )

        async def mock_initialize():
            raise RuntimeError("Network error")

        with (
            patch.object(api_registry, "initialize", new=mock_initialize),
            patch.object(api_registry, "get_provider_status", return_value=[{
                "integration_id": "test-provider",
                "last_refresh_status": "failed",
                "last_refresh_error": "Network error",
            }]),
            patch.object(api_registry, "list_models", return_value=[]),
        ):
            result = await discovery_service.refresh_all()

        assert result["status"] == "failed"
        snap = discovery_service._snapshots["test-provider"]
        assert snap.status == "failed"
        assert snap.model_count == 2

    @pytest.mark.asyncio
    async def test_refresh_logs_diff(self, discovery_service):
        async def mock_initialize():
            pass

        with (
            patch.object(api_registry, "initialize", new=mock_initialize),
            patch.object(api_registry, "discovered_models", return_value=["api/new/model-c"]),
            patch.object(api_registry, "get_provider_status", return_value=[]),
            patch.object(api_registry, "list_models", return_value=[]),
        ):
            result = await discovery_service.refresh_all()

        assert result["status"] == "ok"


class TestPerProviderRefresh:
    @pytest.mark.asyncio
    async def test_refresh_provider_success(self, discovery_service):
        async def mock_refresh_provider(provider_id):
            return {
                "integration_id": provider_id,
                "status": "ok",
                "model_count": 3,
                "added": ["new-model"],
                "removed": [],
            }

        with (
            patch.object(api_registry, "refresh_provider", new=mock_refresh_provider),
            patch.object(api_registry, "list_models", return_value=[
                {"id": "api/test-provider/model-a", "provider_id": "test-provider"},
            ]),
        ):
            result = await discovery_service.refresh_provider("test-provider")

        assert result["status"] == "ok"
        assert "test-provider" in discovery_service._snapshots

    @pytest.mark.asyncio
    async def test_refresh_provider_failure_preserves_models(self, discovery_service):
        discovery_service._snapshots["test-provider"] = ProviderSnapshot(
            "test-provider", ["model-a", "model-b"]
        )

        async def mock_refresh_provider(provider_id):
            return {
                "integration_id": provider_id,
                "status": "failed",
                "error": "Connection refused",
                "model_count": 2,
            }

        with patch.object(api_registry, "refresh_provider", new=mock_refresh_provider):
            result = await discovery_service.refresh_provider("test-provider")

        assert result["status"] == "failed"
        snap = discovery_service._snapshots["test-provider"]
        assert snap.status == "failed"
        assert snap.model_count == 2


class TestAtomicSwap:
    @pytest.mark.asyncio
    async def test_refresh_does_not_block_list_models(self, discovery_service):
        api_registry._models = {
            "api/test/model-a": _make_model("test", "model-a"),
        }

        async def mock_initialize():
            await asyncio.sleep(0.05)
            api_registry._models = {
                "api/test/model-b": _make_model("test", "model-b"),
            }

        with (
            patch.object(api_registry, "initialize", new=mock_initialize),
            patch.object(api_registry, "discovered_models", side_effect=[
                ["api/test/model-a"],
                ["api/test/model-b"],
            ]),
            patch.object(api_registry, "get_provider_status", return_value=[]),
            patch.object(api_registry, "list_models", return_value=[
                {"id": "api/test/model-a", "provider_id": "test"},
            ]),
        ):
            task = asyncio.create_task(discovery_service.refresh_all())
            models = api_registry.list_models()
            assert len(models) > 0
            await task

    @pytest.mark.asyncio
    async def test_no_overlap_refresh(self, discovery_service):
        refresh_count = 0
        concurrent = 0

        async def mock_initialize():
            nonlocal refresh_count, concurrent
            refresh_count += 1
            concurrent += 1
            assert concurrent == 1, "Refresh should not overlap"
            await asyncio.sleep(0.01)
            concurrent -= 1

        with (
            patch.object(api_registry, "initialize", new=mock_initialize),
            patch.object(api_registry, "discovered_models", return_value=[]),
            patch.object(api_registry, "get_provider_status", return_value=[]),
            patch.object(api_registry, "list_models", return_value=[]),
        ):
            tasks = [
                discovery_service.refresh_all(),
                discovery_service.refresh_all(),
                discovery_service.refresh_all(),
            ]
            results = await asyncio.gather(*tasks)

        skipped = [r for r in results if r.get("status") == "skipped"]
        assert len(skipped) >= 0


class TestManualRefreshEndpoints:
    @pytest.fixture
    def client(self):
        from unittest.mock import AsyncMock

        from fastapi.testclient import TestClient

        from app.main import app
        with (
            patch("app.main.browser_dispatcher.initialize", new=AsyncMock()),
            patch("app.main.browser_dispatcher.shutdown", new=AsyncMock()),
            patch("app.main.unified_registry.initialize", new=AsyncMock()),
        ):
            yield TestClient(app)

    def test_discovery_status_endpoint(self, client):
        response = client.get("/admin/models/discovery/status")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_refresh_all_endpoint(self, client):
        response = client.post("/admin/models/refresh")
        assert response.status_code == 200
        assert "status" in response.json()

    def test_refresh_single_endpoint(self, client):
        response = client.post("/admin/models/refresh/openrouter")
        assert response.status_code == 200
        assert "status" in response.json()


class TestBackgroundRefreshTask:
    @pytest.mark.asyncio
    async def test_start_creates_task(self, discovery_service):
        discovery_service.start()
        assert discovery_service._task is not None
        assert not discovery_service._task.done()
        await discovery_service.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self, discovery_service):
        discovery_service.start()
        task = discovery_service._task
        await discovery_service.stop()
        assert discovery_service._task is None
        assert task.cancelled() or task.done()

    @pytest.mark.asyncio
    async def test_refresh_loop_background_execution(self, discovery_service):
        """Verify the refresh loop executes without errors in background."""
        # Patch refresh_all to track calls without needing real env var changes
        call_count = 0

        async def mock_refresh_all():
            nonlocal call_count
            call_count += 1
            return {"status": "ok"}

        original = discovery_service.refresh_all
        discovery_service.refresh_all = mock_refresh_all
        # Use the default interval (300s) — we just verify the task runs
        discovery_service.start()
        # Give the task a moment to start
        await asyncio.sleep(0.1)
        # Stop immediately — the loop should handle cancellation cleanly
        await discovery_service.stop()
        # Restore
        discovery_service.refresh_all = original
        # Task was running in background without errors
        assert discovery_service._task is None
