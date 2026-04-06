import httpx
import pytest
import respx
from httpx import Response

from app.agents.opencode.client import OpenCodeClient
from app.agents.opencode.discovery import discover_models
from app.agents.opencode.provider import OpenCodeProvider
from app.agents.registry import AgentModelDefinition, AgentRegistry
from app.core import config as config_module
from app.core.errors import ServiceUnavailableError


class TestOpenCodeClient:
    @pytest.fixture
    def client(self):
        return OpenCodeClient(
            base_url="http://localhost:4096",
            username="opencode",
            password="test-password",
            timeout=30,
        )

    @respx.mock
    async def test_healthcheck_success(self, client):
        respx.get("http://localhost:4096/global/health").mock(
            return_value=Response(200, json={"healthy": True, "version": "1.0.0"})
        )
        result = await client.healthcheck()
        assert result["healthy"] is True
        assert result["version"] == "1.0.0"

    @respx.mock
    async def test_healthcheck_failure(self, client):
        respx.get("http://localhost:4096/global/health").mock(
            return_value=Response(500, json={"healthy": False})
        )
        with pytest.raises(httpx.HTTPStatusError):
            await client.healthcheck()

    @respx.mock
    async def test_get_config_providers(self, client):
        respx.get("http://localhost:4096/config/providers").mock(
            return_value=Response(200, json={
                "providers": [
                    {"id": "openai", "name": "OpenAI"},
                    {"id": "anthropic", "name": "Anthropic"},
                ],
                "default": {
                    "openai": "gpt-4",
                    "anthropic": "claude-3-sonnet",
                },
            })
        )
        result = await client.get_config_providers()
        assert len(result["providers"]) == 2
        assert "openai" in result["default"]

    @respx.mock
    async def test_create_session(self, client):
        respx.post("http://localhost:4096/session").mock(
            return_value=Response(200, json={"id": "session-123", "title": "test"})
        )
        result = await client.create_session(title="test")
        assert result["id"] == "session-123"

    @respx.mock
    async def test_send_message(self, client):
        respx.post("http://localhost:4096/session/session-123/message").mock(
            return_value=Response(200, json={
                "info": {"role": "assistant"},
                "parts": [{"type": "text", "content": "Hello from assistant"}],
            })
        )
        result = await client.send_message(
            session_id="session-123",
            prompt="Hi",
            model="gpt-4",
        )
        assert len(result["parts"]) == 1
        assert result["parts"][0]["content"] == "Hello from assistant"

    @respx.mock
    async def test_delete_session(self, client):
        respx.delete("http://localhost:4096/session/session-123").mock(
            return_value=Response(200, json={"success": True})
        )
        result = await client.delete_session("session-123")
        assert result is True


class TestOpenCodeDiscovery:
    @respx.mock
    async def test_discovers_opencode_free_models(self):
        """Only free models from the `opencode` built-in provider should be discovered."""
        client = OpenCodeClient(base_url="http://localhost:4096")

        respx.get("http://localhost:4096/provider").mock(
            return_value=Response(200, json={
                "all": [
                    {
                        "id": "opencode",
                        "name": "OpenCode",
                        "env": ["OPENCODE_API_KEY"],
                        "models": {
                            "big-pickle": {},
                            "qwen3.6-plus-free": {},
                            "gpt-5-nano": {},
                            "minimax-m2.5-free": {},
                        },
                    },
                    {"id": "openai", "name": "OpenAI", "env": ["OPENAI_API_KEY"], "models": {"gpt-4": {}}},
                    {"id": "anthropic", "name": "Anthropic", "env": ["ANTHROPIC_API_KEY"], "models": {"claude-4": {}}},
                ],
                "default": {},
                "connected": ["openai"],
            })
        )

        # /config/providers is not used for free model discovery anymore
        respx.get("http://localhost:4096/config/providers").mock(
            return_value=Response(200, json={
                "providers": [
                    {"id": "openai", "name": "OpenAI"},
                ],
                "default": {"openai": "gpt-4"},
            })
        )

        models = await discover_models(client)

        # Only opencode built-in models (4 free models)
        assert len(models) == 4
        model_ids = [m.id for m in models]
        assert "agent/opencode/opencode/big-pickle" in model_ids
        assert "agent/opencode/opencode/qwen3.6-plus-free" in model_ids
        assert "agent/opencode/opencode/gpt-5-nano" in model_ids
        assert "agent/opencode/opencode/minimax-m2.5-free" in model_ids

        # Paid providers excluded
        assert not any("openai" in m.id and "opencode" not in m.id for m in models)
        assert not any("anthropic" in m.id for m in models)

        # All models should be zen source kind
        for m in models:
            assert m.source_kind == "zen"
            assert m.is_runtime_available is True
            assert m.requires_auth is False

    @respx.mock
    async def test_discover_models_bundled_free(self):
        """When opencode built-in provider has no models, discovery returns empty list."""
        client = OpenCodeClient(base_url="http://localhost:4096")

        respx.get("http://localhost:4096/provider").mock(
            return_value=Response(200, json={
                "all": [
                    {"id": "opencode", "name": "OpenCode", "env": ["OPENCODE_API_KEY"], "models": {}},
                ],
                "default": {},
                "connected": [],
            })
        )

        models = await discover_models(client)
        assert len(models) == 0

    @respx.mock
    async def test_discover_models_empty_on_failure(self):
        client = OpenCodeClient(base_url="http://localhost:4096")

        respx.get("http://localhost:4096/provider").mock(
            return_value=Response(500, json={"error": "internal error"})
        )

        models = await discover_models(client)
        assert len(models) == 0


