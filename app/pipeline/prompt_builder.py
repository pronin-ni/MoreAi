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

    # Search-related variables for search-answer pipeline
    search_results = context.metadata.get("search_results", [])
    search_content = context.metadata.get("search_content", {})
    context.metadata.get("search_skipped", False)

    # Format search results for prompt
    search_sources_formatted = _format_search_sources(search_results, search_content)

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
        # Search variables
        "search_sources": search_sources_formatted,
        "search_results": _format_search_results_list(search_results),
    }

    result = template
    for key, value in replacements.items():
        result = result.replace("{" + key + "}", value)

    return result


def build_search_stage_prompt(
    stage_id: str,
    original_request: str,
    search_results: list[dict],
    search_content: dict[str, str],
    search_skipped: bool,
) -> str:
    """Build prompt for search stage with search results.

    This is called from the executor when building the stage prompt
    for the search-answer pipeline.
    """
    # Special handling: if it's a search stage, build default search prompt
    return _build_search_default_prompt(
        original_request, search_results, search_content, search_skipped
    )


def _format_search_sources(search_results: list[dict], search_content: dict[str, str]) -> str:
    """Format search results with content for prompt."""
    if not search_results:
        return "No search results available."

    parts = ["Sources:"]
    for i, r in enumerate(search_results[:5], start=1):
        title = r.get("title", "Untitled")
        url = r.get("url", "")
        snippet = r.get("snippet", "")

        parts.append(f"[{i}] {title}")
        parts.append(f"    URL: {url}")
        if snippet:
            truncated = snippet[:200] + "..." if len(snippet) > 200 else snippet
            parts.append(f"    {truncated}")

        # Add fetched content if available
        if url in search_content:
            content = search_content[url][:1000]
            parts.append(f"    Content: {content}...")

        parts.append("")

    return "\n".join(parts)


def _format_search_results_list(search_results: list[dict]) -> str:
    """Format search results as a simple list."""
    if not search_results:
        return "No results"

    return "\n".join(
        [f"- {r.get('title', 'Untitled')}: {r.get('url', '')}" for r in search_results[:5]]
    )


def _build_search_default_prompt(
    original_request: str,
    search_results: list,
    search_content: dict,
    search_skipped: bool,
) -> str:
    """Build default prompt for synthesize stage (Perplexity-style)."""
    if search_skipped:
        return f"""Answer the question. Use your knowledge.

Question: {original_request}

IMPORTANT:
- Provide the FINAL ANSWER only
- No meta-analysis or evaluation text
- No phrases like "I cannot evaluate" or "no answer"
"""

    if not search_results:
        return f"""Answer the question. No web results found.

Question: {original_request}

IMPORTANT:
- Answer from your knowledge
- Provide the FINAL ANSWER only
- No meta-analysis
"""

    # Build Perplexity-style prompt with numbered sources
    parts = [
        "Answer using the sources below. cite inline like [1], [2].",
        "",
    ]

    # Add sources with content (numbered [1], [2], etc.)
    for i, r in enumerate(search_results[:5], start=1):
        url = r.get("url", "")
        title = r.get("title", "Untitled")

        parts.append(f"[{i}] {title}")
        parts.append(f"    {url}")

        if url in search_content:
            content = search_content[url][:1000]
            parts.append(f"    {content}")
        else:
            snippet = r.get("snippet", "")
            if snippet:
                parts.append(f"    {snippet[:300]}")

        parts.append("")

    parts.extend(
        [
            f"Question: {original_request}",
            "",
            "IMPORTANT:",
            "- Provide the FINAL ANSWER only",
            "- Cite sources as [1], [2], etc.",
            "- NO meta-analysis or evaluation text",
            "- Include sources list at the end",
        ]
    )

    return "\n".join(parts)
