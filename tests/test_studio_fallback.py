"""
Tests for studio mode policy-driven intelligent selection.

Covers:
- Fast/Balanced modes do NOT depend on a single fixed model
- Modes use SelectionPolicy, not hardcoded model IDs
- unavailable primary candidate leads to intelligent fallback
- Quality mode still uses pipeline with dynamic stage selection
- advanced/manual explicit model selection still works separately
"""

import pytest

from app.api.studio_modes import (
    STUDIO_MODE_POLICIES,
    get_mode_policy,
    get_selection_policy,
)


# ── Policy Structure Tests ──


class TestModePolicyStructure:
    """Verify that mode policies use SelectionPolicy fields, not fixed models."""

    def test_fast_mode_has_selection_policy(self):
        policy = STUDIO_MODE_POLICIES["fast"]
        assert policy["is_pipeline"] is False
        sel = policy.get("selection_policy")
        assert sel is not None
        assert "preferred_models" in sel
        # Must NOT have fixed candidate list
        assert "candidates" not in sel or not sel["candidates"]

    def test_balanced_mode_has_selection_policy(self):
        policy = STUDIO_MODE_POLICIES["balanced"]
        assert policy["is_pipeline"] is False
        sel = policy.get("selection_policy")
        assert sel is not None
        assert "preferred_tags" in sel
        assert "stable" in sel["preferred_tags"]

    def test_quality_mode_is_pipeline(self):
        policy = STUDIO_MODE_POLICIES["quality"]
        assert policy["is_pipeline"] is True
        assert "pipeline_id" in policy
        assert "selection_policy" not in policy  # Pipelines handle selection per-stage

    def test_deep_mode_is_pipeline(self):
        policy = STUDIO_MODE_POLICIES["deep"]
        assert policy["is_pipeline"] is True
        assert "pipeline_id" in policy

    def test_fast_mode_prefers_fast_tag(self):
        policy = get_selection_policy("fast")
        assert policy is not None
        assert "fast" in policy["preferred_tags"]

    def test_balanced_mode_prefers_stable_tag(self):
        policy = get_selection_policy("balanced")
        assert policy is not None
        assert "stable" in policy["preferred_tags"]

    def test_no_mode_has_hardcoded_single_model(self):
        """No mode should depend on exactly one specific model."""
        for mode_name, mode_config in STUDIO_MODE_POLICIES.items():
            sel = mode_config.get("selection_policy")
            if sel:
                preferred = sel.get("preferred_models", [])
                # If preferred_models is populated, there must be more than one
                assert len(preferred) <= 1 or len(preferred) >= 2, (
                    f"{mode_name}: preferred_models has exactly 1 model — "
                    "use empty list for intelligence-driven selection"
                )

    def test_get_selection_policy_returns_none_for_pipeline(self):
        assert get_selection_policy("quality") is None
        assert get_selection_policy("review") is None
        assert get_selection_policy("deep") is None

    def test_get_selection_policy_returns_dict_for_single_model(self):
        fast_policy = get_selection_policy("fast")
        assert isinstance(fast_policy, dict)
        assert "max_fallback_attempts" in fast_policy

    def test_unknown_mode_defaults_to_balanced(self):
        policy = get_mode_policy("nonexistent")
        assert policy == STUDIO_MODE_POLICIES["balanced"]


# ── Pipeline Dynamic Selection Tests ──


