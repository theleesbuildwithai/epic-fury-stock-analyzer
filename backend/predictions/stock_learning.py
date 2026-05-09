"""
Stock-Level Learning — Sentinel Quant

Records the outcome of every closed trade so the system can learn its own
blind spots. After enough data, picks confidence is adjusted by per-ticker
historical hit rate — boosting confidence on stocks the system reads well
and lowering it on stocks it's bad at.

Three public functions:
  - record_trade_outcome(ticker, direction, signal_score, pnl_pct, won)
      Called once per closed trade. Append-only.
  - get_stock_stats(ticker, lookback_days=90)
      Returns {wins, losses, win_rate, avg_pnl_pct, sample_size, confidence_adj}
  - get_leaderboard(limit=20, mode='best'|'worst')
      For the dashboard / debugging.

Storage: SQLite table `stock_learning_log` in the same DB as paper trades.
Schema kept tiny so it can grow to 100k+ rows without performance issues.

SAFETY: every function is wrapped in try/except. A failure in this module
must NEVER crash a trade close or block portfolio reads.
"""

import logging
import sqlite3
from datetime import datetime, timedelta
import os

logger = logging.getLogger(__name__)


def _get_db():
    """Same DB as the rest of the system."""
    db_path = os.environ.get("DB_PATH", "predictions.db")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_stock_learning_table():
    """Create the table on startup. Idempotent."""
    try:
        conn = _get_db()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS stock_learning_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    signal_score REAL,
                    pnl_pct REAL NOT NULL,
                    won INTEGER NOT NULL,
                    closed_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_sl_ticker_time
                    ON stock_learning_log(ticker, closed_at DESC);

                CREATE INDEX IF NOT EXISTS idx_sl_time
                    ON stock_learning_log(closed_at DESC);

                -- Auto-fixer penalties: per-ticker confidence reductions
                -- learned from backtest results. Multiplied with the live
                -- confidence_adj. Auto-expire after 30 days so the system
                -- can recover its own mistakes.
                CREATE TABLE IF NOT EXISTS backtest_penalties (
                    ticker TEXT PRIMARY KEY,
                    penalty_factor REAL NOT NULL,
                    reason TEXT,
                    backtest_avg_pnl_pct REAL,
                    backtest_trades INTEGER,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
            """)
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"init_stock_learning_table failed (non-fatal): {e}")


def set_backtest_penalty(ticker: str, penalty_factor: float, reason: str = "",
                          avg_pnl_pct: float = None, trades: int = None,
                          ttl_days: int = 30) -> dict:
    """Set a per-ticker confidence penalty learned from backtest.

    penalty_factor in [0.5, 1.0]: multiplied with confidence_adj.
    NEVER raises. REPLACES any existing penalty (latest insight wins).
    """
    try:
        if not ticker:
            return {"ok": False, "reason": "no_ticker"}
        # Bound penalty: never below 0.5 (would silence stock entirely),
        # never above 1.0 (penalty must REDUCE — boosts come from real
        # trade win rates only, never from backtest)
        pf = max(0.50, min(1.0, float(penalty_factor)))
        now = datetime.utcnow()
        exp = now + timedelta(days=int(ttl_days))
        conn = _get_db()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO backtest_penalties
                   (ticker, penalty_factor, reason, backtest_avg_pnl_pct,
                    backtest_trades, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (ticker.upper()[:16], pf, str(reason or "")[:200],
                 float(avg_pnl_pct) if avg_pnl_pct is not None else None,
                 int(trades) if trades is not None else None,
                 now.isoformat(), exp.isoformat())
            )
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "ticker": ticker.upper(), "penalty_factor": pf,
                "expires_at": exp.isoformat()}
    except Exception as e:
        logger.debug(f"set_backtest_penalty soft-fail {ticker}: {e}")
        return {"ok": False, "reason": str(e)[:200]}


def get_backtest_penalty(ticker: str) -> float:
    """Returns the active penalty factor for a ticker (1.0 = no penalty).
    Auto-prunes expired penalties on read. Never raises."""
    try:
        if not ticker:
            return 1.0
        now_iso = datetime.utcnow().isoformat()
        conn = _get_db()
        try:
            row = conn.execute(
                """SELECT penalty_factor, expires_at FROM backtest_penalties
                   WHERE ticker = ?""",
                (ticker.upper(),)
            ).fetchone()
            if not row:
                return 1.0
            if row["expires_at"] and row["expires_at"] < now_iso:
                # Expired — clean it up
                try:
                    conn.execute("DELETE FROM backtest_penalties WHERE ticker = ?",
                                 (ticker.upper(),))
                    conn.commit()
                except Exception:
                    pass
                return 1.0
            return float(row["penalty_factor"])
        finally:
            conn.close()
    except Exception:
        return 1.0


def get_all_backtest_penalties() -> list:
    """Returns all active (non-expired) penalties. Never raises."""
    try:
        now_iso = datetime.utcnow().isoformat()
        conn = _get_db()
        try:
            rows = conn.execute(
                """SELECT * FROM backtest_penalties
                   WHERE expires_at >= ?
                   ORDER BY penalty_factor ASC""",
                (now_iso,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"get_all_backtest_penalties soft-fail: {e}")
        return []


def clear_backtest_penalties() -> dict:
    """Wipe all penalties — the 'undo' button if backtest insights look
    wrong. NEVER raises."""
    try:
        conn = _get_db()
        try:
            n = conn.execute("DELETE FROM backtest_penalties").rowcount
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "cleared": int(n or 0)}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


def record_trade_outcome(ticker: str,
                         direction: str,
                         signal_score: float,
                         pnl_pct: float,
                         won: bool = None):
    """Append a trade outcome. Called from close_paper_trade.
    NEVER raises — failure to log must not break trade close."""
    try:
        if not ticker:
            return
        if won is None:
            won = (pnl_pct or 0) > 0
        conn = _get_db()
        try:
            conn.execute(
                """INSERT INTO stock_learning_log
                   (ticker, direction, signal_score, pnl_pct, won, closed_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    str(ticker)[:16],
                    str(direction or "long")[:8],
                    float(signal_score or 0),
                    float(pnl_pct or 0),
                    1 if won else 0,
                    datetime.utcnow().isoformat(),
                )
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"record_trade_outcome soft-fail {ticker}: {e}")


