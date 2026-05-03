"""
State Audit Trail — Sentinel Quant

Lightweight append-only log of every meaningful state mutation.
When something goes wrong (cash inflated, pause stuck, peak corrupted) we
get a full timeline instead of guessing what changed.

Tracked mutation types:
  - cash               : every adjust_cash / set_cash call
  - daily_paused       : on/off transitions
  - peak_return        : when peak high-water mark resets
  - exposure_target    : when DYNAMIC_EXPOSURE_BASE/MIN/MAX recomputed
  - pause_clear        : manual or automatic pause clears
  - cash_sentinel      : when sentinel rejects a mutation
  - circuit_breaker    : when trade execution halts
  - snapshot_drift     : when daily snapshot is auto-corrected

API:
  - init_audit_table()              — runs at startup, idempotent
  - audit_log(mutation_type, ...)   — append-only writer (never raises)
  - get_recent_audit(limit, type)   — read recent entries

Storage: single table `state_audit_log` in the same SQLite DB. Indexed
on (mutation_type, timestamp DESC) for fast filtered queries.
"""

import json
import logging
import sqlite3
from datetime import datetime
import os

logger = logging.getLogger(__name__)


def _get_db():
    """Get a connection. Uses same DB_PATH env var as models.py."""
    db_path = os.environ.get("DB_PATH", "predictions.db")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_audit_table():
    """Create the audit table + index. Idempotent. Never raises."""
    try:
        conn = _get_db()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS state_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    mutation_type TEXT NOT NULL,
                    old_value TEXT,
                    new_value TEXT,
                    delta TEXT,
                    caller TEXT,
                    reason TEXT,
                    metadata TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_audit_type_time
                  ON state_audit_log(mutation_type, timestamp DESC);

                CREATE INDEX IF NOT EXISTS idx_audit_time
                  ON state_audit_log(timestamp DESC);
            """)
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"audit init_audit_table failed (non-fatal): {e}")


def audit_log(mutation_type: str,
              old_value=None,
              new_value=None,
              delta=None,
              caller: str = None,
              reason: str = None,
              metadata: dict = None):
    """Append a single audit entry. Never raises — failure to log
    must not break the underlying mutation.

    All non-string fields are stringified for storage so the table
    schema stays simple.
    """
    try:
        conn = _get_db()
        try:
            conn.execute(
                """INSERT INTO state_audit_log
                   (timestamp, mutation_type, old_value, new_value, delta,
                    caller, reason, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.utcnow().isoformat(),
                    str(mutation_type)[:64],
                    None if old_value is None else str(old_value)[:200],
                    None if new_value is None else str(new_value)[:200],
                    None if delta is None else str(delta)[:80],
                    None if caller is None else str(caller)[:120],
                    None if reason is None else str(reason)[:300],
                    None if metadata is None else json.dumps(metadata, default=str)[:500],
                )
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"audit_log write failed (non-fatal) {mutation_type}: {e}")


def get_recent_audit(limit: int = 100, mutation_type: str = None) -> list:
    """Read recent audit entries. Returns list of dicts. Never raises."""
    try:
        conn = _get_db()
        try:
            if mutation_type:
                rows = conn.execute(
                    """SELECT * FROM state_audit_log
                       WHERE mutation_type = ?
                       ORDER BY timestamp DESC LIMIT ?""",
                    (mutation_type, int(limit))
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM state_audit_log
                       ORDER BY timestamp DESC LIMIT ?""",
                    (int(limit),)
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"get_recent_audit failed (non-fatal): {e}")
        return []


def get_audit_summary() -> dict:
    """Aggregate counts per mutation type — for the health endpoint."""
    try:
        conn = _get_db()
        try:
            rows = conn.execute(
                """SELECT mutation_type, COUNT(*) AS n,
                          MAX(timestamp) AS last_at
                   FROM state_audit_log
                   GROUP BY mutation_type
                   ORDER BY n DESC"""
            ).fetchall()
            return {
                "total_entries": sum(r["n"] for r in rows),
                "by_type": [dict(r) for r in rows],
            }
        finally:
            conn.close()
    except Exception as e:
        return {"error": str(e)[:120]}
