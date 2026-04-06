"""
Rate limiting middleware for /v1/chat/completions.

Implements sliding-window rate limiting based on:
- API key (if present via X-API-Key header)
- Client IP (fallback)
- Per-key quotas from access policy
"""

from __future__ import annotations

import time
from threading import Lock

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.core.logging import get_logger

logger = get_logger(__name__)


class TokenBucket:
    """Simple token bucket rate limiter."""

    def __init__(self, rate: float, capacity: int) -> None:
        self.rate = rate  # tokens per second
        self.capacity = capacity
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()

    def consume(self, tokens: int = 1) -> bool:
        
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_refill = now

        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False


class SlidingWindowCounter:
    """Sliding window counter for rate limiting."""

    def __init__(self, window_seconds: float, max_count: int) -> None:
        self.window = window_seconds
        self.max_count = max_count
        self._requests: list[float] = []
        self._lock = Lock()

    def allow(self) -> bool:
        now = time.monotonic()
        cutoff = now - self.window

        with self._lock:
            # Remove expired entries
            self._requests = [ts for ts in self._requests if ts > cutoff]

            if len(self._requests) >= self.max_count:
                return False

            self._requests.append(now)
            return True

    @property
    def count(self) -> int:
        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            return sum(1 for ts in self._requests if ts > cutoff)


class RateLimitStore:
    """Global store for rate limiters."""

    def __init__(
        self,
        default_rpm: int = 60,
        default_burst: int = 10,
    ) -> None:
        self.default_rpm = default_rpm
        self.default_burst = default_burst
        self._counters: dict[str, SlidingWindowCounter] = {}
        self._lock = Lock()

    def get_counter(self, key: str) -> SlidingWindowCounter:
        with self._lock:
            if key not in self._counters:
                self._counters[key] = SlidingWindowCounter(
                    window_seconds=60, max_count=self.default_rpm
                )
            return self._counters[key]

    def update_limit(self, key: str, rpm: int) -> None:
        with self._lock:
            self._counters[key] = SlidingWindowCounter(
                window_seconds=60, max_count=rpm
            )

    def cleanup(self, max_idle_seconds: float = 3600) -> int:
        """Remove idle counters."""
        
        with self._lock:
            to_remove = []
            for key, counter in self._counters.items():
                if counter.count == 0:
                    to_remove.append(key)
            for key in to_remove:
                del self._counters[key]
            return len(to_remove)


_rate_store = RateLimitStore()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Rate limiting middleware for API requests.

    Checks:
    1. X-API-Key header → policy-based quotas
    2. Client IP → default rate limit
    """

    def __init__(
        self,
        app,
        *,
        enabled: bool = True,
        default_rpm: int = 60,
        default_burst: int = 10,
        bypass_paths: list[str] | None = None,
    ) -> None:
        super().__init__(app)
        self.enabled = enabled
        self.default_rpm = default_rpm
        self.default_burst = default_burst
        self.bypass_paths = set(bypass_paths or [
            "/health", "/live", "/ready", "/metrics",
            "/", "/static", "/ui",
        ])

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Bypass for non-rate-limited paths
        if not self.enabled or request.url.path in self.bypass_paths:
            return await call_next(request)

        # Only rate limit chat completions
        if not request.url.path.startswith("/v1/chat/completions"):
            return await call_next(request)

        # Identify the client
        client_id = self._identify_client(request)

        # Check rate limit
        counter = _rate_store.get_counter(client_id)
        if not counter.allow():
            logger.warning(
                "Rate limit exceeded",
                client_id=client_id,
                path=request.url.path,
            )
            return Response(
                status_code=429,
                content='{"error": "Rate limit exceeded. Try again later."}',
                media_type="application/json",
                headers={
                    "Retry-After": "60",
                    "X-RateLimit-Limit": str(self.default_rpm),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(time.time()) + 60),
                },
            )

        # Add rate limit headers to response
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(self.default_rpm)
        response.headers["X-RateLimit-Remaining"] = str(
            max(0, self.default_rpm - counter.count)
        )
        response.headers["X-RateLimit-Reset"] = str(int(time.time()) + 60)
        return response

    @staticmethod
    def _identify_client(request: Request) -> str:
        """Identify client by API key or IP."""
        api_key = request.headers.get("x-api-key", "")
        if api_key:
            return f"key:{api_key[:16]}"

        # Fallback to IP
        client_host = request.client.host if request.client else "test"
        return f"ip:{client_host}"


def get_rate_limit_store() -> RateLimitStore:
    return _rate_store
