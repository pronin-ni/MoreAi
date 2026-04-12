"""
HTTP client for Kilocode server mode API.

Thin wrapper around the shared AgentServerClient with Kilocode-specific defaults.
"""


from app.agents.server_client import AgentServerClient
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class KilocodeClient(AgentServerClient):
    """HTTP client for Kilocode server mode with defaults from settings.kilocode."""

    def __init__(
        self,
        base_url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: int | None = None,
    ):
        super().__init__(
            base_url=base_url or settings.kilocode.base_url,
            username=username or settings.kilocode.username,
            password=password or settings.kilocode.password,
            timeout=timeout or settings.kilocode.timeout_seconds,
        )
