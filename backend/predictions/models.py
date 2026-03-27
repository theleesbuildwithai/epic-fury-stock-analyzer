"""
Database models for tracking predictions and paper trading.
Uses SQLite — a simple database that stores everything in one file.
Includes: predictions, paper trades, portfolio snapshots, signal performance.
"""

import sqlite3
import os
import json
from datetime import datetime

DB_PATH = os.environ.get("DB_PATH", "predictions.db")


def get_db():
    """Get a database connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Create all database tables if they don't exist."""
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

        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_price REAL NOT NULL,
            shares REAL NOT NULL,
            entry_date TEXT NOT NULL,
            exit_price REAL,
            exit_date TEXT,
            pnl_dollars REAL,
            pnl_pct REAL,
            status TEXT DEFAULT 'open',
            signal_score REAL,
            regime_at_entry TEXT,
            factors_used TEXT,
            stop_loss_price REAL,
            target_price REAL,
            hold_duration_days INTEGER DEFAULT 30,
            sector TEXT
        );

        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date TEXT NOT NULL UNIQUE,
            total_value REAL NOT NULL,
            cash REAL NOT NULL,
            positions_value REAL NOT NULL,
            daily_return_pct REAL,
            cumulative_return_pct REAL,
            sp500_daily_return_pct REAL,
            sp500_cumulative_return_pct REAL,
            num_positions INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS signal_performance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            factor_name TEXT NOT NULL UNIQUE,
            current_weight REAL NOT NULL,
            win_rate REAL,
            avg_return REAL,
            sharpe_ratio REAL,
            total_trades INTEGER DEFAULT 0,
            last_updated TEXT,
            weight_history TEXT
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


# ============================================================
#  PAPER TRADING DATABASE FUNCTIONS
# ============================================================

def save_paper_trade(ticker: str, direction: str, entry_price: float,
                     shares: float, signal_score: float = 0, regime: str = "",
                     factors: dict = None, stop_loss: float = 0,
                     target_price: float = 0, hold_days: int = 30,
                     sector: str = "") -> int:
    """Save a new paper trade."""
    conn = get_db()
    cursor = conn.execute(
        """INSERT INTO paper_trades
           (ticker, direction, entry_price, shares, entry_date, signal_score,
            regime_at_entry, factors_used, stop_loss_price, target_price,
            hold_duration_days, sector, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
        (ticker.upper(), direction, entry_price, shares,
         datetime.now().isoformat(), signal_score, regime,
         json.dumps(factors or {}), stop_loss, target_price, hold_days, sector)
    )
    trade_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return trade_id


def get_open_trades() -> list:
    """Get all open paper trades."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM paper_trades WHERE status='open' ORDER BY entry_date DESC"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_closed_trades(limit: int = 200) -> list:
    """Get closed paper trades, most recent first."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM paper_trades WHERE status='closed' ORDER BY exit_date DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_all_paper_trades() -> list:
    """Get all paper trades."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM paper_trades ORDER BY entry_date DESC"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def close_paper_trade(trade_id: int, exit_price: float):
    """Close a paper trade and calculate P&L."""
    conn = get_db()
    trade = conn.execute(
        "SELECT * FROM paper_trades WHERE id = ?", (trade_id,)
    ).fetchone()
    if trade:
        entry = trade["entry_price"]
        shares = trade["shares"]
        direction = trade["direction"]
        if direction == "long":
            pnl_pct = ((exit_price - entry) / entry) * 100
        else:  # short
            pnl_pct = ((entry - exit_price) / entry) * 100
        pnl_dollars = pnl_pct / 100 * entry * shares
        conn.execute(
            """UPDATE paper_trades
               SET exit_price=?, exit_date=?, pnl_dollars=?, pnl_pct=?, status='closed'
               WHERE id=?""",
            (exit_price, datetime.now().isoformat(),
             round(pnl_dollars, 2), round(pnl_pct, 2), trade_id)
        )
        conn.commit()
    conn.close()


def save_portfolio_snapshot(total_value: float, cash: float,
                            positions_value: float, daily_ret: float,
                            cum_ret: float, sp500_daily: float,
                            sp500_cum: float, num_pos: int):
    """Save a daily portfolio snapshot."""
    conn = get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    conn.execute(
        """INSERT OR REPLACE INTO portfolio_snapshots
           (snapshot_date, total_value, cash, positions_value, daily_return_pct,
            cumulative_return_pct, sp500_daily_return_pct, sp500_cumulative_return_pct,
            num_positions)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (today, total_value, cash, positions_value, daily_ret,
         cum_ret, sp500_daily, sp500_cum, num_pos)
    )
    conn.commit()
    conn.close()


def get_portfolio_snapshots(days: int = 365) -> list:
    """Get portfolio snapshots for equity curve."""
    conn = get_db()
    rows = conn.execute(
        """SELECT * FROM portfolio_snapshots
           ORDER BY snapshot_date DESC LIMIT ?""", (days,)
    ).fetchall()
    conn.close()
    return [dict(row) for row in reversed(rows)]


def get_signal_weights() -> dict:
    """Get current factor weights from the learning system."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM signal_performance").fetchall()
    conn.close()
    if not rows:
        # Default weights
        return {
            "momentum": 0.25, "value": 0.20, "quality": 0.15,
            "low_vol": 0.15, "rsi2": 0.15, "volume": 0.10
        }
    return {row["factor_name"]: row["current_weight"] for row in rows}


def update_signal_weight(factor_name: str, weight: float, win_rate: float = 0,
                         avg_return: float = 0, sharpe: float = 0,
                         total_trades: int = 0):
    """Update or insert a signal factor weight."""
    conn = get_db()
    existing = conn.execute(
        "SELECT weight_history FROM signal_performance WHERE factor_name=?",
        (factor_name,)
    ).fetchone()
    history = []
    if existing and existing["weight_history"]:
        try:
            history = json.loads(existing["weight_history"])
        except Exception:
            history = []
    history.append({"date": datetime.now().isoformat(), "weight": weight})
    # Keep last 100 entries
    history = history[-100:]
    conn.execute(
        """INSERT OR REPLACE INTO signal_performance
           (factor_name, current_weight, win_rate, avg_return, sharpe_ratio,
            total_trades, last_updated, weight_history)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (factor_name, weight, win_rate, avg_return, sharpe,
         total_trades, datetime.now().isoformat(), json.dumps(history))
    )
    conn.commit()
    conn.close()
