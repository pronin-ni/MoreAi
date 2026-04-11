"""
Short-lived global penalty cache.

Tracks recent stage failures across pipeline executions and applies
small, temporary penalties to models that have been failing recently.

This is NOT a permanent reputation system — it's a lightweight,
TTL-bounded mechanism to help adaptive selection be more robust
during periods of model instability.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

# Default configuration
_DEFAULT_TTL_SECONDS = 300  # 5 minutes
_MAX_ENTRIES = 50  # Bounded memory
_DEFAULT_PENALTY = 0.08  # Small penalty


class GlobalPenaltyCache:
    """TTL-based cache for recent model failures.

    When a model fails in a stage execution, it gets a penalty entry
    that decays over TTL seconds. The penalty is returned by
    `get_penalty(model_id)` and can be added to the scoring breakdown.

    Multiple failures within the TTL window stack additively (capped).
    """

    def __init__(
        self,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        max_entries: int = _MAX_ENTRIES,
        default_penalty: float = _DEFAULT_PENALTY,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._default_penalty = default_penalty
        self._lock = threading.Lock()

        # model_id -> list of (timestamp, penalty, reason)
        self._entries: dict[str, list[tuple[float, float, str]]] = defaultdict(list)

    def record_failure(
        self,
        model_id: str,
        reason: str = "execution_error",
        penalty: float | None = None,
    ) -> None:
        """Record a recent failure for a model.

        The penalty will be applied to scoring for TTL seconds.
        Multiple failures stack additively (up to max_penalty).
        """
        p = penalty if penalty is not None else self._default_penalty
        now = time.time()

        with self._lock:
            self._entries[model_id].append((now, p, reason))

            # Enforce bounded size
            if len(self._entries) > self._max_entries:
                # Remove oldest entry across all models
                oldest_model = min(
                    self._entries,
                    key=lambda m: self._entries[m][0][0] if self._entries[m] else float("inf"),
                )
                self._entries[oldest_model].pop(0)
                if not self._entries[oldest_model]:
                    del self._entries[oldest_model]

        logger.debug(
            "global_penalty_recorded",
            model_id=model_id,
            reason=reason,
            penalty=str(round(p, 3)),
        )

    def get_penalty(self, model_id: str) -> dict[str, Any]:
        """Get the current global penalty for a model.

        Returns a dict with:
        - total_penalty: sum of all non-expired penalties (capped at 0.3)
        - entry_count: number of active penalty entries
        - reasons: list of unique failure reasons
        - oldest_entry_age_seconds: age of oldest active entry
        """
        now = time.time()

        with self._lock:
            entries = self._entries.get(model_id, [])

            # Filter expired entries
            active = [(ts, p, r) for ts, p, r in entries if now - ts < self._ttl]

            # Clean up expired entries for this model
            if active != entries:
                self._entries[model_id] = active

        total_penalty = min(sum(p for _, p, _ in active), 0.3)  # Cap at 0.3
        reasons = list({r for _, _, r in active})
        oldest_age = (now - active[0][0]) if active else 0.0

        return {
            "total_penalty": round(total_penalty, 3),
            "entry_count": len(active),
            "reasons": reasons,
            "oldest_entry_age_seconds": round(oldest_age, 1),
            "ttl_seconds": self._ttl,
        }

    def get_all_penalties(self) -> dict[str, dict[str, Any]]:
        """Get penalties for all tracked models."""
        result: dict[str, dict[str, Any]] = {}
        for model_id in list(self._entries.keys()):
            penalty = self.get_penalty(model_id)
            if penalty["total_penalty"] > 0:
                result[model_id] = penalty
        return result

    def clear(self) -> None:
        """Clear all cached penalties."""
        with self._lock:
            self._entries.clear()

    def cleanup_expired(self) -> int:
        """Remove expired entries. Returns count of cleaned entries."""
        now = time.time()
        cleaned = 0

        with self._lock:
            for model_id in list(self._entries.keys()):
                before = len(self._entries[model_id])
                self._entries[model_id] = [
                    (ts, p, r) for ts, p, r in self._entries[model_id]
                    if now - ts < self._ttl
                ]
                cleaned += before - len(self._entries[model_id])
                if not self._entries[model_id]:
                    del self._entries[model_id]

        return cleaned


# Global singleton
global_penalty_cache = GlobalPenaltyCache()
