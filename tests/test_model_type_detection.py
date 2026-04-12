"""
Tests for model type detection and non-chat model filtering.

Covers:
- Model type inference from name patterns
- Chat model filtering during discovery
- Non-chat models are excluded from chat candidate pool
- model_type metadata is added to ModelDefinition
"""

import pytest

from app.integrations.adapters import infer_model_type, is_chat_model


# ── Model Type Detection Tests ──


class TestModelTypeDetection:
    """Test model type inference from name patterns."""

    def test_chat_models_detected(self):
        """Chat models should be classified as 'chat'."""
        assert infer_model_type("gpt-4") == "chat"
        assert infer_model_type("gpt-4o") == "chat"
        assert infer_model_type("gpt-4o-mini") == "chat"
        assert infer_model_type("claude-sonnet-4-20250514") == "chat"
        assert infer_model_type("claude-3-5-haiku-20241022") == "chat"
        assert infer_model_type("qwen-plus") == "chat"
        assert infer_model_type("qwen3-coder") == "chat"
        assert infer_model_type("deepseek-chat") == "chat"
        assert infer_model_type("llama-3.1-8b-instruct") == "chat"
        assert infer_model_type("kimi-k2-thinking") == "chat"
        assert infer_model_type("mistral-large-2") == "chat"
        assert infer_model_type("nova-fast") == "chat"
        assert infer_model_type("gemini-2.5-pro") == "chat"

    def test_audio_models_detected(self):
        """Audio/speech-to-text models should be classified as 'audio'."""
        assert infer_model_type("whisper-large-v3-turbo") == "audio"
        assert infer_model_type("whisper-1") == "audio"
        assert infer_model_type("openai/whisper-large-v3") == "audio"

    def test_tts_models_detected(self):
        """Text-to-speech models should be classified as 'tts'."""
        assert infer_model_type("tts-1") == "tts"
        assert infer_model_type("tts-1-hd") == "tts"

    def test_image_models_detected(self):
        """Image models should be classified as 'image'."""
        assert infer_model_type("dall-e-3") == "image"
        assert infer_model_type("dall-e-2") == "image"
        assert infer_model_type("dall_e-3") == "image"
        assert infer_model_type("image-alpha-001") == "image"

    def test_embedding_models_detected(self):
        """Embedding models should be classified as 'embedding'."""
        assert infer_model_type("text-embedding-3-small") == "embedding"
        assert infer_model_type("text-embedding-3-large") == "embedding"
        assert infer_model_type("text-embedding-ada-002") == "embedding"
        assert infer_model_type("embedding-001") == "embedding"

    def test_moderation_models_detected(self):
        """Moderation models should be classified as 'moderation'."""
        assert infer_model_type("text-moderation-latest") == "moderation"
        assert infer_model_type("text-moderation-stable") == "moderation"
        assert infer_model_type("omni-moderation-latest") == "moderation"

    def test_video_models_detected(self):
        """Video models should be classified as 'video'."""
        assert infer_model_type("sora-2") == "video"
        assert infer_model_type("video-001") == "video"

    def test_is_chat_model(self):
        """is_chat_model should return True only for chat models."""
        assert is_chat_model("gpt-4o") is True
        assert is_chat_model("claude-sonnet-4") is True
        assert is_chat_model("whisper-large-v3-turbo") is False
        assert is_chat_model("dall-e-3") is False
        assert is_chat_model("text-embedding-3-small") is False
        assert is_chat_model("text-moderation-latest") is False


# ── Integration with Discovery Tests ──


class TestDiscoveryFiltering:
    """Test that non-chat models are filtered during discovery."""

    def test_fallback_models_get_type_metadata(self):
        """Fallback models should have model_type in metadata."""
        from app.integrations.adapters import BaseIntegrationAdapter
        from app.integrations.types import IntegrationDefinition, IntegrationRuntimeConfig

        definition = IntegrationDefinition(
            integration_id="test-provider",
            display_name="Test",
            integration_type="openai_compatible",
            group="supported_api_route",
            source_type="external_api",
            base_url="https://example.com",
            api_key_requirement="required",
        )
        config = IntegrationRuntimeConfig(
            enabled=True,
            base_url="https://example.com",
            api_key="test-key",
            discover_models=True,
        )

        class TestAdapter(BaseIntegrationAdapter):
            async def discover_models(self):
                return self._build_model_definitions(["gpt-4", "whisper-large-v3"], available=True)

            async def create_chat_completion(self, request, canonical_model_id):
                pass

        adapter = TestAdapter(definition, config)
        models = adapter._build_model_definitions(["gpt-4", "whisper-large-v3"], available=True)

        assert len(models) == 2
        assert models[0].metadata.get("model_type") == "chat"
        assert models[1].metadata.get("model_type") == "audio"
