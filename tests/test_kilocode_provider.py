"""
Tests for Kilocode agent provider integration.

Covers:
- client healthcheck
- discovery success / empty / failure
- provider initialize external mode
- send_prompt
- diagnostics
- registry resolve for agent/kilocode/...
- graceful unavailable behavior
"""

import asyncio
from unittest.mock import patch

import pytest
import respx
from httpx import Response

from app.agents.kilocode.client import KilocodeClient
from app.agents.kilocode.discovery import discover_models
from app.agents.kilocode.provider import KilocodeProvider
from app.agents.registry import AgentModelDefinition, registry as agent_registry
from app.core.errors import ServiceUnavailableError


@pytest.fixture(autouse=True)
def _clean_agent_registry():
    """Save and restore agent registry state to avoid test pollution."""
    saved_providers = dict(agent_registry._providers)
    saved_models = dict(agent_registry._models)
    yield
    agent_registry._providers = saved_providers
    agent_registry._models = saved_models


BASE_URL = "http://127.0.0.1:5096"


@pytest.fixture
def mock_kilocode_server(respx_mock):
    """Mock a healthy Kilocode server with provider registry."""
    # Healthcheck
    respx_mock.get(f"{BASE_URL}/global/health").mock(
        return_value=Response(200, json={"healthy": True, "version": "1.0.0"})
    )
    # Provider registry
    respx_mock.get(f"{BASE_URL}/provider").mock(
        return_value=Response(
            200,
            json={
                "all": [
                    {
                        "id": "kilocode",
                        "name": "Kilocode",
                        "models": {
                            "kilo-model-1": {},
                            "kilo-model-2": {},
                            "free-model": {},
                        },
                    },
                ]
            },
        )
    )
    # Session endpoints
    respx_mock.post(f"{BASE_URL}/session").mock(
        return_value=Response(200, json={"id": "test-session-123"})
    )
    respx_mock.post(f"{BASE_URL}/session/test-session-123/message").mock(
        return_value=Response(
            200,
            json={"parts": [{"type": "text", "text": "Hello from Kilocode!"}]},
        )
    )
    respx_mock.delete(f"{BASE_URL}/session/test-session-123").mock(
        return_value=Response(200)
    )
    yield respx_mock


class TestKilocodeClient:
    def test_healthcheck_success(self, mock_kilocode_server):
        client = KilocodeClient(base_url=BASE_URL)
        result = asyncio.run(client.healthcheck())
        assert result["healthy"] is True

    def test_healthcheck_failure(self, respx_mock):
        respx_mock.get(f"{BASE_URL}/global/health").mock(return_value=Response(500))
        client = KilocodeClient(base_url=BASE_URL)
        with pytest.raises(Exception):
            asyncio.run(client.healthcheck())

    def test_create_session(self, mock_kilocode_server):
        client = KilocodeClient(base_url=BASE_URL)
        result = asyncio.run(client.create_session(title="test"))
        assert result["id"] == "test-session-123"

    def test_send_message(self, mock_kilocode_server):
        client = KilocodeClient(base_url=BASE_URL)
        result = asyncio.run(
            client.send_message("test-session-123", "Hello", "kilocode/kilo-model-1")
        )
        assert "parts" in result


class TestKilocodeDiscovery:
    def test_discovery_success(self, mock_kilocode_server):
        client = KilocodeClient(base_url=BASE_URL)
        models = asyncio.run(discover_models(client))
        assert len(models) == 3
        assert all(isinstance(m, AgentModelDefinition) for m in models)
        m = models[0]
        assert m.provider_id == "kilocode"
        assert m.transport == "agent"
        assert m.source_type == "kilocode_server"
        assert m.source_kind == "zen"
        assert m.requires_auth is False
        assert m.id.startswith("agent/kilocode/kilocode/")

    def test_discovery_empty_provider(self, respx_mock):
        respx_mock.get(f"{BASE_URL}/global/health").mock(
            return_value=Response(200, json={"healthy": True})
        )
        respx_mock.get(f"{BASE_URL}/provider").mock(
            return_value=Response(
                200,
                json={"all": [{"id": "kilocode", "name": "Kilocode", "models": {}}]},
            )
        )
        client = KilocodeClient(base_url=BASE_URL)
        models = asyncio.run(discover_models(client))
        assert models == []

    def test_discovery_failure(self, respx_mock):
        respx_mock.get(f"{BASE_URL}/provider").mock(return_value=Response(500))
        client = KilocodeClient(base_url=BASE_URL)
        models = asyncio.run(discover_models(client))
        assert models == []


class TestKilocodeProvider:
    @pytest.mark.asyncio
    async def test_provider_disabled_by_config(self):
        """Provider should be unavailable if disabled in config."""
        with patch("app.agents.kilocode.provider.settings") as mock_settings:
            mock_settings.kilocode.enabled = False
            mock_settings.kilocode.managed = True
            mock_settings.kilocode.autostart = True

            provider = KilocodeProvider()
            await provider.initialize()

            assert provider._available is False
            assert provider._error == "disabled_by_config"

    @pytest.mark.asyncio
    async def test_provider_send_prompt_unavailable(self):
        """Send prompt should raise if provider unavailable."""
        provider = KilocodeProvider()
        with pytest.raises(ServiceUnavailableError):
            await provider.send_prompt(
                "Hello", "agent/kilocode/kilocode/test", "kilocode"
            )

    def test_diagnostics(self):
        """Diagnostics should return status dict."""
        provider = KilocodeProvider()
        diag = provider.diagnostics()
        assert diag["provider_id"] == "kilocode"
        assert diag["agent_type"] == "kilocode_server"
        assert "available" in diag
        assert "mode" in diag
        assert "model_count" in diag


class TestKilocodeRegistry:
    def test_kilocode_registered_as_pending(self):
        """Kilocode provider should be in pending list."""
        pending_ids = [p.provider_id for p in agent_registry._pending_providers]
        assert "kilocode" in pending_ids

    def test_registry_resolve_kilocode_model(self):
        """Kilocode models should be resolvable after registration."""
        model = AgentModelDefinition(
            id="agent/kilocode/kilocode/test-model",
            provider_id="kilocode",
            transport="agent",
            source_type="kilocode_server",
        )
        from app.agents.kilocode.provider import provider as kilocode_provider

        agent_registry.register(kilocode_provider, [model])

        assert agent_registry.can_resolve_model(
            "agent/kilocode/kilocode/test-model"
        )
        resolved = agent_registry.resolve_model("agent/kilocode/kilocode/test-model")
        assert resolved["provider_id"] == "kilocode"
        assert resolved["transport"] == "agent"
        assert resolved["execution_strategy"] == "agent_completion"
