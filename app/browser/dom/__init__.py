"""
DOM baseline + diff + persistence subsystem for browser providers.

Provides:
- DOMBaseline — lightweight structural DOM fingerprints
- DOMDiffResult / DriftEvent — structured diff between baseline and current
- BaselineStore — in-memory storage for baselines and drift events
- PersistentDOMStore — SQLite-backed persistent storage
- SuggestionEngine — selector maintenance suggestions
- SelectorOverrideManager — controlled override layer
- DOMDriftTelemetry — metrics for baseline capture and drift detection
"""

from app.browser.dom.baseline import DOMBaseline
from app.browser.dom.diff import DOMDiffResult, DriftEvent, diff_against_baseline
from app.browser.dom.overrides import SelectorOverrideManager, selector_override_manager
from app.browser.dom.persistent_store import PersistentDOMStore, persistent_dom_store
from app.browser.dom.refresh import BaselineRefresher, baseline_refresher
from app.browser.dom.store import BaselineStore, DriftRecord, baseline_store
from app.browser.dom.suggestions import MaintenanceSuggestion, SuggestionEngine, suggestion_engine
from app.browser.dom.telemetry import DOMDriftTelemetry, dom_drift_telemetry

__all__ = [
    "DOMBaseline",
    "DOMDiffResult",
    "DriftEvent",
    "diff_against_baseline",
    "BaselineStore",
    "DriftRecord",
    "baseline_store",
    "PersistentDOMStore",
    "persistent_dom_store",
    "SuggestionEngine",
    "MaintenanceSuggestion",
    "suggestion_engine",
    "SelectorOverrideManager",
    "selector_override_manager",
    "BaselineRefresher",
    "baseline_refresher",
    "DOMDriftTelemetry",
    "dom_drift_telemetry",
]
