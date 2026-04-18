"""Chunk-based relevance retrieval for search pipeline.

Implements Perplexity-style chunking with custom BM25-like scoring:
- Split pages into word-bounded chunks (300-500 words, 50-100 word overlap)
- Score using keyword overlap (40%), BM25-style (30%), title boost (15%), position boost (15%)
- Select top-k with diversity (max 2 chunks per URL)
- Fallback to full-page context if insufficient chunks
- Quality thresholds and adaptive selection
- Cross-chunk deduplication and cross-source reinforcement
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from math import log

from app.core.logging import get_logger
from app.search.filtering import FilteredPage

logger = get_logger(__name__)

# Chunk configuration
CHUNK_MIN_WORDS = 300
CHUNK_MAX_WORDS = 500
CHUNK_OVERLAP_WORDS = 75

# Scoring weights
WEIGHT_KEYWORD_OVERLAP = 0.40
WEIGHT_BM25 = 0.30
WEIGHT_TITLE_BOOST = 0.15
WEIGHT_POSITION_BOOST = 0.15
WEIGHT_SOURCE_DIVERSITY = 0.20  # Cross-source reinforcement bonus

# Quality thresholds
MIN_CHUNK_QUALITY_THRESHOLD = 0.3  # Hard fallback to full-page
GRAY_ZONE_THRESHOLD = 0.45  # Expand chunk selection
GOOD_QUALITY_THRESHOLD = 0.7  # Reduce chunk selection

# Selection config
DEFAULT_TOP_K = 5
MAX_CHUNKS_PER_URL = 2

# Context limits
MAX_CHUNK_CONTEXT_CHARS = 4500  # Base limit
CONTEXT_BY_MODEL_SIZE = {
    128000: 6000,
    32000: 5000,
    16000: 4500,
}

# Deduplication threshold
DEDUP_SIMILARITY_THRESHOLD = 0.8

# Stopwords for tokenization (English + Russian)
STOPWORDS = {
    # English stopwords
    "the",
    "a",
    "an",
    "and",
    "or",
    "but",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "have",
    "has",
    "had",
    "do",
    "does",
    "did",
    "will",
    "would",
    "could",
    "should",
    "may",
    "might",
    "must",
    "shall",
    "can",
    "need",
    "dare",
    "ought",
    "used",
    "to",
    "of",
    "in",
    "for",
    "on",
    "with",
    "at",
    "by",
    "from",
    "as",
    "into",
    "through",
    "during",
    "before",
    "after",
    "above",
    "below",
    "between",
    "under",
    "again",
    "further",
    "then",
    "once",
    "here",
    "there",
    "when",
    "where",
    "why",
    "how",
    "all",
    "each",
    "few",
    "more",
    "most",
    "other",
    "some",
    "such",
    "no",
    "nor",
    "not",
    "only",
    "own",
    "same",
    "so",
    "than",
    "too",
    "very",
    "just",
    "also",
    "now",
    "what",
    "which",
    "who",
    "whom",
    "this",
    "that",
    "these",
    "those",
    "it",
    "its",
    "they",
    "them",
    "their",
    "he",
    "she",
    "him",
    "her",
    "his",
    "we",
    "you",
    "your",
    "i",
    "me",
    "my",
    "if",
    "about",
    "while",
    # Russian stopwords
    "и",
    "в",
    "не",
    "на",
    "я",
    "он",
    "с",
    "это",
    "а",
    "по",
    "что",
    "из",
    "к",
    "весь",
    "она",
    "как",
    "у",
    "мы",
    "ты",
    "же",
    "но",
    "да",
    "если",
    "только",
    "её",
    "их",
    "или",
    "так",
    "его",
    "бы",
    "за",
    "от",
    "до",
    "при",
    "для",
    "то",
    "можно",
    "был",
    "была",
    "было",
    "были",
    "ведь",
    "где",
    "когда",
    "куда",
    "почему",
    "чтобы",
    "кто",
    "какой",
    "какая",
    "какое",
    "какие",
    "нибудь",
    "ли",
    "вот",
    "тут",
}

ENTITY_STOPWORDS = {
    "this",
    "that",
    "these",
    "those",
    "today",
    "tomorrow",
    "yesterday",
    "now",
    "then",
    "here",
    "there",
    "where",
    "when",
    "what",
    "which",
    "who",
    "whom",
    "whose",
    "why",
    "how",
    "some",
    "any",
    "all",
    "each",
    "every",
    "both",
    "few",
    "many",
    "much",
    "more",
    "most",
    "less",
    "least",
    "such",
    "other",
    "another",
    "same",
    "different",
    "new",
    "old",
    "good",
    "bad",
    "best",
    "worst",
    "great",
    "small",
    "big",
    "large",
    "little",
    "это",
    "эта",
    "эти",
    "тот",
    "теперь",
    "сегодня",
    "вчера",
    "завтра",
    "здесь",
    "там",
    "где",
    "когда",
    "что",
    "кто",
    "как",
    "почему",
    "один",
    "два",
    "три",
    "четыре",
    "пять",
    "некоторые",
    "все",
    "каждый",
    "другой",
    "новый",
    "старый",
    "хороший",
    "плохой",
    "большой",
    "маленький",
}


@dataclass
class TextChunk:
    """A text chunk extracted from a page."""

    chunk_id: str
    url: str
    title: str
    chunk_text: str
    position_index: int
    word_count: int
    source_diversity_bonus: float = 0.0  # Cross-source boost


@dataclass
class ChunkScore:
    """Scoring details for a chunk."""

    chunk_id: str
    url: str
    score: float
    keyword_overlap: float
    bm25_score: float
    title_boost: float
    position_boost: float
    source_diversity_bonus: float = 0.0


@dataclass
class ChunkingStats:
    """Statistics about chunking process."""

    total_pages: int = 0
    total_chunks_created: int = 0
    chunks_selected_top_k: int = 0
    average_chunk_score: float = 0.0
    dropped_chunks_count: int = 0
    fallback_used: bool = False
    fallback_reason: str | None = None
    # NEW: Quality and logging fields
    avg_keyword_overlap: float = 0.0
    chunks_before_trim: int = 0
    chunks_after_trim: int = 0
    total_context_chars: int = 0
    cross_source_boost_applied: bool = False
    dedup_chunks_removed: int = 0
    quality_zone: str | None = None  # "low", "gray", "good"


def tokenize(text: str) -> list[str]:
    """Tokenize text into lowercase terms, removing stopwords."""
    text_lower = text.lower()
    terms = re.findall(r"\b[a-z0-9]{2,}\b", text_lower)
    return [t for t in terms if t not in STOPWORDS]


def extract_query_terms(query: str) -> list[str]:
    """Extract significant terms from query."""
    return tokenize(query)


def compute_idf(terms: list[str], all_chunks: list[TextChunk]) -> dict[str, float]:
    """Compute IDF for each term across all chunks."""
    n = len(all_chunks)
    if n == 0:
        return {}

    df: dict[str, int] = {}
    for chunk in all_chunks:
        chunk_terms = set(tokenize(chunk.chunk_text))
        for term in terms:
            if term in chunk_terms:
                df[term] = df.get(term, 0) + 1

    idf: dict[str, float] = {}
    for term, df_count in df.items():
        idf[term] = log((n + 1) / (df_count + 1))

    return idf


def jaccard_similarity(text1: str, text2: str) -> float:
    """Compute Jaccard similarity between two texts."""
    set1 = set(tokenize(text1))
    set2 = set(tokenize(text2))
    if not set1 or not set2:
        return 0.0
    return len(set1 & set2) / len(set1 | set2)


def _split_into_chunks(
    text: str,
    min_words: int = CHUNK_MIN_WORDS,
    max_words: int = CHUNK_MAX_WORDS,
    overlap_words: int = CHUNK_OVERLAP_WORDS,
) -> list[str]:
    """Split text into word-bounded chunks with overlap.

    First splits by paragraph boundaries, then coalesces to target size.
    """
    if not text or not text.strip():
        return []

    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current_chunk_words: list[str] = []
    current_word_count = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        para_words = para.split()

        for word in para_words:
            current_chunk_words.append(word)
            current_word_count += 1

            if current_word_count >= min_words:
                chunk_text = " ".join(current_chunk_words)
                chunks.append(chunk_text)

                if current_word_count > max_words:
                    current_chunk_words = current_chunk_words[-overlap_words:]
                    current_word_count = len(current_chunk_words)
                else:
                    current_chunk_words = []
                    current_word_count = 0

    if current_chunk_words and current_word_count >= min_words // 2:
        chunks.append(" ".join(current_chunk_words))

    return chunks


def chunk_page(page: FilteredPage, position_offset: int = 0) -> list[TextChunk]:
    """Split a filtered page into text chunks."""
    if not page.content or len(page.content.strip()) < 100:
        return []

    raw_chunks = _split_into_chunks(page.content)

    chunks = []
    for i, chunk_text in enumerate(raw_chunks):
        words = chunk_text.split()
        if len(words) < 50:
            continue

        chunks.append(
            TextChunk(
                chunk_id=f"{page.url[:50]}_{i}",
                url=page.url,
                title=page.title,
                chunk_text=chunk_text,
                position_index=position_offset + i,
                word_count=len(words),
            )
        )

    return chunks


def chunk_filtered_pages(pages: list[FilteredPage]) -> list[TextChunk]:
    """Chunk all filtered pages into a flat list of chunks."""
    all_chunks: list[TextChunk] = []
    position_counter = 0

    for page in pages:
        page_chunks = chunk_page(page, position_offset=position_counter)
        all_chunks.extend(page_chunks)
        position_counter += len(page_chunks)

    logger.debug(
        "chunks_created",
        pages=len(pages),
        total_chunks=len(all_chunks),
    )

    return all_chunks


def extract_entities(text: str) -> set[str]:
    """Extract entities using lightweight heuristics (no external deps).

    Extracts:
    - Capitalized words (not at sentence start)
    - Technical tokens (hyphens, dots, digits)
    - Frequent meaningful terms
    """
    if not text:
        return set()

    entities = set()
    words = text.split()

    for i, word in enumerate(words):
        clean = re.sub(r'[.,!?;:()\[\]"\'-]+$', "", word).lower()
        if len(clean) < 2:
            continue

        if clean in ENTITY_STOPWORDS:
            continue

        if i > 0 and word[0].isupper() and len(word) > 1:
            if word[0].isupper() and word[1].islower():
                entities.add(clean)

        if "-" in word or "." in word or any(c.isdigit() for c in word):
            entities.add(clean)

    return entities


def extract_key_entities(chunks: list[TextChunk], top_n: int = 10) -> set[str]:
    """Extract top-N frequent meaningful terms from chunks as key entities."""
    term_counts: dict[str, int] = {}

    for chunk in chunks:
        terms = tokenize(chunk.chunk_text)
        for term in terms:
            term_counts[term] = term_counts.get(term, 0) + 1

    sorted_terms = sorted(term_counts.items(), key=lambda x: x[1], reverse=True)
    return set(t[0] for t in sorted_terms[:top_n])


def compute_keyword_overlap(output: str, chunks: list[dict]) -> float:
    """Compute keyword overlap ratio between output and chunks."""
    output_terms = set(tokenize(output))

    if not output_terms or not chunks:
        return 0.0

    matches = 0
    for chunk in chunks:
        chunk_text = chunk.get("chunk_text", "")
        if chunk_text:
            chunk_terms = set(tokenize(chunk_text))
            overlap = len(output_terms & chunk_terms) / max(len(output_terms), 1)
            if overlap > 0.2:
                matches += 1

    return matches / max(len(chunks), 1)


def compute_entity_overlap(output: str, chunks: list[dict]) -> float:
    """Compute entity overlap ratio between output and chunks."""
    output_entities = extract_entities(output)

    if not output_entities or not chunks:
        return 0.0

    matches = 0
    for chunk in chunks:
        chunk_text = chunk.get("chunk_text", "")
        if chunk_text:
            chunk_entities = extract_entities(chunk_text)
            overlap = len(output_entities & chunk_entities) / max(len(output_entities), 1)
            if overlap > 0.15:
                matches += 1

    return matches / max(len(chunks), 1)


def _compute_source_diversity_bonus(
    chunks: list[TextChunk], avg_score: float = 1.0
) -> dict[str, float]:
    """Compute cross-source reinforcement bonus.

    Boost chunks that share keywords appearing in multiple sources.
    ONLY applies if >= 2 unique sources AND avg_score > 0.4.

    Args:
        chunks: List of text chunks
        avg_score: Average chunk quality score (for safety check)

    Returns:
        Dict of chunk_id -> bonus multiplier (0-0.2)
    """
    unique_urls = len(set(c.url for c in chunks))

    if unique_urls < 2 or avg_score <= 0.4:
        return {c.chunk_id: 0.0 for c in chunks}

    term_urls: dict[str, set[str]] = {}

    for chunk in chunks:
        terms = set(tokenize(chunk.chunk_text))
        for term in terms:
            if term not in term_urls:
                term_urls[term] = set()
            term_urls[term].add(chunk.url)

    bonuses: dict[str, float] = {c.chunk_id: 0.0 for c in chunks}

    for term, urls in term_urls.items():
        if len(urls) > 1:
            for chunk in chunks:
                if term in chunk.chunk_text.lower():
                    bonuses[chunk.chunk_id] += WEIGHT_SOURCE_DIVERSITY * (len(urls) - 1) * 0.1

    return bonuses


def _deduplicate_chunks(
    chunks: list[TextChunk],
    scores: list[ChunkScore],
    threshold: float = DEDUP_SIMILARITY_THRESHOLD,
) -> tuple[list[TextChunk], list[ChunkScore], int]:
    """Remove chunks with high similarity to higher-scoring chunks.

    Returns deduplicated chunks, scores, and count of removed chunks.
    """
    if len(chunks) <= 1:
        return chunks, scores, 0

    sorted_indices = sorted(range(len(scores)), key=lambda i: scores[i].score, reverse=True)

    to_remove: set[int] = set()
    chunk_by_idx = {i: chunks[i] for i in sorted_indices}

    for i in sorted_indices:
        if i in to_remove:
            continue
        for j in sorted_indices:
            if i == j or j in to_remove:
                continue
            sim = jaccard_similarity(chunk_by_idx[i].chunk_text, chunk_by_idx[j].chunk_text)
            if sim > threshold:
                to_remove.add(j)

    kept_chunks = [chunks[i] for i in range(len(chunks)) if i not in to_remove]
    kept_scores = [scores[i] for i in range(len(scores)) if i not in to_remove]

    return kept_chunks, kept_scores, len(to_remove)


def score_chunk(
    chunk: TextChunk,
    query_terms: list[str],
    idf: dict[str, float],
    source_diversity_bonus: float = 0.0,
    title_boost_threshold: int = 100,
) -> ChunkScore:
    """Score a single chunk using custom BM25-like algorithm."""
    title_lower = chunk.title.lower() if chunk.title else ""

    chunk_terms = tokenize(chunk.chunk_text)
    chunk_term_set = set(chunk_terms)

    keyword_overlap = 0.0
    if query_terms:
        matches = sum(1 for t in query_terms if t in chunk_term_set)
        keyword_overlap = matches / len(query_terms)

    bm25_score = 0.0
    if query_terms and idf:
        tf_scores: list[float] = []
        for term in query_terms:
            if term in chunk_term_set:
                tf = sum(1 for t in chunk_terms if t == term)
                tf_score = (tf * idf.get(term, 0)) / (tf + 1.2)
                tf_scores.append(tf_score)

        if tf_scores:
            max_possible = len(query_terms) * max(idf.values()) if idf else 1
            bm25_score = sum(tf_scores) / max(max_possible, 1)

    title_boost = 0.0
    first_100_chars = chunk.chunk_text[:title_boost_threshold].lower()
    for term in query_terms:
        if term in title_lower or term in first_100_chars:
            title_boost = 1.0
            break

    position_boost = max(0.1, 1.0 - (chunk.position_index * 0.10))

    final_score = (
        WEIGHT_KEYWORD_OVERLAP * keyword_overlap
        + WEIGHT_BM25 * bm25_score
        + WEIGHT_TITLE_BOOST * title_boost
        + WEIGHT_POSITION_BOOST * position_boost
    )

    if source_diversity_bonus > 0:
        final_score = final_score * (1 + source_diversity_bonus)

    return ChunkScore(
        chunk_id=chunk.chunk_id,
        url=chunk.url,
        score=final_score,
        keyword_overlap=keyword_overlap,
        bm25_score=bm25_score,
        title_boost=title_boost,
        position_boost=position_boost,
        source_diversity_bonus=source_diversity_bonus,
    )


def score_chunks(query: str, chunks: list[TextChunk]) -> list[ChunkScore]:
    """Score all chunks for relevance to query with cross-source reinforcement."""
    if not chunks:
        return []

    query_terms = extract_query_terms(query)

    if not query_terms:
        logger.warning("no_query_terms_for_scoring")
        return [
            ChunkScore(
                chunk_id=c.chunk_id,
                url=c.url,
                score=0.5,
                keyword_overlap=0.5,
                bm25_score=0.5,
                title_boost=0.5,
                position_boost=max(0.1, 1.0 - (c.position_index * 0.10)),
            )
            for c in chunks
        ]

    idf = compute_idf(query_terms, chunks)

    avg_idf = sum(idf.values()) / max(len(idf), 1) if idf else 0.0
    diversity_bonuses = _compute_source_diversity_bonus(chunks, avg_idf)

    scores = [
        score_chunk(c, query_terms, idf, diversity_bonuses.get(c.chunk_id, 0.0)) for c in chunks
    ]

    logger.debug(
        "chunks_scored",
        total=len(scores),
        avg_score=sum(s.score for s in scores) / max(len(scores), 1),
    )

    return scores


def _compute_adaptive_k(avg_score: float) -> int:
    """Compute adaptive chunk count based on average quality score."""
    if avg_score > GOOD_QUALITY_THRESHOLD:
        return 4
    elif avg_score >= GRAY_ZONE_THRESHOLD:
        return 5
    else:
        return 6


def _enforce_context_limit(
    chunks: list[TextChunk],
    scores: list[ChunkScore],
    max_chars: int,
) -> tuple[list[TextChunk], list[ChunkScore], int, int]:
    """Enforce context size limit by trimming lowest-scoring chunks.

    Returns: (trimmed_chunks, trimmed_scores, before_count, after_count)
    """
    if not chunks:
        return [], [], 0, 0

    before_count = len(chunks)

    score_chunk_pairs = list(zip(scores, chunks))
    score_chunk_pairs.sort(key=lambda x: x[0].score, reverse=True)

    kept_chunks: list[TextChunk] = []
    kept_scores: list[ChunkScore] = []
    total_chars = 0

    for score, chunk in score_chunk_pairs:
        if total_chars + len(chunk.chunk_text) > max_chars:
            break
        kept_chunks.append(chunk)
        kept_scores.append(score)
        total_chars += len(chunk.chunk_text)

    after_count = len(kept_chunks)

    logger.info(
        "context_trimmed",
        before_chunks=before_count,
        after_chunks=after_count,
        total_chars=total_chars,
        max_chars=max_chars,
    )

    return kept_chunks, kept_scores, before_count, after_count


def _detect_low_quality_chunks(
    chunks: list[TextChunk],
    scores: list[ChunkScore],
    min_keyword_overlap: float = 0.1,
) -> list[int]:
    """Detect chunks with very low keyword overlap (likely boilerplate).

    Returns list of indices to remove.
    """
    to_remove = []

    for i, score in enumerate(scores):
        if score.keyword_overlap < min_keyword_overlap:
            to_remove.append(i)

    if to_remove:
        logger.info(
            "low_quality_chunks_removed",
            count=len(to_remove),
            indices=to_remove,
        )

    return to_remove


def select_top_k_chunks(
    chunks: list[TextChunk],
    scores: list[ChunkScore],
    k: int = DEFAULT_TOP_K,
    max_per_url: int = MAX_CHUNKS_PER_URL,
    enforce_context_limit: bool = True,
    max_context_chars: int = MAX_CHUNK_CONTEXT_CHARS,
) -> tuple[list[TextChunk], int, int]:
    """Select top-k chunks with source diversity and context limit enforcement.

    Returns: (selected_chunks, chunks_before_trim, chunks_after_trim)
    """
    if not chunks or not scores:
        return [], 0, 0

    sorted_scores = sorted(scores, key=lambda s: s.score, reverse=True)

    selected: list[TextChunk] = []
    url_counts: dict[str, int] = {}

    chunk_map = {c.chunk_id: c for c in chunks}

    for score in sorted_scores:
        if len(selected) >= k:
            break

        url = score.url
        if url_counts.get(url, 0) >= max_per_url:
            continue

        chunk = chunk_map.get(score.chunk_id)
        if chunk:
            selected.append(chunk)
            url_counts[url] = url_counts.get(url, 0) + 1

    before_trim = len(selected)

    if enforce_context_limit:
        selected, _, before_trim, after_trim = _enforce_context_limit(
            selected,
            [s for s in scores if s.chunk_id in {c.chunk_id for c in selected}],
            max_context_chars,
        )
        return selected, before_trim, after_trim

    return selected, before_trim, before_trim


def build_chunk_context(
    chunks: list[TextChunk],
    max_chars: int = MAX_CHUNK_CONTEXT_CHARS,
) -> str:
    """Build context string from selected chunks for LLM.

    Args:
        chunks: Selected chunks to include
        max_chars: Maximum total characters

    Returns:
        Formatted context string
    """
    if not chunks:
        return ""

    parts: list[str] = []
    used_chars = 0

    for i, chunk in enumerate(chunks, start=1):
        if used_chars + len(chunk.chunk_text) > max_chars:
            remaining = max_chars - used_chars
            if remaining > 200:
                parts.append(
                    f"[Source {i}]\nURL: {chunk.url}\nContent:\n{chunk.chunk_text[:remaining]}...\n"
                )
            break

        parts.append(f"[Source {i}]\nURL: {chunk.url}\nContent:\n{chunk.chunk_text}\n")
        used_chars += len(chunk.chunk_text)

    return "\n---\n".join(parts)


def process_chunks(
    query: str,
    pages: list[FilteredPage],
    top_k: int = DEFAULT_TOP_K,
    max_per_url: int = MAX_CHUNKS_PER_URL,
    max_context_chars: int = MAX_CHUNK_CONTEXT_CHARS,
) -> tuple[str, list[dict], ChunkingStats]:
    """Main entry point: chunk pages, score, select, and build context.

    Args:
        query: User query for relevance scoring
        pages: Filtered pages to chunk
        top_k: Number of chunks to select (will be adapted based on quality)
        max_per_url: Max chunks per source URL
        max_context_chars: Max chars in final context

    Returns:
        Tuple of (context_string, selected_chunk_metadata, stats)
    """
    stats = ChunkingStats()
    stats.total_pages = len(pages)

    if not pages:
        stats.fallback_used = True
        stats.fallback_reason = "no_pages"
        stats.quality_zone = "low"
        return "", [], stats

    all_chunks = chunk_filtered_pages(pages)
    stats.total_chunks_created = len(all_chunks)

    if len(all_chunks) < 2:
        stats.fallback_used = True
        stats.fallback_reason = "insufficient_chunks"
        stats.quality_zone = "low"
        logger.warning(
            "chunking_fallback_insufficient",
            chunks=len(all_chunks),
        )
        return "", [], stats

    chunk_scores = score_chunks(query, all_chunks)

    avg_score = sum(s.score for s in chunk_scores) / max(len(chunk_scores), 1)
    stats.average_chunk_score = avg_score

    avg_keyword = sum(s.keyword_overlap for s in chunk_scores) / max(len(chunk_scores), 1)
    stats.avg_keyword_overlap = avg_keyword

    deduped_chunks, deduped_scores, dedup_removed = _deduplicate_chunks(all_chunks, chunk_scores)
    if dedup_removed > 0:
        all_chunks = deduped_chunks
        chunk_scores = deduped_scores
        stats.dedup_chunks_removed = dedup_removed
        logger.info("chunks_deduplicated", removed=dedup_removed)

    has_diversity_bonus = any(s.source_diversity_bonus > 0 for s in chunk_scores)
    stats.cross_source_boost_applied = has_diversity_bonus

    if avg_score < MIN_CHUNK_QUALITY_THRESHOLD:
        stats.fallback_used = True
        stats.fallback_reason = "low_chunk_quality"
        stats.quality_zone = "low"
        logger.warning(
            "chunking_fallback_low_quality",
            avg_score=avg_score,
            threshold=MIN_CHUNK_QUALITY_THRESHOLD,
        )
        return "", [], stats

    if avg_score < GRAY_ZONE_THRESHOLD:
        stats.quality_zone = "gray"
        adaptive_k = min(top_k + 1, 6)
    elif avg_score > GOOD_QUALITY_THRESHOLD:
        stats.quality_zone = "good"
        adaptive_k = max(top_k - 1, 4)
    else:
        stats.quality_zone = "good"
        adaptive_k = top_k

    logger.info(
        "chunking_quality_zone",
        zone=stats.quality_zone,
        avg_score=avg_score,
        adaptive_k=adaptive_k,
    )

    selected = select_top_k_chunks(
        all_chunks,
        chunk_scores,
        k=adaptive_k,
        max_per_url=max_per_url,
        enforce_context_limit=True,
        max_context_chars=max_context_chars,
    )

    if len(selected) < 2:
        stats.fallback_used = True
        stats.fallback_reason = "selection_too_few"
        stats.quality_zone = "low"
        logger.warning(
            "chunking_fallback_selection",
            selected=len(selected),
        )
        return "", [], stats

    stats.chunks_selected_top_k = len(selected)
    stats.dropped_chunks_count = len(all_chunks) - len(selected)

    context = build_chunk_context(selected, max_chars=max_context_chars)
    stats.total_context_chars = len(context)

    chunk_metadata = [
        {
            "url": c.url,
            "title": c.title,
            "chunk_id": c.chunk_id,
            "position_index": c.position_index,
            "word_count": c.word_count,
        }
        for c in selected
    ]

    logger.info(
        "chunking_complete",
        total_pages=stats.total_pages,
        total_chunks=stats.total_chunks_created,
        selected=stats.chunks_selected_top_k,
        avg_score=stats.average_chunk_score,
        avg_keyword_overlap=stats.avg_keyword_overlap,
        context_chars=stats.total_context_chars,
        quality_zone=stats.quality_zone,
        cross_source_boost=stats.cross_source_boost_applied,
        dedup_removed=stats.dedup_chunks_removed,
        fallback_used=stats.fallback_used,
    )

    return context, chunk_metadata, stats


def validate_answer_relevance(output: str, query: str) -> dict:
    """Check if answer is relevant to the query.

    Validates:
    - Answer contains key query terms
    - Answer length is sufficient
    - Answer is not generic

    Args:
        output: LLM response text
        query: Original user query

    Returns:
        Dict with keys: failed (bool), pattern (str | None), query_coverage (float)
    """
    from app.pipeline.prompt_builder import GROUNDING_FAILURE_PATTERNS

    if not output or not query:
        return {"failed": False, "pattern": None, "query_coverage": 0.0}

    output_lower = output.lower()
    query_terms = set(tokenize(query))
    output_terms = set(tokenize(output))

    for pattern in GROUNDING_FAILURE_PATTERNS:
        if pattern in output_lower:
            return {"failed": True, "pattern": pattern, "query_coverage": 0.0}

    query_coverage = len(output_terms & query_terms) / max(len(query_terms), 1)

    answer_len = len(output)

    if answer_len < 120:
        if query_coverage < 0.4:
            return {"failed": True, "pattern": "too_short", "query_coverage": query_coverage}
    elif answer_len < 300:
        if query_coverage < 0.3:
            return {"failed": True, "pattern": "weak_coverage", "query_coverage": query_coverage}

    if len(output_terms) < 3:
        return {"failed": False, "pattern": None, "query_coverage": query_coverage}

    return {"failed": False, "pattern": None, "query_coverage": query_coverage}


def validate_chunk_grounding(output: str, chunks: list[dict]) -> dict:
    """Validate if LLM response used provided chunks.

    Enhanced validation with:
    - Keyword overlap between answer and chunk terms
    - Entity overlap for semantic grounding
    - Absence of generic fallback phrases

    Args:
        output: LLM response text
        chunks: Selected chunks with 'chunk_text' key

    Returns:
        Dict with keys: failed (bool), pattern (str | None),
                        keyword_ratio (float), entity_ratio (float)
    """
    from app.pipeline.prompt_builder import GROUNDING_FAILURE_PATTERNS

    if not output or not chunks:
        return {"failed": False, "pattern": None, "keyword_ratio": 0.0, "entity_ratio": 0.0}

    output_lower = output.lower()

    for pattern in GROUNDING_FAILURE_PATTERNS:
        if pattern in output_lower:
            return {
                "failed": True,
                "pattern": pattern,
                "keyword_ratio": 0.0,
                "entity_ratio": 0.0,
            }

    keyword_ratio = compute_keyword_overlap(output, chunks)

    if len(output) < 150:
        if keyword_ratio < 0.25:
            return {
                "failed": True,
                "pattern": "low_keyword_overlap",
                "keyword_ratio": keyword_ratio,
                "entity_ratio": 0.0,
            }
        return {
            "failed": False,
            "pattern": None,
            "keyword_ratio": keyword_ratio,
            "entity_ratio": 0.0,
        }

    entity_ratio = compute_entity_overlap(output, chunks)

    if keyword_ratio < 0.25 or entity_ratio < 0.2:
        reason = "low_keyword_overlap" if keyword_ratio < 0.25 else "low_entity_overlap"
        return {
            "failed": True,
            "pattern": reason,
            "keyword_ratio": keyword_ratio,
            "entity_ratio": entity_ratio,
        }

    return {
        "failed": False,
        "pattern": None,
        "keyword_ratio": keyword_ratio,
        "entity_ratio": entity_ratio,
    }


def compute_confidence_score(
    grounding_ratio: float,
    query_coverage: float,
    avg_chunk_score: float,
) -> float:
    """Compute overall confidence score.

    confidence = 0.4 * grounding_ratio + 0.3 * query_coverage + 0.3 * avg_chunk_score

    Args:
        grounding_ratio: Keyword overlap ratio (0-1)
        query_coverage: Query term coverage in answer (0-1)
        avg_chunk_score: Average chunk quality score (0-1)

    Returns:
        Confidence score (0-1)
    """
    return 0.4 * grounding_ratio + 0.3 * query_coverage + 0.3 * avg_chunk_score
