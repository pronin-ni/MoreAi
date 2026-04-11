"""
Scoring history storage.

Stores periodic scoring snapshots for model/provider/transport/role combinations.
Provides trend analysis over configurable time windows.

SQLite-backed with bounded retention and graceful degradation.
"""

from __future__ import annotations

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
_MAX_ENTRIES = 50_000

# Snapshot interval default (5 minutes)
_DEFAULT_SNAPSHOT_INTERVAL_SECONDS = 300


@dataclass(slots=True)
class ScoringSnapshot:
    """A single scoring snapshot at a point in time."""

    timestamp: float
    model_id: str
    provider_id: str
    transport: str
    role: str
    final_score: float
    base_static_score: float
    dynamic_adjustment: float
    failure_penalty: float
    success_rate: float
    fallback_rate: float
    avg_duration_ms: float
    sample_count: int
    data_confidence: float


class ScoringHistoryStore:
    """SQLite-backed scoring history storage with bounded retention.

    Stores periodic snapshots of scoring breakdowns per model/provider/transport/role.
    Provides query methods for time-series history and trend analysis.
    """

    _CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS scoring_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        model_id TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        transport TEXT NOT NULL,
        role TEXT NOT NULL,
        final_score REAL NOT NULL,
        base_static_score REAL NOT NULL,
        dynamic_adjustment REAL NOT NULL,
        failure_penalty REAL NOT NULL,
        success_rate REAL NOT NULL,
        fallback_rate REAL NOT NULL,
        avg_duration_ms REAL NOT NULL,
        sample_count INTEGER NOT NULL,
        data_confidence REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_sh_model_role_ts ON scoring_history(model_id, role, timestamp);
    CREATE INDEX IF NOT EXISTS idx_sh_timestamp ON scoring_history(timestamp);
    CREATE INDEX IF NOT EXISTS idx_sh_model_role ON scoring_history(model_id, role);
    """

    def __init__(
        self,
        db_path: str = "data/scoring_history.db",
        max_entries: int = _MAX_ENTRIES,
        max_age_days: int = _MAX_AGE_DAYS,
    ) -> None:
        self._db_path = db_path
        self._max_entries = max_entries
        self._max_age_days = max_age_days
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database schema."""
        path = Path(self._db_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(self._CREATE_TABLE_SQL)
            conn.commit()
        except sqlite3.Error as exc:
            logger.error("scoring_history_db_init_failed", error=str(exc))
            self._db_path = ":memory:"
            conn = self._connect()
            conn.executescript(self._CREATE_TABLE_SQL)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(self._db_path, timeout=10)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error:
            raise

    def record_snapshot(self, snapshot: ScoringSnapshot) -> None:
        """Record a single scoring snapshot."""
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO scoring_history (
                    timestamp, model_id, provider_id, transport, role,
                    final_score, base_static_score, dynamic_adjustment, failure_penalty,
                    success_rate, fallback_rate, avg_duration_ms,
                    sample_count, data_confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.timestamp,
                    snapshot.model_id,
                    snapshot.provider_id,
                    snapshot.transport,
                    snapshot.role,
                    snapshot.final_score,
                    snapshot.base_static_score,
                    snapshot.dynamic_adjustment,
                    snapshot.failure_penalty,
                    snapshot.success_rate,
                    snapshot.fallback_rate,
                    snapshot.avg_duration_ms,
                    snapshot.sample_count,
                    snapshot.data_confidence,
                ),
            )
            conn.commit()
            self._enforce_retention(conn)
        except sqlite3.Error as exc:
            logger.debug("scoring_history_record_failed", error=str(exc))
        finally:
            conn.close()

    def get_history(
        self,
        model_id: str | None = None,
        role: str | None = None,
        window_seconds: float | None = None,
        limit: int = 1000,
    ) -> list[ScoringSnapshot]:
        """Query scoring history with optional filters.

        Args:
            model_id: Filter by model ID (None = all).
            role: Filter by stage role (None = all).
            window_seconds: Only return snapshots within the last N seconds (None = all).
            limit: Maximum number of snapshots to return.

        Returns:
            List of ScoringSnapshot, ordered by timestamp descending.
        """
        conn = self._connect()
        try:
            conditions: list[str] = []
            params: list[Any] = []

            if model_id:
                conditions.append("model_id = ?")
                params.append(model_id)
            if role:
                conditions.append("role = ?")
                params.append(role)
            if window_seconds is not None:
                conditions.append("timestamp >= ?")
                params.append(time.time() - window_seconds)

            where = ""
            if conditions:
                where = "WHERE " + " AND ".join(conditions)

            rows = conn.execute(
                f"""
                SELECT * FROM scoring_history
                {where}
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()

            return [self._row_to_snapshot(r) for r in rows]
        except sqlite3.Error as exc:
            logger.debug("scoring_history_query_failed", error=str(exc))
            return []
        finally:
            conn.close()

    def get_distinct_models(self, role: str | None = None) -> list[str]:
        """Get distinct model IDs that have history."""
        conn = self._connect()
        try:
            query = "SELECT DISTINCT model_id FROM scoring_history"
            params: list[Any] = []
            if role:
                query += " WHERE role = ?"
                params.append(role)
            query += " ORDER BY model_id"

            rows = conn.execute(query, params).fetchall()
            return [r["model_id"] for r in rows]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    def get_distinct_roles(self) -> list[str]:
        """Get distinct roles that have history."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT DISTINCT role FROM scoring_history ORDER BY role"
            ).fetchall()
            return [r["role"] for r in rows]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    def get_stats(self) -> dict[str, Any]:
        """Get store statistics."""
        conn = self._connect()
        try:
            total = conn.execute("SELECT COUNT(*) FROM scoring_history").fetchone()[0]
            oldest_row = conn.execute(
                "SELECT MIN(timestamp) as ts FROM scoring_history"
            ).fetchone()
            newest_row = conn.execute(
                "SELECT MAX(timestamp) as ts FROM scoring_history"
            ).fetchone()

            oldest = oldest_row["ts"] if oldest_row and oldest_row["ts"] else None
            newest = newest_row["ts"] if newest_row and newest_row["ts"] else None

            return {
                "total_snapshots": total,
                "max_entries": self._max_entries,
                "max_age_days": self._max_age_days,
                "oldest_snapshot_ts": oldest,
                "newest_snapshot_ts": newest,
                "storage_type": "sqlite",
                "db_path": self._db_path if self._db_path != ":memory:" else "memory",
            }
        except sqlite3.Error:
            return {
                "total_snapshots": 0,
                "max_entries": self._max_entries,
                "max_age_days": self._max_age_days,
                "storage_type": "sqlite",
                "db_path": self._db_path if self._db_path != ":memory:" else "memory",
            }
        finally:
            conn.close()

    def cleanup(self) -> int:
        """Run retention cleanup. Returns rows deleted."""
        conn = self._connect()
        try:
            count_before = conn.execute("SELECT COUNT(*) FROM scoring_history").fetchone()[0]
            self._enforce_retention(conn)
            conn.commit()
            count_after = conn.execute("SELECT COUNT(*) FROM scoring_history").fetchone()[0]
            deleted = count_before - count_after
            if deleted > 0:
                logger.info("scoring_history_cleanup", deleted=deleted)
            return deleted
        except sqlite3.Error:
            return 0
        finally:
            conn.close()

    def _enforce_retention(self, conn: sqlite3.Connection) -> None:
        """Enforce max entries and max age. Must be followed by commit."""
        # Age-based
        if self._max_age_days > 0:
            cutoff = time.time() - (self._max_age_days * 86400)
            conn.execute("DELETE FROM scoring_history WHERE timestamp < ?", (cutoff,))

        # Count-based: delete oldest entries beyond the limit
        conn.execute(
            """
            DELETE FROM scoring_history
            WHERE id NOT IN (
                SELECT id FROM scoring_history
                ORDER BY timestamp DESC
                LIMIT ?
            )
            """,
            (self._max_entries,),
        )
        conn.commit()

    @staticmethod
    def _row_to_snapshot(row: sqlite3.Row) -> ScoringSnapshot:
        return ScoringSnapshot(
            timestamp=row["timestamp"],
            model_id=row["model_id"],
            provider_id=row["provider_id"],
            transport=row["transport"],
            role=row["role"],
            final_score=row["final_score"],
            base_static_score=row["base_static_score"],
            dynamic_adjustment=row["dynamic_adjustment"],
            failure_penalty=row["failure_penalty"],
            success_rate=row["success_rate"],
            fallback_rate=row["fallback_rate"],
            avg_duration_ms=row["avg_duration_ms"],
            sample_count=row["sample_count"],
            data_confidence=row["data_confidence"],
        )


# Global singleton
scoring_history_store = ScoringHistoryStore()
