from unittest.mock import AsyncMock, MagicMock

import pytest

from app.browser.providers.deepseek import DeepseekProvider


class TestDeepseekProvider:
    def test_recon_hints_include_deepseek_flow(self):
        hints = DeepseekProvider.recon_hints()

        assert hints["provider_id"] == "deepseek"
        assert 'textarea[placeholder="Сообщение для DeepSeek"]' in hints["input"]
        assert ".ds-message .ds-markdown" in hints["assistant_response"]

    def test_chat_home_url_strips_sign_in_suffix(self):
        provider = DeepseekProvider(
            MagicMock(),
            provider_config={"url": "https://chat.deepseek.com/sign_in"},
        )

        assert provider._chat_home_url() == "https://chat.deepseek.com"

    def test_toggle_looks_selected_from_selected_class(self):
        provider = DeepseekProvider(MagicMock())

        assert (
            provider._toggle_looks_selected("ds-toggle-button ds-toggle-button--selected", None)
            is True
        )

    def test_toggle_looks_selected_from_aria_pressed(self):
        provider = DeepseekProvider(MagicMock())

        assert provider._toggle_looks_selected("ds-toggle-button", "true") is True

    def test_toggle_looks_selected_returns_false_for_plain_toggle(self):
        provider = DeepseekProvider(MagicMock())

        assert provider._toggle_looks_selected("ds-toggle-button", None) is False

    @pytest.mark.asyncio
    async def test_extract_assistant_response_ignores_last_user_message(self):
        page = MagicMock()
        markdown_locator = MagicMock()
        markdown_locator.inner_text = AsyncMock(return_value="same text")
        page.locator.return_value.last = markdown_locator

        provider = DeepseekProvider(page)
        provider._last_user_message = "same text"

        assert await provider._extract_assistant_response() == ""

    @pytest.mark.asyncio
    async def test_extract_assistant_response_returns_markdown_text(self):
        page = MagicMock()
        markdown_locator = MagicMock()
        markdown_locator.inner_text = AsyncMock(return_value="DeepSeek answer")
        page.locator.return_value.last = markdown_locator

        provider = DeepseekProvider(page)
        provider._last_user_message = "user prompt"

        assert await provider._extract_assistant_response() == "DeepSeek answer"
