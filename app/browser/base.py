from abc import ABC, abstractmethod
from typing import Any, Optional
from playwright.async_api import Page

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

    def __init__(
        self,
        page: Page,
        request_id: Optional[str] = None,
        provider_config: Optional[dict[str, Any]] = None,
    ):
        self.page = page
        self._request_id = request_id
        self.provider_config = provider_config or {}

    def set_request_id(self, request_id: str) -> None:
        self._request_id = request_id

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

    @abstractmethod
    async def save_debug_artifacts(self, error_message: str) -> Optional[str]:
        """Save screenshot/HTML on error."""
        pass

    async def detect_login_required(self) -> bool:
        """Return True when the provider is blocked by an auth wall."""
        return False

    async def begin_google_login(self) -> Page:
        """Open the Google login flow and return the page that contains it."""
        raise NotImplementedError(f"{self.provider_id} does not support Google auth bootstrap")

    async def wait_for_authenticated_ready(self) -> None:
        """Wait until the provider is ready after auth completes."""
        return None

    @classmethod
    def recon_hints(cls) -> dict[str, list[str] | str | bool | None]:
        """Provider-specific recon hints used by the recon utility."""
        return {
            "provider_id": cls.provider_id,
            "model_name": cls.model_name,
            "display_name": cls.display_name,
            "target_url": cls.target_url,
        }

    async def close(self) -> None:
        """Clean up page resources."""
        try:
            await self.page.close()
        except Exception as e:
            logger.warning("Error closing page", error=str(e))
