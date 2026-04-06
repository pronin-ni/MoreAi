from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.schemas.openai import ChatCompletionResponse, Choice, Message, Usage


class TestUIIndex:
    def test_ui_index_returns_html(self):
        from app.main import app

        with (
            patch("app.main.browser_dispatcher.initialize", new=AsyncMock()),
            patch("app.main.browser_dispatcher.shutdown", new=AsyncMock()),
            patch("app.main.unified_registry.initialize", new=AsyncMock()),
        ):
            client = TestClient(app)
            response = client.get("/ui")

            assert response.status_code == 200
            assert "text/html" in response.headers["content-type"]
            assert "MoreAI" in response.text

    def test_ui_index_contains_models_panel(self):
        from app.main import app

        with (
            patch("app.main.browser_dispatcher.initialize", new=AsyncMock()),
            patch("app.main.browser_dispatcher.shutdown", new=AsyncMock()),
            patch("app.main.unified_registry.initialize", new=AsyncMock()),
        ):
            client = TestClient(app)
            response = client.get("/ui")

            assert "models-panel" in response.text
            assert "chat-messages" in response.text
            assert "diagnostics-panel" in response.text

    def test_ui_index_prefills_selected_model(self):
        from app.main import app

        with (
            patch("app.main.browser_dispatcher.initialize", new=AsyncMock()),
            patch("app.main.browser_dispatcher.shutdown", new=AsyncMock()),
            patch("app.main.unified_registry.initialize", new=AsyncMock()),
        ):
            client = TestClient(app)
            response = client.get("/ui")

            assert 'input type="hidden" name="model" value="' in response.text
            assert 'input type="hidden" name="model" value=""' not in response.text


class TestUIModels:
    def test_ui_models_returns_partial(self):
        from app.main import app

        with (
            patch("app.main.browser_dispatcher.initialize", new=AsyncMock()),
            patch("app.main.browser_dispatcher.shutdown", new=AsyncMock()),
            patch("app.main.unified_registry.initialize", new=AsyncMock()),
        ):
            client = TestClient(app)
            response = client.get("/ui/models")

            assert response.status_code == 200
            assert "text/html" in response.headers["content-type"]

    def test_ui_models_with_search_query(self):
        from app.main import app

        with (
            patch("app.main.browser_dispatcher.initialize", new=AsyncMock()),
            patch("app.main.browser_dispatcher.shutdown", new=AsyncMock()),
            patch("app.main.unified_registry.initialize", new=AsyncMock()),
        ):
            client = TestClient(app)
            response = client.get("/ui/models?q=qwen")

            assert response.status_code == 200


class TestUIChat:
    def test_ui_chat_clear_action(self):
        from app.main import app

        with (
            patch("app.main.browser_dispatcher.initialize", new=AsyncMock()),
            patch("app.main.browser_dispatcher.shutdown", new=AsyncMock()),
            patch("app.main.unified_registry.initialize", new=AsyncMock()),
        ):
            client = TestClient(app)
            response = client.post(
                "/ui/chat",
                data={"model": "browser/qwen", "action": "clear"},
            )

            assert response.status_code == 200
            assert "text/html" in response.headers["content-type"]
            assert 'id="conversation-json-input"' in response.text
            assert 'data-chat-status="cleared"' in response.text

    def test_ui_chat_requires_model_and_message(self):
        from app.main import app

        with (
            patch("app.main.browser_dispatcher.initialize", new=AsyncMock()),
            patch("app.main.browser_dispatcher.shutdown", new=AsyncMock()),
            patch("app.main.unified_registry.initialize", new=AsyncMock()),
        ):
            client = TestClient(app)
            response = client.post(
                "/ui/chat",
                data={"model": "", "message": ""},
            )

            assert response.status_code == 200
            assert "error" in response.text.lower()

    def test_ui_chat_success_updates_conversation_state_without_duplicate_response_block(self):
        from app.main import app

        mocked_response = ChatCompletionResponse(
            id="chatcmpl-ui",
            created=1,
            model="browser/qwen",
            choices=[
                Choice(
                    index=0,
                    message=Message(role="assistant", content="**Hello** from UI"),
                    finish_reason="stop",
                )
            ],
            usage=Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3),
        )

        with (
            patch("app.main.browser_dispatcher.initialize", new=AsyncMock()),
            patch("app.main.browser_dispatcher.shutdown", new=AsyncMock()),
            patch("app.main.unified_registry.initialize", new=AsyncMock()),
            patch(
                "app.api.routes_ui.service.process_completion",
                new=AsyncMock(return_value=mocked_response),
            ),
        ):
            client = TestClient(app)
            response = client.post(
                "/ui/chat",
                data={
                    "model": "browser/qwen",
                    "message": "Hi",
                    "conversation_json": "[]",
                },
            )

            assert response.status_code == 200
            assert 'id="conversation-json-input"' in response.text
            assert 'data-chat-status="success"' in response.text
            assert 'id="last-response"' not in response.text
            assert "<strong>Hello</strong> from UI" in response.text


class TestUIDiagnostics:
    def test_ui_diagnostics_empty_model(self):
        from app.main import app

        with (
            patch("app.main.browser_dispatcher.initialize", new=AsyncMock()),
            patch("app.main.browser_dispatcher.shutdown", new=AsyncMock()),
            patch("app.main.unified_registry.initialize", new=AsyncMock()),
        ):
            client = TestClient(app)
            response = client.get("/ui/diagnostics")

            assert response.status_code == 200
            assert "text/html" in response.headers["content-type"]

    def test_ui_diagnostics_with_model(self):
        from app.main import app

        with (
            patch("app.main.browser_dispatcher.initialize", new=AsyncMock()),
            patch("app.main.browser_dispatcher.shutdown", new=AsyncMock()),
            patch("app.main.unified_registry.initialize", new=AsyncMock()),
        ):
            client = TestClient(app)
            response = client.get("/ui/diagnostics?model=browser/qwen")

            assert response.status_code == 200
            assert "browser" in response.text


class TestStaticFiles:
    def test_static_css_available(self):
        from app.main import app

        with (
            patch("app.main.browser_dispatcher.initialize", new=AsyncMock()),
            patch("app.main.browser_dispatcher.shutdown", new=AsyncMock()),
            patch("app.main.unified_registry.initialize", new=AsyncMock()),
        ):
            client = TestClient(app)
            response = client.get("/static/css/style.css")

            assert response.status_code == 200

    def test_static_js_available(self):
        from app.main import app

        with (
            patch("app.main.browser_dispatcher.initialize", new=AsyncMock()),
            patch("app.main.browser_dispatcher.shutdown", new=AsyncMock()),
            patch("app.main.unified_registry.initialize", new=AsyncMock()),
        ):
            client = TestClient(app)
            response = client.get("/static/js/app.js")

            assert response.status_code == 200

    def test_htmx_available(self):
        from app.main import app

        with (
            patch("app.main.browser_dispatcher.initialize", new=AsyncMock()),
            patch("app.main.browser_dispatcher.shutdown", new=AsyncMock()),
            patch("app.main.unified_registry.initialize", new=AsyncMock()),
        ):
            client = TestClient(app)
            response = client.get("/static/vendor/htmx.min.js")

            assert response.status_code == 200
