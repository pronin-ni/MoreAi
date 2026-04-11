"""Shared debug-artifact saving for browser providers.

Every provider previously implemented its own ``save_debug_artifacts`` with
nearly identical logic.  This module centralises that behaviour.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from playwright.async_api import Page

from app.core.config import settings
from app.core.logging import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)


async def save_debug_artifacts(
    page: Page,
    error_message: str,
    *,
    request_id: str | None = None,
    prefix: str = "error",
) -> str | None:
    """Take a full-page screenshot and save an HTML dump.

    Parameters
    ----------
    page : Playwright page
    error_message : human-readable error context (logged, not in filename)
    request_id : request identifier used to correlate artifacts
    prefix : filename prefix (usually the provider_id, e.g. ``"kimi"``)

    Returns
    -------
    str | None
        Path to the screenshot file, or ``None`` on total failure.
    """
    artifacts_dir = Path(settings.artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    request_id_part = request_id[:8] if request_id else "unknown"

    screenshot_path = artifacts_dir / f"{prefix}_error_{request_id_part}_{timestamp}.png"
    html_path = artifacts_dir / f"{prefix}_error_{request_id_part}_{timestamp}.html"

    saved_screenshot = False

    try:
        await page.screenshot(path=str(screenshot_path), full_page=True)
        logger.info("Saved error screenshot", path=str(screenshot_path))
        saved_screenshot = True
    except Exception as exc:
        logger.warning("Failed to save screenshot", error=str(exc))

    try:
        html_content = await page.content()
        html_path.write_text(html_content, encoding="utf-8")
        logger.info("Saved error HTML", path=str(html_path))
    except Exception as exc:
        logger.warning("Failed to save HTML", error=str(exc))

    return str(screenshot_path) if saved_screenshot else None


async def save_debug_screenshot(
    page: Page,
    *,
    request_id: str | None = None,
    prefix: str = "error",
) -> str | None:
    """Lightweight variant: screenshot only (no HTML dump)."""
    artifacts_dir = Path(settings.artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    request_id_part = request_id[:8] if request_id else "unknown"

    screenshot_path = artifacts_dir / f"{prefix}_error_{request_id_part}_{timestamp}.png"

    try:
        await page.screenshot(path=str(screenshot_path), full_page=True)
        logger.info("Saved error screenshot", path=str(screenshot_path))
        return str(screenshot_path)
    except Exception as exc:
        logger.warning("Failed to save screenshot", error=str(exc))
        return None
