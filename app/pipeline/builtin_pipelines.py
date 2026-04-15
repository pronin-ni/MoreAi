"""
Built-in pipeline definitions.

Declares the default Chain-of-Providers pipelines that ship with MoreAI.
Each pipeline is a data-driven definition — no giant if/else logic.

All stages use selection_policy (intelligent dynamic selection)
instead of hardcoded target_model. The intelligence layer picks the
best available candidate at runtime based on availability, latency,
stability, quality scores, and capability tags.
"""

from app.pipeline.types import (
    FailurePolicy,
    InputMapping,
    OutputMode,
    PipelineDefinition,
    PipelineStage,
    StageRole,
)

# ── Selection policy templates ──
# These define the intent for each stage type.

_GENERATE_POLICY = {
    "preferred_models": [],
    "preferred_tags": ["fast", "stable"],
    "avoid_tags": ["experimental"],
    "min_availability": 0.4,
    "max_latency_s": 60.0,
    "fallback_mode": "next_best",
    "max_fallback_attempts": 2,
}

_REVIEW_POLICY = {
    "preferred_models": [],
    "preferred_tags": ["review_strong", "reasoning_strong", "stable"],
    "avoid_tags": ["experimental"],
    "min_availability": 0.4,
    "max_latency_s": 90.0,
    "fallback_mode": "next_best",
    "max_fallback_attempts": 2,
}

_REFINE_POLICY = {
    "preferred_models": [],
    "preferred_tags": ["stable", "reasoning_strong"],
    "avoid_tags": ["experimental"],
    "min_availability": 0.4,
    "max_latency_s": 90.0,
    "fallback_mode": "next_best",
    "max_fallback_attempts": 2,
}

_CRITIQUE_POLICY = {
    "preferred_models": [],
    "preferred_tags": ["review_strong", "reasoning_strong"],
    "avoid_tags": ["experimental"],
    "min_availability": 0.4,
    "max_latency_s": 90.0,
    "fallback_mode": "next_best",
    "max_fallback_attempts": 2,
}

_VERIFY_POLICY = {
    "preferred_models": [],
    "preferred_tags": ["stable", "review_strong"],
    "avoid_tags": ["experimental"],
    "min_availability": 0.5,
    "max_latency_s": 60.0,
    "fallback_mode": "next_best",
    "max_fallback_attempts": 2,
}

# ── generate-review-refine ──
# Stage 1: generate a draft answer
# Stage 2: review and critique the draft
# Stage 3: refine the final answer based on the critique


GENERATE_REVIEW_REFINE = PipelineDefinition(
    pipeline_id="generate-review-refine",
    display_name="Generate → Review → Refine",
    description=(
        "Three-stage pipeline: first model generates a draft, "
        "second model reviews and critiques it, "
        "third model produces an improved final version."
    ),
    enabled=True,
    max_total_time_ms=180_000,
    max_stage_retries=1,
    stages=[
        PipelineStage(
            stage_id="draft",
            role=StageRole.GENERATE,
            selection_policy=_GENERATE_POLICY,
            output_mode=OutputMode.PLAIN_TEXT,
            failure_policy=FailurePolicy.FAIL_ALL,
            max_retries=1,
            prompt_template=None,
        ),
        PipelineStage(
            stage_id="review",
            role=StageRole.REVIEW,
            selection_policy=_REVIEW_POLICY,
            input_mapping=InputMapping(
                include_original_request=True,
                include_previous_output=True,
                custom_prompt_prefix=(
                    "You are a critical reviewer. "
                    "Below is an original request and a draft answer.\n"
                    "Review the draft for: accuracy, completeness, clarity, and correctness.\n"
                    "Provide structured critique with specific improvement suggestions.\n\n"
                    "--- Original Request ---\n{original_request}\n\n"
                    "--- Draft Answer ---\n{previous_output}\n\n"
                    "--- Your Critique ---"
                ),
            ),
            output_mode=OutputMode.PLAIN_TEXT,
            failure_policy=FailurePolicy.FAIL_ALL,
            max_retries=1,
        ),
        PipelineStage(
            stage_id="refine",
            role=StageRole.REFINE,
            selection_policy=_REFINE_POLICY,
            input_mapping=InputMapping(
                include_original_request=True,
                include_previous_output=True,
                include_all_outputs=False,
                custom_prompt_prefix=(
                    "You are improving an answer based on reviewer feedback.\n"
                    "Below is the original request, the draft answer, and the reviewer's critique.\n"
                    "Produce an improved, final answer that addresses all valid criticisms.\n\n"
                    "--- Original Request ---\n{original_request}\n\n"
                    "--- Draft Answer ---\n{draft_output}\n\n"
                    "--- Reviewer Critique ---\n{review_output}\n\n"
                    "--- Improved Final Answer ---"
                ),
            ),
            output_mode=OutputMode.PLAIN_TEXT,
            failure_policy=FailurePolicy.FAIL_ALL,
            max_retries=1,
        ),
    ],
)

