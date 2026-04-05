from app.browser.providers.glm import GlmProvider
from app.browser.providers.qwen import QwenProvider
from app.browser.providers.chatgpt import ChatGPTProvider
from app.browser.providers.yandex import YandexProvider
from app.browser.providers.kimi import KimiProvider
from app.browser.providers.deepseek import DeepseekProvider
from app.browser.registry import registry
from app.core.config import settings

registry.register(
    QwenProvider,
    canonical_model_id="browser/qwen",
    alias_ids=["internal-web-chat", "qwen"],
    config={
        "url": settings.qwen.url,
        "storage_state_path": settings.qwen.storage_state_path,
    },
)

registry.register(
    GlmProvider,
    canonical_model_id="browser/glm",
    alias_ids=["glm"],
    config={
        "url": settings.glm.url,
        "storage_state_path": settings.glm.storage_state_path,
    },
)

registry.register(
    ChatGPTProvider,
    canonical_model_id="browser/chatgpt",
    alias_ids=["chatgpt"],
    config={
        "url": settings.chatgpt.url,
        "storage_state_path": settings.chatgpt.storage_state_path,
    },
)

registry.register(
    YandexProvider,
    canonical_model_id="browser/yandex",
    alias_ids=["yandex"],
    config={
        "url": settings.yandex.url,
        "storage_state_path": settings.yandex.storage_state_path,
    },
)

registry.register(
    KimiProvider,
    canonical_model_id="browser/kimi",
    alias_ids=["kimi"],
    config={
        "url": settings.kimi.url,
        "skip_auth_url": settings.kimi.skip_auth_url,
        "storage_state_path": settings.kimi.storage_state_path,
        "auth_provider": "google",
    },
)

registry.register(
    DeepseekProvider,
    canonical_model_id="browser/deepseek",
    alias_ids=["deepseek"],
    config={
        "url": settings.deepseek.url,
        "storage_state_path": settings.deepseek.storage_state_path,
        "login": settings.deepseek.login,
        "password": settings.deepseek.password,
        "auth_provider": "credentials",
    },
)

__all__ = [
    "GlmProvider",
    "QwenProvider",
    "ChatGPTProvider",
    "YandexProvider",
    "KimiProvider",
    "DeepseekProvider",
]
