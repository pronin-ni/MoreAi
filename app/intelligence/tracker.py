"""
Model Intelligence Tracker — lifecycle tracking for discovered models.

Tracks:
- When models appear via discovery
- When models temporarily disappear (preserves historical stats)
- When models return (reuses historical intelligence state)
- Discovery timestamps for cold-start identification

Provides diagnostics:
- Which models are newly discovered (cold-start)
- Which are fully ranked with sufficient history
- Which are temporarily unavailable
- Which are excluded and why
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from app.core.logging import get_logger
from app.registry.unified import unified_registry

logger = get_logger(__name__)


class ModelLifecycleEntry:
    """Lifecycle state for a single model."""

    def __init__(self, canonical_id: str):
        self.canonical_id = canonical_id
        self.first_discovered_at: float = time.time()
        self.last_seen_at: float = self.first_discovered_at
        self.last_missing_at: float = 0.0
        self.is_currently_available: bool = True
        self.disappearance_count: int = 0
        self.total_availability_time_s: float = 0.0
        self.discovery_source: str = "unknown"  # api_registry, agent_registry, etc.
        self.last_provider: str = ""  # Which provider last reported this model

        # Exploration tracking for bandit approach
        self.exploration_attempts: int = 0
        self.successful_explorations: int = 0
        self.is_cold_start: bool = True

    def mark_available(self, source: str = "unknown", provider_id: str = "") -> None:
        """Mark model as currently available (discovered or returned)."""
        was_missing = not self.is_currently_available
        self.is_currently_available = True
        self.last_seen_at = time.time()
        self.discovery_source = source
        if provider_id:
            self.last_provider = provider_id

        if was_missing:
            self.disappearance_count += 1
            logger.info(
                "model_returned",
                model=self.canonical_id,
                disappearance_count=str(self.disappearance_count),
                missing_duration_s=str(round(self.last_seen_at - self.last_missing_at, 1)),
            )

    def mark_missing(self, provider_id: str = "") -> None:
        """Mark model as temporarily missing (disappeared from discovery)."""
        # Only mark missing if this model was last seen from the same provider
        # This prevents cross-provider false "missing" events
        if provider_id and self.last_provider and self.last_provider != provider_id:
            return
        if self.is_currently_available:
            self.last_missing_at = time.time()
            self.is_currently_available = False
            logger.debug(
                "model_temporarily_missing",
                model=self.canonical_id,
            )

    def record_exploration_attempt(self, success: bool) -> None:
        """Record an exploration attempt for this model."""
        self.exploration_attempts += 1
        if success:
            self.successful_explorations += 1
            logger.debug(
                "exploration_success",
                model=self.canonical_id,
                attempts=self.exploration_attempts,
                successes=self.successful_explorations,
            )
        else:
            logger.debug(
                "exploration_failed",
                model=self.canonical_id,
                attempts=self.exploration_attempts,
            )
        # Update cold-start status based on config
        self._update_cold_start_status()

    def _update_cold_start_status(self) -> None:
        """Update cold-start status based on exploration results."""
        from app.core.config import settings

        threshold = settings.pipeline.cold_start_threshold
        min_successes = settings.pipeline.exploration_min_successes

        # Exit cold-start if we have enough samples AND enough successes
        if (
            self.exploration_attempts >= threshold
            and self.successful_explorations >= min_successes
            and self.is_cold_start
        ):
            self.is_cold_start = False
            logger.info(
                "cold_start_exited",
                model=self.canonical_id,
                attempts=self.exploration_attempts,
                successes=self.successful_explorations,
            )

    def get_is_cold_start(self, sample_count: int = 0) -> bool:
        """Check if model is in cold-start state based on sample count."""
        if not self.is_cold_start:
            return False

        from app.core.config import settings

        threshold = settings.pipeline.cold_start_threshold
        # Cold-start if we have fewer samples than threshold
        return sample_count < threshold

    def to_dict(self) -> dict:
        return {
            "canonical_id": self.canonical_id,
            "first_discovered_at": self.first_discovered_at,
            "last_seen_at": self.last_seen_at,
            "last_missing_at": self.last_missing_at,
            "is_currently_available": self.is_currently_available,
            "disappearance_count": self.disappearance_count,
            "discovery_source": self.discovery_source,
            "is_cold_start": self.is_cold_start,
            "lifetime_s": round(self.last_seen_at - self.first_discovered_at, 1),
            "exploration_attempts": self.exploration_attempts,
            "successful_explorations": self.successful_explorations,
        }


class ModelIntelligenceTracker:
    """Tracks model lifecycle and bridges discovery → intelligence layer.

    When models appear via discovery:
    - Creates lifecycle entry
    - Triggers callbacks for immediate scoring/tag inference
    - Registers in intelligence subsystem

    When models disappear:
    - Marks as temporarily missing
    - Preserves historical stats (not purged)

    When models return:
    - Reuses historical intelligence state
    - Updates discovery timestamp
    """

    def __init__(self) -> None:
        self._entries: dict[str, ModelLifecycleEntry] = {}
        self._callbacks: list[Callable[[str, list[str], list[str]], Any]] = []

    def register_callback(self, callback) -> None:
        """Register a callback to be called when models are discovered.

        Callback signature: on_models_discovered(provider_id, new_models, removed_models)
        """
        self._callbacks.append(callback)

    def on_discovery_complete(
        self,
        provider_id: str,
        discovered_model_ids: list[str],
        source: str = "unknown",
    ) -> dict:
        """Called after a discovery cycle completes for a provider.

        Compares discovered models against current state to identify:
        - New models (first appearance)
        - Returned models (reappeared after disappearance)
        - Missing models (previously seen, not in current discovery)

        Returns a diff dict with added/returned/missing model lists.
        """
        current_ids = set(discovered_model_ids)
        known_ids = set(self._entries.keys())

        new_ids = current_ids - known_ids
        returned_ids = set()
        missing_ids = set()

        # Track new and returned models
        for model_id in discovered_model_ids:
            if model_id in self._entries:
                entry = self._entries[model_id]
                if not entry.is_currently_available:
                    entry.mark_available(source, provider_id=provider_id)
                    returned_ids.add(model_id)
            else:
                entry = ModelLifecycleEntry(model_id)
                entry.mark_available(source, provider_id=provider_id)
                self._entries[model_id] = entry
                logger.info(
                    "model_discovered",
                    model=model_id,
                    source=source,
                    is_cold_start=str(entry.is_cold_start),
                )

        # Mark missing models — only for models from the same provider
        for model_id in list(self._entries.keys()):
            if model_id not in current_ids:
                entry = self._entries[model_id]
                was_available = entry.is_currently_available
                entry.mark_missing(provider_id=provider_id)
                if was_available and not entry.is_currently_available:
                    missing_ids.add(model_id)

        result = {
            "new": sorted(new_ids),
            "returned": sorted(returned_ids),
            "missing": sorted(missing_ids),
            "total_tracked": len(self._entries),
        }

        # Fire callbacks
        for callback in self._callbacks:
            try:
                callback(provider_id, sorted(new_ids), sorted(missing_ids))
            except Exception:
                logger.exception(
                    "discovery_callback_failed",
                    provider=provider_id,
                )

        return result

    def get_entry(self, canonical_id: str) -> ModelLifecycleEntry | None:
        """Get lifecycle entry for a model."""
        return self._entries.get(canonical_id)

    def get_all_entries(self) -> list[ModelLifecycleEntry]:
        """Get all lifecycle entries."""
        return list(self._entries.values())

    def get_status_summary(self) -> list[dict]:
        """Get status summary for all tracked models with intelligence context."""
        from app.intelligence.stats import stats_aggregator
        from app.intelligence.tags import capability_registry

        result: list[dict] = []

        for entry in self._entries.values():
            # Resolve transport from registry
            transport = self._resolve_transport(entry.canonical_id)
            provider_id = self._resolve_provider_id(entry.canonical_id)

            stats = stats_aggregator.get_model_stats(
                entry.canonical_id,
                provider_id=provider_id,
                transport=transport,
            )
            tags = capability_registry.get_tags(
                entry.canonical_id,
                provider_id=provider_id,
            )

            result.append(
                {
                    **entry.to_dict(),
                    "sample_count": stats.request_count,
                    "success_rate": round(stats.success_rate, 3),
                    "availability_score": round(stats.availability_score, 3),
                    "stability_score": round(stats.stability_score, 3),
                    "tags": sorted(tags),
                    "intelligence_status": "cold_start" if entry.is_cold_start else "established",
                }
            )

        # Also include models in registry that aren't yet tracked
        all_models = unified_registry.list_models()
        tracked_ids = set(self._entries.keys())
        for m in all_models:
            if m["id"] not in tracked_ids:
                canonical_id = m["id"]
                entry = ModelLifecycleEntry(canonical_id)
                entry.mark_available(source="registry_scan")
                self._entries[canonical_id] = entry

                transport = m.get("transport", "api")
                provider_id = m.get("provider_id", "")
                stats = stats_aggregator.get_model_stats(
                    canonical_id, provider_id=provider_id, transport=transport
                )
                tags = capability_registry.get_tags(canonical_id, provider_id)

                result.append(
                    {
                        **entry.to_dict(),
                        "sample_count": stats.request_count,
                        "success_rate": round(stats.success_rate, 3),
                        "availability_score": round(stats.availability_score, 3),
                        "stability_score": round(stats.stability_score, 3),
                        "tags": sorted(tags),
                        "intelligence_status": "cold_start"
                        if entry.is_cold_start
                        else "established",
                    }
                )

        return result

    @staticmethod
    def _resolve_transport(canonical_id: str) -> str:
        """Resolve transport type from canonical model ID."""
        if canonical_id.startswith("browser/"):
            return "browser"
        if canonical_id.startswith("agent/"):
            return "agent"
        if canonical_id.startswith("api/"):
            return "api"
        return "api"

    @staticmethod
    def _resolve_provider_id(canonical_id: str) -> str:
        """Resolve provider ID from canonical model ID."""
        parts = canonical_id.split("/", 2)
        if len(parts) >= 2:
            return parts[1]
        return ""


# Global singleton
model_intelligence_tracker = ModelIntelligenceTracker()
