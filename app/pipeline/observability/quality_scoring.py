"""
Quality-aware scoring for pipeline stages.

Extracts quality signals from stage outputs (review/critique/refine),
computes explainable quality scores, and integrates them into
suitability scoring as a bounded quality_adjustment.

Quality signals extracted:
- For review/critique/verify: issue detection from text analysis
  (severity keywords, correction indicators, negative sentiment markers)
- For refine: rewrite detection (diff-like signals, structural changes)
- For generate: downstream corrections count (how often review flags issues)
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

# Retention defaults
_MAX_AGE_DAYS = 30
_MAX_ENTRIES = 20_000

# Quality score bounds
_MIN_QUALITY = 0.0
_MAX_QUALITY = 1.0

# Max influence of quality on final score (bounded)
_MAX_QUALITY_ADJUSTMENT = 0.15

# ── Quality signal keywords ──

_CRITICAL_KEYWORDS = [
    "critical error", "fundamentally wrong", "completely incorrect",
    "major flaw", "dangerous", "unsafe", "hallucination",
]

_MAJOR_KEYWORDS = [
    "incorrect", "inaccurate", "missing", "incomplete", "wrong",
    "misleading", "outdated", "contradiction", "error", "flaw",
    "issue", "problem", "concern", "bias",
]

_MINOR_KEYWORDS = [
    "unclear", "could be improved", "minor", "typo", "formatting",
    "style", "wording", "awkward", "redundant",
]

_REWRITE_INDICATORS = [
    "rewritten", "revised", "improved", "corrected", "updated",
    "fixed", "changed", "modified", "restructured",
]


@dataclass(slots=True)
class QualitySignals:
    """Extracted quality signals from a stage output."""

    issue_count: int = 0
    critical_count: int = 0
    major_count: int = 0
    minor_count: int = 0

    # For refine: how much the text changed
    rewrite_ratio: float = 0.0  # 0.0 = no change, 1.0 = complete rewrite

    # For generate: corrections from downstream stages
    downstream_corrections: int = 0

    # Output characteristics
    output_length: int = 0
    has_structure: bool = False  # has headings, lists, etc.
    confidence: float = 0.5  # self-assessed confidence from text


@dataclass(slots=True)
class QualityMetrics:
    """Rolling quality metrics for a model+role combination."""

    model_id: str
    provider_id: str
    transport: str
    role: str

    avg_quality_score: float = 0.5
    sample_count: int = 0
    min_quality: float = 0.0
    max_quality: float = 1.0
    quality_stddev: float = 0.0

    # Sub-metrics
    avg_issue_count: float = 0.0
    avg_rewrite_ratio: float = 0.0


class QualityExtractor:
    """Extracts quality signals from stage output text.

    Uses heuristic text analysis — no ML, fully explainable.
    """

    def extract(self, output_text: str, role: str, input_text: str = "") -> QualitySignals:
        """Extract quality signals from stage output.

        Args:
            output_text: The stage output text.
            role: The stage role (generate, review, critique, refine, verify).
            input_text: The input to the stage (previous output) — used for diff signals.

        Returns:
            QualitySignals with extracted indicators.
        """
        signals = QualitySignals()
        signals.output_length = len(output_text) if output_text else 0

        if not output_text:
            return signals

        text_lower = output_text.lower()

        if role in ("review", "critique", "verify"):
            self._extract_review_signals(text_lower, signals)
        elif role == "refine":
            self._extract_refine_signals(text_lower, input_text, output_text, signals)
        elif role == "generate":
            self._extract_generate_signals(text_lower, signals)

        # Structure detection (applies to all roles)
        signals.has_structure = self._detect_structure(output_text)

        return signals

    def compute_quality_score(
        self,
        signals: QualitySignals,
        role: str,
    ) -> float:
        """Compute a quality score (0.0-1.0) from extracted signals.

        The formula is role-specific and fully explainable.

        For generate:
        - Higher output length with structure → better (up to a point)
        - Fewer downstream corrections → better

        For review/critique/verify:
        - More issues found → better review quality (the model is thorough)
        - Mix of severity levels → more useful review
        - But extremely high critical count may indicate the draft was very bad

        For refine:
        - Moderate rewrite ratio → good (not too little, not too much)
        - Maintains structure → good
        """
        if signals.output_length == 0:
            return 0.0

        if role == "generate":
            score = self._score_generate(signals)
        elif role in ("review", "critique", "verify"):
            score = self._score_review(signals, role)
        elif role == "refine":
            score = self._score_refine(signals)
        else:
            score = 0.5

        return max(_MIN_QUALITY, min(_MAX_QUALITY, score))

    def explain_score(
        self,
        signals: QualitySignals,
        quality_score: float,
        role: str,
    ) -> str:
        """Return a human-readable explanation of the quality score."""
        parts: list[str] = []

        if signals.output_length == 0:
            return "empty_output"

        if role == "generate":
            if signals.downstream_corrections > 3:
                parts.append(f"many_corrections({signals.downstream_corrections})")
            elif signals.downstream_corrections == 0:
                parts.append("no_corrections")
            if signals.has_structure:
                parts.append("structured")
            if signals.output_length < 100:
                parts.append("short_output")
            elif signals.output_length > 2000:
                parts.append("long_output")

        elif role in ("review", "critique", "verify"):
            if signals.issue_count == 0:
                parts.append("no_issues_found")
            elif signals.critical_count > 0:
                parts.append(f"critical_issues({signals.critical_count})")
            if signals.major_count > 2:
                parts.append(f"many_major_issues({signals.major_count})")

        elif role == "refine":
            if signals.rewrite_ratio < 0.1:
                parts.append("minimal_changes")
            elif signals.rewrite_ratio > 0.8:
                parts.append("heavy_rewrite")
            else:
                parts.append("moderate_refinement")

        driver = ", ".join(parts) if parts else "baseline"
        return f"quality={quality_score:.2f}: {driver}"

    # ── Signal extraction ──

    def _extract_review_signals(self, text: str, signals: QualitySignals) -> None:
        """Extract issue-related signals from review/critique/verify output."""
        # Count keyword matches
        signals.critical_count = sum(
            1 for kw in _CRITICAL_KEYWORDS if kw in text
        )
        signals.major_count = sum(
            1 for kw in _MAJOR_KEYWORDS if kw in text
        )
        signals.minor_count = sum(
            1 for kw in _MINOR_KEYWORDS if kw in text
        )
        signals.issue_count = (
            signals.critical_count * 3 +
            signals.major_count * 2 +
            signals.minor_count
        )

        # Confidence: look for confidence markers in text
        if any(w in text for w in ["i'm confident", "i am confident", "highly confident"]):
            signals.confidence = 0.9
        elif any(w in text for w in ["somewhat", "might", "could be", "possibly"]):
            signals.confidence = 0.6

    def _extract_refine_signals(self, text: str, input_text: str, output_text: str, signals: QualitySignals) -> None:
        """Extract rewrite/change signals from refine output."""
        # Rewrite ratio: how different is output from input
        if input_text and len(input_text) > 0:
            signals.rewrite_ratio = self._compute_rewrite_ratio(input_text, output_text)

        # Rewrite indicators
        signals.issue_count = sum(
            1 for kw in _REWRITE_INDICATORS if kw in text
        )

    def _extract_generate_signals(self, text: str, signals: QualitySignals) -> None:
        """Extract baseline quality signals from generate output."""
        # Length score: reasonable length is better
        # Very short outputs are lower quality
        if signals.output_length < 50:
            signals.confidence = 0.3
        elif signals.output_length < 500:
            signals.confidence = 0.7
        else:
            signals.confidence = 0.8

    # ── Scoring ──

    def _score_generate(self, signals: QualitySignals) -> float:
        """Score for generate stage.

        Factors:
        - Output length with structure: 30%
        - Structure: 20%
        - Downstream corrections: 50% (fewer = better)
        """
        # Length score: sigmoid-like curve, peaks at ~1000 chars
        length_score = min(1.0, signals.output_length / 1000.0) * 0.3

        # Structure bonus
        structure_score = 0.2 if signals.has_structure else 0.05

        # Downstream corrections: the main cross-stage signal
        # 0 corrections = 1.0, 1-2 = 0.7, 3-5 = 0.4, 6+ = 0.1
        if signals.downstream_corrections == 0:
            correction_score = 1.0
        elif signals.downstream_corrections <= 2:
            correction_score = 0.7
        elif signals.downstream_corrections <= 5:
            correction_score = 0.4
        else:
            correction_score = 0.1

        return length_score + structure_score + correction_score * 0.5

    def _score_review(self, signals: QualitySignals, role: str) -> float:
        """Score for review/critique/verify stage.

        A good review finds real issues (not zero, not excessive).
        Factors:
        - Issue detection: found some issues (not 0, not extreme) → better
        - Critical issues found → thorough review
        - Output structure → organized feedback
        """
        # Issue detection: sweet spot is 1-5 issues
        issue_score = 0.0
        if signals.issue_count == 0:
            issue_score = 0.2  # May have missed issues
        elif signals.issue_count <= 5:
            issue_score = 0.5  # Good balance
        elif signals.issue_count <= 10:
            issue_score = 0.4  # Many issues — thorough but may be nitpicking
        else:
            issue_score = 0.3  # Excessive — likely over-critical

        # Critical issues are valuable
        critical_score = min(0.2, signals.critical_count * 0.1)

        # Structure: organized review is better
        structure_score = 0.15 if signals.has_structure else 0.05

        return issue_score + critical_score + structure_score

    def _score_refine(self, signals: QualitySignals) -> float:
        """Score for refine stage.

        A good refine makes meaningful but not extreme changes.
        Factors:
        - Moderate rewrite ratio (0.1-0.6): good
        - Too little (< 0.05): not useful
        - Too much (> 0.8): may have lost the original intent
        - Structure maintained: good
        """
        ratio = signals.rewrite_ratio

        # Sweet spot: 0.1 to 0.6
        if ratio < 0.05:
            rewrite_score = 0.1  # Almost no change
        elif ratio < 0.1:
            rewrite_score = 0.3
        elif ratio <= 0.6:
            rewrite_score = 0.5  # Good refinement
        elif ratio <= 0.8:
            rewrite_score = 0.35  # Heavy but acceptable
        else:
            rewrite_score = 0.15  # Near-complete rewrite

        structure_score = 0.15 if signals.has_structure else 0.05

        return rewrite_score + structure_score

    # ── Helpers ──

    @staticmethod
    def _compute_rewrite_ratio(input_text: str, output_text: str) -> float:
        """Compute a rough rewrite ratio using character-level diff.

        Uses a simple approach: fraction of output that differs from input.
        Not a true edit distance (too expensive), but a practical proxy.
        """
        if not input_text:
            return 1.0

        input_lines = input_text.splitlines()
        output_lines = output_text.splitlines()

        if not input_lines and not output_lines:
            return 0.0
        if not input_lines or not output_lines:
            return 1.0

        # Line-level diff: fraction of lines changed
        input_set = set(input_lines)
        output_set = set(output_lines)

        common = len(input_set & output_set)
        total = len(input_set | output_set)

        if total == 0:
            return 0.0

        return 1.0 - (common / total)

    @staticmethod
    def _detect_structure(text: str) -> bool:
        """Detect if text has structure (headings, lists, sections)."""
        structure_patterns = [
            r"\n#+ ",           # Markdown headings
            r"\n\d+[\.\)] ",    # Numbered lists
            r"\n[-*] ",         # Bullet lists
            r"\n\n",            # Paragraph breaks
            r"\*\*.*\*\*",      # Bold text
            r"^\s*#{1,6}",      # Heading at start of line
        ]
        return any(re.search(p, text, re.MULTILINE) for p in structure_patterns)


class QualityMetricsStore:
    """SQLite-backed quality metrics storage.

    Stores per-execution quality scores with rolling window queries.
    """

    _CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS quality_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        model_id TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        transport TEXT NOT NULL,
        role TEXT NOT NULL,
        quality_score REAL NOT NULL,
        issue_count INTEGER NOT NULL DEFAULT 0,
        critical_count INTEGER NOT NULL DEFAULT 0,
        major_count INTEGER NOT NULL DEFAULT 0,
        minor_count INTEGER NOT NULL DEFAULT 0,
        rewrite_ratio REAL NOT NULL DEFAULT 0.0,
        output_length INTEGER NOT NULL DEFAULT 0,
        has_structure INTEGER NOT NULL DEFAULT 0,
        explanation TEXT,
        -- Cross-stage signals
        downstream_corrections INTEGER NOT NULL DEFAULT 0,
        correction_severity REAL NOT NULL DEFAULT 0.0,
        review_actionability REAL NOT NULL DEFAULT 0.5,
        refine_effectiveness REAL NOT NULL DEFAULT 0.5,
        final_improvement_score REAL NOT NULL DEFAULT 0.5,
        cross_stage_explanation TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_qm_model_role_ts ON quality_metrics(model_id, role, timestamp);
    CREATE INDEX IF NOT EXISTS idx_qm_timestamp ON quality_metrics(timestamp);
    """

    def __init__(
        self,
        db_path: str = "data/quality_metrics.db",
        max_entries: int = _MAX_ENTRIES,
        max_age_days: int = _MAX_AGE_DAYS,
    ) -> None:
        self._db_path = db_path
        self._max_entries = max_entries
        self._max_age_days = max_age_days
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        path = Path(self._db_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(self._CREATE_TABLE_SQL)
            conn.commit()
        except sqlite3.Error as exc:
            logger.error("quality_metrics_db_init_failed", error=str(exc))
            self._db_path = ":memory:"
            conn = self._connect()
            conn.executescript(self._CREATE_TABLE_SQL)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def record(
        self,
        model_id: str,
        provider_id: str,
        transport: str,
        role: str,
        quality_score: float,
        signals: QualitySignals,
        explanation: str = "",
        cross: CrossStageSignals | None = None,
    ) -> None:
        """Record a quality metrics entry.

        Args:
            model_id: The model that produced the output.
            provider_id: The provider that served the model.
            transport: The transport type (browser, api, agent).
            role: The stage role (generate, review, critique, refine, verify).
            quality_score: Computed quality score (0.0-1.0).
            signals: Extracted quality signals.
            explanation: Human-readable explanation of the score.
            cross: Optional cross-stage signals for this execution.
        """
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO quality_metrics (
                    timestamp, model_id, provider_id, transport, role,
                    quality_score, issue_count, critical_count, major_count, minor_count,
                    rewrite_ratio, output_length, has_structure, explanation,
                    downstream_corrections, correction_severity,
                    review_actionability, refine_effectiveness,
                    final_improvement_score, cross_stage_explanation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(),
                    model_id,
                    provider_id,
                    transport,
                    role,
                    quality_score,
                    signals.issue_count,
                    signals.critical_count,
                    signals.major_count,
                    signals.minor_count,
                    signals.rewrite_ratio,
                    signals.output_length,
                    1 if signals.has_structure else 0,
                    explanation,
                    cross.downstream_corrections if cross else 0,
                    cross.correction_severity if cross else 0.0,
                    cross.review_actionability if cross else 0.5,
                    cross.refine_effectiveness if cross else 0.5,
                    cross.final_improvement_score if cross else 0.5,
                    cross.cross_stage_explanation if cross else "",
                ),
            )
            conn.commit()
            self._enforce_retention(conn)
        except sqlite3.Error as exc:
            logger.debug("quality_metrics_record_failed", error=str(exc))
        finally:
            conn.close()

    def get_avg_quality(
        self,
        model_id: str,
        role: str,
        window: int = 100,
    ) -> float:
        """Get rolling average quality score."""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT AVG(quality_score) as avg_q
                FROM (
                    SELECT quality_score FROM quality_metrics
                    WHERE model_id = ? AND role = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                )
                """,
                (model_id, role, window),
            ).fetchone()

            if row and row["avg_q"] is not None:
                return row["avg_q"]
            return 0.5
        except sqlite3.Error:
            return 0.5
        finally:
            conn.close()

    def get_sample_count(
        self,
        model_id: str,
        role: str,
        window: int = 100,
    ) -> int:
        """Get exact sample count for quality metrics."""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) as cnt
                FROM (
                    SELECT id FROM quality_metrics
                    WHERE model_id = ? AND role = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                )
                """,
                (model_id, role, window),
            ).fetchone()
            return row["cnt"] if row else 0
        except sqlite3.Error:
            return 0
        finally:
            conn.close()

    def get_quality_metrics(
        self,
        model_id: str,
        role: str,
        window: int = 100,
    ) -> QualityMetrics:
        """Get full quality metrics for a model+role."""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) as sample_count,
                    AVG(quality_score) as avg_quality,
                    MIN(quality_score) as min_quality,
                    MAX(quality_score) as max_quality,
                    AVG(issue_count) as avg_issues,
                    AVG(rewrite_ratio) as avg_rewrite
                FROM (
                    SELECT quality_score, issue_count, rewrite_ratio
                    FROM quality_metrics
                    WHERE model_id = ? AND role = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                )
                """,
                (model_id, role, window),
            ).fetchone()

            if row and row["sample_count"] > 0:
                # Compute stddev
                stddev = self._compute_stddev(conn, model_id, role, window)
                return QualityMetrics(
                    model_id=model_id,
                    provider_id="",  # Not tracked at this granularity
                    transport="",
                    role=role,
                    avg_quality_score=row["avg_quality"],
                    sample_count=row["sample_count"],
                    min_quality=row["min_quality"],
                    max_quality=row["max_quality"],
                    quality_stddev=stddev,
                    avg_issue_count=row["avg_issues"] or 0.0,
                    avg_rewrite_ratio=row["avg_rewrite"] or 0.0,
                )
            return QualityMetrics(
                model_id=model_id, provider_id="", transport="", role=role,
            )
        except sqlite3.Error:
            return QualityMetrics(
                model_id=model_id, provider_id="", transport="", role=role,
            )
        finally:
            conn.close()

    def get_all_quality_summary(
        self,
        role: str | None = None,
        window: int = 100,
    ) -> list[dict[str, Any]]:
        """Get quality summary for all model+role combinations."""
        conn = self._connect()
        try:
            where = ""
            params: list[Any] = []
            if role:
                where = "WHERE role = ?"
                params.append(role)

            rows = conn.execute(
                f"""
                SELECT
                    model_id,
                    provider_id,
                    transport,
                    role,
                    COUNT(*) as sample_count,
                    ROUND(AVG(quality_score), 3) as avg_quality,
                    ROUND(MIN(quality_score), 3) as min_quality,
                    ROUND(MAX(quality_score), 3) as max_quality,
                    ROUND(AVG(issue_count), 1) as avg_issues,
                    ROUND(AVG(rewrite_ratio), 3) as avg_rewrite,
                    ROUND(AVG(downstream_corrections), 1) as avg_downstream_corrections,
                    ROUND(AVG(correction_severity), 3) as avg_correction_severity,
                    ROUND(AVG(review_actionability), 3) as avg_review_actionability,
                    ROUND(AVG(refine_effectiveness), 3) as avg_refine_effectiveness,
                    ROUND(AVG(final_improvement_score), 3) as avg_final_improvement_score
                FROM quality_metrics
                {where}
                GROUP BY model_id, role
                HAVING sample_count >= 1
                ORDER BY avg_quality DESC
                """,
                params,
            ).fetchall()

            return [dict(r) for r in rows]
        except sqlite3.Error as exc:
            logger.debug("quality_metrics_get_all_failed", error=str(exc))
            return []
        finally:
            conn.close()

    def get_latest_timestamps_by_role(
        self,
        model_role_pairs: list[tuple[str, str]],
    ) -> dict[tuple[str, str], float]:
        """Get the most recent quality metric timestamps for multiple model+role pairs in one query.

        Args:
            model_role_pairs: List of (model_id, role) tuples to look up.

        Returns:
            Dict mapping (model_id, role) to latest timestamp.
            Pairs with no entries are omitted from the result.
        """
        if not model_role_pairs:
            return {}

        conn = self._connect()
        try:
            model_ids = sorted({mr[0] for mr in model_role_pairs})
            roles = sorted({mr[1] for mr in model_role_pairs})
            model_placeholders = ",".join("?" for _ in model_ids)
            role_placeholders = ",".join("?" for _ in roles)
            query = f"""
                SELECT model_id, role, MAX(timestamp) as latest
                FROM quality_metrics
                WHERE model_id IN ({model_placeholders})
                  AND role IN ({role_placeholders})
                GROUP BY model_id, role
            """
            rows = conn.execute(query, model_ids + roles).fetchall()
            return {(row["model_id"], row["role"]): row["latest"] for row in rows}
        except sqlite3.Error:
            return {}
        finally:
            conn.close()

    def get_latest_timestamps(self, model_ids: list[str]) -> dict[str, float]:
        """Get the most recent quality metric timestamps for multiple models in one query.

        Args:
            model_ids: List of model IDs to look up.

        Returns:
            Dict mapping model_id to latest timestamp.
            Models with no entries are omitted from the result.
        """
        if not model_ids:
            return {}

        conn = self._connect()
        try:
            placeholders = ",".join("?" for _ in model_ids)
            query = f"""
                SELECT model_id, MAX(timestamp) as latest
                FROM quality_metrics
                WHERE model_id IN ({placeholders})
                GROUP BY model_id
            """
            rows = conn.execute(query, model_ids).fetchall()
            return {row["model_id"]: row["latest"] for row in rows}
        except sqlite3.Error:
            return {}
        finally:
            conn.close()

    def get_latest_timestamp(self, model_id: str) -> float:
        """Get the most recent quality metric timestamp for a model.

        Returns:
            Unix timestamp of the most recent quality_metrics entry,
            or 0.0 if no entries exist for this model.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT MAX(timestamp) as latest FROM quality_metrics WHERE model_id = ?",
                (model_id,),
            ).fetchone()
            return row["latest"] if row and row["latest"] else 0.0
        except sqlite3.Error:
            return 0.0
        finally:
            conn.close()

    def cleanup(self) -> int:
        """Run retention cleanup. Returns rows deleted."""
        conn = self._connect()
        try:
            count_before = conn.execute("SELECT COUNT(*) FROM quality_metrics").fetchone()[0]
            self._enforce_retention(conn)
            conn.commit()
            count_after = conn.execute("SELECT COUNT(*) FROM quality_metrics").fetchone()[0]
            return count_before - count_after
        except sqlite3.Error:
            return 0
        finally:
            conn.close()

    def _enforce_retention(self, conn: sqlite3.Connection) -> None:
        """Enforce max entries and max age."""
        if self._max_age_days > 0:
            cutoff = time.time() - (self._max_age_days * 86400)
            conn.execute("DELETE FROM quality_metrics WHERE timestamp < ?", (cutoff,))

        conn.execute(
            """
            DELETE FROM quality_metrics
            WHERE id NOT IN (
                SELECT id FROM quality_metrics
                ORDER BY timestamp DESC
                LIMIT ?
            )
            """,
            (self._max_entries,),
        )
        conn.commit()

    @staticmethod
    def _compute_stddev(
        conn: sqlite3.Connection,
        model_id: str,
        role: str,
        window: int,
    ) -> float:
        """Compute standard deviation of quality scores."""
        rows = conn.execute(
            """
            SELECT quality_score FROM quality_metrics
            WHERE model_id = ? AND role = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (model_id, role, window),
        ).fetchall()

        if len(rows) < 2:
            return 0.0

        scores = [r["quality_score"] for r in rows]
        mean = sum(scores) / len(scores)
        variance = sum((s - mean) ** 2 for s in scores) / len(scores)
        return variance ** 0.5


# Global singletons
quality_extractor = QualityExtractor()
quality_metrics_store = QualityMetricsStore()


# ── Cross-Stage Quality Linking ──


@dataclass(slots=True)
class CrossStageSignals:
    """Cross-stage quality signals computed from a full pipeline execution.

    Links generate → review/critique → refine → final outputs to derive
    downstream quality signals that individual stage scoring cannot see.
    """

    # For generate stage
    downstream_corrections: int = 0  # total issues found by review/critique
    correction_severity: float = 0.0  # 0.0-1.0, weighted by severity
    refine_rewrite_ratio: float = 0.0  # how much refine rewrote the generate output
    final_differs_materially: bool = False  # final output significantly different from generate

    # For review/critique stage
    review_actionability: float = 0.5  # 0.0-1.0, were findings actually acted on?
    issues_addressed_ratio: float = 0.0  # fraction of review issues that refine addressed
    review_was_noise: bool = False  # review found issues but refine didn't change anything

    # For refine stage
    refine_effectiveness: float = 0.5  # 0.0-1.0, did refine actually improve things?
    issues_fixed_ratio: float = 0.0  # fraction of review issues that were fixed
    unnecessary_full_rewrite: bool = False  # rewrote everything when targeted fixes would suffice
    refinement_improved_structure: bool = False

    # Overall
    final_improvement_score: float = 0.5  # 0.0-1.0, is the final output better than the draft?
    cross_stage_explanation: str = ""  # human-readable explanation


class CrossStageAnalyzer:
    """Analyzes cross-stage quality signals within a pipeline execution.

    Takes all stage outputs and derives linking metrics:
    - How many review issues were actually addressed by refine?
    - Did refine make targeted improvements or a blind full rewrite?
    - Was the generate draft fundamentally flawed (needed heavy refinement)?

    All metrics are heuristic-based and fully explainable — no ML.
    """

    def analyze(
        self,
        stage_outputs: dict[str, str],  # stage_id → full output text
        stage_roles: dict[str, str],  # stage_id → role
        stage_signals: dict[str, QualitySignals],  # stage_id → already-extracted signals
    ) -> CrossStageSignals:
        """Compute cross-stage signals from a complete pipeline execution.

        Args:
            stage_outputs: Full output text per stage_id.
            stage_roles: Role name per stage_id.
            stage_signals: Pre-extracted quality signals per stage_id.

        Returns:
            CrossStageSignals with linking metrics.
        """
        result = CrossStageSignals()

        # Identify stages by role
        generate_id = self._find_stage(stage_roles, "generate")
        review_ids = self._find_stages(stage_roles, ("review", "critique"))
        refine_id = self._find_stage(stage_roles, "refine")

        if not generate_id or generate_id not in stage_outputs:
            return result  # No generate stage → nothing to link

        generate_output = stage_outputs.get(generate_id, "")

        # ── Compute review findings ──
        review_issues = 0
        review_critical = 0
        review_major = 0
        for rid in review_ids:
            sig = stage_signals.get(rid)
            if sig:
                review_issues += sig.issue_count
                review_critical += sig.critical_count
                review_major += sig.major_count

        result.downstream_corrections = review_issues
        result.correction_severity = self._compute_severity_score(
            review_critical, review_major, review_issues,
        )

        # ── Compute refine signals ──
        if refine_id and refine_id in stage_outputs:
            refine_output = stage_outputs[refine_id]
            refine_input = generate_output  # refine typically takes generate as input

            rewrite_ratio = QualityExtractor._compute_rewrite_ratio(
                refine_input, refine_output,
            )
            result.refine_rewrite_ratio = rewrite_ratio

            # Check if refine addressed review issues
            if review_issues > 0 and refine_output:
                result.issues_addressed_ratio = self._compute_issues_addressed(
                    review_ids, stage_signals, refine_output,
                )
                result.review_actionability = result.issues_addressed_ratio
            elif review_issues == 0:
                # Review found nothing — check if refine still made changes
                if rewrite_ratio < 0.05:
                    result.review_actionability = 0.5  # Neutral — nothing to act on
                    result.review_was_noise = False
                else:
                    # Review said nothing but refine still changed a lot
                    result.review_actionability = 0.3  # Review may have missed issues
                    result.review_was_noise = True
            else:
                result.review_actionability = 0.3

            # Refine effectiveness
            result.refine_effectiveness = self._compute_refine_effectiveness(
                generate_output, refine_output, review_issues,
                result.issues_addressed_ratio, rewrite_ratio,
            )
            result.issues_fixed_ratio = result.issues_addressed_ratio

            # Unnecessary full rewrite detection
            if rewrite_ratio > 0.8 and review_issues < 3:
                result.unnecessary_full_rewrite = True

            # Structure improvement
            gen_has_structure = QualityExtractor._detect_structure(generate_output)
            ref_has_structure = QualityExtractor._detect_structure(refine_output)
            result.refinement_improved_structure = (
                not gen_has_structure and ref_has_structure
            )

            # Final differs materially from generate
            result.final_differs_materially = rewrite_ratio > 0.5
        else:
            # No refine stage — final = generate (or generate was directly used)
            result.refine_effectiveness = 0.5
            result.final_differs_materially = False

        # Final improvement score
        result.final_improvement_score = self._compute_final_improvement(
            result.refine_effectiveness,
            result.refine_rewrite_ratio,
            result.correction_severity,
            review_ids,
            stage_signals,
        )

        # Explanation
        result.cross_stage_explanation = self._explain(result, review_issues)

        return result

    def apply_to_signals(
        self,
        stage_signals: dict[str, QualitySignals],
        stage_roles: dict[str, str],
        cross: CrossStageSignals,
    ) -> None:
        """Update QualitySignals with cross-stage corrections.

        Modifies signals in-place:
        - generate.downstream_corrections = cross.downstream_corrections
        - generate confidence reduced if many corrections
        """
        generate_id = self._find_stage(stage_roles, "generate")
        if generate_id and generate_id in stage_signals:
            gen_sig = stage_signals[generate_id]
            gen_sig.downstream_corrections = cross.downstream_corrections

            # Reduce confidence if many downstream corrections
            if cross.downstream_corrections > 5:
                gen_sig.confidence = max(0.2, gen_sig.confidence - 0.3)
            elif cross.downstream_corrections > 2:
                gen_sig.confidence = max(0.4, gen_sig.confidence - 0.15)

    def adjust_quality_score(
        self,
        base_score: float,
        role: str,
        cross: CrossStageSignals,
        stage_id: str = "",
        stage_roles: dict[str, str] | None = None,
    ) -> tuple[float, str]:
        """Adjust a quality score based on cross-stage signals.

        Returns (adjusted_score, reason).

        For generate: penalized by downstream correction severity.
        For review: rewarded/punished by actionability.
        For refine: rewarded/punished by effectiveness.
        """
        adjustment = 0.0
        reasons: list[str] = []

        if role == "generate":
            # Penalize for downstream corrections
            if cross.downstream_corrections > 5:
                adjustment -= 0.20
                reasons.append(f"many_downstream_corrections({cross.downstream_corrections})")
            elif cross.downstream_corrections > 2:
                adjustment -= 0.10
                reasons.append(f"moderate_corrections({cross.downstream_corrections})")

            # Penalize if refine had to do a full rewrite
            if cross.refine_rewrite_ratio > 0.8:
                adjustment -= 0.10
                reasons.append("heavy_refine_needed")

            # Penalize for high severity corrections
            if cross.correction_severity > 0.6:
                adjustment -= 0.05
                reasons.append("high_severity_issues")

        elif role in ("review", "critique"):
            # Reward actionable reviews
            if cross.review_actionability >= 0.7:
                adjustment += 0.10
                reasons.append("actionable_review")
            elif cross.review_actionability < 0.3 and cross.downstream_corrections > 0:
                adjustment -= 0.10
                reasons.append("review_not_actionable")

            # Penalize noise reviews (found issues that weren't real)
            if cross.review_was_noise:
                adjustment -= 0.08
                reasons.append("review_was_noise")

        elif role == "refine":
            # Reward effective refinement
            if cross.refine_effectiveness >= 0.7:
                adjustment += 0.10
                reasons.append("effective_refinement")
            elif cross.refine_effectiveness < 0.3:
                adjustment -= 0.10
                reasons.append("ineffective_refinement")

            # Penalize unnecessary full rewrites
            if cross.unnecessary_full_rewrite:
                adjustment -= 0.08
                reasons.append("unnecessary_full_rewrite")

            # Reward structure improvement
            if cross.refinement_improved_structure:
                adjustment += 0.05
                reasons.append("improved_structure")

        adjusted = max(_MIN_QUALITY, min(_MAX_QUALITY, base_score + adjustment))
        reason = ", ".join(reasons) if reasons else "no_cross_stage_impact"

        return adjusted, reason

    # ── Internal computation methods ──

    def _compute_issues_addressed(
        self,
        review_ids: list[str],
        stage_signals: dict[str, QualitySignals],
        refine_output: str,
    ) -> float:
        """Estimate what fraction of review issues were addressed by refine.

        Heuristic: check if review-issue-related terms appear less in refine
        output, and whether refinement indicators are present.
        """
        refine_lower = refine_output.lower()

        # Check for fix/improvement indicators in refine output
        fix_indicators = ["fixed", "corrected", "addressed", "resolved", "improved", "updated"]
        fix_count = sum(1 for ind in fix_indicators if ind in refine_lower)

        # Check if refinement output still contains negative critique patterns
        negative_patterns = ["incorrect", "wrong", "missing", "error", "problem", "issue"]
        still_negative = sum(1 for p in negative_patterns if p in refine_lower)

        if fix_count > 0 and still_negative == 0:
            return min(1.0, 0.5 + fix_count * 0.15)
        elif fix_count > 0:
            return 0.4 + fix_count * 0.1
        elif still_negative > 2:
            return 0.2  # Issues still present
        else:
            return 0.5  # Neutral — can't determine

    def _compute_severity_score(
        self, critical: int, major: int, total: int,
    ) -> float:
        """Compute weighted severity score from issue counts."""
        if total == 0:
            return 0.0
        weighted = critical * 3 + major * 2 + (total - critical - major) * 1
        max_possible = total * 3
        return weighted / max_possible if max_possible > 0 else 0.0

    def _compute_refine_effectiveness(
        self,
        generate_output: str,
        refine_output: str,
        review_issues: int,
        issues_addressed: float,
        rewrite_ratio: float,
    ) -> float:
        """Compute how effective the refine stage was.

        Effective = addressed review issues + maintained/improved structure
        - moderate rewrite, not a blind full rewrite.
        """
        score = 0.5  # Baseline

        # Issues addressed is the primary signal
        if review_issues > 0:
            score = issues_addressed * 0.6 + 0.2
        else:
            # No issues to address — check if refine made useful changes
            if rewrite_ratio < 0.05:
                score = 0.3  # Almost no change
            elif 0.05 <= rewrite_ratio <= 0.5:
                score = 0.6  # Moderate improvement
            else:
                score = 0.4  # Large change without clear need

        # Bonus for structure improvement
        gen_structured = QualityExtractor._detect_structure(generate_output)
        ref_structured = QualityExtractor._detect_structure(refine_output)
        if not gen_structured and ref_structured:
            score += 0.1

        return max(0.0, min(1.0, score))

    def _compute_final_improvement(
        self,
        refine_effectiveness: float,
        refine_rewrite_ratio: float,
        correction_severity: float,
        review_ids: list[str],
        stage_signals: dict[str, QualitySignals],
    ) -> float:
        """Compute overall final improvement score.

        High if: refine was effective, corrections were addressed, structure improved.
        Low if: heavy rewrite with low effectiveness, high-severity issues remain.
        """
        score = 0.5

        # Refine effectiveness is the main driver
        score += (refine_effectiveness - 0.5) * 0.4

        # Moderate rewrite is better than no rewrite or total rewrite
        if 0.1 <= refine_rewrite_ratio <= 0.6:
            score += 0.1
        elif refine_rewrite_ratio > 0.8:
            score -= 0.1

        # High correction severity that was addressed = good outcome
        if correction_severity > 0.5 and refine_effectiveness > 0.6:
            score += 0.1

        return max(0.0, min(1.0, score))

    def _explain(self, cross: CrossStageSignals, review_issues: int) -> str:
        """Generate human-readable explanation of cross-stage analysis."""
        parts: list[str] = []

        if cross.downstream_corrections > 0:
            sev = "high" if cross.correction_severity > 0.6 else "moderate" if cross.correction_severity > 0.3 else "low"
            parts.append(f"generate: {cross.downstream_corrections} downstream corrections ({sev} severity)")

        if review_issues > 0:
            action = "actionable" if cross.review_actionability >= 0.6 else "partially actionable" if cross.review_actionability >= 0.3 else "not actionable"
            parts.append(f"review: {action} ({cross.review_actionability:.2f})")

        if cross.refine_effectiveness > 0:
            eff = "effective" if cross.refine_effectiveness >= 0.6 else "moderately effective" if cross.refine_effectiveness >= 0.4 else "ineffective"
            parts.append(f"refine: {eff} ({cross.refine_effectiveness:.2f})")

        if cross.unnecessary_full_rewrite:
            parts.append("refine: unnecessary full rewrite")

        if cross.refinement_improved_structure:
            parts.append("refine: improved structure")

        return "; ".join(parts) if parts else "no cross-stage signals"

    @staticmethod
    def _find_stage(stage_roles: dict[str, str], role: str) -> str | None:
        """Find first stage ID matching a role."""
        for sid, r in stage_roles.items():
            if r == role:
                return sid
        return None

    @staticmethod
    def _find_stages(stage_roles: dict[str, str], roles: tuple[str, ...]) -> list[str]:
        """Find all stage IDs matching any of the given roles."""
        return [sid for sid, r in stage_roles.items() if r in roles]


# Cross-stage singleton
cross_stage_analyzer = CrossStageAnalyzer()
