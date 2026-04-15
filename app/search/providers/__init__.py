"""Search providers."""

from app.search.providers.base import SearchProvider
from app.search.providers.duckduckgo import DuckDuckGoProvider, duckduckgo_provider
from app.search.providers.searxng import SearXNGProvider, create_searxng_provider

__all__ = [
    "SearchProvider",
    "DuckDuckGoProvider",
    "SearXNGProvider",
    "duckduckgo_provider",
    "create_searxng_provider",
]
