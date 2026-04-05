import os

from app.integrations.config import load_integrations_config
from app.integrations.definitions import READY_TO_USE_DEFINITIONS


class TestIntegrationsConfig:
    def test_defaults_include_enabled_g4f_groq(self):
        snapshot = load_integrations_config(READY_TO_USE_DEFINITIONS)

        assert snapshot.by_integration["g4f-groq"].enabled is True

    def test_missing_required_key_does_not_change_enabled_flag(self):
        snapshot = load_integrations_config(READY_TO_USE_DEFINITIONS)

        assert snapshot.by_integration["g4f-hosted"].enabled is False

    def test_shared_g4f_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("G4F_API_KEY", "shared-token")
        from app.core.config import settings

        object.__setattr__(settings, "g4f_api_key", os.getenv("G4F_API_KEY"))
        snapshot = load_integrations_config(READY_TO_USE_DEFINITIONS)

        assert snapshot.by_integration["g4f-groq"].api_key == "shared-token"
        assert snapshot.by_integration["g4f-groq"].api_key_source == "g4f_shared_env"
