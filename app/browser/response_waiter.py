"""Shared response-waiting logic for browser providers.

All providers wait for the assistant to finish generating a response.
The pattern is:
  1. Optionally detect "generation started" signal (input hidden, stop button, etc.)
  2. Poll extraction until text is stable for N consecutive checks
  3. Optionally check "still generating" indicators each iteration

This module provides a composable ``ResponseWaiter`` that providers can
configure instead of reimplementing the entire loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.errors import (
    GenerationTimeoutError,
)
from app.core.logging import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)


@dataclass
class ResponseWaitConfig:
    """Configuration for the response-wait loop.

    Attributes
    ----------
    timeout : maximum seconds to wait for a response.
    stable_threshold : consecutive identical readings before considering response complete.
    poll_interval : seconds between extraction polls.
    min_response_length : minimum text length to accept as a valid response.
    """

    timeout: float = 120.0
    stable_threshold: int = 2
    poll_interval: float = 1.0
    min_response_length: int = 5


class ResponseWaiter:
    """Configurable response-wait loop.

    Usage::

        waiter = ResponseWaiter(provider_id="kimi", request_id=request_id)
        text = await waiter.wait(
            config=ResponseWaitConfig(timeout=90, stable_threshold=3),
            extract_fn=my_extract,
            is_generating_fn=my_generation_check,  # optional
            generation_started_fn=my_start_signal,  # optional
        )
    """

    def __init__(
        self,
        *,
        provider_id: str = "unknown",
        request_id: str | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.request_id = request_id
        self._iteration = 0
        self._selector_failures = 0

    async def wait(
        self,
        *,
        config: ResponseWaitConfig | None = None,
        extract_fn: Callable[[], Awaitable[str]],
        is_generating_fn: Callable[[], Awaitable[bool]] | None = None,
        generation_started_fn: Callable[[], Awaitable[bool]] | None = None,
        check_interrupted_fn: Callable[[], Awaitable[bool]] | None = None,
    ) -> str:
        """Run the response-wait loop.

        Parameters
        ----------
        config : wait configuration (defaults applied if ``None``).
        extract_fn : async callable returning current assistant text.
        is_generating_fn : return ``True`` while the model is still generating.
        generation_started_fn : return ``True`` once generation has begun.
        check_interrupted_fn : return ``True`` if the wait should abort
            (e.g. login wall appeared).

        Returns
        -------
        str
            The stable assistant response.

        Raises
        ------
        GenerationTimeoutError
            If *timeout* elapses with no stable response.
        BrowserError
            If *check_interrupted_fn* signals an interruption.
        """
        from app.core.errors import BrowserError

        cfg = config or ResponseWaitConfig()
        start = asyncio.get_event_loop().time()
        last_text = ""
        stable_count = 0

        while (asyncio.get_event_loop().time() - start) < cfg.timeout:
            self._iteration += 1

            # Check for interruption (e.g. login wall)
            if check_interrupted_fn and await check_interrupted_fn():
                if last_text:
                    return last_text
                raise BrowserError(
                    f"{self.provider_id} response wait was interrupted",
                    details={"provider_id": self.provider_id, "iteration": self._iteration},
                )

            # Track generation start
            if generation_started_fn and await generation_started_fn():
                pass  # generation_started signal noted; used by caller via is_generating_fn

            # If still generating, skip stability checks
            if is_generating_fn and await is_generating_fn():
                if self._iteration % 10 == 0:
                    logger.debug(
                        "Still generating",
                        provider_id=self.provider_id,
                        iter=self._iteration,
                    )
                await asyncio.sleep(cfg.poll_interval)
                continue

            # Try to extract response
            try:
                response = await extract_fn()
            except Exception as exc:
                logger.debug(
                    "Response extract failed",
                    provider_id=self.provider_id,
                    error=str(exc),
                )
                response = ""
                self._selector_failures += 1

            if response and len(response) >= cfg.min_response_length:
                if response != last_text:
                    last_text = response
                    stable_count = 0
                elif response == last_text:
                    stable_count += 1
                    if stable_count >= cfg.stable_threshold:
                        logger.info(
                            "Response stable",
                            provider_id=self.provider_id,
                            length=len(response),
                            iterations=self._iteration,
                        )
                        return response

            await asyncio.sleep(cfg.poll_interval)

        # Timeout — return last known text if meaningful
        if last_text and len(last_text) >= cfg.min_response_length:
            logger.warning(
                "Response wait timed out, returning last text",
                provider_id=self.provider_id,
                timeout=cfg.timeout,
                length=len(last_text),
            )
            return last_text

        raise GenerationTimeoutError(
            f"{self.provider_id} response generation timed out after {cfg.timeout} seconds",
            details={
                "provider_id": self.provider_id,
                "timeout": cfg.timeout,
                "iterations": self._iteration,
                "selector_failures": self._selector_failures,
            },
        )

    @property
    def iteration(self) -> int:
        return self._iteration

    @property
    def selector_failures(self) -> int:
        return self._selector_failures
