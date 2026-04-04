from app.browser.providers.glm import GlmProvider
from app.browser.providers.qwen import QwenProvider
from app.browser.providers.chatgpt import ChatGPTProvider
from app.browser.providers.yandex import YandexProvider
from app.browser.providers.kimi import KimiProvider
from app.browser.registry import registry
from app.core.config import settings

registry.register(
    QwenProvider,
    model_ids=["internal-web-chat", "qwen"],
    config={
        "url": settings.qwen.url,
        "storage_state_path": settings.qwen.storage_state_path,
    },
)

registry.register(
    GlmProvider,
    model_ids=["glm"],
    config={
        "url": settings.glm.url,
        "storage_state_path": settings.glm.storage_state_path,
    },
)

registry.register(
    ChatGPTProvider,
    model_ids=["chatgpt"],
    config={
        "url": settings.chatgpt.url,
        "storage_state_path": settings.chatgpt.storage_state_path,
    },
)

registry.register(
    YandexProvider,
    model_ids=["yandex"],
    config={
        "url": settings.yandex.url,
        "storage_state_path": settings.yandex.storage_state_path,
    },
)

registry.register(
    KimiProvider,
    model_ids=["kimi"],
    config={
        "url": settings.kimi.url,
        "skip_auth_url": settings.kimi.skip_auth_url,
        "storage_state_path": settings.kimi.storage_state_path,
        "auth_provider": "google",
    },
)

__all__ = ["GlmProvider", "QwenProvider", "ChatGPTProvider", "YandexProvider", "KimiProvider"]
