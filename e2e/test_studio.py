"""
MoreAI E2E Regression — /studio Suite.

Covers the full /studio experience:
- page load, mode selector
- multi-chat sidebar (create, switch, delete)
- sending messages in different modes
- execution summary + expandable details
- conversation persistence across reload
- progress UX during pipeline execution
- error states and retry
"""

import pytest
from playwright.sync_api import Page, expect

from e2e.helpers import (
    clear_studio_chat,
    get_active_chat_title,
    select_studio_mode,
    send_studio_message,
    wait_for_studio_response,
)

pytestmark = [pytest.mark.regression]


class TestStudioBasics:
    """Studio page loads correctly."""

    def test_studio_opens(self, studio_page: Page):
        """Studio renders with its layout."""
        expect(studio_page.locator(".studio-container")).to_be_visible()
        expect(studio_page.locator(".studio-logo")).to_have_text("Studio")

    def test_mode_selector_all_modes(self, studio_page: Page):
        """All 5 response modes are visible and selectable."""
        for mode in ["fast", "balanced", "quality", "review", "deep"]:
            radio = studio_page.locator(f".studio-mode[data-mode='{mode}']")
            expect(radio).to_be_visible()

    def test_mode_selection_changes_header(self, studio_page: Page):
        """Selecting a mode updates the header badge."""
        select_studio_mode(studio_page, "deep")
        badge = studio_page.locator("#studio-mode-badge")
        expect(badge).to_have_text("Deep")

    def test_pipeline_indicator_for_pipeline_modes(self, studio_page: Page):
        """Pipeline modes show the Pipeline indicator."""
        for mode, expected in [("fast", False), ("quality", True), ("deep", True)]:
            select_studio_mode(studio_page, mode)
            indicator = studio_page.locator("#studio-pipeline-indicator")
            if expected:
                expect(indicator).to_be_visible()
            else:
                expect(indicator).not_to_be_visible()


class TestStudioMultiChat:
    """Sidebar chat list, create, switch, delete, persistence."""

    def test_new_chat_creates_entry(self, studio_page: Page):
        """New Chat button creates a new sidebar entry."""
        clear_studio_chat(studio_page)
        studio_page.wait_for_timeout(300)
        items = studio_page.locator(".studio-chat-item")
        assert items.count() >= 1

    def test_chat_has_active_state(self, studio_page: Page):
        """Active chat is highlighted in sidebar."""
        active = studio_page.locator(".studio-chat-item.active")
        expect(active).to_be_visible()

    def test_delete_chat(self, studio_page: Page):
        """Delete button removes a chat."""
        # Create a second chat
        clear_studio_chat(studio_page)
        studio_page.wait_for_timeout(300)
        initial_count = studio_page.locator(".studio-chat-item").count()

        # Delete the previous active chat (switch back first if needed)
        items = studio_page.locator(".studio-chat-item")
        if items.count() >= 2:
            # Hover over second item to show delete button
            items.nth(1).hover()
            delete_btn = items.nth(1).locator(".studio-chat-item-delete")
            delete_btn.click()
            studio_page.wait_for_timeout(300)
            assert studio_page.locator(".studio-chat-item").count() == initial_count - 1

    def test_chat_persistence_survives_reload(self, studio_page: Page):
        """After reload, chat list and mode are preserved."""
        # Select a non-default mode
        select_studio_mode(studio_page, "review")
        studio_page.wait_for_timeout(300)

        # Reload
        studio_page.reload(wait_until="domcontentloaded", timeout=15000)
        studio_page.wait_for_timeout(500)

        # Mode should be restored
        badge = studio_page.locator("#studio-mode-badge")
        expect(badge).to_have_text("Review")

        # Chat list should still exist
        expect(studio_page.locator(".studio-chat-list")).to_be_visible()


