"""
MoreAI E2E Regression — root conftest.

Registers pytest markers and provides shared fixtures for e2e tests.
"""

import os

import httpx
import pytest
from playwright.sync_api import Page, expect

# ── Markers ──


def pytest_configure(config):
    config.addinivalue_line("markers", "smoke: fast critical-path checks")
    config.addinivalue_line("markers", "regression: full regression suite")
    config.addinivalue_line("markers", "live: optional real-provider tests")


# ── Config ──

BASE_URL = os.environ.get("MOREAI_BASE_URL", "http://127.0.0.1:8000")
ADMIN_TOKEN = os.environ.get("MOREAI_ADMIN_TOKEN", "")
TIMEOUT_MS = 30000


# ── Fixtures ──


@pytest.fixture(scope="session")
def base_url():
    """Base URL from config or env override."""
    return BASE_URL


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
    """HTTP client for API tests."""
    with httpx.Client(base_url=base_url, timeout=10.0) as client:
        yield client


# ── Helpers (importable) ──


def send_studio_message(pg: Page, text: str):
    """Type a message in the studio input and send it."""
    textarea = pg.locator("#studio-message-input")
    expect(textarea).to_be_visible(timeout=TIMEOUT_MS)
    textarea.fill(text)
    textarea.press("Enter")


def wait_for_studio_response(pg: Page, timeout_ms: int = TIMEOUT_MS):
    """Wait until the assistant response message appears."""
    pg.wait_for_selector(".studio-message-assistant", state="visible", timeout=timeout_ms)


def clear_studio_chat(pg: Page):
    """Click the New Chat button."""
    pg.locator(".studio-new-chat").click()


def select_studio_mode(pg: Page, mode: str):
    """Select a mode in the studio sidebar."""
    pg.locator(f"input[name='studio-mode'][value='{mode}']").check()


def get_active_chat_title(pg: Page) -> str:
    """Get the title of the currently active chat from the sidebar."""
    active = pg.locator(".studio-chat-item.active .studio-chat-item-title")
    return active.inner_text() if active.count() > 0 else ""


def login_admin(pg: Page, url: str, token: str = ""):
    """Login to admin panel."""
    admin_token = token or ADMIN_TOKEN
    pg.goto(f"{url}/admin", wait_until="domcontentloaded", timeout=15000)
    token_input = pg.locator("#admin-token-input")
    if token_input.is_visible(timeout=3000):
        token_input.fill(admin_token)
        pg.locator("#login-btn").click()
        expect(pg.locator("#dashboard-screen")).to_be_visible(timeout=TIMEOUT_MS)


def click_admin_tab(pg: Page, tab_name: str):
    """Click a tab in the admin dashboard."""
    pg.locator(f"button.nav-tab[data-tab='{tab_name}']").click()
    pg.wait_for_timeout(500)


def expect_api_response_shape(response, required_keys: list[str]):
    """Assert that a JSON response contains all required top-level keys."""
    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
    data = response.json()
    for key in required_keys:
        assert key in data, f"Missing key '{key}' in response: {list(data.keys())}"
    return data
