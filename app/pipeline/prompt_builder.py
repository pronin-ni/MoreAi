"""
Prompt template engine for pipeline stages.

Builds stage prompts from templates with variable substitution.
Keeps handoff between stages controlled and predictable.
"""

from __future__ import annotations

from app.pipeline.types import InputMapping, PipelineContext, StageRole

# Grounding configuration
SEARCH_CONTEXT_CHARS_PER_PAGE = 2500  # Increased from 1000 for better grounding
SEARCH_MAX_PAGES = 3  # Top 3 pages only
GROUNDING_FAILURE_PATTERNS = [
    "не могу",
    "уточните",
    "неизвестно",
    "нет информации",
    "недостаточно",
    "no information",
    "cannot find",
    "don't know",
    "unable to",
    "no data",
    "no answer",
    "not enough",
]


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
    validation_result: str | None = None,
    retry_count: int = 0,
) -> str:
    """Build prompt for search stage with search results.

    This is called from the executor when building the stage prompt
    for the search-answer pipeline.
    """
    # Special handling: if it's a search stage, build default search prompt
    return _build_search_default_prompt(
        original_request,
        search_results,
        search_content,
        search_skipped,
        validation_result,
        retry_count,
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
    validation_result: str | None = None,
    search_retry_count: int = 0,
    content_pages: int = 0,
    total_text_length: int = 0,
) -> str:
    """Build STRICT GROUNDED prompt for synthesize stage.

    Forces model to use provided context - no generic answers.
    Increased context per page for better grounding.

    Args:
        original_request: User's original query
        search_results: List of search result dicts
        search_content: Dict of url -> extracted content
        search_skipped: Whether search was skipped
        validation_result: Validation result from validate_context
        search_retry_count: Number of search retries
        content_pages: Number of pages with content (for grounding override)
        total_text_length: Total char length of all content
    """
    # Context presence override - if we have good context, force grounded mode
    has_sufficient_context = content_pages >= 2 and total_text_length >= 2000

    if search_skipped:
        return f"""Answer the question. Use your knowledge.

Question: {original_request}

IMPORTANT:
- Provide the FINAL ANSWER only
- No meta-analysis or evaluation text
"""

    if validation_result == "INSUFFICIENT":
        return f"""Search found limited information. Answer from your knowledge.

Question: {original_request}

IMPORTANT:
- Answer based on available information
- If uncertain, state briefly
- NO meta-analysis
"""

    if validation_result == "AMBIGUOUS":
        return f"""Answer based on provided sources.

Question: {original_request}

IMPORTANT:
- Use the sources below
- Note assumptions made
"""

    if not search_results:
        return f"""Answer the question. No web results found.

Question: {original_request}

IMPORTANT:
- Answer from your knowledge
- Provide the FINAL ANSWER only
"""

    # STRICT GROUNDED prompt - forces context usage
    if has_sufficient_context:
        return _build_strict_grounded_prompt(
            original_request,
            search_results,
            search_content,
        )

    # Fallback for insufficient context
    return _build_strict_grounded_prompt(
        original_request,
        search_results,
        search_content,
    )


def _build_strict_grounded_prompt(
    original_request: str,
    search_results: list,
    search_content: dict,
) -> str:
    """Build strict grounded prompt - MUST use context.

    Rules:
    - If context has relevant info → MUST use it
    - NO "I don't know" if context exists
    - NO clarifying questions
    - NO prior knowledge (only context)
    """
    # Build with INCREASED context (2500 chars per page)
    parts = [
        "You MUST answer using ONLY the provided context below.",
        "",
        "STRICT RULES:",
        "- If context contains relevant information → you MUST use it",
        "- Do NOT say 'I don't know' if context has the answer",
        "- Do NOT ask clarifying questions",
        "- Do NOT rely on prior knowledge",
        "- Cite sources as [1], [2], etc.",
        "- Provide DIRECT answer",
        "- NO meta-commentary",
        "",
        "=== SOURCES ===",
        "",
    ]

    # Add top 3 sources with MORE context (2500 chars)
    for i, r in enumerate(search_results[:SEARCH_MAX_PAGES], start=1):
        url = r.get("url", "")
        title = r.get("title", "Untitled")

        parts.append(f"[{i}] {title}")
        parts.append(f"URL: {url}")

        if url in search_content:
            # INCREASED from 1000 to 2500 chars
            content = search_content[url][:SEARCH_CONTEXT_CHARS_PER_PAGE]
            # Clean content - remove excessive whitespace
            content_clean = " ".join(content.split())
            parts.append(f"Content: {content_clean}")
        else:
            snippet = r.get("snippet", "")
            if snippet:
                parts.append(f"Snippet: {snippet[:500]}")

        parts.append("")

    parts.extend(
        [
            "=== QUESTION ===",
            original_request,
            "",
            "=== ANSWER ===",
        ]
    )

    return "\n".join(parts)
