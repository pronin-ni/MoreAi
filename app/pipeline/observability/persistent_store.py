"""
Persistent pipeline execution store.

Uses SQLite with a single table to store pipeline execution summaries
and stage traces. Supports retention policy and bounded size.

Bootstrap guarantees:
- Schema is created on first access and at startup
- Thread-safe lazy initialization
- Idempotent CREATE TABLE IF NOT EXISTS
- Schema versioning for future migrations
- Clear logging at each stage
- Graceful in-memory fallback only AFTER bootstrap attempt
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

# Schema version for future migrations
_SCHEMA_VERSION = 1

# Default limits
_MAX_EXECUTIONS = 500
_MAX_AGE_DAYS = 30

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pipeline_executions (
    execution_id TEXT PRIMARY KEY,
    pipeline_id TEXT NOT NULL,
    pipeline_display_name TEXT,
    status TEXT NOT NULL,
    started_at REAL,
    finished_at REAL,
    duration_ms REAL,
    total_budget_ms INTEGER,
    budget_consumed_pct REAL,
    stage_count INTEGER,
    stages_completed INTEGER,
    total_retries INTEGER,
    total_fallbacks INTEGER,
    final_output_summary TEXT,
    failure_reason TEXT,
    failed_stage TEXT,
    request_id TEXT,
    original_model TEXT,
    stage_summaries_json TEXT,
    created_at REAL DEFAULT (strftime('%s', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_exec_pipeline ON pipeline_executions(pipeline_id);
CREATE INDEX IF NOT EXISTS idx_exec_status ON pipeline_executions(status);
CREATE INDEX IF NOT EXISTS idx_exec_created ON pipeline_executions(created_at);
"""


def _ensure_schema(conn: sqlite3.Connection, db_path: str) -> bool:
    """Run idempotent schema creation. Returns True on success."""
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_CREATE_TABLE_SQL)
        # executescript auto-commits, but we need a separate execute for PRAGMA with value
        conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        conn.commit()
        return True
    except sqlite3.Error as exc:
        logger.error("schema_bootstrap_failed", path=db_path, error=str(exc))
        return False


