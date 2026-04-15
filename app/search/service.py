"""Search service with query expansion and deduplication."""

from __future__ import annotations

from urllib.parse import urlparse

from app.core.config import settings
from app.core.logging import get_logger
from app.search.cache import page_cache, search_cache
from app.search.fetcher import content_fetcher
from app.search.models import SearchContext, SearchResult
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
        """Execute search with query expansion and optional content fetching.

        Args:
            query: User search query
            fetch_content: Whether to fetch page content
            max_results: Max results to return (default from config)

        Returns:
            SearchContext with results and optionally fetched content
        """
        context = SearchContext(original_query=query)

        try:
            # Step 1: Expand query
            expanded = await expand_query(query)
            context.expanded_queries = expanded
            logger.debug("query_expanded", original=query, variations=expanded)

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

            logger.info(
                "search_total",
                query=query,
                expanded_count=len(expanded),
                total_results=len(all_results),
                unique_results=len(unique_results),
            )

            # Step 4: Fetch content if requested
            if fetch_content and unique_results:
                await self._fetch_contents(context, unique_results)

            return context

        except Exception as e:
            logger.error("search_failed", query=query, error=str(e))
            context.error = str(e)
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
