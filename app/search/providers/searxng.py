"""SearXNG search provider."""

from __future__ import annotations

import json

from httpx import AsyncClient, TimeoutException

from app.core.config import settings
from app.core.logging import get_logger
from app.search.models import SearchResult
from app.search.providers.base import (
    SearchNetworkError,
    SearchProvider,
    SearchTimeoutError,
)

logger = get_logger(__name__)


class SearXNGProvider(SearchProvider):
    """SearXNG JSON API search provider."""

    provider_id = "searxng"

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = base_url or settings.search.searxng_base_url
        self._client: AsyncClient | None = None

    @property
    def base_url(self) -> str:
        return self._base_url

    async def _get_client(self) -> AsyncClient:
        """Get or create HTTP client."""
        if self._client is None:
            self._client = AsyncClient(
                timeout=settings.search.timeout,
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        """Search using SearXNG JSON API."""
        client = await self._get_client()

        # Build SearXNG API URL
        search_url = f"{self._base_url}/search"
        params = {
            "q": query,
            "format": "json",
            "engines": "",  # Use all engines
            "categories": "general",
        }

        try:
            response = await client.get(search_url, params=params)
            response.raise_for_status()

            return self._parse_results(response.text, limit)

        except TimeoutException as e:
            logger.warning("searxng_timeout", query=query, base_url=self._base_url, error=str(e))
            raise SearchTimeoutError(self.provider_id, f"Timeout: {e}")

        except Exception as e:
            error_msg = str(e)
            if "timeout" in error_msg.lower():
                raise SearchTimeoutError(self.provider_id, f"Timeout: {e}")

            logger.warning("searxng_error", query=query, base_url=self._base_url, error=error_msg)
            raise SearchNetworkError(self.provider_id, f"Network error: {e}")

    def _parse_results(self, response_text: str, limit: int) -> list[SearchResult]:
        """Parse SearXNG JSON response."""
        results: list[SearchResult] = []

        try:
            data = json.loads(response_text)
            search_results = data.get("results", [])

            for item in search_results:
                if len(results) >= limit:
                    break

                title = item.get("title", "")
                url = item.get("url", "")
                content = item.get("content", "")

                if not url or not title:
                    continue

                results.append(
                    SearchResult(
                        title=title,
                        url=url,
                        snippet=content,
                        source=self.provider_id,
                    )
                )

        except json.JSONDecodeError as e:
            logger.warning("searxng_json_parse_failed", error=str(e))
            raise SearchNetworkError(self.provider_id, f"Failed to parse JSON: {e}")

        return results

    async def health_check(self) -> bool:
        """Check if SearXNG instance is available."""
        try:
            client = await self._get_client()
            response = await client.get(
                f"{self._base_url}/search",
                params={"q": "test", "format": "json"},
                timeout=2.0,
            )
            return response.status_code == 200
        except Exception:
            return False


def create_searxng_provider() -> SearXNGProvider:
    """Factory function to create SearXNG provider."""
    return SearXNGProvider()