# ── generate-critique-regenerate ──
# Stage 1: initial responses from model A
# Stage 2: structured critique from model B
# Stage 3: regenerate from model C incorporating the critique


GENERATE_CRITIQUE_REGENERATE = PipelineDefinition(
    pipeline_id="generate-critique-regenerate",
    display_name="Generate → Critique → Regenerate",
    description=(
        "Three-stage pipeline: first model generates an initial response, "
        "second model provides a structured critique, "
        "third model regenerates the answer from scratch using the critique."
    ),
    enabled=True,
    max_total_time_ms=180_000,
    max_stage_retries=1,
    stages=[
        PipelineStage(
            stage_id="initial",
            role=StageRole.GENERATE,
            selection_policy=_GENERATE_POLICY,
            output_mode=OutputMode.PLAIN_TEXT,
            failure_policy=FailurePolicy.FAIL_ALL,
            max_retries=1,
            prompt_template=None,
        ),
        PipelineStage(
            stage_id="critique",
            role=StageRole.CRITIQUE,
            selection_policy=_CRITIQUE_POLICY,
            input_mapping=InputMapping(
                include_original_request=True,
                include_previous_output=True,
                custom_prompt_prefix=(
                    "You are an expert critic. Analyze the following response critically.\n\n"
                    "--- Original Request ---\n{original_request}\n\n"
                    "--- Initial Response ---\n{previous_output}\n\n"
                    "Identify weaknesses, errors, omissions, and areas for improvement. "
                    "Be specific and actionable.\n\n"
                    "--- Critique ---"
                ),
            ),
            output_mode=OutputMode.PLAIN_TEXT,
            failure_policy=FailurePolicy.FAIL_ALL,
            max_retries=1,
        ),
        PipelineStage(
            stage_id="regenerate",
            role=StageRole.GENERATE,
            selection_policy=_GENERATE_POLICY,
            input_mapping=InputMapping(
                include_original_request=True,
                include_previous_output=False,
                include_all_outputs=False,
                include_stage_summaries=True,
                custom_prompt_prefix=(
                    "Generate a completely new answer to the original request.\n"
                    "Incorporate the following critique to avoid previous mistakes.\n\n"
                    "--- Original Request ---\n{original_request}\n\n"
                    "--- Critique of Previous Response ---\n{critique_notes}\n\n"
                    "--- New Answer ---"
                ),
            ),
            output_mode=OutputMode.PLAIN_TEXT,
            failure_policy=FailurePolicy.FAIL_ALL,
            max_retries=1,
        ),
    ],
)

# ── draft-verify-finalize ──
# Stage 1: produce a draft
# Stage 2: verify correctness / fact-check
# Stage 3: finalize with verified information


DRAFT_VERIFY_FINALIZE = PipelineDefinition(
    pipeline_id="draft-verify-finalize",
    display_name="Draft → Verify → Finalize",
    description=(
        "Three-stage pipeline: first model produces a draft, "
        "second model verifies facts and correctness, "
        "third model produces a clean final answer."
    ),
    enabled=True,
    max_total_time_ms=180_000,
    max_stage_retries=1,
    stages=[
        PipelineStage(
            stage_id="draft",
            role=StageRole.GENERATE,
            selection_policy=_GENERATE_POLICY,
            output_mode=OutputMode.PLAIN_TEXT,
            failure_policy=FailurePolicy.FAIL_ALL,
            max_retries=1,
            prompt_template=None,
        ),
        PipelineStage(
            stage_id="verify",
            role=StageRole.VERIFY,
            selection_policy=_VERIFY_POLICY,
            input_mapping=InputMapping(
                include_original_request=True,
                include_previous_output=True,
                custom_prompt_prefix=(
                    "Verify the following answer for factual correctness, "
                    "logical consistency, and completeness.\n\n"
                    "--- Question ---\n{original_request}\n\n"
                    "--- Answer ---\n{previous_output}\n\n"
                    "List any issues found, or confirm if the answer is correct and complete.\n\n"
                    "--- Verification Result ---"
                ),
            ),
            output_mode=OutputMode.PLAIN_TEXT,
            failure_policy=FailurePolicy.SKIP,
            max_retries=1,
        ),
        PipelineStage(
            stage_id="finalize",
            role=StageRole.REFINE,
            selection_policy=_REFINE_POLICY,
            input_mapping=InputMapping(
                include_original_request=True,
                include_previous_output=True,
                include_all_outputs=False,
                custom_prompt_prefix=(
                    "Produce the final answer to the original question.\n"
                    "Below is the draft and the verification result.\n"
                    "If verification found issues, fix them. "
                    "If verification confirmed correctness, return the draft as-is or polished.\n\n"
                    "--- Question ---\n{original_request}\n\n"
                    "--- Draft Answer ---\n{draft_output}\n\n"
                    "--- Verification Result ---\n{verify_output}\n\n"
                    "--- Final Answer ---"
                ),
            ),
            output_mode=OutputMode.PLAIN_TEXT,
            failure_policy=FailurePolicy.FAIL_ALL,
            max_retries=1,
        ),
    ],
)


