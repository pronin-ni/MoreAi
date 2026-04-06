import asyncio
from typing import TYPE_CHECKING

from playwright.async_api import TimeoutError as PlaywrightTimeout

from app.browser.base import BrowserProvider
from app.browser.capabilities import ProviderCapabilities
from app.browser.debug_artifacts import save_debug_artifacts
from app.browser.page_helpers import first_visible
from app.browser.response_waiter import ResponseWaitConfig, ResponseWaiter
from app.browser.selectors import SelectorDef
from app.core.errors import (
    GenerationTimeoutError,
    MessageInputNotFoundError,
)
from app.core.logging import get_logger

if TYPE_CHECKING:
    from playwright.async_api import Locator

logger = get_logger(__name__)


class YandexProvider(BrowserProvider):
    """Browser provider for Alice-Yandex (https://alice.yandex.ru/)."""

    provider_id = "yandex"
    model_name = "yandex"
    display_name = "Alice-Yandex"
    target_url = "https://alice.yandex.ru/"

    @classmethod
    def get_capabilities(cls) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id=cls.provider_id,
            model_name=cls.model_name,
            display_name=cls.display_name,
            target_url=cls.target_url,
            send_mechanism="enter",
            response_strategy="generation_flag",
            input_selectors_hint=(
                'textarea[placeholder*="Спросите"]',
                'textarea.AliceInput-Textarea',
            ),
            default_stable_threshold=2,
        )

    # -- Navigation --

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
        selectors = [
            SelectorDef.css('textarea[placeholder*="Спросите"]', description="textarea[Спросите]"),
            SelectorDef.css('textarea[placeholder*="чём"]', description="textarea[чём]"),
            SelectorDef.raw("textarea.AliceInput-Textarea", description="AliceInput-Textarea"),
            SelectorDef.raw("textarea", first=True, description="textarea.first"),
        ]

        for sel in selectors:
            try:
                loc = sel.resolve(self.page)
                if await loc.is_visible(timeout=5000):
                    logger.debug("Chat ready - input field visible")
                    self._record_selector(sel.description or sel.value, True)
                    return
            except Exception:
                self._record_selector(sel.description or sel.value, False)
                continue

        logger.warning("Could not verify ready state")

    async def start_new_chat(self) -> None:
        logger.info("Starting new chat by navigating to home")
        await self.page.goto(self.target_url)
        await self.page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(3)
        await self._handle_overlays()
        await self._wait_for_ready()

    # -- Send --

    async def send_message(self, text: str) -> None:
        await self._fill_message_input(text)
        await self._press_enter_to_send()

    async def _fill_message_input(self, text: str) -> None:
        selectors = [
            SelectorDef.css('textarea[placeholder*="Спросите"]', description="textarea[Спросите]"),
            SelectorDef.css('textarea[placeholder*="чём"]', description="textarea[чём]"),
            SelectorDef.raw("textarea.AliceInput-Textarea", description="AliceInput-Textarea"),
            SelectorDef.raw("textarea", first=True, description="textarea.first"),
        ]

        input_locator = await first_visible(
            self.page, selectors, timeout_ms=2000,
            telemetry_callback=self._record_selector,
        )

        if not input_locator:
            # Self-healing fallback
            input_locator = await self._try_healing_input(selectors)

        if not input_locator:
            raise MessageInputNotFoundError(
                "Message input not found",
                details={"tried_selectors": len(selectors)},
            )

        await input_locator.fill(text)
        logger.debug("Filled message input", text_length=len(text))

    async def _press_enter_to_send(self) -> None:
        await self.page.keyboard.press("Enter")
        logger.info("Pressed Enter to send message")

    async def _try_healing_input(self, tried_selectors: list[SelectorDef]) -> "Locator | None":
        """Attempt self-healing to find message input."""
        extra = [s.resolve(self.page) for s in tried_selectors]
        try:
            loc = await self.resolve_element(
                "message_input",
                extra_selectors=extra,
                allow_healing=True,
            )
            logger.info(
                "Self-healing found message input",
                provider_id=self.provider_id,
            )
            return loc
        except LookupError:
            return None

    # -- Response wait --

    async def wait_for_response(self, timeout: int = 120) -> str:
        logger.info("Waiting for response generation", timeout=timeout)

        waiter = ResponseWaiter(
            provider_id=self.provider_id,
            request_id=self._request_id,
        )

        async def extract_fn() -> str:
            return await self._extract_from_page()

        async def is_generating_fn() -> bool:
            return await self._is_still_generating()

        try:
            return await waiter.wait(
                config=ResponseWaitConfig(
                    timeout=timeout,
                    stable_threshold=2,
                    poll_interval=1.5,
                    min_response_length=10,
                ),
                extract_fn=extract_fn,
                is_generating_fn=is_generating_fn,
            )
        except GenerationTimeoutError:
            raise
        except Exception as exc:
            raise GenerationTimeoutError(
                f"Response timed out after {timeout}s",
                details={"timeout": timeout, "error": str(exc)},
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

    async def save_debug_artifacts(self, error_message: str) -> str | None:
        return await save_debug_artifacts(
            self.page, error_message,
            request_id=self._request_id,
            prefix=self.provider_id,
        )
