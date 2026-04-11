
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


class GlmProvider(BrowserProvider):
    """Browser provider for Z.ai GLM Chat (https://chat.z.ai/)."""

    provider_id = "glm"
    model_name = "glm"
    display_name = "Z.ai GLM"
    target_url = "https://chat.z.ai/"

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
                'role=textbox[name="How can I help you today?"]',
                '#message-input',
            ),
            send_selectors_hint=(
                '#send-message-button',
                'role=button[name="Send Message"]',
            ),
            default_stable_threshold=2,
        )

    # -- Navigation --

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

    # -- Send --

    async def send_message(self, text: str) -> None:
        await self._fill_message_input(text)
        await self._click_send_button()

    async def _fill_message_input(self, text: str) -> None:
        selectors = [
            SelectorDef.role("textbox", name="How can I help you today?", description="role=textbox[help]"),
            SelectorDef.role("textbox", name="Send a Message", description="role=textbox[Send]"),
            SelectorDef.css('textarea[placeholder*="help"]', description="textarea[help]"),
            SelectorDef.raw("#message-input", description="#message-input"),
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

    async def _click_send_button(self) -> None:
        selectors = [
            SelectorDef.raw("#send-message-button", description="#send-message-button"),
            SelectorDef.role("button", name="Send Message", description="role=button[Send]"),
            SelectorDef.css('button:has(img[src*="send"])', description="button:has(send_img)"),
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
            return await self._is_still_thinking()

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

    async def _is_still_thinking(self) -> bool:
        try:
            html = await self.page.content()
            return "Thinking..." in html or "generating" in html.lower()
        except Exception:
            return False

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
                except Exception:
                    continue

            body_text = await self.page.locator("body").inner_text()
            if body_text:
                lines = body_text.split("\n")
                for line in reversed(lines):
                    line = line.strip()
                    if len(line) > 30 and not line.startswith("GLM-") and not line.startswith("Share"):
                        logger.debug("Found in body lines", preview=line[:30])
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
