"""
Persistent DOM storage — SQLite-backed storage for:
- DOM baselines
- Drift events
- Selector maintenance suggestions
- Selector profile overrides

Uses graceful corruption handling — if DB is corrupt, creates a new one.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "data",
    "dom_baseline.db",
)


def _db_path() -> str:
    """Resolve DB path from settings or default."""
    if hasattr(settings, "data_dir") and settings.data_dir:
        return os.path.join(str(settings.data_dir), "dom_baseline.db")
    return _DEFAULT_DB_PATH


_SCHEMA = """
CREATE TABLE IF NOT EXISTS baselines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL,
    role TEXT NOT NULL,
    selector TEXT NOT NULL,
    tag_name TEXT DEFAULT '',
    aria_role TEXT DEFAULT '',
    placeholder TEXT DEFAULT '',
    aria_label TEXT DEFAULT '',
    text_summary TEXT DEFAULT '',
    parent_tag TEXT DEFAULT '',
    sibling_count INTEGER DEFAULT 0,
    is_visible INTEGER DEFAULT 1,
    is_editable INTEGER DEFAULT 0,
    is_clickable INTEGER DEFAULT 0,
    capture_reason TEXT DEFAULT '',
    confidence REAL DEFAULT 0.0,
    version INTEGER DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(provider_id, role)
);

CREATE TABLE IF NOT EXISTS drift_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL,
    role TEXT NOT NULL,
    trigger TEXT DEFAULT '',
    drift_severity TEXT DEFAULT 'none',
    drift_score REAL DEFAULT 0.0,
    human_summary TEXT DEFAULT '',
    diff_json TEXT DEFAULT '{}',
    timestamp REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drift_provider ON drift_events(provider_id);
CREATE INDEX IF NOT EXISTS idx_drift_timestamp ON drift_events(timestamp);

CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL,
    role TEXT NOT NULL,
    current_selector TEXT DEFAULT '',
    suggested_selector TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_json TEXT DEFAULT '[]',
    confidence REAL DEFAULT 0.0,
    times_observed INTEGER DEFAULT 1,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    status TEXT DEFAULT 'pending',  -- pending, approved, rejected, dismissed, superseded
    override_selector TEXT DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_suggestions_status ON suggestions(status);
CREATE INDEX IF NOT EXISTS idx_suggestions_provider ON suggestions(provider_id);

