import asyncio
from datetime import datetime
from pathlib import Path

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout, Locator

from app.core.config import settings
from app.core.logging import get_logger
from app.core.errors import (
    BrowserError,
    ChatNotReadyError,
    MessageInputNotFoundError,
    SendButtonNotFoundError,
    NewChatButtonNotFoundError,
    AssistantMessageNotFoundError,
    GenerationTimeoutError,
)

logger = get_logger(__name__)


class InternalChatBrowserClient:
    def __init__(self, page: Page):
        self.page = page
        self._request_id: str | None = None

    def set_request_id(self, request_id: str) -> None:
        self._request_id = request_id

    async def navigate_to_chat(self) -> None:
        logger.info("Navigating to Qwen Chat", url=settings.internal_chat_url)
        await self.page.goto(settings.internal_chat_url)
        await self.page.wait_for_load_state("domcontentloaded")
        await self._handle_cookie_consent()
        await self._handle_onboarding()

    async def _handle_cookie_consent(self) -> None:
        try:
            consent_text = "Используя Qwen Chat"
            consent_element = self.page.get_by_text(consent_text, exact=False)
            if await consent_element.is_visible(timeout=3000):
                await consent_element.click()
                logger.info("Accepted cookie consent")
                await asyncio.sleep(0.5)
        except PlaywrightTimeout:
            logger.debug("No cookie consent banner found")
        except Exception as e:
            logger.warning("Cookie consent handling error", error=str(e))

    async def _handle_onboarding(self) -> None:
        try:
            start_button = self.page.get_by_role("button", name="Начать")
            if await start_button.is_visible(timeout=2000):
                await start_button.click()
                logger.info("Closed onboarding")
                await asyncio.sleep(0.5)
        except PlaywrightTimeout:
            logger.debug("No onboarding found")
        except Exception as e:
            logger.warning("Onboarding handling error", error=str(e))

    async def start_new_chat(self) -> None:
        logger.info("Starting new chat by navigating to home")
        await self.page.goto(settings.internal_chat_url)
        await self.page.wait_for_load_state("domcontentloaded")
        await self._handle_cookie_consent()
        
        try:
            help_textbox = self.page.get_by_role("textbox", name="Чем я могу помочь")
            await help_textbox.wait_for(state="visible", timeout=5000)
            logger.debug("New chat ready - input field visible")
        except PlaywrightTimeout:
            logger.warning("Could not verify new chat state")

    async def send_message(self, text: str) -> None:
        await self._fill_message_input(text)
        await self._click_send_button()

    async def _fill_message_input(self, text: str) -> None:
        selectors_to_try = [
            lambda: self.page.get_by_role("textbox", name="Чем я могу помочь"),
            lambda: self.page.locator('textarea[placeholder*="Чем"]'),
            lambda: self.page.locator('textarea').first,
            lambda: self.page.locator("main textarea"),
        ]
        
        input_locator = None
        for selector_fn in selectors_to_try:
            try:
                locator = selector_fn()
                if await locator.is_visible(timeout=2000):
                    input_locator = locator
                    logger.debug("Found input with selector", selector_type=type(locator).__name__)
                    break
            except Exception:
                continue
        
        if not input_locator:
            raise MessageInputNotFoundError(
                "Message input not found with any selector",
                details={"tried_selectors": len(selectors_to_try)},
            )
        
        await input_locator.fill(text)
        logger.debug("Filled message input", text_length=len(text))

    async def _click_send_button(self) -> None:
        selectors_to_try = [
            lambda: self.page.locator('button:has(img[src*="send"])'),
            lambda: self.page.locator("main button").nth(1),
            lambda: self.page.locator("button:has(img)").last,
            lambda: self.page.get_by_role("button").nth(2),
        ]
        
        button_locator = None
        for selector_fn in selectors_to_try:
            try:
                locator = selector_fn()
                if await locator.is_visible(timeout=2000):
                    button_locator = locator
                    break
            except Exception:
                continue
        
        if not button_locator:
            raise SendButtonNotFoundError("Send button not found with any selector")
        
        await button_locator.click()
        logger.info("Clicked send button")

    async def wait_for_response(self, timeout: int = 120) -> str:
        logger.info("Waiting for response generation", timeout=timeout)
        
        try:
            await self._wait_for_response_start(timeout=30)
            
            response_text = await self._wait_for_response_end(timeout=timeout)
            
            logger.info("Received response from assistant", response_length=len(response_text))
            return response_text
            
        except PlaywrightTimeout:
            raise GenerationTimeoutError(
                f"Response generation timed out after {timeout} seconds",
                details={"timeout": timeout},
            )

    async def _wait_for_response_start(self, timeout: int = 30) -> None:
        try:
            help_textbox = self.page.get_by_role("textbox", name="Чем я могу помочь")
            await help_textbox.wait_for(state="hidden", timeout=timeout * 1000)
            logger.debug("Input hidden - generation started")
        except PlaywrightTimeout:
            logger.debug("Input still visible, checking for response")

    async def _wait_for_response_end(self, timeout: int = 120) -> str:
        start_time = asyncio.get_event_loop().time()
        last_text = ""
        stable_count = 0
        
        while (asyncio.get_event_loop().time() - start_time) < timeout:
            try:
                response = await self._extract_assistant_response()
                
                if response and response != last_text:
                    last_text = response
                    stable_count = 0
                elif response and response == last_text:
                    stable_count += 1
                    if stable_count >= 3:
                        logger.debug("Response stable after 3 checks")
                        return response
                        
            except AssistantMessageNotFoundError:
                pass
            
            await asyncio.sleep(0.5)
        
        if last_text:
            logger.debug("Returning last known response", text_length=len(last_text))
            return last_text
        
        raise AssistantMessageNotFoundError(
            "No assistant response found after timeout",
            details={"timeout": timeout},
        )

    async def _extract_assistant_response(self) -> str:
        selectors_to_try = [
            lambda: self.page.locator("main p:last-of-type"),
            lambda: self.page.locator("main").locator("p").last,
            lambda: self.page.locator("main > div > p").last,
            lambda: self.page.locator('[class*="message"]').last,
        ]
        
        for selector_fn in selectors_to_try:
            try:
                locator = selector_fn()
                text = await locator.inner_text(timeout=2000)
                if text and text.strip():
                    text = text.strip()
                    user_message = self.page.locator("main p").first
                    user_text = await user_message.inner_text(timeout=1000) if user_message else ""
                    if text != user_text:
                        return text
            except Exception:
                continue
        
        raise AssistantMessageNotFoundError("Assistant message not found")

    async def save_debug_artifacts(self, error_message: str) -> None:
        artifacts_dir = Path(settings.artifacts_dir)
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        request_id_part = self._request_id[:8] if self._request_id else "unknown"
        
        screenshot_path = artifacts_dir / f"error_{request_id_part}_{timestamp}.png"
        html_path = artifacts_dir / f"error_{request_id_part}_{timestamp}.html"
        
        try:
            await self.page.screenshot(path=str(screenshot_path), full_page=True)
            logger.info("Saved error screenshot", path=str(screenshot_path))
        except Exception as e:
            logger.warning("Failed to save screenshot", error=str(e))
        
        try:
            html_content = await self.page.content()
            html_path.write_text(html_content, encoding="utf-8")
            logger.info("Saved error HTML", path=str(html_path))
        except Exception as e:
            logger.warning("Failed to save HTML", error=str(e))
        
        return str(screenshot_path)

    async def close(self) -> None:
        try:
            await self.page.close()
        except Exception as e:
            logger.warning("Error closing page", error=str(e))
