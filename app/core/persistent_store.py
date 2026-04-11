"""
Persistent storage layer — SQLite-backed.

Provides unified access to:
- Analytics events (requests, errors, fallbacks)
- API keys (secrets, metadata, quotas)
- Tenants (multi-tenant model visibility, budgets)
- Quota usage counters (sliding window)

All tables are created automatically on first use.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Default path for the SQLite database
DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "data",
    "moreai.db",
)


def _db_path() -> str:
    """Resolve the database path from settings or default."""
    if hasattr(settings, "data_dir") and settings.data_dir:
        return os.path.join(str(settings.data_dir), "moreai.db")
    return DEFAULT_DB_PATH


_SCHEMA = """
-- ── Analytics ──
CREATE TABLE IF NOT EXISTS analytics_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    model TEXT NOT NULL,
    provider TEXT NOT NULL,
    transport TEXT NOT NULL,
    status TEXT NOT NULL,
    latency_seconds REAL DEFAULT 0,
    error_type TEXT,
    is_fallback INTEGER DEFAULT 0,
    fallback_from TEXT,
    tenant_id TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_analytics_ts ON analytics_events(ts);
CREATE INDEX IF NOT EXISTS idx_analytics_model ON analytics_events(model);
CREATE INDEX IF NOT EXISTS idx_analytics_provider ON analytics_events(provider);
CREATE INDEX IF NOT EXISTS idx_analytics_tenant ON analytics_events(tenant_id);

-- ── API Keys ──
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    secret_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    quotas TEXT DEFAULT '{}',
    allowed_models TEXT DEFAULT '[]',
    tenant_id TEXT DEFAULT '',
    created_at REAL NOT NULL,
    expires_at REAL,
    is_active INTEGER DEFAULT 1,
    last_used_at REAL,
    metadata TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_api_keys_active ON api_keys(is_active);
CREATE INDEX IF NOT EXISTS idx_api_keys_tenant ON api_keys(tenant_id);

-- ── Quota Usage ──
CREATE TABLE IF NOT EXISTS quota_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id TEXT NOT NULL,
    ts REAL NOT NULL,
    model_id TEXT NOT NULL,
    tokens_used INTEGER DEFAULT 0,
    tenant_id TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_quota_usage_key ON quota_usage(key_id);
CREATE INDEX IF NOT EXISTS idx_quota_usage_ts ON quota_usage(ts);

-- ── Tenants ──
CREATE TABLE IF NOT EXISTS tenants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    allowed_models TEXT DEFAULT '[]',
    hidden_models TEXT DEFAULT '[]',
    budget_monthly_tokens INTEGER DEFAULT 0,
    budget_monthly_requests INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    metadata TEXT DEFAULT '{}'
);

-- ── Canary Events ──
CREATE TABLE IF NOT EXISTS canary_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    event TEXT NOT NULL,
    model_id TEXT NOT NULL,
    details TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_canary_ts ON canary_events(ts);
