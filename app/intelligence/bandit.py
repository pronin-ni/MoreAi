"""
Multi-armed bandit model selection.

Provides dynamic learning based on real execution outcomes:
- Tracks success/failure per model
- Computes dynamic scores
- Uses epsilon-greedy for exploration
- Penalizes unstable providers
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from app.core.logging import get_logger

logger = get_logger(__name__)

# Constants
EPSILON = 0.1  # 10% exploration
SUCCESS_WEIGHT = 0.5
QUALITY_WEIGHT = 0.3
LATENCY_WEIGHT = 0.2
PENALTY_MULTIPLIER = 0.5  # Penalty for unstable providers
MIN_SAMPLES = 3  # Minimum samples before full weight


@dataclass(slots=True)
class ModelStats:
    """Runtime statistics for a model."""

    model_id: str
    provider_id: str
    success_count: int = 0
    failure_count: int = 0
    total_latency_ms: float = 0.0
    total_quality_score: float = 0.0
    last_updated: float = field(default_factory=time.monotonic)

    @property
    def avg_latency_ms(self) -> float:
        total = self.success_count + self.failure_count
        return self.total_latency_ms / max(1, total) if total > 0 else 0.0

    @property
    def avg_quality_score(self) -> float:
        total = self.success_count + self.failure_count
        return self.total_quality_score / max(1, total) if total > 0 else 0.0

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.failure_count
        return self.success_count / max(1, total) if total > 0 else 0.0

    @property
    def is_unstable(self) -> bool:
        total = self.success_count + self.failure_count
        return total > MIN_SAMPLES and self.failure_count > self.success_count


class BanditModel:
    """Multi-armed bandit for model selection."""

    def __init__(self) -> None:
        self._stats: dict[str, ModelStats] = {}
        self._timeout_bans: dict[str, float] = {}

    def get_stats(self, model_id: str, provider_id: str) -> ModelStats:
        key = f"{model_id}:{provider_id}"
        if key not in self._stats:
            self._stats[key] = ModelStats(model_id=model_id, provider_id=provider_id)
        return self._stats[key]

    def record_success(
        self,
        model_id: str,
        provider_id: str,
        latency_ms: float,
        quality_score: float = 0.5,
    ) -> None:
        stats = self.get_stats(model_id, provider_id)
        stats.success_count += 1
        stats.total_latency_ms += latency_ms
        stats.total_quality_score += quality_score
        stats.last_updated = time.monotonic()
        logger.info(
            "bandit_success_recorded",
            model=model_id,
            provider=provider_id,
            success_count=stats.success_count,
            success_rate=stats.success_rate,
        )

    def record_failure(self, model_id: str, provider_id: str, reason: str = "") -> None:
        stats = self.get_stats(model_id, provider_id)
        stats.failure_count += 1
        stats.last_updated = time.monotonic()
        logger.info(
            "bandit_failure_recorded",
            model=model_id,
            provider=provider_id,
            failure_count=stats.failure_count,
            reason=reason,
        )

    def compute_bandit_score(self, model_id: str, provider_id: str) -> float:
        stats = self.get_stats(model_id, provider_id)

        if stats.failure_count > stats.success_count:
            score = PENALTY_MULTIPLIER * self._compute_score(stats)
        else:
            score = self._compute_score(stats)

        return score

    def _compute_score(self, stats: ModelStats) -> float:
        success_rate = stats.success_rate
        avg_quality = stats.avg_quality_score
        latency_factor = 1.0 / (1.0 + stats.avg_latency_ms / 1000.0)

        return (
            SUCCESS_WEIGHT * success_rate
            + QUALITY_WEIGHT * avg_quality
            + LATENCY_WEIGHT * latency_factor
        )

    def select_model(
        self,
        candidates: list[dict],
    ) -> dict | None:
        if not candidates:
            return None

        # Check if we should explore (epsilon-greedy)
        should_explore = random.random() < EPSILON

        if should_explore:
            logger.info(
                "bandit_exploration",
                candidate_count=len(candidates),
                epsilon=EPSILON,
            )
            return random.choice(candidates)

        # Exploitation: select highest scoring model
        best_candidate = None
        best_score = -1.0

        for candidate in candidates:
            model_id = candidate.get("model_id", "")
            provider_id = candidate.get("provider_id", "")

            if not model_id or not provider_id:
                continue

            score = self.compute_bandit_score(model_id, provider_id)

            if score > best_score:
                best_score = score
                best_candidate = candidate

            logger.info(
                "bandit_score_computed",
                model=model_id,
                provider=provider_id,
                score=score,
                is_selected=score == best_score,
            )

        if best_candidate is None:
            return random.choice(candidates)

        logger.info(
            "bandit_exploitation",
            model=best_candidate.get("model_id"),
            provider=best_candidate.get("provider_id"),
            score=best_score,
        )

        return best_candidate

    def get_leaderboard(self) -> list[dict]:
        leaderboard = []
        for stats in self._stats.values():
            score = self._compute_score(stats)
            leaderboard.append(
                {
                    "model_id": stats.model_id,
                    "provider_id": stats.provider_id,
                    "success_count": stats.success_count,
                    "failure_count": stats.failure_count,
                    "success_rate": stats.success_rate,
                    "avg_latency_ms": stats.avg_latency_ms,
                    "avg_quality_score": stats.avg_quality_score,
                    "score": score,
                    "is_unstable": stats.is_unstable,
                }
            )
        return sorted(leaderboard, key=lambda x: x["score"], reverse=True)


bandit_model = BanditModel()
