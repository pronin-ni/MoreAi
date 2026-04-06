from abc import ABC, abstractmethod
from typing import Any


class AgentProvider(ABC):
    """Abstract base class for agent-based providers (e.g., OpenCode server mode)."""

    provider_id: str = "agent"
    model_name: str = ""
    display_name: str = ""
    agent_type: str = ""  # e.g., "opencode_server"

    @abstractmethod
    async def initialize(self) -> None:
        """Initialize the provider (healthcheck, discovery, etc.)."""
        ...

    @abstractmethod
    async def send_prompt(
        self,
        prompt: str,
        model: str,
        provider_id: str,
        timeout: int | None = None,
        **kwargs: Any,
    ) -> str:
        """Send a prompt and return the assistant response text."""
        ...

    @abstractmethod
    async def discover_models(self) -> list[dict]:
        """Discover available models from the agent server."""
        ...

    @abstractmethod
    async def is_available(self) -> bool:
        """Check if the agent server is available."""
        ...

    def diagnostics(self) -> dict[str, Any]:
        """Return diagnostic information about the provider."""
        return {
            "provider_id": self.provider_id,
            "agent_type": self.agent_type,
            "available": False,
        }
