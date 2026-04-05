from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BrowserSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    message_input: str = Field(default='textarea[placeholder*="Чем"]')
    send_button: str = Field(default='button:has(img[src*="send"])')
    assistant_message: str = Field(default="main p:last-of-type")
    generation_indicator: str = Field(default='img[src*="thinking"], img[src*="loading"]')
    new_chat: str = Field(default='a[href="/"], button:has-text("?")')


class QwenSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QWEN_", extra="ignore")

    url: str = Field(default="https://chat.qwen.ai/")
    headless: bool = Field(default=True)
    storage_state_path: Optional[str] = Field(default=None)


class GlmSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GLM_", extra="ignore")

    url: str = Field(default="https://chat.z.ai/")
    headless: bool = Field(default=True)
    storage_state_path: Optional[str] = Field(default=None)


class ChatGPTSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CHATGPT_", extra="ignore")

    url: str = Field(default="https://chatgpt.com/")
    headless: bool = Field(default=True)
    storage_state_path: Optional[str] = Field(default=None)


class YandexSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="YANDEX_", extra="ignore")

    url: str = Field(default="https://alice.yandex.ru/")
    headless: bool = Field(default=True)
    storage_state_path: Optional[str] = Field(default=None)


class KimiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KIMI_", extra="ignore")

    url: str = Field(default="https://www.kimi.com/")
    headless: bool = Field(default=True)
    storage_state_path: Optional[str] = Field(default="./secrets/kimi.storage_state.json")
    skip_auth_url: str = Field(default="https://www.kimi.com/?skip_auth=1")


class DeepseekSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DEEPSEEK_", extra="ignore")

    url: str = Field(default="https://chat.deepseek.com/sign_in")
    headless: bool = Field(default=True)
    storage_state_path: Optional[str] = Field(default="./secrets/deepseek.storage_state.json")
    login: Optional[str] = Field(default=None)
    password: Optional[str] = Field(default=None)


class GoogleAuthSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GOOGLE_AUTH_", extra="ignore")

    credentials_path: Optional[str] = Field(default="./secrets/browser_auth.json")
    auto_bootstrap: bool = Field(default=True)
    timeout_seconds: int = Field(default=180, ge=30)
    post_login_wait_seconds: int = Field(default=10, ge=1)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    internal_chat_url: str = Field(default="https://chat.qwen.ai/")
    auth_storage_state_path: Optional[str] = Field(default=None)

    headless: bool = Field(default=True)
    browser_pool_size: int = Field(default=5, ge=1)
    browser_queue_max_size: int = Field(default=20, ge=1)
    browser_enqueue_timeout_seconds: int = Field(default=2, ge=1)
    browser_queue_wait_timeout_seconds: int = Field(default=30, ge=1)
    browser_task_execution_timeout_seconds: int = Field(default=120, ge=1)
    browser_startup_timeout_seconds: int = Field(default=30, ge=1)
    browser_shutdown_grace_seconds: int = Field(default=10, ge=1)
    browser_max_retries: int = Field(default=1, ge=0)
    browser_retry_backoff_seconds: float = Field(default=0.5, ge=0.0)
    browser_provider_concurrency_limits: dict[str, int] = Field(default_factory=dict)
    browser_provider_circuit_failure_threshold: int = Field(default=3, ge=1)
    browser_provider_circuit_open_seconds: int = Field(default=30, ge=1)
    browser_provider_adaptive_cooldown_seconds: float = Field(default=0.25, ge=0.0)
    browser_provider_adaptive_cooldown_max_seconds: float = Field(default=5.0, ge=0.0)
    response_timeout_seconds: int = Field(default=120, ge=1)
    retry_attempts: int = Field(default=1, ge=0)
    browser_slowmo: int = Field(default=0, ge=0)
    integrations_enabled: bool = Field(default=True)
    integrations_auto_discover_models: bool = Field(default=True)
    integrations_discovery_timeout_seconds: int = Field(default=10, ge=1)
    integrations_retry_attempts: int = Field(default=1, ge=0)
    integrations_allow_fallback_models: bool = Field(default=True)
    integrations_config_path: str = Field(default="./config/integrations.toml")
    integrations_rate_limit_cooldown_seconds: int = Field(default=60, ge=1)
    g4f_api_key: Optional[str] = Field(default=None)

    artifacts_dir: str = Field(default="./artifacts")

    app_host: str = Field(default="0.0.0.0")
    app_port: int = Field(default=8000)
    log_level: str = Field(default="INFO")

    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    qwen: QwenSettings = Field(default_factory=QwenSettings)
    glm: GlmSettings = Field(default_factory=GlmSettings)
    chatgpt: ChatGPTSettings = Field(default_factory=ChatGPTSettings)
    yandex: YandexSettings = Field(default_factory=YandexSettings)
    kimi: KimiSettings = Field(default_factory=KimiSettings)
    deepseek: DeepseekSettings = Field(default_factory=DeepseekSettings)
    google_auth: GoogleAuthSettings = Field(default_factory=GoogleAuthSettings)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if "browser" not in kwargs:
            object.__setattr__(self, "browser", BrowserSettings())
        if "qwen" not in kwargs:
            object.__setattr__(self, "qwen", QwenSettings())
        if "glm" not in kwargs:
            object.__setattr__(self, "glm", GlmSettings())
        if "chatgpt" not in kwargs:
            object.__setattr__(self, "chatgpt", ChatGPTSettings())
        if "yandex" not in kwargs:
            object.__setattr__(self, "yandex", YandexSettings())
        if "kimi" not in kwargs:
            object.__setattr__(self, "kimi", KimiSettings())
        if "deepseek" not in kwargs:
            object.__setattr__(self, "deepseek", DeepseekSettings())
        if "google_auth" not in kwargs:
            object.__setattr__(self, "google_auth", GoogleAuthSettings())


settings = Settings()
