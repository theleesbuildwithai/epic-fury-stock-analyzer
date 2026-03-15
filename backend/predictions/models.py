"""
Database models for tracking predictions.
Uses SQLite — a simple database that stores everything in one file.
No servers needed, perfect for learning.
"""

import sqlite3
import os
from datetime import datetime

DB_PATH = os.environ.get("DB_PATH", "predictions.db")


def get_db():
    """Get a database connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Create the database tables if they don't exist."""
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            predicted_direction TEXT NOT NULL,
            confidence_score REAL NOT NULL,
            entry_price REAL NOT NULL,
            target_price REAL,
            predicted_at TEXT NOT NULL,
            check_after_days INTEGER DEFAULT 30,
            actual_outcome TEXT DEFAULT 'pending',
            actual_price REAL,
            actual_return_pct REAL,
            resolved_at TEXT,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS benchmark_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date TEXT NOT NULL,
            sp500_price REAL,
            nasdaq_price REAL,
            djia_price REAL
        );
    """)
    conn.commit()
    conn.close()


def save_prediction(ticker: str, direction: str, confidence: float,
                    entry_price: float, target_price: float = None,
                    check_after_days: int = 30, notes: str = None) -> int:
    """Save a new prediction to the database."""
    conn = get_db()
    cursor = conn.execute(
        """INSERT INTO predictions
           (ticker, predicted_direction, confidence_score, entry_price,
            target_price, predicted_at, check_after_days, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (ticker.upper(), direction, confidence, entry_price,
         target_price, datetime.now().isoformat(), check_after_days, notes)
    )
    prediction_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return prediction_id


def get_all_predictions() -> list:
    """Get all predictions."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM predictions ORDER BY predicted_at DESC"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_pending_predictions() -> list:
    """Get predictions that haven't been resolved yet."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM predictions WHERE actual_outcome = 'pending' ORDER BY predicted_at DESC"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def resolve_prediction(prediction_id: int, actual_price: float, outcome: str):
    """Mark a prediction as resolved (hit or miss)."""
    conn = get_db()
    pred = conn.execute(
        "SELECT entry_price FROM predictions WHERE id = ?", (prediction_id,)
    ).fetchone()

    if pred:
        entry_price = pred["entry_price"]
        return_pct = round(((actual_price - entry_price) / entry_price) * 100, 2)
        conn.execute(
            """UPDATE predictions
               SET actual_outcome = ?, actual_price = ?,
                   actual_return_pct = ?, resolved_at = ?
               WHERE id = ?""",
            (outcome, actual_price, return_pct,
             datetime.now().isoformat(), prediction_id)
        )
        conn.commit()
    conn.close()


def save_benchmark_snapshot(sp500: float, nasdaq: float, djia: float):
    """Save current benchmark prices for comparison."""
    conn = get_db()
    conn.execute(
        """INSERT INTO benchmark_snapshots (snapshot_date, sp500_price, nasdaq_price, djia_price)
           VALUES (?, ?, ?, ?)""",
        (datetime.now().strftime("%Y-%m-%d"), sp500, nasdaq, djia)
    )
    conn.commit()
    conn.close()


def get_benchmark_snapshots() -> list:
    """Get all benchmark snapshots."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM benchmark_snapshots ORDER BY snapshot_date DESC"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]