CREATE TABLE IF NOT EXISTS selector_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL,
    role TEXT NOT NULL,
    selector TEXT NOT NULL,
    source TEXT DEFAULT 'approved',  -- approved, promoted, manual
    suggestion_id INTEGER,
    created_at REAL NOT NULL,
    created_by TEXT DEFAULT 'system',
    UNIQUE(provider_id, role)
);
"""


class PersistentDOMStore:
    """SQLite-backed persistent storage for DOM baselines, drift, suggestions, overrides."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or _db_path()
        self._initialized = False
        self._ensure_db()

    def _ensure_db(self) -> None:
        """Initialize DB, handling corruption gracefully."""
        try:
            db_dir = os.path.dirname(self._db_path)
            if db_dir:
                Path(db_dir).mkdir(parents=True, exist_ok=True)

            with self._conn() as conn:
                conn.executescript(_SCHEMA)
            self._initialized = True
            logger.info("Persistent DOM store initialized", path=self._db_path)
        except Exception as exc:
            logger.warning(
                "DOM store initialization failed, recreating",
                path=self._db_path,
                error=str(exc),
            )
            # Try to recreate
            try:
                if os.path.exists(self._db_path):
                    os.remove(self._db_path)
                db_dir = os.path.dirname(self._db_path)
                if db_dir:
                    Path(db_dir).mkdir(parents=True, exist_ok=True)
                with self._conn() as conn:
                    conn.executescript(_SCHEMA)
                self._initialized = True
                logger.info("Persistent DOM store recreated after corruption")
            except Exception as exc2:
                logger.error(
                    "Failed to recreate DOM store",
                    path=self._db_path,
                    error=str(exc2),
                )
                self._initialized = False

    @contextmanager
    def _conn(self):
        """Get a database connection with row factory."""
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── Baselines ──

    def save_baseline(self, data: dict[str, Any]) -> bool:
        """Save or update a baseline."""
        try:
            with self._conn() as conn:
                now = time.time()
                conn.execute(
                    """
                    INSERT INTO baselines
                    (provider_id, role, selector, tag_name, aria_role,
                     placeholder, aria_label, text_summary, parent_tag,
                     sibling_count, is_visible, is_editable, is_clickable,
                     capture_reason, confidence, version, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(provider_id, role) DO UPDATE SET
                        selector=excluded.selector,
                        tag_name=excluded.tag_name,
                        aria_role=excluded.aria_role,
                        placeholder=excluded.placeholder,
                        aria_label=excluded.aria_label,
                        text_summary=excluded.text_summary,
                        parent_tag=excluded.parent_tag,
                        sibling_count=excluded.sibling_count,
                        is_visible=excluded.is_visible,
                        is_editable=excluded.is_editable,
                        is_clickable=excluded.is_clickable,
                        capture_reason=excluded.capture_reason,
                        confidence=excluded.confidence,
                        version=excluded.version,
                        updated_at=excluded.updated_at
                    """,
                    (
                        data["provider_id"],
                        data["role"],
                        data["selector"],
                        data.get("tag_name", ""),
                        data.get("aria_role", ""),
                        data.get("placeholder", ""),
                        data.get("aria_label", ""),
                        data.get("text_summary", ""),
                        data.get("parent_tag", ""),
                        data.get("sibling_count", 0),
                        1 if data.get("is_visible", True) else 0,
                        1 if data.get("is_editable", False) else 0,
                        1 if data.get("is_clickable", False) else 0,
                        data.get("capture_reason", ""),
                        data.get("confidence", 0.0),
                        data.get("version", 1),
                        now,
                        now,
                    ),
                )
            return True
        except Exception as exc:
            logger.error("Failed to save baseline", error=str(exc))
            return False

    def get_baseline(self, provider_id: str, role: str) -> dict[str, Any] | None:
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT * FROM baselines WHERE provider_id=? AND role=?",
                    (provider_id, role),
                ).fetchone()
                if row:
                    return dict(row)
            return None
        except Exception:
            return None

    def get_baselines(self, provider_id: str | None = None) -> list[dict[str, Any]]:
        try:
            with self._conn() as conn:
                if provider_id:
                    rows = conn.execute(
                        "SELECT * FROM baselines WHERE provider_id=?", (provider_id,)
                    ).fetchall()
                else:
                    rows = conn.execute("SELECT * FROM baselines").fetchall()
                return [dict(r) for r in rows]
        except Exception:
            return []

    def clear_baseline(self, provider_id: str, role: str | None = None) -> int:
        try:
            with self._conn() as conn:
                if role:
                    cur = conn.execute(
                        "DELETE FROM baselines WHERE provider_id=? AND role=?",
                        (provider_id, role),
                    )
                else:
                    cur = conn.execute(
                        "DELETE FROM baselines WHERE provider_id=?", (provider_id,)
                    )
                return cur.rowcount
        except Exception:
            return 0

    def clear_all_baselines(self) -> int:
        try:
            with self._conn() as conn:
                cur = conn.execute("DELETE FROM baselines")
                return cur.rowcount
        except Exception:
            return 0

    # ── Drift Events ──

    def save_drift_event(self, data: dict[str, Any]) -> bool:
        try:
            with self._conn() as conn:
                conn.execute(
                    """
                    INSERT INTO drift_events
                    (provider_id, role, trigger, drift_severity, drift_score,
                     human_summary, diff_json, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        data["provider_id"],
                        data["role"],
                        data.get("trigger", ""),
                        data.get("drift_severity", "none"),
                        data.get("drift_score", 0.0),
                        data.get("human_summary", ""),
                        json.dumps(data.get("diff_json", {})),
                        time.time(),
                    ),
                )
            return True
        except Exception as exc:
            logger.error("Failed to save drift event", error=str(exc))
            return False

    def get_drift_events(
        self,
        provider_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        try:
            with self._conn() as conn:
                if provider_id:
                    rows = conn.execute(
                        "SELECT * FROM drift_events WHERE provider_id=? ORDER BY timestamp DESC LIMIT ?",
                        (provider_id, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM drift_events ORDER BY timestamp DESC LIMIT ?", (limit,)
                    ).fetchall()
                results = []
                for r in rows:
                    d = dict(r)
                    try:
                        d["diff_json"] = json.loads(d["diff_json"])
                    except Exception:
                        d["diff_json"] = {}
                    results.append(d)
                return results
        except Exception:
            return []

    def prune_drift_events(self, max_age_seconds: float = 86400 * 30) -> int:
        """Remove drift events older than max_age_seconds."""
        try:
            cutoff = time.time() - max_age_seconds
            with self._conn() as conn:
                cur = conn.execute(
                    "DELETE FROM drift_events WHERE timestamp < ?", (cutoff,)
                )
                return cur.rowcount
        except Exception:
            return 0

    def clear_all_drift_events(self) -> int:
        try:
            with self._conn() as conn:
                cur = conn.execute("DELETE FROM drift_events")
                return cur.rowcount
        except Exception:
            return 0

    # ── Suggestions ──

    def save_suggestion(self, data: dict[str, Any]) -> int | None:
        """Save a suggestion. Returns id."""
        try:
            with self._conn() as conn:
                now = time.time()
                cur = conn.execute(
                    """
                    INSERT INTO suggestions
                    (provider_id, role, current_selector, suggested_selector,
                     reason, evidence_json, confidence, times_observed,
                     first_seen, last_seen, status, override_selector,
                     created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        data["provider_id"],
                        data["role"],
                        data.get("current_selector", ""),
                        data["suggested_selector"],
                        data["reason"],
                        json.dumps(data.get("evidence", [])),
                        data.get("confidence", 0.0),
                        data.get("times_observed", 1),
                        now,
                        now,
                        data.get("status", "pending"),
                        data.get("override_selector", ""),
                        now,
                        now,
                    ),
                )
                return cur.lastrowid
        except Exception as exc:
            logger.error("Failed to save suggestion", error=str(exc))
            return None

    def update_suggestion(self, suggestion_id: int, updates: dict[str, Any]) -> bool:
        try:
            set_clauses = []
            params: list = []
            for key, value in updates.items():
                if key in ("status", "override_selector"):
                    set_clauses.append(f"{key}=?")
                    params.append(value)
                elif key == "evidence":
                    set_clauses.append("evidence_json=?")
                    params.append(json.dumps(value))
                elif key in ("times_observed",):
                    set_clauses.append(f"{key}=?")
                    params.append(value)

            if not set_clauses:
                return False

            set_clauses.append("updated_at=?")
            params.append(time.time())
            params.append(suggestion_id)

            with self._conn() as conn:
                conn.execute(
                    f"UPDATE suggestions SET {', '.join(set_clauses)} WHERE id=?",
                    params,
                )
            return True
        except Exception as exc:
            logger.error("Failed to update suggestion", error=str(exc))
            return False

    def get_suggestion(self, suggestion_id: int) -> dict[str, Any] | None:
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT * FROM suggestions WHERE id=?", (suggestion_id,)
                ).fetchone()
                if row:
                    d = dict(row)
                    try:
                        d["evidence"] = json.loads(d["evidence_json"])
                    except Exception:
                        d["evidence"] = []
                    return d
            return None
        except Exception:
            return None

    def get_suggestions(
        self,
        status: str | None = None,
        provider_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        try:
            conditions = []
            params: list = []
            if status:
                conditions.append("status=?")
                params.append(status)
            if provider_id:
                conditions.append("provider_id=?")
                params.append(provider_id)

            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            with self._conn() as conn:
                rows = conn.execute(
                    f"SELECT * FROM suggestions {where} ORDER BY last_seen DESC LIMIT ?",
                    params + [limit],
                ).fetchall()
                results = []
                for r in rows:
                    d = dict(r)
                    try:
                        d["evidence"] = json.loads(d["evidence_json"])
                    except Exception:
                        d["evidence"] = []
                    results.append(d)
                return results
        except Exception:
            return []

    # ── Overrides ──

    def save_override(self, data: dict[str, Any]) -> bool:
        """Save or update a selector override."""
        try:
            with self._conn() as conn:
                now = time.time()
                conn.execute(
                    """
                    INSERT INTO selector_overrides
                    (provider_id, role, selector, source, suggestion_id, created_at, created_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(provider_id, role) DO UPDATE SET
                        selector=excluded.selector,
                        source=excluded.source,
                        suggestion_id=excluded.suggestion_id,
                        created_at=excluded.created_at
                    """,
                    (
                        data["provider_id"],
                        data["role"],
                        data["selector"],
                        data.get("source", "approved"),
                        data.get("suggestion_id"),
                        now,
                        data.get("created_by", "system"),
                    ),
                )
            return True
        except Exception as exc:
            logger.error("Failed to save override", error=str(exc))
            return False

    def get_override(self, provider_id: str, role: str) -> dict[str, Any] | None:
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT * FROM selector_overrides WHERE provider_id=? AND role=?",
                    (provider_id, role),
                ).fetchone()
                if row:
                    return dict(row)
            return None
        except Exception:
            return None

    def get_overrides(
        self, provider_id: str | None = None
    ) -> list[dict[str, Any]]:
        try:
            with self._conn() as conn:
                if provider_id:
                    rows = conn.execute(
                        "SELECT * FROM selector_overrides WHERE provider_id=?",
                        (provider_id,),
                    ).fetchall()
                else:
                    rows = conn.execute("SELECT * FROM selector_overrides").fetchall()
                return [dict(r) for r in rows]
        except Exception:
            return []

    def clear_override(self, provider_id: str, role: str | None = None) -> int:
        try:
            with self._conn() as conn:
                if role:
                    cur = conn.execute(
                        "DELETE FROM selector_overrides WHERE provider_id=? AND role=?",
                        (provider_id, role),
                    )
                else:
                    cur = conn.execute(
                        "DELETE FROM selector_overrides WHERE provider_id=?",
                        (provider_id,),
                    )
                return cur.rowcount
        except Exception:
            return 0

    def clear_all_overrides(self) -> int:
        try:
            with self._conn() as conn:
                cur = conn.execute("DELETE FROM selector_overrides")
                return cur.rowcount
        except Exception:
            return 0

    # ── Summary ──

    def summary(self) -> dict[str, Any]:
        try:
            with self._conn() as conn:
                baseline_count = conn.execute(
                    "SELECT COUNT(*) as c FROM baselines"
                ).fetchone()["c"]
                provider_count = conn.execute(
                    "SELECT COUNT(DISTINCT provider_id) as c FROM baselines"
                ).fetchone()["c"]
                drift_count = conn.execute(
                    "SELECT COUNT(*) as c FROM drift_events"
                ).fetchone()["c"]
                pending_count = conn.execute(
                    "SELECT COUNT(*) as c FROM suggestions WHERE status='pending'"
                ).fetchone()["c"]
                approved_count = conn.execute(
                    "SELECT COUNT(*) as c FROM suggestions WHERE status='approved'"
                ).fetchone()["c"]
                rejected_count = conn.execute(
                    "SELECT COUNT(*) as c FROM suggestions WHERE status='rejected'"
                ).fetchone()["c"]
                override_count = conn.execute(
                    "SELECT COUNT(*) as c FROM selector_overrides"
                ).fetchone()["c"]

                return {
                    "total_baselines": baseline_count,
                    "providers_with_baselines": provider_count,
                    "total_drift_events": drift_count,
                    "pending_suggestions": pending_count,
                    "approved_suggestions": approved_count,
                    "rejected_suggestions": rejected_count,
                    "total_overrides": override_count,
                }
        except Exception as exc:
            return {"error": str(exc)}


persistent_dom_store = PersistentDOMStore()
