import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeout

from app.browser.base import BrowserProvider
from app.core.config import settings
from app.core.errors import (
    AssistantMessageNotFoundError,
    BrowserError,
    GenerationTimeoutError,
    MessageInputNotFoundError,
    SendButtonNotFoundError,
)
from app.core.logging import get_logger

logger = get_logger(__name__)


class KimiProvider(BrowserProvider):
    """Browser provider for Kimi (https://www.kimi.com/)."""

    provider_id = "kimi"
    model_name = "kimi"
    display_name = "Kimi"
    target_url = "https://www.kimi.com/"
    auth_provider = "google"
    requires_auth = True

    def __init__(self, page: Page, request_id: Optional[str] = None, provider_config: Optional[dict] = None):
        super().__init__(page, request_id=request_id, provider_config=provider_config)
        self._last_user_message = ""

    @classmethod
    def recon_hints(cls) -> dict[str, list[str] | str | bool | None]:
        return {
            "provider_id": cls.provider_id,
            "model_name": cls.model_name,
            "display_name": cls.display_name,
            "target_url": cls.target_url,
            "requires_auth": cls.requires_auth,
            "auth_provider": cls.auth_provider,
            "new_chat": [
                'a.new-chat-btn[href="/?chat_enter_method=new_chat"]',
                "role=link[name='New Chat']",
            ],
            "input": [
                "role=textbox",
                "#chat-box .chat-input-editor",
                ".chat-input-editor",
            ],
            "send": [
                ".send-button-container:not(.disabled)",
                ".send-button-container",
            ],
            "login_wall": [
                "text=Continue with Google",
                "text=Chat with Kimi for Free",
                "text=Phone number",
            ],
        }

    async def navigate_to_chat(self) -> None:
        url = self.provider_config.get("url") or self.target_url
        logger.info("Navigating to Kimi", url=url)
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=15_000)
        except PlaywrightTimeout:
            logger.warning("Kimi navigation timed out; continuing with current DOM", url=url)
        await self._dismiss_promotions()
        await self._wait_for_ready()

    async def start_new_chat(self) -> None:
        new_chat_url = f"{(self.provider_config.get('url') or self.target_url).rstrip('/')}/?chat_enter_method=new_chat"
        logger.info("Resetting Kimi conversation", url=new_chat_url)
        try:
            await self.page.goto(new_chat_url, wait_until="domcontentloaded", timeout=15_000)
        except PlaywrightTimeout:
            logger.warning("Kimi new-chat navigation timed out; continuing with current DOM", url=new_chat_url)
        await self._dismiss_promotions()
        await self._wait_for_ready()

    async def send_message(self, text: str) -> None:
        self._last_user_message = text.strip()
        if await self.detect_login_required():
            raise BrowserError("Kimi requires login before sending a message")

        input_locator = await self._find_message_input()
        if input_locator is None:
            raise MessageInputNotFoundError("Kimi message input not found")

        await self._fill_editor(input_locator, text)
        await self._click_send_button()

        if await self.detect_login_required():
            raise BrowserError(
                "Kimi blocked message sending with a login wall",
                details={"provider_id": self.provider_id, "auth_provider": self.auth_provider},
            )

    async def wait_for_response(self, timeout: int = 120) -> str:
        if await self.detect_login_required():
            raise BrowserError("Kimi response flow is blocked by login")

        logger.info("Waiting for Kimi response", timeout=timeout)
        start_time = asyncio.get_event_loop().time()
        last_text = ""
        stable_seconds = 0.0
        saw_generation_signal = False

        while (asyncio.get_event_loop().time() - start_time) < timeout:
            if await self.detect_login_required():
                raise BrowserError("Kimi response flow was interrupted by a login wall")

            is_generating = await self._is_generating()
            saw_generation_signal = saw_generation_signal or is_generating

            response_text = await self._extract_assistant_response()
            if response_text and response_text != last_text:
                last_text = response_text
                stable_seconds = 0.0
            elif response_text and response_text == last_text:
                stable_seconds += 1.0

            if last_text:
                if saw_generation_signal and not is_generating and stable_seconds >= 2.0:
                    return last_text
                if not saw_generation_signal and stable_seconds >= 3.0:
                    return last_text

            await asyncio.sleep(1)

        final_text = await self._extract_assistant_response()
        if final_text:
            return final_text

        raise GenerationTimeoutError(
            f"Kimi response generation timed out after {timeout} seconds",
            details={"timeout": timeout},
        )

    async def detect_login_required(self) -> bool:
        login_locators = [
            self.page.get_by_text("Continue with Google", exact=False),
            self.page.get_by_text("Chat with Kimi for Free", exact=False),
            self.page.get_by_text("Log in with phone number", exact=False),
            self.page.get_by_role("textbox", name="Phone number"),
        ]
        for locator in login_locators:
            try:
                if await locator.first.is_visible(timeout=500):
                    return True
            except Exception:
                continue
        return False

    async def begin_google_login(self) -> Page:
        logger.info("Opening Kimi login modal")
        await self._ensure_login_modal_open()
        google_button = self.page.get_by_text("Continue with Google", exact=False).first
        try:
            async with self.page.expect_popup(timeout=10_000) as popup_info:
                await google_button.click()
            popup = await popup_info.value
            await popup.wait_for_load_state("domcontentloaded")
            logger.info("Kimi opened Google login popup")
            return popup
        except PlaywrightTimeout:
            await google_button.click()
            await self.page.wait_for_load_state("domcontentloaded")
            logger.info("Kimi reused current page for Google login")
            return self.page

    async def wait_for_authenticated_ready(self) -> None:
        deadline = asyncio.get_event_loop().time() + settings.google_auth.post_login_wait_seconds
        while asyncio.get_event_loop().time() < deadline:
            await self._dismiss_promotions()
            if not await self.detect_login_required():
                input_locator = await self._find_message_input(timeout_ms=1_000)
                if input_locator is not None:
                    return
            await asyncio.sleep(1)

        raise BrowserError("Kimi did not become ready after Google login")

    async def save_debug_artifacts(self, error_message: str) -> Optional[str]:
        artifacts_dir = Path(settings.artifacts_dir)
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        request_id_part = self._request_id[:8] if self._request_id else "unknown"

        screenshot_path = artifacts_dir / f"kimi_error_{request_id_part}_{timestamp}.png"
        html_path = artifacts_dir / f"kimi_error_{request_id_part}_{timestamp}.html"

        try:
            await self.page.screenshot(path=str(screenshot_path), full_page=True)
            logger.info("Saved Kimi screenshot", path=str(screenshot_path), error=error_message)
        except Exception as exc:
            logger.warning("Failed to save Kimi screenshot", error=str(exc))

        try:
            html_content = await self.page.content()
            html_path.write_text(html_content, encoding="utf-8")
            logger.info("Saved Kimi HTML", path=str(html_path), error=error_message)
        except Exception as exc:
            logger.warning("Failed to save Kimi HTML", error=str(exc))

        return str(screenshot_path)

    async def _wait_for_ready(self) -> None:
        input_locator = await self._find_message_input(timeout_ms=10_000)
        if input_locator is None and not await self.detect_login_required():
            raise MessageInputNotFoundError("Kimi did not expose a usable chat input")
        logger.info("Kimi page is ready", login_required=await self.detect_login_required())

    async def _dismiss_promotions(self) -> None:
        close_candidates = [
            self.page.get_by_role("button", name="Close").first,
            self.page.locator(".activity-popup img").first,
            self.page.locator('[class*="close"]').first,
        ]
        for locator in close_candidates:
            try:
                if await locator.is_visible(timeout=500):
                    await locator.click()
                    await asyncio.sleep(0.2)
                    return
            except Exception:
                continue

    async def _ensure_login_modal_open(self) -> None:
        if await self.detect_login_required():
            logger.info("Kimi login modal already visible")
            return

        open_modal_candidates = [
            self.page.get_by_text("Log In", exact=False).first,
            self.page.get_by_text("Log in to sync chat history", exact=False).first,
            self.page.get_by_role("button", name="Log In").first,
        ]
        for locator in open_modal_candidates:
            try:
                if await locator.is_visible(timeout=1_000):
                    logger.info("Clicking Kimi login entrypoint")
                    await locator.click()
                    break
            except Exception:
                continue

        deadline = asyncio.get_event_loop().time() + 10
        while asyncio.get_event_loop().time() < deadline:
            if await self.detect_login_required():
                logger.info("Kimi login modal became visible")
                return
            await asyncio.sleep(0.5)

        raise BrowserError("Kimi login modal did not appear")

    async def _find_message_input(self, timeout_ms: int = 5_000) -> Locator | None:
        candidates = [
            lambda: self.page.get_by_role("textbox").first,
            lambda: self.page.locator("#chat-box .chat-input-editor").first,
            lambda: self.page.locator(".chat-input-editor").first,
            lambda: self.page.locator('[contenteditable="true"]').first,
        ]
        for candidate in candidates:
            try:
                locator = candidate()
                await locator.wait_for(state="visible", timeout=timeout_ms)
                return locator
            except Exception:
                continue
        return None

    async def _fill_editor(self, locator: Locator, text: str) -> None:
        await locator.click(force=True)

        try:
            await locator.fill(text)
            return
        except Exception:
            logger.debug("Kimi editor.fill failed, falling back to keyboard input")

        try:
            await self.page.keyboard.press("Meta+A")
        except Exception:
            pass
        try:
            await self.page.keyboard.press("Control+A")
        except Exception:
            pass
        try:
            await self.page.keyboard.press("Backspace")
        except Exception:
            pass

        await self.page.keyboard.insert_text(text)

    async def _click_send_button(self) -> None:
        candidates = [
            lambda: self.page.locator(".send-button-container:not(.disabled)").first,
            lambda: self.page.locator(".send-button-container").first,
            lambda: self.page.locator('.send-icon').locator("xpath=..").first,
        ]
        for candidate in candidates:
            try:
                locator = candidate()
                await locator.wait_for(state="visible", timeout=3_000)
                class_name = await locator.get_attribute("class") or ""
                if "disabled" in class_name:
                    continue
                await locator.click(force=True)
                return
            except Exception:
                continue
        raise SendButtonNotFoundError("Kimi send button not found or stayed disabled")

    async def _is_generating(self) -> bool:
        explicit_done_candidates = [
            self.page.locator(".send-button-container:not(.disabled)").first,
        ]
        for locator in explicit_done_candidates:
            try:
                if await locator.is_visible(timeout=300):
                    return False
            except Exception:
                continue

        generating_candidates = [
            self.page.get_by_role("button", name="Stop").first,
            self.page.get_by_role("button", name="Stop generating").first,
            self.page.get_by_text("Stop", exact=False).first,
            self.page.locator('[class*="spinner"]').first,
            self.page.locator('[class*="loading"]').first,
            self.page.locator('[class*="generating"]').first,
            self.page.locator('[class*="typing"]').first,
        ]
        for locator in generating_candidates:
            try:
                if await locator.is_visible(timeout=300):
                    return True
            except Exception:
                continue
        return False

    async def _extract_assistant_response(self) -> str:
        selector_candidates = [
            self.page.locator('[data-testid*="assistant"]').last,
            self.page.locator('[data-message-author-role="assistant"]').last,
            self.page.locator('#chat-container [class*="assistant"]').last,
            self.page.locator('#chat-container [class*="message"]').last,
            self.page.locator('#chat-container article').last,
            self.page.locator('#chat-container [class*="markdown"]').last,
            self.page.locator('main article').last,
            self.page.locator('main [class*="markdown"]').last,
        ]

        for locator in selector_candidates:
            text = await self._read_locator_text(locator)
            cleaned = self._clean_response_text(text)
            if cleaned:
                return cleaned

        container_text = await self._read_locator_text(self.page.locator("#chat-container").first)
        return self._clean_response_text(container_text)

    async def _read_locator_text(self, locator: Locator) -> str:
        try:
            if not await locator.is_visible(timeout=500):
                return ""
            return (await locator.inner_text(timeout=1_500)).strip()
        except Exception:
            return ""

    def _clean_response_text(self, text: str) -> str:
        if not text:
            return ""

        chrome_lines = {
            "Ask Anything...",
            "New Chat",
            "Websites",
            "Docs",
            "Slides",
            "Sheets",
            "Deep Research",
            "Agent Swarm",
            "Agent",
            "K2.5 Instant",
            "Log In",
            "Log in to sync chat history",
            "Chat History",
            "Mobile App",
            "About Us",
            "Language",
            "User Feedback",
            "Continue with Google",
            "Log in with phone number",
            "Phone number",
            "Verification code",
            "Send Code",
            "Chat with Kimi for Free",
        }

        pieces = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line in chrome_lines:
                continue
            if self._last_user_message and line == self._last_user_message:
                continue
            if line.lower().startswith("ctrl"):
                continue
            pieces.append(line)

        if not pieces:
            return ""

        if self._last_user_message and self._last_user_message in pieces:
            last_user_index = max(i for i, piece in enumerate(pieces) if piece == self._last_user_message)
            pieces = pieces[last_user_index + 1 :]

        if not pieces:
            return ""

        if len(pieces) == 1:
            candidate = pieces[0]
        else:
            candidate = "\n".join(pieces[-8:])

        candidate = candidate.strip()
        if len(candidate) < 5:
            return ""
        return candidate
