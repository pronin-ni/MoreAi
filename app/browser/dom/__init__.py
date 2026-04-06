"""
DOM baseline + diff subsystem for browser providers.

Provides:
- DOMBaseline — lightweight structural DOM fingerprints
- DOMDiffResult / DriftEvent — structured diff between baseline and current
- BaselineStore — in-memory storage for baselines and drift events
- DOMDriftTelemetry — metrics for baseline capture and drift detection
"""

from app.browser.dom.baseline import DOMBaseline
from app.browser.dom.diff import DOMDiffResult, DriftEvent, diff_against_baseline
from app.browser.dom.store import BaselineStore, DriftRecord, baseline_store
from app.browser.dom.telemetry import DOMDriftTelemetry, dom_drift_telemetry

__all__ = [
    "DOMBaseline",
    "DOMDiffResult",
    "DriftEvent",
    "diff_against_baseline",
    "BaselineStore",
    "DriftRecord",
    "baseline_store",
    "DOMDriftTelemetry",
    "dom_drift_telemetry",
]