class TestOpenCodeProvider:
    @pytest.fixture
    def provider(self, monkeypatch):
        # Override settings to use localhost for testing
        from app.core import config as config_module

        monkeypatch.setattr(config_module.settings.opencode, "base_url", "http://localhost:4096")
        monkeypatch.setattr(config_module.settings.opencode, "enabled", True)
        monkeypatch.setattr(config_module.settings.opencode, "discovery_enabled", True)

        provider = OpenCodeProvider()
        # Reset client to pick up the mocked settings
        provider._client = OpenCodeClient()

        yield provider

    @respx.mock
    async def test_initialize_success(self, provider):
        # Mock healthcheck
        respx.get("http://localhost:4096/global/health").mock(
            return_value=Response(200, json={"healthy": True, "version": "1.0.0"})
        )

        # Mock /provider endpoint with opencode built-in free models
        respx.get("http://localhost:4096/provider").mock(
            return_value=Response(200, json={
                "all": [
                    {"id": "opencode", "name": "OpenCode", "env": ["OPENCODE_API_KEY"],
                     "models": {"big-pickle": {}}},
                ],
                "default": {},
                "connected": [],
            })
        )

        await provider.initialize()
        assert provider._available is True
        assert len(provider._models) == 1
        assert provider._models[0].id == "agent/opencode/opencode/big-pickle"
        assert provider._models[0].source_kind == "zen"

    @respx.mock
    async def test_initialize_healthcheck_failure(self, provider, monkeypatch):
        # Disable managed mode so the test only checks external healthcheck
        monkeypatch.setattr(provider, "_mode", "external")
        monkeypatch.setattr(config_module.settings.opencode, "managed", False)

        respx.get("http://localhost:4096/global/health").mock(
            return_value=Response(500, json={"healthy": False})
        )

        await provider.initialize()
        assert provider._available is False
        assert "healthcheck_failed" in provider._error

    @respx.mock
    async def test_send_prompt_success(self, provider):
        # Mock session creation
        respx.post("http://localhost:4096/session").mock(
            return_value=Response(200, json={"id": "session-123"})
        )

        # Mock message response (OpenCode uses "text" field, not "content")
        respx.post("http://localhost:4096/session/session-123/message").mock(
            return_value=Response(200, json={
                "parts": [{"type": "text", "text": "Assistant response"}],
            })
        )

        # Mock session deletion
        respx.delete("http://localhost:4096/session/session-123").mock(
            return_value=Response(200, json={})
        )

        provider._available = True
        response = await provider.send_prompt(
            prompt="Hello",
            model="agent/opencode/openai/gpt-4",
            provider_id="opencode",
        )
        assert response == "Assistant response"

    @respx.mock
    async def test_send_prompt_unavailable(self, provider):
        provider._available = False
        provider._error = "test error"

        with pytest.raises(ServiceUnavailableError):
            await provider.send_prompt(
                prompt="Hello",
                model="agent/opencode/openai/gpt-4",
                provider_id="opencode",
            )

    def test_diagnostics(self, provider):
        provider._available = True
        provider._models = [
            AgentModelDefinition(
                id="agent/opencode/openai/gpt-4",
                provider_id="opencode",
            )
        ]

        diag = provider.diagnostics()
        assert diag["provider_id"] == "opencode"
        assert diag["agent_type"] == "opencode_server"
        assert diag["available"] is True
        assert diag["model_count"] == 1


class TestAgentRegistry:
    @pytest.fixture
    def registry(self):
        return AgentRegistry()

    async def test_register_and_list_models(self, registry):
        from app.agents.opencode.provider import OpenCodeProvider

        provider = OpenCodeProvider()
        models = [
            AgentModelDefinition(
                id="agent/opencode/openai/gpt-4",
                provider_id="opencode",
                transport="agent",
                source_type="opencode_server",
            ),
        ]

        registry.register(provider, models)

        model_list = registry.list_models()
        assert len(model_list) == 1
        assert model_list[0]["id"] == "agent/opencode/openai/gpt-4"
        assert model_list[0]["transport"] == "agent"

    def test_resolve_model(self, registry):
        from app.agents.opencode.provider import OpenCodeProvider

        provider = OpenCodeProvider()
        models = [
            AgentModelDefinition(
                id="agent/opencode/openai/gpt-4",
                provider_id="opencode",
                transport="agent",
                source_type="opencode_server",
            ),
        ]

        registry.register(provider, models)

        resolved = registry.resolve_model("agent/opencode/openai/gpt-4")
        assert resolved["canonical_id"] == "agent/opencode/openai/gpt-4"
        assert resolved["provider_id"] == "opencode"
        assert resolved["transport"] == "agent"
        assert resolved["execution_strategy"] == "agent_completion"

    def test_resolve_model_not_found(self, registry):
        from app.core.errors import BadRequestError

        with pytest.raises(BadRequestError):
            registry.resolve_model("nonexistent-model")

    def test_get_provider(self, registry):
        from app.agents.opencode.provider import OpenCodeProvider

        provider = OpenCodeProvider()
        registry.register(provider, [])

        retrieved = registry.get_provider("opencode")
        assert retrieved is provider

    def test_get_provider_not_found(self, registry):
        from app.core.errors import InternalError

        with pytest.raises(InternalError):
            registry.get_provider("nonexistent")
