"""
Kilocode-specific managed runtime — thin wrapper around shared ManagedAgentRuntime.

Kilocode uses:
- command: "kilocode"
- password env: KILOCODE_SERVER_PASSWORD
- defaults from settings.kilocode
"""

from app.agents.runtime import ManagedAgentRuntime
from app.core.config import settings


class KilocodeAgentRuntime(ManagedAgentRuntime):
    """Managed runtime for Kilocode server with defaults from settings.kilocode."""

    def __init__(self):
        super().__init__(
            command=settings.kilocode.command,
            port=settings.kilocode.port,
            base_url=settings.kilocode.base_url,
            username=settings.kilocode.username,
            password=settings.kilocode.password,
            startup_timeout=settings.kilocode.startup_timeout_seconds,
            healthcheck_interval=settings.kilocode.healthcheck_interval_seconds,
            graceful_shutdown=settings.kilocode.graceful_shutdown_seconds,
            working_dir=settings.kilocode.working_dir,
            extra_env=settings.kilocode.extra_env,
            server_password_env="KILOCODE_SERVER_PASSWORD",
        )
