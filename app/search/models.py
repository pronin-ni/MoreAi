"""Search domain models and validation logic."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

# Constants for validation thresholds
MIN_CONTENT_PAGES = 3
MIN_TOTAL_TEXT_LENGTH = 2000
MAX_RETRIES = 2

# Ambiguity detection heuristics (simple, no LLM)
AMBIGUOUS_PATTERNS = [
    r"\b(best|top|worse|worst)\b",  # Superlatives - relative
    r"\b(compared|comparison)\b",  # Comparison without明确的 target
    r"\b(like|similar to)\b",  # 未completed comparisons
    r"\b(vs|versus|against)\b\s*$",  # Comparison without second term
    r"\?",  # Question with question mark in query
    r"\b(should I|should I|which should)\b",  # Advice-seeking
]

# Fallback query patterns for retry (simple heuristics)
QUERY_MODIFIERS = [
    lambda q: re.sub(r"\b(best|top|good|great|awesome|amazing)\b", "", q, flags=re.IGNORECASE),
    lambda q: re.sub(r"\s+", " ", q).strip(),
    lambda q: re.sub(r"\bin\b\s+", "", q, flags=re.IGNORECASE),  # Remove "in [location]"
    lambda q: re.sub(r"\b\d{4}\b", "", q),  # Remove years
]


def _generate_fallback_queries(query: str, attempt: int) -> list[str]:
    """Generate fallback queries using simple heuristics (no LLM).

    Args:
        query: Original user query
        attempt: Current retry attempt (0-indexed)

    Returns:
        List of alternative queries to try
    """
    fallback_queries = []

    # Get modifier for this attempt
    modifier = QUERY_MODIFIERS[attempt % len(QUERY_MODIFIERS)]
    modified = modifier(query)

    if modified and modified != query:
        fallback_queries.append(modified)

    # Add basic variations based on attempt
    if attempt == 0:
        # First retry: try just the original without fluff
        cleaned = re.sub(r"[^\w\s]", "", query)  # Remove punctuation
        fallback_queries.append(cleaned)
    elif attempt == 1:
        # Second retry: try extracting main topic (first 2-3 words as topic)
        words = query.split()
        if len(words) > 2:
            fallback_queries.append(" ".join(words[:3]))

    # Always return list (may be empty if all attempts failed)
    return fallback_queries[:3]  # Max 3 variations


def _check_ambiguity(query: str) -> bool:
    """Check if query is ambiguous using simple heuristics.

    Args:
        query: User query to check

    Returns:
        True if query appears ambiguous
    """
    query_lower = query.lower()

    for pattern in AMBIGUOUS_PATTERNS:
        if re.search(pattern, query_lower, re.IGNORECASE):
            return True

    # Check for very short queries that might be ambiguous
    if len(query.split()) < 3:
        return True

    return False


def validate_context(
    query: str,
    search_results: list[SearchResult],
    fetched_contents: dict[str, str],
) -> tuple[str, str, list[str]]:
    """Validate search context quality.

    Args:
        query: Original user query
        search_results: List of search results
        fetched_contents: Dict of url -> extracted content

    Returns:
        Tuple of (validation_result, details, keywords_found)
    """
    # Count metrics
    result_count = len(search_results)
    content_pages = len(fetched_contents)
    total_text_length = sum(len(c) for c in fetched_contents.values())

    # Extract keywords from query (simple: remove common words, keep significant terms)
    query_words = re.findall(r"\b\w{4,}\b", query.lower())
    stopwords = {
        "what",
        "when",
        "where",
        "which",
        "who",
        "how",
        "that",
        "this",
        "with",
        "from",
        "have",
        "been",
        "will",
        "would",
        "could",
        "should",
        "about",
        "there",
        "their",
        "these",
        "those",
        "some",
        "into",
        "more",
    }
    keywords = [w for w in query_words if w not in stopwords]

    # Check for keyword presence in content
    keywords_found = []
    if total_text_length > 0:
        content_text = " ".join(fetched_contents.values()).lower()
        for kw in keywords:
            if kw in content_text:
                keywords_found.append(kw)

    # Determine validation result
    issues: list[str] = []

    if content_pages < MIN_CONTENT_PAGES:
        issues.append(f"too_few_pages({content_pages}<{MIN_CONTENT_PAGES})")

    if total_text_length < MIN_TOTAL_TEXT_LENGTH:
        issues.append(f"too_little_text({total_text_length}<{MIN_TOTAL_TEXT_LENGTH})")

    if not keywords_found and keywords:
        issues.append("keywords_not_found")

    # Check for ambiguity
    is_ambiguous = _check_ambiguity(query)

    # Determine result
    if is_ambiguous:
        validation_result = "AMBIGUOUS"
        details = "Query appears ambiguous - may need clarification"
    elif issues:
        validation_result = "INSUFFICIENT"
        details = "; ".join(issues)
    else:
        validation_result = "OK"
        details = f"OK: {content_pages} pages, {total_text_length} chars"

    return validation_result, details, keywords_found


class SearchResult(BaseModel):
    """A single search result from a search provider."""

    title: str = Field(..., description="Result title")
    url: str = Field(..., description="Result URL")
    snippet: str = Field(default="", description="Result snippet/description")
    source: str = Field(..., description="Search provider: duckduckgo, searxng")


class SearchResponse(BaseModel):
    """Response from search operation."""

    results: list[SearchResult] = Field(default_factory=list)
    query: str = Field(..., description="Original search query")
    provider: str = Field(..., description="Provider that returned results")
    total_results: int = Field(default=0, description="Total results found")


@dataclass
class SearchContext:
    """Search context passed through pipeline stages."""

    original_query: str
    expanded_queries: list[str] = field(default_factory=list)
    search_results: list[SearchResult] = field(default_factory=list)
    fetched_contents: dict[str, str] = field(default_factory=dict)  # url -> content

    # Metadata
    sources_used: list[str] = field(default_factory=list)
    error: str | None = None

    # Validation & retry tracking
    validation_result: str | None = None  # OK | INSUFFICIENT | AMBIGUOUS
    retry_count: int = 0
    total_text_length: int = 0
    keywords_found: list[str] = field(default_factory=list)


@dataclass
class SearchError:
    """Search error details."""

    provider: str
    error_type: str  # timeout, network, parse, etc.
    message: str
    details: dict[str, Any] = field(default_factory=dict)
