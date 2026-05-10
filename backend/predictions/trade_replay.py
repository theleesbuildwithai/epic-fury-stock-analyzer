"""
Trade Replay Simulator — ADVISORY ONLY.

For any closed trade, replay it with different stop / take / hold
parameters and discover which parameters would have made it a win.
Aggregate across many losses to find rules: "If I had used 5% stops
instead of 4%, my last 50 trades would have netted X% more."

CRITICAL SAFETY:
  This module NEVER modifies trades, NEVER changes live parameters,
  NEVER blocks anything. It is OFFLINE analysis. Output is purely
  informational — the user decides whether to act on the lessons.

PIPELINE:
  1. replay_trade(trade_id, alt_params) — pulls the historical price
     window for that trade, simulates exit under alternate stop/take/
     hold, returns alternate PnL
  2. find_optimal_params_for_trade(trade_id) — grid search over
     reasonable stop/take/hold combos
  3. aggregate_replay_lessons(lookback_days) — apply find_optimal
     across all recent trades, summarize what would have improved
     overall PnL
"""

import logging
import sqlite3
import os
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Grid search ranges (kept tight — combinatorial explosion risk)
STOP_PCT_GRID = [0.02, 0.03, 0.04, 0.05, 0.06, 0.08]
TAKE_PCT_GRID = [0.05, 0.08, 0.10, 0.15, 0.20]
HOLD_DAYS_GRID = [1, 3, 5, 10, 20]


def _get_db():
    db_path = os.environ.get("DB_PATH", "predictions.db")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _safe_price_window(ticker: str, start: str, days: int = 30) -> list:
    """Pull `days` of closes starting from `start`. Returns [] on fail."""
    try:
        import yfinance as yf
        from datetime import datetime as _dt, timedelta as _td
        st = _dt.strptime(start[:10], "%Y-%m-%d")
        en = st + _td(days=int(days) + 5)
        df = yf.download(ticker, start=st.strftime("%Y-%m-%d"),
                         end=en.strftime("%Y-%m-%d"),
                         progress=False, auto_adjust=True, threads=False)
        if df is None or df.empty:
            return []
        closes = df["Close"].dropna().values.tolist()
        if isinstance(closes[0], list):
            return [float(c[0]) for c in closes]
        return [float(c) for c in closes]
    except Exception as e:
        logger.debug(f"_safe_price_window {ticker} soft-fail: {e}")
        return []


def _simulate_exit(closes: list, entry_price: float, direction: str,
                    stop_pct: float, take_pct: float,
                    max_hold_days: int) -> dict:
    """Walk forward through `closes` (which start at entry day),
    determine exit. Returns {exit_day, exit_price, pnl_pct, exit_reason}."""
    try:
        if not closes or entry_price <= 0:
            return {"exit_day": 0, "exit_price": entry_price, "pnl_pct": 0,
                    "exit_reason": "no_data"}
        sign = 1 if direction == "long" else -1

        for day in range(1, min(len(closes), max_hold_days + 1)):
            cur = closes[day]
            move_pct = (cur - entry_price) / entry_price * sign
            if move_pct <= -stop_pct:
                # Stopped out
                return {"exit_day": day,
                        "exit_price": round(cur, 4),
                        "pnl_pct": round(move_pct * 100, 2),
                        "exit_reason": "stop"}
            if move_pct >= take_pct:
                return {"exit_day": day,
                        "exit_price": round(cur, 4),
                        "pnl_pct": round(move_pct * 100, 2),
                        "exit_reason": "take"}
        # Time stop
        last_day = min(len(closes) - 1, max_hold_days)
        if last_day < 1:
            last_day = 0
        cur = closes[last_day]
        move_pct = (cur - entry_price) / entry_price * sign
        return {"exit_day": last_day,
                "exit_price": round(cur, 4),
                "pnl_pct": round(move_pct * 100, 2),
                "exit_reason": "time"}
    except Exception as e:
        logger.debug(f"_simulate_exit soft-fail: {e}")
        return {"exit_day": 0, "exit_price": entry_price, "pnl_pct": 0,
                "exit_reason": "error"}


