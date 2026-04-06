"""
AdminObserver — watches ConfigManager events and dispatches to RuntimeConfigApplier.

Runs as a background asyncio task during app lifetime.
"""

import asyncio
import contextlib

from app.admin.applier import RuntimeConfigApplier
from app.admin.config_manager import config_manager
from app.core.logging import get_logger

logger = get_logger(__name__)


class AdminObserver:
    """
    Subscribes to ConfigManager events and applies changes to live components.
    Runs as a background task — started during app startup, cancelled during shutdown.
    """

    def __init__(self, applier: RuntimeConfigApplier | None = None):
        self.applier = applier or RuntimeConfigApplier()
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        """Start the observer background task."""
        if self._running:
            logger.warning("AdminObserver already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._run(), name="admin-observer")
        logger.info("AdminObserver started")

    async def stop(self) -> None:
        """Stop the observer background task."""
        if not self._running:
            return

        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("AdminObserver stopped")

    async def _run(self) -> None:
        """Main event loop — receives events and dispatches to applier."""
        queue = config_manager.subscribe()

        while self._running:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=5.0)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            try:
                await self._handle_event(event)
            except Exception as exc:
                logger.exception(
                    "Error handling config event",
                    event_type=event.type,
                    version=event.version,
                    error=str(exc),
                )

    async def _handle_event(self, event) -> None:
        """Dispatch a single config event to the applier."""
        match event.type:
            case (
                "provider_updated"
                | "model_updated"
                | "routing_updated"
                | "rollback"
                | "override_reset"
            ):
                # Apply current overrides to live system
                result = await self.applier.apply(config_manager.overrides)
                logger.info(
                    "Config apply completed",
                    event_type=event.type,
                    version=event.version,
                    apply_status=result.status,
                    error=result.error,
                )

                # Update override state based on result
                if result.status == "applied":
                    for override in chain_all_overrides(config_manager.overrides):
                        override.state = "applied"
                elif result.status == "apply_failed":
                    for override in chain_all_overrides(config_manager.overrides):
                        override.state = "apply_failed"
                        override.error = result.error


def chain_all_overrides(overrides) -> list:
    """Yield all override objects from a RuntimeOverrides."""
    result = []
    result.extend(overrides.providers.values())
    result.extend(overrides.models.values())
    result.extend(overrides.routing.values())
    return result


observer = AdminObserver()
