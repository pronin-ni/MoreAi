"""
DOM Diff Engine — role-level structural comparison between current state and baseline.

Produces structured diff results:
- tag_changed
- role_changed
- attribute_missing
- attribute_changed
- text_hint_changed
- container_changed
- selector_missing
- structure_shifted

Includes drift severity scoring (0.0–1.0) and human-readable summary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.browser.dom.baseline import DOMBaseline


@dataclass(frozen=True, slots=True)
class DriftEvent:
    """A single structural drift event detected between current and baseline."""

    change_type: str  # tag_changed, role_changed, attribute_missing, etc.
    field: str  # which field changed
    baseline_value: str
    current_value: str
    severity: str  # low, medium, high
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_type": self.change_type,
            "field": self.field,
            "baseline_value": self.baseline_value,
            "current_value": self.current_value,
            "severity": self.severity,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class DOMDiffResult:
    """Structured diff between baseline and current DOM state."""

    provider_id: str
    role: str
    has_drift: bool
    drift_events: list[DriftEvent] = field(default_factory=list)
    drift_severity: str = "none"  # none, low, medium, high
    drift_score: float = 0.0  # 0.0–1.0
    human_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "role": self.role,
            "has_drift": self.has_drift,
            "drift_severity": self.drift_severity,
            "drift_score": self.drift_score,
            "human_summary": self.human_summary,
            "drift_events": [e.to_dict() for e in self.drift_events],
            "event_count": len(self.drift_events),
        }


def diff_against_baseline(
    baseline: DOMBaseline,
    current: DOMBaseline,
) -> DOMDiffResult:
    """Compare current DOM state against stored baseline.

    Returns structured diff with severity scoring.
    """
    events: list[DriftEvent] = []

    # 1. Tag change (high severity)
    if baseline.tag_name and current.tag_name:
        if baseline.tag_name != current.tag_name:
            events.append(DriftEvent(
                change_type="tag_changed",
                field="tag_name",
                baseline_value=baseline.tag_name,
                current_value=current.tag_name,
                severity="high",
                description=f"Tag changed from <{baseline.tag_name}> to <{current.tag_name}>",
            ))

    # 2. ARIA role change (high severity)
    if baseline.aria_role and current.aria_role:
        if baseline.aria_role != current.aria_role:
            events.append(DriftEvent(
                change_type="role_changed",
                field="aria_role",
                baseline_value=baseline.aria_role,
                current_value=current.aria_role,
                severity="high",
                description=f"ARIA role changed from '{baseline.aria_role}' to '{current.aria_role}'",
            ))

    # 3. Missing important attributes (medium severity)
    if baseline.placeholder and not current.placeholder:
        events.append(DriftEvent(
            change_type="attribute_missing",
            field="placeholder",
            baseline_value=baseline.placeholder,
            current_value="",
            severity="medium",
            description=f"Placeholder attribute lost: '{baseline.placeholder}'",
        ))

    if baseline.aria_label and not current.aria_label:
        events.append(DriftEvent(
            change_type="attribute_missing",
            field="aria_label",
            baseline_value=baseline.aria_label,
            current_value="",
            severity="medium",
            description=f"ARIA label lost: '{baseline.aria_label}'",
        ))

    # 4. Attribute value changed (medium severity)
    if baseline.placeholder and current.placeholder:
        if _text_differs(baseline.placeholder, current.placeholder):
            events.append(DriftEvent(
                change_type="attribute_changed",
                field="placeholder",
                baseline_value=baseline.placeholder,
                current_value=current.placeholder,
                severity="medium",
                description="Placeholder changed",
            ))

    if baseline.aria_label and current.aria_label:
        if _text_differs(baseline.aria_label, current.aria_label):
            events.append(DriftEvent(
                change_type="attribute_changed",
                field="aria_label",
                baseline_value=baseline.aria_label,
                current_value=current.aria_label,
                severity="medium",
                description="ARIA label changed",
            ))

    # 5. Text hint changed (low-medium severity)
    if baseline.text_summary and current.text_summary:
        if _text_differs(baseline.text_summary, current.text_summary, threshold=0.6):
            events.append(DriftEvent(
                change_type="text_hint_changed",
                field="text_summary",
                baseline_value=baseline.text_summary[:50],
                current_value=current.text_summary[:50],
                severity="low",
                description="Text content hint changed",
            ))

    # 6. Container context changed (medium severity)
    if baseline.parent_tag and current.parent_tag:
        if baseline.parent_tag != current.parent_tag:
            events.append(DriftEvent(
                change_type="container_changed",
                field="parent_tag",
                baseline_value=baseline.parent_tag,
                current_value=current.parent_tag,
                severity="medium",
                description=f"Parent container changed from <{baseline.parent_tag}> to <{current.parent_tag}>",
            ))

    # 7. Selector mismatch (medium severity — selector changed in baseline vs current)
    if baseline.selector and current.selector:
        if baseline.selector != current.selector:
            events.append(DriftEvent(
                change_type="selector_changed",
                field="selector",
                baseline_value=baseline.selector,
                current_value=current.selector,
                severity="medium",
                description="Resolving selector changed",
            ))

    # 8. State expectation changed (low-medium)
    if baseline.is_editable != current.is_editable:
        events.append(DriftEvent(
            change_type="state_changed",
            field="is_editable",
            baseline_value=str(baseline.is_editable),
            current_value=str(current.is_editable),
            severity="medium",
            description=f"Editable state changed: {baseline.is_editable} → {current.is_editable}",
        ))

    if baseline.is_clickable != current.is_clickable:
        events.append(DriftEvent(
            change_type="state_changed",
            field="is_clickable",
            baseline_value=str(baseline.is_clickable),
            current_value=str(current.is_clickable),
            severity="medium",
            description=f"Clickable state changed: {baseline.is_clickable} → {current.is_clickable}",
        ))

    # 9. Sibling count shifted significantly (low severity)
    if baseline.sibling_count > 0 and current.sibling_count > 0:
        ratio = abs(baseline.sibling_count - current.sibling_count) / max(baseline.sibling_count, 1)
        if ratio > 0.5:
            events.append(DriftEvent(
                change_type="structure_shifted",
                field="sibling_count",
                baseline_value=str(baseline.sibling_count),
                current_value=str(current.sibling_count),
                severity="low",
                description=f"Sibling count shifted: {baseline.sibling_count} → {current.sibling_count}",
            ))

    # Compute drift score
    drift_score = _compute_drift_score(events)
    drift_severity = _score_to_severity(drift_score)
    has_drift = len(events) > 0
    human_summary = _human_summary(events, drift_severity)

    return DOMDiffResult(
        provider_id=baseline.provider_id,
        role=baseline.role,
        has_drift=has_drift,
        drift_events=events,
        drift_severity=drift_severity,
        drift_score=drift_score,
        human_summary=human_summary,
    )


def _compute_drift_score(events: list[DriftEvent]) -> float:
    """Compute drift severity score 0.0–1.0."""
    if not events:
        return 0.0

    severity_weights = {"high": 0.4, "medium": 0.2, "low": 0.1}
    total = sum(severity_weights.get(e.severity, 0.1) for e in events)

    # Normalize: cap at 1.0
    return min(total, 1.0)


def _score_to_severity(score: float) -> str:
    if score >= 0.6:
        return "high"
    elif score >= 0.3:
        return "medium"
    elif score > 0.0:
        return "low"
    return "none"


def _human_summary(events: list[DriftEvent], severity: str) -> str:
    if not events:
        return "No drift detected"

    high = [e for e in events if e.severity == "high"]
    medium = [e for e in events if e.severity == "medium"]
    low = [e for e in events if e.severity == "low"]

    parts = []
    if high:
        parts.append(f"{len(high)} high-severity change(s): {', '.join(e.change_type for e in high)}")
    if medium:
        parts.append(f"{len(medium)} medium-severity change(s): {', '.join(e.change_type for e in medium)}")
    if low:
        parts.append(f"{len(low)} low-severity change(s): {', '.join(e.change_type for e in low)}")

    return f"DOM drift [{severity}]: {'; '.join(parts)}"


def _text_differs(a: str, b: str, threshold: float = 0.8) -> bool:
    """Check if two text strings differ significantly.

    Uses simple character overlap ratio.
    """
    if not a or not b:
        return bool(a) != bool(b)

    a_lower = a.lower()
    b_lower = b.lower()

    if a_lower == b_lower:
        return False

    # Simple Jaccard-like similarity
    set_a = set(a_lower)
    set_b = set(b_lower)
    if not set_a or not set_b:
        return True

    overlap = len(set_a & set_b) / len(set_a | set_b)
    return overlap < threshold
