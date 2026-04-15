"""Search prompt builder for search-answer pipeline."""

from __future__ import annotations

from app.search.models import SearchContext, SearchResult


def build_search_prompt(
    query: str,
    search_context: SearchContext,
) -> str:
    """Build prompt with search results for the generate stage.

    Args:
        query: Original user query
        search_context: Search context with results and fetched content

    Returns:
        Formatted prompt with sources
    """
    parts = []

    # Header
    parts.append("Answer the question using the sources below.")
    parts.append("")

    # Sources
    if search_context.fetched_contents:
        parts.append("Sources:")
        for i, (url, content) in enumerate(search_context.fetched_contents.items(), start=1):
            # Truncate content for prompt
            truncated = content[:1500] if len(content) > 1500 else content
            parts.append(f"[{i}] {url}")
            parts.append(truncated)
            parts.append("")
    elif search_context.search_results:
        # Use snippets if no content fetched
        parts.append("Search Results:")
        for i, result in enumerate(search_context.search_results[:5], start=1):
            parts.append(f"[{i}] {result.title}")
            parts.append(f"    URL: {result.url}")
            if result.snippet:
                parts.append(f"    {result.snippet}")
            parts.append("")

    parts.append("Question:")
    parts.append(query)
    parts.append("")
    parts.append("Instructions:")
    parts.append("- Cite sources like [1], [2]")
    parts.append("- Be concise and accurate")
    parts.append("- If information is insufficient, state so")

    return "\n".join(parts)


def build_search_sources_summary(search_results: list[SearchResult], max_results: int = 5) -> str:
    """Build a simple sources summary from search results (no content fetching).

    Args:
        search_results: List of search results
        max_results: Max results to include

    Returns:
        Formatted sources string
    """
    if not search_results:
        return "No search results available."

    parts = ["Sources:"]
    for i, result in enumerate(search_results[:max_results], start=1):
        parts.append(f"[{i}] {result.title}")
        parts.append(f"    {result.url}")
        if result.snippet:
            snippet = result.snippet[:200] + "..." if len(result.snippet) > 200 else result.snippet
            parts.append(f"    {snippet}")
        parts.append("")

    return "\n".join(parts)
