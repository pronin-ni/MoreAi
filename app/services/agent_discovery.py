"""
Agent Discovery Service — periodic model re-discovery for agent providers.

Runs in the background and periodically re-discovers models from
all registered agent providers (OpenCode, Kilocode, etc.).

Key properties:
- Does not restart the agent subprocess — only re-runs discovery
- If discovery succeeds: updates model list atomically, logs added/removed
- If discovery fails: preserves last-known-good models (no registry loss)
- Does not block /v1/models or /v1/chat/completions during refresh
- Configurable refresh interval per agent provider type
"""

from __future__ import annotations

import asyncio
import contextlib
import time

from app.agents.registry import registry as agent_registry
from app.core.logging import get_logger

logger = get_logger(__name__)


class AgentDiscoveryService:
    """Periodic model re-discovery for agent providers.

    For each registered agent provider:
    - Managed mode: calls discover_models() and re-registers models
    - External mode: calls discover_models() and re-registers models
    - If discovery fails: previous models are preserved (last-known-good)
    """

    def __init__(self, refresh_interval_seconds: int | None = None) -> None:
        self.refresh_interval = refresh_interval_seconds or 600  # default 10 min
        self._task: asyncio.Task[None] | None = None
        self._running = False

    async def discover_all(self) -> dict[str, dict]:
        """Run discovery for all agent providers once.

        Returns a dict of provider_id -> result status.
        """
        results: dict[str, dict] = {}

        for provider_id, provider in list(agent_registry._providers.items()):
            result = await self._refresh_single_provider(provider_id, provider)
            results[provider_id] = result

        return results

    async def refresh_provider(self, provider_id: str) -> dict:
        """Refresh models for a single agent provider."""
        provider = agent_registry._providers.get(provider_id)
        if not provider:
            return {"status": "not_found", "error": f"Agent provider not found: {provider_id}"}
        return await self._refresh_single_provider(provider_id, provider)

    async def _refresh_single_provider(self, provider_id: str, provider) -> dict:
        """Refresh models for one provider. Preserves last-known-good on failure."""
        start = time.monotonic()
        old_model_ids = {
            m.id for m in agent_registry._models.values()
            if m.provider_id == provider_id
        }

        if not provider._available:
            logger.info(
                "Skipping agent discovery — provider unavailable",
                provider_id=provider_id,
                error=getattr(provider, "_error", "unknown"),
            )
            return {
                "status": "skipped",
                "reason": "provider_unavailable",
                "error": getattr(provider, "_error", "unknown"),
                "existing_model_count": len(old_model_ids),
            }

        try:
            new_models = await provider.discover_models()
        except Exception as exc:
            logger.warning(
                "Agent model discovery failed — keeping last-known-good models",
                provider_id=provider_id,
                error=str(exc),
            )
            return {
                "status": "failed",
                "error": str(exc),
                "existing_model_count": len(old_model_ids),
            }

        # Atomic model update
        new_model_ids = {m["id"] for m in new_models}
        added = sorted(new_model_ids - old_model_ids)
        removed = sorted(old_model_ids - new_model_ids)

        # Update registry atomically
        self._update_provider_models(provider_id, new_models)

        elapsed_ms = (time.monotonic() - start) * 1000

        if added or removed:
            logger.info(
                "Agent model diff after discovery",
                provider_id=provider_id,
                added=str(added),
                removed=str(removed),
                total_models=str(len(new_model_ids)),
                elapsed_ms=str(round(elapsed_ms, 1)),
            )

        return {
            "status": "ok",
            "model_count": len(new_model_ids),
            "added": added,
            "removed": removed,
            "elapsed_ms": round(elapsed_ms, 1),
        }

    def _update_provider_models(self, provider_id: str, new_models: list[dict]) -> None:
        """Atomically replace models for a provider in the agent registry."""
        from app.agents.registry import AgentModelDefinition

        # Remove old models for this provider
        old_ids = {
            mid for mid, m in agent_registry._models.items()
            if m.provider_id == provider_id
        }
        for mid in old_ids:
            agent_registry._models.pop(mid, None)

        # Add new models
        for m in new_models:
            model_def = AgentModelDefinition(
                id=m["id"],
                provider_id=m["provider_id"],
                transport=m.get("transport", "agent"),
                source_type=m.get("source_type", "agent_server"),
                enabled=m.get("enabled", True),
                available=m.get("available", True),
                metadata=m.get("metadata", {}),
            )
            agent_registry._models[model_def.id] = model_def

    def start(self) -> None:
        """Start the periodic discovery background task."""
        if self._task is not None:
            logger.warning("AgentDiscoveryService already running")
            return

        self._running = True
        self._task = asyncio.create_task(
            self._refresh_loop(), name="agent-discovery-refresh"
        )
        logger.info(
            "AgentDiscoveryService started",
            interval_seconds=str(self.refresh_interval),
        )

    async def stop(self) -> None:
        """Stop the periodic discovery background task."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("AgentDiscoveryService stopped")

    async def _refresh_loop(self) -> None:
        """Background loop: sleep → discover → repeat."""
        import random

        while self._running:
            # Sleep with jitter to avoid thundering herd
            jitter = random.uniform(-30, 30)
            sleep_time = max(60, self.refresh_interval + jitter)
            await asyncio.sleep(sleep_time)

            if not self._running:
                break

            try:
                results = await self.discover_all()
                logger.info(
                    "Agent discovery refresh completed",
                    providers=str(list(results.keys())),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "AgentDiscoveryService refresh loop error",
                    error=str(exc),
                )
                # Sleep briefly before retrying
                await asyncio.sleep(60)


# Global singleton
agent_discovery_service = AgentDiscoveryService()
