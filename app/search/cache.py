"""Search and content caching."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class CacheEntry:
    """Cache entry with expiry."""

    value: Any
    expires_at: float


class SearchCache:
    """In-memory cache for search results."""

    def __init__(self, ttl_seconds: int | None = None) -> None:
        self._ttl = ttl_seconds or settings.search.cache_ttl_seconds
        self._cache: dict[str, CacheEntry] = {}
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Any | None:
        """Get value from cache."""
        entry = self._cache.get(key)
        if entry is None:
            self._misses += 1
            return None

        if time.time() > entry.expires_at:
            # Expired
            del self._cache[key]
            self._misses += 1
            return None

        self._hits += 1
        return entry.value

    def set(self, key: str, value: Any) -> None:
        """Set value in cache with TTL."""
        self._cache[key] = CacheEntry(
            value=value,
            expires_at=time.time() + self._ttl,
        )

    def clear(self) -> None:
        """Clear all cache entries."""
        self._cache.clear()
        self._hits = 0
        self._misses = 0

    def cleanup_expired(self) -> int:
        """Remove expired entries. Returns count of removed entries."""
        now = time.time()
        expired_keys = [k for k, v in self._cache.items() if now > v.expires_at]

        for key in expired_keys:
            del self._cache[key]

        if expired_keys:
            logger.debug("cache_cleanup", removed=len(expired_keys))

        return len(expired_keys)

    @property
    def stats(self) -> dict[str, Any]:
        """Get cache statistics."""
        total = self._hits + self._misses
        hit_rate = self._hits / total if total > 0 else 0.0

        return {
            "entries": len(self._cache),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(hit_rate, 3),
        }


class PageCache(SearchCache):
    """In-memory cache for page content with longer TTL."""

    def __init__(self) -> None:
        super().__init__(ttl_seconds=settings.search.page_cache_ttl_seconds)


# Global cache instances
search_cache = SearchCache()
page_cache = PageCache()
