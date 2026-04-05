import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeout

from app.browser.base import BrowserProvider
from app.core.config import settings
from app.core.errors import (
    BrowserError,
    GenerationTimeoutError,
    MessageInputNotFoundError,
    SendButtonNotFoundError,
)
from app.core.logging import get_logger

logger = get_logger(__name__)


class DeepseekProvider(BrowserProvider):
    provider_id = "deepseek"
    model_name = "deepseek"
    display_name = "Deepseek"
    target_url = "https://chat.deepseek.com/sign_in"
    auth_provider = "credentials"
    requires_auth = True

    def __init__(
        self, page: Page, request_id: Optional[str] = None, provider_config: Optional[dict] = None
    ):
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
                "text=Новый чат",
                '[role="button"]:has-text("Новый чат")',
            ],
            "input": [
                'textarea[placeholder="Сообщение для DeepSeek"]',
                'textarea[placeholder*="DeepSeek"]',
            ],
            "send": [
                '[role="button"][aria-disabled="false"]',
                '.ds-icon-button[role="button"][aria-disabled="false"]',
            ],
            "assistant_response": [
                ".ds-message .ds-markdown",
                "p.ds-markdown-paragraph",
            ],
            "mode_toggles": [
                'button:has-text("Глубокое мышление")',
                'button:has-text("Умный поиск")',
            ],
            "login_wall": [
                "text=Номер телефона / адрес электронной почты",
                'input[type="password"]',
                "text=Войти",
            ],
        }

    async def navigate_to_chat(self) -> None:
        url = self.provider_config.get("url") or self.target_url
        logger.info("Navigating to DeepSeek", url=url)
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        except PlaywrightTimeout:
            logger.warning("DeepSeek navigation timed out; continuing with current DOM", url=url)

        await self._dismiss_cookie_banner()
        if not await self.detect_login_required():
            await self._wait_until_chat_ready(timeout_ms=10_000)

    async def authenticate_with_credentials(self, credentials: dict[str, str]) -> None:
        email = credentials.get("email")
        password = credentials.get("password")
        if not email or not password:
            raise BrowserError("DeepSeek credential-file auth requires email and password")

        await self.page.goto(
            self.provider_config.get("url") or self.target_url, wait_until="domcontentloaded"
        )
        await self._dismiss_cookie_banner()

        email_input = await self._first_visible_locator(
            [
                lambda: (
                    self.page.get_by_role(
                        "textbox", name="Номер телефона / адрес электронной почты"
                    ).first
                ),
                lambda: (
                    self.page.get_by_placeholder("Номер телефона / адрес электронной почты").first
                ),
                lambda: self.page.locator('input[type="text"]').first,
            ],
            timeout_ms=10_000,
        )
        password_input = await self._first_visible_locator(
            [lambda: self.page.locator('input[type="password"]').first],
            timeout_ms=10_000,
        )
        login_button = await self._first_visible_locator(
            [lambda: self.page.get_by_role("button", name="Войти").first],
            timeout_ms=10_000,
        )

        if email_input is None or password_input is None or login_button is None:
            raise BrowserError("DeepSeek login form is not available for credential bootstrap")

        await email_input.fill(email)
        await password_input.fill(password)
        await login_button.click()

    async def wait_for_authenticated_ready(self) -> None:
        deadline = asyncio.get_event_loop().time() + 30
        while asyncio.get_event_loop().time() < deadline:
            await self._dismiss_cookie_banner()
            if not await self.detect_login_required():
                try:
                    await self._wait_until_chat_ready(timeout_ms=1_500)
                    return
                except BrowserError:
                    pass
            await asyncio.sleep(1)

        raise BrowserError("DeepSeek did not become ready after credential login")

    async def start_new_chat(self) -> None:
        if await self.detect_login_required():
            raise BrowserError("DeepSeek is still behind the login wall")

        new_chat = await self._find_new_chat_control(timeout_ms=5_000)
        if new_chat is not None:
            await new_chat.click(force=True)
            await self._wait_until_chat_ready(timeout_ms=10_000)
            return

        try:
            await self.page.goto(
                self._chat_home_url(), wait_until="domcontentloaded", timeout=15_000
            )
        except PlaywrightTimeout:
            logger.warning("DeepSeek home navigation timed out during reset")

        await self._dismiss_cookie_banner()
        await self._wait_until_chat_ready(timeout_ms=10_000)

    async def send_message(self, text: str) -> None:
        if await self.detect_login_required():
            raise BrowserError("DeepSeek requires login before sending a message")

        self._last_user_message = text.strip()
        input_locator = await self._find_message_input(timeout_ms=8_000)
        if input_locator is None:
            raise MessageInputNotFoundError("DeepSeek message input not found")

        await self._disable_optional_modes()
        await input_locator.fill(text)
        try:
            await self._click_send_button()
        except SendButtonNotFoundError:
            await input_locator.press("Enter")

        await self._wait_for_generation_start(timeout=10)

    async def wait_for_response(self, timeout: int = 120) -> str:
        start_time = asyncio.get_event_loop().time()
        last_text = ""
        stable_seconds = 0.0
        saw_explicit_completion_signal = False

        while (asyncio.get_event_loop().time() - start_time) < timeout:
            if await self.detect_login_required():
                raise BrowserError("DeepSeek response flow was interrupted by a login wall")

            response_text = await self._extract_assistant_response()
            if response_text and response_text != last_text:
                last_text = response_text
                stable_seconds = 0.0
            elif response_text and response_text == last_text:
                stable_seconds += 1.0

            input_visible = await self._is_input_visible(timeout_ms=500)
            if last_text and input_visible:
                saw_explicit_completion_signal = True

            if last_text:
                if saw_explicit_completion_signal and stable_seconds >= 2.0:
                    return last_text
                if stable_seconds >= 3.0:
                    return last_text

            await asyncio.sleep(1)

        if last_text:
            return last_text

        raise GenerationTimeoutError(
            f"DeepSeek response generation timed out after {timeout} seconds",
            details={"timeout": timeout},
        )

    async def detect_login_required(self) -> bool:
        if "/sign_in" in self.page.url:
            return True

        if await self._is_input_visible(timeout_ms=500):
            return False

        login_locators = [
            self.page.get_by_placeholder("Номер телефона / адрес электронной почты").first,
            self.page.locator('input[type="password"]').first,
            self.page.get_by_role("button", name="Войти").first,
        ]
        for locator in login_locators:
            try:
                if await locator.is_visible(timeout=500):
                    return True
            except Exception:
                continue
        return False

    async def save_debug_artifacts(self, error_message: str) -> Optional[str]:
        artifacts_dir = Path(settings.artifacts_dir)
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        request_id_part = self._request_id[:8] if self._request_id else "unknown"
        screenshot_path = artifacts_dir / f"deepseek_error_{request_id_part}_{timestamp}.png"
        html_path = artifacts_dir / f"deepseek_error_{request_id_part}_{timestamp}.html"

        try:
            await self.page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception as exc:
            logger.warning("Failed to save DeepSeek screenshot", error=str(exc))

        try:
            html_path.write_text(await self.page.content(), encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to save DeepSeek HTML", error=str(exc))

        logger.info(
            "Saved DeepSeek debug artifacts", screenshot=str(screenshot_path), error=error_message
        )
        return str(screenshot_path)

    async def _dismiss_cookie_banner(self) -> None:
        for button_name in ("Принять все файлы cookie", "Только необходимые файлы cookie"):
            button = self.page.get_by_role("button", name=button_name).first
            try:
                if await button.is_visible(timeout=500):
                    await button.click()
                    await asyncio.sleep(0.2)
                    return
            except Exception:
                continue

    async def _wait_until_chat_ready(self, timeout_ms: int) -> None:
        input_locator = await self._find_message_input(timeout_ms=timeout_ms)
        if input_locator is None:
            raise BrowserError("DeepSeek chat shell did not become ready")

        await self._disable_optional_modes()

    async def _wait_for_generation_start(self, timeout: int) -> None:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if not await self._is_input_visible(timeout_ms=300):
                return

            response_text = await self._extract_assistant_response()
            if response_text:
                return
            await asyncio.sleep(0.5)

    async def _find_new_chat_control(self, timeout_ms: int) -> Locator | None:
        return await self._first_visible_locator(
            [
                lambda: self.page.get_by_text("Новый чат", exact=True).first,
                lambda: self.page.locator('[role="button"]', has_text="Новый чат").first,
            ],
            timeout_ms=timeout_ms,
        )

    async def _find_message_input(self, timeout_ms: int = 5_000) -> Locator | None:
        return await self._first_visible_locator(
            [
                lambda: self.page.get_by_placeholder("Сообщение для DeepSeek").first,
                lambda: self.page.locator('textarea[placeholder="Сообщение для DeepSeek"]').first,
                lambda: self.page.locator('textarea[placeholder*="DeepSeek"]').first,
            ],
            timeout_ms=timeout_ms,
        )

    async def _click_send_button(self) -> None:
        candidates = [
            lambda: self.page.locator('.ds-icon-button[role="button"][aria-disabled="false"]').last,
            lambda: self.page.locator('[role="button"][aria-disabled="false"]').last,
        ]
        for candidate in candidates:
            try:
                locator = candidate()
                await locator.wait_for(state="visible", timeout=2_000)
                await locator.click(force=True)
                return
            except Exception:
                continue
        raise SendButtonNotFoundError("DeepSeek send button not found or stayed disabled")

    async def _disable_optional_modes(self) -> None:
        for label in ("Глубокое мышление", "Умный поиск"):
            toggle = await self._find_mode_toggle(label)
            if toggle is None:
                continue

            if await self._is_toggle_selected(toggle):
                logger.info("Disabling DeepSeek mode before send", mode=label)
                await toggle.click(force=True)
                await asyncio.sleep(0.2)

    async def _find_mode_toggle(self, label: str) -> Locator | None:
        return await self._first_visible_locator(
            [
                lambda: self.page.locator(f'button:has-text("{label}")').first,
                lambda: self.page.get_by_role("button", name=label).first,
                lambda: self.page.locator('[role="button"]', has_text=label).first,
                lambda: self.page.get_by_text(label, exact=True).first,
            ],
            timeout_ms=3_000,
        )

    async def _is_toggle_selected(self, toggle: Locator) -> bool:
        class_name = await toggle.get_attribute("class") or ""
        aria_pressed = await toggle.get_attribute("aria-pressed")
        return self._toggle_looks_selected(class_name, aria_pressed)

    def _toggle_looks_selected(self, class_name: str, aria_pressed: str | None) -> bool:
        return "ds-toggle-button--selected" in class_name or aria_pressed == "true"

    async def _is_input_visible(self, timeout_ms: int) -> bool:
        input_locator = await self._find_message_input(timeout_ms=timeout_ms)
        return input_locator is not None

    async def _extract_assistant_response(self) -> str:
        try:
            assistant_markdown = self.page.locator(".ds-message .ds-markdown").last
            text = (await assistant_markdown.inner_text(timeout=1_500)).strip()
        except Exception:
            return ""

        if not text or text == self._last_user_message:
            return ""
        return text

    async def _first_visible_locator(self, candidates, timeout_ms: int) -> Locator | None:
        for candidate in candidates:
            try:
                locator = candidate()
                await locator.wait_for(state="visible", timeout=timeout_ms)
                return locator
            except Exception:
                continue
        return None

    def _chat_home_url(self) -> str:
        base_url = self.provider_config.get("url") or self.target_url
        return base_url.replace("/sign_in", "")
