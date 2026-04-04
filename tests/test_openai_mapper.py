import pytest
import time
from app.utils.openai_mapper import (
    generate_completion_id,
    create_completion_response,
    create_model_list,
)
from app.schemas.openai import ChatCompletionResponse, ModelList


class TestGenerateCompletionId:
    def test_generates_correct_prefix(self):
        result = generate_completion_id()
        
        assert result.startswith("chatcmpl-")

    def test_generates_unique_ids(self):
        ids = [generate_completion_id() for _ in range(100)]
        
        assert len(set(ids)) == 100


class TestCreateCompletionResponse:
    def test_creates_valid_response(self):
        response = create_completion_response(
            model="glm",
            content="Hello! How can I help you?",
        )
        
        assert isinstance(response, ChatCompletionResponse)
        assert response.id.startswith("chatcmpl-")
        assert response.object == "chat.completion"
        assert response.model == "glm"
        assert len(response.choices) == 1
        assert response.choices[0].message.role == "assistant"
        assert response.choices[0].message.content == "Hello! How can I help you?"
        assert response.choices[0].finish_reason == "stop"
        assert response.usage.prompt_tokens == 0
        assert response.usage.completion_tokens == 0
        assert response.usage.total_tokens == 0

    def test_response_has_created_timestamp(self):
        before = int(time.time())
        response = create_completion_response(
            model="test-model",
            content="test",
        )
        after = int(time.time())
        
        assert before <= response.created <= after


class TestCreateModelList:
    def test_creates_valid_model_list(self):
        model_list = create_model_list()
        
        assert isinstance(model_list, ModelList)
        assert model_list.object == "list"
        assert len(model_list.data) == 6
        
        model_ids = [m.id for m in model_list.data]
        assert "glm" in model_ids
        assert "internal-web-chat" in model_ids
        assert "chatgpt" in model_ids
        assert "yandex" in model_ids
        assert "kimi" in model_ids

    def test_model_has_created_timestamp(self):
        before = int(time.time())
        model_list = create_model_list()
        after = int(time.time())
        
        for model in model_list.data:
            assert before <= model.created <= after

    def test_model_owned_by(self):
        model_list = create_model_list()
        
        glm_model = next(m for m in model_list.data if m.id == "glm")
        assert glm_model.owned_by == "glm"
        
        qwen_model = next(m for m in model_list.data if m.id == "internal-web-chat")
        assert qwen_model.owned_by == "qwen"
