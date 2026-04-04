import json
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout, async_playwright

from app.browser.registry import registry
from app.core.config import settings
from app.core.errors import BrowserError
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class GoogleCredentials:
    email: str
    password: str
    recovery_email: str | None = None


class GoogleAuthBootstrapper:
    """Reusable Google auth bootstrap for providers that expose a Google login entrypoint."""

    async def ensure_model_authenticated(self, model: str) -> str | None:
        provider_class = registry.get_provider_class(model)
        provider_config = registry.get_provider_config(model)
        storage_state_path = provider_config.get("storage_state_path") or settings.auth_storage_state_path

        if provider_class.auth_provider != "google" or not provider_class.requires_auth:
            return storage_state_path

        if storage_state_path and Path(storage_state_path).exists():
            logger.debug("Auth storage state already exists", model=model, path=storage_state_path)
            return storage_state_path

        if not settings.google_auth.auto_bootstrap:
            raise BrowserError(
                "Google auth is required but auto-bootstrap is disabled",
                details={"model": model, "storage_state_path": storage_state_path},
            )

        credentials = self._load_google_credentials()
        if not storage_state_path:
            raise BrowserError(
                "Google auth bootstrap requires a provider storage_state_path",
                details={"model": model},
            )

        await self._bootstrap_with_google(
            model=model,
            provider_class=provider_class,
            provider_config=provider_config,
            credentials=credentials,
            storage_state_path=storage_state_path,
        )
        return storage_state_path

    def _load_google_credentials(self) -> GoogleCredentials:
        credentials_path = settings.google_auth.credentials_path
        if not credentials_path:
            raise BrowserError("GOOGLE_AUTH_CREDENTIALS_PATH is not configured")

        path = Path(credentials_path)
        if not path.exists():
            raise BrowserError(
                "Google auth credentials file not found",
                details={"credentials_path": str(path)},
            )

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise BrowserError(
                "Google auth credentials file is not valid JSON",
                details={"credentials_path": str(path), "error": str(exc)},
            ) from exc

        google_payload = payload.get("google") if isinstance(payload, dict) else None
        if not isinstance(google_payload, dict):
            raise BrowserError(
                "Google auth credentials file must contain a 'google' object",
                details={"credentials_path": str(path)},
            )

        email = google_payload.get("email")
        password = google_payload.get("password")
        if not email or not password:
            raise BrowserError(
                "Google auth credentials file must include email and password",
                details={"credentials_path": str(path)},
            )

        return GoogleCredentials(
            email=email,
            password=password,
            recovery_email=google_payload.get("recovery_email"),
        )

    async def _bootstrap_with_google(
        self,
        model: str,
        provider_class,
        provider_config: dict,
        credentials: GoogleCredentials,
        storage_state_path: str,
    ) -> None:
        path = Path(storage_state_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        playwright = await async_playwright().start()
        browser = None
        context = None
        page = None
        provider = None

        try:
            browser = await playwright.chromium.launch(
                headless=settings.headless,
                slow_mo=settings.browser_slowmo,
            )
            context = await browser.new_context(
                viewport={"width": 1440, "height": 1100},
                ignore_https_errors=True,
            )
            page = await context.new_page()
            provider = provider_class(page, provider_config=provider_config)

            logger.info("Bootstrapping provider auth via Google", model=model, provider_id=provider_class.provider_id)
            await provider.navigate_to_chat()
            logger.info("Provider page opened; starting Google login", model=model)
            google_page = await provider.begin_google_login()
            logger.info("Google login page acquired", model=model, current_url=google_page.url)
            await self._complete_google_login(google_page, credentials)
            logger.info("Google credentials submitted", model=model)
            await provider.wait_for_authenticated_ready()
            logger.info("Provider became ready after login", model=model)
            await context.storage_state(path=str(path))
            logger.info("Saved provider storage state", model=model, path=str(path))
        except Exception as exc:
            if provider is not None:
                try:
                    await provider.save_debug_artifacts(f"auth bootstrap failed: {exc}")
                except Exception:
                    logger.debug("Failed to save provider auth artifacts", model=model)
            raise
        finally:
            if context is not None:
                await context.close()
            if browser is not None:
                await browser.close()
            await playwright.stop()

    async def _complete_google_login(self, page: Page, credentials: GoogleCredentials) -> None:
        timeout_ms = settings.google_auth.timeout_seconds * 1000
        await self._handle_google_account_chooser(page, credentials.email)
        logger.info("Filling Google email step", current_url=page.url)
        await self._fill_google_email(page, credentials.email, timeout_ms)
        await self._handle_google_account_chooser(page, credentials.email)
        logger.info("Filling Google password step", current_url=page.url)
        await self._fill_google_password(page, credentials.password, timeout_ms)

        if credentials.recovery_email:
            logger.info("Checking Google recovery email step", current_url=page.url)
            await self._fill_google_recovery_email(page, credentials.recovery_email)

        try:
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except PlaywrightTimeout:
            logger.debug("Google auth page did not reach networkidle before returning")

    async def _fill_google_email(self, page: Page, email: str, timeout_ms: int) -> None:
        email_selectors = [
            lambda: page.locator('input[type="email"]'),
            lambda: page.get_by_label("Email or phone"),
            lambda: page.get_by_label("Электронная почта или телефон"),
            lambda: page.get_by_role("textbox", name="Email or phone"),
        ]
        email_input = await self._first_visible_locator(email_selectors, timeout_ms)
        if email_input is None:
            password_input = await self._first_visible_locator(
                [lambda: page.locator('input[type="password"]')],
                timeout_ms=2_000,
            )
            if password_input is not None:
                logger.info("Google email step already completed", current_url=page.url)
                return
            raise BrowserError("Google login email input not found")

        await email_input.fill(email)
        await self._click_google_next(page)

    async def _fill_google_password(self, page: Page, password: str, timeout_ms: int) -> None:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except PlaywrightTimeout:
            logger.debug("Password step did not trigger a full navigation")

        password_selectors = [
            lambda: page.locator('input[type="password"]'),
            lambda: page.get_by_label("Enter your password"),
            lambda: page.get_by_label("Введите пароль"),
        ]
        password_input = await self._first_visible_locator(password_selectors, timeout_ms)
        if password_input is None:
            raise BrowserError("Google login password input not found")

        await password_input.fill(password)
        await self._click_google_next(page)

    async def _fill_google_recovery_email(self, page: Page, recovery_email: str) -> None:
        selectors = [
            lambda: page.locator('input[type="email"]'),
            lambda: page.get_by_label("Recovery email"),
            lambda: page.get_by_label("Резервный адрес электронной почты"),
        ]
        locator = await self._first_visible_locator(selectors, timeout_ms=10_000)
        if locator is None:
            return

        await locator.fill(recovery_email)
        await self._click_google_next(page)

    async def _click_google_next(self, page: Page) -> None:
        next_selectors = [
            lambda: page.get_by_role("button", name="Next"),
            lambda: page.get_by_role("button", name="Далее"),
            lambda: page.locator("#identifierNext button"),
            lambda: page.locator("#passwordNext button"),
        ]
        next_button = await self._first_visible_locator(next_selectors, timeout_ms=10_000)
        if next_button is None:
            raise BrowserError("Google login next button not found")
        await next_button.click()

    async def _handle_google_account_chooser(self, page: Page, email: str) -> None:
        chooser_selectors = [
            lambda: page.get_by_text(email, exact=False).first,
            lambda: page.get_by_role("button", name=email).first,
            lambda: page.get_by_text("Use another account", exact=False).first,
            lambda: page.get_by_text("Использовать другой аккаунт", exact=False).first,
        ]
        for selector_fn in chooser_selectors:
            try:
                locator = selector_fn()
                if await locator.is_visible(timeout=1_000):
                    logger.info("Handling Google account chooser", current_url=page.url)
                    await locator.click()
                    return
            except Exception:
                continue

    async def _first_visible_locator(self, selectors, timeout_ms: int):
        for selector_fn in selectors:
            try:
                locator = selector_fn().first
                await locator.wait_for(state="visible", timeout=timeout_ms)
                return locator
            except Exception:
                continue
        return None


google_auth_bootstrapper = GoogleAuthBootstrapper()
