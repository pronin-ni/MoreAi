import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from app.core.config import settings
from app.core.errors import BrowserError
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class RuntimeSession:
    context: BrowserContext
    page: Page


class WorkerBrowserRuntime:
    def __init__(self, worker_name: str):
        self.worker_name = worker_name
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    @property
    def ready(self) -> bool:
        return self._browser is not None

    async def start(self) -> None:
        if self._browser is not None:
            return

        logger.info("Starting browser runtime", worker_name=self.worker_name)
        async with asyncio.timeout(settings.browser_startup_timeout_seconds):
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=settings.headless,
                slow_mo=settings.browser_slowmo,
            )

    async def stop(self) -> None:
        browser = self._browser
        playwright = self._playwright
        self._browser = None
        self._playwright = None

        if browser is not None:
            await browser.close()
        if playwright is not None:
            await playwright.stop()

    async def restart(self) -> None:
        logger.warning("Restarting browser runtime", worker_name=self.worker_name)
        await self.stop()
        await self.start()

    @asynccontextmanager
    async def open_session(self, storage_state_path: str | None = None):
        if self._browser is None:
            raise BrowserError("Browser runtime is not initialized")

        context_options = {
            "viewport": {"width": 1280, "height": 720},
            "ignore_https_errors": True,
            "user_agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
            ),
        }

        if storage_state_path:
            path = Path(storage_state_path)
            if os.path.exists(path):
                context_options["storage_state"] = str(path)

        context = await self._browser.new_context(**context_options)
        page = await context.new_page()
        await page.add_init_script(
            """() => { Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); }"""
        )

        try:
            yield RuntimeSession(context=context, page=page)
        finally:
            try:
                await page.close()
            except Exception as exc:
                logger.warning(
                    "Failed to close page",
                    worker_name=self.worker_name,
                    error=str(exc),
                )
            try:
                await context.close()
            except Exception as exc:
                logger.warning(
                    "Failed to close browser context",
                    worker_name=self.worker_name,
                    error=str(exc),
                )
