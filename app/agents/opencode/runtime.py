"""
OpenCode-specific managed runtime — thin wrapper around shared ManagedAgentRuntime.

OpenCode uses:
- command: "opencode"
- password env: OPENCODE_SERVER_PASSWORD
- defaults from settings.opencode
"""

from app.agents.runtime import ManagedAgentRuntime
from app.core.config import settings


class OpenCodeAgentRuntime(ManagedAgentRuntime):
    """Managed runtime for OpenCode server with defaults from settings.opencode."""

    def __init__(self):
        super().__init__(
            command=settings.opencode.command,
            port=settings.opencode.port,
            base_url=settings.opencode.base_url,
            username=settings.opencode.username,
            password=settings.opencode.password,
            startup_timeout=settings.opencode.startup_timeout_seconds,
            healthcheck_interval=settings.opencode.healthcheck_interval_seconds,
            graceful_shutdown=settings.opencode.graceful_shutdown_seconds,
            working_dir=settings.opencode.working_dir,
            extra_env=settings.opencode.extra_env,
            server_password_env="OPENCODE_SERVER_PASSWORD",
        )
