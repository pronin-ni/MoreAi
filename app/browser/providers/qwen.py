import asyncio
from typing import TYPE_CHECKING

from playwright.async_api import TimeoutError as PlaywrightTimeout

from app.browser.base import BrowserProvider
from app.browser.capabilities import ProviderCapabilities
from app.browser.page_helpers import first_visible
from app.browser.response_waiter import ResponseWaitConfig, ResponseWaiter
from app.browser.selectors import SelectorDef
from app.core.errors import (
    AssistantMessageNotFoundError,
    GenerationTimeoutError,
    MessageInputNotFoundError,
    SendButtonNotFoundError,
)
from app.core.logging import get_logger

if TYPE_CHECKING:
    from playwright.async_api import Locator

logger = get_logger(__name__)


class QwenProvider(BrowserProvider):
    """Browser provider for Qwen Chat (https://chat.qwen.ai/)."""

    provider_id = "qwen"
    model_name = "qwen"
    display_name = "Qwen Chat"
    target_url = "https://chat.qwen.ai/"

    @classmethod
    def get_capabilities(cls) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id=cls.provider_id,
            model_name=cls.model_name,
            display_name=cls.display_name,
            target_url=cls.target_url,
            send_mechanism="button",
            response_strategy="input_hidden",
            input_selectors_hint=(
                'role=textbox[name="Чем я могу помочь"]',
                'textarea[placeholder*="Чем"]',
            ),
            send_selectors_hint=(
                'button:has(img[src*="send"])',
                'main button:nth(1)',
            ),
            default_stable_threshold=3,
        )

    # -- Navigation --

    async def navigate_to_chat(self) -> None:
        logger.info("Navigating to Qwen Chat", url=self.target_url)
        await self.page.goto(self.target_url)
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
        await self.page.goto(self.target_url)
        await self.page.wait_for_load_state("domcontentloaded")
        await self._handle_cookie_consent()

        try:
            help_textbox = self.page.get_by_role("textbox", name="Чем я могу помочь")
            await help_textbox.wait_for(state="visible", timeout=5000)
            logger.debug("New chat ready - input field visible")
        except PlaywrightTimeout:
            logger.warning("Could not verify new chat state")

    # -- Send --

    async def send_message(self, text: str) -> None:
        await self._fill_message_input(text)
        await self._click_send_button()

    async def _fill_message_input(self, text: str) -> None:
        selectors = [
            SelectorDef.role("textbox", name="Чем я могу помочь", description="role=textbox[Чем]"),
            SelectorDef.css('textarea[placeholder*="Чем"]', description="textarea[Чем]"),
            SelectorDef.raw("textarea", first=True, description="textarea.first"),
            SelectorDef.raw("main textarea", description="main textarea"),
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
                "Message input not found with any selector",
                details={"tried_selectors": len(selectors)},
            )

        await input_locator.fill(text)
        logger.debug("Filled message input", text_length=len(text))

    async def _try_healing_input(self, tried_selectors: list[SelectorDef]) -> Locator | None:
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

    async def _click_send_button(self) -> None:
        selectors = [
            SelectorDef.css('button:has(img[src*="send"])', description="button:has(send_img)"),
            SelectorDef.raw("main button", nth=1, description="main button:nth(1)"),
            SelectorDef.css("button:has(img)", last=True, description="button:has(img).last"),
            SelectorDef.role("button", nth=2, description="role=button:nth(2)"),
        ]

        button_locator = await first_visible(
            self.page, selectors, timeout_ms=2000,
            telemetry_callback=self._record_selector,
        )

        if not button_locator:
            # Self-healing fallback
            button_locator = await self._try_healing_send(selectors)

        if not button_locator:
            raise SendButtonNotFoundError("Send button not found with any selector")

        await button_locator.click()
        logger.info("Clicked send button")

    async def _try_healing_send(self, tried_selectors: list[SelectorDef]) -> Locator | None:
        """Attempt self-healing to find send button."""
        extra = [s.resolve(self.page) for s in tried_selectors]
        try:
            loc = await self.resolve_element(
                "send_button",
                extra_selectors=extra,
                allow_healing=True,
            )
            logger.info(
                "Self-healing found send button",
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
            return await self._extract_assistant_response()

        try:
            return await waiter.wait(
                config=ResponseWaitConfig(
                    timeout=timeout,
                    stable_threshold=3,
                    poll_interval=0.5,
                    min_response_length=1,
                ),
                extract_fn=extract_fn,
            )
        except GenerationTimeoutError:
            raise
        except Exception as exc:
            raise GenerationTimeoutError(
                f"Response generation timed out after {timeout} seconds",
                details={"timeout": timeout, "error": str(exc)},
            )

    async def _extract_assistant_response(self) -> str:
        selectors = [
            SelectorDef.raw("main p:last-of-type", description="main p:last-of-type"),
            SelectorDef.raw("main > div > p", last=True, description="main>div>p.last"),
            SelectorDef.raw('[class*="message"]', last=True, description='[class*="message"].last'),
        ]

        for sel in selectors:
            try:
                loc = sel.resolve(self.page)
                text = await loc.inner_text(timeout=2000)
                if text and text.strip():
                    text = text.strip()
                    # Deduplicate user message
                    try:
                        user_message = self.page.locator("main p").first
                        user_text = await user_message.inner_text(timeout=1000) if user_message else ""
                        if text != user_text:
                            self._record_selector(sel.description or sel.value, True)
                            return text
                    except Exception:
                        self._record_selector(sel.description or sel.value, True)
                        return text
            except Exception:
                self._record_selector(sel.description or sel.value, False)
                continue

        raise AssistantMessageNotFoundError("Assistant message not found")
