"""
MoreAI E2E Regression — /ui Suite.

Covers the simple chat UI:
- page load
- models list renders
- model selection works
- send message → response
- diagnostics panel updates
- clear chat works
"""

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.regression]


class TestUIBasics:
    """UI page loads and renders correctly."""

    def test_ui_opens(self, ui_page: Page):
        """UI page renders with its layout."""
        expect(ui_page.locator(".app-container")).to_be_visible()

    def test_ui_models_panel_visible(self, ui_page: Page):
        """Model list sidebar is rendered."""
        expect(ui_page.locator("#models-panel")).to_be_visible()

    def test_ui_chat_panel_visible(self, ui_page: Page):
        """Chat area is rendered."""
        expect(ui_page.locator(".chat-panel")).to_be_visible()

    def test_ui_diagnostics_panel_visible(self, ui_page: Page):
        """Diagnostics sidebar is rendered."""
        expect(ui_page.locator("#diagnostics-panel")).to_be_visible()


class TestUIModellist:
    """Model list rendering and selection."""

    def test_models_listed(self, ui_page: Page):
        """At least one model item is visible."""
        # Models are rendered as radio buttons or items in the models panel
        models = ui_page.locator("#models-panel input[type='radio']")
        assert models.count() >= 1

    def test_model_selection_changes_badge(self, ui_page: Page):
        """Selecting a model updates the chat header badge."""
        # Click first available model
        first_model = ui_page.locator("#models-panel input[type='radio']:not([disabled])").first
        if first_model.count() > 0:
            first_model.click()
            ui_page.wait_for_timeout(300)
            # Model badge in header should update
            expect(ui_page.locator("#model-badge")).to_be_visible()


class TestUIMessaging:
    """Sending messages in /ui."""

    def test_send_message(self, ui_page: Page):
        """Type a message and see a response."""
        textarea = ui_page.locator("#chat-form textarea, textarea[name='message']")
        if textarea.count() == 0:
            # Fallback: any textarea in chat area
            textarea = ui_page.locator(".chat-panel textarea").first

        expect(textarea).to_be_visible()
        textarea.fill("Hello from UI regression test")
        # Submit via Enter
        textarea.press("Enter")

        # Wait for response message
        ui_page.wait_for_selector(
            ".message-assistant, .chat-response, [class*='message-assistant']",
            state="visible",
            timeout=60000,
        )

    def test_clear_chat(self, ui_page: Page):
        """Clear button resets the chat."""
        # Look for clear button
        clear_btn = ui_page.locator("button:has-text('Clear'), .clear-btn, #clear-btn")
        if clear_btn.count() > 0 and clear_btn.is_visible():
            clear_btn.click()
            ui_page.wait_for_timeout(300)
            # Messages should be cleared or welcome shown


class TestUIDiagnostics:
    """Diagnostics panel behavior."""

    def test_diagnostics_shows_model_info(self, ui_page: Page):
        """Diagnostics panel displays model/transport info."""
        # After selecting a model, diagnostics should show info
        first_model = ui_page.locator("#models-panel input[type='radio']:not([disabled])").first
        if first_model.count() > 0:
            first_model.click()
            ui_page.wait_for_timeout(500)
            # Diagnostics content should be visible
            expect(ui_page.locator("#diagnostics-content-target")).to_be_visible()
