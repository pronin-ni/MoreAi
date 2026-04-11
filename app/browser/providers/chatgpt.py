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
    SendButtonNotFoundError,
)
from app.core.logging import get_logger

if TYPE_CHECKING:
    from playwright.async_api import Locator

logger = get_logger(__name__)


class ChatGPTProvider(BrowserProvider):
    """Browser provider for ChatGPT (https://chatgpt.com/)."""

    provider_id = "chatgpt"
    model_name = "chatgpt"
    display_name = "ChatGPT"
    target_url = "https://chatgpt.com/"

    @classmethod
    def get_capabilities(cls) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id=cls.provider_id,
            model_name=cls.model_name,
            display_name=cls.display_name,
            target_url=cls.target_url,
            send_mechanism="button",
            response_strategy="generation_flag",
            input_selectors_hint=(
                'role=textbox[name="Chat with ChatGPT"]',
                'role=textbox',
            ),
            send_selectors_hint=(
                'button[data-testid="send-button"]',
                'button:has(svg.send)',
            ),
            default_stable_threshold=2,
        )

    # -- Navigation --

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

    # -- Send --

    async def send_message(self, text: str) -> None:
        await self._fill_message_input(text)
        await self._click_send_button()

    async def _fill_message_input(self, text: str) -> None:
        selectors = [
            SelectorDef.role("textbox", name="Chat with ChatGPT", description="role=textbox[ChatGPT]"),
            SelectorDef.role("textbox", description="role=textbox"),
            SelectorDef.placeholder("Ask anything", description="placeholder[Ask]"),
            SelectorDef.raw("textarea", first=True, description="textarea.first"),
            SelectorDef.css('textarea[placeholder*="Ask"]', description="textarea[Ask]"),
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

    async def _click_send_button(self) -> None:
        selectors = [
            SelectorDef.css('button[data-testid="send-button"]', description="data-testid=send"),
            SelectorDef.css('button:has(svg[class*="send"])', description="button:has(svg.send)"),
            SelectorDef.css('button:has(svg[class*="paperAirway"])', description="button:has(paperAirway)"),
            SelectorDef.raw("main form button", last=True, description="main form button.last"),
            SelectorDef.raw("main button", last=True, description="main button.last"),
        ]

        button_locator = await first_visible(
            self.page, selectors, timeout_ms=2000,
            telemetry_callback=self._record_selector,
        )

        if not button_locator:
            # Self-healing fallback
            button_locator = await self._try_healing_send(selectors)

        if not button_locator:
            raise SendButtonNotFoundError("Send button not found")

        await button_locator.click()
        logger.info("Clicked send button")

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

    async def _try_healing_send(self, tried_selectors: list[SelectorDef]) -> "Locator | None":
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

            prose_divs = await self.page.locator("main div[tabindex='-1']").all()
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

    async def save_debug_artifacts(self, error_message: str) -> str | None:
        return await save_debug_artifacts(
            self.page, error_message,
            request_id=self._request_id,
            prefix=self.provider_id,
        )
