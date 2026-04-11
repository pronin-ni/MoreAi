"""
Selector maintenance suggestions engine.

Generates suggestions based on:
- Repeated healing successes
- Promoted selectors
- Repeated drift events
- Recon recoveries
- Stable new candidates
- Baseline changes over time

Each suggestion has:
- provider_id, role
- current_selector, suggested_selector
- reason(s), evidence summary
- confidence, times_observed
- status: pending / approved / rejected / dismissed / superseded
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.browser.dom.persistent_store import persistent_dom_store
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class MaintenanceSuggestion:
    """A selector maintenance suggestion."""

    provider_id: str
    role: str
    current_selector: str
    suggested_selector: str
    reason: str
    evidence: list[dict[str, Any]] = field(default_factory=list)
    confidence: float = 0.0
    times_observed: int = 1
    first_seen: float = 0.0
    last_seen: float = 0.0
    status: str = "pending"

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "role": self.role,
            "current_selector": self.current_selector,
            "suggested_selector": self.suggested_selector,
            "reason": self.reason,
            "evidence": self.evidence,
            "confidence": self.confidence,
            "times_observed": self.times_observed,
            "first_seen": round(self.first_seen, 1),
            "last_seen": round(self.last_seen, 1),
            "status": self.status,
        }


class SuggestionEngine:
    """Generates and manages selector maintenance suggestions."""

    def __init__(self) -> None:
        # In-memory tracking for deduplication
        self._tracked: dict[tuple[str, str, str], dict[str, Any]] = {}
        # (provider_id, role, suggested_selector) -> {count, first_seen, last_seen, evidence}

    def record_healing_success(
        self,
        provider_id: str,
        role: str,
        selector: str,
        confidence: float,
        current_selector: str = "",
    ) -> None:
        """Record a successful healing with a specific selector."""
        key = (provider_id, role, selector)
        tracked = self._tracked.setdefault(
            key,
            {
                "count": 0,
                "first_seen": time.monotonic(),
                "last_seen": 0.0,
                "avg_confidence": 0.0,
                "evidence": [],
                "current_selector": current_selector,
            },
        )
        tracked["count"] += 1
        tracked["last_seen"] = time.monotonic()
        # Running average
        old_avg = tracked["avg_confidence"]
        tracked["avg_confidence"] = (
            (old_avg * (tracked["count"] - 1) + confidence) / tracked["count"]
        )
        tracked["evidence"].append({
            "type": "healing_success",
            "confidence": round(confidence, 3),
            "timestamp": time.monotonic(),
        })

        # Generate suggestion if threshold met
        if tracked["count"] >= 5:
            self._generate_suggestion(
                provider_id=provider_id,
                role=role,
                current_selector=current_selector,
                suggested_selector=selector,
                reason=f"healed selector succeeded {tracked['count']} times",
                evidence=tracked["evidence"][-5:],
                confidence=tracked["avg_confidence"],
                times_observed=tracked["count"],
            )

    def record_drift(
        self,
        provider_id: str,
        role: str,
        severity: str,
        current_selector: str = "",
        suggested_selector: str = "",
    ) -> None:
        """Record a drift event for potential suggestion generation."""
        if severity != "high" or not suggested_selector:
            return

        key = (provider_id, role, suggested_selector)
        tracked = self._tracked.setdefault(
            key,
            {
                "count": 0,
                "first_seen": time.monotonic(),
                "last_seen": 0.0,
                "avg_confidence": 0.0,
                "evidence": [],
                "current_selector": current_selector,
            },
        )
        tracked["count"] += 1
        tracked["last_seen"] = time.monotonic()
        tracked["evidence"].append({
            "type": "drift_detected",
            "severity": severity,
            "timestamp": time.monotonic(),
        })

        if tracked["count"] >= 3:
            self._generate_suggestion(
                provider_id=provider_id,
                role=role,
                current_selector=current_selector,
                suggested_selector=suggested_selector,
                reason=f"baseline drift detected {tracked['count']} times",
                evidence=tracked["evidence"][-5:],
                confidence=0.7,
                times_observed=tracked["count"],
            )

    def record_promotion(
        self,
        provider_id: str,
        role: str,
        selector: str,
        current_selector: str = "",
    ) -> None:
        """Record a promoted selector (from runtime promotion)."""
        key = (provider_id, role, selector)
        tracked = self._tracked.setdefault(
            key,
            {
                "count": 0,
                "first_seen": time.monotonic(),
                "last_seen": 0.0,
                "avg_confidence": 0.0,
                "evidence": [],
                "current_selector": current_selector,
            },
        )
        tracked["count"] += 1
        tracked["last_seen"] = time.monotonic()
        tracked["evidence"].append({
            "type": "selector_promoted",
            "timestamp": time.monotonic(),
        })

        # Promoted selectors are strong candidates for maintenance
        self._generate_suggestion(
            provider_id=provider_id,
            role=role,
            current_selector=current_selector,
            suggested_selector=selector,
            reason="promoted selector consistently wins over current primary",
            evidence=tracked["evidence"],
            confidence=0.85,
            times_observed=tracked["count"],
        )

    def _generate_suggestion(
        self,
        provider_id: str,
        role: str,
        current_selector: str,
        suggested_selector: str,
        reason: str,
        evidence: list[dict[str, Any]],
        confidence: float,
        times_observed: int,
    ) -> None:
        """Create or update a suggestion in persistent storage."""
        # Check if we already have a similar pending suggestion
        existing = persistent_dom_store.get_suggestions(
            provider_id=provider_id, status="pending", limit=10
        )
        for s in existing:
            if s["role"] == role and s["suggested_selector"] == suggested_selector:
                # Update existing
                persistent_dom_store.update_suggestion(
                    s["id"],
                    {
                        "times_observed": s["times_observed"] + 1,
                        "evidence": s["evidence"] + evidence[-3:],
                        "confidence": round((s["confidence"] + confidence) / 2, 3),
                    },
                )
                logger.debug(
                    "Maintenance suggestion updated",
                    provider_id=provider_id,
                    role=role,
                    selector=suggested_selector,
                    times_observed=s["times_observed"] + 1,
                )
                return

        # Create new
        data = {
            "provider_id": provider_id,
            "role": role,
            "current_selector": current_selector,
            "suggested_selector": suggested_selector,
            "reason": reason,
            "evidence": evidence[-5:],
            "confidence": confidence,
            "times_observed": times_observed,
            "status": "pending",
        }
        suggestion_id = persistent_dom_store.save_suggestion(data)
        if suggestion_id:
            logger.info(
                "Maintenance suggestion generated",
                provider_id=provider_id,
                role=role,
                reason=reason,
                confidence=confidence,
            )

    def get_pending_suggestions(
        self,
        provider_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return persistent_dom_store.get_suggestions(
            status="pending",
            provider_id=provider_id,
            limit=limit,
        )

    def approve_suggestion(self, suggestion_id: int, override_selector: str = "") -> bool:
        """Approve a suggestion and create an override."""
        suggestion = persistent_dom_store.get_suggestion(suggestion_id)
        if not suggestion:
            return False

        # Update suggestion status
        persistent_dom_store.update_suggestion(
            suggestion_id,
            {"status": "approved", "override_selector": override_selector or suggestion["suggested_selector"]},
        )

        # Create override
        persistent_dom_store.save_override({
            "provider_id": suggestion["provider_id"],
            "role": suggestion["role"],
            "selector": override_selector or suggestion["suggested_selector"],
            "source": "approved",
            "suggestion_id": suggestion_id,
            "created_by": "operator",
        })

        logger.info(
            "Maintenance suggestion approved",
            suggestion_id=suggestion_id,
            provider_id=suggestion["provider_id"],
            role=suggestion["role"],
        )
        return True

    def reject_suggestion(self, suggestion_id: int) -> bool:
        return persistent_dom_store.update_suggestion(suggestion_id, {"status": "rejected"})

    def dismiss_suggestion(self, suggestion_id: int) -> bool:
        return persistent_dom_store.update_suggestion(suggestion_id, {"status": "dismissed"})


suggestion_engine = SuggestionEngine()
