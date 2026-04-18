"""Search service with query expansion, validation, retry logic, and pre-synthesis filtering."""

from __future__ import annotations

from urllib.parse import urlparse

from app.core.config import settings
from app.core.logging import get_logger
from app.search.cache import page_cache, search_cache
from app.search.fetcher import content_fetcher
from app.search.filtering import (
    apply_fallback,
    filter_pages,
)
from app.search.models import (
    MAX_RETRIES,
    SearchContext,
    SearchResult,
    _generate_fallback_queries,
    validate_context,
)
from app.search.query_expansion import expand_query
from app.search.router import search_router

logger = get_logger(__name__)


class SearchService:
    """Main search service with multi-query and content fetching."""

    async def search(
        self,
        query: str,
        fetch_content: bool = False,
        max_results: int | None = None,
    ) -> SearchContext:
        """Execute search with validation, retry and optional content fetching.

        Args:
            query: User search query
            fetch_content: Whether to fetch page content
            max_results: Max results to return (default from config)

        Returns:
            SearchContext with results and optionally fetched content
        """
        context = SearchContext(original_query=query)

        try:
            # Perform search with potential retries
            context = await self._search_with_retry(query, fetch_content, max_results, context)

            # Final validation (after all retries)
            context.validation_result, details, keywords = validate_context(
                query,
                context.search_results,
                context.fetched_contents,
            )
            context.keywords_found = keywords

            logger.info(
                "search_validation",
                query=query[:50],
                validation_result=context.validation_result,
                details=details,
                retry_count=context.retry_count,
                content_pages=len(context.fetched_contents),
                total_text_length=context.total_text_length,
            )

            return context

        except Exception as e:
            logger.error("search_failed", query=query, error=str(e))
            context.error = str(e)
            context.validation_result = "ERROR"
            return context

    async def _search_with_retry(
        self,
        query: str,
        fetch_content: bool,
        max_results: int | None,
        context: SearchContext,
    ) -> SearchContext:
        """Execute search with retry logic for insufficient context."""

        # Track if we need more searches
        needs_retry = True
        current_query = query

        while needs_retry and context.retry_count <= MAX_RETRIES:
            # Step 1: Expand query (only on first attempt)
            if context.retry_count == 0:
                expanded = await expand_query(current_query)
                context.expanded_queries = expanded
            else:
                # Generate fallback queries for retry
                fallbacks = _generate_fallback_queries(current_query, context.retry_count - 1)
                context.expanded_queries.extend(fallbacks)
                expanded = [current_query] + fallbacks

            logger.debug(
                "search_attempt",
                query=current_query,
                attempt=context.retry_count,
                expanded=expanded[:2],
            )

            # Step 2: Search for each query
            all_results: list[SearchResult] = []

            for q in expanded:
                cache_key = f"search:{q}:{settings.search.max_results}"

                # Check cache first
                cached = search_cache.get(cache_key)
                if cached:
                    logger.debug("search_cache_hit", query=q)
                    all_results.extend(cached)
                    continue

                # Execute search
                results = await search_router.search(q, settings.search.max_results)

                if results:
                    # Cache results
                    search_cache.set(cache_key, results)
                    all_results.extend(results)
                    logger.debug(
                        "search_completed",
                        query=q,
                        results=len(results),
                    )
                else:
                    logger.debug("search_no_results", query=q)

            # Step 3: Deduplicate results
            unique_results = _deduplicate_results(
                all_results, max_results or settings.search.max_results
            )
            context.search_results = unique_results

            # Step 4: Fetch content if requested
            if fetch_content and unique_results:
                await self._fetch_contents(context, unique_results)

                # Step 5: Pre-synthesis filtering
                filtered_pages, filtering_stats = filter_pages(
                    current_query,
                    unique_results,
                    context.fetched_contents,
                )

                # Apply fallback if filtering removed everything
                if not filtered_pages:
                    filtered_pages = apply_fallback(
                        filtered_pages,
                        context.fetched_contents,
                        unique_results,
                    )
                    filtering_stats.fallback_used = True

                context.filtered_contents = filtered_pages
                context.filtering_stats = {
                    "total_fetched": filtering_stats.total_fetched,
                    "seo_filtered": filtering_stats.seo_filtered,
                    "duplicates_removed": filtering_stats.duplicates_removed,
                    "final_count": filtering_stats.final_count,
                    "fallback_used": filtering_stats.fallback_used,
                }

                logger.info(
                    "filtering_applied",
                    total_fetched=filtering_stats.total_fetched,
                    seo_filtered=filtering_stats.seo_filtered,
                    duplicates_removed=filtering_stats.duplicates_removed,
                    final_count=filtering_stats.final_count,
                    fallback_used=filtering_stats.fallback_used,
                )

            # Calculate metrics
            context.total_text_length = sum(len(c) for c in context.fetched_contents.values())

            # Validate this attempt
            validation_result, details, keywords = validate_context(
                current_query,
                context.search_results,
                context.fetched_contents,
            )
            context.keywords_found = keywords

            logger.info(
                "search_attempt_result",
                query=current_query[:50],
                attempt=context.retry_count,
                validation_result=validation_result,
                details=details,
                content_pages=len(context.fetched_contents),
                text_length=context.total_text_length,
            )

            # Decide: need retry?
            if validation_result == "INSUFFICIENT" and context.retry_count < MAX_RETRIES:
                context.retry_count += 1
                # Continue loop with fallback query
                logger.info(
                    "search_retry_triggered",
                    query=current_query,
                    attempt=context.retry_count,
                    reason=details,
                )
                # Clear contents to refetch with new query
                context.fetched_contents.clear()
                needs_retry = True
            elif validation_result == "AMBIGUOUS":
                # Don't retry ambiguous - let LLM handle it
                context.validation_result = "AMBIGUOUS"
                needs_retry = False
            else:
                # OK or ERROR - proceed
                needs_retry = False

        return context

    async def _fetch_contents(self, context: SearchContext, results: list[SearchResult]) -> None:
        """Fetch content from search result URLs."""
        urls = [r.url for r in results[: settings.search.fetch_max_pages]]

        # Check page cache
        urls_to_fetch: list[str] = []
        for url in urls:
            cached = page_cache.get(url)
            if cached:
                context.fetched_contents[url] = cached
                logger.debug("page_cache_hit", url=url)
            else:
                urls_to_fetch.append(url)

        if not urls_to_fetch:
            logger.debug("all_pages_cached")
            return

        # Fetch missing pages
        fetched = await content_fetcher.fetch_multiple(urls_to_fetch)

        # Update cache and context
        for url, content in fetched.items():
            if content:
                page_cache.set(url, content)
                context.fetched_contents[url] = content

        # Track sources used
        context.sources_used = list(context.fetched_contents.keys())

        logger.info(
            "content_fetch_complete",
            total_urls=len(urls),
            fetched=len(fetched),
        )


def _deduplicate_results(results: list[SearchResult], max_results: int) -> list[SearchResult]:
    """Deduplicate search results by URL domain and path."""
    seen: dict[str, SearchResult] = {}

    for result in results:
        parsed = urlparse(result.url)
        # Normalize: scheme + netloc + path (without trailing slash)
        key = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"

        # Also track by domain for stricter dedup
        domain_key = f"{parsed.netloc}"

        # Skip if we've seen this exact URL
        if key in seen:
            # Prefer result with longer snippet
            if len(result.snippet) > len(seen[key].snippet):
                seen[key] = result
            continue

        # Skip if we've seen many results from this domain (limit to 2 per domain)
        domain_count = sum(1 for k in seen if urlparse(k).netloc == domain_key)
        if domain_count >= 2:
            continue

        seen[key] = result

    # Return top results sorted by snippet length (more content = potentially better)
    unique = list(seen.values())
    unique.sort(key=lambda r: len(r.snippet), reverse=True)

    return unique[:max_results]


# Global search service
search_service = SearchService()
