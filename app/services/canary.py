"""
Canary / experimental routing hooks.

Provides:
- Marking models as "experimental" in routing
- Controlled rollout (percentage-based traffic splitting)
- Admin-controlled canary promotion
- Auto-rollback on error threshold breach
- Persistent event history
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


class CanaryStatus(Enum):
    DRAFT = "draft"
    CANARY = "canary"
    ROLLOUT = "rollout"
    GA = "ga"
    DEPRECATED = "deprecated"


@dataclass(frozen=True, slots=True)
class CanaryConfig:
    model_id: str
    provider_id: str
    status: CanaryStatus = CanaryStatus.DRAFT
    traffic_percentage: float = 0.0
    error_threshold: float = 0.1
    started_at: float = 0.0
    notes: str = ""

    def is_active(self) -> bool:
        return self.status in (CanaryStatus.CANARY, CanaryStatus.ROLLOUT)

    def should_receive_traffic(self, request_key: str) -> bool:
        if not self.is_active() or self.traffic_percentage <= 0:
            return False
        if self.traffic_percentage >= 100:
            return True
        h = int(hashlib.md5(f"{self.model_id}:{request_key}".encode()).hexdigest(), 16)
        return (h % 100) < self.traffic_percentage


class CanaryRegistry:
    """Registry of canary configurations with persistent event history."""

    def __init__(self) -> None:
        self._canaries: dict[str, CanaryConfig] = {}

    def register(
        self,
        *,
        model_id: str,
        provider_id: str,
        status: CanaryStatus = CanaryStatus.DRAFT,
        traffic_percentage: float = 0.0,
        error_threshold: float = 0.1,
        notes: str = "",
    ) -> CanaryConfig:
        config = CanaryConfig(
            model_id=model_id,
            provider_id=provider_id,
            status=status,
            traffic_percentage=traffic_percentage,
            error_threshold=error_threshold,
            started_at=time.time(),
            notes=notes,
        )
        self._canaries[model_id] = config
        self._record_event("registered", model_id, {"config": self._config_to_dict(config)})
        logger.info(
            "Registered canary model",
            model_id=model_id,
            provider_id=provider_id,
            status=status.value,
            traffic_pct=traffic_percentage,
        )
        return config

    def update(
        self,
        model_id: str,
        *,
        status: CanaryStatus | None = None,
        traffic_percentage: float | None = None,
        error_threshold: float | None = None,
        notes: str | None = None,
    ) -> CanaryConfig | None:
        config = self._canaries.get(model_id)
        if config is None:
            return None

        updates: dict[str, Any] = {"model_id": model_id}
        if status is not None:
            updates["provider_id"] = config.provider_id
            updates["status"] = status
            updates["traffic_percentage"] = config.traffic_percentage
            updates["error_threshold"] = config.error_threshold
            updates["started_at"] = config.started_at
            updates["notes"] = config.notes
            updates["status"] = status
            config = CanaryConfig(**updates)
        if traffic_percentage is not None:
            updates["traffic_percentage"] = traffic_percentage
            if status is None:
                updates["status"] = config.status
                updates["provider_id"] = config.provider_id
                updates["error_threshold"] = config.error_threshold
                updates["started_at"] = config.started_at
                updates["notes"] = config.notes
                config = CanaryConfig(**updates)
            else:
                config = CanaryConfig(**{k: v for k, v in updates.items() if k != "status"} | {"status": status})
        if error_threshold is not None:
            updates["error_threshold"] = error_threshold
            if status is None and traffic_percentage is None:
                updates["status"] = config.status
                updates["provider_id"] = config.provider_id
                updates["started_at"] = config.started_at
                updates["notes"] = config.notes
                config = CanaryConfig(**updates)
        if notes is not None:
            updates["notes"] = notes
            if status is None and traffic_percentage is None and error_threshold is None:
                updates["status"] = config.status
                updates["provider_id"] = config.provider_id
                updates["started_at"] = config.started_at
                config = CanaryConfig(**updates)

        # Simpler approach: rebuild from existing + updates
        data = {
            "model_id": config.model_id,
            "provider_id": config.provider_id,
            "status": status or config.status,
            "traffic_percentage": traffic_percentage if traffic_percentage is not None else config.traffic_percentage,
            "error_threshold": error_threshold if error_threshold is not None else config.error_threshold,
            "started_at": config.started_at,
            "notes": notes if notes is not None else config.notes,
        }
        config = CanaryConfig(**data)
        self._canaries[model_id] = config
        self._record_event("updated", model_id, {"config": self._config_to_dict(config)})
        return config

    def get(self, model_id: str) -> CanaryConfig | None:
        return self._canaries.get(model_id)

    def list_all(self) -> list[CanaryConfig]:
        return list(self._canaries.values())

    def list_active(self) -> list[CanaryConfig]:
        return [c for c in self._canaries.values() if c.is_active()]

    def remove(self, model_id: str) -> bool:
        if model_id in self._canaries:
            del self._canaries[model_id]
            self._record_event("removed", model_id)
            return True
        return False

    def should_route_to_canary(self, model_id: str, request_key: str) -> bool:
        config = self._canaries.get(model_id)
        if config is None:
            return False
        return config.should_receive_traffic(request_key)

    def record_error(self, model_id: str, error_type: str) -> None:
        self._record_event("error", model_id, {"error_type": error_type})
        self._check_auto_rollback(model_id)

    def promote_to_ga(self, model_id: str) -> CanaryConfig | None:
        config = self._canaries.get(model_id)
        if config is None:
            return None
        result = self.update(
            model_id,
            status=CanaryStatus.GA,
            traffic_percentage=100.0,
            notes=f"{config.notes} | Promoted to GA",
        )
        self._record_event("promoted_to_ga", model_id)
        return result

    def rollback(self, model_id: str) -> CanaryConfig | None:
        config = self._canaries.get(model_id)
        if config is None:
            return None
        result = self.update(
            model_id,
            status=CanaryStatus.DRAFT,
            traffic_percentage=0.0,
            notes=f"{config.notes} | Rolled back",
        )
        self._record_event("rollback", model_id, {"reason": "manual"})
        return result

    def get_history(self, model_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        try:
            from app.core.persistent_store import persistent_store
            return persistent_store.get_canary_history(model_id=model_id, limit=limit)
        except Exception:
            return []

    def get_error_rate(self, model_id: str, window_seconds: float = 3600) -> float:
        """Calculate the error rate for a canary model."""
        try:
            from app.core.persistent_store import persistent_store
            events = persistent_store.query_analytics(
                since=time.time() - window_seconds,
                limit=10000,
            )
            model_events = [e for e in events if e.get("model") == model_id]
            if not model_events:
                return 0.0
            errors = sum(1 for e in model_events if e.get("status") == "error")
            return errors / len(model_events)
        except Exception:
            return 0.0

    def _check_auto_rollback(self, model_id: str) -> None:
        """Auto-rollback if error rate exceeds threshold."""
        config = self._canaries.get(model_id)
        if config is None or not config.is_active():
            return

        error_rate = self.get_error_rate(model_id)
        if error_rate > config.error_threshold:
            logger.warning(
                "Canary auto-rollback triggered",
                model_id=model_id,
                error_rate=error_rate,
                threshold=config.error_threshold,
            )
            self.rollback(model_id)
            self._record_event("auto_rollback", model_id, {
                "error_rate": error_rate,
                "threshold": config.error_threshold,
            })

    def _record_event(self, event: str, model_id: str, details: dict[str, Any] | None = None) -> None:
        try:
            from app.core.persistent_store import persistent_store
            persistent_store.record_canary_event(event, model_id, details)
        except Exception:
            pass

    @staticmethod
    def _config_to_dict(config: CanaryConfig) -> dict[str, Any]:
        return {
            "model_id": config.model_id,
            "provider_id": config.provider_id,
            "status": config.status.value,
            "traffic_percentage": config.traffic_percentage,
            "error_threshold": config.error_threshold,
            "started_at": config.started_at,
            "notes": config.notes,
        }


canary_registry = CanaryRegistry()
