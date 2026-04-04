import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from app.browser.base import BrowserProvider
from app.core.config import settings
from app.core.logging import get_logger
from app.core.errors import (
    MessageInputNotFoundError,
    SendButtonNotFoundError,
    AssistantMessageNotFoundError,
    GenerationTimeoutError,
)

logger = get_logger(__name__)


class ChatGPTProvider(BrowserProvider):
    """Browser provider for ChatGPT (https://chatgpt.com/)."""

    provider_id = "chatgpt"
    model_name = "chatgpt"
    display_name = "ChatGPT"
    target_url = "https://chatgpt.com/"

    async def navigate_to_chat(self) -> None:
        logger.info("Navigating to ChatGPT", url=self.target_url)
        await self.page.goto(self.target_url)
        await self.page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(3)
        await self._handle_overlays()
        await self._wait_for_ready()

    async def _handle_overlays(self) -> None:
        try:
            accept_button = self.page.get_by_role("button", name="Accept all").first
            if await accept_button.is_visible(timeout=3000):
                await accept_button.click()
                logger.info("Accepted cookies")
                await asyncio.sleep(0.5)
        except PlaywrightTimeout:
            logger.debug("No cookie banner found")
        except Exception as e:
            logger.warning("Cookie banner handling error", error=str(e))

        try:
            login_button = self.page.get_by_role("button", name="Log in").first
            if await login_button.is_visible(timeout=2000):
                logger.info("ChatGPT requires login - page behind login wall")
        except PlaywrightTimeout:
            logger.debug("No login wall detected")
        except Exception as e:
            logger.debug("Login check error", error=str(e))

    async def _wait_for_ready(self) -> None:
        try:
            textbox = self.page.get_by_role("textbox", name="Chat with ChatGPT")
            await textbox.wait_for(state="visible", timeout=10000)
            logger.debug("Chat ready - input field visible")
        except PlaywrightTimeout:
            logger.warning("Chat input not found within timeout")

    async def start_new_chat(self) -> None:
        logger.info("Starting new chat by navigating to home")
        await self.page.goto(self.target_url)
        await self.page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(3)
        await self._handle_overlays()
        await self._wait_for_ready()

    async def send_message(self, text: str) -> None:
        await self._fill_message_input(text)
        await self._click_send_button()

    async def _fill_message_input(self, text: str) -> None:
        selectors_to_try = [
            lambda: self.page.get_by_role("textbox", name="Chat with ChatGPT"),
            lambda: self.page.get_by_role("textbox"),
            lambda: self.page.get_by_placeholder("Ask anything"),
            lambda: self.page.locator("textarea").first,
            lambda: self.page.locator('textarea[placeholder*="Ask"]'),
        ]

        input_locator = None
        for selector_fn in selectors_to_try:
            try:
                locator = selector_fn()
                if await locator.is_visible(timeout=2000):
                    input_locator = locator
                    logger.debug("Found input", selector_type=type(locator).__name__)
                    break
            except Exception:
                continue

        if not input_locator:
            raise MessageInputNotFoundError(
                "Message input not found",
                details={"tried_selectors": len(selectors_to_try)},
            )

        await input_locator.fill(text)
        logger.debug("Filled message input", text_length=len(text))

    async def _click_send_button(self) -> None:
        selectors_to_try = [
            lambda: self.page.locator('button[data-testid="send-button"]'),
            lambda: self.page.locator('button:has(svg[class*="send"]'),
            lambda: self.page.locator('button:has(svg[class*="paperAirway"]'),
            lambda: self.page.locator("main form button").last,
            lambda: self.page.locator("main button").last,
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
            raise SendButtonNotFoundError("Send button not found")

        await button_locator.click()
        logger.info("Clicked send button")

    async def wait_for_response(self, timeout: int = 120) -> str:
        logger.info("Waiting for response generation", timeout=timeout)

        try:
            await self._wait_for_generation_start(timeout=10)
            response_text = await self._wait_for_generation_end(timeout=timeout)

            logger.info("Received response", response_length=len(response_text))
            return response_text

        except PlaywrightTimeout:
            raise GenerationTimeoutError(
                f"Response timed out after {timeout}s",
                details={"timeout": timeout},
            )

    async def _wait_for_generation_start(self, timeout: int = 10) -> None:
        await asyncio.sleep(2)
        logger.debug("Generation started - waited for initial delay")

    async def _wait_for_generation_end(self, timeout: int = 120) -> str:
        start_time = asyncio.get_event_loop().time()
        last_response = ""
        stable_count = 0
        iteration = 0

        logger.info("Starting response wait", timeout=timeout)

        while (asyncio.get_event_loop().time() - start_time) < timeout:
            iteration += 1

            try:
                is_generating = await self._is_still_generating()
                if is_generating:
                    if iteration % 10 == 0:
                        logger.debug("Still generating...", iter=iteration)
                    await asyncio.sleep(1)
                    continue

                response = await self._extract_from_page()

                if response and len(response) > 10:
                    logger.debug("Got response", iter=iteration, length=len(response), preview=response[:50])

                    if response != last_response:
                        last_response = response
                        stable_count = 0
                    elif response == last_response:
                        stable_count += 1
                        if stable_count >= 2:
                            logger.info("Got stable response", length=len(response))
                            return response

                await asyncio.sleep(1.5)

            except Exception as e:
                logger.debug("Loop error", error=str(e))
                await asyncio.sleep(1)

        logger.warning("Timeout, trying final extraction")

        final_attempt = await self._extract_from_page()
        if final_attempt and len(final_attempt) > 10:
            return final_attempt

        raise AssistantMessageNotFoundError(
            f"No response found after {timeout}s, iterations={iteration}"
        )

    async def _is_still_generating(self) -> bool:
        try:
            regenerate_button = self.page.get_by_role("button", name="Regenerate")
            if await regenerate_button.is_visible(timeout=1000):
                return False

            stop_button = self.page.get_by_role("button", name="Stop generating")
            if await stop_button.is_visible(timeout=1000):
                return True
        except Exception:
            pass
        return False

    async def _extract_from_page(self) -> str:
        try:
            assistant_messages = self.page.locator('[data-message-author-role="assistant"]')

            if await assistant_messages.count() > 0:
                last_message = assistant_messages.last
                text_content = await last_message.locator("div").first.inner_text()

                if text_content and len(text_content.strip()) > 20:
                    text = text_content.strip()
                    logger.debug("Found response in assistant message", length=len(text))
                    return text

            prose_divs = self.page.locator("main div[tabindex='-1']").all()
            for div in reversed(prose_divs):
                try:
                    text = await div.inner_text()
                    if text and len(text.strip()) > 30 and not text.strip().startswith("ChatGPT"):
                        logger.debug("Found response in prose div", length=len(text))
                        return text.strip()
                except Exception:
                    continue

            main_content = await self.page.locator("main").inner_text()
            if main_content:
                lines = main_content.split("\n")
                for line in reversed(lines):
                    line = line.strip()
                    if len(line) > 30 and not line.startswith("Send"):
                        logger.debug("Found in main content", preview=line[:50])
                        return line

        except Exception as e:
            logger.debug("Extract error", error=str(e))

        return ""

    async def save_debug_artifacts(self, error_message: str) -> Optional[str]:
        artifacts_dir = Path(settings.artifacts_dir)
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        request_id_part = self._request_id[:8] if self._request_id else "unknown"

        screenshot_path = artifacts_dir / f"chatgpt_error_{request_id_part}_{timestamp}.png"
        html_path = artifacts_dir / f"chatgpt_error_{request_id_part}_{timestamp}.html"

        try:
            await self.page.screenshot(path=str(screenshot_path), full_page=True)
            logger.info("Saved screenshot", path=str(screenshot_path))
        except Exception as e:
            logger.warning("Screenshot failed", error=str(e))

        try:
            html_content = await self.page.content()
            html_path.write_text(html_content, encoding="utf-8")
            logger.info("Saved HTML", path=str(html_path))
        except Exception as e:
            logger.warning("HTML save failed", error=str(e))

        return str(screenshot_path)
