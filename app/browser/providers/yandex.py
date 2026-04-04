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


class YandexProvider(BrowserProvider):
    """Browser provider for Alice-Yandex (https://alice.yandex.ru/)."""

    provider_id = "yandex"
    model_name = "yandex"
    display_name = "Alice-Yandex"
    target_url = "https://alice.yandex.ru/"

    async def navigate_to_chat(self) -> None:
        logger.info("Navigating to Alice-Yandex", url=self.target_url)
        await self.page.goto(self.target_url)
        await self.page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(3)
        await self._handle_overlays()
        await self._wait_for_ready()

    async def _handle_overlays(self) -> None:
        try:
            allow_all_btn = self.page.get_by_role("button", name="Allow all").first
            if await allow_all_btn.is_visible(timeout=3000):
                await allow_all_btn.click()
                logger.info("Accepted cookies")
                await asyncio.sleep(0.5)
        except PlaywrightTimeout:
            pass
        except Exception as e:
            logger.warning("Cookie banner handling error", error=str(e))

        try:
            accept_essential = self.page.get_by_role("button", name="Allow essential cookies").first
            if await accept_essential.is_visible(timeout=2000):
                await accept_essential.click()
                logger.info("Accepted essential cookies")
                await asyncio.sleep(0.5)
        except PlaywrightTimeout:
            pass
        except Exception as e:
            logger.warning("Essential cookies handling error", error=str(e))

        try:
            login_wall = self.page.get_by_role("button", name="Войти").first
            if await login_wall.is_visible(timeout=2000):
                logger.warning("Alice-Yandex requires login - page behind login wall. Set auth_storage_state_path in .env for authenticated sessions.")
        except PlaywrightTimeout:
            pass
        except Exception as e:
            logger.debug("Login check error", error=str(e))

        try:
            close_buttons = [
                self.page.get_by_role("button", name="Закрыть"),
                self.page.locator('button[class*="close"]'),
                self.page.locator('[class*="modal"] button').first,
            ]
            for btn in close_buttons:
                if await btn.is_visible(timeout=1500):
                    await btn.click()
                    logger.info("Closed overlay")
                    await asyncio.sleep(0.5)
                    break
        except PlaywrightTimeout:
            pass
        except Exception as e:
            logger.debug("Overlay handling error", error=str(e))

    async def _wait_for_ready(self) -> None:
        selectors_to_try = [
            lambda: self.page.locator('textarea[placeholder*="Спросите"]'),
            lambda: self.page.locator('textarea[placeholder*="чём"]'),
            lambda: self.page.locator("textarea.AliceInput-Textarea"),
            lambda: self.page.locator("textarea").first,
        ]

        for selector_fn in selectors_to_try:
            try:
                locator = selector_fn()
                if await locator.is_visible(timeout=5000):
                    logger.debug("Chat ready - input field visible")
                    return
            except Exception:
                continue

        logger.warning("Could not verify ready state")

    async def start_new_chat(self) -> None:
        logger.info("Starting new chat by navigating to home")
        await self.page.goto(self.target_url)
        await self.page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(3)
        await self._handle_overlays()
        await self._wait_for_ready()

    async def send_message(self, text: str) -> None:
        await self._fill_message_input(text)
        await self._press_enter_to_send()

    async def _fill_message_input(self, text: str) -> None:
        selectors_to_try = [
            lambda: self.page.locator('textarea[placeholder*="Спросите"]'),
            lambda: self.page.locator('textarea[placeholder*="чём"]'),
            lambda: self.page.locator("textarea.AliceInput-Textarea"),
            lambda: self.page.locator("textarea").first,
        ]

        input_locator = None
        for i, selector_fn in enumerate(selectors_to_try):
            try:
                locator = selector_fn()
                if await locator.is_visible(timeout=2000):
                    input_locator = locator
                    logger.debug("Found input", selector_index=i)
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

    async def _press_enter_to_send(self) -> None:
        await self.page.keyboard.press("Enter")
        logger.info("Pressed Enter to send message")

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
            stop_button = self.page.get_by_role("button", name="Остановить")
            if await stop_button.is_visible(timeout=1000):
                return True

            generating_indicators = [
                self.page.locator('[class*="loading"]'),
                self.page.locator('[class*="generating"]'),
                self.page.locator('[class*="thinking"]'),
                self.page.locator('[class*="spinner"]'),
                self.page.locator('img[src*="spinner"]'),
            ]
            for indicator in generating_indicators:
                if await indicator.is_visible(timeout=500):
                    return True
            
            body_locator = self.page.locator("body")
            body_text = await body_locator.inner_text()
            
            if "Источники" in body_text:
                return False
            
            if "Выключить звук" in body_text:
                return False
                
        except Exception:
            pass
        return False

    async def _extract_from_page(self) -> str:
        try:
            body_locator = self.page.locator("body")
            body_text = await body_locator.inner_text()
            if body_text:
                idx = body_text.find("Алиса\n")
                if idx >= 0:
                    response = body_text[idx + len("Алиса\n"):]
                    sources_idx = response.find("Источники")
                    if sources_idx > 0:
                        response = response[:sources_idx].strip()
                    
                    if len(response) > 100:
                        logger.debug("Found full response from Alice", length=len(response), preview=response[:100])
                        return response

                idx = body_text.find("Алиса ")
                if idx >= 0:
                    response = body_text[idx:].strip()
                    sources_idx = response.find("Источники")
                    if sources_idx > 0:
                        response = response[:sources_idx].strip()
                    
                    if len(response) > 100:
                        logger.debug("Found response from Alice", length=len(response), preview=response[:100])
                        return response

        except Exception as e:
            logger.debug("Extract error", error=str(e))

        return ""

    async def save_debug_artifacts(self, error_message: str) -> Optional[str]:
        artifacts_dir = Path(settings.artifacts_dir)
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        request_id_part = self._request_id[:8] if self._request_id else "unknown"

        screenshot_path = artifacts_dir / f"yandex_error_{request_id_part}_{timestamp}.png"
        html_path = artifacts_dir / f"yandex_error_{request_id_part}_{timestamp}.html"

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
