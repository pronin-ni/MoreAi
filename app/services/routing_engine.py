"""
Centralized Smart Routing Engine.

Replaces the ad-hoc routing logic scattered across registry/service layers.
Provides policy-based routing with explainable decisions, cross-transport
fallback, and retry/fallback policy separation.

Architecture:
    RoutingEngine.plan(model_id)
        → collect candidates from all registries
        → apply admin overrides (force_provider, primary, fallbacks)
        → filter by availability, enabled, visibility
        → check circuit breaker state
        → build ordered candidate chain with decision trace
        → return RoutingPlan
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

# ── Data structures ──


@dataclass(slots=True)
class CandidateProvider:
    """A single provider candidate in the routing chain."""

    provider_id: str
    transport: str  # browser | api | agent
    canonical_model_id: str
    enabled: bool
    available: bool
    visibility: str  # public | hidden | experimental

    # Why this candidate was included or excluded
    reason: str = ""  # human-readable explanation
    is_selected: bool = False  # True if this candidate is in the final chain
    selection_rule: str = ""  # primary | force_provider | fallback | cross_transport_fallback
    order: int = 0  # position in the final chain (0 = first)


@dataclass(slots=True)
class RoutingPolicy:
    """Per-model routing policy derived from admin overrides."""

    max_retries_per_provider: int = 1  # how many times to retry the same provider
    timeout_override: int | None = None  # per-model timeout (None = use default)
    cross_transport_fallback: bool = True  # allow fallback to different transport


@dataclass(slots=True)
class RoutingPlan:
    """Complete routing plan for a model request.

    This is the output of RoutingEngine.plan(). It contains:
    - The original model requested
    - The ordered chain of providers to try
    - All candidates considered (including rejected ones)
    - The policy to apply (retries, timeout)
    - A human-readable explanation of the decision
    """

    requested_model: str
    candidates: list[CandidateProvider]  # ordered chain to try
    all_candidates: list[CandidateProvider]  # all considered (including rejected)
    policy: RoutingPolicy
    decision_trace: list[str]  # step-by-step explanation
    created_at: float = field(default_factory=time.monotonic)

    @property
    def primary_provider(self) -> CandidateProvider | None:
        """The first (primary) provider in the chain."""
        return self.candidates[0] if self.candidates else None

    @property
    def has_fallbacks(self) -> bool:
        return len(self.candidates) > 1

    @property
    def transports_in_chain(self) -> list[str]:
        return list(dict.fromkeys(c.transport for c in self.candidates))

    @property
    def is_cross_transport(self) -> bool:
        return len(self.transports_in_chain) > 1

    def summary(self) -> dict[str, Any]:
        return {
            "requested_model": self.requested_model,
            "candidate_count": len(self.candidates),
            "all_considered": len(self.all_candidates),
            "primary_provider": self.primary_provider.provider_id if self.primary_provider else None,
            "primary_transport": self.primary_provider.transport if self.primary_provider else None,
            "has_fallbacks": self.has_fallbacks,
            "is_cross_transport": self.is_cross_transport,
            "transports": self.transports_in_chain,
            "max_retries": self.policy.max_retries_per_provider,
            "timeout_override": self.policy.timeout_override,
            "decision": self.decision_trace,
        }


# ── Routing Engine ──


class RoutingEngine:
    """Centralized routing engine.

    Collects candidates from all registries, applies admin overrides,
    and builds an ordered provider chain with explainable decisions.
    """

    def __init__(self):
        self._call_count: int = 0

    def plan(self, requested_model: str) -> RoutingPlan:
        """Build a routing plan for the requested model.

        Args:
            requested_model: The model ID from the user request (e.g. "browser/qwen").

        Returns:
            RoutingPlan with ordered candidates and decision trace.
        """
        self._call_count += 1
        trace: list[str] = []
        trace.append(f"Routing plan for '{requested_model}' (call #{self._call_count})")

        # Step 1: Resolve the base model from registry
        base_model = self._resolve_base_model(requested_model, trace)

        # Step 2: Collect all possible candidates (cross-transport)
        all_candidates = self._collect_candidates(requested_model, base_model, trace)

        # Step 3: Apply admin overrides (force_provider, primary, fallbacks)
        overrides = self._get_overrides(requested_model)
        filtered_candidates = self._apply_overrides(all_candidates, overrides, trace)

        # Step 4: Build ordered chain
        chain = self._build_chain(filtered_candidates, overrides, trace)

        # Step 5: Build policy
        policy = self._build_policy(overrides)

        if not chain:
            trace.append("ERROR: No available providers for this model")

        return RoutingPlan(
            requested_model=requested_model,
            candidates=chain,
            all_candidates=all_candidates,
            policy=policy,
            decision_trace=trace,
        )

    # ── Internal: model resolution ──

    def _resolve_base_model(self, requested_model: str, trace: list[str]) -> dict | None:
        """Resolve the base model from the unified registry."""
        from app.core.errors import BadRequestError
        from app.registry.unified import unified_registry

        try:
            resolved = unified_registry.resolve_model(requested_model)
            trace.append(
                f"Registry resolved: transport={resolved.transport}, "
                f"provider_id={resolved.provider_id}, canonical_id={resolved.canonical_id}"
            )
            return {
                "provider_id": resolved.provider_id,
                "transport": resolved.transport,
                "canonical_id": resolved.canonical_id,
            }
        except BadRequestError:
            trace.append(f"Registry could not resolve '{requested_model}'")
            return None

    # ── Internal: candidate collection ──

    def _collect_candidates(
        self, requested_model: str, base_model: dict | None, trace: list[str]
    ) -> list[CandidateProvider]:
        """Collect all possible providers for this model across all transports."""
        from app.admin.config_manager import config_manager
        from app.agents.registry import registry as agent_registry
        from app.browser.registry import registry as browser_registry
        from app.integrations.registry import api_registry

        candidates: list[CandidateProvider] = []
        overrides = config_manager.overrides.models

        base_provider_id = (base_model or {}).get("provider_id", "")
        base_canonical_id = (base_model or {}).get("canonical_id", "")

        # 1. Browser candidates
        for m in browser_registry.list_models():
            m_id = m["id"]
            ov = overrides.get(m_id)
            enabled = ov.enabled if ov and ov.enabled is not None else m.get("enabled", True)
            visibility = ov.visibility if ov and ov.visibility else "public"
            # Match by exact ID or provider_id
            if m_id == base_canonical_id or m["provider_id"] == base_provider_id:
                candidates.append(CandidateProvider(
                    provider_id=m["provider_id"],
                    transport="browser",
                    canonical_model_id=m_id,
                    enabled=enabled,
                    available=m.get("available", True),
                    visibility=visibility,
                ))

        # 2. API candidates
        for m in api_registry.list_models():
            m_id = m["id"]
            ov = overrides.get(m_id)
            enabled = ov.enabled if ov and ov.enabled is not None else m.get("enabled", True)
            visibility = ov.visibility if ov and ov.visibility else "public"
            # Match by exact ID or provider_id
            if m_id == base_canonical_id or m["provider_id"] == base_provider_id:
                candidates.append(CandidateProvider(
                    provider_id=m["provider_id"],
                    transport="api",
                    canonical_model_id=m_id,
                    enabled=enabled,
                    available=m.get("available", True),
                    visibility=visibility,
                ))

        # 3. Agent candidates
        for m in agent_registry.list_models():
            m_id = m["id"]
            ov = overrides.get(m_id)
            enabled = ov.enabled if ov and ov.enabled is not None else m.get("enabled", True)
            visibility = ov.visibility if ov and ov.visibility else "public"
            # Match by exact ID or provider_id
            if m_id == base_canonical_id or m["provider_id"] == base_provider_id:
                candidates.append(CandidateProvider(
                    provider_id=m["provider_id"],
                    transport="agent",
                    canonical_model_id=m_id,
                    enabled=enabled,
                    available=m.get("available", True),
                    visibility=visibility,
                ))

        trace.append(f"Collected {len(candidates)} candidates across all transports")

        # If no candidates found from registries, create a synthetic candidate
        # from the resolved model (handles test environments and edge cases)
        if not candidates and base_model:
            candidates.append(CandidateProvider(
                provider_id=base_model["provider_id"],
                transport=base_model["transport"],
                canonical_model_id=base_model["canonical_id"],
                enabled=True,
                available=True,
                visibility="public",
                reason="synthetic from resolved model",
            ))
            trace.append(
                f"No registry matches — created synthetic candidate "
                f"{base_model['transport']}/{base_model['provider_id']}"
            )

        return candidates

    # ── Internal: override application ──

    def _get_overrides(self, model_id: str) -> dict:
        """Get all overrides for a model (model + routing)."""
        from app.admin.config_manager import config_manager

        model_override = config_manager.overrides.models.get(model_id)
        routing_override = config_manager.overrides.routing.get(model_id)
        return {
            "model": model_override,
            "routing": routing_override,
        }

    def _apply_overrides(
        self, candidates: list[CandidateProvider], overrides: dict, trace: list[str]
    ) -> list[CandidateProvider]:
        """Filter candidates by overrides (enabled, visibility, force_provider)."""
        filtered = []
        model_override = overrides.get("model")

        for c in candidates:
            # Skip hidden models
            if c.visibility == "hidden":
                trace.append(f"Excluded {c.transport}/{c.provider_id}: visibility=hidden")
                continue

            # Skip disabled models
            if not c.enabled:
                trace.append(f"Excluded {c.transport}/{c.provider_id}: enabled=false")
                continue

            # Skip unavailable models
            if not c.available:
                trace.append(f"Excluded {c.transport}/{c.provider_id}: available=false")
                continue

            # Check circuit breaker for browser providers
            if c.transport == "browser":
                if self._is_circuit_open(c.provider_id):
                    trace.append(f"Excluded {c.transport}/{c.provider_id}: circuit breaker open")
                    continue

            filtered.append(c)

        trace.append(f"{len(filtered)} candidates remain after override filtering")
        return filtered

    def _is_circuit_open(self, provider_id: str) -> bool:
        """Check if the circuit breaker is open for a browser provider."""
        try:
            from app.browser.execution.dispatcher import browser_dispatcher
            pool = browser_dispatcher._pool
            health_ctrl = pool.provider_health
            if health_ctrl:
                opened_until = health_ctrl._opened_until.get(provider_id)
                if opened_until:
                    return opened_until > time.monotonic()
        except Exception:
            pass
        return False

    # ── Internal: chain building ──

    def _build_chain(
        self,
        candidates: list[CandidateProvider],
        overrides: dict,
        trace: list[str],
    ) -> list[CandidateProvider]:
        """Build the ordered provider chain."""
        if not candidates:
            return []

        model_override = overrides.get("model")
        routing_override = overrides.get("routing")

        chain: list[CandidateProvider] = []
        used_providers: set[str] = set()

        # 1. force_provider — highest priority
        if model_override and model_override.force_provider:
            fp = model_override.force_provider
            trace.append(f"force_provider={fp}")
            force_match = next((c for c in candidates if c.provider_id == fp and c.provider_id not in used_providers), None)
            if force_match:
                force_match.is_selected = True
                force_match.selection_rule = "force_provider"
                force_match.order = len(chain)
                chain.append(force_match)
                used_providers.add(fp)
                trace.append(f"Selected {fp} (force_provider)")
            else:
                trace.append(f"force_provider={fp} not found among candidates")

        # 2. primary — next priority
        if routing_override and routing_override.primary:
            primary = routing_override.primary
            trace.append(f"primary={primary}")
            primary_match = next((c for c in candidates if c.provider_id == primary and c.provider_id not in used_providers), None)
            if primary_match:
                primary_match.is_selected = True
                primary_match.selection_rule = "primary"
                primary_match.order = len(chain)
                chain.append(primary_match)
                used_providers.add(primary)
                trace.append(f"Selected {primary} (primary)")
            else:
                trace.append(f"primary={primary} not found among candidates")

        # 3. Add remaining candidates as default (in order: same transport first, then others)
        # Same transport candidates first
        if chain:
            primary_transport = chain[0].transport
        else:
            primary_transport = candidates[0].transport if candidates else None

        for c in candidates:
            if c.provider_id in used_providers:
                continue
            c.is_selected = True
            c.selection_rule = "default"
            c.order = len(chain)
            chain.append(c)
            used_providers.add(c.provider_id)

        # 4. Explicit fallbacks from routing override
        if routing_override and routing_override.fallbacks:
            for fb_provider_id in routing_override.fallbacks:
                if fb_provider_id in used_providers:
                    continue
                trace.append(f"Adding fallback: {fb_provider_id}")
                fb_match = next((c for c in candidates if c.provider_id == fb_provider_id), None)
                if fb_match:
                    fb_match.is_selected = True
                    fb_match.selection_rule = "fallback"
                    fb_match.order = len(chain)
                    chain.append(fb_match)
                    used_providers.add(fb_provider_id)
                else:
                    # Cross-transport fallback: try to find any provider with this ID
                    cross_match = self._find_cross_transport_candidate(fb_provider_id)
                    if cross_match:
                        cross_match.is_selected = True
                        cross_match.selection_rule = "cross_transport_fallback"
                        cross_match.order = len(chain)
                        chain.append(cross_match)
                        used_providers.add(fb_provider_id)
                        trace.append(f"Cross-transport fallback: {fb_provider_id}")

        if chain:
            trace.append(
                f"Chain built: {' → '.join(f'{c.transport}/{c.provider_id}' for c in chain)}"
            )

        return chain

    def _find_cross_transport_candidate(self, provider_id: str) -> CandidateProvider | None:
        """Find a provider by ID across any transport (for cross-transport fallbacks)."""
        try:
            from app.agents.registry import registry as agent_registry
            from app.browser.registry import registry as browser_registry
            from app.integrations.registry import api_registry

            # Check all registries for this provider_id
            for m in browser_registry.list_models():
                if m["provider_id"] == provider_id:
                    return CandidateProvider(
                        provider_id=m["provider_id"],
                        transport="browser",
                        canonical_model_id=m["id"],
                        enabled=m.get("enabled", True),
                        available=m.get("available", True),
                        visibility="public",
                    )

            for m in api_registry.list_models():
                if m["provider_id"] == provider_id:
                    return CandidateProvider(
                        provider_id=m["provider_id"],
                        transport="api",
                        canonical_model_id=m["id"],
                        enabled=m.get("enabled", True),
                        available=m.get("available", True),
                        visibility="public",
                    )

            for m in agent_registry.list_models():
                if m["provider_id"] == provider_id:
                    return CandidateProvider(
                        provider_id=m["provider_id"],
                        transport="agent",
                        canonical_model_id=m["id"],
                        enabled=m.get("enabled", True),
                        available=m.get("available", True),
                        visibility="public",
                    )
        except Exception:
            pass
        return None

    # ── Internal: policy building ──

    def _build_policy(self, overrides: dict) -> RoutingPolicy:
        """Build the routing policy from overrides."""
        routing_override = overrides.get("routing")
        policy = RoutingPolicy()

        if routing_override and routing_override.max_retries is not None:
            policy.max_retries_per_provider = routing_override.max_retries

        if routing_override and routing_override.timeout_override is not None:
            policy.timeout_override = routing_override.timeout_override

        return policy


# ── Global instance ──

routing_engine = RoutingEngine()
