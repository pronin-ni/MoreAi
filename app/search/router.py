"""Search router with fallback logic."""

from __future__ import annotations

import asyncio

from app.core.config import settings
from app.core.logging import get_logger
from app.search.models import SearchError, SearchResult
from app.search.providers.base import SearchProvider, SearchProviderError
from app.search.providers.duckduckgo import duckduckgo_provider
from app.search.providers.searxng import create_searxng_provider

logger = get_logger(__name__)


class SearchRouter:
    """Search router with provider fallback."""

    def __init__(self) -> None:
        self._providers: list[SearchProvider] = []
        self._provider_errors: dict[str, SearchError] = {}
        self._initialize_providers()

    def _initialize_providers(self) -> None:
        """Initialize providers from config."""
        provider_names = settings.search.providers.split(",")

        for name in provider_names:
            name = name.strip().lower()
            if not name:
                continue

            if name == "duckduckgo":
                self._providers.append(duckduckgo_provider)
                logger.info("search_provider_added", provider="duckduckgo")
            elif name == "searxng":
                searx = create_searxng_provider()
                self._providers.append(searx)
                logger.info("search_provider_added", provider="searxng", base_url=searx.base_url)
            else:
                logger.warning("unknown_search_provider", provider=name)

        if not self._providers:
            logger.warning("no_search_providers_configured")

    @property
    def providers(self) -> list[SearchProvider]:
        """Get list of configured providers."""
        return self._providers

    async def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        """Execute search with fallback to next provider on failure.

        Args:
            query: Search query
            limit: Max results to return

        Returns:
            List of SearchResult from first successful provider

        Raises:
            SearchUnavailableError: If all providers fail
        """
        errors: list[SearchError] = []

        for provider in self._providers:
            try:
                logger.debug(
                    "search_attempt",
                    provider=provider.provider_id,
                    query=query,
                    limit=limit,
                )

                results = await asyncio.wait_for(
                    provider.search(query, limit),
                    timeout=settings.search.timeout + 1,
                )

                if results:
                    logger.info(
                        "search_success",
                        provider=provider.provider_id,
                        query=query,
                        result_count=len(results),
                    )
                    # Clear previous errors for this provider
                    self._provider_errors.pop(provider.provider_id, None)
                    return results
                else:
                    logger.debug(
                        "search_empty_results",
                        provider=provider.provider_id,
                        query=query,
                    )
                    # Empty results from this provider, try next
                    continue

            except SearchProviderError as e:
                error = SearchError(
                    provider=provider.provider_id,
                    error_type=e.error_type,
                    message=str(e),
                    details=e.details,
                )
                errors.append(error)
                self._provider_errors[provider.provider_id] = error

                logger.warning(
                    "search_provider_error",
                    provider=provider.provider_id,
                    error_type=e.error_type,
                    query=query,
                    error=str(e),
                )

            except TimeoutError:
                error = SearchError(
                    provider=provider.provider_id,
                    error_type="timeout",
                    message="Provider timeout",
                )
                errors.append(error)
                self._provider_errors[provider.provider_id] = error

                logger.warning(
                    "search_provider_timeout",
                    provider=provider.provider_id,
                    query=query,
                )

            except Exception as e:
                error = SearchError(
                    provider=provider.provider_id,
                    error_type="unknown",
                    message=str(e),
                )
                errors.append(error)
                self._provider_errors[provider.provider_id] = error

                logger.error(
                    "search_provider_exception",
                    provider=provider.provider_id,
                    query=query,
                    error=str(e),
                )

        # All providers failed
        logger.error(
            "search_all_providers_failed",
            query=query,
            provider_count=len(self._providers),
            error_count=len(errors),
        )

        return []

    async def health_check_all(self) -> dict[str, bool]:
        """Check health of all providers."""
        health: dict[str, bool] = {}

        for provider in self._providers:
            try:
                health[provider.provider_id] = await asyncio.wait_for(
                    provider.health_check(),
                    timeout=3.0,
                )
            except Exception:
                health[provider.provider_id] = False

        return health

    def get_provider_errors(self) -> dict[str, SearchError]:
        """Get last error for each provider."""
        return self._provider_errors.copy()


# Global router instance
search_router = SearchRouter()
