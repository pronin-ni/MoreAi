"""
Model Discovery Service — periodic refresh and startup discovery.

A thin orchestration layer on top of the existing registry system.
Does NOT rewrite registries — calls existing initialize() / refresh_provider()
methods with per-provider snapshot tracking, diff logging, and background refresh.

Key properties:
- Last-known-good: failed discovery keeps previous models
- Atomic update: leverages existing registry atomic swap
- Non-blocking: refresh runs in background asyncio task
- Per-provider snapshots: each provider tracks its own state
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time

from app.core.config import settings
from app.core.logging import get_logger
from app.integrations.registry import api_registry

logger = get_logger(__name__)


class ProviderSnapshot:
    """Point-in-time state of a single provider's discovery."""

    def __init__(self, provider_id: str, model_ids: list[str]):
        self.provider_id = provider_id
        self.model_ids = sorted(model_ids)
        self.model_count = len(model_ids)
        self.last_updated = time.time()
        self.last_successful_update = time.time()
        self.status = "available" if model_ids else "empty"
        self.last_error: str | None = None

    def mark_failed(self, error: str) -> None:
        """Mark this snapshot as failed — preserves previous model list."""
        self.status = "failed"
        self.last_error = error
        self.last_updated = time.time()
        # Do NOT update last_successful_update or model_ids (last-known-good)

    def to_dict(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "model_count": self.model_count,
            "model_ids": self.model_ids,
            "status": self.status,
            "last_updated": self.last_updated,
            "last_successful_update": self.last_successful_update,
            "last_error": self.last_error,
        }


