"""
Staleness decay for historical model intelligence.

Implements smooth time-based decay for model performance confidence.
Older data gradually loses influence, drifting toward neutral priors.

Key properties:
- Grace period: no decay for recently active models
- Smooth exponential decay: not an abrupt cutoff
- Floor: data never fully discarded (always some residual influence)
- Neutral target: stale scores decay toward 0.5 (neutral prior)
- Explainable: decay factor and staleness are exposed in diagnostics
"""

from __future__ import annotations

import math
import time

# ── Decay Constants ──

# Grace period: no decay for recently active models
STALENESS_GRACE_SECONDS: float = 3600  # 1 hour

# Half-life: time for influence to drop to 50%
STALENESS_HALF_LIFE: float = 604800  # 7 days

# Maximum decay period: after this, decay hits the floor
STALENESS_MAX_DECAY: float = 2592000  # 30 days

# Floor: minimum decay factor (data never fully discarded)
STALENESS_FLOOR: float = 0.3

# Neutral target: what stale scores decay toward
STALENESS_NEUTRAL_TARGET: float = 0.5


class StalenessDecay:
    """Computes time-based decay factor for historical model intelligence.

    Usage:
        decay = StalenessDecay(last_activity_ts)
        factor = decay.decay_factor()          # 0.3 - 1.0
        adjusted = decay.apply(score)          # decay toward neutral
        info = decay.to_dict()                 # for diagnostics
    """

    def __init__(self, last_activity_at: float, now: float | None = None) -> None:
        """Initialize staleness calculator.

        Args:
            last_activity_at: Timestamp of last known activity (success, failure,
                or discovery). 0.0 means no activity data available.
            now: Current time for testing. Defaults to time.time().
        """
        self.last_activity_at = last_activity_at
        self._now = now or time.time()
        self.staleness_seconds = self._compute_staleness()
        self.decay_factor_value = self._compute_decay()

    def _compute_staleness(self) -> float:
        """Compute seconds since last activity."""
        if self.last_activity_at <= 0:
            # No activity data — consider maximally stale
            return STALENESS_MAX_DECAY
        return max(0.0, self._now - self.last_activity_at)

    def _compute_decay(self) -> float:
        """Compute decay factor: 1.0 (fresh) → floor (very stale)."""
        staleness = self.staleness_seconds

        # Grace period: no decay
        if staleness <= STALENESS_GRACE_SECONDS:
            return 1.0

        # Full decay period: floor
        if staleness >= STALENESS_MAX_DECAY:
            return STALENESS_FLOOR

        # Smooth exponential decay with half-life
        # decay = 0.5^(t / half_life) = exp(-ln(2) * t / half_life)
        effective_staleness = staleness - STALENESS_GRACE_SECONDS
        decay = math.exp(-math.log(2) * effective_staleness / STALENESS_HALF_LIFE)
        return max(STALENESS_FLOOR, decay)

    def decay_factor(self) -> float:
        """Get the decay factor (1.0 = fully trusted, floor = minimal trust)."""
        return self.decay_factor_value

    def apply(self, score: float) -> float:
        """Apply decay to a score, drifting toward neutral target.

        Formula: decayed = score * factor + neutral * (1 - factor)

        For a fresh model (factor=1.0): returns score unchanged.
        For a stale model (factor=0.3): returns score * 0.3 + 0.5 * 0.7 = 0.37 + 0.35 = ~0.42.
        """
        return score * self.decay_factor_value + STALENESS_NEUTRAL_TARGET * (1 - self.decay_factor_value)

    def is_fresh(self) -> bool:
        """Model has recent activity (within grace period)."""
        return self.staleness_seconds <= STALENESS_GRACE_SECONDS

    def is_stale(self) -> bool:
        """Model has exceeded half-life without activity."""
        return self.staleness_seconds > STALENESS_HALF_LIFE

    def is_very_stale(self) -> bool:
        """Model has exceeded max decay period (at floor)."""
        return self.staleness_seconds >= STALENESS_MAX_DECAY

    @property
    def staleness_label(self) -> str:
        """Human-readable staleness label."""
        if self.is_fresh():
            return "fresh"
        if self.staleness_seconds <= STALENESS_HALF_LIFE:
            return "aging"
        if self.is_very_stale():
            return "very_stale"
        return "stale"

    def to_dict(self) -> dict:
        """Diagnostics output."""
        return {
            "staleness_seconds": round(self.staleness_seconds, 1),
            "staleness_hours": round(self.staleness_seconds / 3600, 2),
            "staleness_days": round(self.staleness_seconds / 86400, 2),
            "decay_factor": round(self.decay_factor_value, 4),
            "is_fresh": self.is_fresh(),
            "is_stale": self.is_stale(),
            "is_very_stale": self.is_very_stale(),
            "staleness_label": self.staleness_label,
            "neutral_target": STALENESS_NEUTRAL_TARGET,
            "grace_period_seconds": STALENESS_GRACE_SECONDS,
            "half_life_seconds": STALENESS_HALF_LIFE,
            "max_decay_seconds": STALENESS_MAX_DECAY,
            "floor": STALENESS_FLOOR,
            "last_activity_at": self.last_activity_at,
        }


def compute_decay(last_activity_at: float, now: float | None = None) -> StalenessDecay:
    """Convenience function for one-off decay computation."""
    return StalenessDecay(last_activity_at, now)
