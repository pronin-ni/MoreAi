"""DuckDuckGo search provider."""

from __future__ import annotations

from bs4 import BeautifulSoup
from httpx import AsyncClient, TimeoutException

from app.core.config import settings
from app.core.logging import get_logger
from app.search.models import SearchResult
from app.search.providers.base import (
    SearchNetworkError,
    SearchParseError,
    SearchProvider,
    SearchTimeoutError,
)

logger = get_logger(__name__)

DUCKDUCKGO_URL = "https://html.duckduckgo.com/html/"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


class DuckDuckGoProvider(SearchProvider):
    """DuckDuckGo HTML search provider."""

    provider_id = "duckduckgo"

    def __init__(self) -> None:
        self._client: AsyncClient | None = None

    async def _get_client(self) -> AsyncClient:
        """Get or create HTTP client."""
        if self._client is None:
            self._client = AsyncClient(
                timeout=settings.search.timeout,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        """Search using DuckDuckGo HTML interface."""
        client = await self._get_client()

        try:
            response = await client.post(
                DUCKDUCKGO_URL,
                data={"q": query, "b": ""},
            )
            response.raise_for_status()

            return self._parse_results(response.text, limit)

        except TimeoutException as e:
            logger.warning("duckduckgo_timeout", query=query, error=str(e))
            raise SearchTimeoutError(self.provider_id, f"Timeout: {e}")

        except Exception as e:
            error_msg = str(e)
            if "timeout" in error_msg.lower():
                raise SearchTimeoutError(self.provider_id, f"Timeout: {e}")

            logger.warning("duckduckgo_error", query=query, error=error_msg)
            raise SearchNetworkError(self.provider_id, f"Network error: {e}")

    def _parse_results(self, html: str, limit: int) -> list[SearchResult]:
        """Parse DuckDuckGo HTML results."""
        results: list[SearchResult] = []

        try:
            soup = BeautifulSoup(html, "lxml")

            # DuckDuckGo HTML result class: result__a (title), result__snippet (snippet)
            for result in soup.select(".result"):
                if len(results) >= limit:
                    break

                try:
                    # Title and URL: result__a link
                    title_elem = result.select_one(".result__a")
                    if not title_elem:
                        continue

                    title = title_elem.get_text(strip=True)
                    url = title_elem.get("href", "")
                    if not url:
                        continue

                    # Extract actual URL from redirect
                    if "uddg=" in url:
                        import urllib.parse

                        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                        url = params.get("uddg", [url])[0]

                    # Snippet: result__snippet
                    snippet_elem = result.select_one(".result__snippet")
                    snippet = snippet_elem.get_text(strip=True) if snippet_elem else ""

                    results.append(
                        SearchResult(
                            title=title,
                            url=url,
                            snippet=snippet,
                            source=self.provider_id,
                        )
                    )

                except Exception as e:
                    logger.debug("duckduckgo_parse_error", error=str(e))
                    continue

        except Exception as e:
            logger.warning("duckduckgo_html_parse_failed", error=str(e))
            raise SearchParseError(self.provider_id, f"Failed to parse HTML: {e}")

        return results

    async def health_check(self) -> bool:
        """Check if DuckDuckGo is available."""
        try:
            client = await self._get_client()
            response = await client.get("https://html.duckduckgo.com/html/", timeout=2.0)
            return response.status_code == 200
        except Exception:
            return False


# Global provider instance
duckduckgo_provider = DuckDuckGoProvider()
