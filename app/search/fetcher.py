"""Content fetcher for extracting page content."""

from __future__ import annotations

import asyncio
import re

import trafilatura
from bs4 import BeautifulSoup
from httpx import AsyncClient, TimeoutException

from app.core.config import settings
from app.core.logging import get_logger
from app.search.providers.base import SearchNetworkError, SearchTimeoutError

logger = get_logger(__name__)

# Max content length to store
MAX_CONTENT_LENGTH = 5000


class ContentFetcher:
    """Fetches and extracts content from web pages."""

    def __init__(self) -> None:
        self._client: AsyncClient | None = None

    async def _get_client(self) -> AsyncClient:
        """Get or create HTTP client."""
        if self._client is None:
            self._client = AsyncClient(
                timeout=settings.search.fetch_timeout,
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                },
            )
        return self._client

    async def close(self) -> None:
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def fetch(self, url: str) -> str | None:
        """Fetch and extract content from URL.

        Args:
            url: URL to fetch

        Returns:
            Extracted text content or None on failure

        Note:
            Uses trafilatura as primary extractor.
            Falls back to BeautifulSoup + plain text extraction.
            Returns snippet as fallback if all methods fail.
        """
        client = await self._get_client()

        try:
            response = await client.get(url, timeout=settings.search.fetch_timeout)
            response.raise_for_status()

            html = response.text

            # Try trafilatura first
            content = _extract_with_trafilatura(html, url)
            if content:
                logger.debug("content_fetched_trafilatura", url=url, length=len(content))
                return _truncate_content(content)

            # Fallback to BeautifulSoup
            content = _extract_with_beautifulsoup(html)
            if content:
                logger.debug("content_fetched_beautifulsoup", url=url, length=len(content))
                return _truncate_content(content)

            logger.warning("content_extraction_failed", url=url)
            return None

        except TimeoutException:
            logger.warning("content_fetch_timeout", url=url)
            raise SearchTimeoutError("content_fetcher", f"Timeout fetching {url}")

        except Exception as e:
            error_msg = str(e).lower()
            if "timeout" in error_msg:
                raise SearchTimeoutError("content_fetcher", f"Timeout fetching {url}")

            logger.warning("content_fetch_error", url=url, error=str(e))
            raise SearchNetworkError("content_fetcher", f"Failed to fetch {url}: {e}")

    async def fetch_multiple(self, urls: list[str], max_pages: int | None = None) -> dict[str, str]:
        """Fetch content from multiple URLs concurrently.

        Args:
            urls: List of URLs to fetch
            max_pages: Max pages to fetch (default from config)

        Returns:
            Dict mapping URL to extracted content
        """
        if not urls:
            return {}

        limit = max_pages or settings.search.fetch_max_pages
        urls_to_fetch = urls[:limit]

        logger.debug("fetching_multiple_urls", total=len(urls), fetching=len(urls_to_fetch))

        results: dict[str, str] = {}

        # Fetch concurrently with semaphore to limit concurrency
        semaphore = asyncio.Semaphore(3)

        async def fetch_with_limit(url: str) -> tuple[str, str | None]:
            async with semaphore:
                try:
                    content = await self.fetch(url)
                    return (url, content)
                except Exception as e:
                    logger.warning("fetch_url_failed", url=url, error=str(e))
                    return (url, None)

        # Run all fetches concurrently
        tasks = [fetch_with_limit(url) for url in urls_to_fetch]
        fetched = await asyncio.gather(*tasks, return_exceptions=True)

        for result in fetched:
            if isinstance(result, Exception):
                continue
            url, content = result
            if content:
                results[url] = content

        logger.info("content_fetch_complete", total=len(urls_to_fetch), successful=len(results))
        return results


def _extract_with_trafilatura(html: str, url: str) -> str | None:
    """Extract content using trafilatura."""
    try:
        result = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=True,
            include_images=False,
            output_format="text",
            with_metadata=False,
        )

        if result and len(result.strip()) > 50:
            return result.strip()

    except Exception as e:
        logger.debug("trafilatura_extraction_failed", error=str(e))

    return None


def _extract_with_beautifulsoup(html: str) -> str | None:
    """Extract content using BeautifulSoup as fallback."""
    try:
        soup = BeautifulSoup(html, "lxml")

        # Remove script and style elements
        for script in soup(["script", "style", "nav", "header", "footer"]):
            script.decompose()

        # Try to find main content area
        content = (
            soup.find("article")
            or soup.find("main")
            or soup.find("div", class_=re.compile(r"content|article|post", re.I))
            or soup.find("div", id=re.compile(r"content|article|post", re.I))
        )

        if content:
            text = content.get_text(separator="\n", strip=True)
            if text and len(text) > 50:
                return text

        # Fallback to body text
        body = soup.find("body")
        if body:
            text = body.get_text(separator="\n", strip=True)
            if text and len(text) > 50:
                return text

    except Exception as e:
        logger.debug("beautifulsoup_extraction_failed", error=str(e))

    return None


def _truncate_content(content: str, max_length: int = MAX_CONTENT_LENGTH) -> str:
    """Truncate content to max length."""
    if len(content) <= max_length:
        return content

    # Find a good break point (end of sentence or paragraph)
    truncated = content[:max_length]
    last_period = truncated.rfind(".")
    last_newline = truncated.rfind("\n")

    # Break at sentence or paragraph boundary
    if last_period > max_length * 0.8:
        return truncated[: last_period + 1]
    elif last_newline > max_length * 0.8:
        return truncated[:last_newline]

    return truncated + "..."


# Global fetcher instance
content_fetcher = ContentFetcher()
