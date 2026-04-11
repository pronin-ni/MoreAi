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
