"""
MoreAI E2E Regression — Shared fixtures and helpers.

Provides:
- page_with_base_url: browser page navigated to the app
- studio_page: page ready for /studio tests
- admin_page: page ready for /admin tests
- ui_page: page ready for /ui tests
- api_client: httpx.AsyncClient for API tests
- Helper functions for common flows
"""

import os

import httpx
import pytest
from playwright.sync_api import Page, expect

from e2e.config import ADMIN_TOKEN, BASE_URL, TIMEOUT_MS


@pytest.fixture(scope="session")
def base_url():
    """Base URL from config or env override."""
    return os.environ.get("MOREAI_BASE_URL", BASE_URL)


@pytest.fixture
def page_with_base_url(page: Page, base_url: str):
    """Page with base URL already set."""
    page.set_default_timeout(TIMEOUT_MS)
    page.goto(base_url, wait_until="domcontentloaded", timeout=15000)
    return page


@pytest.fixture
def studio_page(page: Page, base_url: str) -> Page:
    """Page navigated to /studio."""
    page.set_default_timeout(TIMEOUT_MS)
    page.goto(f"{base_url}/studio", wait_until="domcontentloaded", timeout=15000)
    return page


@pytest.fixture
def admin_page(page: Page, base_url: str) -> Page:
    """Page navigated to /admin."""
    page.set_default_timeout(TIMEOUT_MS)
    page.goto(f"{base_url}/admin", wait_until="domcontentloaded", timeout=15000)
    return page


@pytest.fixture
def ui_page(page: Page, base_url: str) -> Page:
    """Page navigated to /ui."""
    page.set_default_timeout(TIMEOUT_MS)
    page.goto(f"{base_url}/ui", wait_until="domcontentloaded", timeout=15000)
    return page


@pytest.fixture
def api_client(base_url: str):
    """Async HTTP client for API tests."""
    with httpx.Client(base_url=base_url, timeout=10.0) as client:
        yield client


# ── Helper functions ──


def send_studio_message(page: Page, text: str):
    """Type a message in the studio input and send it."""
    textarea = page.locator("#studio-message-input")
    expect(textarea).to_be_visible(timeout=TIMEOUT_MS)
    textarea.fill(text)
    # Trigger Enter key
    textarea.press("Enter")


def wait_for_studio_response(page: Page, timeout_ms: int = TIMEOUT_MS):
    """Wait until the assistant response message appears."""
    page.wait_for_selector(
        ".studio-message-assistant",
        state="visible",
        timeout=timeout_ms,
    )


def clear_studio_chat(page: Page):
    """Click the New Chat button."""
    page.locator(".studio-new-chat").click()


def select_studio_mode(page: Page, mode: str):
    """Select a mode in the studio sidebar by clicking the label."""
    page.locator(f".studio-mode[data-mode='{mode}']").click()


def get_active_chat_title(page: Page) -> str:
    """Get the title of the currently active chat from the sidebar."""
    active = page.locator(".studio-chat-item.active .studio-chat-item-title")
    return active.inner_text() if active.count() > 0 else ""


def login_admin(page: Page, base_url: str, token: str = ""):
    """Login to admin panel. Uses token from param, env, or config."""
    admin_token = token or os.environ.get("MOREAI_ADMIN_TOKEN", ADMIN_TOKEN)
    page.goto(f"{base_url}/admin", wait_until="domcontentloaded", timeout=15000)

    # If there's a login screen, enter the token
    token_input = page.locator("#admin-token-input")
    if token_input.is_visible(timeout=3000):
        token_input.fill(admin_token)
        page.locator("#login-btn").click()
        # Wait for dashboard to appear
        expect(page.locator("#dashboard-screen")).to_be_visible(timeout=TIMEOUT_MS)


def click_admin_tab(page: Page, tab_name: str):
    """Click a tab in the admin dashboard."""
    page.locator(f"button.nav-tab[data-tab='{tab_name}']").click()
    # Give tab content time to render
    page.wait_for_timeout(500)


def expect_api_response_shape(response, required_keys: list[str]):
    """Assert that a JSON response contains all required top-level keys."""
    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
    data = response.json()
    for key in required_keys:
        assert key in data, f"Missing key '{key}' in response: {list(data.keys())}"
    return data