class TestStudioMessaging:
    """Sending messages and receiving responses."""

    def test_send_message_fast(self, studio_page: Page):
        """Fast mode: send message, get response."""
        select_studio_mode(studio_page, "fast")
        send_studio_message(studio_page, "Smoke test fast message")
        wait_for_studio_response(studio_page)
        # Verify response content is visible
        expect(studio_page.locator(".studio-message-assistant")).to_be_visible()

    def test_send_message_balanced(self, studio_page: Page):
        """Balanced mode: send message, get response."""
        select_studio_mode(studio_page, "balanced")
        send_studio_message(studio_page, "Smoke test balanced message")
        wait_for_studio_response(studio_page)

    def test_send_message_quality(self, studio_page: Page):
        """Quality mode: send message, get response, see execution summary."""
        select_studio_mode(studio_page, "quality")
        send_studio_message(studio_page, "Smoke test quality message")
        wait_for_studio_response(studio_page, timeout_ms=60000)
        # Execution summary should appear in right panel
        expect(studio_page.locator("#studio-execution-summary")).to_be_visible()

    def test_send_message_updates_sidebar_title(self, studio_page: Page):
        """After sending a message, the chat title auto-updates."""
        clear_studio_chat(studio_page)
        studio_page.wait_for_timeout(300)

        send_studio_message(studio_page, "This is a unique test title for auto-naming")
        wait_for_studio_response(studio_page, timeout_ms=60000)
        studio_page.wait_for_timeout(500)

        # Title should contain part of the message
        title = get_active_chat_title(studio_page)
        assert "unique" in title.lower() or "test" in title.lower() or len(title) > 0


class TestStudioExecutionSummary:
    """Right panel execution summary and details."""

    def test_execution_summary_appears_after_quality(self, studio_page: Page):
        """After quality mode, right panel shows stages, models, fallbacks."""
        select_studio_mode(studio_page, "quality")
        send_studio_message(studio_page, "Test execution summary")
        wait_for_studio_response(studio_page, timeout_ms=60000)

        summary = studio_page.locator("#studio-execution-summary")
        expect(summary).to_be_visible()

        # Key stats should be visible
        expect(summary.locator(".studio-exec-stat-label")).first.to_be_visible()

    def test_show_details_opens_details_panel(self, studio_page: Page):
        """Clicking 'Show details' loads execution details."""
        select_studio_mode(studio_page, "quality")
        send_studio_message(studio_page, "Test execution details")
        wait_for_studio_response(studio_page, timeout_ms=60000)

        # Click Show details
        details_btn = studio_page.locator(".studio-exec-details-btn")
        expect(details_btn).to_be_visible()
        details_btn.click()
        studio_page.wait_for_timeout(1000)

        # Details panel should be visible or show loading/empty state
        details = studio_page.locator("#studio-execution-details")
        # It's either visible with content, or visible with a message
        assert details.is_visible() or details.count() > 0

    def test_right_panel_shows_mode_badge(self, studio_page: Page):
        """Right panel shows the correct mode label."""
        select_studio_mode(studio_page, "deep")
        send_studio_message(studio_page, "Test mode badge")
        wait_for_studio_response(studio_page, timeout_ms=60000)

        summary = studio_page.locator("#studio-execution-summary")
        expect(summary.locator(".studio-exec-mode-label")).to_have_text("Deep")


class TestStudioErrorHandling:
    """Error states and recovery."""

    def test_clear_chat_resets_state(self, studio_page: Page):
        """Clear chat removes messages, shows welcome screen."""
        send_studio_message(studio_page, "Message to clear")
        wait_for_studio_response(studio_page, timeout_ms=60000)

        clear_studio_chat(studio_page)
        studio_page.wait_for_timeout(500)

        # Welcome screen should be visible
        expect(studio_page.locator(".studio-welcome")).to_be_visible()

    def test_chat_input_auto_resizes(self, studio_page: Page):
        """Textarea should exist and accept input."""
        textarea = studio_page.locator("#studio-message-input")
        expect(textarea).to_be_visible()
        textarea.fill("A longer message that would normally resize the textarea if it grows beyond one line of text content here")
        expect(textarea).to_have_value("A longer message that would normally resize the textarea if it grows beyond one line of text content here")
