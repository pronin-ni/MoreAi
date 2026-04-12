"""
HTTP client for OpenCode server mode API.

Thin wrapper around the shared AgentServerClient with OpenCode-specific defaults.
"""


from app.agents.server_client import AgentServerClient
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class OpenCodeClient(AgentServerClient):
    """HTTP client for OpenCode server mode with defaults from settings.opencode."""

    def __init__(
        self,
        base_url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: int | None = None,
    ):
        super().__init__(
            base_url=base_url or settings.opencode.base_url,
            username=username or settings.opencode.username,
            password=password or settings.opencode.password,
            timeout=timeout or settings.opencode.timeout_seconds,
        )
