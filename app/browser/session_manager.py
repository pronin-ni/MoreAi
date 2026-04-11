"""Session / auth management separation.

This module extracts auth-session bootstrap and lifecycle management out of
the message-execution flow so that auth can be handled, recovered, and
diagnosed independently.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.config import settings
from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.browser.base import BrowserProvider
    from app.browser.execution.runtime import WorkerBrowserRuntime

logger = get_logger(__name__)


class AuthMode(Enum):
    """How a provider authenticates."""

    NONE = "none"
    GOOGLE_OAUTH = "google_oauth"
    CREDENTIALS = "credentials"  # email / password file
    STORAGE_STATE = "storage_state"  # pre-existing session file


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Information about an authenticated session."""

    auth_mode: AuthMode
    storage_state_path: str | None
    requires_auth: bool
    provider_id: str
    is_valid: bool = True


class SessionManager:
    """Manages provider authentication sessions.

    Separates auth bootstrap from message execution so that:
    - Auth failures can be diagnosed independently
    - Session invalidation/recovery is centralised
    - Different auth modes (Google OAuth, credentials, storage state) are
      handled uniformly
    """

    def __init__(self) -> None:
        self._auth_bootstrapper = None  # lazy import to avoid circular

    def _get_auth_bootstrapper(self):
        if self._auth_bootstrapper is None:
            from app.browser.auth import auth_bootstrapper
            self._auth_bootstrapper = auth_bootstrapper
        return self._auth_bootstrapper

    def resolve_session(
        self,
        provider_class: type[BrowserProvider],
        provider_config: dict,
        model: str,
    ) -> SessionInfo:
        """Determine the auth session for a provider/model.

        Returns a ``SessionInfo`` describing what auth is needed and where
        the storage state file lives.  Does **not** perform auth — that is
        done lazily by ``ensure_authenticated``.
        """
        storage_state_path = (
            provider_config.get("storage_state_path")
            or settings.auth_storage_state_path
        )
        return SessionInfo(
            auth_mode=self._auth_mode(provider_class),
            storage_state_path=storage_state_path,
            requires_auth=provider_class.requires_auth,
            provider_id=provider_class.provider_id,
        )

    async def ensure_authenticated(
        self,
        provider_class: type[BrowserProvider],
        provider_config: dict,
        model: str,
        runtime: WorkerBrowserRuntime | None = None,
    ) -> str | None:
        """Ensure the provider has a valid session.

        Performs auth bootstrap if needed and returns the storage state path.
        """
        bootstrapper = self._get_auth_bootstrapper()
        return await bootstrapper.ensure_model_authenticated(model, runtime=runtime)

    def invalidate_session(
        self,
        model: str,
    ) -> None:
        """Mark the current session as invalid (delete storage state)."""
        bootstrapper = self._get_auth_bootstrapper()
        bootstrapper.invalidate_model_storage_state(model)

    def has_existing_session(
        self,
        provider_class: type[BrowserProvider],
        provider_config: dict,
    ) -> bool:
        """Check if a valid storage state file already exists."""
        storage_state_path = (
            provider_config.get("storage_state_path")
            or settings.auth_storage_state_path
        )
        if not storage_state_path:
            return False
        return Path(storage_state_path).exists()

    @staticmethod
    def _auth_mode(provider_class: type[BrowserProvider]) -> AuthMode:
        if not provider_class.requires_auth:
            return AuthMode.NONE
        auth_provider = getattr(provider_class, "auth_provider", None)
        if auth_provider == "google":
            return AuthMode.GOOGLE_OAUTH
        if auth_provider == "credentials":
            return AuthMode.CREDENTIALS
        return AuthMode.STORAGE_STATE


session_manager = SessionManager()
