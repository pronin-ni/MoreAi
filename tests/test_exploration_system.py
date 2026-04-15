"""
Tests for exploration system:
- Exploration pipeline success/fallback
- Bandit selection distribution
- Cold-start model selection
- No hardcoded models in pipelines
"""

import os
import pytest

# Ensure OPENCODE env var doesn't interfere with pydantic settings loading
os.environ.pop("OPENCODE", None)

from app.intelligence.selection import ModelSelector, RANKING_WEIGHTS
from app.intelligence.types import (
    SelectionPolicy,
    SelectionMode,
    FallbackMode,
)
from app.pipeline.builtin_pipelines import (
    BUILTIN_PIPELINES,
    EXPLORE_AND_ANSWER,
    GENERATE_REVIEW_REFINE,
)
from app.api.studio_modes import STUDIO_MODE_POLICIES


class TestExplorationPipeline:
    """Tests for exploration pipeline behavior."""

    def test_explore_and_answer_pipeline_exists(self):
        """Verify explore-and-answer pipeline is defined."""
        assert EXPLORE_AND_ANSWER is not None
        assert EXPLORE_AND_ANSWER.pipeline_id == "explore-and-answer"
        assert len(EXPLORE_AND_ANSWER.stages) == 1
        assert EXPLORE_AND_ANSWER.stages[0].role.value == "generate"

    def test_explore_pipeline_uses_exploration_mode(self):
        """Verify explore pipeline uses selection_mode=explore."""
        stage = EXPLORE_AND_ANSWER.stages[0]
        assert stage.selection_policy is not None
        assert stage.selection_policy.get("selection_mode") == "explore"


class TestExplorationFallback:
    """Tests for fallback behavior when exploration fails."""

    def test_selector_has_fallback_method(self):
        """Verify ModelSelector has fallback method."""
        selector = ModelSelector()
        assert hasattr(selector, "fallback")
        assert callable(selector.fallback)

    def test_fallback_excludes_failed_model(self):
        """Verify fallback selects next best excluding failed model."""
        from app.intelligence.types import CandidateRanking, SelectionTrace

        selector = ModelSelector()

        # Create mock trace with candidates
        candidates = [
            CandidateRanking(
                model_id="model_a", provider_id="p1", transport="api", canonical_id="model_a"
            ),
            CandidateRanking(
                model_id="model_b", provider_id="p2", transport="api", canonical_id="model_b"
            ),
            CandidateRanking(
                model_id="model_c", provider_id="p3", transport="api", canonical_id="model_c"
            ),
        ]
        for i, c in enumerate(candidates):
            c.rank = i + 1

        trace = SelectionTrace(
            stage_id="test",
            stage_role="generate",
            selected_model="model_a",
            selected_provider="p1",
            selected_transport="api",
            all_candidates=candidates,
        )

        policy = SelectionPolicy(
            fallback_mode=FallbackMode.NEXT_BEST,
            max_fallback_attempts=2,
        )

        # Call fallback after model_a fails
        result = selector.fallback(
            selection_trace=trace,
            policy=policy,
            failed_model="model_a",
            failed_reason="timeout",
            stage_role="generate",
        )

        # Should select model_b as fallback
        assert result is not None
        assert result.selected_model == "model_b"
        assert "model_a" in [e["failed_model"] for e in result.fallback_chain]


class TestBanditSelection:
    """Tests for multi-armed bandit selection distribution."""

    def test_selector_has_should_explore_method(self):
        """Verify ModelSelector has bandit exploration logic."""
        selector = ModelSelector()
        assert hasattr(selector, "_should_explore")

    def test_selection_mode_weights_defined(self):
        """Verify objective-based weights are defined."""
        from app.intelligence.types import SELECTION_MODE_WEIGHTS

        assert SelectionMode.FAST in SELECTION_MODE_WEIGHTS
        assert SelectionMode.BALANCED in SELECTION_MODE_WEIGHTS
        assert SelectionMode.QUALITY in SELECTION_MODE_WEIGHTS
        assert SelectionMode.DEEP in SELECTION_MODE_WEIGHTS
        assert SelectionMode.EXPLORE in SELECTION_MODE_WEIGHTS

    def test_fast_mode_prioritizes_latency(self):
        """Verify FAST mode weights latency heavily."""
        from app.intelligence.types import SELECTION_MODE_WEIGHTS

        weights = SELECTION_MODE_WEIGHTS[SelectionMode.FAST]
        assert weights["latency"] == 0.6
        assert weights["success_rate"] == 0.3
        assert weights["quality"] == 0.1

    def test_quality_mode_prioritizes_quality(self):
        """Verify QUALITY mode weights quality heavily."""
        from app.intelligence.types import SELECTION_MODE_WEIGHTS

        weights = SELECTION_MODE_WEIGHTS[SelectionMode.QUALITY]
        assert weights["latency"] == 0.1
        assert weights["success_rate"] == 0.3
        assert weights["quality"] == 0.6

    def test_explore_mode_prioritizes_novelty(self):
        """Verify EXPLORE mode has novelty bonus."""
        from app.intelligence.types import SELECTION_MODE_WEIGHTS

        weights = SELECTION_MODE_WEIGHTS[SelectionMode.EXPLORE]
        assert "novelty" in weights
        assert weights["novelty"] == 0.6


