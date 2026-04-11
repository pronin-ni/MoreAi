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
        self._restart_in_progress = False
        self._restart_event = asyncio.Event()
        self._restart_event.set()  # Initially ready

    @property
    def ready(self) -> bool:
        return self._browser is not None and not self._restart_in_progress

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
        """Safe restart: signal in-flight sessions, block new ones, then restart."""
        logger.warning("Restarting browser runtime", worker_name=self.worker_name)
        self._restart_in_progress = True
        self._restart_event.clear()  # Block new open_session calls
        try:
            await self.stop()
            await self.start()
        finally:
            self._restart_in_progress = False
            self._restart_event.set()  # Allow new open_session calls

    @asynccontextmanager
    async def open_session(self, storage_state_path: str | None = None):
        """Open a session, but bail out if a restart is in progress."""
        # Wait for any ongoing restart to complete
        await self._restart_event.wait()

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
        except Exception:
            # If the browser was restarted during this session,
            # the context/page may be closed. Swallow close errors.
            pass
        finally:
            try:
                await page.close()
            except Exception:
                pass
            try:
                await context.close()
            except Exception:
                pass
