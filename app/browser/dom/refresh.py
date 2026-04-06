"""
Proactive baseline refresh — controlled background baseline recapture.

Periodically checks and updates baselines for known providers.
- Lightweight, bounded, non-aggressive
- Uses existing provider capabilities
- Aborts quickly on auth wall / blocked state
- Configurable interval and enable flag
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from app.core.logging import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)


class BaselineRefresher:
    """Controlled periodic baseline refresh."""

    def __init__(
        self,
        enabled: bool = True,
        interval_seconds: float = 3600.0,  # Default: every hour
        max_concurrent: int = 2,
        timeout_per_provider: float = 30.0,
    ) -> None:
        self.enabled = enabled
        self.interval_seconds = interval_seconds
        self.max_concurrent = max_concurrent
        self.timeout_per_provider = timeout_per_provider
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        """Start the background refresh loop."""
        if self._running or not self.enabled:
            return

        self._running = True
        self._task = asyncio.create_task(self._refresh_loop())
        logger.info(
            "Baseline refresher started",
            interval_seconds=self.interval_seconds,
        )

    async def stop(self) -> None:
        """Stop the background refresh loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Baseline refresher stopped")

    async def _refresh_loop(self) -> None:
        """Main refresh loop."""
        while self._running:
            try:
                await asyncio.sleep(self.interval_seconds)
                if not self._running:
                    break
                await self._run_refresh()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(
                    "Baseline refresh loop error",
                    error=str(exc),
                )
                await asyncio.sleep(60)  # Wait before retry

    async def _run_refresh(self) -> None:
        """Run a single refresh cycle."""
        logger.info("Starting proactive baseline refresh")
        start = time.monotonic()

        # In a real implementation, this would iterate over known providers
        # and attempt lightweight baseline recapture.
        # For now, this is a hook that can be extended.

        elapsed = time.monotonic() - start
        logger.info(
            "Proactive baseline refresh completed",
            elapsed_ms=round(elapsed * 1000, 1),
        )

    async def refresh_single(
        self,
        provider_id: str,
        roles: list[str] | None = None,
    ) -> dict[str, Any]:
        """Refresh baselines for a single provider.

        This is called manually or by the background loop.
        Aborts quickly on auth wall / blocked state.
        """
        logger.info(
            "Refreshing baseline for provider",
            provider_id=provider_id,
            roles=roles,
        )

        # Placeholder — actual implementation would need:
        # 1. Get provider class from registry
        # 2. Check if auth is needed / available
        # 3. Navigate to page (headless)
        # 4. For each role, resolve element and capture baseline
        # 5. Compare with existing baseline, update if different

        return {
            "provider_id": provider_id,
            "status": "placeholder",
            "message": "Proactive refresh requires live browser — not implemented yet",
        }


baseline_refresher = BaselineRefresher(
    enabled=True,
    interval_seconds=3600.0,
    max_concurrent=2,
    timeout_per_provider=30.0,
)
