
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
    storage_state_path: str | None = Field(default=None)


class GlmSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GLM_", extra="ignore")

    url: str = Field(default="https://chat.z.ai/")
    headless: bool = Field(default=True)
    storage_state_path: str | None = Field(default=None)


class ChatGPTSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CHATGPT_", extra="ignore")

    url: str = Field(default="https://chatgpt.com/")
    headless: bool = Field(default=True)
    storage_state_path: str | None = Field(default=None)


class YandexSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="YANDEX_", extra="ignore")

    url: str = Field(default="https://alice.yandex.ru/")
    headless: bool = Field(default=True)
    storage_state_path: str | None = Field(default=None)


class KimiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KIMI_", extra="ignore")

    url: str = Field(default="https://www.kimi.com/")
    headless: bool = Field(default=True)
    storage_state_path: str | None = Field(default="./secrets/kimi.storage_state.json")
    skip_auth_url: str = Field(default="https://www.kimi.com/?skip_auth=1")


class DeepseekSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DEEPSEEK_", extra="ignore")

    url: str = Field(default="https://chat.deepseek.com/sign_in")
    headless: bool = Field(default=True)
    storage_state_path: str | None = Field(default="./secrets/deepseek.storage_state.json")
    login: str | None = Field(default=None)
    password: str | None = Field(default=None)


class GoogleAuthSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GOOGLE_AUTH_", extra="ignore")

    credentials_path: str | None = Field(default="./secrets/browser_auth.json")
    auto_bootstrap: bool = Field(default=True)
    timeout_seconds: int = Field(default=180, ge=30)
    post_login_wait_seconds: int = Field(default=10, ge=1)


class ReconSettings(BaseSettings):
    """Auto-recon recovery configuration."""

    model_config = SettingsConfigDict(env_prefix="RECON_", extra="ignore")

    enabled: bool = Field(default=True, description="Enable auto-recon recovery")
    max_time_ms: float = Field(default=3000.0, ge=500, le=10000, description="Max time budget for recon recovery")
    max_dom_scans: int = Field(default=1, ge=0, le=3, description="Max HealingEngine DOM scans")
    max_page_reloads: int = Field(default=1, ge=0, le=2, description="Max soft page reloads")
    max_replay_attempts: int = Field(default=1, ge=0, le=3, description="Max action replay attempts")
    candidate_limit: int = Field(default=10, ge=1, le=50, description="Max candidates per role scan")
    allow_soft_reload: bool = Field(default=True, description="Allow soft page reload during recon")
    allow_new_chat_recovery: bool = Field(default=True, description="Allow start_new_chat as recovery action")
    abort_on_login_wall: bool = Field(default=True, description="Abort recon immediately on login wall")
    abort_on_modal_blockers: bool = Field(default=True, description="Abort recon if modal/dialog overlay detected")


class OpenCodeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENCODE_", extra="ignore")

    # Core
    enabled: bool = Field(default=True)
    base_url: str = Field(default="http://127.0.0.1:4096")
    username: str = Field(default="opencode")
    password: str | None = Field(default=None)
    timeout_seconds: int = Field(default=120, ge=1)
    discovery_enabled: bool = Field(default=True)
    session_ttl_seconds: int = Field(default=60, ge=1)

    # Managed lifecycle
    managed: bool = Field(
        default=True,
        description="If true, MoreAI manages the OpenCode subprocess lifecycle",
    )
    autostart: bool = Field(
        default=True,
        description="If true and managed=true, automatically start opencode serve on app startup",
    )
    command: str = Field(default="opencode", description="Command to run for OpenCode server")
    port: int = Field(default=4096, ge=1, le=65535, description="Port for OpenCode server")
    startup_timeout_seconds: int = Field(default=30, ge=5, description="Max seconds to wait for server to become healthy")
    healthcheck_interval_seconds: int = Field(default=1, ge=1, description="Poll interval for readiness healthcheck")
    graceful_shutdown_seconds: int = Field(default=10, ge=1, description="Grace period for SIGTERM before SIGKILL")
    working_dir: str | None = Field(default=None, description="Working directory for the subprocess")
    extra_env: dict[str, str] = Field(default_factory=dict, description="Additional environment variables for the subprocess")
    required: bool = Field(default=False, description="If true and managed+autostart fails, fail app startup")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    internal_chat_url: str = Field(default="https://chat.qwen.ai/")
    auth_storage_state_path: str | None = Field(default=None)

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
    g4f_api_key: str | None = Field(default=None)

    # Admin
    admin_token: str | None = Field(default=None, description="Admin API auth token")

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
    opencode: OpenCodeSettings = Field(default_factory=OpenCodeSettings)
    google_auth: GoogleAuthSettings = Field(default_factory=GoogleAuthSettings)
    recon: ReconSettings = Field(default_factory=ReconSettings)

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
        if "opencode" not in kwargs:
            object.__setattr__(self, "opencode", OpenCodeSettings())
        if "google_auth" not in kwargs:
            object.__setattr__(self, "google_auth", GoogleAuthSettings())
        if "recon" not in kwargs:
            object.__setattr__(self, "recon", ReconSettings())


settings = Settings()
