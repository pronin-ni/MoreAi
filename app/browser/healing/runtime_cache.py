"""
Runtime cache for healed locators.

Features:
- Per-session storage (cleared on restart)
- Dynamic TTL: higher success → longer TTL
- LRU eviction when cache is full
- Invalidation on failure
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class CachedHealedLocator:
    """A healed locator cached for reuse."""

    selector: str
    profile_role: str
    provider_id: str
    created_at: float
    ttl: float  # seconds
    use_count: int = 0
    last_used_at: float = 0.0
    success_count: int = 0
    failure_count: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        return time.monotonic() - self.created_at > self.ttl

    @property
    def effective_ttl(self) -> float:
        """Dynamic TTL based on success rate."""
        total = self.success_count + self.failure_count
        if total == 0:
            return self.ttl
        success_rate = self.success_count / total
        # Scale TTL: 0.5x for bad entries, 2x for excellent ones
        multiplier = 0.5 + (1.5 * success_rate)
        return self.ttl * multiplier

    def mark_used(self, success: bool = True) -> None:
        self.use_count += 1
        self.last_used_at = time.monotonic()
        if success:
            self.success_count += 1
        else:
            self.failure_count += 1


class RuntimeHealingCache:
    """Per-session cache for healed locators with LRU eviction.

    Keyed by (provider_id, role) -> CachedHealedLocator.
    """

    def __init__(
        self,
        default_ttl: float = 300.0,
        max_size: int = 200,
    ) -> None:
        self._default_ttl = default_ttl
        self._max_size = max_size
        # OrderedDict for LRU ordering
        self._cache: OrderedDict[tuple[str, str], CachedHealedLocator] = OrderedDict()

    def get(self, provider_id: str, role: str) -> CachedHealedLocator | None:
        """Get a cached healed locator if valid. Moves to end (LRU)."""
        key = (provider_id, role)
        if key not in self._cache:
            return None

        cached = self._cache[key]
        if cached.is_expired:
            del self._cache[key]
            logger.debug(
                "Healing cache expired",
                provider_id=provider_id,
                role=role,
            )
            return None

        # Move to end (most recently used)
        self._cache.move_to_end(key)
        cached.mark_used(success=True)

        logger.debug(
            "Healing cache hit",
            provider_id=provider_id,
            role=role,
            selector=cached.selector,
            use_count=cached.use_count,
            success_rate=round(cached.success_count / max(cached.success_count + cached.failure_count, 1), 2),
        )
        return cached

    def put(
        self,
        provider_id: str,
        role: str,
        selector: str,
        meta: dict[str, Any] | None = None,
        ttl: float | None = None,
    ) -> None:
        """Store a healed locator in the cache."""
        key = (provider_id, role)

        # If already exists, update it
        if key in self._cache:
            existing = self._cache[key]
            existing.selector = selector
            existing.meta = meta or {}
            existing.ttl = ttl or self._default_ttl
            existing.created_at = time.monotonic()  # Reset TTL
            existing.success_count += 1
            self._cache.move_to_end(key)
            logger.debug(
                "Healing cache updated (existing entry refreshed)",
                provider_id=provider_id,
                role=role,
                selector=selector,
            )
            return

        # Evict if full (LRU: remove oldest)
        if len(self._cache) >= self._max_size:
            evicted_key, evicted = self._cache.popitem(last=False)
            logger.debug(
                "Healing cache evicted LRU entry",
                provider_id=evicted.provider_id,
                role=evicted.profile_role,
                selector=evicted.selector,
                use_count=evicted.use_count,
            )

        self._cache[key] = CachedHealedLocator(
            selector=selector,
            profile_role=role,
            provider_id=provider_id,
            created_at=time.monotonic(),
            ttl=ttl or self._default_ttl,
            meta=meta or {},
        )
        logger.info(
            "Healing cache updated",
            provider_id=provider_id,
            role=role,
            selector=selector,
            ttl=ttl or self._default_ttl,
            size=len(self._cache),
        )

    def record_failure(self, provider_id: str, role: str) -> None:
        """Record a failure for a cached entry (reduces TTL, may invalidate)."""
        key = (provider_id, role)
        if key in self._cache:
            cached = self._cache[key]
            cached.mark_used(success=False)
            # If failure rate > 50%, invalidate
            total = cached.success_count + cached.failure_count
            if total > 1 and cached.failure_count / total > 0.5:
                del self._cache[key]
                logger.info(
                    "Healing cache invalidated due to high failure rate",
                    provider_id=provider_id,
                    role=role,
                    failure_rate=round(cached.failure_count / total, 2),
                )

    def invalidate(self, provider_id: str, role: str | None = None) -> None:
        """Invalidate cached entries."""
        if role:
            key = (provider_id, role)
            if key in self._cache:
                del self._cache[key]
            logger.debug("Healing cache invalidated", provider_id=provider_id, role=role)
        else:
            # Invalidate all for this provider
            to_remove = [k for k in self._cache if k[0] == provider_id]
            for k in to_remove:
                del self._cache[k]
            logger.debug(
                "All healing cache entries invalidated for provider",
                provider_id=provider_id,
            )

    def clear(self) -> None:
        """Clear all cached entries."""
        self._cache.clear()

    def snapshot(self) -> list[dict[str, Any]]:
        """Return a snapshot of the cache for diagnostics."""
        now = time.monotonic()
        return [
            {
                "provider_id": v.provider_id,
                "role": v.profile_role,
                "selector": v.selector,
                "age_seconds": round(now - v.created_at, 1),
                "effective_ttl": round(v.effective_ttl, 1),
                "use_count": v.use_count,
                "success_count": v.success_count,
                "failure_count": v.failure_count,
                "expired": v.is_expired,
                "meta": v.meta,
            }
            for v in self._cache.values()
        ]

    @property
    def size(self) -> int:
        return len(self._cache)

    @property
    def hit_count(self) -> int:
        return sum(v.use_count for v in self._cache.values())


# Global shared cache instance
healing_cache = RuntimeHealingCache()