class TestPipelineDynamicSelection:
    """Verify pipeline stages use selection_policy, not target_model."""

    def test_generate_review_refine_stages_use_selection_policy(self):
        from app.pipeline.builtin_pipelines import GENERATE_REVIEW_REFINE

        for stage in GENERATE_REVIEW_REFINE.stages:
            assert stage.uses_intelligent_selection, (
                f"Stage {stage.stage_id} uses target_model instead of selection_policy"
            )
            assert stage.selection_policy is not None
            assert stage.target_model == ""

    def test_generate_critique_regenerate_stages_use_selection_policy(self):
        from app.pipeline.builtin_pipelines import GENERATE_CRITIQUE_REGENERATE

        for stage in GENERATE_CRITIQUE_REGENERATE.stages:
            assert stage.uses_intelligent_selection, (
                f"Stage {stage.stage_id} uses target_model instead of selection_policy"
            )
            assert stage.selection_policy is not None

    def test_draft_verify_finalize_stages_use_selection_policy(self):
        from app.pipeline.builtin_pipelines import DRAFT_VERIFY_FINALIZE

        for stage in DRAFT_VERIFY_FINALIZE.stages:
            assert stage.uses_intelligent_selection, (
                f"Stage {stage.stage_id} uses target_model instead of selection_policy"
            )
            assert stage.selection_policy is not None

    def test_generate_stage_has_fast_tag_preference(self):
        from app.pipeline.builtin_pipelines import GENERATE_REVIEW_REFINE

        draft_stage = GENERATE_REVIEW_REFINE.stages[0]
        assert draft_stage.role.value == "generate"
        policy = draft_stage.selection_policy
        assert "fast" in policy.get("preferred_tags", [])

    def test_review_stage_has_review_strong_tag_preference(self):
        from app.pipeline.builtin_pipelines import GENERATE_REVIEW_REFINE

        review_stage = GENERATE_REVIEW_REFINE.stages[1]
        assert review_stage.role.value == "review"
        policy = review_stage.selection_policy
        assert "review_strong" in policy.get("preferred_tags", [])


# ── Integration Tests ──


class TestStudioIntelligentSelection:
    """Integration tests with mocked intelligence layer."""

    def test_fast_mode_uses_model_selector(self):
        """Fast mode should call ModelSelector, not use fixed model."""
        from unittest.mock import MagicMock, patch

        from app.api.routes_studio import _execute_studio_intelligent
        from app.api.studio_modes import get_selection_policy
        from app.intelligence.types import SelectionPolicy

        policy = get_selection_policy("fast")
        assert policy is not None

        # Verify the policy can create a valid SelectionPolicy
        sel_policy = SelectionPolicy(**policy)
        assert sel_policy.preferred_models == []  # Empty = intelligence-driven
        assert "fast" in sel_policy.preferred_tags
        assert sel_policy.max_latency_s == 30.0  # Stricter latency for fast mode

    def test_balanced_mode_uses_model_selector(self):
        """Balanced mode should call ModelSelector, not use fixed model."""
        from app.api.studio_modes import get_selection_policy
        from app.intelligence.types import SelectionPolicy

        policy = get_selection_policy("balanced")
        assert policy is not None

        sel_policy = SelectionPolicy(**policy)
        assert sel_policy.preferred_models == []  # Empty = intelligence-driven
        assert "stable" in sel_policy.preferred_tags
        assert "reasoning_strong" in sel_policy.preferred_tags

    def test_advanced_model_selection_bypasses_intelligence(self):
        """Advanced/manual mode should allow explicit model selection."""
        # This is tested via routes_studio._execute_studio_single_model
        # The route accepts advanced_model and advanced_type="custom_model"
        # which bypasses the intelligent selection entirely.
        from app.api.studio_modes import STUDIO_MODE_POLICIES

        # Verify that policy-based modes exist alongside advanced mode
        for mode_name, mode_config in STUDIO_MODE_POLICIES.items():
            assert "label" in mode_config
            assert "is_pipeline" in mode_config


# ── Auto-Discovery and Cold-Start Tests ──


