"""
RuntimeOverrides model and ConfigManager.

ConfigManager is responsible for:
- Storing runtime overrides (deltas over base Settings)
- Validating overrides against known providers/models
- Persisting to config/admin.json
- Versioning and history (for rollback)
- Publishing change events to subscribers

It does NOT apply changes to live components — that's RuntimeConfigApplier's job.
"""

import asyncio
import copy
import json
import time
from collections import deque
from pathlib import Path
from typing import Any

from app.core.errors import BadRequestError
from app.core.logging import get_logger

logger = get_logger(__name__)

# ── Override models ──


class ProviderOverride:
    """Delta override for a single provider."""

    def __init__(
        self,
        enabled: bool | None = None,
        concurrency_limit: int | None = None,
        priority: int | None = None,
    ):
        self.enabled = enabled
        self.concurrency_limit = concurrency_limit
        self.priority = priority
        self.applied_at: float | None = None
        self.applied_by: str | None = None
        self.state: str = "pending"  # pending|validated|applied|apply_failed|restart_required
        self.error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "concurrency_limit": self.concurrency_limit,
            "priority": self.priority,
            "applied_at": self.applied_at,
            "applied_by": self.applied_by,
            "state": self.state,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ProviderOverride:
        obj = cls(
            enabled=data.get("enabled"),
            concurrency_limit=data.get("concurrency_limit"),
            priority=data.get("priority"),
        )
        obj.applied_at = data.get("applied_at")
        obj.applied_by = data.get("applied_by")
        obj.state = data.get("state", "pending")
        obj.error = data.get("error")
        return obj


class ModelOverride:
    """Delta override for a single model."""

    def __init__(
        self,
        enabled: bool | None = None,
        visibility: str | None = None,
        force_provider: str | None = None,
    ):
        self.enabled = enabled
        self.visibility = visibility
        self.force_provider = force_provider
        self.applied_at: float | None = None
        self.applied_by: str | None = None
        self.state: str = "pending"
        self.error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "visibility": self.visibility,
            "force_provider": self.force_provider,
            "applied_at": self.applied_at,
            "applied_by": self.applied_by,
            "state": self.state,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ModelOverride:
        obj = cls(
            enabled=data.get("enabled"),
            visibility=data.get("visibility"),
            force_provider=data.get("force_provider"),
        )
        obj.applied_at = data.get("applied_at")
        obj.applied_by = data.get("applied_by")
        obj.state = data.get("state", "pending")
        obj.error = data.get("error")
        return obj


class RoutingOverride:
    """Delta override for routing rules of a model."""

    def __init__(
        self,
        primary: str | None = None,
        fallbacks: list[str] | None = None,
        max_retries: int | None = None,
        timeout_override: int | None = None,
    ):
        self.primary = primary
        self.fallbacks = fallbacks
        self.max_retries = max_retries
        self.timeout_override = timeout_override
        self.applied_at: float | None = None
        self.applied_by: str | None = None
        self.state: str = "pending"
        self.error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary": self.primary,
            "fallbacks": self.fallbacks,
            "max_retries": self.max_retries,
            "timeout_override": self.timeout_override,
            "applied_at": self.applied_at,
            "applied_by": self.applied_by,
            "state": self.state,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict) -> RoutingOverride:
        obj = cls(
            primary=data.get("primary"),
            fallbacks=data.get("fallbacks"),
            max_retries=data.get("max_retries"),
            timeout_override=data.get("timeout_override"),
        )
        obj.applied_at = data.get("applied_at")
        obj.applied_by = data.get("applied_by")
        obj.state = data.get("state", "pending")
        obj.error = data.get("error")
        return obj


class RuntimeOverrides:
    """
    Complete set of runtime overrides (deltas over base Settings).
    Never stores full copies — only the deltas.
    """

    def __init__(self):
        self.version: int = 0
        self.updated_at: float = 0.0
        self.providers: dict[str, ProviderOverride] = {}
        self.models: dict[str, ModelOverride] = {}
        self.routing: dict[str, RoutingOverride] = {}
        # Config state tracking
        self.state: str = "applied"  # pending|validated|applied|apply_failed|restart_required
        self.error: str | None = None
        self.rollback_available: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "updated_at": self.updated_at,
            "providers": {k: v.to_dict() for k, v in self.providers.items()},
            "models": {k: v.to_dict() for k, v in self.models.items()},
            "routing": {k: v.to_dict() for k, v in self.routing.items()},
            "state": self.state,
            "error": self.error,
            "rollback_available": self.rollback_available,
        }

    @classmethod
    def from_dict(cls, data: dict) -> RuntimeOverrides:
        obj = cls()
        obj.version = data.get("version", 0)
        obj.updated_at = data.get("updated_at", 0.0)
        obj.providers = {
            k: ProviderOverride.from_dict(v)
            for k, v in data.get("providers", {}).items()
        }
        obj.models = {
            k: ModelOverride.from_dict(v)
            for k, v in data.get("models", {}).items()
        }
        obj.routing = {
            k: RoutingOverride.from_dict(v)
            for k, v in data.get("routing", {}).items()
        }
        obj.state = data.get("state", "applied")
        obj.error = data.get("error")
        obj.rollback_available = data.get("rollback_available", False)
        return obj

    def is_empty(self) -> bool:
        return not self.providers and not self.models and not self.routing


