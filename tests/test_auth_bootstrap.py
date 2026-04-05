import json

import pytest

from app.browser.auth import AuthBootstrapper, GoogleAuthBootstrapper
from app.core.config import settings
from app.core.errors import BrowserError


class TestGoogleAuthBootstrapper:
    def test_load_google_credentials_success(self, tmp_path):
        credentials_file = tmp_path / "browser_auth.json"
        credentials_file.write_text(
            json.dumps(
                {
                    "google": {
                        "email": "user@example.com",
                        "password": "secret-password",
                        "recovery_email": "recovery@example.com",
                    }
                }
            ),
            encoding="utf-8",
        )

        bootstrapper = GoogleAuthBootstrapper()
        original_path = settings.google_auth.credentials_path
        object.__setattr__(settings.google_auth, "credentials_path", str(credentials_file))
        try:
            credentials = bootstrapper._load_google_credentials()
        finally:
            object.__setattr__(settings.google_auth, "credentials_path", original_path)

        assert credentials.email == "user@example.com"
        assert credentials.password == "secret-password"
        assert credentials.recovery_email == "recovery@example.com"

    def test_load_google_credentials_requires_google_object(self, tmp_path):
        credentials_file = tmp_path / "browser_auth.json"
        credentials_file.write_text(json.dumps({"not_google": {}}), encoding="utf-8")

        bootstrapper = GoogleAuthBootstrapper()
        original_path = settings.google_auth.credentials_path
        object.__setattr__(settings.google_auth, "credentials_path", str(credentials_file))
        try:
            with pytest.raises(BrowserError) as exc_info:
                bootstrapper._load_google_credentials()
        finally:
            object.__setattr__(settings.google_auth, "credentials_path", original_path)

        assert "google" in str(exc_info.value).lower()


class TestProviderCredentialsBootstrapper:
    def test_load_provider_credentials_success(self, tmp_path):
        bootstrapper = AuthBootstrapper()
        credentials = bootstrapper._load_provider_credentials(
            "deepseek",
            {"login": "user@example.com", "password": "secret-password"},
        )

        assert credentials.email == "user@example.com"
        assert credentials.password == "secret-password"

    def test_load_provider_credentials_requires_values(self):
        bootstrapper = AuthBootstrapper()

        with pytest.raises(BrowserError) as exc_info:
            bootstrapper._load_provider_credentials(
                "deepseek",
                {"login": "", "password": ""},
            )

        assert (
            "not configured" in str(exc_info.value).lower()
            or "must not be empty" in str(exc_info.value).lower()
        )
