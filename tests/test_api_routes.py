import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


class TestHealthEndpoint:
    def test_health_check(self, client):
        response = client.get("/health")
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["version"] == "0.1.0"


class TestModelsEndpoint:
    def test_list_models_includes_all(self, client):
        response = client.get("/v1/models")
        
        assert response.status_code == 200
        data = response.json()
        assert data["object"] == "list"
        
        model_ids = [m["id"] for m in data["data"]]
        assert "glm" in model_ids
        assert "internal-web-chat" in model_ids
        assert "chatgpt" in model_ids
        assert "yandex" in model_ids
        assert "kimi" in model_ids
        assert len(model_ids) == 6


class TestChatCompletionsEndpoint:
    def test_stream_not_supported(self, client):
        request_body = {
            "model": "glm",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": True,
        }
        
        response = client.post("/v1/chat/completions", json=request_body)
        
        assert response.status_code == 400
        data = response.json()
        assert "Streaming is not supported" in data["message"]

    def test_unknown_model(self, client):
        request_body = {
            "model": "unknown-model",
            "messages": [{"role": "user", "content": "Hello"}],
        }
        
        response = client.post("/v1/chat/completions", json=request_body)
        
        assert response.status_code == 400
        data = response.json()
        assert "Unknown model" in data["message"]

    @patch("app.services.chat_proxy_service.service.process_completion")
    def test_successful_completion(self, mock_process, client):
        mock_process.return_value = "Hello! How can I help you?"
        
        request_body = {
            "model": "glm",
            "messages": [{"role": "user", "content": "Hello"}],
        }
        
        response = client.post("/v1/chat/completions", json=request_body)
        
        assert response.status_code == 200
        data = response.json()
        assert data["object"] == "chat.completion"
        assert data["model"] == "glm"
        assert data["choices"][0]["message"]["role"] == "assistant"
        assert data["choices"][0]["message"]["content"] == "Hello! How can I help you?"
        assert data["choices"][0]["finish_reason"] == "stop"


class TestRootEndpoint:
    def test_root(self, client):
        response = client.get("/")
        
        assert response.status_code == 200
        data = response.json()
        assert "MoreAI Proxy is running" in data["message"]
