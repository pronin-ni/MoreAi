"""Internal chat browser client — thin wrapper around QwenProvider.

The previous implementation was a near-identical copy of QwenProvider.
Now it simply adapts QwenProvider to the legacy interface so that
existing callers continue to work without changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from playwright.async_api import Page

from app.browser.providers.qwen import QwenProvider
from app.core.config import settings
from app.core.logging import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)


class InternalChatBrowserClient:
    """Legacy-compatible wrapper around QwenProvider for internal chat.

    Internal chat uses the same Qwen Chat UI as the QwenProvider; this
    class simply adapts the interface so existing code paths keep working.
    """

    def __init__(self, page: Page):
        self._qwen = QwenProvider(
            page=page,
            request_id=None,
            provider_config={"url": settings.internal_chat_url},
        )
        self.page = page  # legacy compatibility

    def set_request_id(self, request_id: str) -> None:
        self._qwen.set_request_id(request_id)

    async def navigate_to_chat(self) -> None:
        await self._qwen.navigate_to_chat()

    async def start_new_chat(self) -> None:
        await self._qwen.start_new_chat()

    async def send_message(self, text: str) -> None:
        await self._qwen.send_message(text)

    async def wait_for_response(self, timeout: int = 120) -> str:
        return await self._qwen.wait_for_response(timeout)

    async def save_debug_artifacts(self, error_message: str) -> str | None:
        return await self._qwen.save_debug_artifacts(error_message)

    async def close(self) -> None:
        await self._qwen.close()