class TestColdStartModels:
    """Tests for cold-start model handling."""

    def test_model_lifecycle_entry_has_exploration_fields(self):
        """Verify ModelLifecycleEntry tracks exploration."""
        from app.intelligence.tracker import ModelLifecycleEntry

        entry = ModelLifecycleEntry("test_model")

        assert hasattr(entry, "exploration_attempts")
        assert hasattr(entry, "successful_explorations")
        assert hasattr(entry, "is_cold_start")

    def test_record_exploration_attempt(self):
        """Verify recording exploration attempts."""
        from app.intelligence.tracker import ModelLifecycleEntry

        entry = ModelLifecycleEntry("test_model")

        entry.record_exploration_attempt(success=True)
        assert entry.exploration_attempts == 1
        assert entry.successful_explorations == 1

        entry.record_exploration_attempt(success=False)
        assert entry.exploration_attempts == 2
        assert entry.successful_explorations == 1

    def test_get_is_cold_start_uses_sample_count(self):
        """Verify cold-start check uses sample count."""
        from app.intelligence.tracker import ModelLifecycleEntry

        entry = ModelLifecycleEntry("test_model")
        entry.is_cold_start = True  # Start as cold

        # Should be cold-start with low sample count
        assert entry.get_is_cold_start(3) == True  # 3 < threshold(5)

        # Should exit cold-start with sufficient samples (mock behavior)


class TestNoHardcodedModels:
    """Tests verifying no hardcoded model lists in pipelines."""

    def test_no_hardcoded_models_in_builtin_pipelines(self):
        """Verify builtin pipelines don't have hardcoded preferred_models."""
        for pipeline in BUILTIN_PIPELINES:
            for stage in pipeline.stages:
                if stage.selection_policy:
                    preferred = stage.selection_policy.get("preferred_models", [])
                    assert preferred == [], (
                        f"Pipeline {pipeline.pipeline_id} stage {stage.stage_id} "
                        f"has hardcoded preferred_models: {preferred}"
                    )

    def test_no_hardcoded_models_in_studio_modes(self):
        """Verify studio modes don't have hardcoded models."""
        for mode, config in STUDIO_MODE_POLICIES.items():
            if "selection_policy" in config:
                preferred = config["selection_policy"].get("preferred_models", [])
                assert preferred == [], (
                    f"Studio mode '{mode}' has hardcoded preferred_models: {preferred}"
                )

    def test_all_pipelines_use_selection_policy(self):
        """Verify all pipeline stages use selection_policy."""
        for pipeline in BUILTIN_PIPELINES:
            for stage in pipeline.stages:
                assert stage.selection_policy is not None, (
                    f"Pipeline {pipeline.pipeline_id} stage {stage.stage_id} "
                    "missing selection_policy"
                )


class TestSelectionTraceExplorationFields:
    """Tests for SelectionTrace exploration fields."""

    def test_selection_trace_has_is_exploration_field(self):
        """Verify SelectionTrace tracks exploration selection."""
        from app.intelligence.types import SelectionTrace

        trace = SelectionTrace(stage_id="test", stage_role="generate")

        assert hasattr(trace, "is_exploration")
        assert trace.is_exploration == False

    def test_selection_trace_has_selection_reason_field(self):
        """Verify SelectionTrace has selection reason."""
        from app.intelligence.types import SelectionTrace

        trace = SelectionTrace(stage_id="test", stage_role="generate")

        assert hasattr(trace, "selection_reason")
        assert trace.selection_reason == ""


class TestConfigExplorationSettings:
    """Tests for exploration configuration."""

    def test_pipeline_settings_have_exploration_rate(self):
        """Verify PipelineSettings has exploration_rate."""
        from app.core.config import settings

        assert hasattr(settings.pipeline, "exploration_rate")
        assert settings.pipeline.exploration_rate == 0.2

    def test_pipeline_settings_have_cold_start_threshold(self):
        """Verify PipelineSettings has cold_start_threshold."""
        from app.core.config import settings

        assert hasattr(settings.pipeline, "cold_start_threshold")
        assert settings.pipeline.cold_start_threshold == 5

    def test_pipeline_settings_have_exploration_min_successes(self):
        """Verify PipelineSettings has exploration_min_successes."""
        from app.core.config import settings

        assert hasattr(settings.pipeline, "exploration_min_successes")
        assert settings.pipeline.exploration_min_successes == 8
