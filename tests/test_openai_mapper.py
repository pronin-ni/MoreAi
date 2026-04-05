import time
from unittest.mock import patch

from app.schemas.openai import ChatCompletionResponse, ModelList
from app.utils.openai_mapper import (
    create_completion_response,
    create_model_list,
    generate_completion_id,
)


class TestGenerateCompletionId:
    def test_generates_correct_prefix(self):
        assert generate_completion_id().startswith("chatcmpl-")

    def test_generates_unique_ids(self):
        ids = [generate_completion_id() for _ in range(100)]
        assert len(set(ids)) == 100


class TestCreateCompletionResponse:
    def test_creates_valid_response(self):
        response = create_completion_response(
            model="browser/glm",
            content="Hello! How can I help you?",
        )

        assert isinstance(response, ChatCompletionResponse)
        assert response.id.startswith("chatcmpl-")
        assert response.object == "chat.completion"
        assert response.model == "browser/glm"
        assert len(response.choices) == 1
        assert response.choices[0].message.role == "assistant"
        assert response.choices[0].message.content == "Hello! How can I help you?"
        assert response.choices[0].finish_reason == "stop"

    def test_response_has_created_timestamp(self):
        before = int(time.time())
        response = create_completion_response(model="test-model", content="test")
        after = int(time.time())
        assert before <= response.created <= after


class TestCreateModelList:
    @patch(
        "app.registry.unified.unified_registry.list_models",
        return_value=[
            {
                "id": "browser/qwen",
                "provider_id": "qwen",
                "transport": "browser",
                "source_type": "browser",
                "enabled": True,
                "available": True,
            },
            {
                "id": "api/g4f-auto/default",
                "provider_id": "g4f-auto",
                "transport": "api",
                "source_type": "g4f_openai",
                "enabled": True,
                "available": True,
            },
        ],
    )
    def test_creates_valid_model_list(self, _mock_list_models):
        model_list = create_model_list()

        assert isinstance(model_list, ModelList)
        assert model_list.object == "list"
        assert len(model_list.data) == 2

        model_ids = [m.id for m in model_list.data]
        assert "browser/qwen" in model_ids
        assert "api/g4f-auto/default" in model_ids

    @patch(
        "app.registry.unified.unified_registry.list_models",
        return_value=[
            {
                "id": "browser/qwen",
                "provider_id": "qwen",
                "transport": "browser",
                "source_type": "browser",
                "enabled": True,
                "available": True,
            }
        ],
    )
    def test_model_has_created_timestamp(self, _mock_list_models):
        before = int(time.time())
        model_list = create_model_list()
        after = int(time.time())

        for model in model_list.data:
            assert before <= model.created <= after

    @patch(
        "app.registry.unified.unified_registry.list_models",
        return_value=[
            {
                "id": "browser/qwen",
                "provider_id": "qwen",
                "transport": "browser",
                "source_type": "browser",
                "enabled": True,
                "available": True,
            }
        ],
    )
    def test_model_owned_by(self, _mock_list_models):
        model_list = create_model_list()

        qwen_model = next(m for m in model_list.data if m.id == "browser/qwen")
        assert qwen_model.owned_by == "qwen"
