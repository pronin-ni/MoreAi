"""
MoreAI E2E Regression — Smoke Suite.

Fast, critical-path checks that the app is basically alive and functional.
Run: pytest e2e/ -m smoke --headed
"""

import pytest
from playwright.sync_api import Page, expect

from e2e.helpers import (
    select_studio_mode,
    send_studio_message,
    wait_for_studio_response,
)

pytestmark = [pytest.mark.smoke, pytest.mark.regression]


class TestSmokeStudio:
    """Smoke: /studio must open and accept a message."""

    def test_studio_opens(self, studio_page: Page):
        """Studio page loads."""
        expect(studio_page.locator(".studio-container")).to_be_visible()
        expect(studio_page.locator(".studio-logo")).to_have_text("Studio")

    def test_studio_mode_selector_visible(self, studio_page: Page):
        """All 5 modes are visible (labels)."""
        for mode in ["fast", "balanced", "quality", "review", "deep"]:
            expect(studio_page.locator(f".studio-mode[data-mode='{mode}']")).to_be_visible()

    def test_studio_send_message_fast(self, studio_page: Page):
        """Send a message in Fast mode, see a response."""
        select_studio_mode(studio_page, "fast")
        send_studio_message(studio_page, "Hello smoke test")
        wait_for_studio_response(studio_page)


class TestSmokeUI:
    """Smoke: /ui must open and show models."""

    def test_ui_opens(self, ui_page: Page):
        """UI page loads."""
        expect(ui_page.locator(".app-container")).to_be_visible()

    def test_ui_models_visible(self, ui_page: Page):
        """Model list is rendered."""
        expect(ui_page.locator("#models-panel")).to_be_visible()


class TestSmokeAPI:
    """Smoke: key API endpoints respond."""

    def test_v1_models(self, api_client):
        """GET /v1/models returns model list."""
        resp = api_client.get("/v1/models")
        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data or "models" in data

    def test_root(self, api_client):
        """GET / returns service info."""
        resp = api_client.get("/")
        assert resp.status_code == 200
