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
            sector TEXT,
            hold_class TEXT DEFAULT 'swing'
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

        CREATE TABLE IF NOT EXISTS sp500_cache (
            date TEXT PRIMARY KEY,
            close_price REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS paper_cash (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            cash REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trading_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS regime_factor_weights (
            regime TEXT NOT NULL,
            factor_name TEXT NOT NULL,
            weight REAL NOT NULL,
            win_rate REAL DEFAULT 0,
            sharpe REAL DEFAULT 0,
            total_trades INTEGER DEFAULT 0,
            last_updated TEXT,
            PRIMARY KEY (regime, factor_name)
        );

        CREATE TABLE IF NOT EXISTS geo_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            region TEXT,
            description TEXT,
            estimated_date TEXT NOT NULL,
            confidence TEXT DEFAULT 'low',
            source_headline TEXT,
            source_feed TEXT,
            detected_at TEXT NOT NULL,
            outcome TEXT DEFAULT 'pending',
            outcome_detected_at TEXT,
            is_manual_override INTEGER DEFAULT 0,
            last_seen_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_geo_events_date ON geo_events(estimated_date);
    """)

    # Performance indexes
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON paper_trades(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_date ON paper_trades(entry_date)")

    # Migration: add hold_class column if missing (for existing DBs)
    try:
        conn.execute("SELECT hold_class FROM paper_trades LIMIT 1")
    except Exception:
        try:
            conn.execute("ALTER TABLE paper_trades ADD COLUMN hold_class TEXT DEFAULT 'swing'")
        except Exception:
            pass

    # Migration: add options trading columns if missing
    options_columns = [
        ("instrument_type", "TEXT DEFAULT 'equity'"),
        ("strike_price", "REAL"),
        ("expiration_date", "TEXT"),
        ("contracts", "REAL"),
        ("premium_per_contract", "REAL"),
        ("underlying_price_at_entry", "REAL"),
        ("option_delta", "REAL"),
        ("option_iv", "REAL"),
    ]
    for col_name, col_type in options_columns:
        try:
            conn.execute(f"SELECT {col_name} FROM paper_trades LIMIT 1")
        except Exception:
            try:
                conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {col_name} {col_type}")
            except Exception:
                pass

    # ===== DUPLICATE PREVENTION (DB-level) =====
    # Partial UNIQUE index — guarantees only ONE open position can exist per
    # (ticker, direction, instrument_type, strike, expiry) combination at any
    # time. WHERE status='open' makes it only enforced for live trades.
    # MUST RUN AFTER the column migrations above so instrument_type and
    # strike_price columns exist (otherwise the COALESCE refs fail).
    # If creation fails because of existing dups in DB, the consolidation
    # migration in main.py startup will dedupe first, then a subsequent
    # startup will be able to enforce the constraint.
    try:
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_open_position
            ON paper_trades(
                ticker,
                direction,
                COALESCE(instrument_type, 'equity'),
                COALESCE(strike_price, 0),
                COALESCE(expiration_date, '')
            )
            WHERE status = 'open'
        """)
    except Exception:
        pass

    # Initialize paper_cash if empty
    existing = conn.execute("SELECT cash FROM paper_cash WHERE id=1").fetchone()
    if not existing:
        conn.execute("INSERT INTO paper_cash (id, cash) VALUES (1, 109000.0)")
    conn.commit()
    conn.close()


def get_trading_state(key: str, default: str = "0") -> str:
    """Get a persistent trading state value."""
    conn = get_db()
    row = conn.execute("SELECT value FROM trading_state WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_trading_state(key: str, value: str):
    """Set a persistent trading state value."""
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO trading_state (key, value, updated_at) VALUES (?, ?, ?)",
        (key, str(value), datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def get_cash() -> float:
    """Get current cash — always accurate, updated atomically with every trade."""
    conn = get_db()
    row = conn.execute("SELECT cash FROM paper_cash WHERE id=1").fetchone()
    conn.close()
    return row["cash"] if row else 109000.0


def set_cash(amount: float):
    """Set cash to a specific amount (for resets)."""
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO paper_cash (id, cash) VALUES (1, ?)", (round(amount, 2),))
    conn.commit()
    conn.close()


def adjust_cash(delta: float):
    """Atomically add/subtract from cash. Positive = add, negative = subtract."""
    conn = get_db()
    conn.execute("UPDATE paper_cash SET cash = cash + ? WHERE id=1", (round(delta, 2),))
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
                     sector: str = "", hold_class: str = "swing",
                     instrument_type: str = "equity", strike_price: float = None,
                     expiration_date: str = None, contracts: float = None,
                     premium_per_contract: float = None,
                     underlying_price_at_entry: float = None,
                     option_delta: float = None, option_iv: float = None) -> int:
    """Save a new paper trade and atomically deduct cash.
    For options: cost = premium_per_contract * contracts * 100 (total premium).
    For equity: cost = entry_price * shares (as before).

    DUPLICATE PREVENTION (added after the parallel-cycle dup incident):
    If an open trade already exists for the same ticker+direction
    (and same instrument_type for options), refuse to insert and return
    the existing trade's id. Prevents the case where two scheduler
    threads (max_instances=2) each see open_tickers as empty and both
    open the same position.
    """
    import logging as _log_save
    _logger_save = _log_save.getLogger("paper_trader.save")

    if instrument_type in ("call", "put") and contracts and premium_per_contract:
        cost = round(premium_per_contract * contracts * 100, 2)
    else:
        cost = round(entry_price * shares, 2)
    conn = get_db()

    # ===== DUPLICATE GUARD =====
    # Same ticker + direction + instrument_type already open? Skip insert.
    # For options, also match strike+expiry (same contract).
    try:
        if instrument_type in ("call", "put"):
            existing = conn.execute(
                """SELECT id FROM paper_trades
                   WHERE status='open' AND ticker=? AND direction=?
                     AND instrument_type=? AND strike_price=? AND expiration_date=?""",
                (ticker.upper(), direction, instrument_type, strike_price, expiration_date)
            ).fetchone()
        else:
            existing = conn.execute(
                """SELECT id FROM paper_trades
                   WHERE status='open' AND ticker=? AND direction=?
                     AND (instrument_type IS NULL OR instrument_type='equity')""",
                (ticker.upper(), direction)
            ).fetchone()
        if existing:
            existing_id = existing["id"]
            conn.close()
            _logger_save.warning(
                f"DUPLICATE BLOCKED: open {direction} {instrument_type} on "
                f"{ticker.upper()} already exists (id={existing_id}). "
                f"Skipping new save_paper_trade call to prevent duplicate position."
            )
            return existing_id
    except Exception as _e:
        # Duplicate guard failure must NEVER block a legitimate save
        _logger_save.warning(f"Duplicate guard error (proceeding with save): {_e}")

    # Atomically: insert trade AND deduct cash in same transaction
    cursor = conn.execute(
        """INSERT INTO paper_trades
           (ticker, direction, entry_price, shares, entry_date, signal_score,
            regime_at_entry, factors_used, stop_loss_price, target_price,
            hold_duration_days, sector, hold_class, status,
            instrument_type, strike_price, expiration_date, contracts,
            premium_per_contract, underlying_price_at_entry, option_delta, option_iv)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open',
                   ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ticker.upper(), direction, entry_price, shares,
         datetime.now().isoformat(), signal_score, regime,
         json.dumps(factors or {}), stop_loss, target_price, hold_days, sector,
         hold_class,
         instrument_type, strike_price, expiration_date, contracts,
         premium_per_contract, underlying_price_at_entry, option_delta, option_iv)
    )
    trade_id = cursor.lastrowid
    conn.execute("UPDATE paper_cash SET cash = cash - ? WHERE id=1", (cost,))
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


def update_trade_stop(trade_id: int, new_stop: float):
    """Update the trailing stop loss price for an open trade."""
    try:
        conn = get_db()
        conn.execute(
            "UPDATE paper_trades SET stop_loss_price = ? WHERE id = ? AND status = 'open'",
            (new_stop, trade_id)
        )
        conn.commit()
    except Exception:
        pass  # Non-critical — will recalculate next cycle


def close_paper_trade(trade_id: int, exit_price: float):
    """Close a paper trade, calculate P&L, and atomically return cash.
    For options: exit_price is the exit premium per share.
    P&L = (exit_premium - entry_premium) * contracts * 100.
    Cash returned = exit_premium * contracts * 100.

    SAFETY VALIDATOR (added after the cash-inflation incident):
    Rejects impossible price ratios (>15x or <1/15) which indicate a
    units mismatch (e.g., entry stored as option premium but exit passed
    as equity price). Without this guard, a trade can credit thousands
    of dollars in bogus cash. When the validator triggers, the trade is
    closed at the entry price (zero pnl) and an error is logged so the
    bug can be diagnosed without blowing up cash.
    """
    import logging as _log
    _logger = _log.getLogger("paper_trader.close")
    conn = get_db()
    trade = conn.execute(
        "SELECT * FROM paper_trades WHERE id = ?", (trade_id,)
    ).fetchone()
    if trade:
        entry = trade["entry_price"]
        shares = trade["shares"]
        direction = trade["direction"]
        instrument_type = trade["instrument_type"] or "equity"

        # ===== SAFETY VALIDATOR =====
        # If the exit_price is wildly different from the entry price (>15x or
        # <1/15), this is almost certainly a units mismatch — e.g., entry
        # stored as $2.53 (option premium) but exit passed as $128 (equity).
        # In that case, close the trade FLAT (no pnl, no cash credit) so the
        # database isn't corrupted, and log the incident loudly.
        # Thresholds chosen to:
        #   - CATCH the bug (observed at 50-64x ratio)
        #   - ALLOW legitimate big moves (options can move 10-30x legitimately)
        #   - ALLOW options expiring near-worthless (premium drops to pennies)
        # HIGH-RATIO (>30x): always rejected. No legitimate trade should ever
        #   close 30x+ its entry.
        # LOW-RATIO (<1/30): allowed for OPTIONS (legit when expiring worthless),
        #   rejected for EQUITY (a stock dropping 97%+ in a single trade is
        #   essentially always a units mismatch).
        try:
            if entry and exit_price and entry > 0:
                ratio = exit_price / entry
                _is_option = instrument_type in ("call", "put")
                _reject = False
                _reject_reason = ""
                if ratio > 30:
                    _reject = True
                    _reject_reason = f"exit/entry ratio {ratio:.2f}x exceeds 30x ceiling"
                elif ratio < (1 / 30) and not _is_option:
                    _reject = True
                    _reject_reason = (
                        f"exit/entry ratio {ratio:.4f} below 1/30 floor "
                        f"for non-options (likely units mismatch)"
                    )

                if _reject:
                    _logger.error(
                        f"REJECTED IMPOSSIBLE PRICE RATIO on trade {trade_id} "
                        f"({trade['ticker']} {direction} {instrument_type}): "
                        f"entry=${entry:.4f} exit=${exit_price:.4f} — {_reject_reason}. "
                        f"Closing FLAT (no pnl, no cash change) to prevent corruption."
                    )
                    conn.execute(
                        """UPDATE paper_trades
                           SET exit_price=?, exit_date=?, pnl_dollars=0, pnl_pct=0,
                               status='closed_flat_validator'
                           WHERE id=?""",
                        (entry, datetime.now().isoformat(), trade_id)
                    )
                    conn.commit()
                    conn.close()
                    return
        except Exception as _e:
            # Validator must NEVER block a normal close — fall through
            _logger.warning(f"Close validator error for trade {trade_id}: {_e}")

        if instrument_type in ("call", "put"):
            # Options P&L: based on premium change
            entry_premium = trade["premium_per_contract"] or entry
            exit_premium = exit_price
            num_contracts = trade["contracts"] or 1
            multiplier = 100  # 100 shares per contract

            if direction == "long":
                # Bought option: profit if premium rises
                pnl_dollars = (exit_premium - entry_premium) * num_contracts * multiplier
                pnl_pct = ((exit_premium - entry_premium) / entry_premium) * 100 if entry_premium > 0 else 0
            else:
                # Sold option: profit if premium falls
                pnl_dollars = (entry_premium - exit_premium) * num_contracts * multiplier
                pnl_pct = ((entry_premium - exit_premium) / entry_premium) * 100 if entry_premium > 0 else 0
            if direction == "long":
                # Bought option: we paid premium at entry (cash deducted)
                # At exit: we sell the option, get back exit_premium * contracts * 100
                cash_returned = max(0, exit_premium * num_contracts * multiplier)
            else:
                # Sold option: we collected premium at entry (cash was NOT deducted —
                # instead, collateral equal to premium was reserved by deducting cash)
                # At exit: we buy back the option. Cash returned = collateral - buyback cost
                # = entry_premium * contracts * 100 - exit_premium * contracts * 100 + pnl
                # Simplified: just return the net P&L (collateral was already in cash)
                buyback_cost = exit_premium * num_contracts * multiplier
                collateral = entry_premium * num_contracts * multiplier
                cash_returned = max(0, collateral - buyback_cost + pnl_dollars)
        else:
            # Equity P&L (unchanged)
            if direction == "long":
                pnl_pct = ((exit_price - entry) / entry) * 100
            else:  # short
                pnl_pct = ((entry - exit_price) / entry) * 100
            pnl_dollars = pnl_pct / 100 * entry * shares
            if direction == "long":
                cash_returned = exit_price * shares
            else:
                cash_returned = entry * shares + pnl_dollars

        conn.execute(
            """UPDATE paper_trades
               SET exit_price=?, exit_date=?, pnl_dollars=?, pnl_pct=?, status='closed'
               WHERE id=?""",
            (exit_price, datetime.now().isoformat(),
             round(pnl_dollars, 2), round(pnl_pct, 2), trade_id)
        )
        conn.execute("UPDATE paper_cash SET cash = cash + ? WHERE id=1", (round(cash_returned, 2),))
        conn.commit()
    conn.close()


def get_options_exposure() -> dict:
    """Get total options exposure for risk management."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM paper_trades WHERE status='open' AND instrument_type IN ('call', 'put')"
    ).fetchall()
    conn.close()
    total_premium = 0
    total_contracts = 0
    total_delta_exposure = 0
    for r in rows:
        premium = (r["premium_per_contract"] or 0) * (r["contracts"] or 0) * 100
        total_premium += premium
        total_contracts += (r["contracts"] or 0)
        delta = r["option_delta"] or 0.5
        total_delta_exposure += delta * (r["contracts"] or 0) * 100
    return {
        "total_premium_deployed": round(total_premium, 2),
        "total_contracts": total_contracts,
        "total_delta_exposure": round(total_delta_exposure, 2),
        "num_option_positions": len(rows),
    }


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


def update_snapshot_sp500(snapshot_date: str, sp500_cum: float, sp500_daily: float = None):
    """Update the S&P 500 cumulative return on an existing snapshot.
    Used to backfill correct historical S&P data when the calculation bug is fixed."""
    conn = get_db()
    if sp500_daily is not None:
        conn.execute(
            """UPDATE portfolio_snapshots
               SET sp500_cumulative_return_pct=?, sp500_daily_return_pct=?
               WHERE snapshot_date=?""",
            (sp500_cum, sp500_daily, snapshot_date)
        )
    else:
        conn.execute(
            """UPDATE portfolio_snapshots
               SET sp500_cumulative_return_pct=?
               WHERE snapshot_date=?""",
            (sp500_cum, snapshot_date)
        )
    conn.commit()
    conn.close()


def get_signal_weights() -> dict:
    """Get current factor weights from the learning system."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM signal_performance").fetchall()
    conn.close()
    if not rows:
        # Default weights for all 22 factors (sum ≈ 1.00)
        return {
            "momentum": 0.11, "value": 0.08, "quality": 0.07,
            "low_vol": 0.06, "rsi2": 0.06, "volume": 0.05,
            "smart_money": 0.06, "relative_strength": 0.06,
            "bb_squeeze": 0.05, "vwap": 0.05,
            "hurst": 0.04, "autocorr": 0.03, "stat_arb": 0.03, "kurtosis": 0.02,
            "vol_compression": 0.03, "mtf_alignment": 0.04,
            "earnings_drift": 0.05, "vpoc": 0.03, "ichimoku": 0.04, "sector_rotation": 0.03,
            "candlestick": 0.03, "beta": 0.03,
        }
    return {row["factor_name"]: row["current_weight"] for row in rows}


# ============================================================
#  GEOPOLITICAL EVENT TRACKING
# ============================================================

def save_geo_event(event_key: str, event_type: str, region: str,
                   description: str, estimated_date: str, confidence: str = "low",
                   source_headline: str = "", source_feed: str = ""):
    """Save or update a detected geopolitical event. Manual overrides are never overwritten."""
    conn = get_db()
    now = datetime.now().isoformat()
    # Check if manual override exists — don't overwrite its date
    existing = conn.execute(
        "SELECT is_manual_override, estimated_date FROM geo_events WHERE event_key=?",
        (event_key,)
    ).fetchone()
    if existing and existing["is_manual_override"]:
        # Just update last_seen_at, don't touch the date
        conn.execute(
            "UPDATE geo_events SET last_seen_at=?, source_headline=?, source_feed=? WHERE event_key=?",
            (now, source_headline, source_feed, event_key)
        )
    else:
        conn.execute(
            """INSERT OR REPLACE INTO geo_events
               (event_key, event_type, region, description, estimated_date,
                confidence, source_headline, source_feed, detected_at, last_seen_at,
                outcome, is_manual_override)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0)""",
            (event_key, event_type, region, description, estimated_date,
             confidence, source_headline, source_feed, now, now)
        )
    conn.commit()
    conn.close()


def save_manual_geo_event(event_key: str, event_type: str, region: str,
                          description: str, estimated_date: str):
    """Save a manually entered geo event (overrides auto-detection)."""
    conn = get_db()
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT OR REPLACE INTO geo_events
           (event_key, event_type, region, description, estimated_date,
            confidence, source_headline, source_feed, detected_at, last_seen_at,
            outcome, is_manual_override)
           VALUES (?, ?, ?, ?, ?, 'high', 'manual entry', 'manual', ?, ?, 'pending', 1)""",
        (event_key, event_type, region, description, estimated_date, now, now)
    )
    conn.commit()
    conn.close()


