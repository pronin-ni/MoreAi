"""Provider capabilities / metadata model.

Each provider can declare its capabilities explicitly so that routing,
diagnostics, and admin tooling can reason about them without inspecting
implementation details.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Explicit declaration of what a provider can do.

    This replaces implicit knowledge scattered across provider
    implementations and enables future routing/diagnostics features.
    """

    provider_id: str
    model_name: str
    display_name: str
    target_url: str

    # Auth
    requires_auth: bool = False
    auth_mode: str | None = None  # "google_oauth", "credentials", or None

    # Interaction
    supports_new_chat: bool = True
    supports_streaming_detection: bool = True
    send_mechanism: str = "button"  # "button", "enter", or "custom"

    # Response extraction
    response_strategy: str = "stability"  # "stability", "generation_flag", "custom"

    # Known UI elements (used by recon and diagnostics)
    input_selectors_hint: tuple[str, ...] = field(default_factory=tuple)
    send_selectors_hint: tuple[str, ...] = field(default_factory=tuple)
    login_wall_selectors_hint: tuple[str, ...] = field(default_factory=tuple)

    # Stability config
    default_stable_threshold: int = 2
    default_timeout_seconds: int = 120

    def to_dict(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "model_name": self.model_name,
            "display_name": self.display_name,
            "target_url": self.target_url,
            "requires_auth": self.requires_auth,
            "auth_mode": self.auth_mode,
            "supports_new_chat": self.supports_new_chat,
            "supports_streaming_detection": self.supports_streaming_detection,
            "send_mechanism": self.send_mechanism,
            "response_strategy": self.response_strategy,
            "default_timeout_seconds": self.default_timeout_seconds,
        }


@dataclass(frozen=True, slots=True)
class ProviderDiagnosticState:
    """Point-in-time diagnostic snapshot for a provider."""

    provider_id: str
    session_valid: bool
    circuit_open: bool = False
    consecutive_failures: int = 0
    last_error: str | None = None
    last_error_kind: str | None = None
    selector_failure_rate: float = 0.0
    auth_failure_count: int = 0
    avg_response_seconds: float = 0.0
    timeout_rate: float = 0.0

    def to_dict(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "session_valid": self.session_valid,
            "circuit_open": self.circuit_open,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "last_error_kind": self.last_error_kind,
            "selector_failure_rate": self.selector_failure_rate,
            "auth_failure_count": self.auth_failure_count,
            "avg_response_seconds": self.avg_response_seconds,
            "timeout_rate": self.timeout_rate,
        }
