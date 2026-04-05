import pytest
from pydantic import ValidationError
from app.core.config import BrowserSettings, Settings


class TestBrowserSettings:
    def test_default_selectors(self):
        settings = BrowserSettings()

        assert "Чем" in settings.message_input
        assert "send" in settings.send_button

    def test_custom_selectors_from_env(self):
        settings = BrowserSettings(
            message_input="#custom-input",
            send_button="#custom-send",
        )

        assert settings.message_input == "#custom-input"
        assert settings.send_button == "#custom-send"


class TestSettings:
    def test_default_settings(self):
        settings = Settings()

        assert "qwen.ai" in settings.internal_chat_url
        assert settings.headless is True
        assert settings.browser_pool_size == 5
        assert settings.response_timeout_seconds == 120
        assert settings.kimi.url == "https://www.kimi.com/"
        assert settings.deepseek.url == "https://chat.deepseek.com/sign_in"
        assert settings.google_auth.auto_bootstrap is True

    def test_browser_settings_integration(self):
        settings = Settings()

        assert isinstance(settings.browser, BrowserSettings)
        assert settings.browser.message_input is not None

    def test_provider_specific_storage_state_settings(self):
        settings = Settings(
            kimi={"storage_state_path": "./secrets/kimi.custom.json"},
            deepseek={
                "storage_state_path": "./secrets/deepseek.custom.json",
                "login": "deepseek-user@example.com",
                "password": "deepseek-password",
            },
            google_auth={"credentials_path": "./secrets/auth.json", "timeout_seconds": 240},
        )

        assert settings.kimi.storage_state_path == "./secrets/kimi.custom.json"
        assert settings.deepseek.storage_state_path == "./secrets/deepseek.custom.json"
        assert settings.deepseek.login == "deepseek-user@example.com"
        assert settings.deepseek.password == "deepseek-password"
        assert settings.google_auth.credentials_path == "./secrets/auth.json"
        assert settings.google_auth.timeout_seconds == 240

    def test_pool_size_validation(self):
        with pytest.raises(ValidationError):
            Settings(browser_pool_size=0)

    def test_timeout_validation(self):
        with pytest.raises(ValidationError):
            Settings(response_timeout_seconds=-1)

    def test_settings_singleton(self):
        from app.core.config import settings as singleton_settings

        assert singleton_settings.internal_chat_url is not None
