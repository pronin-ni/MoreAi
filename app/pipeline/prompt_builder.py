"""
Prompt template engine for pipeline stages.

Builds stage prompts from templates with variable substitution.
Keeps handoff between stages controlled and predictable.
"""

from __future__ import annotations

from app.pipeline.types import InputMapping, PipelineContext, StageRole


def _extract_stage_output(context: PipelineContext, stage_id: str) -> str:
    """Get a named stage output from context for template variable substitution."""
    result = context.stage_outputs.get(stage_id)
    if result and result.success:
        return result.output
    return f"[{stage_id} produced no output]"


def build_stage_prompt(
    stage_id: str,
    role: StageRole,
    original_request: str,
    input_mapping: InputMapping,
    prompt_template: str | None,
    context: PipelineContext,
) -> str:
    """Build the prompt for a pipeline stage using input mapping and templates.

    This is the controlled handoff mechanism — stages only receive
    what the input_mapping explicitly allows.
    """
    # If stage has a custom prompt template, use it
    if prompt_template:
        return _render_template(prompt_template, stage_id, context)

    # If input_mapping has custom prefix, use it with default suffix
    if input_mapping.custom_prompt_prefix:
        return _render_template(input_mapping.custom_prompt_prefix, stage_id, context)

    # Default: pass through the previous output or original request
    prev_output = context.get_previous_output(stage_id)
    if prev_output:
        return prev_output

    return original_request


def _render_template(template: str, stage_id: str, context: PipelineContext) -> str:
    """Render a prompt template with stage-specific variables."""
    original_request = context.original_user_input
    previous_output = context.get_previous_output(stage_id) or ""

    # Get named stage outputs for multi-stage templates
    draft_output = _extract_stage_output(context, "draft")
    review_output = _extract_stage_output(context, "review")
    critique_notes = _extract_stage_output(context, "critique")
    verify_output = _extract_stage_output(context, "verify")
    initial_output = _extract_stage_output(context, "initial")

    # Build stage summaries block if requested
    summaries = context.get_summary_text()

    # All outputs concatenated if needed
    all_outputs = context.get_all_outputs_text()

    replacements: dict[str, str] = {
        "original_request": original_request,
        "previous_output": previous_output,
        "draft_output": draft_output,
        "review_output": review_output,
        "critique_notes": critique_notes,
        "verify_output": verify_output,
        "initial_output": initial_output,
        "stage_summaries": summaries,
        "all_outputs": all_outputs,
    }

    result = template
    for key, value in replacements.items():
        result = result.replace("{" + key + "}", value)

    return result