def replay_trade(trade_id: int, stop_pct: float = 0.04,
                  take_pct: float = 0.10, max_hold_days: int = 5) -> dict:
    """Replay a specific trade with alternate parameters. Returns
    actual_pnl + simulated_pnl + delta."""
    try:
        conn = _get_db()
        try:
            row = conn.execute(
                "SELECT * FROM paper_trades WHERE id = ?",
                (int(trade_id),)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return {"ok": False, "reason": "trade_not_found"}

        # Skip options trades (different exit mechanics)
        if row["instrument_type"] in ("call", "put"):
            return {"ok": False, "reason": "options_trade_replay_not_supported"}

        ticker = row["ticker"]
        direction = row["direction"]
        entry_price = float(row["entry_price"] or 0)
        entry_date = row["entry_date"]
        actual_pnl_pct = float(row["pnl_pct"] or 0)

        if entry_price <= 0 or not entry_date:
            return {"ok": False, "reason": "missing_entry_data"}

        closes = _safe_price_window(ticker, entry_date,
                                     days=int(max_hold_days) + 10)
        if not closes:
            return {"ok": False, "reason": "no_price_data"}

        sim = _simulate_exit(closes, entry_price, direction,
                              float(stop_pct), float(take_pct),
                              int(max_hold_days))
        return {
            "ok": True,
            "trade_id": int(trade_id),
            "ticker": ticker,
            "direction": direction,
            "entry_price": entry_price,
            "entry_date": entry_date,
            "actual_pnl_pct": actual_pnl_pct,
            "simulated": sim,
            "params": {"stop_pct": stop_pct, "take_pct": take_pct,
                       "max_hold_days": max_hold_days},
            "improvement_pct": round(sim["pnl_pct"] - actual_pnl_pct, 2),
        }
    except Exception as e:
        logger.warning(f"replay_trade {trade_id} fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}


def find_optimal_params_for_trade(trade_id: int) -> dict:
    """Grid-search optimal stop/take/hold for one trade. Returns the
    parameter combo that gave the best outcome + the actual params used."""
    try:
        conn = _get_db()
        try:
            row = conn.execute(
                "SELECT * FROM paper_trades WHERE id = ?",
                (int(trade_id),)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return {"ok": False, "reason": "trade_not_found"}
        if row["instrument_type"] in ("call", "put"):
            return {"ok": False, "reason": "options_not_supported"}

        ticker = row["ticker"]
        direction = row["direction"]
        entry_price = float(row["entry_price"] or 0)
        entry_date = row["entry_date"]
        actual_pnl_pct = float(row["pnl_pct"] or 0)

        if entry_price <= 0 or not entry_date:
            return {"ok": False, "reason": "missing_entry_data"}

        # Pull closes ONCE (max grid hold = 20)
        closes = _safe_price_window(ticker, entry_date, days=30)
        if not closes:
            return {"ok": False, "reason": "no_price_data"}

        results = []
        for sp in STOP_PCT_GRID:
            for tp in TAKE_PCT_GRID:
                for hd in HOLD_DAYS_GRID:
                    sim = _simulate_exit(closes, entry_price, direction, sp, tp, hd)
                    results.append({
                        "stop_pct": sp, "take_pct": tp, "hold_days": hd,
                        "pnl_pct": sim["pnl_pct"],
                        "exit_reason": sim["exit_reason"],
                        "exit_day": sim["exit_day"],
                    })

        if not results:
            return {"ok": False, "reason": "no_results"}

        best = max(results, key=lambda r: r["pnl_pct"])
        return {
            "ok": True,
            "trade_id": int(trade_id),
            "ticker": ticker,
            "actual_pnl_pct": actual_pnl_pct,
            "best_params": best,
            "improvement_pct": round(best["pnl_pct"] - actual_pnl_pct, 2),
            "n_combos_tested": len(results),
        }
    except Exception as e:
        logger.warning(f"find_optimal_params_for_trade {trade_id} fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}


def aggregate_replay_lessons(lookback_days: int = 90,
                              limit: int = 50) -> dict:
    """For recent closed trades, find optimal params for each, then
    aggregate which parameter changes would have improved overall PnL.

    HEAVY operation — N trades × ~150 grid combos × yfinance fetch.
    Caller should expect 30-90s. Capped at `limit` trades for safety."""
    try:
        cutoff = (datetime.utcnow() - timedelta(days=int(lookback_days))).isoformat()
        conn = _get_db()
        try:
            trades = conn.execute(
                """SELECT id, ticker, direction, entry_price, entry_date,
                          pnl_pct, instrument_type
                   FROM paper_trades
                   WHERE status = 'closed'
                     AND entry_date >= ?
                     AND (instrument_type IS NULL OR instrument_type = 'equity')
                   ORDER BY entry_date DESC
                   LIMIT ?""",
                (cutoff, int(limit))
            ).fetchall()
        finally:
            conn.close()

        if not trades:
            return {"ok": True, "trades_analyzed": 0,
                    "message": "no eligible trades in window"}

        from collections import Counter
        per_trade = []
        improvements = []
        best_stops = Counter()
        best_takes = Counter()
        best_holds = Counter()
        actual_total = 0
        best_total = 0

        for t in trades:
            try:
                opt = find_optimal_params_for_trade(t["id"])
                if not opt.get("ok"):
                    continue
                actual = float(t["pnl_pct"] or 0)
                best_pnl = opt["best_params"]["pnl_pct"]
                actual_total += actual
                best_total += best_pnl
                improvement = best_pnl - actual
                improvements.append(improvement)
                per_trade.append({
                    "trade_id": t["id"], "ticker": t["ticker"],
                    "actual_pnl_pct": actual, "optimal_pnl_pct": best_pnl,
                    "improvement_pct": round(improvement, 2),
                    "optimal_stop_pct": opt["best_params"]["stop_pct"],
                    "optimal_take_pct": opt["best_params"]["take_pct"],
                    "optimal_hold_days": opt["best_params"]["hold_days"],
                })
                best_stops[opt["best_params"]["stop_pct"]] += 1
                best_takes[opt["best_params"]["take_pct"]] += 1
                best_holds[opt["best_params"]["hold_days"]] += 1
            except Exception:
                continue

        if not per_trade:
            return {"ok": True, "trades_analyzed": 0,
                    "message": "no replays succeeded"}

        return {
            "ok": True,
            "lookback_days": lookback_days,
            "trades_analyzed": len(per_trade),
            "actual_total_pnl_pct": round(actual_total, 2),
            "optimal_total_pnl_pct": round(best_total, 2),
            "improvement_total_pct": round(best_total - actual_total, 2),
            "avg_improvement_per_trade_pct":
                round(sum(improvements) / len(improvements), 2),
            "most_common_optimal_stop": best_stops.most_common(3),
            "most_common_optimal_take": best_takes.most_common(3),
            "most_common_optimal_hold": best_holds.most_common(3),
            "per_trade_sample": per_trade[:20],
        }
    except Exception as e:
        logger.warning(f"aggregate_replay_lessons fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}
