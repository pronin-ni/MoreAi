import pytest
from app.browser.registry import ProviderRegistry
from app.browser.base import BrowserProvider
from app.browser.providers.glm import GlmProvider
from app.browser.providers.qwen import QwenProvider
from app.browser.providers.chatgpt import ChatGPTProvider
from app.browser.providers.yandex import YandexProvider
from app.browser.providers.kimi import KimiProvider


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
        reg.register(DummyProvider, model_ids=["dummy-model"], config={"url": "http://test"})

        assert "dummy" in reg._providers
        assert reg._model_to_provider["dummy-model"] == "dummy"

    def test_register_multiple_models(self):
        reg = ProviderRegistry()
        reg.register(DummyProvider, model_ids=["dm1", "dm2", "dm3"])

        assert reg._model_to_provider["dm1"] == "dummy"
        assert reg._model_to_provider["dm2"] == "dummy"
        assert reg._model_to_provider["dm3"] == "dummy"

    def test_get_provider_class(self):
        from app.browser.providers import GlmProvider, QwenProvider, ChatGPTProvider

        reg = ProviderRegistry()
        reg.register(GlmProvider, model_ids=["glm"])
        reg.register(QwenProvider, model_ids=["qwen", "internal-web-chat"])
        reg.register(ChatGPTProvider, model_ids=["chatgpt"])

        glm_cls = reg.get_provider_class("glm")
        assert glm_cls == GlmProvider

        qwen_cls = reg.get_provider_class("qwen")
        assert qwen_cls == QwenProvider

        qwen_cls2 = reg.get_provider_class("internal-web-chat")
        assert qwen_cls2 == QwenProvider

    def test_get_unknown_model_raises(self):
        from app.core.errors import BadRequestError

        reg = ProviderRegistry()
        reg.register(GlmProvider, model_ids=["glm"])

        with pytest.raises(BadRequestError) as exc_info:
            reg.get_provider_class("unknown-model")

        assert "Unknown model" in exc_info.value.detail["message"]

    def test_list_models(self):
        reg = ProviderRegistry()
        reg.register(GlmProvider, model_ids=["glm"])
        reg.register(QwenProvider, model_ids=["qwen", "internal-web-chat"])
        reg.register(KimiProvider, model_ids=["kimi"])

        models = reg.list_models()

        assert len(models) == 4
        model_ids = [m["id"] for m in models]
        assert "glm" in model_ids
        assert "qwen" in model_ids
        assert "internal-web-chat" in model_ids
        assert "kimi" in model_ids

    def test_get_provider_config(self):
        reg = ProviderRegistry()
        reg.register(GlmProvider, model_ids=["glm"], config={"url": "https://chat.z.ai/"})

        config = reg.get_provider_config("glm")
        assert config["url"] == "https://chat.z.ai/"


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

    def test_yandex_routing(self):
        from app.browser.providers import YandexProvider

        reg = ProviderRegistry()
        reg.register(YandexProvider, model_ids=["yandex"])

        provider_cls = reg.get_provider_class("yandex")
        assert provider_cls == YandexProvider

        models = reg.list_models()
        model_ids = [m["id"] for m in models]
        assert "yandex" in model_ids

    def test_kimi_routing(self):
        reg = ProviderRegistry()
        reg.register(KimiProvider, model_ids=["kimi"], config={"url": "https://www.kimi.com/"})

        provider_cls = reg.get_provider_class("kimi")
        assert provider_cls == KimiProvider

        config = reg.get_provider_config("kimi")
        assert config["url"] == "https://www.kimi.com/"
