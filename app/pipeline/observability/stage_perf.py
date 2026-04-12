"""
Stage-specific performance tracking.

Tracks rolling performance metrics per model per stage role.
Used to improve stage suitability scoring with real data.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

_MAX_AGE_DAYS = 30
_MAX_ENTRIES = 5000


@dataclass(slots=True)
class RolePerformanceEntry:
    """A single performance entry for a model in a stage role."""

    model_id: str
    provider_id: str
    stage_role: str
    success: bool
    duration_ms: float
    had_fallback: bool
    had_retry: bool
    output_quality_hint: float = 0.5  # 0.0-1.0, proxy for quality
    timestamp: float = field(default_factory=time.time)


class StagePerformanceTracker:
    """Tracks rolling performance metrics per model per stage role.

    Stores entries in SQLite for persistence, with bounded size.
    Provides aggregation methods for suitability scoring.
    """

    _CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS stage_performance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        model_id TEXT NOT NULL,
        provider_id TEXT,
        stage_role TEXT NOT NULL,
        success INTEGER NOT NULL,
        duration_ms REAL,
        had_fallback INTEGER NOT NULL DEFAULT 0,
        had_retry INTEGER NOT NULL DEFAULT 0,
        output_quality_hint REAL DEFAULT 0.5,
        timestamp REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_sp_model_role ON stage_performance(model_id, stage_role);
    CREATE INDEX IF NOT EXISTS idx_sp_timestamp ON stage_performance(timestamp);
    """

    def __init__(
        self,
        db_path: str = "data/stage_performance.db",
        max_entries: int = _MAX_ENTRIES,
        max_age_days: int = _MAX_AGE_DAYS,
    ) -> None:
        self._db_path = db_path
        self._max_entries = max_entries
        self._max_age_days = max_age_days
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database."""
        path = Path(self._db_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(self._CREATE_TABLE_SQL)
            conn.commit()
        except sqlite3.Error as exc:
            logger.error("stage_perf_db_init_failed", error=str(exc))
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

    def record(self, entry: RolePerformanceEntry) -> None:
        """Record a stage performance entry."""
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO stage_performance (
                    model_id, provider_id, stage_role, success,
                    duration_ms, had_fallback, had_retry,
                    output_quality_hint, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.model_id,
                    entry.provider_id,
                    entry.stage_role,
                    1 if entry.success else 0,
                    entry.duration_ms,
                    1 if entry.had_fallback else 0,
                    1 if entry.had_retry else 0,
                    entry.output_quality_hint,
                    entry.timestamp,
                ),
            )
            conn.commit()
            self._enforce_retention(conn)
        except sqlite3.Error as exc:
            logger.debug("stage_perf_record_failed", error=str(exc))
        finally:
            conn.close()

    def get_success_rate(
        self,
        model_id: str,
        stage_role: str,
        window: int = 100,
    ) -> float:
        """Get rolling success rate for a model in a stage role."""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successes
                FROM (
                    SELECT success FROM stage_performance
                    WHERE model_id = ? AND stage_role = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                )
                """,
                (model_id, stage_role, window),
            ).fetchone()

            if row and row["total"] > 0:
                return row["successes"] / row["total"]
            return 0.5  # Default when no data
        except sqlite3.Error:
            return 0.5
        finally:
            conn.close()

    def get_avg_duration(
        self,
        model_id: str,
        stage_role: str,
        window: int = 100,
    ) -> float:
        """Get average duration for a model in a stage role."""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT AVG(duration_ms) as avg_ms
                FROM (
                    SELECT duration_ms FROM stage_performance
                    WHERE model_id = ? AND stage_role = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                )
                """,
                (model_id, stage_role, window),
            ).fetchone()

            if row and row["avg_ms"]:
                return row["avg_ms"]
            return 0.0
        except sqlite3.Error:
            return 0.0
        finally:
            conn.close()

    def get_fallback_rate(
        self,
        model_id: str,
        stage_role: str,
        window: int = 100,
    ) -> float:
        """Get fallback rate for a model in a stage role."""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN had_fallback = 1 THEN 1 ELSE 0 END) as fallbacks
                FROM (
                    SELECT had_fallback FROM stage_performance
                    WHERE model_id = ? AND stage_role = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                )
                """,
                (model_id, stage_role, window),
            ).fetchone()

            if row and row["total"] > 0:
                return row["fallbacks"] / row["total"]
            return 0.0
        except sqlite3.Error:
            return 0.0
        finally:
            conn.close()

    def get_all_model_roles(self) -> list[dict[str, Any]]:
        """Get all tracked model+role combinations with stats."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT
                    model_id,
                    provider_id,
                    stage_role,
                    COUNT(*) as count,
                    ROUND(100.0 * SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) / COUNT(*), 1) as success_rate,
                    ROUND(AVG(duration_ms), 0) as avg_duration_ms,
                    ROUND(100.0 * SUM(CASE WHEN had_fallback = 1 THEN 1 ELSE 0 END) / COUNT(*), 1) as fallback_rate
                FROM stage_performance
                WHERE timestamp > ?
                GROUP BY model_id, stage_role
                HAVING count >= 3
                ORDER BY model_id, stage_role
                """,
                (time.time() - self._max_age_days * 86400,),
            ).fetchall()

            return [dict(r) for r in rows]
        except sqlite3.Error as exc:
            logger.debug("stage_perf_get_all_failed", error=str(exc))
            return []
        finally:
            conn.close()

    def get_sample_count(
        self,
        model_id: str,
        stage_role: str,
        window: int = 100,
    ) -> int:
        """Get exact sample count for a model+role in the rolling window."""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) as cnt
                FROM (
                    SELECT id FROM stage_performance
                    WHERE model_id = ? AND stage_role = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                )
                """,
                (model_id, stage_role, window),
            ).fetchone()
            return row["cnt"] if row else 0
        except sqlite3.Error:
            return 0
        finally:
            conn.close()

    def get_model_role_stats(
        self,
        model_id: str,
        stage_role: str,
        window: int = 100,
    ) -> dict[str, Any]:
        """Get full stats for a model+role combination with exact sample count."""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) as sample_count,
                    ROUND(1.0 * SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 3) as success_rate,
                    ROUND(AVG(duration_ms), 1) as avg_duration_ms,
                    ROUND(1.0 * SUM(CASE WHEN had_fallback = 1 THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 3) as fallback_rate,
                    ROUND(AVG(output_quality_hint), 3) as avg_quality_hint
                FROM (
                    SELECT success, duration_ms, had_fallback, had_retry, output_quality_hint
                    FROM stage_performance
                    WHERE model_id = ? AND stage_role = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                )
                """,
                (model_id, stage_role, window),
            ).fetchone()

            if row and row["sample_count"] > 0:
                return {
                    "model_id": model_id,
                    "stage_role": stage_role,
                    "success_rate": row["success_rate"] if row["success_rate"] else 0.5,
                    "avg_duration_ms": row["avg_duration_ms"] if row["avg_duration_ms"] else 0.0,
                    "fallback_rate": row["fallback_rate"] if row["fallback_rate"] else 0.0,
                    "sample_count": row["sample_count"],
                    "avg_quality_hint": row["avg_quality_hint"] if row["avg_quality_hint"] else 0.5,
                }
            return {
                "model_id": model_id,
                "stage_role": stage_role,
                "success_rate": 0.5,
                "avg_duration_ms": 0.0,
                "fallback_rate": 0.0,
                "sample_count": 0,
                "avg_quality_hint": 0.5,
            }
        except sqlite3.Error:
            return {
                "model_id": model_id,
                "stage_role": stage_role,
                "success_rate": 0.5,
                "avg_duration_ms": 0.0,
                "fallback_rate": 0.0,
                "sample_count": 0,
                "avg_quality_hint": 0.5,
            }
        finally:
            conn.close()

    def _enforce_retention(self, conn: sqlite3.Connection) -> None:
        """Enforce max entries and max age."""
        # Age-based
        if self._max_age_days > 0:
            cutoff = time.time() - (self._max_age_days * 86400)
            conn.execute("DELETE FROM stage_performance WHERE timestamp < ?", (cutoff,))

        # Count-based
        conn.execute(
            """
            DELETE FROM stage_performance
            WHERE id IN (
                SELECT id FROM stage_performance
                ORDER BY timestamp DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (self._max_entries,),
        )

    def get_latest_timestamps_by_role(
        self,
        model_role_pairs: list[tuple[str, str]],
    ) -> dict[tuple[str, str], float]:
        """Get the most recent activity timestamps for multiple model+role pairs in one query.

        Args:
            model_role_pairs: List of (model_id, stage_role) tuples to look up.

        Returns:
            Dict mapping (model_id, stage_role) to latest timestamp.
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
                SELECT model_id, stage_role, MAX(timestamp) as latest
                FROM stage_performance
                WHERE model_id IN ({model_placeholders})
                  AND stage_role IN ({role_placeholders})
                GROUP BY model_id, stage_role
            """
            rows = conn.execute(query, model_ids + roles).fetchall()
            return {(row["model_id"], row["stage_role"]): row["latest"] for row in rows}
        except sqlite3.Error:
            return {}
        finally:
            conn.close()

    def get_latest_timestamps(self, model_ids: list[str]) -> dict[str, float]:
        """Get the most recent activity timestamps for multiple models in one query.

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
                FROM stage_performance
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
        """Get the most recent activity timestamp for a model across all roles.

        Returns:
            Unix timestamp of the most recent stage_performance entry,
            or 0.0 if no entries exist for this model.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT MAX(timestamp) as latest FROM stage_performance WHERE model_id = ?",
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
            count_before = conn.execute("SELECT COUNT(*) FROM stage_performance").fetchone()[0]
            self._enforce_retention(conn)
            conn.commit()
            count_after = conn.execute("SELECT COUNT(*) FROM stage_performance").fetchone()[0]
            return count_before - count_after
        except sqlite3.Error:
            return 0
        finally:
            conn.close()


# Global singleton
stage_performance = StagePerformanceTracker()
