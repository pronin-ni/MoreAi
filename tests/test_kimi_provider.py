from app.browser.providers.kimi import KimiProvider


class TestKimiProvider:
    def test_recon_hints_include_kimi_specific_targets(self):
        hints = KimiProvider.recon_hints()

        assert hints["provider_id"] == "kimi"
        assert hints["target_url"] == "https://www.kimi.com/"
        assert any("new-chat" in selector.lower() or "chat_enter_method" in selector.lower() for selector in hints["new_chat"])
        assert any("chat-input-editor" in selector for selector in hints["input"])
        assert any("send-button-container" in selector for selector in hints["send"])

    def test_clean_response_text_filters_known_chrome(self):
        provider = KimiProvider(page=None)
        provider._last_user_message = "Привет"

        cleaned = provider._clean_response_text(
            "\n".join(
                [
                    "New Chat",
                    "Ask Anything...",
                    "Привет",
                    "Это ответ Kimi",
                    "с несколькими строками",
                    "Agent",
                ]
            )
        )

        assert cleaned == "Это ответ Kimi\nс несколькими строками"

    def test_clean_response_text_returns_empty_for_only_chrome(self):
        provider = KimiProvider(page=None)

        cleaned = provider._clean_response_text("Ask Anything...\nNew Chat\nLog In")

        assert cleaned == ""