# ── explore-and-answer ──
# Exploration pipeline: try cold-start models first, fallback to normal selection
# Uses bandit approach to balance exploration (try new models) vs exploitation (use best models)


_EXPLORE_POLICY = {
    "preferred_models": [],
    "preferred_tags": [],
    "avoid_tags": ["experimental"],
    "min_availability": 0.3,
    "max_latency_s": 60.0,
    "fallback_mode": "next_best",
    "max_fallback_attempts": 3,
    "selection_mode": "explore",
}


EXPLORE_AND_ANSWER = PipelineDefinition(
    pipeline_id="explore-and-answer",
    display_name="Explore → Answer",
    description=(
        "Single-stage exploration pipeline that tries cold-start/novel models first. "
        "Uses multi-armed bandit approach: 20% exploration (try new models), "
        "80% exploitation (use best-ranked models). Falls back to normal selection "
        "if all exploration candidates fail."
    ),
    enabled=True,
    max_total_time_ms=120_000,
    max_stage_retries=1,
    stages=[
        PipelineStage(
            stage_id="explore_and_answer",
            role=StageRole.GENERATE,
            selection_policy=_EXPLORE_POLICY,
            output_mode=OutputMode.PLAIN_TEXT,
            failure_policy=FailurePolicy.FAIL_ALL,
            max_retries=1,
            prompt_template=None,
        ),
    ],
)


# ── search-answer ──
# Web search pipeline: search → fetch content → generate answer with sources


_SEARCH_GENERATE_POLICY = {
    "preferred_models": [],
    "preferred_tags": ["fast", "stable"],
    "avoid_tags": ["experimental"],
    "min_availability": 0.4,
    "max_latency_s": 30.0,
    "fallback_mode": "next_best",
    "max_fallback_attempts": 2,
}

_SEARCH_REVIEW_POLICY = {
    "preferred_models": [],
    "preferred_tags": ["review_strong", "reasoning_strong"],
    "avoid_tags": ["experimental"],
    "min_availability": 0.4,
    "max_latency_s": 60.0,
    "fallback_mode": "next_best",
    "max_fallback_attempts": 2,
}


SEARCH_ANSWER = PipelineDefinition(
    pipeline_id="search-answer",
    display_name="Search → Answer",
    description=(
        "Web search pipeline: expand query, search (DuckDuckGo/SearXNG), "
        "fetch page content, generate answer with citations."
    ),
    enabled=True,
    max_total_time_ms=120_000,
    max_stage_retries=1,
    stages=[
        # Stage 1: Generate with search context
        PipelineStage(
            stage_id="search_generate",
            role=StageRole.GENERATE,
            selection_policy=_SEARCH_GENERATE_POLICY,
            input_mapping=InputMapping(
                include_original_request=True,
                include_previous_output=False,
                custom_prompt_prefix=None,
            ),
            output_mode=OutputMode.PLAIN_TEXT,
            failure_policy=FailurePolicy.FAIL_ALL,
            max_retries=1,
            prompt_template=None,
        ),
        # Stage 2: Optional review/citation check
        PipelineStage(
            stage_id="review_sources",
            role=StageRole.REVIEW,
            selection_policy=_SEARCH_REVIEW_POLICY,
            input_mapping=InputMapping(
                include_original_request=True,
                include_previous_output=True,
                custom_prompt_prefix=(
                    "Review the answer for factual accuracy and citation quality.\n\n"
                    "--- Question ---\n{original_request}\n\n"
                    "--- Generated Answer ---\n{previous_output}\n\n"
                    "Instructions:\n"
                    "- Verify that claims are supported by the source material\n"
                    "- Check that citations are properly formatted\n"
                    "- If sources are missing or incorrect, provide specific corrections\n\n"
                    "--- Review ---"
                ),
            ),
            output_mode=OutputMode.PLAIN_TEXT,
            failure_policy=FailurePolicy.SKIP,
            max_retries=1,
        ),
    ],
)


# ── Registry bootstrap ──

BUILTIN_PIPELINES: list[PipelineDefinition] = [
    GENERATE_REVIEW_REFINE,
    GENERATE_CRITIQUE_REGENERATE,
    DRAFT_VERIFY_FINALIZE,
    EXPLORE_AND_ANSWER,
    SEARCH_ANSWER,
]


def register_builtin_pipelines(registry) -> None:
    """Register all built-in pipelines into the given registry."""
    for pdef in BUILTIN_PIPELINES:
        registry.register(pdef)
