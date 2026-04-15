"""Search provider base interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.search.models import SearchResult


class SearchProvider(ABC):
    """Abstract base class for search providers."""

    provider_id: str = ""

    @abstractmethod
    async def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        """Execute a search query.

        Args:
            query: Search query string
            limit: Maximum number of results to return

        Returns:
            List of SearchResult objects

        Raises:
            SearchProviderError: On provider-specific errors
        """
        raise NotImplementedError

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if provider is available.

        Returns:
            True if provider is healthy, False otherwise
        """
        raise NotImplementedError

    def get_provider_id(self) -> str:
        """Get provider identifier."""
        return self.provider_id


class SearchProviderError(Exception):
    """Base exception for search provider errors."""

    def __init__(
        self,
        provider_id: str,
        message: str,
        error_type: str = "unknown",
        details: dict[str, Any] | None = None,
    ):
        self.provider_id = provider_id
        self.error_type = error_type
        self.details = details or {}
        super().__init__(message)


class SearchTimeoutError(SearchProviderError):
    """Timeout during search operation."""

    def __init__(self, provider_id: str, message: str = "Search timeout"):
        super().__init__(provider_id, message, "timeout")


class SearchNetworkError(SearchProviderError):
    """Network error during search operation."""

    def __init__(self, provider_id: str, message: str = "Network error"):
        super().__init__(provider_id, message, "network")


class SearchParseError(SearchProviderError):
    """Failed to parse search results."""

    def __init__(self, provider_id: str, message: str = "Failed to parse results"):
        super().__init__(provider_id, message, "parse")
