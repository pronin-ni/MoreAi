"""Pre-synthesis filtering layer for search results.

Filters fetched pages for quality, relevance, and deduplication
before passing to LLM.

No LLM, no embeddings - pure heuristic filtering.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

# Configuration
MAX_PAGES_TO_KEEP = 3
MIN_TEXT_LENGTH = 200
MIN_USEFUL_TEXT_LENGTH = 300
MIN_KEYWORD_OVERLAP_SCORE = 0.5
DEDUP_SIMILARITY_THRESHOLD = 0.8
JACCARD_SIMILARITY_THRESHOLD = DEDUP_SIMILARITY_THRESHOLD

# SEO garbage phrases (case-insensitive)
SEO_GARBAGE_PHRASES = [
    "cookies",
    "cookie policy",
    "subscribe",
    "sign up",
    "sign up now",
    "advertisement",
    "advertising",
    "promoted",
    "sponsored",
    "learn more",
    "click here",
    "newsletter",
    "free trial",
    "limited time",
    "offer ends",
    "buy now",
    "order now",
    "shop now",
]

# Navigation/boilerplate indicators
NAVIGATION_PATTERNS = [
    r"^(menu|navigation|home|about|contact|terms|privacy|faq|help)",
    r"(copyright\s*\d{4})",
    r"^(search|login|register|sign in)",
]

# Stopwords for keyword extraction
STOPWORDS = {
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
    "the",
    "and",
    "or",
    "but",
    "is",
    "are",
    "was",
    "were",
    "be",
    "to",
    "of",
    "in",
    "for",
    "on",
    "at",
    "by",
    "an",
    "as",
    "it",
    "its",
}


@dataclass
class FilteredPage:
    """A page with filtering metadata."""

    url: str
    title: str
    content: str
    score: float = 0.0
    filter_reason: str | None = None  # None = passed, or "seo", "duplicate", "low_quality"

    # Scoring breakdown for logging
    keyword_matches: int = 0
    title_matches: int = 0
    density: float = 0.0
    is_duplicate_of: str | None = None  # URL this is duplicate of


@dataclass
class FilteringStats:
    """Statistics about the filtering process."""

    total_fetched: int = 0
    seo_filtered: int = 0
    low_quality_filtered: int = 0
    duplicates_removed: int = 0
    final_count: int = 0
    fallback_used: bool = False


class RelevanceScorer:
    """Computes relevance score for a page based on query keywords."""

    def __init__(self, query: str) -> None:
        self.query = query.lower()
        self.keywords = self._extract_keywords(query)
        self.query_words_set = set(self.keywords)

    def _extract_keywords(self, query: str) -> list[str]:
        """Extract significant keywords from query (no stopwords, >=4 chars)."""
        words = re.findall(r"\b\w{4,}\b", query.lower())
        return [w for w in words if w not in STOPWORDS]

    def score(self, title: str, text: str) -> tuple[float, int, int, float]:
        """Score a page.

        Returns:
            Tuple of (score, keyword_matches, title_matches, density)
        """
        if not self.keywords:
            return 0.0, 0, 0, 0.0

        title_lower = title.lower()
        text_lower = text.lower()

        # 1. Keyword overlap (count keywords present in text)
        keyword_matches = sum(1 for kw in self.keywords if kw in text_lower)

        # 2. Title match bonus
        title_matches = sum(1 for kw in self.keywords if kw in title_lower)

        # 3. Density: frequency of keywords / text length
        total_keyword_occurrences = sum(
            len(re.findall(re.escape(kw), text_lower)) for kw in self.keywords
        )
        text_length = len(text) or 1
        density = total_keyword_occurrences / text_length

        # Compute base score
        score = 0.0
        score += keyword_matches * 1.0  # Each keyword in text = +1
        score += title_matches * 0.5  # Each keyword in title = +0.5 bonus
        score += density * 100  # Density boost

        # 4. Penalties
        if len(text) < MIN_TEXT_LENGTH:
            score -= 1.0

        if keyword_matches == 0:
            score -= 0.5

        return max(0.0, score), keyword_matches, title_matches, density


class SEOFilter:
    """Detects and filters low-quality/SEO garbage pages."""

    def __init__(self) -> None:
        self._garbage_pattern = re.compile(
            "|".join(re.escape(p) for p in SEO_GARBAGE_PHRASES),
            re.IGNORECASE,
        )
        self._nav_pattern = re.compile("|".join(NAVIGATION_PATTERNS), re.IGNORECASE | re.MULTILINE)

    def is_low_quality(self, text: str) -> tuple[bool, str]:
        """Check if page is low quality (SEO garbage, boilerplate, etc).

        Returns:
            Tuple of (is_low_quality, reason)
        """
        if len(text) < MIN_USEFUL_TEXT_LENGTH:
            return True, f"too_short({len(text)}<{MIN_USEFUL_TEXT_LENGTH})"

        # Check for excessive SEO garbage phrases
        garbage_count = len(self._garbage_pattern.findall(text))
        text_words = len(text.split())
        if text_words > 0 and (garbage_count / text_words) > 0.05:
            return True, f"too_much_seo_garbage({garbage_count} phrases)"

        # Check for high ratio of navigation/boilerplate (lines that look like menus)
        lines = text.split("\n")
        nav_lines = sum(1 for line in lines if self._nav_pattern.match(line.strip()))
        if len(lines) > 0 and (nav_lines / len(lines)) > 0.4:
            return True, "too_much_navigation"

        # Check for repeated phrases (keyword stuffing indicator)
        words = text.lower().split()
        if len(words) > 50:
            unique_words = set(words)
            unique_ratio = len(unique_words) / len(words)
            if unique_ratio < 0.3:
                return True, f"low_unique_word_ratio({unique_ratio:.2f}<0.3)"

        # Check for too many repeated words (another stuffing indicator)
        if len(words) > 20:
            word_freq: dict[str, int] = {}
            for w in words:
                if len(w) > 3:
                    word_freq[w] = word_freq.get(w, 0) + 1

            if word_freq:
                max_freq = max(word_freq.values())
                if max_freq / len(words) > 0.15:
                    return True, "keyword_stuffing"

        return False, ""


class Deduplicator:
    """Removes near-duplicate pages using Jaccard similarity."""

    def __init__(self, threshold: float = JACCARD_SIMILARITY_THRESHOLD) -> None:
        self.threshold = threshold

    def jaccard_similarity(self, text1: str, text2: str) -> float:
        """Compute Jaccard similarity between two texts (on first 500 chars)."""
        # Take first 500 chars for comparison
        sample1 = set(text1[:500].lower().split())
        sample2 = set(text2[:500].lower().split())

        if not sample1 or not sample2:
            return 0.0

        intersection = len(sample1 & sample2)
        union = len(sample1 | sample2)

        return intersection / union if union > 0 else 0.0

    def deduplicate(self, pages: list[FilteredPage]) -> tuple[list[FilteredPage], int]:
        """Remove duplicate pages, keeping the one with higher score.

        Returns:
            Tuple of (deduplicated list, number removed)
        """
        if len(pages) <= 1:
            return pages, 0

        kept: list[FilteredPage] = []
        removed_count = 0

        for page in pages:
            is_duplicate = False
            duplicate_of: str | None = None

            for kept_page in kept:
                # Exact match check
                if page.content[:500] == kept_page.content[:500]:
                    is_duplicate = True
                    duplicate_of = kept_page.url
                    break

                # Jaccard similarity check
                sim = self.jaccard_similarity(page.content, kept_page.content)
                if sim > self.threshold:
                    is_duplicate = True
                    duplicate_of = kept_page.url
                    break

            if is_duplicate:
                page.filter_reason = "duplicate"
                page.is_duplicate_of = duplicate_of
                removed_count += 1
                logger.debug(
                    "page_duplicate_removed",
                    url=page.url,
                    duplicate_of=duplicate_of,
                    similarity=self.jaccard_similarity(page.content, kept[-1].content)
                    if kept
                    else 0,
                )
            else:
                kept.append(page)

        return kept, removed_count


def filter_pages(
    query: str,
    results: list[Any],  # SearchResult list
    fetched_contents: dict[str, str],
) -> tuple[list[FilteredPage], FilteringStats]:
    """Main filtering pipeline.

    Applies in order:
    1. SEO/Low-quality filter
    2. Relevance scoring
    3. Deduplication
    4. Top-N selection

    Args:
        query: Original search query
        results: Search results (title/url)
        fetched_contents: Dict of url -> content

    Returns:
        Tuple of (filtered pages, statistics)
    """
    stats = FilteringStats()
    stats.total_fetched = len(fetched_contents)

    # Step 1: Build initial page list - ONLY include pages that pass SEO filter
    pages: list[FilteredPage] = []
    seo_filter = SEOFilter()

    for result in results:
        url = result.url
        content = fetched_contents.get(url, "")

        if not content:
            continue

        # Check SEO/low quality
        is_low_quality, reason = seo_filter.is_low_quality(content)
        if is_low_quality:
            stats.seo_filtered += 1
            logger.debug("page_filtered_seo", url=url, reason=reason)
            continue  # Skip - do NOT add to pages list

        # Quality page - add to list for scoring
        pages.append(
            FilteredPage(
                url=url,
                title=result.title,
                content=content,
            )
        )

    stats.low_quality_filtered = stats.seo_filtered
    logger.info(
        "filtering_step_seo",
        total_fetched=stats.total_fetched,
        seo_filtered=stats.seo_filtered,
        pages_remaining=len(pages),
    )

    if not pages:
        logger.warning("filtering_no_pages_after_seo", total_fetched=stats.total_fetched)
        return [], stats

    # Step 2: Relevance scoring
    scorer = RelevanceScorer(query)

    for page in pages:
        score, keyword_matches, title_matches, density = scorer.score(page.title, page.content)
        page.score = score
        page.keyword_matches = keyword_matches
        page.title_matches = title_matches
        page.density = density

        logger.debug(
            "page_scored",
            url=page.url,
            score=score,
            keyword_matches=keyword_matches,
            title_matches=title_matches,
            density=density,
        )

    # Sort by score descending
    pages.sort(key=lambda p: p.score, reverse=True)

    # Step 3: Deduplication
    dedup = Deduplicator()
    pages, dedup_count = dedup.deduplicate(pages)
    stats.duplicates_removed = dedup_count

    logger.info(
        "filtering_step_dedup",
        before_dedup=len(pages) + dedup_count,
        after_dedup=len(pages),
        removed=dedup_count,
    )

    if not pages:
        logger.warning("filtering_no_pages_after_dedup")
        return [], stats

    # Step 4: Keep top N
    top_pages = pages[:MAX_PAGES_TO_KEEP]
    stats.final_count = len(top_pages)

    # Log final selection with scores
    for i, page in enumerate(top_pages):
        logger.info(
            "page_selected_for_context",
            rank=i + 1,
            url=page.url,
            score=page.score,
            keywords=page.keyword_matches,
            title_matches=page.title_matches,
        )

    logger.info(
        "filtering_complete",
        total_fetched=stats.total_fetched,
        seo_filtered=stats.seo_filtered,
        duplicates_removed=stats.duplicates_removed,
        final_count=stats.final_count,
    )

    return top_pages, stats


def build_context(pages: list[FilteredPage], max_chars: int = 1500) -> str:
    """Build final context string from filtered pages.

    Args:
        pages: List of filtered pages (should be quality-checked)
        max_chars: Max characters per page to include

    Returns:
        Concatenated context string with source markers
    """
    if not pages:
        return ""

    context_parts: list[str] = []

    for page in pages:
        # Trim to relevant chunk
        trimmed = _trim_to_relevant_chunk(page.content, max_chars)

        context_parts.append(f"[Source: {page.title}]\n{trimmed}\n")

    return "\n---\n".join(context_parts)


def _trim_to_relevant_chunk(text: str, max_chars: int) -> str:
    """Trim text to max_chars at sentence/paragraph boundary."""
    if len(text) <= max_chars:
        return text

    # Find last period or newline before max_chars
    sample = text[:max_chars]
    last_period = sample.rfind(".")
    last_newline = sample.rfind("\n")

    # Break at sentence or paragraph boundary
    if last_period > max_chars * 0.7:
        return sample[: last_period + 1]
    elif last_newline > max_chars * 0.7:
        return sample[:last_newline]

    return sample + "..."


def apply_fallback(
    filtered_pages: list[FilteredPage],
    raw_pages: dict[str, str],
    results: list[Any],
) -> list[FilteredPage]:
    """Apply fallback: if filtered is empty, use top raw pages.

    Args:
        filtered_pages: Already filtered pages (may be empty)
        raw_pages: Raw fetched contents (url -> content)
        results: Search results for title info

    Returns:
        Filtered pages or fallback to top raw pages
    """
    if filtered_pages:
        return filtered_pages

    if not raw_pages or not results:
        return []

    # Build a quick result -> title mapping
    title_map = {r.url: r.title for r in results}

    # Take top 3 raw pages by content length (simple proxy for quality)
    sorted_urls = sorted(
        raw_pages.keys(),
        key=lambda u: len(raw_pages.get(u, "")),
        reverse=True,
    )[:MAX_PAGES_TO_KEEP]

    fallback_pages = []
    for url in sorted_urls:
        content = raw_pages.get(url, "")
        if content:
            fallback_pages.append(
                FilteredPage(
                    url=url,
                    title=title_map.get(url, ""),
                    content=content,
                    score=0.0,
                    filter_reason="fallback",
                )
            )

    logger.warning(
        "filtering_fallback_used",
        fallback_count=len(fallback_pages),
    )

    return fallback_pages