# ── Config event ──


class ConfigEvent:
    """Published when overrides change."""

    def __init__(
        self,
        event_type: str,
        payload: dict[str, Any],
        version: int,
    ):
        self.type = event_type
        self.payload = payload
        self.version = version
        self.timestamp = time.time()


# ── ConfigManager ──


class ConfigManager:
    """
    Storage, validation, persistence of runtime overrides.

    Does NOT apply changes to live components.
    """

    def __init__(self, config_path: str = "config/admin.json"):
        self._path = Path(config_path)
        self._overrides = RuntimeOverrides()
        self._history: deque[tuple[int, RuntimeOverrides]] = deque(maxlen=20)
        self._subscribers: list[asyncio.Queue[ConfigEvent]] = []
        self._lock = asyncio.Lock()
        self._known_providers: set[str] = set()
        self._known_models: set[str] = set()
        self._load()

    def register_known_providers(self, provider_ids: set[str]) -> None:
        """Called at startup to register known provider IDs for validation."""
        self._known_providers = provider_ids

    def register_known_models(self, model_ids: set[str]) -> None:
        """Called at startup to register known model IDs for validation."""
        self._known_models = model_ids

    # ── Public API ──

    @property
    def overrides(self) -> RuntimeOverrides:
        return self._overrides

    @property
    def current_version(self) -> int:
        return self._overrides.version

    @property
    def state(self) -> str:
        return self._overrides.state

    async def update_provider(
        self, provider_id: str, patch: dict, applied_by: str | None = None
    ) -> ProviderOverride:
        """Update override for a single provider (resource-specific PATCH)."""
        async with self._lock:
            # Save current state to history BEFORE modification
            self._save_to_history()

            existing = self._overrides.providers.get(provider_id, ProviderOverride())

            for field, value in patch.items():
                if hasattr(existing, field):
                    setattr(existing, field, value)

            existing.applied_at = time.time()
            existing.applied_by = applied_by
            existing.state = "pending"

            # Validate
            self._validate_provider_override(provider_id, existing)

            self._overrides.providers[provider_id] = existing
            self._overrides.version += 1
            self._overrides.updated_at = time.time()
            self._overrides.rollback_available = bool(self._history)
            self._persist()
            self._publish("provider_updated", {"provider_id": provider_id})
            return existing

    async def update_model(
        self, model_id: str, patch: dict, applied_by: str | None = None
    ) -> ModelOverride:
        """Update override for a single model."""
        async with self._lock:
            self._save_to_history()

            existing = self._overrides.models.get(model_id, ModelOverride())

            for field, value in patch.items():
                if hasattr(existing, field):
                    setattr(existing, field, value)

            existing.applied_at = time.time()
            existing.applied_by = applied_by
            existing.state = "pending"

            self._validate_model_override(model_id, existing)

            self._overrides.models[model_id] = existing
            self._overrides.version += 1
            self._overrides.updated_at = time.time()
            self._overrides.rollback_available = bool(self._history)
            self._persist()
            self._publish("model_updated", {"model_id": model_id})
            return existing

    async def update_routing(
        self, model_id: str, patch: dict, applied_by: str | None = None
    ) -> RoutingOverride:
        """Update routing override for a single model."""
        async with self._lock:
            self._save_to_history()

            existing = self._overrides.routing.get(model_id, RoutingOverride())

            for field, value in patch.items():
                if hasattr(existing, field):
                    if field == "fallbacks" and isinstance(value, list):
                        setattr(existing, field, value)
                    else:
                        setattr(existing, field, value)

            existing.applied_at = time.time()
            existing.applied_by = applied_by
            existing.state = "pending"

            self._overrides.routing[model_id] = existing
            self._overrides.version += 1
            self._overrides.updated_at = time.time()
            self._overrides.rollback_available = bool(self._history)
            self._persist()
            self._publish("routing_updated", {"model_id": model_id})
            return existing

    async def reset_override(self, resource_type: str, resource_id: str) -> None:
        """Remove override → inherit from base."""
        async with self._lock:
            self._save_to_history()

            if resource_type == "provider":
                self._overrides.providers.pop(resource_id, None)
            elif resource_type == "model":
                self._overrides.models.pop(resource_id, None)
            elif resource_type == "routing":
                self._overrides.routing.pop(resource_id, None)
            else:
                raise BadRequestError(f"Unknown resource type: {resource_type}")

            self._overrides.version += 1
            self._overrides.updated_at = time.time()
            self._overrides.rollback_available = bool(self._history)
            self._persist()
            self._publish(
                "override_reset",
                {"resource_type": resource_type, "resource_id": resource_id},
            )

    def _save_to_history(self) -> None:
        """Save current state to history before making changes."""
        if self._history:
            last_version, _ = self._history[-1]
            self._history.append((last_version, copy.deepcopy(self._overrides)))
        else:
            # First change — save current (empty) state
            self._history.append((0, copy.deepcopy(self._overrides)))

    async def rollback(self, version: int | None = None) -> RuntimeOverrides:
        """Rollback to previous version."""
        async with self._lock:
            if not self._history:
                raise BadRequestError("No history to rollback to")

            if version is None:
                target_version, target_overrides = self._history.pop()
            else:
                found = None
                for _i, (v, o) in enumerate(self._history):
                    if v == version:
                        found = (v, o)
                        break
                if found is None:
                    raise BadRequestError(f"Version {version} not found in history")
                self._history.remove(found)
                target_version, target_overrides = found

            self._overrides = copy.deepcopy(target_overrides)
            self._overrides.state = "pending"
            self._overrides.version = self._overrides.version + 1
            self._overrides.rollback_available = bool(self._history)
            self._persist()
            self._publish(
                "rollback",
                {"from_version": self.current_version - 1, "to_version": target_version},
            )
            return self._overrides

    def get_version(self, version: int) -> RuntimeOverrides | None:
        """Get a specific version from history."""
        for v, o in self._history:
            if v == version:
                return copy.deepcopy(o)
        return None

    def get_history(self) -> list[dict[str, Any]]:
        """Return list of historical versions."""
        return [
            {
                "version": v,
                "updated_at": o.updated_at,
                "state": o.state,
                "provider_count": len(o.providers),
                "model_count": len(o.models),
            }
            for v, o in self._history
        ]

    def subscribe(self) -> asyncio.Queue[ConfigEvent]:
        """Subscribe to config change events."""
        q: asyncio.Queue[ConfigEvent] = asyncio.Queue(maxsize=100)
        self._subscribers.append(q)
        return q

    # ── Internal ──

    def _validate_provider_override(self, provider_id: str, override: ProviderOverride) -> None:
        """Validate provider override against known providers."""
        if self._known_providers and provider_id not in self._known_providers:
            raise BadRequestError(
                f"Unknown provider: {provider_id}. Known: {sorted(self._known_providers)}"
            )

        if override.concurrency_limit is not None:
            if override.concurrency_limit < 1:
                raise BadRequestError("concurrency_limit must be >= 1")
            if override.concurrency_limit > 100:
                raise BadRequestError("concurrency_limit must be <= 100")

    def _validate_model_override(self, model_id: str, override: ModelOverride) -> None:
        """Validate model override."""
        if override.visibility is not None:
            valid = {"public", "hidden", "experimental"}
            if override.visibility not in valid:
                raise BadRequestError(f"visibility must be one of {valid}")

    def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        event = ConfigEvent(
            event_type=event_type,
            payload=payload,
            version=self._overrides.version,
        )
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("Config event dropped — subscriber queue full")

    def _persist(self) -> None:
        """Atomically persist to disk."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        data = self._overrides.to_dict()
        tmp_path.write_text(json.dumps(data, indent=2, default=str))
        tmp_path.rename(self._path)

    def _load(self) -> None:
        """Load from disk if exists."""
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                self._overrides = RuntimeOverrides.from_dict(data)
                logger.info(
                    "Loaded admin config from disk",
                    path=str(self._path),
                    version=self._overrides.version,
                )
            except Exception as exc:
                logger.error(
                    "Failed to load admin config, starting fresh",
                    path=str(self._path),
                    error=str(exc),
                )
                self._overrides = RuntimeOverrides()
        else:
            self._overrides = RuntimeOverrides()


config_manager = ConfigManager()
