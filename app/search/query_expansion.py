"""Query expansion using LLM."""

from __future__ import annotations

import asyncio
import re

from app.core.config import settings
from app.core.logging import get_logger
from app.schemas.openai import ChatCompletionRequest, ChatMessage

logger = get_logger(__name__)

QUERY_EXPANSION_PROMPT = """You are a search query assistant. Generate alternative search queries for the given question.

Generate exactly 2 alternative queries (variations that might return different but useful results).
Keep queries short and focused (5-15 words).

Original question: {query}

Output format (just the queries, one per line, no numbering):
"""


async def expand_query(query: str) -> list[str]:
    """Expand search query using LLM to generate variations.

    Args:
        query: Original user query

    Returns:
        List of queries: [original, variation1, variation2]

    Note:
        Uses fast mode selection to minimize latency.
        Returns original query on failure.
    """
    if not query or len(query.strip()) < 2:
        return [query]

    try:
        prompt = QUERY_EXPANSION_PROMPT.format(query=query)

        # Create request for LLM - use auto for intelligent selection
        request = ChatCompletionRequest(
            model="auto",
            messages=[
                ChatMessage(role="system", content="You generate search query variations."),
                ChatMessage(role="user", content=prompt),
            ],
            max_tokens=100,
            temperature=0.3,
        )

        # Use chat_proxy_service for completion
        from app.services.chat_proxy_service import service as chat_proxy_service

        response = await asyncio.wait_for(
            chat_proxy_service.process_completion(request, request_id="query_expansion"),
            timeout=10.0,
        )

        # Extract variations from response
        variations = _parse_variations(response.choices[0].message.content)

        # Combine original with variations
        all_queries = [query] + variations[: settings.search.max_queries - 1]

        logger.debug(
            "query_expanded",
            original=query,
            variations=variations,
            total=len(all_queries),
        )

        return all_queries[: settings.search.max_queries]

    except TimeoutError:
        logger.warning("query_expansion_timeout", query=query)
        return [query]

    except Exception as e:
        logger.warning("query_expansion_error", query=query, error=str(e))
        return [query]


def _parse_variations(response_text: str) -> list[str]:
    """Parse LLM response to extract query variations.

    Args:
        response_text: LLM response text

    Returns:
        List of extracted queries
    """
    if not response_text:
        return []

    # Split by newlines and clean
    lines = response_text.strip().split("\n")

    queries: list[str] = []
    for line in lines:
        # Clean up line
        line = line.strip()
        # Remove numbering like "1.", "2.", "-", "•"
        line = re.sub(r"^[\d\.\-\•]+\s*", "", line)
        # Skip empty lines
        if line and len(line) >= 3:
            queries.append(line)

    return queries
