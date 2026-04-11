"""
MoreAI E2E Regression — API Suite.

Covers key backend endpoints with shape validation.
No browser needed — pure HTTP tests via httpx.
"""

import pytest

from e2e.helpers import expect_api_response_shape

pytestmark = [pytest.mark.regression]


class TestAPIBasics:
    """Core API endpoints respond correctly."""

    def test_root(self, api_client):
        """GET / returns service info."""
        resp = api_client.get("/")
        data = expect_api_response_shape(resp, ["message"])
        assert "version" in data or "MoreAI" in data.get("message", "")

    def test_health(self, api_client):
        """GET /health responds."""
        resp = api_client.get("/health")
        assert resp.status_code == 200


class TestAPIOpenAI:
    """OpenAI-compatible endpoints."""

    def test_v1_models(self, api_client):
        """GET /v1/models returns model list."""
        resp = api_client.get("/v1/models")
        data = expect_api_response_shape(resp, ["data", "object"])
        assert data["object"] == "list"
        assert isinstance(data["data"], list)

    def test_v1_chat_completions_rejects_streaming(self, api_client):
        """Streaming is not supported — returns 400."""
        resp = api_client.post(
            "/v1/chat/completions",
            json={
                "model": "qwen",
                "messages": [{"role": "user", "content": "test"}],
                "stream": True,
            },
        )
        assert resp.status_code == 400


class TestAPIStudio:
    """Studio-specific endpoints."""

    def test_studio_page_renders(self, api_client):
        """GET /studio returns HTML."""
        resp = api_client.get("/studio")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    def test_studio_execution_detail_not_found(self, api_client):
        """GET /studio/executions/nonexistent returns error shape."""
        resp = api_client.get("/studio/executions/nonexistent-12345")
        assert resp.status_code == 200  # returns JSON with error field
        data = resp.json()
        assert "error" in data


class TestAPIAdmin:
    """Admin pipeline and intelligence endpoints."""

    def test_stage_scoring(self, api_client):
        """GET /admin/pipelines/stage-scoring returns scoring data."""
        resp = api_client.get("/admin/pipelines/stage-scoring")
        data = expect_api_response_shape(resp, ["stage_role", "scoring", "total"])
        assert isinstance(data["scoring"], list)

    def test_scoring_trends(self, api_client):
        """GET /admin/pipelines/scoring-trends returns trend data."""
        resp = api_client.get("/admin/pipelines/scoring-trends")
        data = expect_api_response_shape(resp, ["trends", "total"])
        assert isinstance(data["trends"], list)

    def test_stage_quality(self, api_client):
        """GET /admin/pipelines/stage-quality returns quality data."""
        resp = api_client.get("/admin/pipelines/stage-quality")
        data = expect_api_response_shape(resp, ["quality", "total"])
        assert isinstance(data["quality"], list)

    def test_cross_stage_quality(self, api_client):
        """GET /admin/pipelines/stage-quality/cross-stage returns cross-stage data."""
        resp = api_client.get("/admin/pipelines/stage-quality/cross-stage")
        assert resp.status_code == 200

    def test_scoring_history(self, api_client):
        """GET /admin/pipelines/scoring-history returns history data."""
        resp = api_client.get("/admin/pipelines/scoring-history")
        data = expect_api_response_shape(resp, ["history", "total"])
        assert isinstance(data["history"], list)

    def test_scheduler_status(self, api_client):
        """GET /admin/pipelines/scoring-history/scheduler returns scheduler state."""
        resp = api_client.get("/admin/pipelines/scoring-history/scheduler")
        expect_api_response_shape(resp, ["running", "interval_seconds"])

    def test_executions_list(self, api_client):
        """GET /admin/pipelines/executions returns execution list."""
        resp = api_client.get("/admin/pipelines/executions")
        data = expect_api_response_shape(resp, ["executions", "total"])
        assert isinstance(data["executions"], list)

    def test_intelligence_models(self, api_client):
        """GET /admin/intelligence/models returns model intelligence data."""
        resp = api_client.get("/admin/intelligence/models")
        data = expect_api_response_shape(resp, ["models", "total"])
        assert isinstance(data["models"], list)


class TestAPIDiagnostics:
    """Diagnostics endpoints respond."""

    def test_diagnostics_status(self, api_client):
        """GET /diagnostics/status responds."""
        resp = api_client.get("/diagnostics/status")
        assert resp.status_code == 200

    def test_diagnostics_routing(self, api_client):
        """GET /diagnostics/routing responds."""
        resp = api_client.get("/diagnostics/routing")
        assert resp.status_code == 200
