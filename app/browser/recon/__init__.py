"""
Auto-recon recovery for browser providers.

Provides:
- Failure classification (recon-eligible vs retry-only vs fatal)
- ReconPolicy — centralized eligibility, budget, action, stop rules
- ReconManager — orchestration layer with budget/guardrails
- Recon telemetry — metrics and event tracking
"""

from app.browser.recon.failure_classification import FailureCategory, classify_failure
from app.browser.recon.manager import ReconManager, ReconResult, attempt_recon_recovery
from app.browser.recon.policy import (
    ReconActionPolicy,
    ReconBudget,
    ReconPolicy,
    ReconStopConditions,
    recon_policy,
)
from app.browser.recon.telemetry import ReconTelemetry, recon_telemetry

__all__ = [
    "FailureCategory",
    "classify_failure",
    "ReconPolicy",
    "ReconBudget",
    "ReconActionPolicy",
    "ReconStopConditions",
    "recon_policy",
    "ReconManager",
    "ReconResult",
    "attempt_recon_recovery",
    "ReconTelemetry",
    "recon_telemetry",
]
