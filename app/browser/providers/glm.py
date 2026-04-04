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


class GlmProvider(BrowserProvider):
    """Browser provider for Z.ai GLM Chat (https://chat.z.ai/)."""

    provider_id = "glm"
    model_name = "glm"
    display_name = "Z.ai GLM"
    target_url = "https://chat.z.ai/"

    async def navigate_to_chat(self) -> None:
        logger.info("Navigating to Z.ai GLM", url=self.target_url)
        await self.page.goto(self.target_url)
        await self.page.wait_for_load_state("domcontentloaded")
        await self._wait_for_ready()

    async def _wait_for_ready(self) -> None:
        try:
            textbox = self.page.get_by_role("textbox", name="How can I help you today?")
            await textbox.wait_for(state="visible", timeout=10000)
            logger.debug("Chat ready - input field visible")
        except PlaywrightTimeout:
            logger.warning("Chat input not found within timeout")

    async def start_new_chat(self) -> None:
        logger.info("Starting new chat by navigating to home")
        await self.page.goto(self.target_url)
        await self.page.wait_for_load_state("domcontentloaded")
        await self._wait_for_ready()

    async def send_message(self, text: str) -> None:
        await self._fill_message_input(text)
        await self._click_send_button()

    async def _fill_message_input(self, text: str) -> None:
        selectors_to_try = [
            lambda: self.page.get_by_role("textbox", {"name": "How can I help you today?"}),
            lambda: self.page.get_by_role("textbox", {"name": "Send a Message"}),
            lambda: self.page.locator('textarea[placeholder*="help"]'),
            lambda: self.page.locator("#message-input"),
            lambda: self.page.locator("textarea").first,
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
            lambda: self.page.locator("#send-message-button"),
            lambda: self.page.get_by_role("button", name="Send Message"),
            lambda: self.page.locator('button:has(img[src*="send"])'),
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
                html = await self.page.content()
                
                if "Thinking..." in html or "generating" in html.lower():
                    if iteration % 10 == 0:
                        logger.debug("Still thinking...", iter=iteration)
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

        try:
            ss_path = f"/Users/nikitapronin/PycharmProjects/MoreAi/artifacts/debug_{iteration}.png"
            await self.page.screenshot(path=ss_path, full_page=True)
            logger.info("Saved debug screenshot", path=ss_path)
        except:
            pass

        raise AssistantMessageNotFoundError(f"No response found after {timeout}s, iterations={iteration}")

    async def _extract_from_page(self) -> str:
        try:
            ps = await self.page.locator("p").all()
            
            for p in reversed(ps):
                try:
                    text = await p.inner_text()
                    if text and len(text.strip()) > 20:
                        text = text.strip()
                        if not text.startswith("Hi, I'm") and not text.startswith("Interact"):
                            logger.debug("Found response in p", length=len(text), preview=text[:30])
                            return text
                except:
                    continue
            
            body_text = await self.page.locator("body").inner_text()
            if body_text:
                lines = body_text.split('\n')
                for line in reversed(lines):
                    line = line.strip()
                    if len(line) > 30 and not line.startswith("GLM-") and not line.startswith("Share"):
                        logger.debug("Found in body lines", preview=line[:30])
                        return line
                        
        except Exception as e:
            logger.debug("Extract error", error=str(e))
        
        return ""

    async def _extract_assistant_response(self) -> str:
        try:
            page = self.page
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        
        selectors_to_try = [
            lambda: self.page.locator("main").locator("p").last,
            lambda: self.page.locator("main > div").last.locator("p").last,
            lambda: self.page.locator("div[class*='message']").last.locator("p").last,
            lambda: self.page.locator("div").last.locator("p").last,
        ]

        for selector_fn in selectors_to_try:
            try:
                locator = selector_fn()
                text = await locator.inner_text(timeout=3000)
                if text and text.strip() and len(text.strip()) > 5:
                    text = text.strip()
                    if len(text) > 10:
                        logger.debug("Found response", length=len(text))
                        return text
            except Exception as e:
                logger.debug("Selector failed", error=str(e))
                continue

        all_ps = await self.page.locator("main p").all()
        logger.debug("Found paragraphs", count=len(all_ps))
        for i, p in enumerate(all_ps):
            try:
                text = await p.inner_text()
                logger.debug(f"P {i}", text=text[:50])
            except:
                pass

        raise AssistantMessageNotFoundError("Response not found")

    async def save_debug_artifacts(self, error_message: str) -> Optional[str]:
        artifacts_dir = Path(settings.artifacts_dir)
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        request_id_part = self._request_id[:8] if self._request_id else "unknown"

        screenshot_path = artifacts_dir / f"glm_error_{request_id_part}_{timestamp}.png"
        html_path = artifacts_dir / f"glm_error_{request_id_part}_{timestamp}.html"

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
