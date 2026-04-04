import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright
import os
import uuid

from app.core.config import settings
from app.core.logging import get_logger
from app.core.errors import BrowserError

logger = get_logger(__name__)


class BrowserSession:
    def __init__(self, context: BrowserContext, page: Page, session_id: str):
        self.context = context
        self.page = page
        self.session_id = session_id
        self._in_use = False

    @property
    def in_use(self) -> bool:
        return self._in_use

    def mark_used(self) -> None:
        self._in_use = True

    def mark_available(self) -> None:
        self._in_use = False


class BrowserSessionPool:
    def __init__(self, pool_size: int = 5):
        self.pool_size = pool_size
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._sessions: list[BrowserSession] = []
        self._semaphore: asyncio.Semaphore | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        logger.info("Initializing browser session pool", pool_size=self.pool_size)
        
        self._playwright = await async_playwright().start()
        
        self._browser = await self._playwright.chromium.launch(
            headless=settings.headless,
            slow_mo=settings.browser_slowmo,
        )
        
        self._semaphore = asyncio.Semaphore(self.pool_size)
        
        logger.info("Browser session pool initialized successfully")

    async def shutdown(self) -> None:
        logger.info("Shutting down browser session pool")
        
        async with self._lock:
            for session in self._sessions:
                try:
                    await session.context.close()
                except Exception as e:
                    logger.warning("Error closing browser context", error=str(e))
            
            self._sessions.clear()
        
        if self._browser:
            await self._browser.close()
        
        if self._playwright:
            await self._playwright.stop()
        
        logger.info("Browser session pool shutdown complete")

    @asynccontextmanager
    async def acquire_session(self, model: str | None = None) -> AsyncGenerator[BrowserSession, None]:
        if not self._semaphore:
            raise BrowserError("Browser pool not initialized")

        async with self._semaphore:
            storage_state_path = self._get_storage_state_path(model)
            if storage_state_path:
                session = await self._create_new_session(storage_state_path=storage_state_path)
                session.mark_used()
                try:
                    yield session
                finally:
                    session.mark_available()
                    await self._dispose_session(session)
                return

            session = await self._get_or_create_session()
            session.mark_used()

            try:
                yield session
            finally:
                session.mark_available()
                await self._cleanup_session(session)

    def _get_storage_state_path(self, model: str | None) -> str | None:
        if not model:
            return settings.auth_storage_state_path

        from app.browser.registry import registry

        provider_config = registry.get_provider_config(model)
        configured_path = provider_config.get("storage_state_path")
        if configured_path:
            path = Path(configured_path)
            return str(path) if path.exists() else str(path)

        return settings.auth_storage_state_path

    async def _get_or_create_session(self) -> BrowserSession:
        async with self._lock:
            for session in self._sessions:
                if not session.in_use:
                    logger.debug("Reusing existing browser session", session_id=session.session_id)
                    return session
            
            if len(self._sessions) < self.pool_size:
                session = await self._create_new_session()
                self._sessions.append(session)
                logger.debug("Created new browser session", session_id=session.session_id)
                return session
        
        raise BrowserError("No available browser sessions")

    async def _create_new_session(self, storage_state_path: str | None = None) -> BrowserSession:
        context_options = {
            "viewport": {"width": 1280, "height": 720},
            "ignore_https_errors": True,
            "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        }

        chosen_storage_state = storage_state_path or settings.auth_storage_state_path
        if chosen_storage_state and os.path.exists(chosen_storage_state):
            context_options["storage_state"] = chosen_storage_state
        
        context = await self._browser.new_context(**context_options)
        page = await context.new_page()
        
        await page.add_init_script('''() => {
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        }''')
        
        session_id = str(uuid.uuid4())
        return BrowserSession(context, page, session_id)

    async def _cleanup_session(self, session: BrowserSession) -> None:
        await self._dispose_session(session)

        async with self._lock:
            if session in self._sessions:
                self._sessions.remove(session)

            try:
                new_session = await self._create_new_session()
                self._sessions.append(new_session)
                logger.debug("Created replacement session", session_id=new_session.session_id)
            except Exception as create_error:
                logger.error("Failed to create replacement session", error=str(create_error))

    async def _dispose_session(self, session: BrowserSession) -> None:
        page_closed = False
        try:
            await session.page.close()
            page_closed = True
        except Exception as e:
            logger.warning("Error closing page", session_id=session.session_id, error=str(e))
        
        try:
            await session.context.close()
        except Exception as close_error:
            logger.warning(
                "Error closing context",
                session_id=session.session_id,
                error=str(close_error),
            )
        
pool = BrowserSessionPool(pool_size=settings.browser_pool_size)
