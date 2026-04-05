import pytest

from app.browser.base import BrowserProvider
from app.browser.providers.chatgpt import ChatGPTProvider
from app.browser.providers.deepseek import DeepseekProvider
from app.browser.providers.glm import GlmProvider
from app.browser.providers.kimi import KimiProvider
from app.browser.providers.qwen import QwenProvider
from app.browser.providers.yandex import YandexProvider
from app.browser.registry import ProviderRegistry


class DummyProvider(BrowserProvider):
    provider_id = "dummy"
    model_name = "dummy"
    display_name = "Dummy"
    target_url = "http://localhost"

    async def navigate_to_chat(self):
        pass

    async def start_new_chat(self):
        pass

    async def send_message(self, text: str):
        pass

    async def wait_for_response(self, timeout: int = 120) -> str:
        return "response"

    async def save_debug_artifacts(self, error_message: str):
        pass


class TestProviderRegistry:
    def test_register_provider(self):
        reg = ProviderRegistry()
        reg.register(
            DummyProvider,
            canonical_model_id="browser/dummy",
            alias_ids=["dummy-model"],
            config={"url": "http://test"},
        )

        assert "dummy" in reg._providers
        assert reg.resolve_model("dummy-model") == "browser/dummy"

    def test_register_multiple_aliases(self):
        reg = ProviderRegistry()
        reg.register(
            DummyProvider,
            canonical_model_id="browser/dummy",
            alias_ids=["dm1", "dm2", "dm3"],
        )

        assert reg.resolve_model("dm1") == "browser/dummy"
        assert reg.resolve_model("dm2") == "browser/dummy"
        assert reg.resolve_model("dm3") == "browser/dummy"

    def test_get_provider_class(self):
        reg = ProviderRegistry()
        reg.register(GlmProvider, canonical_model_id="browser/glm", alias_ids=["glm"])
        reg.register(
            QwenProvider,
            canonical_model_id="browser/qwen",
            alias_ids=["qwen", "internal-web-chat"],
        )
        reg.register(
            ChatGPTProvider,
            canonical_model_id="browser/chatgpt",
            alias_ids=["chatgpt"],
        )

        assert reg.get_provider_class("glm") == GlmProvider
        assert reg.get_provider_class("browser/qwen") == QwenProvider
        assert reg.get_provider_class("internal-web-chat") == QwenProvider

    def test_get_unknown_model_raises(self):
        from app.core.errors import BadRequestError

        reg = ProviderRegistry()
        reg.register(GlmProvider, canonical_model_id="browser/glm", alias_ids=["glm"])

        with pytest.raises(BadRequestError) as exc_info:
            reg.get_provider_class("unknown-model")

        assert "Unknown model" in exc_info.value.detail["message"]

    def test_list_models_returns_only_canonical_ids(self):
        reg = ProviderRegistry()
        reg.register(GlmProvider, canonical_model_id="browser/glm", alias_ids=["glm"])
        reg.register(
            QwenProvider,
            canonical_model_id="browser/qwen",
            alias_ids=["qwen", "internal-web-chat"],
        )
        reg.register(KimiProvider, canonical_model_id="browser/kimi", alias_ids=["kimi"])
        reg.register(
            DeepseekProvider,
            canonical_model_id="browser/deepseek",
            alias_ids=["deepseek"],
        )

        models = reg.list_models()

        assert len(models) == 4
        model_ids = [m["id"] for m in models]
        assert "browser/glm" in model_ids
        assert "browser/qwen" in model_ids
        assert "browser/kimi" in model_ids
        assert "browser/deepseek" in model_ids
        assert "glm" not in model_ids

    def test_get_provider_config(self):
        reg = ProviderRegistry()
        reg.register(
            GlmProvider,
            canonical_model_id="browser/glm",
            alias_ids=["glm"],
            config={"url": "https://chat.z.ai/"},
        )

        assert reg.get_provider_config("glm")["url"] == "https://chat.z.ai/"


class TestProviderClasses:
    def test_glm_provider_attrs(self):
        assert GlmProvider.provider_id == "glm"
        assert GlmProvider.model_name == "glm"
        assert GlmProvider.display_name == "Z.ai GLM"
        assert GlmProvider.target_url == "https://chat.z.ai/"

    def test_qwen_provider_attrs(self):
        assert QwenProvider.provider_id == "qwen"
        assert QwenProvider.model_name == "qwen"
        assert QwenProvider.display_name == "Qwen Chat"
        assert QwenProvider.target_url == "https://chat.qwen.ai/"

    def test_chatgpt_provider_attrs(self):
        assert ChatGPTProvider.provider_id == "chatgpt"
        assert ChatGPTProvider.model_name == "chatgpt"
        assert ChatGPTProvider.display_name == "ChatGPT"
        assert ChatGPTProvider.target_url == "https://chatgpt.com/"

    def test_yandex_provider_attrs(self):
        assert YandexProvider.provider_id == "yandex"
        assert YandexProvider.model_name == "yandex"
        assert YandexProvider.display_name == "Alice-Yandex"
        assert YandexProvider.target_url == "https://alice.yandex.ru/"

    def test_kimi_provider_attrs(self):
        assert KimiProvider.provider_id == "kimi"
        assert KimiProvider.model_name == "kimi"
        assert KimiProvider.display_name == "Kimi"
        assert KimiProvider.target_url == "https://www.kimi.com/"
        assert KimiProvider.requires_auth is True
        assert KimiProvider.auth_provider == "google"

    def test_deepseek_provider_attrs(self):
        assert DeepseekProvider.provider_id == "deepseek"
        assert DeepseekProvider.model_name == "deepseek"
        assert DeepseekProvider.display_name == "Deepseek"
        assert DeepseekProvider.target_url == "https://chat.deepseek.com/sign_in"
        assert DeepseekProvider.requires_auth is True
        assert DeepseekProvider.auth_provider == "credentials"
