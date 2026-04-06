"""
Access policy — API keys, quotas, and role-based access.

Provides:
- Persistent API key management with secret generation
- Sliding-window quota enforcement
- Model-level access restrictions
- Per-tenant key management
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from app.core.logging import get_logger

logger = get_logger(__name__)


class AccessRole(Enum):
    """Access role for API consumers."""

    ADMIN = "admin"
    USER = "user"
    READONLY = "readonly"
    SANDBOX = "sandbox"


class QuotaPeriod(Enum):
    """Quota measurement window."""

    MINUTE = "minute"
    HOUR = "hour"
    DAY = "day"
    MONTH = "month"


@dataclass(frozen=True, slots=True)
class QuotaConfig:
    """Rate/usage quota configuration."""

    max_requests: int
    period: QuotaPeriod
    max_tokens_per_request: int | None = None
    burst_limit: int | None = None

    def period_seconds(self) -> float:
        mapping = {
            QuotaPeriod.MINUTE: 60,
            QuotaPeriod.HOUR: 3600,
            QuotaPeriod.DAY: 86400,
            QuotaPeriod.MONTH: 86400 * 30,
        }
        return mapping.get(self.period, 3600)


def generate_api_key_secret() -> tuple[str, str]:
    """Generate a new API key secret and its hash.

    Returns
    -------
    (secret, secret_hash) — secret is returned once and never stored.
    """
    secret = f"moreai_{secrets.token_urlsafe(32)}"
    secret_hash = hashlib.sha256(secret.encode()).hexdigest()
    return secret, secret_hash


class QuotaEnforcer(Protocol):
    """Interface for quota enforcement."""

    def check_quota(self, key_id: str, model_id: str, tenant_id: str = "") -> tuple[bool, str | None]: ...
    def record_usage(self, key_id: str, model_id: str, tokens_used: int = 0, tenant_id: str = "") -> None: ...
    def get_usage(self, key_id: str) -> dict[str, Any]: ...


class SlidingWindowQuotaEnforcer:
    """Sliding-window quota enforcement using persistent storage.

    Checks request counts within the configured window and rejects
    requests that exceed the limit.
    """

    def __init__(self) -> None:
        pass

    def check_quota(
        self, key_id: str, model_id: str, tenant_id: str = ""
    ) -> tuple[bool, str | None]:
        """Check if a request is within quota limits."""
        try:
            from app.core.persistent_store import persistent_store

            key_info = persistent_store.get_api_key(key_id)
            if key_info is None:
                return False, f"Unknown API key: {key_id}"

            quotas = key_info.get("quotas", {})
            if not quotas:
                return True, None  # No quotas configured

            now = time.time()

            for period_str, qcfg in quotas.items():
                max_requests = qcfg.get("max_requests")
                if max_requests is None:
                    continue

                period = QuotaPeriod(period_str)
                window_seconds = period.period_seconds()
                since = now - window_seconds

                usage = persistent_store.get_quota_usage(key_id=key_id, since=since)
                total = usage.get("total_requests", 0)

                if total >= max_requests:
                    return False, f"Quota exceeded for period {period_str}: {total}/{max_requests} requests"

            return True, None
        except Exception:
            return True, None  # Fail open if storage is unavailable

    def record_usage(
        self, key_id: str, model_id: str, tokens_used: int = 0, tenant_id: str = ""
    ) -> None:
        try:
            from app.core.persistent_store import persistent_store
            persistent_store.record_quota_usage(
                key_id=key_id,
                model_id=model_id,
                tokens_used=tokens_used,
                tenant_id=tenant_id,
            )
        except Exception:
            pass

    def get_usage(self, key_id: str) -> dict[str, Any]:
        try:
            from app.core.persistent_store import persistent_store
            return persistent_store.get_quota_usage(key_id=key_id)
        except Exception:
            return {"total_requests": 0, "total_tokens": 0, "last_60s": 0, "last_1h": 0, "last_24h": 0}


class AccessPolicyManager:
    """Central access policy manager with persistent storage."""

    def __init__(self) -> None:
        self._quota_enforcer: QuotaEnforcer = SlidingWindowQuotaEnforcer()

    def create_key(
        self,
        *,
        name: str,
        role: AccessRole = AccessRole.USER,
        quotas: dict[str, dict[str, Any]] | None = None,
        allowed_models: list[str] | None = None,
        tenant_id: str = "",
        expires_at: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Create a new API key.

        Returns
        -------
        (key_info, secret) — secret is only returned once.
        """
        key_id = f"key_{secrets.token_urlsafe(12)}"
        secret, secret_hash = generate_api_key_secret()

        key_data = {
            "key_id": key_id,
            "name": name,
            "secret_hash": secret_hash,
            "role": role.value if isinstance(role, AccessRole) else role,
            "quotas": quotas or {},
            "allowed_models": allowed_models or [],
            "tenant_id": tenant_id,
            "created_at": time.time(),
            "expires_at": expires_at,
            "is_active": True,
            "metadata": metadata or {},
        }

        try:
            from app.core.persistent_store import persistent_store
            persistent_store.create_api_key(key_data)
        except Exception as exc:
            logger.error("Failed to persist API key", key_id=key_id, error=str(exc))

        logger.info(
            "Created API key",
            key_id=key_id,
            name=name,
            role=key_data["role"],
        )
        return key_data, secret

    def authenticate(self, secret: str) -> dict[str, Any] | None:
        """Authenticate an API key by its secret.

        Returns key info if valid, None otherwise.
        """
        secret_hash = hashlib.sha256(secret.encode()).hexdigest()
        try:
            from app.core.persistent_store import persistent_store
            key_info = persistent_store.lookup_api_key_by_secret(secret_hash)
            if key_info is None:
                return None

            if not key_info["is_active"]:
                return None

            if key_info.get("expires_at") and time.time() > key_info["expires_at"]:
                return None

            # Update last_used_at
            persistent_store.update_api_key(key_info["key_id"], {"last_used_at": time.time()})

            return key_info
        except Exception:
            return None

    def get_key(self, key_id: str) -> dict[str, Any] | None:
        try:
            from app.core.persistent_store import persistent_store
            return persistent_store.get_api_key(key_id)
        except Exception:
            return None

    def list_keys(self, *, tenant_id: str | None = None) -> list[dict[str, Any]]:
        try:
            from app.core.persistent_store import persistent_store
            return persistent_store.list_api_keys(tenant_id=tenant_id)
        except Exception:
            return []

    def update_key(self, key_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        try:
            from app.core.persistent_store import persistent_store
            return persistent_store.update_api_key(key_id, updates)
        except Exception:
            return None

    def delete_key(self, key_id: str) -> bool:
        try:
            from app.core.persistent_store import persistent_store
            return persistent_store.delete_api_key(key_id)
        except Exception:
            return False

    def check_access(
        self,
        key_id: str,
        model_id: str,
        tenant_id: str = "",
    ) -> tuple[bool, str | None]:
        """Check if a key can access a model."""
        key_info = self.get_key(key_id)
        if key_info is None:
            return False, f"Unknown API key: {key_id}"

        if not key_info.get("is_active", False):
            return False, f"API key is not active: {key_id}"

        if key_info.get("expires_at") and time.time() > key_info["expires_at"]:
            return False, f"API key has expired: {key_id}"

        # Check model access
        allowed = key_info.get("allowed_models", [])
        if allowed and model_id not in allowed:
            return False, f"API key {key_id} cannot access model {model_id}"

        # Check quota
        allowed_quota, reason = self._quota_enforcer.check_quota(key_id, model_id, tenant_id)
        if not allowed_quota:
            return False, reason

        return True, None

    def record_usage(self, key_id: str, model_id: str, tokens_used: int = 0, tenant_id: str = "") -> None:
        self._quota_enforcer.record_usage(key_id, model_id, tokens_used, tenant_id)

    def get_usage(self, key_id: str) -> dict[str, Any]:
        return self._quota_enforcer.get_usage(key_id)

    def get_quota_enforcer(self) -> QuotaEnforcer:
        return self._quota_enforcer

    # ── Tenant management ──

    def create_tenant(
        self,
        *,
        tenant_id: str,
        name: str,
        allowed_models: list[str] | None = None,
        hidden_models: list[str] | None = None,
        budget_monthly_tokens: int = 0,
        budget_monthly_requests: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        tenant_data = {
            "tenant_id": tenant_id,
            "name": name,
            "allowed_models": allowed_models or [],
            "hidden_models": hidden_models or [],
            "budget_monthly_tokens": budget_monthly_tokens,
            "budget_monthly_requests": budget_monthly_requests,
            "metadata": metadata or {},
        }
        try:
            from app.core.persistent_store import persistent_store
            return persistent_store.create_tenant(tenant_data)
        except Exception as exc:
            logger.error("Failed to create tenant", tenant_id=tenant_id, error=str(exc))
            raise

    def get_tenant(self, tenant_id: str) -> dict[str, Any] | None:
        try:
            from app.core.persistent_store import persistent_store
            return persistent_store.get_tenant(tenant_id)
        except Exception:
            return None

    def list_tenants(self) -> list[dict[str, Any]]:
        try:
            from app.core.persistent_store import persistent_store
            return persistent_store.list_tenants()
        except Exception:
            return []

    def update_tenant(self, tenant_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        try:
            from app.core.persistent_store import persistent_store
            return persistent_store.update_tenant(tenant_id, updates)
        except Exception:
            return None

    def delete_tenant(self, tenant_id: str) -> bool:
        try:
            from app.core.persistent_store import persistent_store
            return persistent_store.delete_tenant(tenant_id)
        except Exception:
            return False


access_policy = AccessPolicyManager()
