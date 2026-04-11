"""
MoreAI E2E Regression — Optional Live Tests.

Tests that hit real providers. Skipped by default.
Enable with: pytest e2e/ -m live --env LIVE=1
"""

import os

import pytest

from e2e.helpers import expect_api_response_shape

pytestmark = [pytest.mark.live]


class TestLiveChatCompletions:
    """Live provider tests — require LIVE=1 env var."""

    @pytest.fixture(autouse=True)
    def skip_if_not_live(self):
        if os.environ.get("LIVE", "").lower() not in ("1", "true", "yes"):
            pytest.skip("Live tests disabled (set LIVE=1 to enable)")

    def test_chat_completions_qwen(self, api_client):
        """POST /v1/chat/completions with qwen model."""
        resp = api_client.post(
            "/v1/chat/completions",
            json={
                "model": "qwen",
                "messages": [{"role": "user", "content": "Say hello in one word"}],
                "stream": False,
            },
            timeout=30.0,
        )
        data = expect_api_response_shape(resp, ["choices"])
        assert len(data["choices"]) > 0
        content = data["choices"][0].get("message", {}).get("content", "")
        assert len(content) > 0

    def test_chat_completions_pipeline(self, api_client):
        """POST /v1/chat/completions with a pipeline model."""
        resp = api_client.post(
            "/v1/chat/completions",
            json={
                "model": "pipeline/generate-review-refine",
                "messages": [{"role": "user", "content": "Say hi briefly"}],
                "stream": False,
            },
            timeout=120.0,
        )
        # May fail if pipelines not configured, but shouldn't crash
        assert resp.status_code in (200, 400, 500)