CREATE INDEX IF NOT EXISTS idx_canary_model ON canary_events(model_id);
"""


class PersistentStore:
    """SQLite-backed persistent storage for all operational data."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or _db_path()
        self._init_db()

    def _init_db(self) -> None:
        """Create the database and all tables if they don't exist."""
        db_dir = os.path.dirname(self._db_path)
        if db_dir:
            Path(db_dir).mkdir(parents=True, exist_ok=True)

        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            logger.info("Persistent store initialized", path=self._db_path)

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection]:
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

    # ── Analytics ──

    def record_analytics_event(self, event: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO analytics_events
                (ts, model, provider, transport, status, latency_seconds,
                 error_type, is_fallback, fallback_from, tenant_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.get("ts", time.time()),
                    event.get("model", ""),
                    event.get("provider", ""),
                    event.get("transport", ""),
                    event.get("status", ""),
                    event.get("latency_seconds", 0.0),
                    event.get("error_type"),
                    1 if event.get("is_fallback") else 0,
                    event.get("fallback_from"),
                    event.get("tenant_id", ""),
                ),
            )

    def query_analytics(
        self,
        *,
        since: float | None = None,
        until: float | None = None,
        tenant_id: str | None = None,
        limit: int = 10000,
    ) -> list[dict[str, Any]]:
        conditions = []
        params: list = []

        if since is not None:
            conditions.append("ts >= ?")
            params.append(since)
        if until is not None:
            conditions.append("ts <= ?")
            params.append(until)
        if tenant_id is not None:
            conditions.append("tenant_id = ?")
            params.append(tenant_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"SELECT * FROM analytics_events {where} ORDER BY ts DESC LIMIT ?"
        params.append(limit)

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def aggregate_top_models(
        self,
        *,
        since: float | None = None,
        tenant_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        conditions = []
        params: list = []

        if since is not None:
            conditions.append("ts >= ?")
            params.append(since)
        if tenant_id is not None:
            conditions.append("tenant_id = ?")
            params.append(tenant_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        query = f"""
            SELECT
                model as model_id,
                provider as provider_id,
                transport,
                COUNT(*) as request_count,
                SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success_count,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as error_count,
                SUM(CASE WHEN is_fallback = 1 THEN 1 ELSE 0 END) as fallback_count,
                AVG(CASE WHEN latency_seconds > 0 THEN latency_seconds END) as avg_latency,
                MAX(ts) as last_request_at
            FROM analytics_events
            {where}
            GROUP BY model
            ORDER BY request_count DESC
            LIMIT ?
        """
        params.append(limit)

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
            results = []
            for r in rows:
                d = dict(r)
                total = d["request_count"]
                errors = d["error_count"]
                d["error_rate"] = round(errors / total, 4) if total > 0 else 0.0
                results.append(d)
            return results

    def aggregate_top_providers(
        self,
        *,
        since: float | None = None,
        tenant_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        conditions = []
        params: list = []

        if since is not None:
            conditions.append("ts >= ?")
            params.append(since)
        if tenant_id is not None:
            conditions.append("tenant_id = ?")
            params.append(tenant_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        query = f"""
            SELECT
                provider as provider_id,
                transport,
                COUNT(*) as request_count,
                SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success_count,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as error_count,
                AVG(CASE WHEN latency_seconds > 0 THEN latency_seconds END) as avg_latency
            FROM analytics_events
            {where}
            GROUP BY provider
            ORDER BY request_count DESC
            LIMIT ?
        """
        params.append(limit)

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
            results = []
            for r in rows:
                d = dict(r)
                total = d["request_count"]
                errors = d["error_count"]
                d["error_rate"] = round(errors / total, 4) if total > 0 else 0.0
                results.append(d)
            return results

    def cleanup_old_analytics(self, max_age_seconds: float = 86400 * 30) -> int:
        """Delete analytics events older than max_age_seconds."""
        cutoff = time.time() - max_age_seconds
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM analytics_events WHERE ts < ?", (cutoff,))
            deleted = cur.rowcount
            if deleted > 0:
                conn.execute("VACUUM")
            return deleted

    # ── API Keys ──

    def create_api_key(self, key_data: dict[str, Any]) -> dict[str, Any]:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO api_keys
                (key_id, name, secret_hash, role, quotas, allowed_models,
                 tenant_id, created_at, expires_at, is_active, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key_data["key_id"],
                    key_data["name"],
                    key_data["secret_hash"],
                    key_data.get("role", "user"),
                    json.dumps(key_data.get("quotas", {})),
                    json.dumps(key_data.get("allowed_models", [])),
                    key_data.get("tenant_id", ""),
                    key_data.get("created_at", time.time()),
                    key_data.get("expires_at"),
                    1 if key_data.get("is_active", True) else 0,
                    json.dumps(key_data.get("metadata", {})),
                ),
            )
        return key_data

    def get_api_key(self, key_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM api_keys WHERE key_id = ?", (key_id,)
            ).fetchone()
            if row is None:
                return None
            d = dict(row)
            d["quotas"] = json.loads(d["quotas"])
            d["allowed_models"] = json.loads(d["allowed_models"])
            d["metadata"] = json.loads(d["metadata"])
            d["is_active"] = bool(d["is_active"])
            return d

    def list_api_keys(
        self, *, tenant_id: str | None = None
    ) -> list[dict[str, Any]]:
        conditions = []
        params: list = []
        if tenant_id is not None:
            conditions.append("tenant_id = ?")
            params.append(tenant_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM api_keys {where} ORDER BY created_at DESC", params
            ).fetchall()
            results = []
            for r in rows:
                d = dict(r)
                d["quotas"] = json.loads(d["quotas"])
                d["allowed_models"] = json.loads(d["allowed_models"])
                d["metadata"] = json.loads(d["metadata"])
                d["is_active"] = bool(d["is_active"])
                results.append(d)
            return results

    def update_api_key(self, key_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        set_clauses = []
        params: list = []

        if "name" in updates:
            set_clauses.append("name = ?")
            params.append(updates["name"])
        if "role" in updates:
            set_clauses.append("role = ?")
            params.append(updates["role"])
        if "quotas" in updates:
            set_clauses.append("quotas = ?")
            params.append(json.dumps(updates["quotas"]))
        if "allowed_models" in updates:
            set_clauses.append("allowed_models = ?")
            params.append(json.dumps(updates["allowed_models"]))
        if "expires_at" in updates:
            set_clauses.append("expires_at = ?")
            params.append(updates["expires_at"])
        if "is_active" in updates:
            set_clauses.append("is_active = ?")
            params.append(1 if updates["is_active"] else 0)
        if "metadata" in updates:
            set_clauses.append("metadata = ?")
            params.append(json.dumps(updates["metadata"]))
        if "last_used_at" in updates:
            set_clauses.append("last_used_at = ?")
            params.append(updates["last_used_at"])

        if not set_clauses:
            return self.get_api_key(key_id)

        set_clause = ", ".join(set_clauses)
        params.append(key_id)

        with self._conn() as conn:
            conn.execute(
                f"UPDATE api_keys SET {set_clause} WHERE key_id = ?", params
            )
        return self.get_api_key(key_id)

    def delete_api_key(self, key_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM api_keys WHERE key_id = ?", (key_id,))
            return cur.rowcount > 0

    def lookup_api_key_by_secret(self, secret_hash: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM api_keys WHERE secret_hash = ? AND is_active = 1",
                (secret_hash,),
            ).fetchone()
            if row is None:
                return None
            d = dict(row)
            d["quotas"] = json.loads(d["quotas"])
            d["allowed_models"] = json.loads(d["allowed_models"])
            d["metadata"] = json.loads(d["metadata"])
            d["is_active"] = bool(d["is_active"])
            return d

    # ── Quota Usage ──

    def record_quota_usage(
        self, *, key_id: str, model_id: str, tokens_used: int = 0, tenant_id: str = ""
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO quota_usage (key_id, ts, model_id, tokens_used, tenant_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (key_id, time.time(), model_id, tokens_used, tenant_id),
            )

    def get_quota_usage(
        self, *, key_id: str, since: float | None = None
    ) -> dict[str, Any]:
        conditions = ["key_id = ?"]
        params: list = [key_id]
        if since is not None:
            conditions.append("ts >= ?")
            params.append(since)

        where = " AND ".join(conditions)
        now = time.time()

        with self._conn() as conn:
            row = conn.execute(
                f"""
                SELECT
                    COUNT(*) as total_requests,
                    SUM(tokens_used) as total_tokens,
                    SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END) as last_60s,
                    SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END) as last_1h,
                    SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END) as last_24h
                FROM quota_usage
                WHERE {where}
                """,
                [now - 60, now - 3600, now - 86400] + params,
            ).fetchone()
            if row:
                return dict(row)
            return {"total_requests": 0, "total_tokens": 0, "last_60s": 0, "last_1h": 0, "last_24h": 0}

    def cleanup_old_quota_usage(self, max_age_seconds: float = 86400 * 7) -> int:
        cutoff = time.time() - max_age_seconds
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM quota_usage WHERE ts < ?", (cutoff,))
            return cur.rowcount

    # ── Tenants ──

    def create_tenant(self, tenant_data: dict[str, Any]) -> dict[str, Any]:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO tenants
                (tenant_id, name, allowed_models, hidden_models,
                 budget_monthly_tokens, budget_monthly_requests, created_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tenant_data["tenant_id"],
                    tenant_data["name"],
                    json.dumps(tenant_data.get("allowed_models", [])),
                    json.dumps(tenant_data.get("hidden_models", [])),
                    tenant_data.get("budget_monthly_tokens", 0),
                    tenant_data.get("budget_monthly_requests", 0),
                    time.time(),
                    json.dumps(tenant_data.get("metadata", {})),
                ),
            )
        return tenant_data

    def get_tenant(self, tenant_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM tenants WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()
            if row is None:
                return None
            d = dict(row)
            d["allowed_models"] = json.loads(d["allowed_models"])
            d["hidden_models"] = json.loads(d["hidden_models"])
            d["metadata"] = json.loads(d["metadata"])
            return d

    def list_tenants(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM tenants ORDER BY created_at DESC").fetchall()
            results = []
            for r in rows:
                d = dict(r)
                d["allowed_models"] = json.loads(d["allowed_models"])
                d["hidden_models"] = json.loads(d["hidden_models"])
                d["metadata"] = json.loads(d["metadata"])
                results.append(d)
            return results

    def update_tenant(self, tenant_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        set_clauses = []
        params: list = []

        if "name" in updates:
            set_clauses.append("name = ?")
            params.append(updates["name"])
        if "allowed_models" in updates:
            set_clauses.append("allowed_models = ?")
            params.append(json.dumps(updates["allowed_models"]))
        if "hidden_models" in updates:
            set_clauses.append("hidden_models = ?")
            params.append(json.dumps(updates["hidden_models"]))
        if "budget_monthly_tokens" in updates:
            set_clauses.append("budget_monthly_tokens = ?")
            params.append(updates["budget_monthly_tokens"])
        if "budget_monthly_requests" in updates:
            set_clauses.append("budget_monthly_requests = ?")
            params.append(updates["budget_monthly_requests"])
        if "metadata" in updates:
            set_clauses.append("metadata = ?")
            params.append(json.dumps(updates["metadata"]))

        if not set_clauses:
            return self.get_tenant(tenant_id)

        set_clause = ", ".join(set_clauses)
        params.append(tenant_id)

        with self._conn() as conn:
            conn.execute(
                f"UPDATE tenants SET {set_clause} WHERE tenant_id = ?", params
            )
        return self.get_tenant(tenant_id)

    def delete_tenant(self, tenant_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM tenants WHERE tenant_id = ?", (tenant_id,))
            return cur.rowcount > 0

    # ── Canary Events ──

    def record_canary_event(self, event: str, model_id: str, details: dict[str, Any] | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO canary_events (ts, event, model_id, details)
                VALUES (?, ?, ?, ?)
                """,
                (time.time(), event, model_id, json.dumps(details or {})),
            )

    def get_canary_history(
        self, *, model_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        conditions = []
        params: list = []
        if model_id is not None:
            conditions.append("model_id = ?")
            params.append(model_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM canary_events {where} ORDER BY ts DESC LIMIT ?",
                params + [limit],
            ).fetchall()
            results = []
            for r in rows:
                d = dict(r)
                d["details"] = json.loads(d["details"])
                results.append(d)
            return results


persistent_store = PersistentStore()
