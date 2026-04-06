from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.errors import BadRequestError
from app.main import app
from app.schemas.openai import ChatCompletionResponse, Choice, Message, Model, ModelList, Usage


@pytest.fixture
def client():
    with (
        patch("app.main.browser_dispatcher.initialize", new=AsyncMock()),
        patch("app.main.browser_dispatcher.shutdown", new=AsyncMock()),
        patch("app.main.unified_registry.initialize", new=AsyncMock()),
    ):
        yield TestClient(app)


class TestHealthEndpoint:
    def test_health_check(self, client):
        response = client.get("/health")

        assert response.status_code == 200
        data = response.json()
        # In test context, components may not be initialized, so status can vary
        assert "status" in data
        assert data["version"] == "0.1.0"

    def test_liveness_probe(self, client):
        response = client.get("/live")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "alive"

    def test_readiness_probe(self, client):
        response = client.get("/ready")
        # May be 200 or 503 depending on component init state
        assert response.status_code in (200, 503)
        data = response.json()
        assert "ready" in data
        assert "components" in data


class TestModelsEndpoint:
    @patch(
        "app.api.routes_openai.create_model_list",
        return_value=ModelList(
            object="list",
            data=[
                Model(id="browser/qwen", created=1, owned_by="qwen"),
                Model(id="api/g4f-auto/default", created=1, owned_by="g4f-auto"),
                Model(
                    id="api/ollamafreeapi/llama3.3:70b",
                    created=1,
                    owned_by="ollamafreeapi",
                ),
            ],
        ),
    )
    def test_list_models_includes_browser_and_api(self, _mock_models, client):
        response = client.get("/v1/models")

        assert response.status_code == 200
        data = response.json()
        assert data["object"] == "list"

        model_ids = [m["id"] for m in data["data"]]
        assert "browser/qwen" in model_ids
        assert "api/g4f-auto/default" in model_ids
        assert "api/ollamafreeapi/llama3.3:70b" in model_ids

    @patch(
        "app.api.routes_openai.unified_registry.diagnostics",
        return_value={"browser": [], "api_integrations": [], "api_models": []},
    )
    @patch(
        "app.api.routes_openai.browser_dispatcher.diagnostics",
        return_value={"queue_size": 0, "state": "running"},
    )
    def test_diagnostics_endpoint(self, _mock_browser_diagnostics, _mock_diagnostics, client):
        response = client.get("/diagnostics/integrations")

        assert response.status_code == 200
        assert response.json()["api_models"] == []
        assert response.json()["browser_execution"]["state"] == "running"


class TestChatCompletionsEndpoint:
    def test_stream_not_supported(self, client):
        request_body = {
            "model": "browser/glm",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": True,
        }

        response = client.post("/v1/chat/completions", json=request_body)

        assert response.status_code == 400
        data = response.json()
        assert "Streaming is not supported" in data["message"]

    @patch(
        "app.services.chat_proxy_service.service.process_completion",
        side_effect=BadRequestError("Unknown model"),
    )
    def test_unknown_model(self, _mock_process, client):
        request_body = {
            "model": "unknown-model",
            "messages": [{"role": "user", "content": "Hello"}],
        }

        response = client.post("/v1/chat/completions", json=request_body)

        assert response.status_code == 400
        assert "Unknown model" in response.json()["message"]

    @patch("app.services.chat_proxy_service.service.process_completion")
    def test_successful_completion(self, mock_process, client):
        mock_process.return_value = ChatCompletionResponse(
            id="chatcmpl-test",
            created=1,
            model="browser/glm",
            choices=[
                Choice(
                    index=0,
                    message=Message(role="assistant", content="Hello! How can I help you?"),
                    finish_reason="stop",
                )
            ],
            usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
        )

        request_body = {
            "model": "glm",
            "messages": [{"role": "user", "content": "Hello"}],
        }

        response = client.post("/v1/chat/completions", json=request_body)

        assert response.status_code == 200
        data = response.json()
        assert data["object"] == "chat.completion"
        assert data["model"] == "browser/glm"
        assert data["choices"][0]["message"]["role"] == "assistant"
        assert data["choices"][0]["message"]["content"] == "Hello! How can I help you?"
        assert data["choices"][0]["finish_reason"] == "stop"


class TestRootEndpoint:
    def test_root(self, client):
        response = client.get("/")

        assert response.status_code == 200
        data = response.json()
        assert "MoreAI Proxy is running" in data["message"]