def get_upcoming_geo_events(days_ahead: int = 14) -> list:
    """Get pending geo events in the next N days."""
    conn = get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    from datetime import timedelta
    future = (datetime.now() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    rows = conn.execute(
        """SELECT * FROM geo_events
           WHERE estimated_date BETWEEN ? AND ? AND outcome='pending'
           ORDER BY estimated_date ASC""",
        (today, future)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_active_geo_events() -> list:
    """Get events that have passed but have no outcome yet (within 14 days)."""
    conn = get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
    rows = conn.execute(
        """SELECT * FROM geo_events
           WHERE estimated_date < ? AND estimated_date >= ? AND outcome='pending'
           ORDER BY estimated_date DESC""",
        (today, cutoff)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_geo_event_outcome(event_key: str, outcome: str):
    """Update the outcome of a geo event (positive, negative, expired_unknown)."""
    conn = get_db()
    conn.execute(
        "UPDATE geo_events SET outcome=?, outcome_detected_at=? WHERE event_key=?",
        (outcome, datetime.now().isoformat(), event_key)
    )
    conn.commit()
    conn.close()


def get_all_geo_events(limit: int = 50) -> list:
    """Get all geo events for API display."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM geo_events ORDER BY estimated_date DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_regime_factor_weights(regime: str, min_trades: int = 0) -> dict:
    """Get per-regime factor weights. Returns {} if regime has no learned weights
    yet OR if the maximum trade count across factors is below min_trades.

    The min_trades guard is critical for safety — using regime weights based on
    very small samples (e.g., 10-20 trades) would be worse than using the
    global blended weights. Production callers should set min_trades >= 50.

    SAFETY: Always returns a dict (possibly empty). Never raises. Callers should
    fall back to global weights from get_signal_weights() when this returns {}.
    """
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT factor_name, weight, total_trades FROM regime_factor_weights WHERE regime=?",
            (regime,)
        ).fetchall()
        conn.close()
        if not rows:
            return {}
        if min_trades > 0:
            # Use MAX trade count across factors as the regime sample size
            # (different factors may have different counts; max is the best proxy
            # for "how much data does this regime have to learn from")
            max_trades = max((r["total_trades"] or 0) for r in rows)
            if max_trades < min_trades:
                return {}
        return {row["factor_name"]: row["weight"] for row in rows}
    except Exception:
        return {}


def update_regime_factor_weight(regime: str, factor_name: str, weight: float,
                                win_rate: float = 0, sharpe: float = 0,
                                total_trades: int = 0):
    """Update or insert a per-regime factor weight. Never raises (logs and continues)."""
    try:
        conn = get_db()
        conn.execute(
            """INSERT OR REPLACE INTO regime_factor_weights
               (regime, factor_name, weight, win_rate, sharpe, total_trades, last_updated)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (regime, factor_name, weight, win_rate, sharpe, total_trades,
             datetime.now().isoformat())
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # Per-regime learning failure must never break the main learner cycle


def get_all_regime_factor_weights() -> dict:
    """Get all learned per-regime weights as a nested dict for diagnostics.

    Returns: {regime: {factor_name: weight}}. Empty dict on failure.
    """
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT regime, factor_name, weight FROM regime_factor_weights"
        ).fetchall()
        conn.close()
        out = {}
        for row in rows:
            r = row["regime"]
            if r not in out:
                out[r] = {}
            out[r][row["factor_name"]] = row["weight"]
        return out
    except Exception:
        return {}


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
