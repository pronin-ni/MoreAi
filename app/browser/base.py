from abc import ABC, abstractmethod
from typing import Any

from playwright.async_api import Locator, Page

from app.browser.capabilities import ProviderCapabilities
from app.browser.debug_artifacts import save_debug_artifacts as _save_debug_artifacts
from app.browser.telemetry import browser_telemetry
from app.core.logging import get_logger

logger = get_logger(__name__)


class BrowserProvider(ABC):
    """Abstract base class for browser automation providers."""

    provider_id: str
    model_name: str
    display_name: str
    target_url: str
    auth_provider: str | None = None
    requires_auth: bool = False

    # Self-healing: shared resolver instance (lazy)
    _resolver: Any = None

    def __init__(
        self,
        page: Page,
        request_id: str | None = None,
        provider_config: dict[str, Any] | None = None,
    ):
        self.page = page
        self._request_id = request_id
        self.provider_config = provider_config or {}
        self._resolver = None

    def set_request_id(self, request_id: str) -> None:
        self._request_id = request_id

    # -- Self-healing element resolution --

    def _get_resolver(self):
        """Lazy-initialize the LocatorResolver for this provider."""
        if self._resolver is None:
            from app.browser.healing.locator_resolver import create_resolver

            self._resolver = create_resolver(
                page=self.page,
                provider_id=self.provider_id,
                healing_enabled=True,
            )
        return self._resolver

    async def resolve_element(
        self,
        role: str,
        *,
        extra_selectors: list[str] | None = None,
        allow_healing: bool = True,
        timeout_ms: int = 2000,
    ) -> Locator:
        """Resolve an element using self-healing fallback chain.

        Order: runtime cache → primary selectors → fallback selectors → healing.

        Parameters
        ----------
        role : semantic role ("message_input", "send_button", …)
        extra_selectors : additional selectors to try before healing
        allow_healing : set False to skip healing
        timeout_ms : per-selector timeout

        Returns
        -------
        Locator for the found element.

        Raises
        ------
        LookupError if no element found after all attempts.
        """
        resolver = self._get_resolver()
        return await resolver.resolve(
            role,
            extra_selectors=extra_selectors,
            allow_healing=allow_healing,
        )

    # -- Telemetry helper --

    def _record_selector(self, selector_name: str, success: bool) -> None:
        """Record a selector attempt for quality telemetry."""
        browser_telemetry.record_selector_attempt(
            provider_id=self.provider_id,
            selector_name=selector_name,
            success=success,
        )

    # -- Shared debug artifacts --

    async def save_debug_artifacts(self, error_message: str) -> str | None:
        """Save screenshot/HTML on error using shared implementation."""
        return await _save_debug_artifacts(
            self.page,
            error_message,
            request_id=self._request_id,
            prefix=self.provider_id,
        )

    # -- Capabilities --

    @classmethod
    def get_capabilities(cls) -> ProviderCapabilities:
        """Return explicit capabilities of this provider.

        Subclasses can override to provide richer metadata.
        The default implementation infers from class attributes.
        """
        return ProviderCapabilities(
            provider_id=cls.provider_id,
            model_name=cls.model_name,
            display_name=cls.display_name,
            target_url=cls.target_url,
            requires_auth=cls.requires_auth,
            auth_mode=cls.auth_provider,
        )

    # -- Abstract methods --

    @abstractmethod
    async def navigate_to_chat(self) -> None:
        """Navigate to the chat homepage."""
        pass

    @abstractmethod
    async def start_new_chat(self) -> None:
        """Start a new conversation / reset context."""
        pass

    @abstractmethod
    async def send_message(self, text: str) -> None:
        """Fill input and send message."""
        pass

    @abstractmethod
    async def wait_for_response(self, timeout: int = 120) -> str:
        """Wait for response and extract text."""
        pass

    # -- Auth hooks --

    async def detect_login_required(self) -> bool:
        """Return True when the provider is blocked by an auth wall."""
        return False

    async def begin_google_login(self) -> Page:
        """Open the Google login flow and return the page that contains it."""
        raise NotImplementedError(f"{self.provider_id} does not support Google auth bootstrap")

    async def wait_for_authenticated_ready(self) -> None:
        """Wait until the provider is ready after auth completes."""
        return None

    async def authenticate_with_credentials(self, credentials: dict[str, str]) -> None:
        """Perform provider-specific auth using credentials from a mounted config file."""
        raise NotImplementedError(
            f"{self.provider_id} does not support credential-file auth bootstrap"
        )

    # -- Recon --

    @classmethod
    def recon_hints(cls) -> dict[str, list[str] | str | bool | None]:
        """Provider-specific recon hints used by the recon utility."""
        caps = cls.get_capabilities()
        hints: dict[str, list[str] | str | bool | None] = {
            "provider_id": caps.provider_id,
            "model_name": caps.model_name,
            "display_name": caps.display_name,
            "target_url": caps.target_url,
            "requires_auth": caps.requires_auth,
            "auth_mode": caps.auth_mode,
        }
        return hints

    # -- Cleanup --

    async def close(self) -> None:
        """Clean up page resources."""
        try:
            await self.page.close()
        except Exception as e:
            logger.warning("Error closing page", error=str(e))
