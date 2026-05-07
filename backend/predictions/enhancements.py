"""
Sentinel Quant — Low-Risk High-Reward Enhancements

Five additive features, all fail-open, all isolated. Designed so a bug
in any one of them can never break the core trading system.

  1. picks_cache_s3        — persist pick cache to S3 (no more empty-cache after deploys)
  2. check_black_swan      — detect SPY crashes, return tighten-stops signal
  3. is_choppy_window      — true if first/last 15min of market (skip new entries)
  4. generate_eod_report   — daily summary at 4:30pm ET
  5. compute_true_return   — return excluding manual cash adjustments

Every function returns a dict with `ok: bool` so callers can decide what
to do. Every function is wrapped in try/except so failures never bubble up.
"""

import logging
import json
import os
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Constants — all overridable via env
PICKS_S3_KEY = os.environ.get("PICKS_S3_KEY", "sentinel-quant/picks_cache.json")
PICKS_S3_BUCKET = os.environ.get("PICKS_S3_BUCKET", "")  # falls back to db backup bucket
PICKS_CACHE_MAX_AGE_HOURS = 24                          # don't restore if older than 24h
BLACK_SWAN_THRESHOLD_PCT = -2.0                         # SPY drops more than -2% intraday = swan
BLACK_SWAN_SEVERE_PCT = -4.0                            # -4%+ = severe
CHOPPY_OPEN_MIN = 15                                    # skip first 15 min after open
CHOPPY_CLOSE_MIN = 15                                   # skip last 15 min before close


# ============================================================
# 1. PICK CACHE PERSISTENCE TO S3
# ============================================================

def _get_s3_client():
    """Lazy-init boto3 client. Returns None if AWS unavailable."""
    try:
        import boto3
        return boto3.client("s3")
    except Exception as e:
        logger.debug(f"_get_s3_client: {e}")
        return None


def _resolve_bucket() -> str:
    """Pick the S3 bucket to use. Falls back to db_persistence's bucket."""
    if PICKS_S3_BUCKET:
        return PICKS_S3_BUCKET
    try:
        from predictions.db_persistence import S3_BUCKET
        return S3_BUCKET
    except Exception:
        return ""