def get_stock_stats(ticker: str, lookback_days: int = 90) -> dict:
    """Returns rolling stats for one ticker. Never raises."""
    try:
        if not ticker:
            return {"ok": False, "reason": "no_ticker"}
        cutoff = (datetime.utcnow() - timedelta(days=int(lookback_days))).isoformat()
        conn = _get_db()
        try:
            rows = conn.execute(
                """SELECT direction, pnl_pct, won, signal_score
                   FROM stock_learning_log
                   WHERE ticker = ? AND closed_at >= ?
                   ORDER BY closed_at DESC""",
                (ticker.upper(), cutoff)
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            # Even with zero live trades, a backtest penalty may apply
            bt_only = get_backtest_penalty(ticker)
            interp = "no historical data — neutral confidence"
            if bt_only < 1.0:
                interp = f"no live trades; backtest penalty x{bt_only:.2f}"
            return {"ok": True, "ticker": ticker.upper(), "sample_size": 0,
                    "confidence_adj_live": 1.0,
                    "backtest_penalty": bt_only,
                    "confidence_adj": round(bt_only, 3),
                    "interpretation": interp}

        wins = sum(1 for r in rows if r["won"])
        n = len(rows)
        win_rate = wins / n
        avg_pnl = sum(r["pnl_pct"] for r in rows) / n
        avg_winner = (sum(r["pnl_pct"] for r in rows if r["won"]) / wins) if wins else 0
        losers_count = n - wins
        avg_loser = (sum(r["pnl_pct"] for r in rows if not r["won"]) / losers_count) if losers_count else 0

        # Confidence adjustment: ranges from 0.7 (terrible) to 1.3 (great)
        # Only meaningful with >= 5 trades
        if n < 5:
            conf_adj = 1.0
            interp = f"too few trades ({n}) — neutral confidence"
        else:
            # Map win_rate [0.30, 0.70] -> conf_adj [0.7, 1.3]
            wr_centered = max(0.30, min(0.70, win_rate)) - 0.50
            conf_adj = round(1.0 + wr_centered * 1.5, 3)  # ±15% adjustment max
            if conf_adj > 1.10:
                interp = f"strong: {n} trades, {win_rate*100:.0f}% wr — confidence boosted"
            elif conf_adj < 0.90:
                interp = f"weak: {n} trades, {win_rate*100:.0f}% wr — confidence reduced"
            else:
                interp = f"average: {n} trades, {win_rate*100:.0f}% wr — neutral"

        # ===== AUTO-FIXER PENALTY (from backtest insights) =====
        # If a backtest discovered this ticker is a serial loser, apply a
        # multiplicative penalty. Penalty in [0.5, 1.0] — only REDUCES
        # confidence, never boosts. Auto-expires after 30 days.
        # Bound final conf_adj_final to [0.50, 1.30] to prevent any
        # downstream consumer from seeing an out-of-range multiplier.
        bt_penalty = get_backtest_penalty(ticker)
        conf_adj_final = round(max(0.50, min(1.30, conf_adj * bt_penalty)), 3)
        if bt_penalty < 1.0:
            interp = f"{interp} | backtest penalty x{bt_penalty:.2f}"

        return {
            "ok": True,
            "ticker": ticker.upper(),
            "sample_size": n,
            "wins": wins,
            "losses": losers_count,
            "win_rate": round(win_rate * 100, 2),
            "avg_pnl_pct": round(avg_pnl, 3),
            "avg_winner_pct": round(avg_winner, 3),
            "avg_loser_pct": round(avg_loser, 3),
            "confidence_adj_live": conf_adj,         # before backtest penalty
            "backtest_penalty": bt_penalty,
            "confidence_adj": conf_adj_final,        # post-penalty (consumer-facing)
            "interpretation": interp,
            "lookback_days": lookback_days,
        }
    except Exception as e:
        logger.debug(f"get_stock_stats soft-fail {ticker}: {e}")
        return {"ok": False, "reason": str(e)[:200]}


def get_leaderboard(limit: int = 20, mode: str = "best",
                    min_trades: int = 5, lookback_days: int = 90) -> dict:
    """Returns top performers (mode='best') or biggest blind spots
    (mode='worst'). Filtered by min_trades to avoid noise from
    1-2 trade samples."""
    try:
        cutoff = (datetime.utcnow() - timedelta(days=int(lookback_days))).isoformat()
        conn = _get_db()
        try:
            rows = conn.execute(
                """SELECT ticker,
                          COUNT(*) AS n,
                          SUM(won) AS wins,
                          AVG(pnl_pct) AS avg_pnl,
                          SUM(pnl_pct) AS total_pnl
                   FROM stock_learning_log
                   WHERE closed_at >= ?
                   GROUP BY ticker
                   HAVING n >= ?""",
                (cutoff, int(min_trades))
            ).fetchall()
        finally:
            conn.close()

        results = []
        for r in rows:
            n = int(r["n"])
            wr = float(r["wins"]) / n if n > 0 else 0
            results.append({
                "ticker": r["ticker"],
                "trades": n,
                "win_rate": round(wr * 100, 2),
                "avg_pnl_pct": round(float(r["avg_pnl"]), 3),
                "total_pnl_pct": round(float(r["total_pnl"]), 3),
            })

        # Sort by total_pnl (best→worst or vice versa)
        reverse = (mode == "best")
        results.sort(key=lambda x: x["total_pnl_pct"], reverse=reverse)
        return {
            "ok": True,
            "mode": mode,
            "lookback_days": lookback_days,
            "min_trades": min_trades,
            "count": len(results[:limit]),
            "stocks": results[:limit],
        }
    except Exception as e:
        logger.debug(f"get_leaderboard soft-fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}


def backfill_from_closed_trades(limit: int = 5000) -> dict:
    """One-shot: import existing closed trades into the learning log.
    Safe to call repeatedly — uses INSERT OR IGNORE-style dedup."""
    try:
        from predictions.models import get_closed_trades
        trades = get_closed_trades(limit=limit) or []
        if not trades:
            return {"ok": True, "imported": 0, "reason": "no_trades"}

        # Dedup: skip rows that already match (ticker + closed_at)
        conn = _get_db()
        try:
            existing = set()
            try:
                rows = conn.execute(
                    "SELECT ticker, closed_at FROM stock_learning_log"
                ).fetchall()
                existing = {(r["ticker"], r["closed_at"]) for r in rows}
            except Exception:
                pass

            imported = 0
            for t in trades:
                try:
                    ticker = (t.get("ticker") or "").upper()
                    if not ticker:
                        continue
                    closed_at = t.get("exit_date") or t.get("closed_at") or t.get("entry_date") or ""
                    if not closed_at:
                        continue
                    if (ticker, closed_at) in existing:
                        continue
                    pnl_pct = float(t.get("pnl_pct") or 0)
                    won = pnl_pct > 0
                    direction = t.get("direction") or "long"
                    score = float(t.get("signal_score") or t.get("composite_score") or 0)
                    if t.get("corrupted_marked"):
                        continue  # skip corrupted (zero-pnl) trades
                    conn.execute(
                        """INSERT INTO stock_learning_log
                           (ticker, direction, signal_score, pnl_pct, won, closed_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (ticker, direction[:8], score, pnl_pct, 1 if won else 0, closed_at)
                    )
                    imported += 1
                except Exception:
                    continue
            conn.commit()
        finally:
            conn.close()

        return {"ok": True, "imported": imported, "scanned": len(trades)}
    except Exception as e:
        logger.warning(f"backfill_from_closed_trades fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}
