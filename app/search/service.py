"""Search service - simplified single-pass search without retries."""

from __future__ import annotations

import time
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
    SearchContext,
    SearchResult,
    validate_context,
)
from app.search.router import search_router

logger = get_logger(__name__)


class SearchService:
    """Main search service - single pass, no retries."""

    async def search(
        self,
        query: str,
        fetch_content: bool = False,
        max_results: int | None = None,
    ) -> SearchContext:
        """Execute search exactly once - no retries.

        Args:
            query: User search query
            fetch_content: Whether to fetch page content
            max_results: Max results to return (default from config)

        Returns:
            SearchContext with results and optionally fetched content
        """
        context = SearchContext(original_query=query)
        start_time = time.monotonic()

        try:
            max_results = max_results or settings.search.max_results
            cache_key = f"search:{query}:{max_results}"

            cached = search_cache.get(cache_key)
            if cached:
                logger.debug("search_cache_hit", query=query)
                context.search_results = cached
            else:
                logger.debug("search_execute", query=query)
                context.search_results = await search_router.search(query, max_results)

                if context.search_results:
                    search_cache.set(cache_key, context.search_results)
                    logger.debug(
                        "search_completed",
                        query=query,
                        results=len(context.search_results),
                    )
                else:
                    logger.debug("search_no_results", query=query)

            if fetch_content and context.search_results:
                await self._fetch_contents(context, context.search_results)

                filtered_pages, filtering_stats = filter_pages(
                    query,
                    context.search_results,
                    context.fetched_contents,
                )

                if not filtered_pages:
                    filtered_pages = apply_fallback(
                        filtered_pages,
                        context.fetched_contents,
                        context.search_results,
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

            context.total_text_length = sum(len(c) for c in context.fetched_contents.values())

            context.validation_result, details, keywords = validate_context(
                query,
                context.search_results,
                context.fetched_contents,
            )
            context.keywords_found = keywords

            search_duration = time.monotonic() - start_time

            logger.info(
                "search_complete",
                query=query[:50],
                validation_result=context.validation_result,
                content_pages=len(context.fetched_contents),
                total_text_length=context.total_text_length,
                search_duration_seconds=round(search_duration, 2),
                results_count=len(context.search_results),
            )

            return context

        except Exception as e:
            search_duration = time.monotonic() - start_time
            logger.error(
                "search_failed",
                query=query,
                error=str(e),
                duration_seconds=round(search_duration, 2),
            )
            context.error = str(e)
            context.validation_result = "ERROR"
            return context

    async def _fetch_contents(self, context: SearchContext, results: list[SearchResult]) -> None:
        """Fetch content from search result URLs."""
        urls = [r.url for r in results[: settings.search.fetch_max_pages]]

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

        fetched = await content_fetcher.fetch_multiple(urls_to_fetch)

        for url, content in fetched.items():
            if content:
                page_cache.set(url, content)
                context.fetched_contents[url] = content

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
        key = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
        domain_key = f"{parsed.netloc}"

        if key in seen:
            if len(result.snippet) > len(seen[key].snippet):
                seen[key] = result
            continue

        domain_count = sum(1 for k in seen if urlparse(k).netloc == domain_key)
        if domain_count >= 2:
            continue

        seen[key] = result

    unique = list(seen.values())
    unique.sort(key=lambda r: len(r.snippet), reverse=True)

    return unique[:max_results]


search_service = SearchService()