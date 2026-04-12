"""
Generic HTTP client for agent server mode.

Provides a shared abstraction for agent providers that expose
a server-mode HTTP API with the same surface:
- GET  /global/health
- GET  /config/providers
- GET  /provider
- POST /session
- POST /session/{id}/message
- DELETE /session/{id}

Used by OpenCodeClient and KilocodeClient.
"""

from typing import Any

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)


class AgentServerClient:
    """Generic HTTP client for agent server mode API."""

    def __init__(
        self,
        base_url: str,
        username: str = "agent",
        password: str | None = None,
        timeout: int = 120,
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            auth = (self.username, self.password) if self.password else None
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                auth=auth,
                timeout=self.timeout,
                headers={"Content-Type": "application/json"},
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def healthcheck(self) -> dict[str, Any]:
        """Check server health via /global/health."""
        client = self._get_client()
        try:
            response = await client.get("/global/health")
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            logger.warning(
                "Agent server healthcheck failed",
                base_url=self.base_url,
                error=str(exc),
            )
            raise

    async def get_config_providers(self) -> dict[str, Any]:
        """Get configured providers via /config/providers."""
        client = self._get_client()
        response = await client.get("/config/providers")
        response.raise_for_status()
        return response.json()

    async def get_provider_registry(self) -> dict[str, Any]:
        """Get full provider registry via /provider."""
        client = self._get_client()
        response = await client.get("/provider")
        response.raise_for_status()
        return response.json()

    async def create_session(self, title: str | None = None) -> dict[str, Any]:
        """Create a new session via POST /session."""
        client = self._get_client()
        body: dict[str, Any] = {}
        if title:
            body["title"] = title
        response = await client.post("/session", json=body)
        response.raise_for_status()
        return response.json()

    async def send_message(
        self,
        session_id: str,
        prompt: str,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Send a message and wait for response via POST /session/:id/message.

        Args:
            session_id: Agent server session ID
            prompt: User prompt text
            model: Full model reference (e.g., "opencode/big-pickle")
        """
        client = self._get_client()
        body: dict[str, Any] = {
            "parts": [{"type": "text", "text": prompt}],
        }
        if model:
            # Agent server expects model as object: { providerID, modelID }
            parts = model.split("/", 1)
            if len(parts) == 2:
                body["model"] = {
                    "providerID": parts[0],
                    "modelID": parts[1],
                }
            else:
                body["model"] = {
                    "providerID": "",
                    "modelID": model,
                }

        response = await client.post(f"/session/{session_id}/message", json=body)
        response.raise_for_status()
        return response.json()

    async def delete_session(self, session_id: str) -> bool:
        """Delete a session via DELETE /session/:id."""
        try:
            client = self._get_client()
            response = await client.delete(f"/session/{session_id}")
            return response.status_code == 200
        except Exception as exc:
            logger.debug(
                "Failed to delete agent server session",
                session_id=session_id,
                error=str(exc),
            )
            return False

    async def __aenter__(self) -> AgentServerClient:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()
