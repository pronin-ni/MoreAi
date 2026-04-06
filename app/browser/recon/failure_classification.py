"""
Failure classification for auto-recon recovery.

Classifies browser errors into:
- recon_eligible: should trigger recon recovery flow
- retry_only: standard retry without recon
- fatal: non-recoverable, don't attempt recon
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class FailureCategory(Enum):
    RECON_ELIGIBLE = "recon_eligible"
    RETRY_ONLY = "retry_only"
    FATAL = "fatal"


# Recon-eligible error types: UI/DOM drift, element not found, page structure changed
_RECON_ELIGIBLE_ERRORS = frozenset({
    "MessageInputNotFoundError",
    "SendButtonNotFoundError",
    "NewChatButtonNotFoundError",
    "AssistantMessageNotFoundError",
    "ChatNotReadyError",
    "ElementNotFound",
    "LookupError",  # from locator resolver
})

# Error message patterns that indicate recon-eligible failures
_RECON_ELIGIBLE_PATTERNS = frozenset({
    "not found",
    "message input not found",
    "send button not found",
    "assistant message not found",
    "chat not ready",
    "did not expose a usable chat input",
    "did not become ready",
    "no candidate",  # healing found no candidates
    "below threshold",  # healing confidence too low
})

# Fatal error types: should never trigger recon
_FATAL_ERRORS = frozenset({
    # Auth-related
    "BrowserError",  # generic auth/login errors carry this
    # These are checked via message patterns below
})

# Auth/block patterns that disqualify from recon
_FATAL_PATTERNS = frozenset({
    "requires login",
    "require login",
    "blocked message sending with a login wall",
    "response flow is blocked by login",
    "response flow was interrupted by a login wall",
    "did not become ready after",  # auth bootstrap failure
    "login modal did not appear",
    "login form is not available",
    "credential-file auth requires",
    "google auth is required",
    "credentials file",
    "captcha",
    "anti-bot",
    "blocked",
    "access denied",
    "rate limit",
    "too many requests",
    "cancelled",
    "cancellation",
    "browser has been closed",
    "context closed",
    "target page",
    "crash",
    "disconnected",
})

# Retry-only errors: transient, recon won't help
_RETRY_ONLY_ERRORS = frozenset({
    "ExecutionTimeoutError",
    "GenerationTimeoutError",
})

_RETRY_ONLY_PATTERNS = frozenset({
    "timed out",
    "timed out after",
    "response generation timed out",
    "network",
    "navigation",
    "websocket",
    "target closed",
    "page crashed",
})


def classify_failure(
    error_type: str,
    error_message: str,
    details: dict[str, Any] | None = None,
) -> tuple[FailureCategory, str]:
    """Classify a browser failure for recon decision.

    Parameters
    ----------
    error_type : exception class name
    error_message : str(exception)
    details : optional error details dict

    Returns
    -------
    (category, reason) — category determines if recon should be triggered.
    """
    msg_lower = error_message.lower()

    # 1. Check fatal patterns first (auth, block, crash)
    for pattern in _FATAL_PATTERNS:
        if pattern in msg_lower:
            return (
                FailureCategory.FATAL,
                f"fatal pattern: {pattern}",
            )

    # 2. Check fatal error types
    if error_type in _FATAL_ERRORS:
        # BrowserError is generic — check message for auth patterns
        for pattern in _FATAL_PATTERNS:
            if pattern in msg_lower:
                return (
                    FailureCategory.FATAL,
                    f"fatal pattern in {error_type}: {pattern}",
                )

    # 3. Check retry-only errors
    if error_type in _RETRY_ONLY_ERRORS:
        return (
            FailureCategory.RETRY_ONLY,
            f"retry-only error type: {error_type}",
        )
    for pattern in _RETRY_ONLY_PATTERNS:
        if pattern in msg_lower:
            return (
                FailureCategory.RETRY_ONLY,
                f"retry-only pattern: {pattern}",
            )

    # 4. Check recon-eligible error types
    if error_type in _RECON_ELIGIBLE_ERRORS:
        return (
            FailureCategory.RECON_ELIGIBLE,
            f"recon-eligible error type: {error_type}",
        )

    # 5. Check recon-eligible message patterns
    for pattern in _RECON_ELIGIBLE_PATTERNS:
        if pattern in msg_lower:
            return (
                FailureCategory.RECON_ELIGIBLE,
                f"recon-eligible pattern: {pattern}",
            )

    # Default: retry-only (conservative)
    return (
        FailureCategory.RETRY_ONLY,
        f"default: unknown error type {error_type}",
    )
