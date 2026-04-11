"""
Built-in pipeline definitions.

Declares the default Chain-of-Providers pipelines that ship with MoreAI.
Each pipeline is a data-driven definition — no giant if/else logic.
"""

from app.pipeline.types import (
    FailurePolicy,
    InputMapping,
    OutputMode,
    PipelineDefinition,
    PipelineStage,
    StageRole,
)

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
            target_model="qwen",
            output_mode=OutputMode.PLAIN_TEXT,
            failure_policy=FailurePolicy.FAIL_ALL,
            max_retries=1,
            prompt_template=None,
        ),
        PipelineStage(
            stage_id="review",
            role=StageRole.REVIEW,
            target_model="glm",
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
            target_model="qwen",
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
# Stage 1: initial response from model A
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
            target_model="kimi",
            output_mode=OutputMode.PLAIN_TEXT,
            failure_policy=FailurePolicy.FAIL_ALL,
            max_retries=1,
            prompt_template=None,
        ),
        PipelineStage(
            stage_id="critique",
            role=StageRole.CRITIQUE,
            target_model="glm",
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
            target_model="kimi",
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
            target_model="qwen",
            output_mode=OutputMode.PLAIN_TEXT,
            failure_policy=FailurePolicy.FAIL_ALL,
            max_retries=1,
            prompt_template=None,
        ),
        PipelineStage(
            stage_id="verify",
            role=StageRole.VERIFY,
            target_model="glm",
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
            target_model="qwen",
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

# ── Registry bootstrap ──

BUILTIN_PIPELINES: list[PipelineDefinition] = [
    GENERATE_REVIEW_REFINE,
    GENERATE_CRITIQUE_REGENERATE,
    DRAFT_VERIFY_FINALIZE,
]


def register_builtin_pipelines(registry) -> None:
    """Register all built-in pipelines into the given registry."""
    for pdef in BUILTIN_PIPELINES:
        registry.register(pdef)
