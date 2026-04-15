"""Search domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field


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


@dataclass
class SearchError:
    """Search error details."""

    provider: str
    error_type: str  # timeout, network, parse, etc.
    message: str
    details: dict[str, Any] = field(default_factory=dict)
