"""Tests for search system."""

import os
import pytest

# Ensure OPENCODE env var doesn't interfere with pydantic settings loading
os.environ.pop("OPENCODE", None)


class TestSearchConfig:
    """Test search configuration."""

    def test_search_settings_loaded(self):
        """Verify search settings are loaded."""
        from app.core.config import settings

        assert hasattr(settings, "search")
        assert settings.search.enabled is True
        assert settings.search.providers == "duckduckgo,searxng"
        assert settings.search.searxng_base_url == "http://localhost:8080"
        assert settings.search.timeout == 5
        assert settings.search.max_results == 5
        assert settings.search.max_queries == 3


class TestSearchModels:
    """Test search domain models."""

    def test_search_result_model(self):
        """Verify SearchResult model."""
        from app.search.models import SearchResult

        result = SearchResult(
            title="Test Title",
            url="https://example.com",
            snippet="Test snippet",
            source="duckduckgo",
        )

        assert result.title == "Test Title"
        assert result.url == "https://example.com"
        assert result.snippet == "Test snippet"
        assert result.source == "duckduckgo"


class TestSearchProviders:
    """Test search provider setup."""

    def test_router_has_providers(self):
        """Verify router initializes with providers."""
        from app.search.router import search_router

        assert len(search_router.providers) >= 1
        provider_ids = [p.provider_id for p in search_router.providers]
        assert "duckduckgo" in provider_ids

    def test_duckduckgo_provider_exists(self):
        """Verify DuckDuckGo provider is available."""
        from app.search.providers.duckduckgo import duckduckgo_provider

        assert duckduckgo_provider.provider_id == "duckduckgo"


class TestSearchCache:
    """Test search caching."""

    def test_search_cache_operations(self):
        """Verify cache get/set."""
        from app.search.cache import SearchCache

        cache = SearchCache(ttl_seconds=60)

        # Test get on empty - this is a miss
        assert cache.get("key") is None

        # Test set and get
        cache.set("key", "value")
        result = cache.get("key")
        assert result == "value"

        # Test stats (note: first get was a miss, second get after set is a hit)
        stats = cache.stats
        assert stats["entries"] == 1
        # hits = 1 (the get after set), misses = 1 (the first get)
        assert stats["hits"] == 1
        assert stats["misses"] == 1


class TestSearchService:
    """Test search service setup."""

    def test_search_service_exists(self):
        """Verify search service is available."""
        from app.search.service import search_service

        assert search_service is not None


class TestBuiltinPipelines:
    """Test pipeline registration."""

    def test_search_answer_pipeline_exists(self):
        """Verify search-answer pipeline is registered."""
        from app.pipeline.builtin_pipelines import register_builtin_pipelines
        from app.pipeline.types import PipelineRegistry, pipeline_registry

        # Register pipelines first
        registry = PipelineRegistry()
        register_builtin_pipelines(registry)

        pipeline = registry.get("search-answer")
        assert pipeline is not None
        assert pipeline.enabled is True
        assert len(pipeline.stages) == 2

        # First stage is search_generate
        assert pipeline.stages[0].stage_id == "search_generate"
        assert pipeline.stages[0].role.value == "generate"

    def test_search_pipeline_in_registry(self):
        """Verify search pipeline is in registry."""
        from app.pipeline.builtin_pipelines import BUILTIN_PIPELINES

        pipeline_ids = [p.pipeline_id for p in BUILTIN_PIPELINES]
        assert "search-answer" in pipeline_ids


class TestStudioModes:
    """Test studio mode integration."""

    def test_search_mode_exists(self):
        """Verify search mode is defined in studio modes."""
        from app.api.studio_modes import STUDIO_MODE_POLICIES

        assert "search" in STUDIO_MODE_POLICIES
        search_mode = STUDIO_MODE_POLICIES["search"]
        assert search_mode["is_pipeline"] is True
        assert search_mode["pipeline_id"] == "search-answer"


class TestContentFetcher:
    """Test content fetcher setup."""

    def test_fetcher_exists(self):
        """Verify content fetcher is available."""
        from app.search.fetcher import content_fetcher

        assert content_fetcher is not None
