"""
Auto-Fixer — closes the loop between backtest insights and live picks.

The system already has:
  - Level 1: stock_learning.py records every closed trade outcome
  - Level 2: backtest.py replays a momentum strategy on 100 historical tickers

This module wires them together:
  - run_feedback_loop() runs a backtest, extracts which tickers reliably
    LOSE money, then writes per-ticker confidence penalties to the
    stock_learning store.
  - The penalties are picked up by get_stock_stats() and reduce the
    confidence_adj that downstream pick generation already consumes.
  - Penalties auto-expire after 30 days, so the system can recover its
    own mistakes if a ticker stops being a serial loser.

SAFETY GUARANTEES (must never violate):
  - Only REDUCES confidence — never boosts (boosts come from real trade
    win rates only).
  - Penalties are bounded: factor in [0.50, 1.00], duration <= 30 days.
  - Apply phase is independent from backtest run — if backtest fails,
    nothing changes. If apply fails partway, the writes are independent.
  - Every function is try/except wrapped — cannot crash trade-close,
    scheduler, or any existing endpoint.
  - clear_all_penalties() is the undo button.

INSIGHT THRESHOLDS (conservative — better to under-fix than over-fix):
  - >= 3 backtest trades AND avg_pnl_pct <= -2.0%  -> penalty 0.85
  - >= 3 backtest trades AND avg_pnl_pct <= -5.0%  -> penalty 0.70
  - >= 5 backtest trades AND win_rate < 30%        -> penalty 0.75
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Insight thresholds — tweak only with great care
MIN_TRADES_FOR_INSIGHT = 3
MILD_AVG_PNL_THRESHOLD = -2.0   # avg_pnl_pct
MILD_PENALTY = 0.85
SEVERE_AVG_PNL_THRESHOLD = -5.0
SEVERE_PENALTY = 0.70
LOW_WR_MIN_TRADES = 5
LOW_WR_THRESHOLD = 30.0
LOW_WR_PENALTY = 0.75


def extract_insights_from_backtest(bt_result: dict) -> dict:
    """Pure function — given a backtest result dict, return a list of
    proposed per-ticker penalties. Does NOT touch the database.

    Returns:
      {
        "ok": bool,
        "proposed_penalties": [
          {"ticker": "...", "penalty_factor": 0.7, "reason": "...",
           "avg_pnl_pct": -6.1, "trades": 4},
          ...
        ],
        "summary": {"total_evaluated": int, "flagged": int}
      }
    """
    try:
        if not bt_result or not bt_result.get("ok"):
            return {"ok": False, "reason": "no_valid_backtest_result",
                    "proposed_penalties": []}

        per_ticker = (bt_result.get("results") or {}).get("per_ticker") or {}
        if not per_ticker:
            return {"ok": False, "reason": "backtest_missing_per_ticker",
                    "proposed_penalties": []}

        proposed = []
        for ticker, stats in per_ticker.items():
            try:
                trades = int(stats.get("trades") or 0)
                avg_pnl = float(stats.get("avg_pnl_pct") or 0)
                wr = float(stats.get("win_rate_pct") or 0)

                if trades < MIN_TRADES_FOR_INSIGHT:
                    continue  # not enough data — no fix

                # Tier 1: severe negative avg pnl
                if avg_pnl <= SEVERE_AVG_PNL_THRESHOLD:
                    proposed.append({
                        "ticker": ticker,
                        "penalty_factor": SEVERE_PENALTY,
                        "reason": f"backtest avg_pnl {avg_pnl:.2f}% over {trades} trades (severe)",
                        "avg_pnl_pct": avg_pnl,
                        "trades": trades,
                        "win_rate_pct": wr,
                    })
                    continue

                # Tier 2: mild negative avg pnl
                if avg_pnl <= MILD_AVG_PNL_THRESHOLD:
                    proposed.append({
                        "ticker": ticker,
                        "penalty_factor": MILD_PENALTY,
                        "reason": f"backtest avg_pnl {avg_pnl:.2f}% over {trades} trades (mild)",
                        "avg_pnl_pct": avg_pnl,
                        "trades": trades,
                        "win_rate_pct": wr,
                    })
                    continue

                # Tier 3: low win rate even if avg_pnl not severe
                if trades >= LOW_WR_MIN_TRADES and wr < LOW_WR_THRESHOLD:
                    proposed.append({
                        "ticker": ticker,
                        "penalty_factor": LOW_WR_PENALTY,
                        "reason": f"backtest wr {wr:.1f}% over {trades} trades (low)",
                        "avg_pnl_pct": avg_pnl,
                        "trades": trades,
                        "win_rate_pct": wr,
                    })
            except Exception as _e:
                logger.debug(f"insight extract soft-fail {ticker}: {_e}")
                continue

        # Sort worst first (lowest penalty factor = strongest reduction)
        proposed.sort(key=lambda p: p["penalty_factor"])
        return {
            "ok": True,
            "proposed_penalties": proposed,
            "summary": {
                "total_evaluated": len(per_ticker),
                "flagged": len(proposed),
            },
        }
    except Exception as e:
        logger.warning(f"extract_insights_from_backtest fail: {e}")
        return {"ok": False, "reason": str(e)[:200],
                "proposed_penalties": []}


def apply_safe_fixes(insights: dict) -> dict:
    """Apply the proposed penalties to the stock_learning store.
    Returns a per-ticker apply summary. Soft-fails individual tickers
    so a single failure does not block the rest."""
    try:
        if not insights or not insights.get("ok"):
            return {"ok": False, "reason": "invalid_insights", "applied": []}
        proposed = insights.get("proposed_penalties") or []
        if not proposed:
            return {"ok": True, "applied": [], "skipped": 0,
                    "note": "no tickers met fix thresholds"}

        from predictions.stock_learning import set_backtest_penalty
        applied = []
        skipped = 0
        for p in proposed:
            try:
                r = set_backtest_penalty(
                    ticker=p["ticker"],
                    penalty_factor=p["penalty_factor"],
                    reason=p.get("reason", ""),
                    avg_pnl_pct=p.get("avg_pnl_pct"),
                    trades=p.get("trades"),
                    ttl_days=30,
                )
                if r.get("ok"):
                    applied.append({
                        "ticker": p["ticker"],
                        "penalty_factor": r["penalty_factor"],
                        "expires_at": r["expires_at"],
                        "reason": p.get("reason"),
                    })
                else:
                    skipped += 1
            except Exception as _e:
                logger.debug(f"apply_safe_fixes per-ticker fail {p.get('ticker')}: {_e}")
                skipped += 1
        return {"ok": True, "applied": applied, "applied_count": len(applied),
                "skipped": skipped}
    except Exception as e:
        logger.warning(f"apply_safe_fixes fail: {e}")
        return {"ok": False, "reason": str(e)[:200], "applied": []}


def run_feedback_loop(days: int = 180,
                       top_n: int = 10,
                       hold_days: int = 5,
                       apply: bool = True) -> dict:
    """Full closed-loop: run backtest → extract insights → optionally apply
    them as per-ticker penalties → return a complete report.

    Set apply=False to preview the recommended fixes WITHOUT writing them.
    NEVER raises — every failure mode returns ok=False with a reason.
    """
    try:
        from predictions.backtest import run_backtest
        from datetime import datetime as _dt, timedelta as _td

        end = _dt.utcnow().date().isoformat()
        start = (_dt.utcnow() - _td(days=int(days))).date().isoformat()

        bt = run_backtest(start_date=start, end_date=end,
                          top_n=int(top_n), hold_days=int(hold_days))
        if not bt.get("ok"):
            return {"ok": False, "phase": "backtest",
                    "reason": bt.get("reason", "backtest_failed")}

        insights = extract_insights_from_backtest(bt)
        applied_report = None
        if apply and insights.get("ok"):
            applied_report = apply_safe_fixes(insights)

        return {
            "ok": True,
            "ran_at": datetime.utcnow().isoformat(),
            "config": {"days": days, "top_n": top_n,
                       "hold_days": hold_days, "apply": apply},
            "backtest_summary": {
                "total_return_pct": bt["results"].get("total_return_pct"),
                "sp500_return_pct": bt["results"].get("sp500_return_pct"),
                "alpha_vs_sp500_pct": bt["results"].get("alpha_vs_sp500_pct"),
                "sharpe": bt["results"].get("sharpe_ratio"),
                "win_rate_pct": bt["results"].get("win_rate_pct"),
                "total_trades": bt["results"].get("total_trades"),
                "tickers_count": bt["config"].get("tickers_count"),
            },
            "insights": insights,
            "applied": applied_report,
        }
    except Exception as e:
        logger.warning(f"run_feedback_loop fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}
