"""
Kilocode server mode provider.

Handles managed subprocess lifecycle, external server connection,
model discovery, and prompt completion via Kilocode HTTP API.
"""

from typing import Any

from app.agents.base import AgentProvider
from app.agents.kilocode.client import KilocodeClient
from app.agents.kilocode.discovery import discover_models
from app.agents.kilocode.runtime import KilocodeAgentRuntime
from app.agents.registry import AgentModelDefinition, registry
from app.agents.utils import extract_response_text
from app.core.config import settings
from app.core.errors import ServiceUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)


class KilocodeProvider(AgentProvider):
    """Provider for Kilocode server mode integration."""

    provider_id = "kilocode"
    agent_type = "kilocode_server"

    def __init__(self):
        self._runtime: KilocodeAgentRuntime | None = None
        self._client = KilocodeClient()
        self._models: list[AgentModelDefinition] = []
        self._available = False
        self._error: str | None = None
        self._mode: str = "unknown"  # "managed" or "external"

    async def initialize(self) -> None:
        """Initialize provider: start managed subprocess (if configured) or connect to external, then healthcheck and model discovery."""
        if not settings.kilocode.enabled:
            logger.info("Kilocode provider is disabled by config")
            self._available = False
            self._error = "disabled_by_config"
            self._mode = "external"
            return

        # Determine mode
        if settings.kilocode.managed and settings.kilocode.autostart:
            self._mode = "managed"
            await self._initialize_managed()
        elif settings.kilocode.managed and not settings.kilocode.autostart:
            self._mode = "managed_no_autostart"
            logger.info("Kilocode provider is managed but autostart is disabled")
            self._available = False
            self._error = "managed_autostart_disabled"
        else:
            self._mode = "external"
            await self._initialize_external()

    async def _initialize_managed(self) -> None:
        """Start and manage the Kilocode subprocess."""
        self._runtime = KilocodeAgentRuntime()

        started = await self._runtime.start()
        if not started:
            self._available = False
            self._error = self._runtime._error or "failed_to_start"
            logger.error(
                "Kilocode managed startup failed",
                error=self._error,
            )
            if settings.kilocode.required:
                raise RuntimeError(
                    f"Kilocode provider is required but failed to start: {self._error}"
                )
            return

        # Now run healthcheck + discovery via HTTP client
        try:
            health = await self._client.healthcheck()
            logger.info(
                "Kilocode server healthcheck passed",
                version=health.get("version", "unknown"),
            )
            self._available = True
        except Exception as exc:
            logger.warning(
                "Kilocode server healthcheck failed after managed start",
                error=str(exc),
            )
            self._available = False
            self._error = f"post_start_healthcheck_failed: {exc}"
            if settings.kilocode.required:
                raise RuntimeError(
                    f"Kilocode provider is required but healthcheck failed: {exc}"
                ) from exc
            return

        if settings.kilocode.discovery_enabled:
            try:
                self._models = await discover_models(self._client)
                logger.info(
                    "Kilocode models discovered",
                    model_count=len(self._models),
                )
            except Exception as exc:
                logger.warning(
                    "Kilocode model discovery failed",
                    error=str(exc),
                )
                self._models = []
                self._error = f"discovery_failed: {exc}"
        else:
            logger.info("Kilocode model discovery is disabled")
            self._models = []

        registry.register(self, self._models)

    async def _initialize_external(self) -> None:
        """Connect to an externally managed Kilocode server."""
        try:
            health = await self._client.healthcheck()
            logger.info(
                "Kilocode server healthcheck passed (external)",
                version=health.get("version", "unknown"),
            )
            self._available = True
        except Exception as exc:
            logger.warning(
                "Kilocode external server healthcheck failed",
                error=str(exc),
            )
            self._available = False
            self._error = f"healthcheck_failed: {exc}"
            return

        if settings.kilocode.discovery_enabled:
            try:
                self._models = await discover_models(self._client)
                logger.info(
                    "Kilocode models discovered (external)",
                    model_count=len(self._models),
                )
            except Exception as exc:
                logger.warning(
                    "Kilocode model discovery failed",
                    error=str(exc),
                )
                self._models = []
                self._error = f"discovery_failed: {exc}"
        else:
            logger.info("Kilocode model discovery is disabled")
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
        """Send a prompt via Kilocode server and return the assistant response."""
        if not self._available:
            raise ServiceUnavailableError(
                "Kilocode provider is not available",
                details={"error": self._error},
            )

        # Extract upstream model info from canonical_id: agent/kilocode/<provider_key>/<model_id>
        parts = model.split("/", 3)
        if len(parts) >= 4:
            upstream_provider_key = parts[2]
            upstream_model_id = parts[3]
            # Full model reference for Kilocode: "kilocode/some-model"
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
                    "Failed to create Kilocode session",
                    details={"session_result": session_result},
                )

            logger.info(
                "Sending message to Kilocode",
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
            return extract_response_text(response)

        except ServiceUnavailableError:
            raise
        except Exception as exc:
            raise ServiceUnavailableError(
                "Kilocode completion failed",
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


# Singleton instance
provider = KilocodeProvider()
