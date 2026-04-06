"""Shared page-interaction helpers used by browser providers.

These functions extract the duplicated "try each selector until one is
visible" pattern that every provider implements on its own.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from app.browser.selectors import SelectorDef
from app.core.logging import get_logger

logger = get_logger(__name__)


async def first_visible(
    page: Page,
    selectors: Sequence[SelectorDef],
    *,
    timeout_ms: int = 2000,
    telemetry_callback: Callable[[str, bool], None] | None = None,
) -> Locator | None:
    """Return the first visible locator from *selectors*.

    Parameters
    ----------
    page : Playwright page
    selectors : ordered fallback definitions
    timeout_ms : per-selector visibility timeout
    telemetry_callback : optional ``(selector_description, succeeded)`` hook
                         for quality metrics.

    Returns
    -------
    Locator | None
        The first visible element, or ``None`` when all candidates are hidden.
    """
    for sel in selectors:
        try:
            loc = sel.resolve(page)
            await loc.wait_for(state="visible", timeout=timeout_ms)
            if telemetry_callback:
                telemetry_callback(sel.description or sel.value, True)
            return loc
        except (PlaywrightTimeout, Exception) as exc:
            if telemetry_callback:
                telemetry_callback(sel.description or sel.value, False)
            logger.debug(
                "Selector not visible",
                selector=sel.description or sel.value,
                error=str(exc),
            )
            continue
    return None


async def first_visible_legacy(
    page: Page,
    selector_fns: Sequence[Callable[[], Locator]],
    *,
    timeout_ms: int = 2000,
    telemetry_callback: Callable[[str, bool], None] | None = None,
) -> Locator | None:
    """Legacy variant that accepts the old ``list[lambda]`` pattern.

    Used during incremental migration so providers don't need to switch
    to ``SelectorDef`` all at once.
    """
    for i, fn in enumerate(selector_fns):
        try:
            loc = fn()
            await loc.wait_for(state="visible", timeout=timeout_ms)
            if telemetry_callback:
                telemetry_callback(f"legacy_idx_{i}", True)
            return loc
        except (PlaywrightTimeout, Exception) as exc:
            if telemetry_callback:
                telemetry_callback(f"legacy_idx_{i}", False)
            logger.debug(
                "Legacy selector not visible",
                index=i,
                error=str(exc),
            )
            continue
    return None


async def retry_find(
    page: Page,
    selectors: Sequence[SelectorDef],
    *,
    timeout_ms: int = 2000,
    not_found_error_msg: str = "Element not found",
    not_found_details: dict | None = None,
    telemetry_callback: Callable[[str, bool], None] | None = None,
) -> Locator:
    """Like ``first_visible`` but raises a ``BrowserError`` on total failure.

    Import here to avoid circular imports at module level.
    """
    from app.core.errors import BrowserError

    result = await first_visible(
        page, selectors, timeout_ms=timeout_ms,
        telemetry_callback=telemetry_callback,
    )
    if result is None:
        details = not_found_details or {}
        details["tried_selectors"] = len(selectors)
        raise BrowserError(not_found_error_msg, details=details)
    return result


async def wait_for_stable(
    extract_fn,
    *,
    timeout: float = 120.0,
    stable_threshold: int = 2,
    poll_interval: float = 1.0,
    check_done_fn=None,
    on_progress=None,
) -> str:
    """Generic stability-detection loop.

    Polls *extract_fn* until the returned text is stable for
    *stable_threshold* consecutive checks, or *timeout* elapses.

    Parameters
    ----------
    extract_fn : async callable returning the current text (or empty string).
    timeout : maximum seconds to wait.
    stable_threshold : how many identical consecutive readings count as stable.
    poll_interval : seconds between polls.
    check_done_fn : optional async callable called each iteration; if it
        returns ``True``, the loop returns the current text immediately.
    on_progress : optional async callback(text, stable_count) invoked each
        iteration for telemetry.

    Returns
    -------
    str
        The stable response text.

    Raises
    ------
    BrowserError
        When timeout elapses with no stable response.
    """
    import asyncio

    from app.core.errors import BrowserError

    start = asyncio.get_event_loop().time()
    last_text = ""
    stable_count = 0

    while (asyncio.get_event_loop().time() - start) < timeout:
        if check_done_fn and await check_done_fn() and last_text:
            return last_text

        try:
            response = await extract_fn()
        except Exception:
            response = ""

        if response and response != last_text:
            last_text = response
            stable_count = 0
        elif response and response == last_text:
            stable_count += 1
            if stable_count >= stable_threshold:
                return response

        if on_progress:
            await on_progress(last_text, stable_count)

        await asyncio.sleep(poll_interval)

    if last_text:
        return str(last_text)

    raise BrowserError(
        f"No stable response after {timeout}s",
        details={"timeout": timeout, "last_text_length": len(last_text)},
    )