class ModelDiscoveryService:
    """Orchestrates model discovery and periodic refresh.

    Works on top of the existing registry system:
    - Startup: calls unified_registry.initialize()
    - Periodic: calls api_registry.initialize() (full refresh)
    - Per-provider: calls api_registry.refresh_provider()
    """

    def __init__(self) -> None:
        self._snapshots: dict[str, ProviderSnapshot] = {}
        self._refresh_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._running = False

    # ── Startup Discovery ──

    async def discover_all(self) -> dict:
        """Run initial discovery for all providers.

        If MODEL_DISCOVERY_ON_STARTUP=false, skips discovery.
        Does NOT fail entirely if one provider fails — partial population.
        After discovery, notifies intelligence tracker and fires callbacks.
        """
        if not settings.model_discovery.discovery_on_startup:
            logger.info("Model discovery on startup is disabled — skipping")
            return {"status": "skipped", "reason": "discovery_on_startup=false"}

        logger.info("Starting model discovery for all providers")
        results: dict[str, dict] = {}

        # Discover API providers via existing api_registry.initialize()
        try:
            await api_registry.initialize()
            api_models = api_registry.discovered_models()
            results["api"] = {"status": "ok", "model_count": len(api_models)}

            # Capture per-provider snapshots and notify intelligence tracker
            for status_item in api_registry.get_provider_status():
                pid = status_item.get("integration_id", status_item.get("provider_id", "unknown"))
                model_ids = status_item.get("discovered_models", [])
                if not model_ids and status_item.get("model_count", 0) > 0:
                    model_ids = [
                        m["id"]
                        for m in api_registry.list_models()
                        if m.get("provider_id") == pid
                    ]
                self._snapshots[pid] = ProviderSnapshot(pid, model_ids)

                # Notify intelligence tracker
                from app.intelligence.tracker import model_intelligence_tracker
                model_intelligence_tracker.on_discovery_complete(
                    pid, model_ids, source="api_registry"
                )

        except Exception as exc:
            logger.exception("API provider discovery failed", error=str(exc))
            results["api"] = {"status": "failed", "error": str(exc)}

            # Still capture whatever models were discovered
            for status_item in api_registry.get_provider_status():
                pid = status_item.get("integration_id", "unknown")
                model_ids = status_item.get("discovered_models", [])
                if not model_ids:
                    model_ids = [
                        m["id"]
                        for m in api_registry.list_models()
                        if m.get("provider_id") == pid
                    ]
                snapshot = ProviderSnapshot(pid, model_ids)
                if status_item.get("last_refresh_status") == "failed":
                    snapshot.mark_failed(status_item.get("last_refresh_error", "unknown"))
                self._snapshots[pid] = snapshot

                # Notify intelligence tracker even on partial failure
                from app.intelligence.tracker import model_intelligence_tracker
                model_intelligence_tracker.on_discovery_complete(
                    pid, model_ids, source="api_registry_partial"
                )

        logger.info(
            "Model discovery complete",
            total_providers=str(len(self._snapshots)),
            total_models=str(sum(s.model_count for s in self._snapshots.values())),
        )

        return {"status": "completed", "providers": results}

    # ── Periodic Refresh ──

    async def refresh_all(self) -> dict:
        """Run a full refresh for all API providers.

        Uses the existing api_registry.initialize() which does atomic swap.
        On failure, previous models remain (last-known-good).
        """
        if not self._refresh_lock.locked():
            async with self._refresh_lock:
                return await self._do_refresh_all()
        else:
            logger.info("Refresh already in progress — skipping")
            return {"status": "skipped", "reason": "refresh_in_progress"}

    async def _do_refresh_all(self) -> dict:
        """Internal: perform the actual refresh (caller must hold lock)."""
        start_time = time.monotonic()
        logger.info("Starting periodic model refresh")

        # Capture old model IDs for diff
        old_model_ids = set(api_registry.discovered_models())

        try:
            await api_registry.initialize()
        except Exception as exc:
            logger.warning(
                "Periodic refresh failed — keeping last-known-good models",
                error=str(exc),
            )
            # Update snapshots with failure but keep old model lists
            for status_item in api_registry.get_provider_status():
                pid = status_item.get("integration_id", "unknown")
                if pid in self._snapshots:
                    self._snapshots[pid].mark_failed(
                        status_item.get("last_refresh_error", str(exc))
                    )
                else:
                    snapshot = ProviderSnapshot(pid, [])
                    snapshot.mark_failed(str(exc))
                    self._snapshots[pid] = snapshot

            elapsed_ms = (time.monotonic() - start_time) * 1000
            return {"status": "failed", "error": str(exc), "elapsed_ms": round(elapsed_ms, 1)}

        # Capture new model IDs for diff
        new_model_ids = set(api_registry.discovered_models())
        added = sorted(new_model_ids - old_model_ids)
        removed = sorted(old_model_ids - new_model_ids)

        # Update per-provider snapshots and notify intelligence tracker
        from app.intelligence.tracker import model_intelligence_tracker
        for status_item in api_registry.get_provider_status():
            pid = status_item.get("integration_id", "unknown")
            model_ids = status_item.get("discovered_models", [])
            if not model_ids:
                model_ids = [
                    m["id"]
                    for m in api_registry.list_models()
                    if m.get("provider_id") == pid
                ]
            self._snapshots[pid] = ProviderSnapshot(pid, model_ids)

            # Notify intelligence tracker of refreshed models
            model_intelligence_tracker.on_discovery_complete(
                pid, model_ids, source="periodic_refresh"
            )

        elapsed_ms = (time.monotonic() - start_time) * 1000

        if added or removed:
            logger.info(
                "Model diff after refresh",
                added=str(added),
                removed=str(removed),
                total_models=str(len(new_model_ids)),
            )

        logger.info(
            "Periodic refresh complete",
            total_models=str(len(new_model_ids)),
            added=str(len(added)),
            removed=str(len(removed)),
            elapsed_ms=str(round(elapsed_ms, 1)),
        )

        return {
            "status": "ok",
            "total_models": len(new_model_ids),
            "added": added,
            "removed": removed,
            "elapsed_ms": round(elapsed_ms, 1),
        }

    # ── Per-Provider Refresh ──

    async def refresh_provider(self, provider_id: str) -> dict:
        """Refresh models for a single provider.

        Uses api_registry.refresh_provider() for atomic merge.
        On failure, previous models for this provider remain (last-known-good).
        Notifies intelligence tracker of model changes.
        """
        result = await api_registry.refresh_provider(provider_id)

        # Update snapshot and notify intelligence tracker
        from app.intelligence.tracker import model_intelligence_tracker
        if result["status"] == "ok":
            model_ids = [
                m["id"]
                for m in api_registry.list_models()
                if m.get("provider_id") == provider_id
            ]
            self._snapshots[provider_id] = ProviderSnapshot(provider_id, model_ids)

            # Notify intelligence tracker
            model_intelligence_tracker.on_discovery_complete(
                provider_id, model_ids, source="provider_refresh"
            )
        elif result["status"] == "failed":
            if provider_id in self._snapshots:
                self._snapshots[provider_id].mark_failed(result.get("error", "unknown"))
            else:
                snapshot = ProviderSnapshot(provider_id, [])
                snapshot.mark_failed(result.get("error", "unknown"))
                self._snapshots[provider_id] = snapshot

        return result

    # ── Status ──

    def get_status(self) -> list[dict]:
        """Return per-provider discovery status."""
        result: list[dict] = []

        for _pid, snapshot in sorted(self._snapshots.items()):
            result.append(snapshot.to_dict())

        # Add providers that exist in registry but not in snapshots
        for status_item in api_registry.get_provider_status():
            pid = status_item.get("integration_id", "unknown")
            if pid not in self._snapshots:
                model_ids = status_item.get("discovered_models", [])
                if not model_ids:
                    model_ids = [
                        m["id"]
                        for m in api_registry.list_models()
                        if m.get("provider_id") == pid
                    ]
                snapshot = ProviderSnapshot(pid, model_ids)
                if status_item.get("last_refresh_status") == "failed":
                    snapshot.mark_failed(status_item.get("last_refresh_error", "unknown"))
                self._snapshots[pid] = snapshot
                result.append(snapshot.to_dict())

        return result

    # ── Background Task ──

    def start(self) -> None:
        """Start the periodic refresh background task."""
        if self._task is not None:
            logger.warning("ModelDiscoveryService already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._refresh_loop(), name="model-discovery-refresh")
        logger.info(
            "ModelDiscoveryService started",
            interval_seconds=str(settings.model_discovery.refresh_interval_seconds),
        )

    async def stop(self) -> None:
        """Stop the periodic refresh background task."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("ModelDiscoveryService stopped")

    async def _refresh_loop(self) -> None:
        """Background loop: sleep → refresh → repeat."""
        interval = settings.model_discovery.refresh_interval_seconds
        jitter = settings.model_discovery.refresh_jitter_seconds

        while self._running:
            # Sleep with jitter to avoid thundering herd
            sleep_time = interval + random.uniform(-jitter, jitter)
            sleep_time = max(10, sleep_time)  # Minimum 10 seconds
            await asyncio.sleep(sleep_time)

            if not self._running:
                break

            try:
                await self.refresh_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "ModelDiscoveryService refresh loop error",
                    error=str(exc),
                )
                # Sleep briefly before retrying to avoid tight error loop
                await asyncio.sleep(60)


# ── Singleton ──

model_discovery_service = ModelDiscoveryService()
