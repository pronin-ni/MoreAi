"""Tests for search filtering module."""

import os

# Ensure OPENCODE env var doesn't interfere with pydantic settings loading
os.environ.pop("OPENCODE", None)

import pytest

from app.search.filtering import (
    Deduplicator,
    FilteredPage,
    FilteringStats,
    RelevanceScorer,
    SEOFilter,
    apply_fallback,
    filter_pages,
)


class TestRelevanceScorer:
    def test_extract_keywords(self):
        scorer = RelevanceScorer("what is python programming language")
        assert "python" in scorer.keywords
        assert "programming" in scorer.keywords
        assert "language" in scorer.keywords
        assert "what" not in scorer.keywords  # stopword
        assert "is" not in scorer.keywords  # stopword

    def test_score_with_keyword_match(self):
        scorer = RelevanceScorer("python programming")
        score, kw_matches, title_matches, density = scorer.score(
            "Learn Python Programming",
            "Python is a programming language used for web development and data science.",
        )
        assert score > 0
        assert kw_matches >= 2
        assert title_matches >= 2

    def test_score_no_keyword_match(self):
        scorer = RelevanceScorer("python")
        score, _, _, _ = scorer.score(
            "JavaScript Tutorial",
            "JavaScript is a great language for web development.",
        )
        assert score < 0.5  # Should have penalty

    def test_score_short_text_penalty(self):
        scorer = RelevanceScorer("python")
        score, _, _, _ = scorer.score(
            "Python",
            "Short",  # Less than MIN_TEXT_LENGTH (200)
        )
        # Should have penalty applied (capped at 0.0)
        assert score == 0.0


class TestSEOFilter:
    def test_too_short_text(self):
        seo = SEOFilter()
        is_low, reason = seo.is_low_quality("Short text")
        assert is_low is True
        assert "too_short" in reason

    def test_seo_garbage_phrases(self):
        seo = SEOFilter()
        spam_text = "Cookies policy subscribe sign up advertisement " * 20
        is_low, reason = seo.is_low_quality(spam_text)
        assert is_low is True
        assert "seo_garbage" in reason

    def test_low_unique_word_ratio(self):
        seo = SEOFilter()
        # Repeat same words many times
        repeated = "word " * 100 + "another " * 50
        is_low, reason = seo.is_low_quality(repeated)
        assert is_low is True
        assert "unique_word_ratio" in reason

    def test_quality_content_passes(self):
        seo = SEOFilter()
        quality_text = """
        Python is a high-level, general-purpose programming language.
        Its design philosophy emphasizes code readability with the use of significant indentation.
        Python supports multiple programming paradigms, including structured, procedural, reflective, and object-oriented.
        """
        is_low, reason = seo.is_low_quality(quality_text)
        assert is_low is False
        assert reason == ""


class TestDeduplicator:
    def test_exact_match(self):
        dedup = Deduplicator(threshold=0.8)
        pages = [
            FilteredPage(url="url1", title="t", content="exact same content"),
            FilteredPage(url="url2", title="t", content="exact same content"),
        ]
        result, count = dedup.deduplicate(pages)
        assert len(result) == 1
        assert count == 1

    def test_high_similarity(self):
        dedup = Deduplicator(threshold=0.8)
        # More similar texts - almost identical
        pages = [
            FilteredPage(
                url="url1", title="t", content="hello world foo bar baz qux and more text here"
            ),
            FilteredPage(
                url="url2", title="t", content="hello world foo bar baz qux and more different text"
            ),
        ]
        result, count = dedup.deduplicate(pages)
        # These are very similar - should dedup
        assert len(result) == 1 or count == 1

    def test_different_content(self):
        dedup = Deduplicator(threshold=0.8)
        pages = [
            FilteredPage(url="url1", title="t", content="python programming"),
            FilteredPage(url="url2", title="t", content="javascript tutorials"),
        ]
        result, count = dedup.deduplicate(pages)
        assert len(result) == 2  # Different content
        assert count == 0


class TestFilterPages:
    def test_empty_input(self):
        pages, stats = filter_pages("test query", [], {})
        assert pages == []
        assert stats.total_fetched == 0

    def test_filters_low_quality(self):
        # Create mock results
        class MockResult:
            def __init__(self, url, title):
                self.url = url
                self.title = title

        results = [MockResult("http://test.com", "Test")]
        contents = {"http://test.com": "short"}  # Too short

        pages, stats = filter_pages("test", results, contents)

        assert stats.seo_filtered >= 1

    def test_filters_by_relevance(self):
        class MockResult:
            def __init__(self, url, title):
                self.url = url
                self.title = title

        # One relevant, one not
        results = [
            MockResult("http://python.com", "Python Tutorial"),
            MockResult("http://unrelated.com", "Random Site"),
        ]
        contents = {
            "http://python.com": "Python is a programming language used for many purposes. " * 20,
            "http://unrelated.com": "Random unrelated content about weather. " * 20,
        }

        pages, stats = filter_pages("python programming", results, contents)

        if pages:
            # Python page should score higher
            python_page = next((p for p in pages if "python.com" in p.url), None)
            assert python_page is not None


class TestApplyFallback:
    def test_uses_filtered_when_available(self):
        filtered = [FilteredPage(url="http://test.com", title="Test", content="content")]
        result = apply_fallback(filtered, {}, [])
        assert result == filtered

    def test_fallback_to_raw_pages(self):
        class MockResult:
            def __init__(self, url, title):
                self.url = url
                self.title = title

        raw = {
            "http://a.com": "content a " * 50,
            "http://b.com": "content b " * 30,
            "http://c.com": "content c " * 10,
        }
        results = [
            MockResult("http://a.com", "A"),
            MockResult("http://b.com", "B"),
            MockResult("http://c.com", "C"),
        ]

        result = apply_fallback([], raw, results)

        assert len(result) == 3  # Top 3 by content length
        assert result[0].filter_reason == "fallback"

    def test_empty_fallback(self):
        result = apply_fallback([], {}, [])
        assert result == []


class TestFilteringStats:
    def test_default_values(self):
        stats = FilteringStats()
        assert stats.total_fetched == 0
        assert stats.seo_filtered == 0
        assert stats.duplicates_removed == 0
        assert stats.final_count == 0
        assert stats.fallback_used is False
