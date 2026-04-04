import asyncio
import json
from datetime import datetime
from pathlib import Path

from playwright.async_api import Page, Playwright, async_playwright

from app.browser.registry import registry
from app.core.logging import configure_logging, get_logger

configure_logging("INFO")
logger = get_logger(__name__)


class UIRecon:
    def __init__(self, model: str):
        self.model = model
        self.playwright: Playwright | None = None
        self.browser = None
        self.page: Page | None = None
        self.findings: dict = {"model": model}
        self.artifacts_dir = Path("artifacts") / "recon" / model
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.provider_class = registry.get_provider_class(model)
        self.provider_config = registry.get_provider_config(model)

    async def initialize(self) -> None:
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=False)
        context = await self.browser.new_context(
            viewport={"width": 1440, "height": 1100},
            ignore_https_errors=True,
        )
        self.page = await context.new_page()
        logger.info("Browser initialized for recon", model=self.model)

    async def close(self) -> None:
        if self.page:
            await self.page.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        logger.info("Browser closed", model=self.model)

    async def discover(self) -> dict:
        provider = self.provider_class(self.page, provider_config=self.provider_config)
        logger.info("Starting provider recon", model=self.model, provider_id=provider.provider_id)

        await provider.navigate_to_chat()
        await self._capture_screenshot("01_initial_load")

        self.findings.update(provider.recon_hints())
        self.findings["resolved_url"] = self.page.url
        self.findings["title"] = await self.page.title()
        self.findings["login_required"] = await provider.detect_login_required()
        self.findings["new_chat_url"] = f"{provider.target_url.rstrip('/')}/?chat_enter_method=new_chat"
        self.findings["visible_text_preview"] = await self._safe_inner_text(self.page.locator("body").first)

        await self._save_html("01_initial_load")

        try:
            await provider.start_new_chat()
            self.findings["start_new_chat"] = "success"
            await self._capture_screenshot("02_after_new_chat")
        except Exception as exc:
            self.findings["start_new_chat"] = f"error: {exc}"

        self.findings["login_required_after_reset"] = await provider.detect_login_required()
        self.findings["dom_snapshot"] = await self._collect_dom_snapshot()
        await self._save_findings()
        return self.findings

    async def _collect_dom_snapshot(self) -> dict:
        snapshot = {
            "textbox_count": await self.page.get_by_role("textbox").count(),
            "button_count": await self.page.get_by_role("button").count(),
            "link_count": await self.page.get_by_role("link").count(),
        }
        key_locators = {
            "new_chat": self.page.get_by_role("link", name="New Chat").first,
            "continue_with_google": self.page.get_by_text("Continue with Google", exact=False).first,
            "phone_number": self.page.get_by_role("textbox", name="Phone number").first,
            "send_button_container": self.page.locator(".send-button-container").first,
            "chat_input_editor": self.page.locator(".chat-input-editor").first,
        }
        for key, locator in key_locators.items():
            try:
                snapshot[key] = await locator.is_visible(timeout=500)
            except Exception:
                snapshot[key] = False
        return snapshot

    async def _capture_screenshot(self, name: str) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{name}_{timestamp}.png"
        path = self.artifacts_dir / filename
        await self.page.screenshot(path=str(path), full_page=True)
        logger.info("Saved screenshot", path=str(path))

    async def _save_html(self, name: str) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{name}_{timestamp}.html"
        path = self.artifacts_dir / filename
        path.write_text(await self.page.content(), encoding="utf-8")
        logger.info("Saved HTML", path=str(path))

    async def _save_findings(self) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"findings_{timestamp}.json"
        path = self.artifacts_dir / filename
        path.write_text(json.dumps(self.findings, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("Saved findings", path=str(path))

    async def _safe_inner_text(self, locator) -> str:
        try:
            text = await locator.inner_text(timeout=1_500)
        except Exception:
            return ""
        return text[:4000]


async def run_recon(model: str = "internal-web-chat") -> None:
    recon = UIRecon(model=model)
    try:
        await recon.initialize()
        await recon.discover()
        print(f"\nUI recon complete for {model}. Findings saved to: {recon.artifacts_dir}")
    finally:
        await recon.close()


if __name__ == "__main__":
    asyncio.run(run_recon())
