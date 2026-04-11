"""
MoreAI E2E Regression — /admin Suite.

Covers safe, read-only admin flows:
- page load, login (if token set)
- key tabs open without errors
- pipeline/scoring/trends pages load
- diagnostics respond
"""

import os

import pytest
from playwright.sync_api import Page, expect

from e2e.helpers import click_admin_tab, login_admin

pytestmark = [pytest.mark.regression]


class TestAdminBasics:
    """Admin page loads and renders."""

    def test_admin_opens(self, admin_page: Page):
        """Admin page renders (login or dashboard)."""
        # Either login screen or dashboard should be visible
        has_login = admin_page.locator("#login-screen").is_visible(timeout=3000)
        has_dashboard = admin_page.locator("#dashboard-screen").is_visible(timeout=3000)
        assert has_login or has_dashboard, "Admin page should show login or dashboard"


class TestAdminTabs:
    """Key admin tabs open without errors."""

    @pytest.fixture(autouse=True)
    def ensure_logged_in(self, admin_page: Page, base_url: str):
        """Try to log in if login screen is visible."""
        token = os.environ.get("MOREAI_ADMIN_TOKEN", "")
        if token:
            login_admin(admin_page, base_url, token)
        # If still on login screen without token, skip tests that need dashboard
        yield

    def _skip_if_not_logged_in(self, page: Page):
        """Skip test if we're stuck on login screen."""
        try:
            if page.locator("#login-screen").is_visible(timeout=2000):
                pytest.skip("Admin login required (set MOREAI_ADMIN_TOKEN)")
        except Exception:
            pass

    def test_tab_overview(self, admin_page: Page):
        """Overview tab loads."""
        self._skip_if_not_logged_in(admin_page)
        click_admin_tab(admin_page, "Overview")
        expect(admin_page.locator("#tab-Overview")).to_be_visible()

    def test_tab_providers(self, admin_page: Page):
        """Providers tab loads."""
        self._skip_if_not_logged_in(admin_page)
        click_admin_tab(admin_page, "Providers")
        expect(admin_page.locator("#tab-Providers")).to_be_visible()

    def test_tab_models(self, admin_page: Page):
        """Models tab loads."""
        self._skip_if_not_logged_in(admin_page)
        click_admin_tab(admin_page, "Models")
        expect(admin_page.locator("#tab-Models")).to_be_visible()

    def test_tab_pipelines(self, admin_page: Page):
        """Pipelines tab loads."""
        self._skip_if_not_logged_in(admin_page)
        click_admin_tab(admin_page, "Pipelines")
        expect(admin_page.locator("#tab-Pipelines")).to_be_visible()

    def test_tab_scoring(self, admin_page: Page):
        """Scoring tab loads."""
        self._skip_if_not_logged_in(admin_page)
        click_admin_tab(admin_page, "Scoring")
        expect(admin_page.locator("#tab-Scoring")).to_be_visible()

    def test_tab_trends(self, admin_page: Page):
        """Trends tab loads."""
        self._skip_if_not_logged_in(admin_page)
        click_admin_tab(admin_page, "Trends")
        expect(admin_page.locator("#tab-Trends")).to_be_visible()

    def test_tab_health(self, admin_page: Page):
        """Health tab loads."""
        self._skip_if_not_logged_in(admin_page)
        click_admin_tab(admin_page, "Health")
        expect(admin_page.locator("#tab-Health")).to_be_visible()

    def test_tab_analytics(self, admin_page: Page):
        """Analytics tab loads."""
        self._skip_if_not_logged_in(admin_page)
        click_admin_tab(admin_page, "Analytics")
        expect(admin_page.locator("#tab-Analytics")).to_be_visible()
