"""
Recon policy — centralized rules for auto-recon recovery.

Separates:
- eligibility rules (when to trigger recon)
- action policy (what recovery actions are allowed)
- budget enforcement (time, scans, reloads, replays)
- stop conditions (when to abort recon)

This replaces scattered if/else logic across executor and recon manager.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.browser.recon.failure_classification import FailureCategory, classify_failure
from app.core.config import settings


@dataclass(frozen=True, slots=True)
class ReconBudget:
    """Enforceable resource limits for a recon cycle."""

    max_time_ms: float = 3000.0
    max_dom_scans: int = 1
    max_page_reloads: int = 1
    max_replay_attempts: int = 1
    candidate_limit: int = 10

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_time_ms": self.max_time_ms,
            "max_dom_scans": self.max_dom_scans,
            "max_page_reloads": self.max_page_reloads,
            "max_replay_attempts": self.max_replay_attempts,
            "candidate_limit": self.candidate_limit,
        }


@dataclass(frozen=True, slots=True)
class ReconActionPolicy:
    """Which recovery actions are allowed during recon."""

    allow_soft_reload: bool = True
    allow_new_chat_recovery: bool = True
    allow_banner_close: bool = True
    allow_input_focus: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "allow_soft_reload": self.allow_soft_reload,
            "allow_new_chat_recovery": self.allow_new_chat_recovery,
            "allow_banner_close": self.allow_banner_close,
            "allow_input_focus": self.allow_input_focus,
        }


@dataclass(frozen=True, slots=True)
class ReconStopConditions:
    """Conditions that immediately abort recon."""

    abort_on_login_wall: bool = True
    abort_on_modal_blockers: bool = True
    abort_on_captcha: bool = True
    abort_on_blocked_page: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "abort_on_login_wall": self.abort_on_login_wall,
            "abort_on_modal_blockers": self.abort_on_modal_blockers,
            "abort_on_captcha": self.abort_on_captcha,
            "abort_on_blocked_page": self.abort_on_blocked_page,
        }


@dataclass
class ReconPolicy:
    """Complete recon policy for a provider.

    Centralizes all rules in one place.
    """

    enabled: bool = True
    budget: ReconBudget = field(default_factory=ReconBudget)
    action_policy: ReconActionPolicy = field(default_factory=ReconActionPolicy)
    stop_conditions: ReconStopConditions = field(default_factory=ReconStopConditions)

    def is_eligible(self, error_type: str, error_message: str) -> tuple[bool, str]:
        """Check if this error should trigger recon.

        Returns (eligible, reason).
        """
        category, reason = classify_failure(error_type, error_message)
        if category == FailureCategory.RECON_ELIGIBLE:
            return True, reason
        return False, f"not recon-eligible: {category.value} — {reason}"

    def should_abort(self, blocking_state: str | None) -> tuple[bool, str]:
        """Check if recon should abort based on detected blocking state.

        Returns (should_abort, reason).
        """
        if not blocking_state:
            return False, ""

        state_lower = blocking_state.lower()

        if self.stop_conditions.abort_on_login_wall and "login" in state_lower:
            return True, f"login wall detected: {blocking_state}"

        if self.stop_conditions.abort_on_modal_blockers and "modal" in state_lower:
            return True, f"modal blocker detected: {blocking_state}"

        if self.stop_conditions.abort_on_captcha and "captcha" in state_lower:
            return True, f"captcha detected: {blocking_state}"

        if self.stop_conditions.abort_on_blocked_page and "blocked" in state_lower:
            return True, f"page blocked: {blocking_state}"

        return False, ""

    def is_action_allowed(self, action_name: str) -> bool:
        """Check if a recovery action is allowed by policy."""
        action_map = {
            "soft_reload": self.action_policy.allow_soft_reload,
            "new_chat_recovery": self.action_policy.allow_new_chat_recovery,
            "banner_close": self.action_policy.allow_banner_close,
            "input_focus": self.action_policy.allow_input_focus,
        }
        return action_map.get(action_name, False)

    @classmethod
    def from_settings(cls) -> ReconPolicy:
        """Build policy from application settings."""
        r = settings.recon
        return cls(
            enabled=r.enabled,
            budget=ReconBudget(
                max_time_ms=r.max_time_ms,
                max_dom_scans=r.max_dom_scans,
                max_page_reloads=r.max_page_reloads,
                max_replay_attempts=r.max_replay_attempts,
                candidate_limit=r.candidate_limit,
            ),
            action_policy=ReconActionPolicy(
                allow_soft_reload=r.allow_soft_reload,
                allow_new_chat_recovery=r.allow_new_chat_recovery,
            ),
            stop_conditions=ReconStopConditions(
                abort_on_login_wall=r.abort_on_login_wall,
                abort_on_modal_blockers=r.abort_on_modal_blockers,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "budget": self.budget.to_dict(),
            "action_policy": self.action_policy.to_dict(),
            "stop_conditions": self.stop_conditions.to_dict(),
        }


# Global default policy instance (reads from settings on construction)
recon_policy = ReconPolicy.from_settings()