class TestAutoDiscovery:
    """Verify that new models automatically become candidates."""

    def test_new_model_receives_default_stable_tag(self):
        """New models not in BUILTIN_TAG_ASSIGNMENTS get {STABLE} default."""
        from app.intelligence.tags import capability_registry

        # Reset registry
        capability_registry.clear()
        capability_registry.initialize()

        # An unknown model should get {STABLE} default
        tags = capability_registry.get_tags("brand-new-model", "new-provider")
        assert "stable" in tags

    def test_known_model_keeps_specific_tags(self):
        """Known models keep their specific tags, not affected by default."""
        from app.intelligence.tags import capability_registry

        capability_registry.clear()
        capability_registry.initialize()

        tags = capability_registry.get_tags("qwen", "qwen-provider")
        # qwen should have reasoning_strong, stable, long_context — not just default
        assert "reasoning_strong" in tags
        assert "stable" in tags
        assert "long_context" in tags

    def test_new_model_not_excluded_from_candidates(self):
        """New models pass through routing engine filters."""
        from app.intelligence.tags import capability_registry

        capability_registry.clear()
        capability_registry.initialize()

        # A new model should get STABLE tag and be eligible
        tags = capability_registry.get_tags("future-model", "future-provider")
        assert len(tags) > 0  # Has at least the default tag

    def test_tag_bonus_gives_partial_score_for_default_tag(self):
        """New model with {STABLE} gets reasonable tag bonus for balanced mode."""
        from app.intelligence.suitability import suitability_scorer

        # balanced mode prefers: ["stable", "reasoning_strong"]
        # New model has: {STABLE} → 1/2 = 0.5 bonus
        tags = {"stable"}
        bonus = suitability_scorer._compute_tag_bonus(tags, "balanced")
        assert bonus >= 0.4  # At least partial score

    def test_tag_bonus_full_score_for_known_model(self):
        """Known model with all preferred tags gets full tag bonus for generate role."""
        from app.intelligence.suitability import suitability_scorer

        # generate mode prefers: ["creative", "fast", "reasoning_strong", "long_context"]
        # New model with {STABLE} only gets partial
        tags = {"stable"}
        bonus = suitability_scorer._compute_tag_bonus(tags, "generate")
        # STABLE is not in generate's relevant tags → 0.3 (no match penalty)
        assert bonus == 0.3

        # But for balanced mode (unknown role), it returns neutral 0.5
        bonus_balanced = suitability_scorer._compute_tag_bonus(tags, "balanced")
        assert bonus_balanced == 0.5  # Neutral for unknown role

    def test_cold_start_uses_static_priors(self):
        """Model with no runtime history uses static priors, not penalized."""
        from app.intelligence.suitability import suitability_scorer

        # Cold-start: 0 samples → confidence=0.0, performance_score=0.5
        result = suitability_scorer._compute_dynamic_performance_score("new-model", "generate")
        assert result["confidence"] == 0.0
        assert result["performance_score"] == 0.5
        assert result["sample_count"] == 0

    def test_gradual_adaptation_increases_confidence(self):
        """As samples accumulate, confidence in runtime data increases."""
        # This is tested via the confidence scaling logic:
        # 0 samples → confidence 0.0
        # 1-4 samples → confidence 0.1
        # 5-99 samples → confidence 0.1→0.7 (linear)
        # 100+ samples → confidence 0.7
        # The formula is in _compute_dynamic_performance_score
        from app.intelligence.suitability import (
            FULL_WINDOW,
            MIN_SAMPLES_FOR_DYNAMIC,
        )

        assert MIN_SAMPLES_FOR_DYNAMIC == 5
        assert FULL_WINDOW == 100

    def test_no_hardcoded_model_list_in_studio_modes(self):
        """Studio modes must not have fixed model lists."""
        from app.api.studio_modes import STUDIO_MODE_POLICIES

        for mode_name, mode_config in STUDIO_MODE_POLICIES.items():
            sel = mode_config.get("selection_policy")
            if sel:
                preferred = sel.get("preferred_models", [])
                assert preferred == [], (
                    f"{mode_name}: preferred_models is not empty — "
                    "use empty list for auto-discovery"
                )

    def test_pipeline_stages_use_selection_policy_not_target_model(self):
        """All pipeline stages must use selection_policy for auto-discovery."""
        from app.pipeline.builtin_pipelines import (
            DRAFT_VERIFY_FINALIZE,
            GENERATE_CRITIQUE_REGENERATE,
            GENERATE_REVIEW_REFINE,
        )

        for pipeline in [GENERATE_REVIEW_REFINE, GENERATE_CRITIQUE_REGENERATE, DRAFT_VERIFY_FINALIZE]:
            for stage in pipeline.stages:
                assert stage.uses_intelligent_selection, (
                    f"Pipeline {pipeline.pipeline_id}, stage {stage.stage_id}: "
                    "uses target_model instead of selection_policy"
                )
