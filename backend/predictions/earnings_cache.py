"""
Earnings Cache — the decision engine's zero-network view of earnings dates.

/api/analyze-extras already fetches each ticker's next earnings date from
yfinance (threaded, timeout-guarded). This module persists that date so the
ANALYZE path — which must never add network calls — can read "days until
earnings" with a single local SQLite lookup and hard-gate bullish verdicts
into a known binary event.

Fail-safe everywhere: a cache problem must never break any response.
"""

import os
import sqlite3
from datetime import datetime


def _conn():
    db_path = os.environ.get("DB_PATH", "predictions.db")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS earnings_cache (
            ticker TEXT PRIMARY KEY,
            next_date TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    return conn


def save_next_earnings(ticker, next_date):
    """Upsert a ticker's next scheduled earnings date (YYYY-MM-DD).

    Returns True if written, False if skipped (invalid input). Never raises.
    """
    try:
        if not ticker or not isinstance(next_date, str):
            return False
        datetime.strptime(next_date[:10], "%Y-%m-%d")  # validate format
    except (TypeError, ValueError):
        return False
    try:
        conn = _conn()
        with conn:
            conn.execute(
                """INSERT INTO earnings_cache (ticker, next_date, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(ticker) DO UPDATE SET
                     next_date = excluded.next_date,
                     updated_at = excluded.updated_at""",
                (str(ticker).upper(), next_date[:10],
                 datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
            )
        return True
    except Exception:
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_days_until_earnings(ticker):
    """Calendar days until the ticker's cached next earnings date.

    Returns an int >= 0, or None when unknown / date already passed.
    Pure local read — zero network. Never raises.
    """
    try:
        conn = _conn()
        row = conn.execute(
            "SELECT next_date FROM earnings_cache WHERE ticker = ?",
            (str(ticker).upper(),),
        ).fetchone()
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if not row or not row[0]:
        return None
    try:
        nd = datetime.strptime(str(row[0])[:10], "%Y-%m-%d").date()
        days = (nd - datetime.utcnow().date()).days
        return days if days >= 0 else None
    except (TypeError, ValueError):
        return None
