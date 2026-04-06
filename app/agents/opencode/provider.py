from typing import Any

from app.agents.base import AgentProvider
from app.agents.opencode.client import OpenCodeClient
from app.agents.opencode.discovery import discover_models
from app.agents.opencode.runtime import ManagedAgentRuntime
from app.agents.registry import AgentModelDefinition, registry
from app.core.config import settings
from app.core.errors import ServiceUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)


class OpenCodeProvider(AgentProvider):
    """Provider for OpenCode server mode integration."""

    provider_id = "opencode"
    agent_type = "opencode_server"

    def __init__(self):
        self._runtime: ManagedAgentRuntime | None = None
        self._client = OpenCodeClient()
        self._models: list[AgentModelDefinition] = []
        self._available = False
        self._error: str | None = None
        self._mode: str = "unknown"  # "managed" or "external"

    async def initialize(self) -> None:
        """Initialize provider: start managed subprocess (if configured) or connect to external, then healthcheck and model discovery."""
        if not settings.opencode.enabled:
            logger.info("OpenCode provider is disabled by config")
            self._available = False
            self._error = "disabled_by_config"
            self._mode = "external"
            return

        # Determine mode
        if settings.opencode.managed and settings.opencode.autostart:
            self._mode = "managed"
            await self._initialize_managed()
        elif settings.opencode.managed and not settings.opencode.autostart:
            self._mode = "managed_no_autostart"
            logger.info("OpenCode provider is managed but autostart is disabled")
            self._available = False
            self._error = "managed_autostart_disabled"
        else:
            self._mode = "external"
            await self._initialize_external()

    async def _initialize_managed(self) -> None:
        """Start and manage the OpenCode subprocess."""
        self._runtime = ManagedAgentRuntime()

        started = await self._runtime.start()
        if not started:
            self._available = False
            self._error = self._runtime._error or "failed_to_start"
            logger.error(
                "OpenCode managed startup failed",
                error=self._error,
            )
            if settings.opencode.required:
                raise RuntimeError(
                    f"OpenCode provider is required but failed to start: {self._error}"
                )
            return

        # Now run healthcheck + discovery via HTTP client
        try:
            health = await self._client.healthcheck()
            logger.info(
                "OpenCode server healthcheck passed",
                version=health.get("version", "unknown"),
            )
            self._available = True
        except Exception as exc:
            logger.warning(
                "OpenCode server healthcheck failed after managed start",
                error=str(exc),
            )
            self._available = False
            self._error = f"post_start_healthcheck_failed: {exc}"
            if settings.opencode.required:
                raise RuntimeError(
                    f"OpenCode provider is required but healthcheck failed: {exc}"
                ) from exc
            return

        if settings.opencode.discovery_enabled:
            try:
                self._models = await discover_models(self._client)
                logger.info(
                    "OpenCode models discovered",
                    model_count=len(self._models),
                )
            except Exception as exc:
                logger.warning(
                    "OpenCode model discovery failed",
                    error=str(exc),
                )
                self._models = []
                self._error = f"discovery_failed: {exc}"
        else:
            logger.info("OpenCode model discovery is disabled")
            self._models = []

        registry.register(self, self._models)

    async def _initialize_external(self) -> None:
        """Connect to an externally managed OpenCode server."""
        try:
            health = await self._client.healthcheck()
            logger.info(
                "OpenCode server healthcheck passed (external)",
                version=health.get("version", "unknown"),
            )
            self._available = True
        except Exception as exc:
            logger.warning(
                "OpenCode external server healthcheck failed",
                error=str(exc),
            )
            self._available = False
            self._error = f"healthcheck_failed: {exc}"
            return

        if settings.opencode.discovery_enabled:
            try:
                self._models = await discover_models(self._client)
                logger.info(
                    "OpenCode models discovered (external)",
                    model_count=len(self._models),
                )
            except Exception as exc:
                logger.warning(
                    "OpenCode model discovery failed",
                    error=str(exc),
                )
                self._models = []
                self._error = f"discovery_failed: {exc}"
        else:
            logger.info("OpenCode model discovery is disabled")
            self._models = []

        registry.register(self, self._models)

    async def send_prompt(
        self,
        prompt: str,
        model: str,
        provider_id: str,
        timeout: int | None = None,
        **kwargs: Any,
    ) -> str:
        """Send a prompt via OpenCode server and return the assistant response."""
        if not self._available:
            raise ServiceUnavailableError(
                "OpenCode provider is not available",
                details={"error": self._error},
            )

        # Extract upstream model info from canonical_id: agent/opencode/<provider_key>/<model_id>
        parts = model.split("/", 3)
        if len(parts) >= 4:
            upstream_provider_key = parts[2]
            upstream_model_id = parts[3]
            # Full model reference for OpenCode: "opencode/big-pickle"
            upstream_model = f"{upstream_provider_key}/{upstream_model_id}"
        else:
            # Fallback
            upstream_model = model

        session_id = None
        try:
            # Create a new session (stateless mode)
            session_result = await self._client.create_session(title=f"gateway-{provider_id}")
            session_id = session_result.get("id")

            if not session_id:
                raise ServiceUnavailableError(
                    "Failed to create OpenCode session",
                    details={"session_result": session_result},
                )

            logger.info(
                "Sending message to OpenCode",
                session_id=session_id,
                model=upstream_model,
            )

            # Send the prompt and wait for response
            response = await self._client.send_message(
                session_id=session_id,
                prompt=prompt,
                model=upstream_model,
            )

            # Extract assistant response text from parts
            return self._extract_response_text(response)

        except ServiceUnavailableError:
            raise
        except Exception as exc:
            raise ServiceUnavailableError(
                "OpenCode completion failed",
                details={"model": model, "error": str(exc)},
            ) from exc
        finally:
            # Cleanup session (stateless mode)
            if session_id:
                await self._client.delete_session(session_id)

    async def discover_models(self) -> list[dict]:
        """Return discovered models as dicts."""
        return [
            {
                "id": m.id,
                "provider_id": m.provider_id,
                "transport": m.transport,
                "source_type": m.source_type,
                "enabled": m.enabled,
                "available": m.available,
                **m.metadata,
            }
            for m in self._models
        ]

    async def is_available(self) -> bool:
        """Check if provider is available."""
        return self._available

    async def shutdown(self) -> None:
        """Gracefully shut down the managed subprocess (if running)."""
        if self._runtime is not None:
            await self._runtime.stop()
            self._runtime = None

    def diagnostics(self) -> dict[str, Any]:
        """Return diagnostic information."""
        # Group models by source kind
        models_by_kind: dict[str, list[str]] = {}
        for m in self._models:
            models_by_kind.setdefault(m.source_kind, []).append(m.id)

        result: dict[str, Any] = {
            "provider_id": self.provider_id,
            "agent_type": self.agent_type,
            "mode": self._mode,
            "available": self._available,
            "error": self._error,
            "model_count": len(self._models),
            "models_by_source_kind": models_by_kind,
            "connected_providers": [
                m.discovered_from_provider
                for m in self._models
                if m.provider_connected
            ],
        }

        # Add managed runtime diagnostics
        if self._runtime is not None:
            result["runtime"] = self._runtime.diagnostics()

        result["models"] = [
            {
                "id": m.id,
                "source_kind": m.source_kind,
                "requires_auth": m.requires_auth,
                "provider_connected": m.provider_connected,
                "is_runtime_available": m.is_runtime_available,
            }
            for m in self._models
        ]

        return result

    @staticmethod
    def _extract_response_text(response: dict) -> str:
        """Extract assistant response text from the message response."""
        parts = response.get("parts", [])
        if not parts:
            # Fallback: check for content in message info
            info = response.get("info", {})
            return info.get("content", "")

        text_parts = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type", "")
            if part_type == "text":
                # OpenCode uses "text" field in response parts
                content = part.get("text", "")
                if content:
                    text_parts.append(content)
            elif part_type in ("tool", "tool-use", "tool-result"):
                # Skip tool parts
                continue

        return "\n".join(text_parts).strip()


# Singleton instance
provider = OpenCodeProvider()