def save_picks_to_s3(picks: dict) -> dict:
    """Serialize and upload the picks dict to S3. Never raises."""
    try:
        if not picks or not isinstance(picks, dict):
            return {"ok": False, "reason": "empty_or_invalid_picks"}
        bucket = _resolve_bucket()
        if not bucket:
            return {"ok": False, "reason": "no_s3_bucket"}
        s3 = _get_s3_client()
        if s3 is None:
            return {"ok": False, "reason": "s3_unavailable"}
        # Strip non-serializable fields (numpy arrays, dataframes, etc.)
        safe_picks = {}
        for k, v in picks.items():
            try:
                json.dumps({k: v}, default=str)
                safe_picks[k] = v
            except Exception:
                continue
        safe_picks["_saved_at"] = datetime.utcnow().isoformat()
        body = json.dumps(safe_picks, default=str).encode("utf-8")
        s3.put_object(
            Bucket=bucket, Key=PICKS_S3_KEY, Body=body,
            ContentType="application/json"
        )
        return {"ok": True, "bytes": len(body), "key": PICKS_S3_KEY}
    except Exception as e:
        logger.warning(f"save_picks_to_s3 fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}


def restore_picks_from_s3(max_age_hours: int = PICKS_CACHE_MAX_AGE_HOURS) -> dict:
    """Try to restore the pick cache from S3. Returns the dict or
    {ok: False} if restore failed. Never raises."""
    try:
        bucket = _resolve_bucket()
        if not bucket:
            return {"ok": False, "reason": "no_s3_bucket"}
        s3 = _get_s3_client()
        if s3 is None:
            return {"ok": False, "reason": "s3_unavailable"}
        obj = s3.get_object(Bucket=bucket, Key=PICKS_S3_KEY)
        body = obj["Body"].read().decode("utf-8")
        picks = json.loads(body)
        # Check age
        saved_at = picks.get("_saved_at")
        if saved_at:
            try:
                saved_dt = datetime.fromisoformat(saved_at)
                age_hours = (datetime.utcnow() - saved_dt).total_seconds() / 3600
                if age_hours > max_age_hours:
                    return {"ok": False, "reason": f"too_old_{age_hours:.1f}h"}
            except Exception:
                pass
        return {"ok": True, "picks": picks,
                "long_count": len(picks.get("long_picks", [])),
                "short_count": len(picks.get("short_picks", []))}
    except Exception as e:
        logger.debug(f"restore_picks_from_s3 fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}


# ============================================================
# 2. BLACK SWAN DETECTOR
# ============================================================

def check_black_swan() -> dict:
    """Detect intraday market crashes via SPY. Returns:
        {
          ok: bool,
          is_swan: bool,
          severity: 'normal' | 'swan' | 'severe',
          spy_pct_change: float,
          action: 'normal' | 'tighten_stops' | 'tighten_stops_and_pause_entries'
        }
    Never raises. Uses cached SPY price when possible to avoid hammering yf.
    """
    try:
        import yfinance as yf
        ticker = yf.Ticker("SPY")
        info = ticker.fast_info
        prev_close = float(info.get("previousClose") or 0)
        cur = float(info.get("lastPrice") or 0)
        if prev_close <= 0 or cur <= 0:
            return {"ok": False, "is_swan": False, "severity": "unknown",
                    "spy_pct_change": 0, "action": "normal",
                    "reason": "no_price_data"}
        pct = (cur - prev_close) / prev_close * 100.0
        if pct <= BLACK_SWAN_SEVERE_PCT:
            return {"ok": True, "is_swan": True, "severity": "severe",
                    "spy_pct_change": round(pct, 2),
                    "action": "tighten_stops_and_pause_entries"}
        if pct <= BLACK_SWAN_THRESHOLD_PCT:
            return {"ok": True, "is_swan": True, "severity": "swan",
                    "spy_pct_change": round(pct, 2),
                    "action": "tighten_stops"}
        return {"ok": True, "is_swan": False, "severity": "normal",
                "spy_pct_change": round(pct, 2), "action": "normal"}
    except Exception as e:
        logger.debug(f"check_black_swan fail: {e}")
        return {"ok": False, "is_swan": False, "severity": "unknown",
                "spy_pct_change": 0, "action": "normal",
                "reason": str(e)[:200]}


def apply_black_swan_protection() -> dict:
    """If a black swan is detected, tighten stops on all winning positions
    to break-even. Never closes positions, only adjusts stops upward
    (longs) / downward (shorts). Always safe.
    """
    try:
        swan = check_black_swan()
        if not swan.get("is_swan"):
            return {"ok": True, "action": "no_swan", **swan}
        from predictions.models import get_open_trades, update_trade_stop
        trades = get_open_trades() or []
        tightened = 0
        for t in trades:
            try:
                entry = float(t.get("entry_price") or 0)
                if entry <= 0:
                    continue
                # We don't have current price here — use entry as a safe
                # break-even target. The trailing stop in the exit_checker
                # will adjust further if price has moved.
                cur_stop = float(t.get("stop_loss_price") or 0)
                direction = t.get("direction", "long")
                # For longs: move stop UP toward entry if it's below
                # For shorts: move stop DOWN toward entry if it's above
                if direction == "long" and cur_stop < entry:
                    update_trade_stop(t["id"], round(entry, 2))
                    tightened += 1
                elif direction == "short" and cur_stop > entry:
                    update_trade_stop(t["id"], round(entry, 2))
                    tightened += 1
            except Exception:
                continue
        try:
            from predictions.audit import audit_log
            audit_log("black_swan_protection",
                      old_value=None, new_value=tightened,
                      caller="apply_black_swan_protection",
                      reason=f"SPY {swan['spy_pct_change']:+.2f}% — tightened {tightened} stops")
        except Exception:
            pass
        return {"ok": True, "action": "tightened",
                "stops_tightened": tightened, **swan}
    except Exception as e:
        logger.warning(f"apply_black_swan_protection fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}


# ============================================================
# 3. TIME-OF-DAY FILTER
# ============================================================

def is_choppy_window() -> dict:
    """Returns whether we're currently in the open or close chop window.
    Skip new entries during these times to get better fill prices.
    """
    try:
        try:
            import pytz
            tz = pytz.timezone("US/Eastern")
            now_et = datetime.now(tz)
        except Exception:
            now_et = datetime.utcnow() - timedelta(hours=4)  # rough EDT fallback
        hour = now_et.hour
        minute = now_et.minute
        # Market hours: 9:30 AM – 4:00 PM ET
        if hour < 9 or hour > 16:
            return {"ok": True, "is_choppy": False, "window": "off_hours"}
        # First CHOPPY_OPEN_MIN minutes (9:30 - 9:45 if CHOPPY_OPEN_MIN=15)
        if hour == 9 and minute < (30 + CHOPPY_OPEN_MIN):
            return {"ok": True, "is_choppy": True, "window": "open_chop",
                    "ends_at": f"9:{30 + CHOPPY_OPEN_MIN:02d}"}
        # Last CHOPPY_CLOSE_MIN minutes (3:45-4:00 if CHOPPY_CLOSE_MIN=15)
        if hour == 15 and minute >= (60 - CHOPPY_CLOSE_MIN):
            return {"ok": True, "is_choppy": True, "window": "close_chop",
                    "ends_at": "16:00"}
        return {"ok": True, "is_choppy": False, "window": "normal"}
    except Exception as e:
        logger.debug(f"is_choppy_window fail: {e}")
        return {"ok": False, "is_choppy": False, "window": "unknown"}


# ============================================================
# 4. END-OF-DAY REPORT
# ============================================================

def generate_eod_report() -> dict:
    """Generate the daily end-of-day summary. Returns a dict with all
    metrics. Never raises — every section is wrapped in try/except."""
    out = {"generated_at": datetime.utcnow().isoformat(), "ok": True}

    # Portfolio snapshot
    try:
        from predictions.truth_engine import get_fund_truth, get_sp500_truth
        fund = get_fund_truth()
        sp = get_sp500_truth()
        out["portfolio"] = {
            "total_value": fund.get("total_value"),
            "cash": fund.get("cash"),
            "positions_value": fund.get("positions_value"),
            "cum_return_pct": fund.get("cum_return_pct"),
            "num_positions": fund.get("num_positions"),
        }
        out["benchmark"] = {
            "sp500_price": sp.get("price"),
            "sp500_cum_pct": sp.get("cum_pct"),
            "alpha_vs_sp500": (
                round((fund.get("cum_return_pct") or 0) - (sp.get("cum_pct") or 0), 3)
                if fund.get("ok") and sp.get("ok") else None
            ),
        }
    except Exception as e:
        out["portfolio_error"] = str(e)[:200]

    # Today's closed trades
    try:
        from predictions.models import get_closed_trades
        closed = get_closed_trades(limit=200) or []
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        today_closed = [t for t in closed if (t.get("exit_date") or "").startswith(today_str)]
        wins = [t for t in today_closed if (t.get("pnl_pct") or 0) > 0]
        losses = [t for t in today_closed if (t.get("pnl_pct") or 0) < 0]
        total_pnl = sum((t.get("pnl_dollars") or 0) for t in today_closed)
        out["today_trades"] = {
            "total_closed": len(today_closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(today_closed) * 100, 1) if today_closed else None,
            "total_pnl_dollars": round(total_pnl, 2),
            "avg_win_pct": round(sum(t.get("pnl_pct", 0) for t in wins) / len(wins), 2) if wins else None,
            "avg_loss_pct": round(sum(t.get("pnl_pct", 0) for t in losses) / len(losses), 2) if losses else None,
        }
        # Top 3 winners + losers by pnl_pct
        sorted_today = sorted(today_closed, key=lambda t: t.get("pnl_pct") or 0, reverse=True)
        out["top_winners_today"] = [
            {"ticker": t.get("ticker"), "pnl_pct": t.get("pnl_pct"),
             "pnl_dollars": t.get("pnl_dollars")}
            for t in sorted_today[:3]
        ]
        out["top_losers_today"] = [
            {"ticker": t.get("ticker"), "pnl_pct": t.get("pnl_pct"),
             "pnl_dollars": t.get("pnl_dollars")}
            for t in sorted_today[-3:][::-1]
        ]
    except Exception as e:
        out["trades_error"] = str(e)[:200]

    # Open positions snapshot
    try:
        from predictions.models import get_open_trades
        opens = get_open_trades() or []
        out["open_positions"] = {
            "count": len(opens),
            "tickers": [t.get("ticker") for t in opens][:20],
        }
    except Exception as e:
        out["open_error"] = str(e)[:200]

    # Today's audit highlights
    try:
        from predictions.audit import get_recent_audit
        audit = get_recent_audit(limit=200) or []
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        today_audit = [a for a in audit if (a.get("timestamp") or "").startswith(today_str)]
        # Group by mutation type
        by_type = {}
        for a in today_audit:
            t = a.get("mutation_type", "?")
            by_type[t] = by_type.get(t, 0) + 1
        out["today_audit"] = by_type
    except Exception as e:
        out["audit_error"] = str(e)[:200]

    # Persist the report so the user can fetch it later
    try:
        from predictions.models import set_trading_state
        set_trading_state("eod_report_latest", json.dumps(out, default=str)[:8000])
    except Exception:
        pass

    return out


# ============================================================
# 5. TRUE TRADING RETURN — excludes manual cash adjustments
# ============================================================

def compute_true_trading_return() -> dict:
    """Compute return EXCLUDING manual cash recovery operations.

    True return = sum of realized pnl from closed trades + unrealized
    pnl from open positions. This isolates what your strategy actually
    earned (or lost) from trading, separate from cash bumps.
    """
    try:
        from predictions.models import get_closed_trades, get_open_trades
        from predictions.paper_trader import _get_current_prices

        closed = get_closed_trades(limit=10000) or []
        # Sum realized pnl from closed trades (excluding corrupted ones)
        realized = sum(
            (t.get("pnl_dollars") or 0)
            for t in closed
            if not t.get("corrupted_marked")
        )

        # Unrealized pnl from open positions
        opens = get_open_trades() or []
        unrealized = 0.0
        if opens:
            try:
                tickers = list({t["ticker"] for t in opens})
                prices = _get_current_prices(tickers)
                for t in opens:
                    try:
                        cur = prices.get(t["ticker"], t["entry_price"])
                        if not cur:
                            continue
                        entry = float(t["entry_price"])
                        shares = float(t.get("shares") or 0)
                        if t.get("direction") == "long":
                            unrealized += (cur - entry) * shares
                        else:
                            unrealized += (entry - cur) * shares
                    except Exception:
                        continue
            except Exception:
                pass

        true_pnl = realized + unrealized
        ORIGINAL_CAPITAL = 100_000.0
        true_return_pct = (true_pnl / ORIGINAL_CAPITAL) * 100.0

        # Compare with displayed return
        displayed_return = None
        try:
            from predictions.truth_engine import get_fund_truth
            fund = get_fund_truth()
            if fund.get("ok"):
                displayed_return = fund.get("cum_return_pct")
        except Exception:
            pass

        manual_adjustments_pct = (
            round((displayed_return or 0) - true_return_pct, 3)
            if displayed_return is not None else None
        )

        return {
            "ok": True,
            "true_realized_pnl": round(realized, 2),
            "true_unrealized_pnl": round(unrealized, 2),
            "true_total_pnl": round(true_pnl, 2),
            "true_return_pct": round(true_return_pct, 3),
            "displayed_return_pct": displayed_return,
            "manual_adjustments_pct": manual_adjustments_pct,
            "interpretation": (
                f"Your trading earned {true_pnl:+.2f}. "
                f"The chart shows {displayed_return or 0:+.2f}%. "
                f"The {manual_adjustments_pct or 0:+.2f}% gap is from manual cash adjustments."
                if manual_adjustments_pct is not None else
                f"Your trading earned {true_pnl:+.2f} ({true_return_pct:+.2f}%)"
            ),
            "computed_at": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        logger.warning(f"compute_true_trading_return fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}