class PersistentExecutionStore:
    """SQLite-backed execution store with retention policy.

    Stores pipeline execution summaries and stage traces.
    Automatically enforces max count and max age retention.

    Bootstrap:
    - Schema is created in __init__ with idempotent CREATE TABLE IF NOT EXISTS
    - Thread-safe lazy initialization via get_persistent_store()
    - If disk DB fails, falls back to in-memory with clear logging
    - Schema version recorded for future migration support
    """

    def __init__(
        self,
        db_path: str = "data/pipeline_executions.db",
        max_executions: int = _MAX_EXECUTIONS,
        max_age_days: int = _MAX_AGE_DAYS,
    ) -> None:
        self._db_path = db_path
        self._max_executions = max_executions
        self._max_age_days = max_age_days
        self._initialized = False
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database, bootstrap schema, handle failures gracefully."""
        path = Path(self._db_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        conn = self._connect()
        try:
            success = _ensure_schema(conn, self._db_path)
            if success:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                logger.info(
                    "persistent_execution_store_initialized",
                    path=self._db_path,
                    schema_version=str(version),
                    mode="disk",
                )
                self._initialized = True
            else:
                self._fallback_to_memory()
        except sqlite3.Error as exc:
            logger.error("database_init_failed", path=self._db_path, error=str(exc))
            self._fallback_to_memory()
        finally:
            conn.close()

    def _fallback_to_memory(self) -> None:
        """Switch to in-memory database as last resort."""
        self._db_path = ":memory:"
        conn = self._connect()
        try:
            success = _ensure_schema(conn, self._db_path)
            if success:
                logger.warning(
                    "using_in_memory_store",
                    reason="disk_bootstrap_failed",
                    note="executions_will_not_persist_across_restarts",
                )
                self._initialized = True
            else:
                logger.error("in_memory_store_also_failed", reason="critical")
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        """Create a database connection."""
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def store(self, summary: Any) -> None:
        """Store an execution summary.

        Args:
            summary: A PipelineExecutionSummary object (duck-typed).
        """
        stage_summaries = []
        for s in summary.stage_summaries:
            if isinstance(s, dict):
                stage_summaries.append(s)
            elif hasattr(s, "to_dict"):
                stage_summaries.append(s.to_dict())
            else:
                stage_summaries.append(dict(s.__dict__))

        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO pipeline_executions (
                    execution_id, pipeline_id, pipeline_display_name, status,
                    started_at, finished_at, duration_ms, total_budget_ms,
                    budget_consumed_pct, stage_count, stages_completed,
                    total_retries, total_fallbacks, final_output_summary,
                    failure_reason, failed_stage, request_id, original_model,
                    stage_summaries_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    summary.execution_id,
                    summary.pipeline_id,
                    getattr(summary, "pipeline_display_name", ""),
                    summary.status,
                    summary.started_at,
                    summary.finished_at,
                    summary.duration_ms,
                    summary.total_budget_ms,
                    summary.budget_consumed_pct,
                    summary.stage_count,
                    summary.stages_completed,
                    summary.total_retries,
                    summary.total_fallbacks,
                    getattr(summary, "final_output_summary", ""),
                    getattr(summary, "failure_reason", ""),
                    getattr(summary, "failed_stage", ""),
                    summary.request_id,
                    summary.original_model,
                    json.dumps(stage_summaries),
                ),
            )
            conn.commit()

            # Enforce retention
            self._enforce_retention(conn)
        except sqlite3.Error as exc:
            logger.error("execution_store_write_failed", error=str(exc))
        finally:
            conn.close()

    def get_recent(
        self,
        limit: int = 20,
        pipeline_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get recent execution summaries.

        Returns raw dicts (not Summary objects) to decouple from the
        in-memory store's object model.
        """
        conn = self._connect()
        try:
            query = "SELECT * FROM pipeline_executions WHERE 1=1"
            params: list = []

            if pipeline_id:
                query += " AND pipeline_id = ?"
                params.append(pipeline_id)
            if status:
                query += " AND status = ?"
                params.append(status)

            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            return [self._row_to_dict(r) for r in rows]
        except sqlite3.Error as exc:
            logger.error("execution_store_read_failed", error=str(exc))
            return []
        finally:
            conn.close()

    def get(self, execution_id: str) -> dict[str, Any] | None:
        """Get a single execution by ID."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM pipeline_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if row:
                return self._row_to_dict(row)
            return None
        except sqlite3.Error as exc:
            logger.error("execution_store_read_failed", error=str(exc))
            return None
        finally:
            conn.close()

    def get_stats(self) -> dict[str, Any]:
        """Get aggregate statistics."""
        conn = self._connect()
        try:
            total = conn.execute("SELECT COUNT(*) FROM pipeline_executions").fetchone()[0]

            by_status: dict[str, int] = {}
            for row in conn.execute(
                "SELECT status, COUNT(*) as cnt FROM pipeline_executions GROUP BY status"
            ):
                by_status[row["status"]] = row["cnt"]

            by_pipeline: dict[str, int] = {}
            for row in conn.execute(
                "SELECT pipeline_id, COUNT(*) as cnt FROM pipeline_executions GROUP BY pipeline_id"
            ):
                by_pipeline[row["pipeline_id"]] = row["cnt"]

            return {
                "total_stored": total,
                "max_capacity": self._max_executions,
                "by_status": by_status,
                "by_pipeline": by_pipeline,
                "pipeline_count": len(by_pipeline),
                "persistent": self._db_path != ":memory:",
            }
        except sqlite3.Error as exc:
            logger.error("execution_store_stats_failed", error=str(exc))
            return {"total_stored": 0, "by_status": {}, "by_pipeline": {}, "pipeline_count": 0}
        finally:
            conn.close()

    def get_by_pipeline(
        self,
        pipeline_id: str,
        limit: int = 10,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get recent executions for a specific pipeline."""
        return self.get_recent(limit=limit, pipeline_id=pipeline_id, status=status)

    def cleanup(self) -> int:
        """Manually run retention cleanup. Returns rows deleted."""
        conn = self._connect()
        try:
            count_before = conn.execute("SELECT COUNT(*) FROM pipeline_executions").fetchone()[0]
            self._enforce_retention(conn)
            count_after = conn.execute("SELECT COUNT(*) FROM pipeline_executions").fetchone()[0]
            conn.commit()
            return count_before - count_after
        except sqlite3.Error as exc:
            logger.error("execution_store_cleanup_failed", error=str(exc))
            return 0
        finally:
            conn.close()

    def close(self) -> None:
        """Close the database connection (no-op for sqlite3, but good practice)."""
        pass

    def _enforce_retention(self, conn: sqlite3.Connection) -> None:
        """Enforce max count and max age retention."""
        # Age-based retention
        if self._max_age_days > 0:
            cutoff = time.time() - (self._max_age_days * 86400)
            conn.execute(
                "DELETE FROM pipeline_executions WHERE created_at < ?",
                (cutoff,),
            )

        # Count-based retention: delete oldest beyond limit
        conn.execute(
            """
            DELETE FROM pipeline_executions
            WHERE execution_id IN (
                SELECT execution_id FROM pipeline_executions
                ORDER BY created_at DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (self._max_executions,),
        )

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        """Convert a database row to a dict, parsing JSON fields."""
        d = dict(row)
        if d.get("stage_summaries_json"):
            try:
                d["stages"] = json.loads(d["stage_summaries_json"])
            except json.JSONDecodeError:
                d["stages"] = []
            del d["stage_summaries_json"]
        return d


# Global singleton — thread-safe lazy initialization
_persistent_store: PersistentExecutionStore | None = None
_store_lock = threading.Lock()


def get_persistent_store() -> PersistentExecutionStore:
    """Get the global persistent execution store, initializing if needed.

    Thread-safe: uses a lock to prevent double-initialization.
    """
    global _persistent_store
    if _persistent_store is None:
        with _store_lock:
            if _persistent_store is None:
                _persistent_store = PersistentExecutionStore()
    return _persistent_store


def initialize_persistent_store() -> PersistentExecutionStore:
    """Eagerly initialize the persistent store. Called at app startup.

    Returns the store instance. Safe to call multiple times (idempotent).
    """
    global _persistent_store
    if _persistent_store is not None:
        return _persistent_store
    with _store_lock:
        if _persistent_store is None:
            _persistent_store = PersistentExecutionStore()
        return _persistent_store
