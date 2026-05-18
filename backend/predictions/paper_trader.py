"""
Paper Trading System — the execution engine of the Sentinel Quant hedge fund.

This manages a $109K virtual portfolio that:
  1. Auto-executes trades based on quant signals (LONG/SHORT picks)
  2. Manages position sizing (uncapped — constrained by cash)
  3. Enforces risk management (6% stop-loss, target exits, time exits)
  4. Runs rapid backtesting using historical data to learn quickly
  5. Tracks portfolio performance vs S&P 500 benchmark

The goal: execute thousands of trades, track every outcome,
and feed results to the learning system so it can improve.

No real money is used. This is a paper trading simulator.
"""

import yfinance as yf
import numpy as np
import pandas as pd
import time
import json
import logging
import threading
from datetime import datetime, timedelta
from analysis.quant_engine import _throttle

logger = logging.getLogger(__name__)

# MULTI-DAY CONFIRMATION TRACKER
# For low-conviction picks (35-55%), require the signal to appear
# in 2 consecutive scans before entering. Reduces whipsaw losses.
_signal_confirmation = {}  # {"AAPL_long": {"first_seen": datetime, "scan_count": int}}
_CONFIRMATION_TTL = timedelta(hours=36)


def _check_signal_confirmation(symbol: str, direction: str, confidence: int) -> bool:
    """
    For low-conviction picks (35-45%), require 2 consecutive scans.
    Most picks (45%+) execute immediately — was blocking too many good trades.
    """
    if confidence >= 45:
        return True
    now = datetime.now()

    # Periodic cleanup: purge expired entries to prevent unbounded growth
    if len(_signal_confirmation) > 200:
        expired = [k for k, v in _signal_confirmation.items()
                   if now - v["first_seen"] > _CONFIRMATION_TTL]
        for k in expired:
            del _signal_confirmation[k]

    key = f"{symbol}_{direction}"
    if key in _signal_confirmation:
        entry = _signal_confirmation[key]
        if now - entry["first_seen"] > _CONFIRMATION_TTL:
            _signal_confirmation[key] = {"first_seen": now, "scan_count": 1}
            return False
        entry["scan_count"] += 1
        if entry["scan_count"] >= 2:
            del _signal_confirmation[key]
            return True
        return False
    else:
        _signal_confirmation[key] = {"first_seen": now, "scan_count": 1}
        return False


def _safe_col(df, col_name):
    """Extract a column from yfinance DataFrame, handling multi-level columns."""
    if df is None or df.empty:
        return pd.Series(dtype=float)
    c = df[col_name]
    if hasattr(c, "columns"):
        c = c.iloc[:, 0]
    return c


# ============================================================
#  BENCHMARK CACHE — robust S&P 500 data with last-known-good fallback
# ============================================================
# Problem: First call to /api/paper-performance often returned sp500_sharpe=0
# because yfinance was rate limited / cold. Subsequent calls still failed because
# there was no cache — every request did fresh yfinance downloads.
#
# Fix: Persistent in-memory cache + last-known-good fallback. Even if a fresh
# download fails, we return the previously-cached good result instead of zeros.
_benchmark_cache = {
    "data": None,         # Last successful benchmark dict
    "time": 0.0,          # When it was last fetched successfully
    "sp_closes": None,    # Raw S&P closes from inception window
    "sharpe_closes": None,# Raw S&P closes from 180d window for Sharpe
    "inception": None,    # Inception date used
}
_BENCHMARK_CACHE_TTL = 1800  # 30 minutes — S&P only moves so much intraday
_benchmark_lock = threading.Lock()


def _fetch_sp500_data(inception_date: str, sharpe_start: str):
    """Download S&P 500 data with retries + persistent disk cache fallback.

    Tries 3 sources in order:
      1. Live yfinance fetch (3 retries each for inception + sharpe)
      2. Last successful disk cache (~/.sp500_disk_cache.json)
      3. Returns (None, None) only if no live and no cache

    The disk cache survives container restarts so a single yfinance outage
    can never wipe out the S&P benchmark display.
    """
    import json as _json_sp
    import os as _os_sp
    sp_closes = None
    sharpe_closes = None

    # Disk cache path (persists across deploys via S3 or local volume)
    _disk_cache_path = _os_sp.path.join(_os_sp.path.dirname(__file__), ".sp500_disk_cache.json")

    try:
        for attempt in range(3):
            try:
                _throttle()
                sp_df = yf.download("^GSPC", start=inception_date, progress=False, timeout=10)
                if sp_df is not None and len(sp_df) >= 2:
                    sp_closes = _safe_col(sp_df, "Close").values.astype(float).flatten()
                    sp_closes = sp_closes[~np.isnan(sp_closes)]
                    if len(sp_closes) >= 2:
                        break
                time.sleep(1.0 + attempt)  # Backoff
            except Exception as retry_err:
                logger.warning(f"S&P inception download attempt {attempt+1} failed: {retry_err}")
                time.sleep(1.0 + attempt)

        for attempt in range(3):
            try:
                _throttle()
                sharpe_df = yf.download("^GSPC", start=sharpe_start, progress=False, timeout=10)
                if sharpe_df is not None and len(sharpe_df) >= 30:
                    sharpe_closes = _safe_col(sharpe_df, "Close").values.astype(float).flatten()
                    sharpe_closes = sharpe_closes[~np.isnan(sharpe_closes)]
                    if len(sharpe_closes) >= 30:
                        break
                time.sleep(1.0 + attempt)
            except Exception as retry_err:
                logger.warning(f"S&P sharpe download attempt {attempt+1} failed: {retry_err}")
                time.sleep(1.0 + attempt)
    except Exception as e:
        logger.error(f"_fetch_sp500_data outer failure: {e}")

    # On successful fetch, save to disk cache for future fallback
    if sp_closes is not None and sharpe_closes is not None:
        try:
            with open(_disk_cache_path, "w") as _f:
                _json_sp.dump({
                    "saved_at": datetime.now().isoformat(),
                    "inception_date": inception_date,
                    "sharpe_start": sharpe_start,
                    "sp_closes": sp_closes.tolist(),
                    "sharpe_closes": sharpe_closes.tolist(),
                }, _f)
            logger.info(f"S&P disk cache updated: {len(sp_closes)} inception, {len(sharpe_closes)} sharpe")
        except Exception as cache_err:
            logger.warning(f"Could not save S&P disk cache: {cache_err}")
        return sp_closes, sharpe_closes

    # Live fetch failed — try disk cache fallback
    if sp_closes is None or sharpe_closes is None:
        try:
            if _os_sp.path.exists(_disk_cache_path):
                with open(_disk_cache_path) as _f:
                    cached = _json_sp.load(_f)
                if sp_closes is None and cached.get("sp_closes"):
                    sp_closes = np.array(cached["sp_closes"], dtype=float)
                    logger.warning(f"S&P inception: using disk cache from {cached.get('saved_at', '?')} "
                                   f"({len(sp_closes)} pts) — yfinance unavailable")
                if sharpe_closes is None and cached.get("sharpe_closes"):
                    sharpe_closes = np.array(cached["sharpe_closes"], dtype=float)
                    logger.warning(f"S&P sharpe: using disk cache from {cached.get('saved_at', '?')} "
                                   f"({len(sharpe_closes)} pts) — yfinance unavailable")
        except Exception as cache_err:
            logger.error(f"Could not read S&P disk cache fallback: {cache_err}")

    return sp_closes, sharpe_closes


def prewarm_benchmark_cache():
    """Warm up the benchmark cache on server startup so the first request is fast.
    Safe to call from a background thread — failures are logged but not raised."""
    try:
        logger.info("Pre-warming benchmark cache (S&P 500)...")
        from predictions.models import get_all_paper_trades
        # Use PERSISTENT inception date — survives portfolio resets so the S&P
        # comparison window stays meaningful across deploys.
        import os as _os_warm
        _inception_flag = _os_warm.path.join(_os_warm.path.dirname(__file__), ".fund_inception_date")
        try:
            if _os_warm.path.exists(_inception_flag):
                with open(_inception_flag) as _f:
                    inception_date = _f.read().strip()[:10]
            else:
                try:
                    all_trades = get_all_paper_trades()
                    if all_trades:
                        earliest = min(t.get("entry_date", "2026-01-01") for t in all_trades)
                        inception_date = earliest[:10]
                    else:
                        inception_date = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")
                except Exception:
                    inception_date = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")
                # Persist forever — never changes again
                try:
                    with open(_inception_flag, "w") as _f:
                        _f.write(inception_date)
                except Exception:
                    pass
        except Exception:
            inception_date = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")

        sharpe_start = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")
        sp_closes, sharpe_closes = _fetch_sp500_data(inception_date, sharpe_start)

        if sp_closes is not None and len(sp_closes) >= 2:
            with _benchmark_lock:
                _benchmark_cache["sp_closes"] = sp_closes
                _benchmark_cache["sharpe_closes"] = sharpe_closes
                _benchmark_cache["inception"] = inception_date
                _benchmark_cache["time"] = time.time()
            logger.info(f"Benchmark cache warmed: {len(sp_closes)} inception pts, {len(sharpe_closes) if sharpe_closes is not None else 0} sharpe pts")
        else:
            logger.warning("Benchmark pre-warm got no data — will retry on first request")
    except Exception as e:
        logger.error(f"prewarm_benchmark_cache failed: {e}")


# Portfolio configuration
ORIGINAL_CAPITAL = 100_000.0  # What we ACTUALLY started with — used for total return calculation
INITIAL_CAPITAL = 109_000.0   # Current capital level — used for cash init if no S3 restore
MAX_POSITIONS = 999  # No limit — only constrained by cash
STOP_LOSS_PCT = 0.05  # Default fallback — overridden by per-stock ATR calculation
DEFAULT_HOLD_DAYS = 30

# ============================================================
#  CAPITAL PRESERVATION MODE — activated when portfolio is up big
# ============================================================
# The system checks total return and automatically shifts to
# conservative trading when gains exceed expectations.
# This prevents giving back hard-won profits.
#
# When total return >= PRESERVATION_THRESHOLD:
#   - Position sizes shrink to 3% (from 6%)
#   - Entry filters tighten (confidence 55+, score 3.0+)
#   - New trade activity reduced by 75%
#   - Profits locked earlier (10% instead of 15%)
#   - Flat trades cut faster (40% hold time instead of 50%)
#
# Week 2: Aggressive mode — no preservation, maximize alpha
# Set to False to disable capital preservation and trade aggressively
PRESERVATION_ENABLED = False
PRESERVATION_THRESHOLD = 8.0  # Only matters when PRESERVATION_ENABLED = True

def _is_preservation_mode() -> bool:
    """Check if we should be in capital preservation mode based on total return."""
    if not PRESERVATION_ENABLED:
        return False  # Aggressive mode — preservation disabled
    try:
        from predictions.models import get_cash, get_open_trades
        cash = get_cash()
        open_trades = get_open_trades()
        # Quick estimate of portfolio value
        positions_val = sum(t.get("entry_price", 0) * t.get("shares", 0) for t in open_trades)
        total_val = cash + positions_val
        total_return = ((total_val / ORIGINAL_CAPITAL) - 1) * 100
        return total_return >= PRESERVATION_THRESHOLD
    except Exception:
        return True  # Default to cautious if we can't check


# ============================================================
#  LIVE TRADING SAFETY MODE — activated by env var for real-money IBKR
# ============================================================
# When LIVE_TRADING_SAFETY_MODE=true (set in App Runner env), the system
# uses conservative-but-not-paranoid limits suitable for first weeks of
# live trading.  "Just right" — not so safe that nothing fires, not so
# loose that one bad day wipes 10%.  Layered on top of all existing
# sentinels (circuit breaker, snapshot drift, daily pause).
#
# Effects when active:
#   - Position size 4% (vs 8% paper)
#   - Min confidence 50 (vs 40 paper)
#   - Min composite score 2.5 (vs 2.0 paper)
#   - Gross exposure cap 50% (vs 80% paper) — see _get_dynamic_exposure_cap
#   - Auto-pause at -2% daily loss (existing drawdown halt logic)
#
# Override individual limits via env if needed:
#   LIVE_POSITION_SIZE_PCT, LIVE_MIN_CONFIDENCE, LIVE_MIN_SCORE,
#   LIVE_MAX_GROSS_EXPOSURE
def _is_live_safety_mode() -> bool:
    """Check if LIVE_TRADING_SAFETY_MODE is enabled.  Cached after first
    read — never raises."""
    try:
        import os as _os_ls
        return _os_ls.environ.get("LIVE_TRADING_SAFETY_MODE", "").lower() in ("true", "1", "yes")
    except Exception:
        return False


def _live_safety_float(env_name: str, default: float) -> float:
    """Read a float override from env, fall back to default on any error."""
    try:
        import os as _os_lsf
        v = _os_lsf.environ.get(env_name, "").strip()
        if v:
            return float(v)
    except Exception:
        pass
    return default


def _get_position_size_pct() -> float:
    """Dynamic position sizing: larger when aggressive, smaller in
    preservation or live safety mode.

    LOOSENED 2026-05-16: safety mode was too strict (only 4 of 28 picks
    qualified). New "just right" calibration: 5% (vs paper 8%, vs old
    safety 4%) — still half-risk vs paper, allows ~20 positions max."""
    if _is_live_safety_mode():
        # 2026-05-18: bumped to 9% so 60-70% gross is hit with as few as
        # 7-8 positions firing (not just 10). With 10 positions × 9% × 0.90
        # floor = 81% (capped at 70%). With 7 positions × 9% × 0.90 = 56%.
        return _live_safety_float("LIVE_POSITION_SIZE_PCT", 0.09)
    if _is_preservation_mode():
        return 0.03  # 3% per position — half size in preservation mode
    return 0.08  # 8% per position — aggressive conviction sizing


# Auto-tune confidence floor based on recent win rate ───────────────────
# Reads rolling 30-day win rate; nudges threshold up/down within bounds.
# SOFTENED 2026-05-17: previous +10 max shift fully cancelled the safety
# mode loosening (45 + 10 = 55 = back to original strict).  New cap +5
# so loosened thresholds always retain some pick headroom even during
# losing streaks.
#  Win rate < 35% → +5  (selective protection, not crushing)
#  Win rate 35-50% → +3 (mild caution)
#  Win rate 50-55% → 0  (baseline)
#  Win rate > 55% → -3  (slightly looser, we're winning)
# Bounded at [base, base+5] so it can never overrule the loosening.
_AUTOTUNE_CACHE = {"shift": 0, "ts": 0, "win_rate": None}
_AUTOTUNE_TTL = 3600  # recompute hourly

def _get_autotune_conf_shift() -> int:
    """Compute confidence shift based on rolling 30-day win rate.
    Returns int in [-3, +5].  Cached 1 hour to avoid hammering DB."""
    try:
        import time as _t
        if _AUTOTUNE_CACHE["ts"] and (_t.time() - _AUTOTUNE_CACHE["ts"]) < _AUTOTUNE_TTL:
            return _AUTOTUNE_CACHE["shift"]
        from predictions.models import get_db
        from datetime import datetime as _dt, timedelta as _td
        cutoff = (_dt.utcnow() - _td(days=30)).isoformat()
        conn = get_db()
        try:
            row = conn.execute(
                """SELECT
                     SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) AS wins,
                     COUNT(*) AS total
                   FROM paper_trades
                   WHERE status='closed' AND exit_date >= ?""",
                (cutoff,)
            ).fetchone()
        finally:
            conn.close()
        total = int(row["total"] or 0)
        wins = int(row["wins"] or 0)
        if total < 20:   # too few trades — don't auto-tune
            shift = 0
            win_rate = None
        else:
            win_rate = (wins / total) * 100
            if win_rate < 35:
                shift = 5
            elif win_rate < 50:
                shift = 3
            elif win_rate < 55:
                shift = 0
            else:
                shift = -3
        _AUTOTUNE_CACHE.update({"shift": shift, "ts": _t.time(), "win_rate": win_rate})
        return shift
    except Exception:
        return 0   # fail-open to baseline


def _get_min_confidence() -> int:
    """Dynamic confidence filter — sweet spot 40 (was 35 too loose,
    50 too strict). Wide pick funnel + asymmetric R:R + win-lock +
    loss-cut do the quality work. Don't make the entry filter so
    strict that the book ever sits at 0 picks.

    AUTO-TUNE 2026-05-16: applies rolling 30-day win-rate-driven shift
    on top of the base threshold.  Clamped to [base-3, base+5]."""
    if _is_live_safety_mode():
        base = int(_live_safety_float("LIVE_MIN_CONFIDENCE", 45))
    elif _is_preservation_mode():
        base = 50
    else:
        base = 40
    # Apply auto-tune shift, clamped: -3 (winning streak) to +5 (losing)
    shift = _get_autotune_conf_shift()
    final = max(base - 3, min(base + 5, base + shift))
    return final

def _get_min_composite_score() -> float:
    """Dynamic score filter — sweet spot 2.0 (was 1.5 too loose,
    2.5 too strict).

    LOOSENED 2026-05-16: safety mode now uses 2.0 (vs old 2.5).  The
    confidence filter does most quality work; piling on score floor too
    high crushes pick count without much loss-prevention upside."""
    if _is_live_safety_mode():
        return _live_safety_float("LIVE_MIN_SCORE", 2.0)
    if _is_preservation_mode():
        return 3.0
    return 2.0

POSITION_SIZE_PCT = 0.06  # Default — overridden by _get_position_size_pct() at trade time
MIN_CONFIDENCE = 40  # Default — overridden by _get_min_confidence() at trade time


# ============================================================
#  DYNAMIC EXPOSURE CONTROLLER (the "thinking" layer)
# ============================================================
# Auto-adjusts the gross-exposure cap on every trade cycle so the user
# never has to manually say "increase exposure" or "decrease exposure".
# The system reads market conditions and its own recent performance, then
# decides how aggressive to be. Result is clamped to safety bounds so it
# can never hit 100% (always keeps a cash buffer) and never collapses
# below the minimum trading level.

DYNAMIC_EXPOSURE_MIN = 0.65  # Hard floor — always trade at least 65%
DYNAMIC_EXPOSURE_MAX = 0.80  # Hard ceiling — 70-80% deployed in good markets (raised from 0.70)
DYNAMIC_EXPOSURE_BASE = 0.75  # Starting point — targets 25% cash buffer (raised from 0.65)
# Position-size floor: the product of all 11 sizing multipliers cannot
# crush a trade below this fraction of nominal. Without this, multiplier
# stacking (~0.7^11) was reducing trades to ~2% of intended size,
# leaving the portfolio at 1.6% gross exposure when target was 65%+.
# RAISED 2026-05-18: 0.50 was still letting positions shrink to ~1% of
# nominal in production (see Monday open: 5 positions × $1,200 = 4.8%
# gross instead of ~50%).  New floor 0.90 means multiplier stacking can
# at most shrink a position by 10% — ensures actual size ≈ target size.
POSITION_SIZE_MULT_FLOOR = 0.90
DYNAMIC_EXPOSURE_SAFE_DEFAULT = 0.60  # Used if calculation fails


# ============================================================
#  MACRO EVENT REACTOR — TACO TRADE / SHOCK REVERSAL DETECTOR
# ============================================================
# Recognizes patterns where market panic on policy/political news tends
# to REVERSE rather than persist (the "TACO" pattern: Trump Always
# Chickens Out — tariff threat -> sell-off -> threat softened -> rally).
#
# Same dynamic for: Fed jawboning, OPEC threats, sanctions deadlines,
# ultimatum-style geopolitical announcements. The signal is "panic
# without fundamental damage." When detected:
#   - boost LONG confidence on quality names (+10)
#   - veto NEW SHORTS in safe-haven sectors (Tech, Healthcare, Financials)
#   - bias exposure higher (+0.05 on dynamic exposure target)
#
# Detection logic:
#   1. Active geo event with type in TACO_REVERSAL_EVENT_TYPES
#   2. AND VIX showing stress (>20) but not crisis (<35)
#   3. Returns boolean + reason string for transparency

TACO_REVERSAL_EVENT_TYPES = {
    "tariff", "sanction", "ultimatum", "deadline", "threat",
    "trade war", "negotiation", "summit",
}

TACO_PROTECTED_SECTORS = {
    "Technology", "Healthcare", "Financials", "Consumer Discretionary"
}


def _detect_taco_reversal_event(quant_picks: dict) -> dict:
    """Detect a TACO/shock-reversal signal from active macro state.

    Reads:
      - quant_picks.regime.vix_level (current VIX)
      - DB: get_active_geo_events() (active policy/political events)
      - quant_picks.macro.ceasefire_ending_overlay (existing flag)

    Returns dict:
      active (bool): is the TACO signal firing?
      reason (str): human-readable explanation
      confidence_boost (int): how much to bump long confidence (0-15)
      veto_shorts_in (set): sectors where new shorts should be vetoed
      exposure_boost (float): adjustment to dynamic exposure target

    SAFETY: never raises, always returns a dict with active=False on error.
    """
    try:
        result = {
            "active": False,
            "reason": "no_signal",
            "confidence_boost": 0,
            "veto_shorts_in": set(),
            "exposure_boost": 0.0,
        }

        # Get VIX
        try:
            vix = float(quant_picks.get("regime", {}).get("vix_level") or 0)
        except Exception:
            vix = 0

        # Need stress but not crisis to call it TACO
        if not (20 <= vix <= 35):
            return result

        # Look for active reversal-pattern events in DB
        try:
            from predictions.models import get_active_geo_events
            active_events = get_active_geo_events() or []
        except Exception:
            active_events = []

        # Match against TACO event types
        matching = []
        for ev in active_events:
            ev_type = (ev.get("event_type") or "").lower()
            ev_desc = (ev.get("description") or "").lower()
            ev_headline = (ev.get("source_headline") or "").lower()
            for taco_type in TACO_REVERSAL_EVENT_TYPES:
                if taco_type in ev_type or taco_type in ev_desc or taco_type in ev_headline:
                    matching.append({
                        "event_key": ev.get("event_key"),
                        "type": ev_type,
                        "match": taco_type,
                    })
                    break

        # Also check macro overlay flags
        macro = quant_picks.get("macro") or {}
        ceasefire_ending = bool(macro.get("ceasefire_ending_overlay"))

        if matching or ceasefire_ending:
            triggers = []
            if matching:
                triggers.append(f"{len(matching)} TACO-pattern event(s): {[m['match'] for m in matching][:3]}")
            if ceasefire_ending:
                triggers.append("ceasefire_ending overlay active")
            result.update({
                "active": True,
                "reason": f"TACO REVERSAL detected — {' + '.join(triggers)} (VIX {vix:.1f} = stress, not crisis)",
                "confidence_boost": 10,
                "veto_shorts_in": set(TACO_PROTECTED_SECTORS),
                "exposure_boost": 0.05,
            })
        return result
    except Exception as _e:
        logger.warning(f"_detect_taco_reversal_event error (returning inactive): {_e}")
        return {
            "active": False,
            "reason": f"detector_error: {_e}",
            "confidence_boost": 0,
            "veto_shorts_in": set(),
            "exposure_boost": 0.0,
        }


def _compute_dynamic_exposure_target(vix_level=None, drawdown_pct=None) -> dict:
    """Compute target gross exposure (0.50 - 0.95) from market conditions.

    Inputs (all optional — function returns safe default if anything missing):
      vix_level: current VIX from regime detection
      drawdown_pct: current portfolio return (from peak)

    Reads from DB internally:
      - Recent 30-day Sharpe ratio (from closed trades)
      - Days since last losing day (from closed trades)

    Returns dict with:
      target (float): exposure cap to use (0.50 - 0.95)
      base (float): starting value before adjustments
      adjustments (list): factor-by-factor reasoning for transparency
      reasoning (str): one-line summary

    SAFETY: Never returns target outside [DYNAMIC_EXPOSURE_MIN, MAX].
    Wrapped in try/except — any error returns DYNAMIC_EXPOSURE_SAFE_DEFAULT.
    """
    try:
        target = DYNAMIC_EXPOSURE_BASE
        adjustments = []
        closed_trades_list = []  # initialize so days_since_loss check can access it safely

        # ----- Factor 1: Recent 30-day Sharpe -----
        # High Sharpe = system is reading the market well = press the bet
        # Low/negative Sharpe = system is wrong = pull back
        recent_sharpe = None
        try:
            from predictions.models import get_closed_trades
            from datetime import timedelta as _td_dyn
            closed_trades_list = get_closed_trades(limit=200) or []
            if len(closed_trades_list) >= 10:
                cutoff = datetime.now() - _td_dyn(days=30)
                recent = []
                for t in closed_trades_list:
                    try:
                        exit_d = datetime.fromisoformat(t.get("exit_date", ""))
                        if exit_d >= cutoff:
                            recent.append(t)
                    except Exception:
                        continue
                if len(recent) >= 5:
                    rets = [float(t.get("pnl_pct", 0) or 0) for t in recent]
                    mean_r = float(np.mean(rets))
                    std_r = float(np.std(rets)) or 1.0
                    # Annualized Sharpe approximation (assume ~10 trades/wk)
                    recent_sharpe = (mean_r / std_r) * np.sqrt(40)
        except Exception:
            recent_sharpe = None

        if recent_sharpe is not None:
            if recent_sharpe >= 2.0:
                target += 0.08
                adjustments.append(f"sharpe={recent_sharpe:.2f} (hot) +0.08")
            elif recent_sharpe >= 1.0:
                target += 0.04
                adjustments.append(f"sharpe={recent_sharpe:.2f} (good) +0.04")
            elif recent_sharpe < 0:
                target -= 0.15
                adjustments.append(f"sharpe={recent_sharpe:.2f} (negative) -0.15")
            elif recent_sharpe < 0.5:
                target -= 0.05
                adjustments.append(f"sharpe={recent_sharpe:.2f} (weak) -0.05")
            else:
                adjustments.append(f"sharpe={recent_sharpe:.2f} (neutral) +0.00")
        else:
            adjustments.append("sharpe=insufficient_data +0.00")

        # ----- Factor 2: VIX level -----
        # Low VIX = calm market = safer to deploy more
        # High VIX = panic = pull back hard
        if vix_level is not None and 0 < float(vix_level) < 100:
            v = float(vix_level)
            if v < 13:
                target += 0.05
                adjustments.append(f"vix={v:.1f} (calm) +0.05")
            elif v < 18:
                target += 0.02
                adjustments.append(f"vix={v:.1f} (normal) +0.02")
            elif v < 25:
                adjustments.append(f"vix={v:.1f} (elevated) +0.00")
            elif v < 35:
                target -= 0.10
                adjustments.append(f"vix={v:.1f} (high) -0.10")
            else:
                target -= 0.20
                adjustments.append(f"vix={v:.1f} (crisis) -0.20")
        else:
            adjustments.append("vix=unavailable +0.00")

        # ----- Factor 3: Days since last loss -----
        # Recent loss = system might be off — be more cautious
        # No loss in a while = ride the streak
        days_since_loss = None
        try:
            if closed_trades_list:
                losses = [t for t in closed_trades_list if (t.get("pnl_pct") or 0) < 0]
                if losses:
                    losses.sort(key=lambda t: t.get("exit_date", "") or "", reverse=True)
                    last_loss_date_str = losses[0].get("exit_date", "")
                    if last_loss_date_str:
                        last_loss_date = datetime.fromisoformat(last_loss_date_str)
                        days_since_loss = (datetime.now() - last_loss_date).days
        except Exception:
            days_since_loss = None

        if days_since_loss is not None:
            if days_since_loss == 0:
                target -= 0.05
                adjustments.append(f"loss_today -0.05")
            elif days_since_loss <= 1:
                target -= 0.02
                adjustments.append(f"loss_yesterday -0.02")
            elif days_since_loss >= 7:
                target += 0.03
                adjustments.append(f"days_since_loss={days_since_loss} +0.03")
            else:
                adjustments.append(f"days_since_loss={days_since_loss} +0.00")
        else:
            adjustments.append("loss_history=unavailable +0.00")

        # ----- Factor 4: Portfolio drawdown -----
        # Deeper drawdown = pull exposure way back to preserve remaining capital
        if drawdown_pct is not None:
            d = float(drawdown_pct)
            # drawdown_pct is computed elsewhere as total_return_now
            # If positive, we're up — no drawdown adjustment.
            # If negative, we're in drawdown.
            if d <= -10:
                target -= 0.25
                adjustments.append(f"drawdown={d:.1f}% (deep) -0.25")
            elif d <= -5:
                target -= 0.10
                adjustments.append(f"drawdown={d:.1f}% (mild) -0.10")
            elif d <= -2:
                target -= 0.03
                adjustments.append(f"drawdown={d:.1f}% (small) -0.03")
            else:
                adjustments.append(f"return={d:+.1f}% (no drawdown) +0.00")
        else:
            adjustments.append("drawdown=unavailable +0.00")

        # ----- Factor 5: Cross-asset macro regime modifier -----
        # The macro engine returns an exposure_modifier in [0.5, 1.2] based
        # on yield curve, credit stress, VIX term structure, dollar moves,
        # and global equity flows. We apply it MULTIPLICATIVELY so it can
        # only amplify or dampen — the absolute clamp below still applies.
        # SAFE: any failure leaves target unchanged.
        try:
            from analysis.cross_asset_macro import get_macro_signals as _gms
            macro = _gms()
            mod = macro.get("exposure_modifier")
            regime = macro.get("macro_regime", "?")
            if mod is not None and 0.4 < float(mod) < 1.5:
                old = target
                target = float(target) * float(mod)
                adjustments.append(
                    f"macro={regime} mod={float(mod):.2f} ({old:.2f}->{target:.2f})"
                )
            else:
                adjustments.append("macro=neutral mod=1.00")
        except Exception as _macro_err:
            adjustments.append(f"macro=unavailable +0.00")

        # ----- Clamp to safety bounds -----
        target = max(DYNAMIC_EXPOSURE_MIN, min(DYNAMIC_EXPOSURE_MAX, target))
        target = round(target, 3)

        # LIVE TRADING SAFETY MODE: cap exposure at 65% (loosened from 50%)
        # Applied AFTER normal clamp so safety mode is the final word.
        if _is_live_safety_mode():
            _live_cap = _live_safety_float("LIVE_MAX_GROSS_EXPOSURE", 0.70)
            if target > _live_cap:
                adjustments.append(
                    f"LIVE_SAFETY_CAP {target:.2f} -> {_live_cap:.2f}"
                )
                target = round(_live_cap, 3)

        return {
            "target": target,
            "base": DYNAMIC_EXPOSURE_BASE,
            "adjustments": adjustments,
            "reasoning": " | ".join(adjustments),
            "live_safety_mode": _is_live_safety_mode(),
        }
    except Exception as _e:
        logger.warning(f"_compute_dynamic_exposure_target error (using safe default): {_e}")
        return {
            "target": DYNAMIC_EXPOSURE_SAFE_DEFAULT,
            "base": DYNAMIC_EXPOSURE_BASE,
            "adjustments": [f"ERROR: {_e}"],
            "reasoning": f"computation_failed_safe_default={DYNAMIC_EXPOSURE_SAFE_DEFAULT}",
        }
MIN_COMPOSITE_SCORE = 2.0  # Default — overridden by _get_min_composite_score() at trade time


def _dedupe_contradictory_picks(quant_picks: dict) -> dict:
    """Remove tickers that appear in BOTH long_picks and short_picks.

    The picks engine occasionally returns the same ticker on both sides
    (we saw RBLX with conf=91% appearing as both long and short). That's
    a contradictory signal — the engine has no clear edge.

    Resolution rules:
      - If LONG conf > SHORT conf → keep LONG, drop SHORT
      - If SHORT conf > LONG conf → keep SHORT, drop LONG
      - If conf TIES → drop from BOTH (no clear edge, safest path)

    Returns NEW dict (does not mutate input). Adds "_dedup_log" field
    listing each contradiction resolved. Never raises — returns the
    original picks unchanged on any error.
    """
    try:
        longs = list(quant_picks.get("long_picks", []) or [])
        shorts = list(quant_picks.get("short_picks", []) or [])
        long_syms = {p.get("symbol"): p for p in longs if p.get("symbol")}
        short_syms = {p.get("symbol"): p for p in shorts if p.get("symbol")}
        contradictions = set(long_syms.keys()) & set(short_syms.keys())
        if not contradictions:
            return quant_picks

        drop_long = set()
        drop_short = set()
        dedup_log = []
        for sym in contradictions:
            lconf = long_syms[sym].get("confidence", 0) or 0
            sconf = short_syms[sym].get("confidence", 0) or 0
            if lconf > sconf:
                drop_short.add(sym)
                dedup_log.append(f"{sym}: keep LONG ({lconf}%) drop SHORT ({sconf}%)")
            elif sconf > lconf:
                drop_long.add(sym)
                dedup_log.append(f"{sym}: keep SHORT ({sconf}%) drop LONG ({lconf}%)")
            else:
                drop_long.add(sym)
                drop_short.add(sym)
                dedup_log.append(f"{sym}: TIE ({lconf}%) — drop from BOTH (no edge)")

        new_picks = dict(quant_picks)
        new_picks["long_picks"] = [p for p in longs if p.get("symbol") not in drop_long]
        new_picks["short_picks"] = [p for p in shorts if p.get("symbol") not in drop_short]
        new_picks["_dedup_log"] = dedup_log
        logger.warning(f"PICK DEDUP: removed {len(contradictions)} contradictions: {dedup_log[:3]}")
        return new_picks
    except Exception as _e:
        logger.warning(f"_dedupe_contradictory_picks error (returning original): {_e}")
        return quant_picks


def _auto_tighten_stops(open_trades: list, drawdown_pct) -> dict:
    """Auto-tighten stop losses on open positions during portfolio drawdown.

    Ratchet ONE DIRECTION ONLY (tighter, never looser). Never relaxes
    an existing stop. Skips options trades (different stop scale).

    Drawdown thresholds (drawdown_pct = total return from inception):
      drawdown > -3%: no action (not in real drawdown)
      -5% to -3%: tighten 30% of way toward current price
      -10% to -5%: tighten 50% of way toward current price
      -10%+: tighten 70% of way toward current price (emergency)

    Returns dict: {tightened: int, details: list}.
    Never raises — per-trade try/except + outer try/except.
    """
    result = {"tightened": 0, "details": [], "severity": "none"}
    try:
        if drawdown_pct is None:
            return result
        d = float(drawdown_pct)
        if d > -3:
            return result  # not really in drawdown

        if d <= -10:
            factor = 0.70
            severity = "emergency"
        elif d <= -5:
            factor = 0.50
            severity = "high"
        else:
            factor = 0.30
            severity = "mild"
        result["severity"] = severity

        if not open_trades:
            return result
        symbols = list({t["ticker"] for t in open_trades})
        try:
            prices = _get_current_prices(symbols)
        except Exception:
            prices = {}

        from predictions.models import get_db
        conn = get_db()
        try:
            for trade in open_trades:
                try:
                    instrument_type = trade.get("instrument_type") or "equity"
                    if instrument_type in ("call", "put"):
                        continue  # options stops use premium scale, skip
                    ticker = trade.get("ticker")
                    cur_price = prices.get(ticker)
                    if cur_price is None or cur_price <= 0:
                        continue
                    entry = trade.get("entry_price")
                    direction = trade.get("direction")
                    existing_stop = trade.get("stop_loss_price") or 0
                    if entry is None or entry <= 0:
                        continue

                    if direction == "long":
                        if existing_stop <= 0:
                            new_stop = entry * (1 - 0.05 * (1 - factor))
                        else:
                            new_stop = existing_stop + (cur_price - existing_stop) * factor
                        # Only ratchet tighter (HIGHER stop for long)
                        if existing_stop > 0 and new_stop <= existing_stop:
                            continue
                    else:  # short
                        if existing_stop <= 0:
                            new_stop = entry * (1 + 0.05 * (1 - factor))
                        else:
                            new_stop = existing_stop - (existing_stop - cur_price) * factor
                        # Only ratchet tighter (LOWER stop for short)
                        if existing_stop > 0 and new_stop >= existing_stop:
                            continue

                    new_stop = round(new_stop, 2)
                    if new_stop <= 0:
                        continue
                    conn.execute(
                        "UPDATE paper_trades SET stop_loss_price = ? WHERE id = ?",
                        (new_stop, trade["id"])
                    )
                    result["tightened"] += 1
                    result["details"].append({
                        "ticker": ticker, "direction": direction,
                        "old_stop": round(existing_stop, 2),
                        "new_stop": new_stop,
                        "current_price": round(cur_price, 2),
                    })
                except Exception as _ie:
                    logger.debug(f"_auto_tighten_stops: skip trade {trade.get('id')}: {_ie}")
                    continue
            conn.commit()
        finally:
            conn.close()

        if result["tightened"] > 0:
            logger.warning(
                f"AUTO STOP TIGHTEN ({severity}, drawdown {d:+.1f}%): "
                f"tightened {result['tightened']} stop(s) by factor {factor}"
            )
        return result
    except Exception as _e:
        logger.warning(f"_auto_tighten_stops error: {_e}")
        return result


# ============================================================
#  KELLY CRITERION POSITION SIZING
# ============================================================
# Optimal bet sizing: f* = (p * b - q) / b
# where p = win probability, b = avg_win / avg_loss, q = 1 - p
# Uses half-Kelly (0.5x) for safety. Clamps 2%-12%.
# This replaces fixed 6-8% sizing — allocates more to high-edge trades.

def _kelly_position_size(confidence, composite_score, sector, regime, direction, vix_level=None):
    """
    Calculate Kelly Criterion position size based on historical edge.

    Returns float: fraction of portfolio to allocate (0.02 to 0.12)
    """
    try:
        from predictions.models import get_closed_trades
        closed = get_closed_trades(limit=200)
    except Exception:
        closed = []

    # Need at least 20 trades to calculate meaningful statistics
    if len(closed) < 20:
        return _get_position_size_pct()  # Fall back to fixed sizing

    # Filter trades by direction for directional edge
    dir_trades = [t for t in closed if t.get("direction") == direction]
    if len(dir_trades) < 10:
        dir_trades = closed  # Use all trades if not enough directional data

    wins = [t for t in dir_trades if (t.get("pnl_pct") or 0) > 0]
    losses = [t for t in dir_trades if (t.get("pnl_pct") or 0) <= 0]

    if not wins or not losses:
        return _get_position_size_pct()

    # Win probability (p) and loss probability (q)
    p = len(wins) / len(dir_trades)
    q = 1 - p

    # Average win and average loss magnitudes
    avg_win = np.mean([abs(t.get("pnl_pct", 0) or 0) for t in wins])
    avg_loss = np.mean([abs(t.get("pnl_pct", 0) or 0) for t in losses])

    if avg_loss == 0:
        avg_loss = 1.0  # Prevent division by zero

    # Payoff ratio (b)
    b = avg_win / avg_loss
    if b <= 0:
        return 0.02  # No edge — use minimum size

    # Kelly fraction: f* = (p * b - q) / b
    kelly_full = (p * b - q) / b

    # If Kelly is negative, the edge is negative — use minimum size
    if kelly_full <= 0:
        return 0.02

    # Half-Kelly for safety (institutional standard)
    kelly_half = kelly_full * 0.5

    # Adjust by confidence — higher confidence = closer to Kelly
    # Low confidence (35%) -> 60% of Kelly, high confidence (90%) -> 120% of Kelly
    conf_multiplier = 0.4 + (confidence / 100) * 0.8
    kelly_adjusted = kelly_half * conf_multiplier

    # Adjust by composite score magnitude
    score_boost = min(0.02, abs(composite_score) * 0.003)  # up to 2% boost for strong scores
    kelly_adjusted += score_boost

    # Regime adjustment
    if regime == "BEAR" and direction == "long":
        kelly_adjusted *= 0.7  # Less sizing for longs in bear
    elif regime == "BULL" and direction == "long":
        kelly_adjusted *= 1.1  # Slight boost for longs in bull

    # Regime-aware Kelly clamps — tighter in dangerous markets
    if regime == "BEAR":
        kelly_adjusted = max(0.02, min(0.08, kelly_adjusted))  # Max 8% in bear
    elif regime == "VOLATILE":
        kelly_adjusted = max(0.02, min(0.06, kelly_adjusted))  # Max 6% in volatile
    else:
        kelly_adjusted = max(0.02, min(0.12, kelly_adjusted))  # Normal 12% cap

    # VIX override: high VIX caps Kelly regardless of regime
    if vix_level is not None and vix_level > 25:
        kelly_adjusted = min(0.06, kelly_adjusted)  # Max 6% when VIX > 25

    logger.debug(f"KELLY: p={p:.2f} b={b:.2f} full={kelly_full:.3f} half={kelly_half:.3f} "
                 f"adj={kelly_adjusted:.3f} conf={confidence}")

    return round(kelly_adjusted, 4)


# ============================================================
#  SMART ORDER TIMING
# ============================================================
# First 15 min = noise. Power hour (3-4 PM) = institutional flow.
# Avoid bad entry timing, prefer high-quality windows.

def _is_good_entry_time(force_market_open: bool = False, force_anytime: bool = False):
    """
    Check current ET time and classify the trading window.

    Args:
        force_market_open: If True, allows trading during the 9:30-9:45 avoid window.
            Used when MARKET OPEN trigger fires so we capture the open instead of
            waiting until 9:45. Weekend and off-hours blocks are NEVER overridden.
        force_anytime: If True, bypasses ALL time gates including weekend and
            off-hours. Used by the admin force-trade-now endpoint for paper-trading
            weekend gap bets. Higher confidence threshold (+10) and smaller size
            (0.5x) to compensate for stale/illiquid pricing data.

    Returns dict with:
        can_trade (bool): whether to allow new entries
        window (str): current window classification
        size_modifier (float): position size multiplier
        confidence_shift (int): adjustment to confidence threshold
    """
    try:
        import pytz
        et = datetime.now(pytz.timezone("US/Eastern"))
        hour, minute = et.hour, et.minute
        t = hour * 60 + minute  # minutes since midnight

        # WEEKEND CHECK: No trading on Saturday/Sunday — prices are stale
        # OVERRIDABLE only via force_anytime (admin force-trade-now)
        if et.weekday() >= 5:  # 5=Saturday, 6=Sunday
            if force_anytime:
                return {"can_trade": True, "window": "weekend_force", "size_modifier": 0.5, "confidence_shift": 10}
            return {"can_trade": False, "window": "weekend", "size_modifier": 0.0, "confidence_shift": 0}

        market_open = 9 * 60 + 30   # 9:30 AM
        avoid_end = 9 * 60 + 45     # 9:45 AM
        caution_end = 10 * 60 + 30  # 10:30 AM
        power_start = 15 * 60       # 3:00 PM
        market_close = 16 * 60      # 4:00 PM

        if t < market_open or t >= market_close:
            # Outside market hours — prices are stale.
            # OVERRIDABLE only via force_anytime (paper-trading weekend gap bet).
            if force_anytime:
                return {"can_trade": True, "window": "off_hours_force", "size_modifier": 0.5, "confidence_shift": 10}
            return {"can_trade": False, "window": "off_hours", "size_modifier": 0.0, "confidence_shift": 0}
        elif t < avoid_end:
            # First 15 minutes — normally avoid new entries (noise, spread is wide)
            # BUT: if MARKET OPEN trigger fires, we want to capture the open, not wait.
            # Use smaller size and higher confidence requirement to compensate for noise.
            if force_market_open:
                return {"can_trade": True, "window": "market_open_force", "size_modifier": 0.6, "confidence_shift": 5}
            return {"can_trade": False, "window": "avoid", "size_modifier": 0.0, "confidence_shift": 0}
        elif t < caution_end:
            # 9:45-10:30 — caution zone, reduce size
            return {"can_trade": True, "window": "caution", "size_modifier": 0.7, "confidence_shift": 5}
        elif t >= power_start:
            # 3:00-4:00 PM — power hour, best institutional flow
            return {"can_trade": True, "window": "power_hour", "size_modifier": 1.1, "confidence_shift": -5}
        else:
            # 10:30-3:00 PM — normal trading window
            return {"can_trade": True, "window": "normal", "size_modifier": 1.0, "confidence_shift": 0}
    except Exception:
        return {"can_trade": True, "window": "unknown", "size_modifier": 1.0, "confidence_shift": 0}


# ============================================================
#  ADAPTIVE STREAK CALIBRATION
# ============================================================
# When on a winning streak, press harder. Losing streak, tighten up.
# Acts within 3-5 trades — faster than the weekly learner cycle.

def _get_streak_calibration():
    """
    Analyze recent trade streak and return sizing/confidence adjustments.

    Returns dict with:
        streak_type (str): 'win', 'loss', or 'mixed'
        streak_length (int): consecutive wins or losses
        size_multiplier (float): position size multiplier
        confidence_shift (int): adjustment to min confidence threshold
    """
    try:
        from predictions.models import get_closed_trades
        recent = get_closed_trades(limit=10)
    except Exception:
        return {"streak_type": "mixed", "streak_length": 0, "size_multiplier": 1.0, "confidence_shift": 0}

    if len(recent) < 3:
        return {"streak_type": "mixed", "streak_length": 0, "size_multiplier": 1.0, "confidence_shift": 0}

    # Count consecutive wins/losses from most recent
    streak = 0
    streak_type = "win" if (recent[0].get("pnl_pct", 0) or 0) > 0 else "loss"

    for t in recent:
        pnl = t.get("pnl_pct", 0) or 0
        if streak_type == "win" and pnl > 0:
            streak += 1
        elif streak_type == "loss" and pnl <= 0:
            streak += 1
        else:
            break

    # Calibration based on streak
    if streak_type == "win":
        if streak >= 8:
            return {"streak_type": "win", "streak_length": streak,
                    "size_multiplier": 1.25, "confidence_shift": -8,
                    "sector_penalties": _get_sector_streak_penalties(recent)}
        elif streak >= 5:
            return {"streak_type": "win", "streak_length": streak,
                    "size_multiplier": 1.15, "confidence_shift": -5,
                    "sector_penalties": _get_sector_streak_penalties(recent)}
        else:
            return {"streak_type": "win", "streak_length": streak,
                    "size_multiplier": 1.0, "confidence_shift": 0,
                    "sector_penalties": _get_sector_streak_penalties(recent)}
    else:  # loss
        if streak >= 5:
            return {"streak_type": "loss", "streak_length": streak,
                    "size_multiplier": 0.50, "confidence_shift": 15,
                    "sector_penalties": _get_sector_streak_penalties(recent)}
        elif streak >= 3:
            return {"streak_type": "loss", "streak_length": streak,
                    "size_multiplier": 0.75, "confidence_shift": 10,
                    "sector_penalties": _get_sector_streak_penalties(recent)}
        else:
            return {"streak_type": "loss", "streak_length": streak,
                    "size_multiplier": 1.0, "confidence_shift": 0,
                    "sector_penalties": _get_sector_streak_penalties(recent)}


def _get_sector_streak_penalties(recent_trades: list) -> dict:
    """
    Track per-sector streaks (losses AND wins).
    3+ consecutive losses → 0.50x size (penalty)
    3+ consecutive wins → confidence boost for next pick in that sector
    Returns dict of {sector: {"size_multiplier": float, "confidence_boost": int}}.
    """
    sector_results = {}
    for t in recent_trades:
        sector = t.get("sector", "Unknown")
        pnl = t.get("pnl_pct", 0) or 0
        if sector not in sector_results:
            sector_results[sector] = []
        sector_results[sector].append(pnl)

    adjustments = {}
    for sector, pnls in sector_results.items():
        consecutive_losses = 0
        consecutive_wins = 0
        for p in pnls:
            if p <= 0:
                if consecutive_wins > 0:
                    break
                consecutive_losses += 1
            else:
                if consecutive_losses > 0:
                    break
                consecutive_wins += 1

        adj = {"size_multiplier": 1.0, "confidence_boost": 0}
        if consecutive_losses >= 3:
            adj["size_multiplier"] = 0.50
        elif consecutive_losses >= 2:
            adj["size_multiplier"] = 0.75

        if consecutive_wins >= 5:
            adj["confidence_boost"] = 12
        elif consecutive_wins >= 3:
            adj["confidence_boost"] = 8

        if adj["size_multiplier"] != 1.0 or adj["confidence_boost"] != 0:
            adjustments[sector] = adj

    return adjustments


# ============================================================
#  AUTONOMOUS DECISION ENGINE — Per-Stock Stop Loss & Profit Targets
# ============================================================
# Instead of fixed 5% stop loss for every stock, the machine calculates
# the right stop loss for each stock based on:
#   1. ATR (Average True Range) — how much it ACTUALLY moves day-to-day
#   2. Signal strength — high confidence = wider stops (more room to breathe)
#   3. Regime — BEAR = tighter stops, BULL = wider stops
#   4. Stock type — volatile growth stock vs stable dividend stock
#
# This is how real quant funds do it. A 5% stop on TSLA (which moves 5%
# in a single day) is meaningless. But 5% on KO (which barely moves 1%)
# is way too wide. The machine must decide per-stock.

def _calculate_stock_atr(symbol: str, period: int = 14) -> float:
    """
    Calculate Average True Range as a percentage of price.
    ATR measures how much a stock typically moves per day.
    Returns ATR as a decimal (e.g., 0.03 = 3% daily movement).
    """
    try:
        _throttle()
        df = yf.download(symbol, period="2mo", progress=False)
        if df is None or len(df) < period + 1:
            return 0.025  # Default 2.5% if no data

        highs = _safe_col(df, "High").values.astype(float)
        lows = _safe_col(df, "Low").values.astype(float)
        closes = _safe_col(df, "Close").values.astype(float)

        # True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|)
        tr_values = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            tr_values.append(tr)

        # ATR = rolling average of True Range
        atr = float(np.mean(tr_values[-period:]))
        atr_pct = atr / closes[-1]  # Convert to percentage of current price

        return max(0.005, min(atr_pct, 0.15))  # Clamp between 0.5% and 15%
    except Exception as e:
        logger.debug(f"ATR calculation failed for {symbol}: {e}")
        return 0.025  # Default fallback


def _autonomous_stop_and_target(
    symbol: str,
    direction: str,
    price: float,
    regime: str,
    confidence: int,
    composite_score: float,
    sector: str = "",
    is_mean_reversion: bool = False,
) -> dict:
    """
    THE MACHINE DECIDES — Per-stock stop loss and profit target.

    Uses ATR (volatility), signal strength, regime, and trade type to
    calculate the optimal stop loss and take-profit for each individual stock.

    Returns dict with: stop_loss, target_price, hold_days, reasoning
    """
    atr_pct = _calculate_stock_atr(symbol)

    # --- STOP LOSS: Based on ATR ---
    # Standard: 2x ATR gives ~95% chance of NOT being stopped by noise
    # High conviction: 2.5x ATR (more room)
    # Low conviction: 1.5x ATR (tighter, less risk)
    if confidence >= 80 and abs(composite_score) >= 5:
        atr_mult_stop = 2.0  # High conviction = reasonable room (was 2.5 — too wide)
    elif confidence >= 60:
        atr_mult_stop = 1.5  # Standard (was 2.0 — tightened to cut losses faster)
    else:
        atr_mult_stop = 1.2  # Low conviction = tight stop (was 1.5 — less risk)

    # Regime adjustment
    if regime == "BEAR":
        if direction == "long":
            atr_mult_stop *= 0.8  # Tighter stops on longs in bear
        else:
            atr_mult_stop *= 1.1  # Shorts get a bit more room in bear
    elif regime == "BULL":
        if direction == "long":
            atr_mult_stop *= 1.2  # More room for longs in bull
        else:
            atr_mult_stop *= 0.8  # Tighter on shorts in bull

    # Mean reversion trades are quick — tighter stops
    if is_mean_reversion:
        atr_mult_stop *= 0.7

    stop_pct = atr_pct * atr_mult_stop

    # Hard clamp: never risk more than 7% or less than 1.5% (was 2-10% — too wide)
    stop_pct = max(0.015, min(stop_pct, 0.07))

    # --- TAKE PROFIT: ASYMMETRIC R:R for profit-factor edge ---
    # Math: even at 35% win rate, 2.7:1 R:R produces +0.75R per trade.
    # Profit factor = (win_rate * avg_win) / (loss_rate * avg_loss)
    # With 2.7:1 and 35% wr -> PF = (0.35 * 2.7) / (0.65 * 1) = 1.45
    # With 3.5:1 and 35% wr -> PF = (0.35 * 3.5) / (0.65 * 1) = 1.88
    # PROFIT-FACTOR FIX: bumped all R:R floors to 2.7+ (was 1.3-2.5)
    if confidence >= 80 and abs(composite_score) >= 5:
        rr_ratio = 4.0  # High conviction — let big winners run (was 2.5)
    elif confidence >= 60:
        rr_ratio = 3.2  # Standard — 3:1 R:R minimum (was 2.0)
    else:
        rr_ratio = 2.7  # Low conviction — still asymmetric (was 1.5)

    # Mean reversion: still asymmetric but quicker
    if is_mean_reversion:
        rr_ratio = 2.5  # was 1.3 — even MR trades need positive R:R

    target_pct = stop_pct * rr_ratio

    # Hard clamp: target between 4% and 30% (raised from 2-20% to allow
    # asymmetric R:R math to actually produce big targets)
    target_pct = max(0.04, min(target_pct, 0.30))

    # --- HOLD DURATION: Based on ATR and signal ---
    # High volatility stocks resolve faster (shorter hold)
    # Low volatility stocks need more time (longer hold)
    if is_mean_reversion:
        hold_days = max(1, min(5, int(5 / (atr_pct * 40 + 0.5))))  # 1-5 days
    elif regime == "BEAR":
        base_hold = 7 if confidence >= 70 else 3
        hold_days = max(2, min(14, base_hold))
    elif regime == "BULL":
        base_hold = 30 if confidence >= 70 else 14
        hold_days = max(7, min(60, base_hold))
    else:
        base_hold = 14 if confidence >= 70 else 7
        hold_days = max(5, min(30, base_hold))

    # --- CALCULATE ACTUAL PRICES ---
    if direction == "long":
        stop_loss = round(price * (1 - stop_pct), 2)
        target_price = round(price * (1 + target_pct), 2)
    else:  # short
        stop_loss = round(price * (1 + stop_pct), 2)
        target_price = round(price * (1 - target_pct), 2)

    # Classify hold type for selective sell logic
    if hold_days <= 1:
        hold_class = "intraday"
    elif hold_days <= 14:
        hold_class = "swing"
    else:
        hold_class = "position"

    reasoning = (
        f"ATR={atr_pct*100:.1f}%/day | "
        f"Stop={stop_pct*100:.1f}% ({atr_mult_stop:.1f}x ATR) | "
        f"Target={target_pct*100:.1f}% ({rr_ratio:.1f}:1 R:R) | "
        f"Hold={hold_days}d ({hold_class})"
    )

    logger.info(f"AUTONOMOUS DECISION {symbol} {direction}: {reasoning}")

    return {
        "stop_loss": stop_loss,
        "target_price": target_price,
        "hold_days": hold_days,
        "hold_class": hold_class,
        "stop_pct": round(stop_pct * 100, 1),
        "target_pct": round(target_pct * 100, 1),
        "atr_pct": round(atr_pct * 100, 1),
        "rr_ratio": rr_ratio,
        "reasoning": reasoning,
    }

# ============================================================
#  ADVANCED: VIX-SCALED POSITION SIZING
# ============================================================
# When VIX is high (fear), we reduce position sizes to limit risk
# When VIX is low (calm), we can be more aggressive
# This is standard institutional risk management
VIX_SIZE_SCALE = {
    "low": 1.3,      # VIX < 15: calm markets, slightly bigger positions
    "normal": 1.0,    # VIX 15-20: standard sizing
    "elevated": 0.85,  # VIX 20-25: slight reduction (was 0.7 — too timid)
    "high": 0.65,      # VIX 25-35: moderate reduction (was 0.5 — crushed trades)
    "crisis": 0.45,    # VIX > 35: reduced but still trading (was 0.25 — froze us)
}

def _get_vix_scale() -> float:
    """Get position size multiplier based on current VIX level."""
    try:
        _throttle()
        vix_df = yf.download("^VIX", period="5d", progress=False)
        if vix_df is not None and not vix_df.empty:
            vix = float(_safe_col(vix_df, "Close").dropna().iloc[-1])
            if vix < 15:
                return VIX_SIZE_SCALE["low"]
            elif vix < 20:
                return VIX_SIZE_SCALE["normal"]
            elif vix < 25:
                return VIX_SIZE_SCALE["elevated"]
            elif vix < 35:
                return VIX_SIZE_SCALE["high"]
            else:
                return VIX_SIZE_SCALE["crisis"]
    except Exception:
        pass
    return 1.0


def _per_position_quick_profit_lock(trade: dict, pnl_pct: float, vix: float = 20.0,
                                    regime: str = "SIDEWAYS") -> tuple:
    """Per-position dynamic WIN-LOCK — TRULY DYNAMIC, no hardcoded thresholds.

    Locks a winner when the gain is statistically "too fast" — meaning
    the realized profit exceeds what is expected for the holding window
    given current market volatility, the position's own risk profile,
    and the regime.

    Algorithm:

      1. EXPECTED MOVE (sigma) — derived from VIX:
            expected_daily_pct = VIX / 16   (VIX = annualized vol % * 100;
                                             dividing by 16 ≈ sqrt(252)
                                             gives 1-day expected move)
            expected_hours_pct = expected_daily_pct * sqrt(hours / 6.5)
                                 (6.5 = trading hours per day; volatility
                                 scales with sqrt(time))

      2. INSTRUMENT LEVERAGE:
            Equity:  leverage = 1.0
            Option:  leverage = 4.0      (typical OTM near-the-money delta
                                          and gamma combined produce ~4x
                                          underlying-equivalent move)

      3. RISK-BASED OVERLAY (uses the trade's own stop_loss):
            risk_pct = abs(entry - stop) / entry * 100
            If realized profit >= risk_pct * R_MULT in less than half the
            trade's expected hold window, the trade has hit its target
            faster than planned — lock it in.

      4. REGIME MULTIPLIER:
            BULL    → K_sigma = 3.5    (let winners run more in bull)
            BEAR    → K_sigma = 2.0    (lock fast in bear — reversals brutal)
            SIDEWAYS→ K_sigma = 2.5    (default — catch statistical outliers)
            R_MULT  derived similarly:  BULL=3.0, BEAR=1.5, default=2.0

    Lock fires when EITHER:
        - pnl_pct >= K_sigma * leverage * expected_hours_pct
        - pnl_pct >= R_MULT * risk_pct AND hours_held < hold_window/2

    Returns: (should_close: bool, reason: str)
    """
    try:
        import math
        from datetime import datetime as _dt

        if pnl_pct <= 0:
            return False, ""

        # 1. Hours held
        try:
            entry_dt = _dt.fromisoformat(trade.get("entry_date", ""))
            hours_held = max(0.05, (_dt.now() - entry_dt).total_seconds() / 3600.0)
        except Exception:
            return False, ""   # unknown entry time — defer to other exits

        # 2. VIX → expected move
        try:
            vix_f = float(vix or 20.0)
            if vix_f <= 0 or vix_f > 200:
                vix_f = 20.0
        except Exception:
            vix_f = 20.0
        expected_daily_pct = vix_f / 16.0                # ~1-day SPY move
        # Time-scaled expected move (volatility scales with sqrt(t))
        expected_hours_pct = expected_daily_pct * math.sqrt(max(hours_held, 0.5) / 6.5)

        # 3. Instrument leverage
        instr = (trade.get("instrument_type") or "equity").lower()
        is_option = instr in ("call", "put")
        leverage = 4.0 if is_option else 1.0

        # 4. Regime-driven sigma multiplier and R-multiple
        reg = (regime or "SIDEWAYS").upper()
        if reg == "BULL":
            k_sigma, r_mult = 3.5, 3.0
        elif reg == "BEAR":
            k_sigma, r_mult = 2.0, 1.5
        else:
            k_sigma, r_mult = 2.5, 2.0

        # 5. Statistical lock: gain > k_sigma * leverage * expected window move
        sigma_threshold = k_sigma * leverage * expected_hours_pct

        # 6. Risk-based lock: hit target faster than planned
        risk_pct = None
        risk_threshold = None
        try:
            entry_p = float(trade.get("entry_price") or 0)
            stop_p = float(trade.get("stop_loss_price") or 0)
            if entry_p > 0 and stop_p > 0:
                risk_pct = abs(entry_p - stop_p) / entry_p * 100
                risk_threshold = r_mult * risk_pct
        except Exception:
            pass
        hold_days = float(trade.get("hold_duration_days") or 5)
        hold_hours = hold_days * 24.0
        risk_window_ok = hours_held < (hold_hours / 2.0)

        # Decide
        ticker = trade.get("ticker", "?")
        kind = "OPT" if is_option else "EQT"

        if pnl_pct >= sigma_threshold:
            return True, (
                f"WIN-LOCK ({kind}): {ticker} +{pnl_pct:.1f}% in {hours_held:.1f}h "
                f">= sigma_threshold {sigma_threshold:.1f}% "
                f"(VIX={vix_f:.0f}, lev={leverage}x, k={k_sigma}, regime={reg})"
            )

        if (risk_threshold is not None and pnl_pct >= risk_threshold
                and risk_window_ok):
            return True, (
                f"WIN-LOCK ({kind}): {ticker} +{pnl_pct:.1f}% in {hours_held:.1f}h "
                f">= {r_mult:.1f}R risk_threshold {risk_threshold:.1f}% "
                f"(half hold window: {hold_hours/2:.1f}h, regime={reg})"
            )

        return False, ""
    except Exception:
        return False, ""


def _per_position_quick_loss_cut(trade: dict, pnl_pct: float, vix: float = 20.0,
                                  regime: str = "SIDEWAYS") -> tuple:
    """Per-position dynamic LOSS-CUT — mirror of _per_position_quick_profit_lock.

    Cuts a position immediately when the loss is statistically "too fast"
    for the holding window. This catches:
      - "Wrong from minute one" trades that gap against entry
      - Catalyst trades where the thesis broke fast
      - Options that decayed faster than expected

    Same dynamic algorithm as the win-lock, mirrored for losses:

      1. EXPECTED MOVE (sigma) from VIX:
            expected_daily_pct = VIX / 16
            expected_hours_pct = expected_daily_pct * sqrt(hours/6.5)

      2. INSTRUMENT LEVERAGE:
            Equity:  1.0    Option: 4.0

      3. LOSS THRESHOLD:
            cut when |loss_pct| >= K_loss * leverage * expected_hours_pct
            K_loss derived from regime (mirror of K_sigma):
              BULL    K_loss = 2.5  (less likely to revert, cut early)
              BEAR    K_loss = 1.8  (very fast cut — bear losses snowball)
              SIDEWAYS K_loss = 2.2

      4. RISK-MULTIPLE CUT:
            If loss exceeds 1.0R (full original stop distance) within
            half the planned hold window, exit. The regular stop-loss
            line will catch it eventually but this is FASTER.

    Returns: (should_close: bool, reason: str)

    SAFETY: only acts on NEGATIVE pnl_pct. Returns (False, "") on any
    error so a bug here can never trigger spurious exits.
    """
    try:
        import math
        from datetime import datetime as _dt

        # Only act on losses
        if pnl_pct >= 0:
            return False, ""

        loss_pct = abs(pnl_pct)

        # Hours held
        try:
            entry_dt = _dt.fromisoformat(trade.get("entry_date", ""))
            hours_held = max(0.05, (_dt.now() - entry_dt).total_seconds() / 3600.0)
        except Exception:
            return False, ""

        # DELIBERATE GUARD: never cut a position in the first 30 minutes.
        # That window is dominated by entry slippage, bid/ask noise, and
        # normal price discovery — not signal. Only after 30 min does a
        # loss become statistically meaningful.
        if hours_held < 0.5:
            return False, ""

        # VIX → expected move (volatility scales with sqrt(time))
        try:
            vix_f = float(vix or 20.0)
            if vix_f <= 0 or vix_f > 200:
                vix_f = 20.0
        except Exception:
            vix_f = 20.0
        expected_daily_pct = vix_f / 16.0
        expected_hours_pct = expected_daily_pct * math.sqrt(max(hours_held, 0.5) / 6.5)

        # Instrument leverage
        instr = (trade.get("instrument_type") or "equity").lower()
        is_option = instr in ("call", "put")
        leverage = 4.0 if is_option else 1.0

        # ============================================================
        # DYNAMIC, SELF-THINKING K_loss
        # ============================================================
        # Starts from a regime baseline, then adjusts based on 5 LIVE
        # signals. Each signal nudges the engine to be more or less
        # patient based on real conditions, not a hardcoded value.
        #
        # Higher k_loss = more patient (waits longer before cutting)
        # Lower  k_loss = more aggressive (cuts losses faster)
        reg = (regime or "SIDEWAYS").upper()
        if reg == "BULL":
            k_loss = 2.5
            r_cut = 1.0
        elif reg == "BEAR":
            k_loss = 2.0
            r_cut = 0.85
        else:
            k_loss = 2.3
            r_cut = 0.95

        signal_notes = []

        # SIGNAL 1: Recent overall win rate
        # Losing streak means our edge is dulled — cut losses faster
        try:
            from predictions.models import get_closed_trades
            recent = get_closed_trades(limit=20) or []
            if len(recent) >= 5:
                wins = sum(1 for t in recent if (t.get("pnl_pct") or 0) > 0)
                wr = wins / len(recent)
                if wr < 0.30:
                    k_loss *= 0.85
                    signal_notes.append(f"low_wr={wr:.0%}")
                elif wr > 0.55:
                    k_loss *= 1.10
                    signal_notes.append(f"hot_wr={wr:.0%}")
        except Exception:
            pass

        # SIGNAL 2: Portfolio drawdown — cut faster when bleeding
        try:
            from predictions.models import get_portfolio_snapshots
            snaps = get_portfolio_snapshots(days=30) or []
            if len(snaps) >= 5:
                vals = [float(s.get("total_value") or 0) for s in snaps if s.get("total_value")]
                if vals:
                    peak = max(vals)
                    cur = vals[-1]
                    dd = (peak - cur) / peak if peak > 0 else 0
                    if dd > 0.08:
                        k_loss *= 0.85
                        signal_notes.append(f"drawdown={dd*100:.1f}%")
                    elif dd < 0.02:
                        k_loss *= 1.05
                        signal_notes.append(f"shallow_dd={dd*100:.1f}%")
        except Exception:
            pass

        # SIGNAL 3: Same-sector recent losses — cut faster on weak sectors
        try:
            from predictions.models import get_closed_trades
            trade_sector = trade.get("sector", "")
            if trade_sector and trade_sector != "Unknown":
                recent = get_closed_trades(limit=30) or []
                sector_recent = [t for t in recent if t.get("sector") == trade_sector]
                if len(sector_recent) >= 3:
                    sector_losses = sum(1 for t in sector_recent
                                        if (t.get("pnl_pct") or 0) < 0)
                    sector_loss_rate = sector_losses / len(sector_recent)
                    if sector_loss_rate > 0.6:
                        k_loss *= 0.88
                        signal_notes.append(f"weak_sector={trade_sector[:10]}")
        except Exception:
            pass

        # SIGNAL 4: VIX rising — cut faster in deteriorating conditions
        try:
            if vix_f >= 25:
                k_loss *= 0.92
                signal_notes.append(f"high_vix={vix_f:.0f}")
        except Exception:
            pass

        # SIGNAL 5: Trade is already past half its planned hold window
        # AND still losing — thesis isn't playing out, cut faster
        try:
            hold_days_p = float(trade.get("hold_duration_days") or 5)
            if hours_held > (hold_days_p * 24 * 0.5):
                k_loss *= 0.85
                signal_notes.append(f"past_half_window")
        except Exception:
            pass

        # Hard bounds so the multiplier stack can't make k_loss insane
        k_loss = max(1.0, min(4.0, k_loss))

        # Statistical cut threshold
        sigma_threshold = k_loss * leverage * expected_hours_pct

        # Risk-multiple cut threshold (uses trade's own stop distance)
        risk_pct = None
        risk_threshold = None
        try:
            entry_p = float(trade.get("entry_price") or 0)
            stop_p = float(trade.get("stop_loss_price") or 0)
            if entry_p > 0 and stop_p > 0:
                risk_pct = abs(entry_p - stop_p) / entry_p * 100
                risk_threshold = r_cut * risk_pct
        except Exception:
            pass
        hold_days = float(trade.get("hold_duration_days") or 5)
        hold_hours = hold_days * 24.0
        risk_window_ok = hours_held < (hold_hours / 2.0)

        ticker = trade.get("ticker", "?")
        kind = "OPT" if is_option else "EQT"

        # Statistical cut — loss is statistically extreme for the window
        if loss_pct >= sigma_threshold:
            return True, (
                f"LOSS-CUT ({kind}): {ticker} -{loss_pct:.1f}% in {hours_held:.1f}h "
                f">= sigma_cut {sigma_threshold:.1f}% "
                f"(VIX={vix_f:.0f}, lev={leverage}x, k={k_loss}, regime={reg})"
            )

        # Risk-multiple cut — exceeded fraction of stop distance fast
        if (risk_threshold is not None and loss_pct >= risk_threshold
                and risk_window_ok):
            return True, (
                f"LOSS-CUT ({kind}): {ticker} -{loss_pct:.1f}% in {hours_held:.1f}h "
                f">= {r_cut:.2f}R cut_threshold {risk_threshold:.1f}% "
                f"(half hold window: {hold_hours/2:.1f}h, regime={reg})"
            )

        return False, ""
    except Exception:
        return False, ""


def _cached_vix_for_winlock() -> float:
    """Light VIX getter for the per-position win-lock. Falls back to 20
    on any error so the lock still works. Uses a 5-minute process-local
    cache to avoid hammering yfinance on every exit cycle."""
    global _vix_winlock_cache
    try:
        import time as _t
        now = _t.time()
        if "_vix_winlock_cache" not in globals():
            _vix_winlock_cache = {"vix": 20.0, "ts": 0}
        if (now - _vix_winlock_cache.get("ts", 0)) < 300:
            return _vix_winlock_cache.get("vix", 20.0)
        _throttle()
        df = yf.download("^VIX", period="2d", progress=False)
        if df is not None and not df.empty:
            v = float(_safe_col(df, "Close").dropna().iloc[-1])
            if 0 < v < 200:
                _vix_winlock_cache = {"vix": v, "ts": now}
                return v
    except Exception:
        pass
    return 20.0


def _get_dynamic_winlock(regime: str = "SIDEWAYS") -> dict:
    """
    Dynamic WIN-LOCK: the system decides its own profit-lock threshold
    based on VIX level and market regime. No hardcoded values.

    High VIX / BEAR → lock early (take what you can get)
    Low VIX / BULL  → let winners run (big catalyst days need room)
    """
    try:
        _throttle()
        vix_df = yf.download("^VIX", period="5d", progress=False)
        vix = float(_safe_col(vix_df, "Close").dropna().iloc[-1]) if vix_df is not None and not vix_df.empty else 20
    except Exception:
        vix = 20

    # Base threshold from VIX
    if vix >= 35:
        # Crisis mode — grab any win, market could reverse hard
        lock_pct = 1.5
        caution_pct = 1.0
        reason = f"VIX={vix:.0f} CRISIS — lock gains early"
    elif vix >= 25:
        lock_pct = 2.5
        caution_pct = 1.5
        reason = f"VIX={vix:.0f} HIGH — moderate lock"
    elif vix >= 20:
        lock_pct = 3.5
        caution_pct = 2.5
        reason = f"VIX={vix:.0f} ELEVATED — standard lock"
    elif vix >= 15:
        lock_pct = 5.0
        caution_pct = 3.0
        reason = f"VIX={vix:.0f} NORMAL — let winners run"
    else:
        lock_pct = 6.0
        caution_pct = 4.0
        reason = f"VIX={vix:.0f} CALM — maximum room to run"

    # Regime adjustment
    if regime == "BULL":
        lock_pct *= 1.2   # Let it run more in bull
        caution_pct *= 1.2
        reason += " | BULL regime +20%"
    elif regime == "BEAR":
        lock_pct *= 0.7   # Take profits faster in bear
        caution_pct *= 0.7
        reason += " | BEAR regime -30%"

    return {
        "lock_pct": round(lock_pct, 1),
        "caution_pct": round(caution_pct, 1),
        "vix": round(vix, 1),
        "regime": regime,
        "reason": reason,
    }


# ============================================================
#  ADVANCED: CORRELATION-BASED DIVERSIFICATION
# ============================================================
def _check_correlation(new_symbol: str, open_tickers: set, price_data: dict = None) -> dict:
    """
    Check if a new position would be too correlated with existing positions.
    Returns correlation info and whether the trade should be blocked.

    Correlation > 0.80 in same direction = too similar, skip
    This prevents holding GOOGL + META + NFLX all at once (highly correlated)
    """
    result = {"correlated": False, "max_corr": 0, "correlated_with": None}

    if not open_tickers or len(open_tickers) < 2:
        return result

    try:
        check_symbols = list(open_tickers) + [new_symbol]
        _throttle()
        df = yf.download(check_symbols, period="3mo", progress=False, group_by="ticker")
        if df is None or df.empty:
            return result

        # Extract close prices for each symbol
        close_data = {}
        for sym in check_symbols:
            try:
                if isinstance(df.columns, pd.MultiIndex):
                    if sym in df.columns.get_level_values(0):
                        close_series = df[(sym, "Close")].dropna()
                        if len(close_series) >= 20:
                            close_data[sym] = close_series.pct_change().dropna().values
                elif len(check_symbols) == 1:
                    close_series = df["Close"].dropna()
                    if len(close_series) >= 20:
                        close_data[sym] = close_series.pct_change().dropna().values
            except Exception:
                continue

        if new_symbol not in close_data:
            return result

        new_returns = close_data[new_symbol]
        max_corr = 0
        corr_ticker = None

        for sym, returns in close_data.items():
            if sym == new_symbol:
                continue
            # Align lengths
            min_len = min(len(new_returns), len(returns))
            if min_len < 15:
                continue
            corr = float(np.corrcoef(new_returns[:min_len], returns[:min_len])[0, 1])
            if abs(corr) > abs(max_corr):
                max_corr = corr
                corr_ticker = sym

        result["max_corr"] = round(max_corr, 3)
        result["correlated_with"] = corr_ticker

        # Dynamic threshold using crisis correlations from 50-year history
        # If two stocks become highly correlated during crashes, use a tighter threshold
        corr_threshold = 0.70  # Loosened from 0.45 — was blocking too many good trades
        try:
            from analysis.historical_calibration import get_calibration
            cal = get_calibration()
            crisis_corr = cal.get("crisis_correlations", {})
            if new_symbol in crisis_corr and corr_ticker:
                crisis_pairs = crisis_corr[new_symbol].get("crisis_pairs", {})
                if corr_ticker in crisis_pairs and crisis_pairs[corr_ticker] > 0.80:
                    corr_threshold = 0.55  # Loosened from 0.35 — still careful for crisis pairs
                    logger.info(f"CRISIS CORR: {new_symbol}↔{corr_ticker} crisis_corr={crisis_pairs[corr_ticker]:.2f} → threshold tightened to 0.35")
        except Exception:
            pass

        if max_corr > corr_threshold:
            result["correlated"] = True

    except Exception as e:
        logger.debug(f"Correlation check failed for {new_symbol}: {e}")

    return result


# ============================================================
#  PORTFOLIO STATE
# ============================================================

# Cache portfolio state for 15 seconds so both charts show identical values
_portfolio_cache = {"state": None, "timestamp": 0}
_CACHE_TTL = 15  # seconds

def get_portfolio_state() -> dict:
    """
    Get current portfolio state: cash, positions, total value.
    Cash comes from paper_cash table — updated ATOMICALLY with every trade.
    Cached for 15 seconds so all API endpoints show identical numbers.
    """
    global _portfolio_cache
    now = time.time()
    if _portfolio_cache["state"] and (now - _portfolio_cache["timestamp"]) < _CACHE_TTL:
        return _portfolio_cache["state"]

    from predictions.models import get_open_trades, get_closed_trades, get_cash

    open_trades = get_open_trades()
    closed_trades = get_closed_trades(limit=500)

    # Cash from atomic paper_cash table — ALWAYS accurate
    cash = get_cash()

    # Get current prices for open positions
    positions = []
    positions_value = 0
    if open_trades:
        symbols = list(set(t["ticker"] for t in open_trades))
        current_prices = _get_current_prices(symbols)

        for trade in open_trades:
            # PER-TRADE TRY/EXCEPT: a single corrupted trade row can never
            # crash the entire portfolio endpoint. Skip the bad trade, log,
            # continue with the rest. This is what kept the API down after
            # the dup consolidation — line 1064 referenced undefined `symbol`
            # for options trades, crashing the whole call.
            try:
                ticker = trade["ticker"]
                current_price = current_prices.get(ticker, trade["entry_price"])
                entry_price = trade["entry_price"]
                shares = trade["shares"]
                direction = trade["direction"]
                instrument_type = trade.get("instrument_type") or "equity"

                # Sanity guards — skip trades with critical None/zero values
                if entry_price is None or entry_price <= 0:
                    logger.warning(f"PORTFOLIO: skip trade {trade.get('id')} — bad entry_price={entry_price}")
                    continue
                if shares is None:
                    logger.warning(f"PORTFOLIO: skip trade {trade.get('id')} — shares is None")
                    continue
                if current_price is None or current_price <= 0:
                    current_price = entry_price  # fallback

                if instrument_type in ("call", "put"):
                    entry_premium = trade.get("premium_per_contract") or entry_price
                    num_contracts = trade.get("contracts") or shares
                    strike = trade.get("strike_price", 0)

                    # ============================================================
                    # OPTIONS PNL — try LIVE premium first, fallback to estimate
                    # ============================================================
                    # The previous estimate-only path was wrong in two places:
                    #   1. underlying_price_at_entry fell back to entry_price,
                    #      but entry_price for an option is the PREMIUM, not
                    #      the stock price -> garbage entry_intrinsic
                    #   2. linear time_decay_factor (dte/hold_days) was too
                    #      aggressive — assumed 50% decay just halfway through
                    # Now we fetch the real market premium first; only fall
                    # back to estimate if the chain fetch fails.
                    est_premium = None
                    _premium_source = "estimate"
                    try:
                        from predictions.options_engine import get_current_premium
                        live_prem = get_current_premium(
                            ticker, strike,
                            trade.get("expiration_date", ""),
                            instrument_type
                        )
                        if live_prem and live_prem > 0:
                            est_premium = float(live_prem)
                            _premium_source = "live_chain"
                    except Exception:
                        pass

                    if est_premium is None:
                        # Fallback estimate — fixed math + sqrt time decay
                        if instrument_type == "call":
                            intrinsic = max(0, current_price - strike) if strike else 0
                        else:
                            intrinsic = max(0, strike - current_price) if strike else 0

                        # Use stored underlying_at_entry, fall back to STRIKE
                        # (better proxy than premium when missing)
                        ul_at_entry = trade.get("underlying_price_at_entry")
                        if ul_at_entry is None or ul_at_entry <= 0:
                            ul_at_entry = strike if strike else current_price

                        if strike:
                            if instrument_type == "call":
                                entry_intrinsic = max(0, ul_at_entry - strike)
                            else:
                                entry_intrinsic = max(0, strike - ul_at_entry)
                        else:
                            entry_intrinsic = 0
                        entry_time_value = max(0, entry_premium - entry_intrinsic)

                        # DTE
                        dte_est = 14
                        if trade.get("expiration_date"):
                            try:
                                exp = datetime.strptime(trade["expiration_date"], "%Y-%m-%d")
                                dte_est = max(0, (exp - datetime.now()).days)
                            except Exception:
                                pass

                        # SQRT time decay (Black-Scholes-ish): vol scales
                        # with sqrt(t), not linear. A 30-day option at 15
                        # DTE should retain ~71% of time value (sqrt(15/30)),
                        # not 50%. Fixes the over-aggressive decay that
                        # showed TXN call at -50% when in reality it's
                        # closer to break-even.
                        import math as _math
                        try:
                            entry_dt = datetime.fromisoformat(trade.get("entry_date", ""))
                            total_life_days = max(1, dte_est + max(0, (datetime.now() - entry_dt).days))
                        except Exception:
                            total_life_days = max(dte_est, 30)
                        time_decay_factor = max(0.10, min(1.0, _math.sqrt(dte_est / total_life_days)))
                        remaining_time_value = entry_time_value * time_decay_factor
                        est_premium = max(intrinsic + remaining_time_value,
                                          entry_premium * 0.10)  # floor 10% of entry
                        # CLAMP: the estimate cannot move pnl by >70% from
                        # entry without a real chain fetch. Prevents the
                        # display from showing scary numbers like -50% when
                        # the chain just isn't loading.
                        est_premium = max(entry_premium * 0.30,
                                          min(entry_premium * 3.0, est_premium))

                    position_value = max(0, est_premium * num_contracts * 100)
                    if direction == "long":
                        unrealized_pnl = (est_premium - entry_premium) * num_contracts * 100
                        unrealized_pct = ((est_premium / entry_premium) - 1) * 100 if entry_premium > 0 else 0
                    else:
                        unrealized_pnl = (entry_premium - est_premium) * num_contracts * 100
                        unrealized_pct = ((entry_premium / est_premium) - 1) * 100 if est_premium > 0 else 0
                    positions_value += position_value
                else:
                    if direction == "long":
                        unrealized_pnl = (current_price - entry_price) * shares
                        unrealized_pct = ((current_price / entry_price) - 1) * 100
                    else:
                        unrealized_pnl = (entry_price - current_price) * shares
                        unrealized_pct = ((entry_price / current_price) - 1) * 100
                    position_value = abs(shares * current_price)
                    positions_value += position_value

                try:
                    entry_date = datetime.fromisoformat(trade["entry_date"])
                    days_held = (datetime.now() - entry_date).days
                except Exception:
                    days_held = 0

                dte = None
                if instrument_type in ("call", "put") and trade.get("expiration_date"):
                    try:
                        exp = datetime.strptime(trade["expiration_date"], "%Y-%m-%d")
                        dte = (exp - datetime.now()).days
                    except Exception:
                        pass

                pos_data = {
                    "trade_id": trade["id"],
                    "ticker": ticker,
                    "direction": direction,
                    "instrument_type": instrument_type,
                    "entry_price": entry_price,
                    "current_price": round(current_price, 2),
                    "shares": shares,
                    "position_value": round(position_value, 2),
                    "unrealized_pnl": round(unrealized_pnl, 2),
                    "unrealized_pct": round(unrealized_pct, 2),
                    "days_held": days_held,
                    "stop_loss": trade.get("stop_loss_price"),
                    "target": trade.get("target_price"),
                    "signal_score": trade.get("signal_score"),
                    "regime": trade.get("regime_at_entry"),
                    "sector": trade.get("sector"),
                }

                if instrument_type in ("call", "put"):
                    _opt_label = "CALL" if instrument_type == "call" else "PUT"
                    pos_data.update({
                        "strike_price": trade.get("strike_price"),
                        "expiration_date": trade.get("expiration_date"),
                        "contracts": trade.get("contracts"),
                        "premium": trade.get("premium_per_contract"),
                        "dte": dte,
                        "option_delta": trade.get("option_delta"),
                        "option_iv": trade.get("option_iv"),
                        "option_label": _opt_label,
                        # FIX: was `{symbol}` (undefined NameError); should be `{ticker}`
                        "display_name": f"{_opt_label} {ticker} ${trade.get('strike_price', '?')} exp {trade.get('expiration_date', '?')}",
                    })

                positions.append(pos_data)
            except Exception as _trade_err:
                logger.error(
                    f"PORTFOLIO: skip trade id={trade.get('id')} "
                    f"ticker={trade.get('ticker')} due to: {_trade_err}"
                )
                continue

    # Calculate performance metrics
    total_current = cash + positions_value
    total_return = ((total_current / ORIGINAL_CAPITAL) - 1) * 100

    # --- Exposure calculations ---
    long_value = sum(p["position_value"] for p in positions if p["direction"] == "long")
    short_value = sum(p["position_value"] for p in positions if p["direction"] == "short")
    gross_exposure = long_value + short_value
    net_exposure = long_value - short_value
    gross_exposure_pct = round((gross_exposure / total_current) * 100, 1) if total_current > 0 else 0
    net_exposure_pct = round((net_exposure / total_current) * 100, 1) if total_current > 0 else 0
    long_pct = round((long_value / total_current) * 100, 1) if total_current > 0 else 0
    short_pct = round((short_value / total_current) * 100, 1) if total_current > 0 else 0
    cash_pct = round((cash / total_current) * 100, 1) if total_current > 0 else 0

    # Win/loss stats from closed trades
    wins = [t for t in closed_trades if (t.get("pnl_pct") or 0) > 0]
    losses = [t for t in closed_trades if (t.get("pnl_pct") or 0) <= 0]
    win_rate = (len(wins) / len(closed_trades) * 100) if closed_trades else 0
    avg_win = np.mean([t["pnl_pct"] for t in wins]) if wins else 0
    avg_loss = np.mean([t["pnl_pct"] for t in losses]) if losses else 0
    profit_factor = abs(avg_win * len(wins)) / (abs(avg_loss * len(losses)) + 0.01) if losses else (99.0 if wins else 0)

    # --- Trades per day ---
    trades_per_day = 0
    first_trade_date = None
    if closed_trades:
        dates_with_trades = set()
        for t in closed_trades:
            entry_d = t.get("entry_date", "")
            if entry_d:
                try:
                    d = datetime.fromisoformat(entry_d).strftime("%Y-%m-%d")
                    dates_with_trades.add(d)
                    if first_trade_date is None or d < first_trade_date:
                        first_trade_date = d
                except Exception:
                    pass
        if first_trade_date:
            try:
                start = datetime.strptime(first_trade_date, "%Y-%m-%d")
                trading_days = max(1, np.busday_count(
                    start.date(), datetime.now().date()
                ))
                trades_per_day = round(len(closed_trades) / trading_days, 1)
            except Exception:
                trades_per_day = round(len(closed_trades) / max(1, len(dates_with_trades)), 1)

    # --- Options exposure metrics (CALLS and PUTS emphasized) ---
    options_positions = [p for p in positions if p.get("instrument_type") in ("call", "put")]
    call_positions = [p for p in positions if p.get("instrument_type") == "call"]
    put_positions = [p for p in positions if p.get("instrument_type") == "put"]
    equity_positions = [p for p in positions if p.get("instrument_type", "equity") == "equity"]
    options_premium_deployed = sum(p["position_value"] for p in options_positions)
    options_pct = round((options_premium_deployed / total_current) * 100, 1) if total_current > 0 else 0
    # Defensive: option_delta can be None in DB; .get() default only applies
    # when key is missing, not when value is explicitly None. Wrap each term.
    def _safe_delta_exposure(p):
        try:
            d = p.get("option_delta")
            if d is None:
                d = 0.5  # neutral default
            c = p.get("contracts")
            if c is None:
                c = p.get("shares") or 0
            return abs(float(d)) * float(c) * 100
        except Exception:
            return 0
    options_delta_exposure = sum(_safe_delta_exposure(p) for p in options_positions)
    calls_value = sum(p["position_value"] for p in call_positions)
    puts_value = sum(p["position_value"] for p in put_positions)

    result = {
        "total_value": round(total_current, 2),
        "cash": round(cash, 2),
        "positions_value": round(positions_value, 2),
        "total_return_pct": round(total_return, 2),
        "initial_capital": ORIGINAL_CAPITAL,
        "num_positions": len(positions),
        "num_longs": sum(1 for p in positions if p["direction"] == "long"),
        "num_shorts": sum(1 for p in positions if p["direction"] == "short"),
        "num_options": len(options_positions),
        "num_calls": len(call_positions),
        "num_puts": len(put_positions),
        "max_positions": MAX_POSITIONS,
        "exposure": {
            "long_value": round(long_value, 2),
            "short_value": round(short_value, 2),
            "gross_exposure": round(gross_exposure, 2),
            "net_exposure": round(net_exposure, 2),
            "gross_exposure_pct": gross_exposure_pct,
            "net_exposure_pct": net_exposure_pct,
            "long_pct": long_pct,
            "short_pct": short_pct,
            "cash_pct": cash_pct,
            "options_premium_deployed": round(options_premium_deployed, 2),
            "options_pct": options_pct,
            "options_delta_exposure": round(options_delta_exposure, 2),
            "calls_value": round(calls_value, 2),
            "puts_value": round(puts_value, 2),
            "calls_count": len(call_positions),
            "puts_count": len(put_positions),
        },
        "positions": positions,
        "recent_closed": [{
            "ticker": t["ticker"],
            "direction": t["direction"],
            "pnl_pct": round(t.get("pnl_pct", 0) or 0, 2),
            "pnl_dollars": round(t.get("pnl_dollars", 0) or 0, 2),
            "entry_price": round(t["entry_price"], 2),
            "exit_price": round(t.get("exit_price") or 0, 2),
        } for t in closed_trades[:10]],
        "stats": {
            "total_trades": len(closed_trades),
            "total_open": len(positions),
            "trades_per_day": trades_per_day,
            "win_rate": round(win_rate, 1),
            "avg_win_pct": round(float(avg_win), 2),
            "avg_loss_pct": round(float(avg_loss), 2),
            "profit_factor": round(profit_factor, 2),
        },
        "timestamp": datetime.now().isoformat(),
    }

    # Cache the result so all API endpoints show identical numbers
    _portfolio_cache["state"] = result
    _portfolio_cache["timestamp"] = time.time()

    return result


def _smart_close_trade(trade: dict, equity_price: float):
    """Smart wrapper around close_paper_trade that auto-detects options
    and converts the equity stock price to the correct option premium.

    THIS IS THE ROOT-CAUSE FIX for the cash-inflation incident: most close
    sites in the codebase were calling close_paper_trade(trade_id, current_price)
    where current_price is the EQUITY stock price. For an OPTIONS trade
    (instrument_type='call'/'put'), close_paper_trade then used that equity
    price as the "exit_premium" in the options pnl formula, which produced
    massively wrong pnl (4000-6000% on the production incident trades).

    Behavior:
      - For EQUITY trades: passes equity_price through unchanged (no behavior change)
      - For OPTIONS trades:
          1. First try get_current_premium() from the options engine (real fetch)
          2. If unavailable, estimate premium from intrinsic + time-decay
          3. Last resort: close at entry premium (flat trade, no pnl)
        Never passes the equity stock price as exit_premium for options.

    Args:
        trade: dict-like (sqlite3.Row or dict) — must have 'id', 'instrument_type'
        equity_price: current STOCK price (used directly for equity trades,
                      ignored for options where premium is fetched separately)
    """
    from predictions.models import close_paper_trade as _close_db
    try:
        instrument_type = trade.get("instrument_type") if hasattr(trade, "get") else trade["instrument_type"]
        instrument_type = instrument_type or "equity"
    except Exception:
        instrument_type = "equity"

    trade_id = trade["id"]

    # EQUITY trades: equity_price is correct as-is
    if instrument_type not in ("call", "put"):
        _close_db(trade_id, equity_price)
        return

    # OPTIONS trades: equity_price is WRONG — need the option premium
    ticker = trade.get("ticker") if hasattr(trade, "get") else trade["ticker"]
    try:
        strike = trade.get("strike_price", 0) if hasattr(trade, "get") else trade["strike_price"]
    except Exception:
        strike = 0
    try:
        expiry = trade.get("expiration_date", "") if hasattr(trade, "get") else trade["expiration_date"]
    except Exception:
        expiry = ""
    try:
        entry_premium = trade.get("premium_per_contract", 0) if hasattr(trade, "get") else trade["premium_per_contract"]
        entry_premium = entry_premium or 0
    except Exception:
        entry_premium = 0

    # 1) Try the real premium fetch from the options engine
    premium = None
    try:
        from predictions.options_engine import get_current_premium
        premium = get_current_premium(ticker, strike, expiry, instrument_type)
    except Exception as e:
        logger.debug(f"_smart_close: get_current_premium failed for {ticker}: {e}")

    # 2) Fallback: estimate from intrinsic + time-decay using equity_price
    if not premium or premium <= 0:
        try:
            if instrument_type == "call":
                intrinsic = max(0, equity_price - strike) if strike else 0
            else:
                intrinsic = max(0, strike - equity_price) if strike else 0
            dte = 0
            if expiry:
                try:
                    exp = datetime.strptime(expiry, "%Y-%m-%d")
                    dte = max(0, (exp - datetime.now()).days)
                except Exception:
                    dte = 0
            if intrinsic > 0:
                time_value = max(0.05, entry_premium * 0.1) if dte > 5 else 0.01
                premium = intrinsic + time_value
            elif dte > 5 and entry_premium > 0:
                decay = max(0.15, dte / 60.0)
                premium = max(0.05, entry_premium * decay)
            else:
                premium = max(0.03, intrinsic + 0.02)
        except Exception as e:
            logger.warning(f"_smart_close: premium estimation failed for {ticker}: {e}")
            premium = entry_premium  # 3) Last resort: close at entry (flat)

    _close_db(trade_id, premium)


def _get_current_prices(symbols: list) -> dict:
    """Get current prices for a list of symbols (batch download).

    FOUR-TIER SAFETY NET — separate infrastructure on every tier so a
    single-vendor outage cannot blind the portfolio.

      TIER 0: Finnhub (PRIMARY when FINNHUB_API_KEY env var set; skipped otherwise)
      TIER 1: Yahoo Finance batch (legacy primary — fills what Finnhub missed)
      TIER 2: CNBC quote API (fills any symbols Yahoo dropped)
      TIER 3: Stooq CSV API (catches anything CNBC also missed)

    This is the SINGLE chokepoint for ALL portfolio price lookups
    (called from portfolio state, exit checks, stop-loss checks, win-lock).
    Loss of pricing here would break stop-losses and show wrong P&L —
    the multi-tier fallback is critical safety infrastructure.
    """
    if not symbols:
        return {}
    prices = {}

    # TIER 0: Finnhub (only when API key configured — fail-safe no-op otherwise)
    # Finnhub gives a guaranteed 60/min budget vs Yahoo's silent random
    # rate-limiting. If the key is unset, finnhub.is_enabled() returns
    # False and this entire block is skipped — existing behavior preserved.
    try:
        from predictions.finnhub_adapter import is_enabled as _fh_enabled, get_prices_batch as _fh_batch
        if _fh_enabled():
            try:
                fh_prices = _fh_batch(symbols)
                if fh_prices:
                    prices.update(fh_prices)
                    logger.info(
                        f"FINNHUB PRICES (T0): {len(fh_prices)}/{len(symbols)} "
                        f"symbols served from Finnhub"
                    )
            except Exception as _fh_e:
                logger.debug(f"Finnhub T0 fetch failed (non-fatal): {_fh_e}")
    except Exception:
        pass  # finnhub_adapter import failure → skip silently

    # TIER 1: Yahoo Finance — fills any symbol Finnhub missed (or all if disabled).
    # Only fetches symbols not already filled by Finnhub. This both saves
    # yfinance budget AND prevents Yahoo overwriting fresher Finnhub values.
    yahoo_symbols = [s for s in symbols if s not in prices]
    if yahoo_symbols:
        _throttle()
        try:
            df = yf.download(yahoo_symbols, period="5d", progress=False, group_by="ticker")
            if df is not None and not df.empty:
                for sym in yahoo_symbols:
                    try:
                        if isinstance(df.columns, pd.MultiIndex):
                            if sym in df.columns.get_level_values(0):
                                close = df[(sym, "Close")].dropna()
                                if len(close) > 0:
                                    prices[sym] = float(close.iloc[-1])
                        elif len(yahoo_symbols) == 1:
                            close = df["Close"].dropna()
                            if len(close) > 0:
                                prices[sym] = float(close.iloc[-1])
                    except Exception:
                        continue
        except Exception as e:
            logger.warning(f"Yahoo price batch failed for {len(yahoo_symbols)} symbols: {e}")

    # TIER 2: CNBC for any symbol Yahoo dropped.
    # cnbc_get_prices() is hardened: returns {} on any error, filters bad data,
    # and never raises — so we cannot accidentally crash portfolio valuation.
    try:
        missing = [s for s in symbols if s not in prices]
        if missing:
            try:
                from analysis.extras import cnbc_get_prices
                cnbc_prices = cnbc_get_prices(missing)
                if cnbc_prices:
                    prices.update(cnbc_prices)
                    logger.info(
                        f"CNBC PRICE FALLBACK (T2): filled {len(cnbc_prices)}/{len(missing)} "
                        f"symbols Yahoo missed: {list(cnbc_prices.keys())}"
                    )
            except Exception as e:
                # Even though cnbc_get_prices is wrapped, double-guard at call site
                logger.debug(f"CNBC price fallback failed (non-fatal): {e}")
    except Exception:
        # Last-resort guard: never let fallback bookkeeping break primary prices
        pass

    # TIER 3: Stooq for anything CNBC ALSO missed (full-vendor outage).
    # Stooq has separate infrastructure entirely — survives Yahoo+CNBC outage.
    try:
        still_missing = [s for s in symbols if s not in prices]
        if still_missing:
            try:
                from analysis.extras import stooq_quote_batch
                stooq_prices = stooq_quote_batch(still_missing)
                if stooq_prices:
                    # stooq returns {sym: {price, change_pct}} — extract price only
                    for sym, q in stooq_prices.items():
                        if isinstance(q, dict) and q.get("price") is not None:
                            prices[sym] = float(q["price"])
                    logger.warning(
                        f"STOOQ PRICE FALLBACK (T3): filled {len(stooq_prices)}/{len(still_missing)} "
                        f"symbols both Yahoo+CNBC missed: {list(stooq_prices.keys())}"
                    )
            except Exception as e:
                logger.debug(f"Stooq price fallback failed (non-fatal): {e}")
    except Exception:
        pass

    return prices


# ============================================================
#  TRADE EXECUTION
# ============================================================

def execute_trades_from_signals(quant_picks: dict) -> dict:
    """
    Execute paper trades based on quant signal picks.

    Logic:
      1. Close positions that hit stop-loss, target, or hold duration
      2. Open new LONG positions for top long picks
      3. Open new SHORT positions for top short picks
      4. Respect max position limit and minimum confidence

    Args:
        quant_picks: output from generate_quant_picks()

    Returns:
        dict with trades executed, positions closed, portfolio state
    """
    from predictions.models import (
        get_open_trades, close_paper_trade, save_paper_trade,
        get_portfolio_snapshots, save_portfolio_snapshot, get_cash
    )

    results = {
        "opened": [],
        "closed": [],
        "skipped": [],
        "errors": [],
    }

    # ----- PICK DEDUPE: remove tickers in BOTH long and short lists -----
    # Prevents the contradictory-signal bug (e.g., RBLX in both long & short).
    # Mutates a NEW dict; original quant_picks unchanged.
    try:
        quant_picks = _dedupe_contradictory_picks(quant_picks)
        if quant_picks.get("_dedup_log"):
            results["pick_dedup_log"] = quant_picks["_dedup_log"]
    except Exception as _dde:
        logger.warning(f"Pick dedup wrapper error (proceeding with original picks): {_dde}")

    open_trades = get_open_trades()
    open_tickers = set(t["ticker"] for t in open_trades)
    snapshots = get_portfolio_snapshots(days=5)

    # Cash from atomic paper_cash table — ALWAYS accurate
    cash = get_cash()

    regime = quant_picks.get("regime", {}).get("regime", "SIDEWAYS")

    # --- Step 1: Check exits for open positions ---
    if open_trades:
        exit_symbols = list(set(t["ticker"] for t in open_trades))
        current_prices = _get_current_prices(exit_symbols)

        for trade in open_trades:
            ticker = trade["ticker"]
            current_price = current_prices.get(ticker)
            if current_price is None:
                continue

            entry_price = trade["entry_price"]
            direction = trade["direction"]
            should_close = False
            close_reason = ""

            # Calculate current P&L
            if direction == "long":
                pnl_pct = ((current_price / entry_price) - 1) * 100
            else:
                pnl_pct = ((entry_price / current_price) - 1) * 100

            # Check stop loss
            stop_loss = trade.get("stop_loss_price", 0)
            if stop_loss and direction == "long" and current_price <= stop_loss:
                should_close = True
                close_reason = f"Stop loss hit (${stop_loss})"
            elif stop_loss and direction == "short" and current_price >= stop_loss:
                should_close = True
                close_reason = f"Stop loss hit (${stop_loss})"

            # Check target
            target = trade.get("target_price", 0)
            if target and direction == "long" and current_price >= target:
                should_close = True
                close_reason = f"Target hit (${target})"
            elif target and direction == "short" and current_price <= target:
                should_close = True
                close_reason = f"Target hit (${target})"

            # Check hold duration
            try:
                entry_date = datetime.fromisoformat(trade["entry_date"])
                days_held = (datetime.now() - entry_date).days
                max_hold = trade.get("hold_duration_days", DEFAULT_HOLD_DAYS)
                if days_held >= max_hold:
                    should_close = True
                    close_reason = f"Hold duration expired ({days_held} days)"
            except Exception:
                pass

            # AUTONOMOUS TRAILING PROFIT PROTECTION
            # The machine decides when to lock profits based on each stock's volatility.
            # A volatile stock (ATR 5%) needs to be up 10%+ before trailing makes sense.
            # A stable stock (ATR 1%) can start trailing at 3%.
            # Trail width = 1x ATR below peak — gives exactly one day of noise room.
            stock_atr = _calculate_stock_atr(ticker)
            trail_start_pct = stock_atr * 100 * 1.5  # Start trailing at 1.5x daily ATR (was 2x — too late)
            trail_start_pct = max(2.0, min(trail_start_pct, 8.0))  # Clamp 2-8% (was 3-12% — waited too long)

            if not should_close and pnl_pct > trail_start_pct:
                try:
                    _throttle()
                    hist_df = yf.download(ticker, period="1mo", progress=False)
                    if hist_df is not None and len(hist_df) >= 3:
                        hist_closes = _safe_col(hist_df, "Close").values.astype(float)

                        # Get peak P&L since entry
                        if direction == "long":
                            peak_price = float(np.max(hist_closes))
                            peak_pnl = ((peak_price / entry_price) - 1) * 100
                        else:
                            trough_price = float(np.min(hist_closes))
                            peak_pnl = ((entry_price / trough_price) - 1) * 100

                        # Smart trail: keep more gains, protect profits aggressively
                        if peak_pnl >= 15:
                            trail_pct = 0.70  # Keep 70% of big gains (was 65%)
                        elif peak_pnl >= 8:
                            trail_pct = 0.60  # Keep 60% (was 55%)
                        else:
                            trail_pct = 0.50  # Keep 50% of smaller gains (was 45%)

                        trail_level = peak_pnl * trail_pct

                        if peak_pnl >= trail_start_pct and pnl_pct < trail_level:
                            should_close = True
                            close_reason = (
                                f"SMART TRAIL: ATR={stock_atr*100:.1f}%, peaked at {peak_pnl:+.1f}%, "
                                f"trail at {trail_level:+.1f}%, now {pnl_pct:+.1f}%"
                            )
                except Exception:
                    pass

            # AUTONOMOUS TAKE PROFIT — based on stock's target (set at entry)
            # The target was already calculated by _autonomous_stop_and_target()
            # at entry time and stored in trade["target_price"]. We check it above.
            # But if somehow the stock is up 20%+ and target wasn't hit, lock it.
            # Lower profit lock in preservation mode (10% vs 15%)
            profit_lock_threshold = 10.0 if _is_preservation_mode() else 15.0
            if not should_close and pnl_pct >= profit_lock_threshold:
                should_close = True
                close_reason = f"AUTO PROFIT LOCK: up {pnl_pct:+.1f}% — gain secured (threshold: {profit_lock_threshold}%{'  [PRESERVATION MODE]' if profit_lock_threshold == 10 else ''})"

            # TIME DECAY EXIT: If trade hasn't moved in our favor after 50% of hold time, cut it
            # This prevents dead-weight trades from sitting and eventually hitting stop loss
            if not should_close:
                try:
                    entry_date = datetime.fromisoformat(trade["entry_date"])
                    days_held = (datetime.now() - entry_date).days
                    max_hold = trade.get("hold_duration_days", DEFAULT_HOLD_DAYS)
                    # Cut flat trades faster in preservation mode (40% vs 50% of hold time)
                    decay_frac = 0.4 if _is_preservation_mode() else 0.5
                    half_hold = max(2, int(max_hold * decay_frac))
                    if days_held >= half_hold and pnl_pct <= 0.5:
                        # Held for half the expected time and still flat or losing
                        should_close = True
                        close_reason = (
                            f"TIME DECAY: {days_held}d held (half of {max_hold}d), "
                            f"only {pnl_pct:+.1f}% — cutting dead weight"
                        )
                except Exception:
                    pass

            # BEAR PROTECTION: close losing longs at stop level, not at -2%
            # Was -2% = too tight, gets stopped out on normal volatility
            if not should_close and regime == "BEAR" and direction == "long" and pnl_pct < -4:
                should_close = True
                close_reason = f"BEAR regime protection — closing losing long ({pnl_pct:+.1f}%)"

            # QUICK-CUT RULE: Shorts losing >3% in first 2 days = bad trade, cut immediately
            # Shorts can gap up violently — don't wait for stop-loss
            if not should_close and direction == "short" and pnl_pct < -3:
                try:
                    entry_date = datetime.fromisoformat(trade["entry_date"])
                    days_held = (datetime.now() - entry_date).days
                    if days_held <= 2:
                        should_close = True
                        close_reason = f"QUICK-CUT: short losing {pnl_pct:+.1f}% in {days_held} days — bad entry"
                except Exception:
                    pass

            # SHORTS MAX LOSS: Never let a short lose more than 5%
            if not should_close and direction == "short" and pnl_pct < -5:
                should_close = True
                close_reason = f"SHORT MAX LOSS: down {pnl_pct:+.1f}% — hard cap reached"

            # GEO EVENT OVERRIDE: Close positions fighting headline sentiment
            # Context-aware: reads actual headlines instead of hardcoded rules
            if not should_close:
                macro_data = quant_picks.get("macro", {})
                ceasefire_ending = macro_data.get("ceasefire_ending_overlay", False)
                ceasefire_active = macro_data.get("ceasefire_overlay", False)
                trade_sector = trade.get("sector", "")
                geo_impact = macro_data.get("geo_impact_analysis", {})
                sector_signals = geo_impact.get("sector_signals", {})
                sector_signal = sector_signals.get(trade_sector, 0)

                # Close positions that FIGHT the headline sentiment
                if direction == "short" and sector_signal > 0.5:
                    should_close = True
                    close_reason = f"GEO EVENT EXIT: Short {trade_sector} closed — headlines bullish (signal={sector_signal:+.2f})"
                    logger.warning(f"GEO EXIT: Closing short {ticker} ({trade_sector}) — headlines bullish")
                elif direction == "long" and sector_signal < -0.5:
                    if pnl_pct < 2:
                        should_close = True
                        close_reason = f"GEO EVENT EXIT: Long {trade_sector} closed — headlines bearish (signal={sector_signal:+.2f})"

            # Check if signal has reversed (optional aggressive exit)
            if not should_close and pnl_pct < -5:
                # If losing more than 5% and we have new signals,
                # check if the stock is now in opposite direction
                for pick in quant_picks.get("short_picks", []) if direction == "long" else quant_picks.get("long_picks", []):
                    if pick["symbol"] == ticker:
                        should_close = True
                        close_reason = f"Signal reversed to {pick['direction']}"
                        break

            if should_close:
                try:
                    _smart_close_trade(trade, current_price)  # auto-handles options
                    # Cash is updated atomically in close_paper_trade()
                    open_tickers.discard(ticker)
                    results["closed"].append({
                        "ticker": ticker,
                        "direction": direction,
                        "entry_price": entry_price,
                        "exit_price": round(current_price, 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "reason": close_reason,
                    })
                except Exception as e:
                    results["errors"].append(f"Failed to close {ticker}: {str(e)}")

    # Refresh cash after closes (atomic paper_cash is always current)
    cash = get_cash()

    # --- Step 2: Open new positions ---
    current_positions = len(open_tickers)
    available_slots = MAX_POSITIONS - current_positions
    regime = quant_picks.get("regime", {}).get("regime", "SIDEWAYS")
    vix_level = quant_picks.get("regime", {}).get("vix_level", None)

    # --- DYNAMIC WIN-LOCK SYSTEM ---
    # The system decides its own lock threshold based on VIX + regime.
    # No hardcoded values — adapts to market conditions in real time.
    current_prices_for_winlock = _get_current_prices(list(open_tickers)) if open_tickers else {}
    positions_value_now = sum(
        current_prices_for_winlock.get(t["ticker"], t["entry_price"]) * t["shares"]
        for t in get_open_trades()
    )
    total_current_value = cash + positions_value_now
    total_return_now = ((total_current_value / ORIGINAL_CAPITAL) - 1) * 100

    # ----- AUTO STOP-LOSS TIGHTENING -----
    # When portfolio is in drawdown (>3%), auto-tightens stops on ALL
    # open equity positions to lock in remaining gains. Ratchet ONLY
    # tighter, never looser. Skips options. Logs each tightening.
    try:
        _tight_result = _auto_tighten_stops(get_open_trades(), total_return_now)
        if _tight_result.get("tightened", 0) > 0:
            results["auto_stop_tighten"] = _tight_result
    except Exception as _ate:
        logger.warning(f"Auto-tighten wrapper error (no stops changed): {_ate}")

    # Compare to yesterday's snapshot to get TODAY's gain.
    # ROOT-CAUSE GUARD: with 0 open positions any "gain" relative to a prior
    # snapshot is mathematically synthetic (recovery script, manual cash
    # adjustment, snapshot drift, etc.). Don't let it trigger WIN-LOCK.
    daily_gain = 0
    if snapshots and current_positions > 0:
        yesterday_val = snapshots[-1].get("total_value", INITIAL_CAPITAL)
        daily_gain = ((total_current_value / yesterday_val) - 1) * 100

    # Dynamic WIN-LOCK: system decides based on VIX + regime
    winlock = _get_dynamic_winlock(regime)
    winlock_threshold = winlock["lock_pct"]
    winlock_caution = winlock["caution_pct"]

    if daily_gain >= winlock_threshold:
        # SELECTIVE WIN-LOCK: sell intraday trades and positions near target, keep multi-day holds
        logger.warning(f"WIN-LOCK: Up {daily_gain:+.1f}% today (threshold: {winlock_threshold}%) — SELECTIVE SELL | {winlock['reason']}")
        sold_count = 0
        for trade in get_open_trades():
            t_ticker = trade["ticker"]
            t_price = current_prices_for_winlock.get(t_ticker)
            if not t_price:
                continue
            t_dir = trade["direction"]
            t_entry = trade["entry_price"]
            t_target = trade.get("target_price", t_entry * 1.05)
            t_hold_class = trade.get("hold_class", "swing")

            if t_dir == "long":
                t_pnl = ((t_price / t_entry) - 1) * 100
                target_achieved = (t_price - t_entry) / (t_target - t_entry + 0.01) if t_target > t_entry else 1.0
            else:
                t_pnl = ((t_entry / t_price) - 1) * 100
                target_achieved = (t_entry - t_price) / (t_entry - t_target + 0.01) if t_entry > t_target else 1.0

            # Sell if: intraday trade, OR 80%+ of target achieved, OR losing money
            should_sell = (
                t_hold_class == "intraday" or
                target_achieved >= 0.8 or
                t_pnl < 0
            )

            if should_sell:
                try:
                    _smart_close_trade(trade, t_price)  # auto-handles options
                    sold_count += 1
                    results["closed"].append({
                        "ticker": t_ticker, "direction": t_dir,
                        "entry_price": t_entry, "exit_price": round(t_price, 2),
                        "pnl_pct": round(t_pnl, 2),
                        "reason": f"WIN-LOCK selective: daily +{daily_gain:.1f}% | class={t_hold_class} target={target_achieved:.0%}",
                    })
                    open_tickers.discard(t_ticker)
                except Exception as e:
                    results["errors"].append(f"WIN-LOCK close {t_ticker}: {e}")
            else:
                logger.info(f"WIN-LOCK HOLD: Keeping {t_ticker} ({t_hold_class}, {t_pnl:+.1f}%, target {target_achieved:.0%})")

        cash = get_cash()
        results["skipped"].append({"symbol": "WIN-LOCK", "reason": f"Selective sell: up {daily_gain:+.1f}% — sold {sold_count} trades, kept multi-day holds"})
        available_slots = max(0, available_slots - sold_count)
    elif daily_gain >= winlock_caution:
        logger.warning(f"WIN-LOCK CAUTION: Up {daily_gain:+.1f}% (caution at {winlock_caution}%) — reducing new trade size by 50%")

    # --- DRAWDOWN PROTECTION (hedge fund risk management) ---
    drawdown_pct = total_return_now
    drawdown_multiplier = 1.0

    if drawdown_pct <= -10:
        logger.warning(f"DRAWDOWN PROTECTION: Portfolio down {drawdown_pct:.1f}% — HALTING new trades")
        results["skipped"].append({"symbol": "ALL", "reason": f"Drawdown protection: portfolio down {drawdown_pct:.1f}%"})
        available_slots = 0
    elif drawdown_pct <= -5:
        logger.warning(f"DRAWDOWN PROTECTION: Portfolio down {drawdown_pct:.1f}% — halving position sizes")
        drawdown_multiplier = 0.5

    # No position limits — the computer trades freely like a real hedge fund

    # Get VIX-based position size multiplier
    vix_multiplier = _get_vix_scale()
    logger.info(f"VIX position size multiplier: {vix_multiplier}")

    # --- LEARN FROM MISTAKES: Apply corrections from past losing trades ---
    try:
        from predictions.learner import get_mistake_adjustments
        mistake_adj = get_mistake_adjustments()
        logger.info(f"Mistake adjustments loaded: {len(mistake_adj.get('sector_penalties', {}))} sector penalties, "
                     f"{len(mistake_adj.get('blocked_combos', []))} blocked combos")
    except Exception:
        mistake_adj = {"sector_penalties": {}, "blocked_combos": [], "confidence_cap": 95, "tighten_stops": False}

    # --- OVERNIGHT INTELLIGENCE: Apply pre-market position sizing ---
    overnight = quant_picks.get("overnight", {})
    overnight_size_mod = overnight.get("position_size_modifier", 1.0) if overnight else 1.0

    # --- SMART ORDER TIMING: Check if this is a good entry window ---
    # Allow MARKET OPEN trigger to override the 9:30-9:45 avoid window so we
    # capture the open instead of waiting until 9:45 (which delays everything).
    # force_anytime overrides EVERYTHING (weekend/off-hours) — used by the
    # admin force-trade-now endpoint for paper-trading weekend gap bets.
    force_market_open = bool(quant_picks.get("force_market_open", False))
    force_anytime = bool(quant_picks.get("force_anytime", False))
    timing = _is_good_entry_time(force_market_open=force_market_open, force_anytime=force_anytime)
    timing_size_mod = timing["size_modifier"]
    timing_conf_shift = timing["confidence_shift"]
    if timing["window"] == "avoid":
        logger.warning(f"SMART TIMING: BLOCKED new entries — first 15 min window (9:30-9:45 ET). "
                       f"Pass force_market_open=True to override.")
    elif timing["window"] == "market_open_force":
        logger.warning(f"SMART TIMING: MARKET OPEN FORCE — trading the 9:30 open with reduced size (0.6x)")
    elif timing["window"] in ("off_hours_force", "weekend_force"):
        logger.warning(f"SMART TIMING: FORCE-ANYTIME ACTIVE ({timing['window']}) — paper-trade weekend gap bet, 0.5x size, +10 conf threshold")
    elif timing["window"] == "power_hour":
        logger.info(f"SMART TIMING: Power hour active — institutional flow window")
    elif timing["window"] == "caution":
        logger.info(f"SMART TIMING: Caution window (9:45-10:30) — reduced sizing")
    elif timing["window"] == "off_hours":
        logger.warning(f"SMART TIMING: BLOCKED — outside market hours (9:30-16:00 ET)")
    elif timing["window"] == "weekend":
        logger.warning(f"SMART TIMING: BLOCKED — weekend, market closed")

    # --- DYNAMIC EXPOSURE CONTROLLER (the "thinking" layer) ---
    # Auto-decides how much of the portfolio to deploy based on:
    #   - recent 30-day Sharpe (win rate signal)
    #   - VIX level (calm vs panic)
    #   - days since last losing day (recency)
    #   - portfolio drawdown from peak
    # No human input needed — eliminates the need to manually say "increase
    # exposure". Result is clamped to [0.50, 0.95]. Falls back to 0.85 on
    # any error.
    try:
        _dyn_exposure = _compute_dynamic_exposure_target(
            vix_level=vix_level,
            drawdown_pct=total_return_now,
        )
        dynamic_max_exposure_pct = _dyn_exposure.get("target", DYNAMIC_EXPOSURE_SAFE_DEFAULT)
        logger.warning(
            f"DYNAMIC EXPOSURE: target={dynamic_max_exposure_pct:.2%} | "
            f"reasoning: {_dyn_exposure.get('reasoning', 'n/a')}"
        )
        results["dynamic_exposure"] = _dyn_exposure
    except Exception as _e:
        logger.warning(f"Dynamic exposure failed (using safe default 0.85): {_e}")
        dynamic_max_exposure_pct = DYNAMIC_EXPOSURE_SAFE_DEFAULT
        results["dynamic_exposure"] = {
            "target": dynamic_max_exposure_pct,
            "reasoning": f"error: {_e}",
        }

    # --- MACRO EVENT REACTOR — TACO TRADE DETECTOR ---
    # Looks for shock-but-not-crisis market state where panic is likely
    # to reverse rather than persist. When detected:
    #   - boost long confidence (+10) so we capture the reversal
    #   - veto new shorts in safe-haven sectors (don't fight the rally)
    #   - bump exposure target up by +0.05 (lean in to the opportunity)
    # All effects are captured in results["taco_signal"] for visibility.
    try:
        _taco = _detect_taco_reversal_event(quant_picks)
        results["taco_signal"] = {
            "active": _taco.get("active", False),
            "reason": _taco.get("reason", "n/a"),
            "confidence_boost": _taco.get("confidence_boost", 0),
            "veto_shorts_in_sectors": list(_taco.get("veto_shorts_in", set())),
            "exposure_boost": _taco.get("exposure_boost", 0.0),
        }
        if _taco.get("active"):
            # Apply exposure bump (still respect the hard cap)
            taco_exp_boost = float(_taco.get("exposure_boost", 0.0))
            new_target = min(DYNAMIC_EXPOSURE_MAX, dynamic_max_exposure_pct + taco_exp_boost)
            logger.warning(
                f"TACO REVERSAL ACTIVE: {_taco.get('reason')} | "
                f"exposure {dynamic_max_exposure_pct:.2%} -> {new_target:.2%} | "
                f"+10 long conf, veto shorts in {sorted(_taco.get('veto_shorts_in', set()))}"
            )
            dynamic_max_exposure_pct = new_target
            results["dynamic_exposure"]["target"] = new_target
            results["dynamic_exposure"]["taco_boost"] = taco_exp_boost
    except Exception as _te:
        logger.warning(f"TACO detector failed (no boost applied): {_te}")
        _taco = {"active": False, "confidence_boost": 0, "veto_shorts_in": set()}
        results["taco_signal"] = {"active": False, "reason": f"error: {_te}"}

    # --- PORTFOLIO VaR BUDGET ---
    var_data = quant_picks.get("portfolio_var", {})
    var_multiplier = var_data.get("var_multiplier", 1.0)
    if var_multiplier < 1.0:
        logger.warning(f"VAR BUDGET: VaR={var_data.get('var_pct', 0):.2f}% | "
                       f"status={var_data.get('status', 'UNKNOWN')} | "
                       f"size multiplier={var_multiplier}x")

    # --- ADAPTIVE STREAK CALIBRATION ---
    streak = _get_streak_calibration()
    streak_size_mod = streak["size_multiplier"]
    streak_conf_shift = streak["confidence_shift"]
    sector_streak_penalties = streak.get("sector_penalties", {})
    if sector_streak_penalties:
        logger.warning(f"SECTOR STREAK PENALTIES: {sector_streak_penalties}")
    if streak["streak_length"] >= 3:
        logger.info(f"STREAK CALIBRATION: {streak['streak_type']} streak x{streak['streak_length']} — "
                    f"size {streak_size_mod:.2f}x, confidence shift {streak_conf_shift:+d}")

    # DIAGNOSTIC: log entry-guard state so we can see why trades aren't firing
    logger.warning(
        f"TRADE EXECUTION ENTRY: available_slots={available_slots}, "
        f"cash=${cash:.2f}, timing.window={timing.get('window')}, "
        f"timing.can_trade={timing.get('can_trade')}, regime={regime}, "
        f"long_picks_in={len(quant_picks.get('long_picks', []))}, "
        f"short_picks_in={len(quant_picks.get('short_picks', []))}"
    )

    if available_slots > 0 and cash > 1000 and timing.get("can_trade", True):
        # REGIME-AWARE PICK SELECTION
        # In BEAR: prioritize shorts heavily, limit longs
        # In BULL: prioritize longs heavily, limit shorts
        all_picks = []

        # Use dynamic filters — tighter in preservation mode
        # Apply streak calibration to confidence threshold
        min_conf = _get_min_confidence() + streak_conf_shift + timing_conf_shift
        min_conf = max(20, min(80, min_conf))  # Clamp to sane range
        min_score = _get_min_composite_score()
        preservation = _is_preservation_mode()
        if preservation:
            logger.warning(f"CAPITAL PRESERVATION MODE: confidence >= {min_conf}, score >= {min_score}, position size 3%")

        long_candidates = [p for p in quant_picks.get("long_picks", [])
                          if p["confidence"] >= min_conf
                          and abs(p.get("composite_score", 0)) >= min_score
                          and p["symbol"] not in open_tickers]
        short_candidates = [p for p in quant_picks.get("short_picks", [])
                           if p["confidence"] >= min_conf
                           and abs(p.get("composite_score", 0)) >= min_score
                           and p["symbol"] not in open_tickers]

        # ────────────────────────────────────────────────────────────────
        # SECTOR CONCENTRATION CAP (2026-05-17 ADD)
        # Caps any single sector at MAX_PER_SECTOR picks so the book is
        # diversified.  Without this, the system can put 100% of capital
        # into one sector (e.g., all Energy) which is single-event risk.
        #
        # SAFETY:
        #   - Sorts by confidence desc first so we keep highest-conviction
        #     name in each sector.
        #   - If applying the cap would drop us below MIN_PICKS, we KEEP
        #     extra picks from the dominant sector — never let the cap
        #     completely starve the book.
        #   - Stocks with no sector tag (rare) get treated as their own
        #     "Unknown" bucket and capped together.
        # ────────────────────────────────────────────────────────────────
        MAX_PER_SECTOR = 4    # 40% of 10 picks max in one sector
        MIN_PICKS = 5         # don't let the cap drop us below 5 trades

        def _apply_sector_cap(cands):
            """Cap any sector at MAX_PER_SECTOR; never drop below MIN_PICKS.
            Pure function — returns new list, never mutates input."""
            try:
                if not cands:
                    return cands
                # Sort by confidence desc (then |score| desc) — keep top in each sector
                sorted_cands = sorted(
                    cands,
                    key=lambda p: (p.get("confidence", 0), abs(p.get("composite_score", 0))),
                    reverse=True,
                )
                kept, overflow = [], []
                sector_counts = {}
                for p in sorted_cands:
                    sec = (p.get("sector") or "Unknown")
                    if sector_counts.get(sec, 0) < MAX_PER_SECTOR:
                        kept.append(p)
                        sector_counts[sec] = sector_counts.get(sec, 0) + 1
                    else:
                        overflow.append(p)
                # Safety: if cap dropped us below minimum, backfill from overflow
                if len(kept) < MIN_PICKS and overflow:
                    backfill = overflow[: MIN_PICKS - len(kept)]
                    kept.extend(backfill)
                    logger.warning(
                        f"SECTOR CAP: backfilled {len(backfill)} from overflow "
                        f"to stay above MIN_PICKS={MIN_PICKS}"
                    )
                if len(kept) < len(cands):
                    logger.warning(
                        f"SECTOR CAP: {len(cands)} → {len(kept)} candidates "
                        f"(max {MAX_PER_SECTOR}/sector)"
                    )
                return kept
            except Exception as _se:
                # NEVER block trades — if cap logic errors, return original list
                logger.warning(f"SECTOR CAP soft-fail (returning unfiltered): {_se}")
                return cands

        long_candidates = _apply_sector_cap(long_candidates)
        # Shorts already capped at 2 by regime logic — but apply for completeness
        if len(short_candidates) > MAX_PER_SECTOR:
            short_candidates = _apply_sector_cap(short_candidates)

        # SHORT-SIDE QUALITY GATE: Shorts are harder — require modestly stronger signals.
        # LOOSENED: was score<=-3 + conf>=55 (rejected 100% of shorts in audit).
        # 2026-05-17 FIX: previous gate used `composite_score <= -2.0` which
        # required negative-signed scores, but the picks API returns short
        # scores as positive abs values (HOLX displayed as +2.22).  This
        # rejected ALL shorts and cascaded to ZERO put options.  Now uses
        # abs() so positive- or negative-signed scores both pass the magnitude
        # check.  Symmetric with long-side filter at line 2972 which already
        # uses abs(p.get("composite_score", 0)).
        pre_gate = len(short_candidates)
        short_candidates = [p for p in short_candidates
                           if abs(p.get("composite_score", 0)) >= 2.0 and p["confidence"] >= 45]
        if pre_gate > len(short_candidates):
            logger.info(f"SHORT QUALITY GATE: {pre_gate - len(short_candidates)} shorts filtered (need |score|>=2, conf>=45%)")

        # Defensive sectors — safe for long positions even in bear markets
        # These are stable, dividend-paying, recession-resistant sectors
        DEFENSIVE_SECTORS = {"Consumer Staples", "Healthcare", "Utilities", "ETF"}

        if regime == "BEAR":
            # BEAR: Take ALL qualifying shorts, plus safe defensive longs
            # Smart investing = shorts for profit + defensive longs for stability
            for p in short_candidates:
                p["_adj_confidence"] = p["confidence"] + 10
                all_picks.append(p)

            # FORCED SAFE LONGS: Only take longs in defensive sectors (low risk)
            # Defensive stocks (WMT, JNJ, PG, KO) hold up in bear markets
            safe_longs = [p for p in long_candidates
                          if p.get("sector") in DEFENSIVE_SECTORS]
            # Also allow any long with very high confidence (65%+) regardless of sector
            high_conviction_longs = [p for p in long_candidates
                                     if p["confidence"] >= 65 and p.get("sector") not in DEFENSIVE_SECTORS]
            bear_longs = (safe_longs + high_conviction_longs)[:10]

            for p in bear_longs:
                p["_adj_confidence"] = p["confidence"]  # no penalty for safe longs
                all_picks.append(p)
            logger.info(f"BEAR regime: {len(short_candidates)} shorts, {len(bear_longs)} safe longs "
                        f"({len(safe_longs)} defensive + {len(high_conviction_longs)} high-conviction)")
        elif regime == "BULL":
            # BULL: Check if de-escalation is active (context-aware)
            _geo_dir = quant_picks.get("macro", {}).get("geo_impact_analysis", {}).get("geo_direction", "neutral")
            ceasefire_active = _geo_dir == "deescalation" or quant_picks.get("macro", {}).get("ceasefire_overlay", False)

            for p in long_candidates:
                p["_adj_confidence"] = p["confidence"] + 15
                all_picks.append(p)

            if ceasefire_active:
                # TACO TRADE: NO new shorts during ceasefire — everything is up
                logger.warning("TACO TRADE ACTIVE: Ceasefire detected — blocking ALL new short entries")
                # Only allow 1 short max, and only if extremely high conviction
                top_shorts = [p for p in short_candidates if p["confidence"] >= 75 and abs(p.get("composite_score", 0)) >= 6]
                for p in top_shorts[:1]:
                    p["_adj_confidence"] = p["confidence"] - 20
                    all_picks.append(p)
                logger.info(f"TACO BULL: {len(long_candidates)} longs, {min(1, len(top_shorts))} extreme-conviction shorts only")
            else:
                for p in short_candidates[:2]:  # max 2 shorts in normal bull
                    p["_adj_confidence"] = p["confidence"] - 10
                    all_picks.append(p)
                logger.info(f"BULL regime: {len(long_candidates)} longs, {min(2, len(short_candidates))} shorts selected")
        else:
            # SIDEWAYS: balanced
            for p in long_candidates:
                p["_adj_confidence"] = p["confidence"]
                all_picks.append(p)
            for p in short_candidates:
                p["_adj_confidence"] = p["confidence"]
                all_picks.append(p)

        # DIAGNOSTIC: log filtering stats so we can see why all_picks may be empty
        logger.warning(
            f"PICK FILTERING: min_conf={min_conf}, min_score={min_score}, "
            f"long_candidates={len(long_candidates)}, "
            f"short_candidates={len(short_candidates)}, "
            f"all_picks_built={len(all_picks)}"
        )
        if not all_picks:
            # Record a diagnostic skipped entry so we can see this in API output
            results["skipped"].append({
                "symbol": "DIAGNOSTIC",
                "reason": (
                    f"all_picks empty after regime={regime} filter — "
                    f"long_candidates={len(long_candidates)}, "
                    f"short_candidates={len(short_candidates)}, "
                    f"min_conf={min_conf}, min_score={min_score}, "
                    f"long_picks_in={len(quant_picks.get('long_picks', []))}, "
                    f"short_picks_in={len(quant_picks.get('short_picks', []))}"
                ),
            })

        # Sort by adjusted confidence (highest first)
        all_picks.sort(key=lambda x: x.get("_adj_confidence", x["confidence"]), reverse=True)

        # ============================================================
        # INTERNATIONAL DIVERSIFICATION QUOTA
        # ============================================================
        # If international ADRs (or country/region ETFs) appear in the
        # candidate pool, ensure at least ~30% of the selected picks
        # come from outside the US. Without this, US names dominate the
        # composite score (more data, longer histories) and we end up
        # 100% US — defeating the purpose of adding the 222 international
        # tickers to the universe.
        try:
            from analysis.quant_engine import INTERNATIONAL_UNIVERSE as _INTL_LIST
            _INTL_SET = set(_INTL_LIST or [])
        except Exception:
            _INTL_SET = set()

        if _INTL_SET and len(all_picks) > 5:
            us_picks = [p for p in all_picks if p["symbol"] not in _INTL_SET]
            intl_picks = [p for p in all_picks if p["symbol"] in _INTL_SET]
            if intl_picks:
                # Target: 30% international when candidates exist
                target_intl_count = max(1, int(round(len(all_picks) * 0.30)))
                target_intl_count = min(target_intl_count, len(intl_picks))
                top_us = us_picks[:max(1, len(all_picks) - target_intl_count)]
                top_intl = intl_picks[:target_intl_count]
                # Re-merge keeping confidence ranking inside each group
                merged = top_us + top_intl
                merged.sort(key=lambda x: x.get("_adj_confidence", x["confidence"]), reverse=True)
                all_picks = merged
                logger.info(
                    f"INTL QUOTA applied: {target_intl_count} intl + {len(top_us)} US "
                    f"(from {len(intl_picks)} intl candidates, {len(us_picks)} us candidates)"
                )

        # CAPITAL PRESERVATION: Limit new trades to top 25% when protecting gains
        if preservation and len(all_picks) > 3:
            orig_count = len(all_picks)
            all_picks = all_picks[:max(3, len(all_picks) // 4)]  # Keep top 25%, min 3
            logger.warning(f"PRESERVATION MODE: Reduced new trades from {orig_count} to {len(all_picks)} (top 25% only)")

        # SECTOR CONCENTRATION LIMIT: max 3 positions per sector per direction
        # Diversification is what separates hedge funds from retail gamblers
        sector_counts = {}
        for t in get_open_trades():
            if t["ticker"] in open_tickers:
                key = f"{t.get('sector', 'Unknown')}_{t['direction']}"
                sector_counts[key] = sector_counts.get(key, 0) + 1

        for pick in all_picks[:available_slots]:
            symbol = pick["symbol"]
            price = pick["price"]
            direction = "long" if pick["direction"] == "LONG" else "short"

            # ----- TACO REVERSAL EFFECTS (per-pick application) -----
            # If TACO signal is active:
            #   - LONGS in any sector get a confidence boost (panic = opportunity)
            #   - SHORTS in safe-haven sectors get vetoed (don't fight the rally)
            try:
                if _taco.get("active"):
                    if direction == "long":
                        _taco_boost = int(_taco.get("confidence_boost", 0))
                        if _taco_boost > 0:
                            pick["confidence"] = min(95, int(pick.get("confidence", 50)) + _taco_boost)
                    elif direction == "short":
                        _veto_set = _taco.get("veto_shorts_in", set())
                        if pick.get("sector") in _veto_set:
                            results["skipped"].append({
                                "symbol": symbol,
                                "reason": f"TACO VETO: shorts blocked in {pick.get('sector')} during reversal-pattern event",
                            })
                            continue
            except Exception as _te2:
                logger.debug(f"TACO per-pick effect skipped for {symbol}: {_te2}")

            # GROSS EXPOSURE LIMIT: dynamic, decided per-cycle by the
            # Dynamic Exposure Controller (Sharpe + VIX + drawdown + recency).
            # In preservation mode, hard-cap at 75% regardless of dynamic value.
            # Otherwise use the dynamic target (clamped 0.50-0.95).
            gross_exposure = sum(
                t.get("shares", 0) * t.get("entry_price", 0)
                for t in get_open_trades() if t["ticker"] in open_tickers
            )
            if preservation:
                max_exposure_pct = 0.75
            else:
                max_exposure_pct = dynamic_max_exposure_pct
            max_exposure = total_current_value * max_exposure_pct
            if gross_exposure >= max_exposure:
                results["skipped"].append({
                    "symbol": symbol,
                    "reason": f"Gross exposure limit ({max_exposure_pct:.0%} of portfolio, dynamic)",
                })
                break  # Stop opening more positions

            # CONFIDENCE GATE: In BEAR only take decent-conviction longs
            # Defensive sector longs get a lower gate (35%) — they're safe by nature
            # Other sector longs need higher conviction (55%)
            if regime == "BEAR" and direction == "long":
                is_defensive = pick.get("sector") in DEFENSIVE_SECTORS
                min_conf = 35 if is_defensive else 55
                if pick["confidence"] < min_conf:
                    results["skipped"].append({
                        "symbol": symbol,
                        "reason": f"Low conviction long in BEAR ({pick['confidence']}%, need {min_conf}%)",
                    })
                    continue

            # MULTI-DAY CONFIRMATION: Low conviction picks need 2 scans
            if not _check_signal_confirmation(symbol, direction, pick.get("confidence", 50)):
                results["skipped"].append({
                    "symbol": symbol,
                    "reason": f"Multi-day confirmation: needs 2 scans (confidence {pick['confidence']}%, <45%)",
                })
                continue

            # CORRELATION CHECK: Don't hold highly correlated positions
            # This is what separates hedge funds from retail — true diversification
            if len(open_tickers) >= 3:
                corr_check = _check_correlation(symbol, open_tickers)
                if corr_check["correlated"]:
                    results["skipped"].append({
                        "symbol": symbol,
                        "reason": f"Too correlated with {corr_check['correlated_with']} (r={corr_check['max_corr']:.2f})",
                    })
                    continue

            # MISTAKE LEARNING: Per-direction sector penalty — fixes short-side bias
            # Uses long_sector_penalties for long picks, short_sector_penalties for shorts.
            # Falls back to legacy sector_penalties for backwards compatibility.
            pick_sector = pick.get("sector", "Unknown")
            if direction == "long":
                dir_penalty = mistake_adj.get("long_sector_penalties", {}).get(pick_sector, 0)
            else:
                dir_penalty = mistake_adj.get("short_sector_penalties", {}).get(pick_sector, 0)
            legacy_penalty = mistake_adj.get("sector_penalties", {}).get(pick_sector, 0)
            sector_penalty = dir_penalty if dir_penalty != 0 else legacy_penalty
            if sector_penalty != 0:
                # Cap penalty at -20 (was -10) — stronger signal for repeat losers
                capped_penalty = max(-20, sector_penalty) if sector_penalty < 0 else min(10, sector_penalty)
                pick["confidence"] = max(15, pick["confidence"] + capped_penalty)
                if pick["confidence"] < MIN_CONFIDENCE:
                    results["skipped"].append({
                        "symbol": symbol,
                        "reason": f"Learned mistake: {direction} {pick_sector} has high loss rate (penalty {capped_penalty})",
                    })
                    continue

            # MISTAKE LEARNING: Block bad regime/direction combos
            combo_key = f"{regime}_{direction.upper()}"
            if combo_key in mistake_adj.get("blocked_combos", []):
                # Don't fully block — just heavily penalize confidence
                pick["confidence"] = max(15, pick["confidence"] - 15)
                if pick["confidence"] < MIN_CONFIDENCE:
                    results["skipped"].append({
                        "symbol": symbol,
                        "reason": f"Learned mistake: {direction} in {regime} regime has high loss rate",
                    })
                    continue

            # MISTAKE LEARNING: Cap max confidence if system has been overconfident
            conf_cap = mistake_adj.get("confidence_cap", 95)
            pick["confidence"] = min(pick["confidence"], conf_cap)

            # Check sector concentration — max 4 per sector per direction for diversification
            sector_key = f"{pick.get('sector', 'Unknown')}_{direction}"
            if sector_counts.get(sector_key, 0) >= 8:
                results["skipped"].append({
                    "symbol": symbol,
                    "reason": f"Sector concentration limit ({pick.get('sector')} {direction})",
                })
                continue

            # Position sizing: CONVICTION-BASED like a real hedge fund
            # Renaissance uses 5-10% per position, not 2%
            cash = get_cash()  # Always fresh
            total_value = cash + positions_value_now

            # RENTECH: Apply circuit breaker from portfolio risk module
            circuit_breaker = quant_picks.get("circuit_breaker", {})
            cb_multiplier = circuit_breaker.get("position_size_multiplier", 1.0)
            if not circuit_breaker.get("allow_new_longs", True) and direction == "long":
                results["skipped"].append({
                    "symbol": symbol,
                    "reason": f"Circuit breaker HALT: {circuit_breaker.get('message', 'drawdown too large')}",
                })
                continue
            if not circuit_breaker.get("allow_new_shorts", True) and direction == "short":
                results["skipped"].append({
                    "symbol": symbol,
                    "reason": f"Circuit breaker: no new shorts — {circuit_breaker.get('message', '')}",
                })
                continue

            # DRAWDOWN RECOVERY ENGINE — enhanced strategy shift during drawdowns
            dd_mode = quant_picks.get("drawdown_mode", {})
            dd_multiplier = dd_mode.get("size_multiplier", 1.0)
            dd_allowed = dd_mode.get("allowed_sectors")
            dd_min_conf = dd_mode.get("min_confidence", 0)
            dd_strategy = dd_mode.get("strategy_shift", "none")

            if dd_multiplier == 0:
                results["skipped"].append({
                    "symbol": symbol,
                    "reason": f"DRAWDOWN HALT: {dd_mode.get('message', 'trading suspended')}",
                })
                continue
            if dd_allowed is not None and pick.get("sector", "") not in dd_allowed:
                results["skipped"].append({
                    "symbol": symbol,
                    "reason": f"Drawdown mode: only {dd_allowed} sectors allowed",
                })
                continue
            # Enhanced: enforce minimum confidence during drawdown
            if dd_min_conf > 0 and pick.get("confidence", 0) < dd_min_conf:
                results["skipped"].append({
                    "symbol": symbol,
                    "reason": f"Drawdown recovery: need {dd_min_conf}%+ confidence, got {pick.get('confidence', 0)}%",
                })
                continue
            # Enhanced: strategy shift — only allow mean reversion in emergency mode
            if dd_strategy == "mean_reversion":
                is_mr = pick.get("mean_reversion", False) or pick.get("signal_type") == "MEAN_REVERSION"
                if not is_mr and pick.get("confidence", 0) < 80:
                    results["skipped"].append({
                        "symbol": symbol,
                        "reason": "Drawdown EMERGENCY: only mean reversion trades or 80%+ conviction allowed",
                    })
                    continue

            # EARNINGS SHIELD — Block stocks with earnings in next 1-2 days
            earnings_shield = quant_picks.get("earnings_shield", {})
            earnings_blocked_syms = {s["symbol"] for s in earnings_shield.get("blocked", [])}
            if symbol in earnings_blocked_syms:
                results["skipped"].append({
                    "symbol": symbol,
                    "reason": "EARNINGS SHIELD: earnings imminent, trade blocked",
                })
                continue

            # GEO EVENT ENTRY BLOCKER — context-aware, reads headline sentiment
            # Instead of hardcoding "never short energy when ceasefire ending",
            # the system reads current headlines to determine what to block.
            macro_data = quant_picks.get("macro", {})
            geo_ceasefire_ending = macro_data.get("ceasefire_ending_overlay", False)
            geo_ceasefire_active = macro_data.get("ceasefire_overlay", False)
            pick_sector = pick.get("sector", "")
            geo_impact = macro_data.get("geo_impact_analysis", {})
            geo_sector_signals = geo_impact.get("sector_signals", {})
            geo_overlay_source = macro_data.get("geo_overlay_source", "")

            if geo_ceasefire_ending or geo_ceasefire_active:
                # Check if headlines say THIS sector is going in THIS direction
                sector_signal = geo_sector_signals.get(pick_sector, 0)

                # Block trades that FIGHT the headline sentiment
                if direction == "short" and sector_signal > 0.3:
                    # Headlines say sector is bullish — don't short it
                    results["skipped"].append({
                        "symbol": symbol,
                        "reason": f"GEO BLOCK: Cannot short {pick_sector} — headlines bullish (signal={sector_signal:+.2f})",
                    })
                    logger.warning(f"GEO ENTRY BLOCK: Blocked short {symbol} ({pick_sector}) — headline bullish")
                    continue
                elif direction == "long" and sector_signal < -0.3:
                    # Headlines say sector is bearish — penalize long
                    pick["confidence"] = max(15, pick["confidence"] - 20)
                    if pick["confidence"] < MIN_CONFIDENCE:
                        results["skipped"].append({
                            "symbol": symbol,
                            "reason": f"GEO BLOCK: Long {pick_sector} penalized — headlines bearish (signal={sector_signal:+.2f})",
                        })
                        continue

                # If no headline signal but geo event is active, apply mild caution
                if geo_ceasefire_ending and abs(sector_signal) < 0.3:
                    if direction in ("long", "short"):
                        # Mild confidence reduction for uncertainty
                        pick["confidence"] = max(15, pick["confidence"] - 8)
                        logger.info(f"GEO CAUTION: {direction} {symbol} ({pick_sector}) — geo event active, no clear headline direction")

            # KELLY CRITERION POSITION SIZING — allocate based on actual edge
            # Falls back to fixed sizing if insufficient trade history
            kelly_size = _kelly_position_size(
                confidence=pick.get("confidence", 50),
                composite_score=pick.get("composite_score", 0),
                sector=pick.get("sector", ""),
                regime=regime,
                direction=direction,
                vix_level=vix_level,
            )

            # Regime adjustment on top of Kelly
            if regime == "BEAR":
                size_pct = min(0.12, kelly_size * 1.2) if direction == "short" else min(0.08, kelly_size * 0.8)
            elif regime == "BULL":
                size_pct = min(0.12, kelly_size * 1.2) if direction == "long" else min(0.06, kelly_size * 0.7)
            else:
                size_pct = kelly_size

            # INTELLIGENCE OVERLAY size factor (Level 6) — cuts size before
            # known macro events (FOMC/CPI) and when sector concentration
            # is too high. ALWAYS in [0.5, 1.0] range, NEVER zeroes the
            # size. Disable via env DISABLE_INTELLIGENCE_OVERLAY=1.
            try:
                from predictions.intelligence_overlay import compute_size_factor
                _sf_result = compute_size_factor()
                _size_factor = float(_sf_result.get("factor", 1.0))
                if 0.5 <= _size_factor <= 1.0 and _size_factor < 1.0:
                    size_pct = size_pct * _size_factor
            except Exception:
                pass  # soft-fail to no adjustment

            # CORRELATION-AWARE POSITION LIMIT (from rentech VaR module)
            # Uses real return correlations, not just sector proxies
            corr_multiplier = 1.0
            try:
                price_data = quant_picks.get("_price_data", {})
                if price_data and len(open_tickers) >= 2:
                    from analysis.rentech import check_correlation_limit
                    corr_result = check_correlation_limit(symbol, get_open_trades(), price_data)
                    if not corr_result["allow"]:
                        results["skipped"].append({
                            "symbol": symbol,
                            "reason": f"CORRELATION BLOCK: {corr_result['reason']}",
                        })
                        continue
                    corr_multiplier = corr_result["correlation_multiplier"]
            except Exception as e:
                logger.debug(f"Correlation check failed for {symbol}: {e}")

            # VaR budget block — only halt if multiplier is exactly 0 (now rare)
            if var_multiplier <= 0:
                results["skipped"].append({
                    "symbol": symbol,
                    "reason": f"VAR BUDGET EXCEEDED: Portfolio VaR at {var_data.get('var_pct', 0):.2f}% (max 5%)",
                })
                continue

            # Per-sector streak adjustment (penalty for losing sectors, boost for winning)
            sector_streak_data = sector_streak_penalties.get(pick.get("sector", ""), {})
            if isinstance(sector_streak_data, dict):
                sector_streak_mod = sector_streak_data.get("size_multiplier", 1.0)
                sector_conf_boost = sector_streak_data.get("confidence_boost", 0)
                if sector_conf_boost > 0:
                    pick["confidence"] = min(95, pick["confidence"] + sector_conf_boost)
                    logger.info(f"SECTOR WIN STREAK: {pick.get('sector')} +{sector_conf_boost}% confidence")
            else:
                sector_streak_mod = float(sector_streak_data) if sector_streak_data else 1.0

            # ============================================================
            # QUALITY-GATE SOFT RESIZER (does NOT block trades)
            # ============================================================
            # Each failing gate multiplies position size by 0.85x. Worst
            # case ~0.85^2 = 0.72 -> still above POSITION_SIZE_MULT_FLOOR
            # so trade still goes through. NEVER hard-rejects.
            # YFINANCE-FRIENDLY: only uses data already cached or cheap
            # fast_info calls. The previous RSI gate triggered a fresh
            # 3-month download per pick — removed to prevent rate limits.
            quality_mult = 1.0
            quality_notes = []
            try:
                # Gate 1: ATR > 7% (extreme volatility — slippage risk)
                # _calculate_stock_atr is already memoized via shared cache
                _atr = _calculate_stock_atr(symbol, period=14)
                if _atr > 0.07:
                    quality_mult *= 0.85
                    quality_notes.append(f"high_atr={_atr*100:.1f}%")

                # Gate 2: Gap chasing — uses fast_info (very cheap, single
                # JSON read, no historical download). Fails open on any
                # network error so a Yahoo blip never penalizes trade.
                # Skip entirely when yfinance is in degraded state.
                try:
                    from predictions.sentinels import yf_is_degraded, yf_record_failure
                    if not yf_is_degraded():
                        try:
                            _t = yf.Ticker(symbol)
                            _info = _t.fast_info
                            _prev_close = float(_info.get("previousClose") or 0)
                            if _prev_close > 0 and price > 0:
                                _gap_pct = (price - _prev_close) / _prev_close * 100
                                _gap_in_dir = _gap_pct if direction == "long" else -_gap_pct
                                if _gap_in_dir > 5.0:
                                    quality_mult *= 0.85
                                    quality_notes.append(f"gap_chasing={_gap_in_dir:.1f}%")
                        except Exception:
                            try:
                                yf_record_failure()
                            except Exception:
                                pass
                except Exception:
                    pass  # fail-open
            except Exception:
                pass  # entire quality block fails open — no impact

            if quality_notes:
                logger.info(
                    f"QUALITY GATES {symbol}: mult={quality_mult:.2f} ({', '.join(quality_notes)})"
                )

            # Apply all multipliers: VIX, drawdown, overnight, circuit breaker, streak, timing, VaR, correlation, sector streak
            # SIZING FLOOR: clamp the product of all reducer multipliers
            # to >= POSITION_SIZE_MULT_FLOOR. Stacking 11 multipliers at
            # 0.7-0.8 each produced ~2% of nominal, leaving exposure
            # stuck at 1.6% even with 27 valid picks. The floor preserves
            # safety overrides (any single multiplier being 0 still
            # blocks the trade — these are checked separately above)
            # while preventing death-by-1000-cuts compounding.
            _reducer_product = (drawdown_multiplier * vix_multiplier *
                                overnight_size_mod * cb_multiplier * dd_multiplier *
                                streak_size_mod * timing_size_mod *
                                var_multiplier * corr_multiplier * sector_streak_mod *
                                quality_mult)
            _reducer_product = max(POSITION_SIZE_MULT_FLOOR, _reducer_product)
            position_value = total_value * size_pct * _reducer_product
            shares = round(position_value / price, 4)

            if shares * price > cash:
                # Not enough cash
                results["skipped"].append({
                    "symbol": symbol,
                    "reason": "Insufficient cash",
                })
                continue

            # AUTONOMOUS DECISION ENGINE — Machine decides per-stock
            # No more fixed percentages. Each stock gets its own stop loss
            # and profit target based on its actual volatility (ATR),
            # signal strength, regime, and trade type.
            is_mr = pick.get("mean_reversion", False) or pick.get("signal_type") == "MEAN_REVERSION"
            auto_decision = _autonomous_stop_and_target(
                symbol=symbol,
                direction=direction,
                price=price,
                regime=regime,
                confidence=pick.get("confidence", 50),
                composite_score=pick.get("composite_score", 0),
                sector=pick.get("sector", ""),
                is_mean_reversion=is_mr,
            )
            stop_loss = auto_decision["stop_loss"]
            target = auto_decision["target_price"]

            # MISTAKE LEARNING: Tighten stops if we've been holding losers too long
            if mistake_adj.get("tighten_stops"):
                stop_pct_adj = auto_decision["stop_pct"] / 100 * 0.7  # 30% tighter
                if direction == "long":
                    stop_loss = round(price * (1 - stop_pct_adj), 2)
                else:
                    stop_loss = round(price * (1 + stop_pct_adj), 2)

            # --- OPTIONS DECISION: Check if options are better than equity ---
            options_opened = False
            try:
                from predictions.options_engine import (
                    should_use_options, fetch_option_chain,
                    select_strike, select_expiration,
                )
                from predictions.models import get_options_exposure

                portfolio_for_opts = {
                    "total_value": total_value,
                    "cash": cash,
                }
                open_trades_for_opts = []
                for t in get_open_trades():
                    t_data = dict(t)
                    if t.get("ticker") == symbol:
                        # Add unrealized P&L for covered call evaluation
                        t_data["unrealized_pct"] = ((price - t["entry_price"]) / t["entry_price"] * 100) if t["direction"] == "long" else 0
                    open_trades_for_opts.append(t_data)

                opts_decision = should_use_options(
                    pick={
                        "ticker": symbol, "direction": direction,
                        "confidence": pick.get("confidence", 50),
                        "composite_score": pick.get("composite_score", 0),
                        "price": price, "entry_price": price,
                    },
                    regime=regime,
                    portfolio_state=portfolio_for_opts,
                    open_trades=open_trades_for_opts,
                )

                if opts_decision.get("use_options"):
                    strategy = opts_decision["strategy"]
                    chain_data = fetch_option_chain(symbol, max_expiries=3)

                    if chain_data:
                        # Determine option type from strategy
                        if strategy in ("buy_call", "sell_covered_call"):
                            opt_type = "call"
                        else:
                            opt_type = "put"

                        # Select strike
                        opt_dir = "long" if strategy.startswith("buy") else "short"
                        strike_info = select_strike(
                            chain_data, price, opt_type,
                            conviction=pick.get("confidence", 50),
                            composite_score=pick.get("composite_score", 0),
                        )

                        if strike_info and strike_info.get("premium", 0) > 0:
                            premium = strike_info["premium"]
                            # Position sizing: same dollar amount as equity trade
                            max_opts_cost = position_value  # Same $ as would use for equity
                            # Check options exposure limit (25% of portfolio)
                            opts_exp = get_options_exposure()
                            remaining_opts_budget = max(0, (total_value * 0.25) - opts_exp["total_premium_deployed"])
                            max_opts_cost = min(max_opts_cost, remaining_opts_budget)
                            # Single option trade limit: 5% of portfolio
                            max_opts_cost = min(max_opts_cost, total_value * 0.05)

                            if max_opts_cost > premium * 100:  # At least 1 contract worth
                                num_contracts = max(1, int(max_opts_cost / (premium * 100)))
                                total_cost = premium * num_contracts * 100

                                if total_cost <= cash:
                                    adaptive_hold = auto_decision["hold_days"]
                                    opt_trade_id = save_paper_trade(
                                        ticker=symbol,
                                        direction=opt_dir,
                                        entry_price=premium,
                                        shares=num_contracts,  # contracts stored in shares for compatibility
                                        signal_score=pick.get("composite_score", 0),
                                        regime=regime,
                                        factors={**(pick.get("factors", {})), "options_strategy": strategy, "options_rationale": opts_decision["rationale"]},
                                        stop_loss=round(premium * 0.5, 2),  # 50% premium stop
                                        target_price=round(premium * 2.0, 2),  # 100% premium target
                                        hold_days=adaptive_hold,
                                        sector=pick.get("sector", ""),
                                        hold_class="swing",
                                        instrument_type=opt_type,
                                        strike_price=strike_info["strike"],
                                        expiration_date=strike_info["expiry"],
                                        contracts=num_contracts,
                                        premium_per_contract=premium,
                                        underlying_price_at_entry=price,
                                        option_delta=strike_info.get("delta_est"),
                                        option_iv=strike_info.get("iv"),
                                    )

                                    cash = get_cash()
                                    open_tickers.add(symbol)
                                    current_positions += 1
                                    sector_counts[sector_key] = sector_counts.get(sector_key, 0) + 1
                                    options_opened = True

                                    results["opened"].append({
                                        "trade_id": opt_trade_id,
                                        "symbol": symbol,
                                        "direction": opt_dir,
                                        "instrument_type": opt_type,
                                        "strategy": strategy,
                                        "strike": strike_info["strike"],
                                        "expiry": strike_info["expiry"],
                                        "premium": premium,
                                        "contracts": num_contracts,
                                        "position_value": round(total_cost, 2),
                                        "confidence": pick["confidence"],
                                        "score": pick["composite_score"],
                                        "sector": pick.get("sector"),
                                        "auto_decision": opts_decision["rationale"],
                                    })
                                    _opt_label = "CALL" if opt_type == "call" else "PUT"
                                    logger.warning(f"OPTIONS TRADE: {_opt_label} — {strategy} {num_contracts}x {symbol} ${strike_info['strike']} @ ${premium:.2f}/contract (exp {strike_info['expiry']}) | Total: ${premium * num_contracts * 100:.0f}")
                    else:
                        logger.info(f"OPTIONS SKIP: No option chain available for {symbol} — falling through to equity")
            except Exception as e:
                logger.debug(f"Options decision failed for {symbol}: {e}")

            if options_opened:
                continue  # Skip equity execution — options trade was placed

            try:
                # AUTONOMOUS HOLD DURATION — decided by the machine per-stock
                adaptive_hold = auto_decision["hold_days"]

                trade_id = save_paper_trade(
                    ticker=symbol,
                    direction=direction,
                    entry_price=price,
                    shares=shares,
                    signal_score=pick.get("composite_score", 0),
                    regime=regime,
                    factors=pick.get("factors", {}),
                    stop_loss=stop_loss,
                    target_price=target,
                    hold_days=adaptive_hold,
                    sector=pick.get("sector", ""),
                    hold_class=auto_decision.get("hold_class", "swing"),
                )

                # Cash deducted atomically inside save_paper_trade()
                cash = get_cash()  # Refresh from DB
                open_tickers.add(symbol)
                current_positions += 1
                sector_counts[sector_key] = sector_counts.get(sector_key, 0) + 1

                results["opened"].append({
                    "trade_id": trade_id,
                    "symbol": symbol,
                    "direction": direction,
                    "instrument_type": "equity",
                    "price": price,
                    "shares": round(shares, 4),
                    "position_value": round(shares * price, 2),
                    "stop_loss": stop_loss,
                    "target": target,
                    "confidence": pick["confidence"],
                    "score": pick["composite_score"],
                    "sector": pick.get("sector"),
                    "auto_decision": auto_decision["reasoning"],
                })
            except Exception as e:
                results["errors"].append(f"Failed to open {symbol}: {str(e)}")

    # DIAGNOSTIC: if cycle finished with 0 opened AND 0 skipped, the user
    # has no visibility into WHY. Record an entry guard diagnostic so the
    # /api/auto-trading-status response shows a reason.
    if not results["opened"] and not results["skipped"]:
        results["skipped"].append({
            "symbol": "ENTRY_GUARD",
            "reason": (
                f"Entry guard or pick filter blocked all trades: "
                f"available_slots={available_slots}, "
                f"cash=${cash:.2f}, "
                f"timing.can_trade={timing.get('can_trade')}, "
                f"timing.window={timing.get('window')}, "
                f"regime={regime}, "
                f"long_picks_in={len(quant_picks.get('long_picks', []))}, "
                f"short_picks_in={len(quant_picks.get('short_picks', []))}"
            ),
        })
        logger.warning(
            f"TRADE EXECUTION: 0 opened, 0 skipped — diagnostic recorded. "
            f"slots={available_slots} cash=${cash:.2f} can_trade={timing.get('can_trade')} "
            f"window={timing.get('window')} regime={regime}"
        )

    # --- Step 3: Save portfolio snapshot ---
    try:
        # Use atomic cash — always accurate
        cash = get_cash()
        state = get_portfolio_state()
        positions_value = state["positions_value"]
        total_value = state["total_value"]  # Already uses atomic cash

        # Get S&P 500 performance for comparison
        # IMPORTANT: sp500_cum must be TRUE cumulative from inception, not 1-month rolling
        sp500_daily = 0
        sp500_cum = 0
        try:
            # Determine inception date (matches fund's actual start)
            try:
                from predictions.models import get_all_paper_trades as _get_all_trades
                _all_trades = _get_all_trades()
                if _all_trades:
                    _earliest = min(t.get("entry_date", "2026-01-01") for t in _all_trades)
                    _inception = _earliest[:10]
                else:
                    _inception = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
            except Exception:
                _inception = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

            _throttle()
            # Download from inception to today (not just last month!)
            sp_df = yf.download("^GSPC", start=_inception, progress=False)
            if sp_df is not None and len(sp_df) >= 2:
                sp_closes = _safe_col(sp_df, "Close").values.astype(float)
                sp500_daily = ((sp_closes[-1] / sp_closes[-2]) - 1) * 100
                # TRUE cumulative: from inception close to today
                sp500_cum = ((sp_closes[-1] / sp_closes[0]) - 1) * 100
        except Exception:
            pass

        # ROUTE THROUGH TRUTH ENGINE — bulletproof S&P 500 + fund value
        # validation, options-aware bounds, sp500 multi-source fallback,
        # carry-forward on yfinance failure, snapshot rejection on
        # balance mismatch. Falls back to legacy save on engine failure.
        try:
            from predictions.truth_engine import safe_save_snapshot as _safe_snap
            _r = _safe_snap()
            if not _r.get("ok"):
                # Truth engine rejected the snapshot — log + fall back to legacy
                results["errors"].append(
                    f"Truth engine rejected snapshot: {_r.get('action')} "
                    f"reason={_r.get('reason') or _r.get('cash')}"
                )
                raise RuntimeError("truth_engine_rejected")
        except Exception:
            # Legacy fallback — preserves prior sp500 from last snapshot
            # instead of writing 0 (which polluted the chart historically)
            prev_value = snapshots[-1]["total_value"] if snapshots else INITIAL_CAPITAL
            daily_return = ((total_value / prev_value) - 1) * 100 if prev_value > 0 else 0
            cum_return = ((total_value / ORIGINAL_CAPITAL) - 1) * 100
            # Carry-forward sp500 if today's fetch returned 0
            if sp500_cum == 0 and snapshots:
                sp500_cum = float(snapshots[-1].get("sp500_cumulative_return_pct") or 0)
                sp500_daily = 0.0
            save_portfolio_snapshot(
                total_value=round(total_value, 2),
                cash=round(cash, 2),
                positions_value=round(positions_value, 2),
                daily_ret=round(daily_return, 2),
                cum_ret=round(cum_return, 2),
                sp500_daily=round(sp500_daily, 2),
                sp500_cum=round(sp500_cum, 2),
                num_pos=current_positions,
            )
    except Exception as e:
        results["errors"].append(f"Snapshot save failed: {str(e)}")

    results["portfolio_after"] = {
        "cash": round(cash, 2),
        "num_positions": current_positions,
        "regime": regime,
    }

    # ── IBKR Dual-Track: Mirror paper trades 1:1 (scaled to real account) ──
    # Paper trades ALWAYS run above. IBKR mirrors them proportionally so
    # whatever % of paper portfolio is bought, same % of IBKR account is bought.
    # User's account value auto-scales (deposits/withdrawals adjust sizing).
    try:
        from predictions.ibkr_adapter import IBKR_ENABLED, ibkr_execute_trades
        if IBKR_ENABLED:
            import threading
            # Snapshot the paper trades we just opened + portfolio value for scaling
            _paper_opened = list(results.get("opened", []))

            # ── STRATEGY FILTERS — applied BEFORE mirroring to IBKR ──
            # Reduces whipsaw, blocks rapid-fire opens, gates options to
            # liquid windows. Paper still fired these trades; IBKR is the
            # filtered slice that reaches real money.
            try:
                from predictions.strategy_filters import (
                    apply_all_filters, record_sector_trade, record_open,
                )
                _filtered_opens = []
                _filter_blocked = []
                for _trade in _paper_opened:
                    _result = apply_all_filters(_trade)
                    if _result["allow"]:
                        # Apply confidence adjustment so downstream sizing is correct
                        _trade["confidence"] = _result["adjusted_confidence"]
                        _filtered_opens.append(_trade)
                        record_sector_trade(_trade.get("sector", ""), _trade.get("direction", ""))
                        record_open(_trade.get("symbol", _trade.get("ticker", "")))
                    else:
                        _filter_blocked.append({
                            "symbol": _trade.get("symbol", _trade.get("ticker")),
                            "blocked_by": _result["blocking_filter"],
                            "reasons": _result["reasons"],
                        })
                if _filter_blocked:
                    logger.warning(
                        f"STRATEGY FILTERS blocked {len(_filter_blocked)}/{len(_paper_opened)} "
                        f"trades from IBKR mirror: {[b['symbol'] for b in _filter_blocked]}"
                    )
                _paper_opened = _filtered_opens
            except ImportError:
                pass  # filters module not available — proceed with all trades
            except Exception as _fe:
                logger.error(f"Strategy filter error (proceeding without filter): {_fe}")

            # Fresh portfolio value after trades
            try:
                _paper_portfolio_value = get_portfolio_state().get("total_value", ORIGINAL_CAPITAL)
            except Exception:
                _paper_portfolio_value = get_cash() + positions_value
            def _ibkr_mirror():
                try:
                    ibkr_result = ibkr_execute_trades(
                        quant_picks,
                        paper_opened=_paper_opened,
                        paper_portfolio_value=_paper_portfolio_value,
                    )
                    logger.warning(f"IBKR MIRROR: opened={len(ibkr_result.get('opened', []))} "
                               f"skipped={len(ibkr_result.get('skipped', []))} "
                               f"errors={len(ibkr_result.get('errors', []))} "
                               f"scale={ibkr_result.get('scale_factor')} "
                               f"live_acct=${ibkr_result.get('live_account_value', 0):.0f}")
                except Exception as e:
                    logger.error(f"IBKR dual-track error: {e}")
            t = threading.Thread(target=_ibkr_mirror, daemon=True, name="ibkr-mirror")
            t.start()
    except ImportError:
        pass  # ib_insync not installed — paper-only mode
    except Exception as e:
        logger.error(f"IBKR mirror setup error: {e}")

    return results


# ============================================================
#  RAPID BACKTESTING — Historical Simulated Trades
# ============================================================

def run_backtest(days_back: int = 180, num_trades_target: int = 500) -> dict:
    """
    Rapid backtesting — simulate hundreds of trades using historical data
    to immediately populate the learning system with win/loss history.

    Strategy:
      For each trading day in the lookback period:
        1. Calculate RSI(2) for each stock
        2. If RSI(2) < 10 and price > 200-SMA → BUY (Connors strategy)
        3. Exit when RSI(2) > 70 or after 5 trading days (whichever first)
        4. Record outcome as win/loss

    This is the Connors RSI(2) mean-reversion strategy which has
    a documented 75-91% win rate historically.

    We also test momentum and value factors for comparison.

    Args:
        days_back: how many calendar days of history to test
        num_trades_target: approximate number of trades to generate

    Returns:
        dict with trade results, factor performance, overall stats
    """
    from predictions.models import save_paper_trade, close_paper_trade

    logger.info(f"Starting backtest: {days_back} days, target {num_trades_target} trades")
    start_time = time.time()

    # Use a subset of liquid stocks for backtesting
    backtest_symbols = [
        "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA",
        "JPM", "V", "UNH", "JNJ", "XOM", "HD", "PG", "BA",
        "CRM", "AMD", "NFLX", "WMT", "GS", "CAT", "LLY",
        "MRK", "COST", "CVX", "MA", "ABBV",
    ]

    # Batch download historical data
    _throttle()
    try:
        period = "2y" if days_back > 365 else "1y"
        df = yf.download(
            backtest_symbols, period=period, progress=False, group_by="ticker"
        )
    except Exception as e:
        return {"error": f"Download failed: {e}", "trades": []}

    if df is None or df.empty:
        return {"error": "No data available", "trades": []}

    all_trades = []
    factor_stats = {
        "rsi2_mean_reversion": {"wins": 0, "losses": 0, "returns": []},
        "momentum": {"wins": 0, "losses": 0, "returns": []},
        "mean_reversion_value": {"wins": 0, "losses": 0, "returns": []},
    }

    for symbol in backtest_symbols:
        try:
            if isinstance(df.columns, pd.MultiIndex):
                if symbol not in df.columns.get_level_values(0):
                    continue
                sym_df = df[symbol].dropna(how="all")
            else:
                continue

            if len(sym_df) < 250:
                continue

            closes = _safe_col(sym_df, "Close").values.astype(float)
            dates = sym_df.index

            # Scan through history looking for trade setups
            for i in range(210, len(closes) - 10):
                # Only backtest within the requested window
                try:
                    trade_date = dates[i]
                    if hasattr(trade_date, 'date'):
                        td = trade_date.date()
                    else:
                        td = pd.Timestamp(trade_date).date()
                    cutoff = datetime.now().date() - timedelta(days=days_back)
                    if td < cutoff:
                        continue
                except Exception:
                    continue

                # --- Strategy 1: RSI(2) Mean Reversion ---
                if i >= 3:
                    deltas = closes[i-2:i+1] - closes[i-3:i]
                    gain = float(np.sum(np.maximum(deltas, 0)))
                    loss = float(np.sum(np.maximum(-deltas, 0)))
                    rsi2 = 100 - (100 / (1 + (gain / (loss + 1e-10))))

                    # 200-SMA filter
                    sma200 = float(np.mean(closes[i-200:i]))
                    current = closes[i]

                    if rsi2 < 10 and current > sma200:
                        # Entry signal! Find exit
                        entry_price = closes[i + 1] if i + 1 < len(closes) else current  # buy next day open approx
                        exit_price = entry_price
                        exit_day = i + 1
                        hold_days = 0

                        # Exit when RSI(2) > 70 or after 5 days
                        for j in range(i + 2, min(i + 7, len(closes))):
                            d = closes[j-2:j+1] - closes[j-3:j]
                            g = float(np.sum(np.maximum(d, 0)))
                            l = float(np.sum(np.maximum(-d, 0)))
                            exit_rsi = 100 - (100 / (1 + (g / (l + 1e-10))))
                            hold_days = j - i - 1
                            if exit_rsi > 70 or hold_days >= 5:
                                exit_price = closes[j]
                                exit_day = j
                                break
                        else:
                            if i + 6 < len(closes):
                                exit_price = closes[i + 6]
                                hold_days = 5

                        ret_pct = ((exit_price / entry_price) - 1) * 100
                        is_win = ret_pct > 0

                        factor_stats["rsi2_mean_reversion"]["returns"].append(ret_pct)
                        if is_win:
                            factor_stats["rsi2_mean_reversion"]["wins"] += 1
                        else:
                            factor_stats["rsi2_mean_reversion"]["losses"] += 1

                        all_trades.append({
                            "symbol": symbol,
                            "strategy": "rsi2_mean_reversion",
                            "direction": "long",
                            "entry_price": round(float(entry_price), 2),
                            "exit_price": round(float(exit_price), 2),
                            "return_pct": round(float(ret_pct), 2),
                            "hold_days": hold_days,
                            "is_win": is_win,
                            "entry_date": str(td),
                            "rsi2_at_entry": round(rsi2, 1),
                        })

                # --- Strategy 2: Momentum (buy winners) ---
                # Every 20 trading days, check 60-day momentum
                if i % 20 == 0 and i >= 60:
                    mom_60d = ((closes[i] / closes[i - 60]) - 1) * 100
                    if mom_60d > 10:  # Strong momentum
                        entry_price = closes[i + 1] if i + 1 < len(closes) else closes[i]
                        # Hold for 20 trading days
                        exit_idx = min(i + 21, len(closes) - 1)
                        exit_price = closes[exit_idx]
                        ret_pct = ((exit_price / entry_price) - 1) * 100

                        is_win = ret_pct > 0
                        factor_stats["momentum"]["returns"].append(ret_pct)
                        if is_win:
                            factor_stats["momentum"]["wins"] += 1
                        else:
                            factor_stats["momentum"]["losses"] += 1

                        all_trades.append({
                            "symbol": symbol,
                            "strategy": "momentum",
                            "direction": "long",
                            "entry_price": round(float(entry_price), 2),
                            "exit_price": round(float(exit_price), 2),
                            "return_pct": round(float(ret_pct), 2),
                            "hold_days": min(20, exit_idx - i),
                            "is_win": is_win,
                            "entry_date": str(td),
                            "momentum_60d": round(mom_60d, 1),
                        })

                # --- Strategy 3: Mean Reversion / Value ---
                # Buy stocks that have dropped > 10% from 60-day high
                if i % 10 == 5 and i >= 60:
                    high_60d = float(np.max(closes[i-60:i]))
                    drawdown = ((closes[i] / high_60d) - 1) * 100

                    if drawdown < -10 and closes[i] > float(np.mean(closes[i-200:i])):
                        # Dropped 10%+ but still above 200-SMA (not in freefall)
                        entry_price = closes[i + 1] if i + 1 < len(closes) else closes[i]
                        exit_idx = min(i + 16, len(closes) - 1)
                        exit_price = closes[exit_idx]
                        ret_pct = ((exit_price / entry_price) - 1) * 100

                        is_win = ret_pct > 0
                        factor_stats["mean_reversion_value"]["returns"].append(ret_pct)
                        if is_win:
                            factor_stats["mean_reversion_value"]["wins"] += 1
                        else:
                            factor_stats["mean_reversion_value"]["losses"] += 1

                        all_trades.append({
                            "symbol": symbol,
                            "strategy": "mean_reversion_value",
                            "direction": "long",
                            "entry_price": round(float(entry_price), 2),
                            "exit_price": round(float(exit_price), 2),
                            "return_pct": round(float(ret_pct), 2),
                            "hold_days": min(15, exit_idx - i),
                            "is_win": is_win,
                            "entry_date": str(td),
                            "drawdown_pct": round(drawdown, 1),
                        })

                # Early exit if we have enough trades
                if len(all_trades) >= num_trades_target:
                    break

        except Exception as e:
            logger.debug(f"Backtest failed for {symbol}: {e}")
            continue

        if len(all_trades) >= num_trades_target:
            break

    # --- Calculate overall statistics ---
    if all_trades:
        returns = [t["return_pct"] for t in all_trades]
        wins = [r for r in returns if r > 0]
        losses = [r for r in returns if r <= 0]
        win_rate = len(wins) / len(returns) * 100
        avg_return = float(np.mean(returns))
        avg_win = float(np.mean(wins)) if wins else 0
        avg_loss = float(np.mean(losses)) if losses else 0

        # Sharpe ratio (annualized, assuming ~252 trading days / avg hold period)
        avg_hold = float(np.mean([t["hold_days"] for t in all_trades]))
        trades_per_year = 252 / max(1, avg_hold)
        if np.std(returns) > 0:
            sharpe = (avg_return / float(np.std(returns))) * np.sqrt(trades_per_year)
        else:
            sharpe = 0

        # Max drawdown
        cumulative = np.cumsum(returns)
        running_max = np.maximum.accumulate(cumulative)
        drawdowns = cumulative - running_max
        max_drawdown = float(np.min(drawdowns)) if len(drawdowns) > 0 else 0

        # Profit factor
        total_gains = sum(wins) if wins else 0
        total_losses = abs(sum(losses)) if losses else 0.01
        profit_factor = total_gains / total_losses
    else:
        win_rate = 0
        avg_return = 0
        avg_win = 0
        avg_loss = 0
        sharpe = 0
        max_drawdown = 0
        profit_factor = 0

    # Factor-level statistics
    factor_results = {}
    for factor_name, stats in factor_stats.items():
        total = stats["wins"] + stats["losses"]
        if total > 0:
            rets = stats["returns"]
            factor_results[factor_name] = {
                "total_trades": total,
                "win_rate": round(stats["wins"] / total * 100, 1),
                "avg_return": round(float(np.mean(rets)), 2),
                "best_trade": round(float(max(rets)), 2) if rets else 0,
                "worst_trade": round(float(min(rets)), 2) if rets else 0,
                "sharpe": round(
                    float(np.mean(rets) / (np.std(rets) + 1e-10)) * np.sqrt(trades_per_year), 2
                ),
            }

    elapsed = round(time.time() - start_time, 1)

    return {
        "total_trades": len(all_trades),
        "win_rate": round(win_rate, 1),
        "avg_return_pct": round(avg_return, 2),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "sharpe_ratio": round(sharpe, 2),
        "max_drawdown_pct": round(max_drawdown, 2),
        "profit_factor": round(profit_factor, 2),
        "factor_results": factor_results,
        "trades_sample": all_trades[:50],  # First 50 for display
        "computation_time_seconds": elapsed,
        "period_days": days_back,
        "symbols_tested": len(backtest_symbols),
    }


# ============================================================
#  PERFORMANCE ANALYTICS
# ============================================================

def get_performance_analytics() -> dict:
    """
    Comprehensive performance analytics for the paper trading portfolio.

    Calculates:
      - Sharpe ratio (annualized)
      - Max drawdown
      - Win rate by sector, by regime, by direction
      - Equity curve data for charting
      - Comparison vs S&P 500
    """
    from predictions.models import get_closed_trades, get_portfolio_snapshots

    closed = get_closed_trades(limit=500)
    snapshots = get_portfolio_snapshots(days=365)

    if not closed and not snapshots:
        return {
            "message": "No trading history yet. Run a backtest or wait for live trades.",
            "has_data": False,
        }

    result = {"has_data": True}

    # --- Overall stats ---
    if closed:
        returns = [t.get("pnl_pct", 0) or 0 for t in closed]
        wins = [r for r in returns if r > 0]
        losses = [r for r in returns if r <= 0]

        result["overall"] = {
            "total_trades": len(closed),
            "win_rate": round(len(wins) / len(returns) * 100, 1) if returns else 0,
            "avg_return": round(float(np.mean(returns)), 2),
            "avg_win": round(float(np.mean(wins)), 2) if wins else 0,
            "avg_loss": round(float(np.mean(losses)), 2) if losses else 0,
            "best_trade": round(float(max(returns)), 2) if returns else 0,
            "worst_trade": round(float(min(returns)), 2) if returns else 0,
            "total_pnl": round(sum(t.get("pnl_dollars", 0) or 0 for t in closed), 2),
        }

        # Sharpe ratio — using ACTUAL average holding period, not hardcoded
        if len(returns) >= 5 and np.std(returns) > 0:
            # Calculate actual average holding period from closed trades
            hold_days_list = []
            for t in closed:
                try:
                    entry_d = datetime.fromisoformat(t.get("entry_date", ""))
                    exit_d = datetime.fromisoformat(t.get("exit_date", ""))
                    hd = max(1, (exit_d - entry_d).days)
                    hold_days_list.append(hd)
                except Exception:
                    pass
            avg_hold = float(np.mean(hold_days_list)) if hold_days_list else 10
            avg_hold = max(1, avg_hold)  # Safety floor

            trades_per_year = 252 / avg_hold
            # Risk-free rate adjustment (annualized ~5% / trades_per_year)
            rf_per_trade = 0.05 / trades_per_year
            excess_returns = [r / 100 - rf_per_trade for r in returns]  # Convert % to decimal
            sharpe = (np.mean(excess_returns) / np.std(excess_returns)) * np.sqrt(trades_per_year)
            result["overall"]["sharpe_ratio"] = round(float(sharpe), 2)
            result["overall"]["avg_hold_days"] = round(avg_hold, 1)
        else:
            result["overall"]["sharpe_ratio"] = 0
            result["overall"]["avg_hold_days"] = 0

        # Sortino ratio — proper downside deviation (uses 0 for positive returns)
        if len(returns) >= 5:
            avg_hold = result["overall"].get("avg_hold_days", 10) or 10
            trades_per_year = 252 / max(1, avg_hold)
            rf_per_trade = 0.05 / trades_per_year
            all_excess = [r / 100 - rf_per_trade for r in returns]
            # Proper downside deviation: min(0, excess_return)^2 for ALL trades
            downside_diff = [min(0, r) for r in all_excess]
            downside_dev = float(np.sqrt(np.mean([d**2 for d in downside_diff])))
            if downside_dev > 0:
                sortino = (np.mean(all_excess) / downside_dev) * np.sqrt(trades_per_year)
                result["overall"]["sortino_ratio"] = round(float(sortino), 2)
            else:
                # No downside = infinite sortino, cap at 99
                result["overall"]["sortino_ratio"] = 99.0 if np.mean(returns) > 0 else 0
        else:
            result["overall"]["sortino_ratio"] = 0

        # Trades per day
        first_date = None
        for t in closed:
            ed = t.get("entry_date", "")
            if ed:
                try:
                    d = datetime.fromisoformat(ed).strftime("%Y-%m-%d")
                    if first_date is None or d < first_date:
                        first_date = d
                except Exception:
                    pass
        if first_date:
            try:
                start = datetime.strptime(first_date, "%Y-%m-%d")
                trading_days = max(1, int(np.busday_count(start.date(), datetime.now().date())))
                result["overall"]["trades_per_day"] = round(len(closed) / trading_days, 1)
                result["overall"]["trading_days_active"] = trading_days
            except Exception:
                result["overall"]["trades_per_day"] = 0
                result["overall"]["trading_days_active"] = 0
        else:
            result["overall"]["trades_per_day"] = 0
            result["overall"]["trading_days_active"] = 0

        # Profit factor
        total_gains = sum(wins) if wins else 0
        total_losses_abs = abs(sum(losses)) if losses else 0.01
        result["overall"]["profit_factor"] = round(total_gains / total_losses_abs, 2)

        # --- Win rate by sector ---
        sector_stats = {}
        for trade in closed:
            sector = trade.get("sector") or "Unknown"
            if sector not in sector_stats:
                sector_stats[sector] = {"wins": 0, "total": 0, "returns": []}
            sector_stats[sector]["total"] += 1
            pnl = trade.get("pnl_pct", 0) or 0
            sector_stats[sector]["returns"].append(pnl)
            if pnl > 0:
                sector_stats[sector]["wins"] += 1

        result["by_sector"] = {
            sector: {
                "win_rate": round(s["wins"] / s["total"] * 100, 1),
                "total_trades": s["total"],
                "avg_return": round(float(np.mean(s["returns"])), 2),
            }
            for sector, s in sector_stats.items()
            if s["total"] >= 3  # minimum sample size
        }

        # --- Win rate by regime ---
        regime_stats = {}
        for trade in closed:
            regime = trade.get("regime_at_entry") or "Unknown"
            if regime not in regime_stats:
                regime_stats[regime] = {"wins": 0, "total": 0, "returns": []}
            regime_stats[regime]["total"] += 1
            pnl = trade.get("pnl_pct", 0) or 0
            regime_stats[regime]["returns"].append(pnl)
            if pnl > 0:
                regime_stats[regime]["wins"] += 1

        result["by_regime"] = {
            regime: {
                "win_rate": round(s["wins"] / s["total"] * 100, 1),
                "total_trades": s["total"],
                "avg_return": round(float(np.mean(s["returns"])), 2),
            }
            for regime, s in regime_stats.items()
            if s["total"] >= 3
        }

        # --- Win rate by direction ---
        long_trades = [t for t in closed if t.get("direction") == "long"]
        short_trades = [t for t in closed if t.get("direction") == "short"]

        if long_trades:
            long_rets = [(t.get("pnl_pct", 0) or 0) for t in long_trades]
            result["long_stats"] = {
                "total": len(long_trades),
                "win_rate": round(sum(1 for r in long_rets if r > 0) / len(long_rets) * 100, 1),
                "avg_return": round(float(np.mean(long_rets)), 2),
            }

        if short_trades:
            short_rets = [(t.get("pnl_pct", 0) or 0) for t in short_trades]
            result["short_stats"] = {
                "total": len(short_trades),
                "win_rate": round(sum(1 for r in short_rets if r > 0) / len(short_rets) * 100, 1),
                "avg_return": round(float(np.mean(short_rets)), 2),
            }

    # --- Equity curve ---
    if snapshots:
        result["equity_curve"] = [{
            "date": s["snapshot_date"],
            "portfolio_value": s["total_value"],
            "portfolio_return": s.get("cumulative_return_pct", 0),
            "sp500_return": s.get("sp500_cumulative_return_pct", 0),
            "num_positions": s.get("num_positions", 0),
        } for s in snapshots]

        # Max drawdown from equity curve
        values = [s["total_value"] for s in snapshots]
        if len(values) >= 2:
            peak = values[0]
            max_dd = 0
            for v in values:
                peak = max(peak, v)
                dd = ((v / peak) - 1) * 100
                max_dd = min(max_dd, dd)
            result["max_drawdown_pct"] = round(max_dd, 2)

    # --- Benchmarking vs S&P 500 (with robust caching + last-known-good fallback) ---
    try:
        from predictions.models import get_all_paper_trades
        # PERSISTENT INCEPTION DATE — survives portfolio resets so S&P comparison
        # window stays meaningful. Stored in a flag file on first run, never changes.
        # Must be >= 60 days back for a meaningful S&P comparison window.
        import os as _os_inception
        _inception_flag = _os_inception.path.join(_os_inception.path.dirname(__file__), ".fund_inception_date")
        _min_inception_date = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")
        try:
            if _os_inception.path.exists(_inception_flag):
                with open(_inception_flag) as _f:
                    inception_date = _f.read().strip()[:10]
            else:
                # First time: derive from earliest trade, but enforce min 60 days back
                try:
                    all_trades = get_all_paper_trades()
                    if all_trades:
                        earliest = min(t.get("entry_date", "2026-01-01") for t in all_trades)
                        candidate = earliest[:10]
                        # If earliest trade is < 60 days ago, use 180 days back instead
                        candidate_dt = datetime.strptime(candidate, "%Y-%m-%d")
                        if (datetime.now() - candidate_dt).days < 60:
                            inception_date = _min_inception_date
                        else:
                            inception_date = candidate
                    else:
                        inception_date = _min_inception_date
                except Exception:
                    inception_date = _min_inception_date
                # Persist it forever — never changes again, even after portfolio reset
                try:
                    with open(_inception_flag, "w") as _f:
                        _f.write(inception_date)
                    logger.warning(f"FUND INCEPTION FIXED: {inception_date} (persists across portfolio resets — meaningful S&P window)")
                except Exception:
                    pass
        except Exception:
            inception_date = _min_inception_date

        sharpe_start = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")

        # Check cache first — only refresh if stale
        now_t = time.time()
        cache_fresh = (
            _benchmark_cache["sp_closes"] is not None and
            _benchmark_cache["inception"] == inception_date and
            (now_t - _benchmark_cache["time"]) < _BENCHMARK_CACHE_TTL
        )

        if not cache_fresh:
            sp_closes, sharpe_closes = _fetch_sp500_data(inception_date, sharpe_start)
            if sp_closes is not None and len(sp_closes) >= 2:
                # Cache the fresh data
                with _benchmark_lock:
                    _benchmark_cache["sp_closes"] = sp_closes
                    _benchmark_cache["sharpe_closes"] = sharpe_closes
                    _benchmark_cache["inception"] = inception_date
                    _benchmark_cache["time"] = now_t
            elif _benchmark_cache["sp_closes"] is not None:
                # Fetch failed but we have last-known-good — use it
                sp_closes = _benchmark_cache["sp_closes"]
                sharpe_closes = _benchmark_cache["sharpe_closes"]
                logger.warning("Using stale benchmark cache — fresh fetch failed")
            else:
                # No cache, fetch failed — will fall through to except
                raise RuntimeError("S&P 500 download failed and no cached data available")
        else:
            sp_closes = _benchmark_cache["sp_closes"]
            sharpe_closes = _benchmark_cache["sharpe_closes"]

        # Compute benchmark metrics from cached closes
        sp_sharpe = 0.0
        sp_total_return = 0.0
        if sp_closes is not None and len(sp_closes) >= 2:
            sp_total_return = ((sp_closes[-1] / sp_closes[0]) - 1) * 100

        if sharpe_closes is not None and len(sharpe_closes) >= 30:
            sharpe_returns = np.diff(sharpe_closes) / sharpe_closes[:-1]
            sharpe_returns = sharpe_returns[~np.isnan(sharpe_returns)]
            if len(sharpe_returns) >= 30:
                std = np.std(sharpe_returns, ddof=1)
                if std > 1e-10:
                    sp_sharpe = (np.mean(sharpe_returns) / std) * np.sqrt(252)

        fund_sharpe = result.get("overall", {}).get("sharpe_ratio", 0) or 0

        # Alpha = our return - benchmark return
        portfolio_state = get_portfolio_state()
        our_total = ((portfolio_state.get("total_value", ORIGINAL_CAPITAL) / ORIGINAL_CAPITAL) - 1) * 100

        benchmark_result = {
            "sp500_return_pct": round(float(sp_total_return), 2),
            "sp500_sharpe": round(float(sp_sharpe), 2),
            "fund_return_pct": round(our_total, 2),
            "fund_sharpe": round(float(fund_sharpe), 2),
            "alpha_pct": round(our_total - float(sp_total_return), 2),
            "sharpe_edge": round(float(fund_sharpe) - float(sp_sharpe), 2),
            "period": "Since Inception",
        }
        result["benchmark"] = benchmark_result
        # Save last successful benchmark dict
        with _benchmark_lock:
            _benchmark_cache["data"] = benchmark_result

    except Exception as e:
        logger.error(f"Benchmark calculation FAILED: {type(e).__name__}: {e}")
        # Last-resort fallback: use previously cached benchmark dict if available
        try:
            portfolio_state = get_portfolio_state()
            our_total = ((portfolio_state.get("total_value", ORIGINAL_CAPITAL) / ORIGINAL_CAPITAL) - 1) * 100
            fund_sharpe = result.get("overall", {}).get("sharpe_ratio", 0) or 0
        except Exception:
            our_total = 0
            fund_sharpe = 0

        if _benchmark_cache.get("data"):
            # Use last known good values but update fund numbers to current
            cached = dict(_benchmark_cache["data"])
            cached["fund_return_pct"] = round(our_total, 2)
            cached["fund_sharpe"] = round(float(fund_sharpe), 2)
            cached["alpha_pct"] = round(our_total - cached.get("sp500_return_pct", 0), 2)
            cached["sharpe_edge"] = round(float(fund_sharpe) - cached.get("sp500_sharpe", 0), 2)
            cached["period"] = "Since Inception (cached)"
            result["benchmark"] = cached
        else:
            result["benchmark"] = {
                "sp500_return_pct": 0, "sp500_sharpe": 0,
                "fund_return_pct": round(our_total, 2), "fund_sharpe": round(float(fund_sharpe), 2),
                "alpha_pct": 0, "sharpe_edge": 0, "period": "Loading…",
            }

    result["timestamp"] = datetime.now().isoformat()
    return result


# ============================================================
#  STANDALONE EXIT CHECKER
#  Runs independently of entry logic — checks stops/targets
#  every cycle even when no new trades are planned.
# ============================================================

def check_and_exit_positions(regime: str = "SIDEWAYS") -> dict:
    """
    Check all open positions for exit conditions WITHOUT generating new picks.
    This decouples exit management from entry decisions so stop-losses always fire.
    Also handles dynamic WIN-LOCK: system decides its own profit-lock threshold based on VIX + regime.
    Returns dict with list of closed positions.
    """
    from predictions.models import get_open_trades, close_paper_trade, get_portfolio_snapshots, save_portfolio_snapshot, get_cash

    open_trades = get_open_trades()
    if not open_trades:
        return {"closed": [], "checked": 0}

    exit_symbols = list(set(t["ticker"] for t in open_trades))
    current_prices = _get_current_prices(exit_symbols)

    if not current_prices:
        return {"closed": [], "checked": len(open_trades), "error": "Could not fetch prices"}

    closed = []
    snapshots = get_portfolio_snapshots(days=5)
    cash = get_cash()  # Atomic cash — always accurate

    # --- DYNAMIC WIN-LOCK CHECK: system decides threshold based on VIX + regime ---
    positions_val = sum(
        current_prices.get(t["ticker"], t["entry_price"]) * t["shares"]
        for t in open_trades
    )
    total_now = cash + positions_val
    yesterday_val = snapshots[-1]["total_value"] if snapshots else INITIAL_CAPITAL
    daily_gain = ((total_now / yesterday_val) - 1) * 100

    # Let the system decide its own WIN-LOCK threshold
    winlock = _get_dynamic_winlock(regime)
    winlock_threshold = winlock["lock_pct"]

    if daily_gain >= winlock_threshold:
        # SELECTIVE WIN-LOCK: only sell intraday + near-target + losing positions
        logger.warning(f"EXIT CHECKER WIN-LOCK: Up {daily_gain:+.1f}% (threshold: {winlock_threshold}%) — SELECTIVE SELL | {winlock['reason']}")
        kept_count = 0
        for trade in open_trades:
            ticker = trade["ticker"]
            price = current_prices.get(ticker)
            if not price:
                continue
            direction = trade["direction"]
            entry_price = trade["entry_price"]
            target = trade.get("target_price", entry_price * 1.05)
            hold_class = trade.get("hold_class", "swing")

            if direction == "long":
                pnl_pct = ((price / entry_price) - 1) * 100
                target_achieved = (price - entry_price) / (target - entry_price + 0.01) if target > entry_price else 1.0
            else:
                pnl_pct = ((entry_price / price) - 1) * 100
                target_achieved = (entry_price - price) / (entry_price - target + 0.01) if entry_price > target else 1.0

            should_sell = (
                hold_class == "intraday" or
                target_achieved >= 0.8 or
                pnl_pct < 0
            )

            if should_sell:
                try:
                    _smart_close_trade(trade, price)  # auto-handles options
                    closed.append({
                        "ticker": ticker, "direction": direction,
                        "entry_price": entry_price, "exit_price": round(price, 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "reason": f"WIN-LOCK selective: daily +{daily_gain:.1f}% | class={hold_class} target={target_achieved:.0%}",
                    })
                except Exception as e:
                    logger.error(f"WIN-LOCK close {ticker}: {e}")
            else:
                kept_count += 1
                logger.info(f"WIN-LOCK HOLD: Keeping {ticker} ({hold_class}, {pnl_pct:+.1f}%, target {target_achieved:.0%})")

        cash = get_cash()
        remaining_positions = len(get_open_trades())
        positions_val_after = sum(
            current_prices.get(t["ticker"], t["entry_price"]) * t["shares"]
            for t in get_open_trades()
        )
        total_value = cash + positions_val_after
        cum_ret = ((total_value / ORIGINAL_CAPITAL) - 1) * 100
        # ROUTE THROUGH TRUTH ENGINE — bulletproof sp500 + carry-forward
        try:
            from predictions.truth_engine import safe_save_snapshot as _safe_snap_wl
            _safe_snap_wl()
        except Exception:
            save_portfolio_snapshot(total_value, cash, remaining_positions, daily_gain, cum_ret, 0, 0, 0)
        return {"closed": closed, "checked": len(open_trades), "kept": kept_count, "win_lock": True, "winlock_info": winlock}

    # Cache VIX once per cycle for the per-position win-lock
    _winlock_vix = _cached_vix_for_winlock()

    for trade in open_trades:
        ticker = trade["ticker"]
        current_price = current_prices.get(ticker)
        if current_price is None:
            continue

        entry_price = trade["entry_price"]
        direction = trade["direction"]
        instrument_type = trade.get("instrument_type") or "equity"
        should_close = False
        close_reason = ""

        # ============================================================
        # PER-POSITION DYNAMIC WIN-LOCK (highest priority — runs FIRST)
        # ============================================================
        # If a single position has gained more than the statistically-
        # expected window move (k_sigma * VIX-derived volatility *
        # leverage) OR has hit risk-multiple target faster than half
        # the planned hold window, lock the win immediately. Thresholds
        # are 100% derived from VIX, regime, hold-time and the trade's
        # own risk profile — never hardcoded.
        try:
            # Compute provisional pnl for the win-lock check
            if instrument_type in ("call", "put"):
                # For options use premium-based pnl when available
                _entry_prem = trade.get("premium_per_contract") or entry_price
                _wl_pnl = 0.0
                try:
                    from predictions.options_engine import get_current_premium
                    _strike = trade.get("strike_price", 0)
                    _exp = trade.get("expiration_date", "")
                    _cur_prem = get_current_premium(ticker, _strike, _exp, instrument_type)
                    if _cur_prem and _cur_prem > 0 and _entry_prem and _entry_prem > 0:
                        if direction == "long":
                            _wl_pnl = ((_cur_prem / _entry_prem) - 1) * 100
                        else:
                            _wl_pnl = ((_entry_prem / _cur_prem) - 1) * 100
                except Exception:
                    _wl_pnl = 0.0
            else:
                if direction == "long":
                    _wl_pnl = ((current_price / entry_price) - 1) * 100
                else:
                    _wl_pnl = ((entry_price / current_price) - 1) * 100

            _wl_should, _wl_reason = _per_position_quick_profit_lock(
                trade, _wl_pnl, vix=_winlock_vix, regime=regime
            )
            if _wl_should:
                try:
                    _smart_close_trade(trade, current_price)
                    closed.append({
                        "ticker": ticker, "direction": direction,
                        "instrument_type": instrument_type,
                        "entry_price": entry_price,
                        "exit_price": round(current_price, 2),
                        "pnl_pct": round(_wl_pnl, 2),
                        "reason": _wl_reason,
                    })
                    logger.warning(_wl_reason)
                except Exception as _e:
                    logger.error(f"PER-POS WIN-LOCK close {ticker} failed: {_e}")
                continue  # Move to next trade — this one is closed

            # Mirror cutter: dynamic, self-thinking loss-cut
            # Only fires on losses; uses 5 live signals (win-rate, drawdown,
            # sector streak, VIX level, time-in-trade) to decide patience.
            _lc_should, _lc_reason = _per_position_quick_loss_cut(
                trade, _wl_pnl, vix=_winlock_vix, regime=regime
            )
            if _lc_should:
                try:
                    _smart_close_trade(trade, current_price)
                    closed.append({
                        "ticker": ticker, "direction": direction,
                        "instrument_type": instrument_type,
                        "entry_price": entry_price,
                        "exit_price": round(current_price, 2),
                        "pnl_pct": round(_wl_pnl, 2),
                        "reason": _lc_reason,
                    })
                    logger.warning(_lc_reason)
                except Exception as _e:
                    logger.error(f"PER-POS LOSS-CUT close {ticker} failed: {_e}")
                continue
        except Exception as _e:
            logger.debug(f"per-position winlock/losscut soft-fail {ticker}: {_e}")

        # --- OPTIONS EXIT CHECK (before equity logic) ---
        if instrument_type in ("call", "put"):
            try:
                from predictions.options_engine import get_current_premium, check_option_exit
                # For options, get current premium instead of stock price
                strike = trade.get("strike_price", 0)
                expiry = trade.get("expiration_date", "")
                current_premium = get_current_premium(ticker, strike, expiry, instrument_type)
                if current_premium <= 0:
                    # Fallback: estimate premium from intrinsic + rough time value
                    if instrument_type == "call":
                        intrinsic = max(0, current_price - strike) if strike else 0
                    else:
                        intrinsic = max(0, strike - current_price) if strike else 0
                    # Add estimated time value based on DTE to avoid false -99% P&L
                    dte = 0
                    if expiry:
                        try:
                            from datetime import datetime as _dt_fb
                            dte = (_dt_fb.strptime(expiry, "%Y-%m-%d") - _dt_fb.now()).days
                        except Exception:
                            dte = 0
                    entry_prem_fb = trade.get("premium_per_contract", 0) or 0
                    if intrinsic > 0:
                        # Has intrinsic: add small time value
                        time_value = max(0.05, entry_prem_fb * 0.1) if dte > 5 else 0.01
                        current_premium = intrinsic + time_value
                    elif dte > 5 and entry_prem_fb > 0:
                        # OTM but still has time: decay from entry premium
                        decay_factor = max(0.15, dte / 60.0)
                        current_premium = max(0.05, entry_prem_fb * decay_factor)
                    else:
                        # Near-expiry OTM: nearly worthless but not $0.01
                        current_premium = max(0.03, intrinsic + 0.02)
                    logger.debug(f"Options premium fallback for {ticker}: intrinsic={intrinsic:.2f}, dte={dte}, est_premium={current_premium:.2f}")

                exit_check = check_option_exit(trade, current_premium)
                if exit_check.get("should_exit"):
                    try:
                        close_paper_trade(trade["id"], current_premium)
                        entry_prem = trade.get("premium_per_contract") or entry_price
                        opt_pnl = ((current_premium - entry_prem) / entry_prem * 100) if entry_prem > 0 else 0
                        if direction == "short":
                            opt_pnl = ((entry_prem - current_premium) / entry_prem * 100) if entry_prem > 0 else 0
                        closed.append({
                            "ticker": ticker,
                            "direction": direction,
                            "instrument_type": instrument_type,
                            "entry_price": entry_price,
                            "exit_price": round(current_premium, 2),
                            "pnl_pct": round(opt_pnl, 2),
                            "reason": exit_check["reason"],
                        })
                        logger.warning(f"OPTIONS EXIT: {ticker} {direction} {instrument_type} | {opt_pnl:+.1f}% | {exit_check['reason']}")
                    except Exception as e:
                        logger.error(f"Options close failed {ticker}: {e}")
                continue  # Skip equity exit logic for options
            except Exception as e:
                logger.debug(f"Options exit check failed for {ticker}: {e}")
                continue  # Don't apply equity exit rules to options

        # Calculate current P&L
        if direction == "long":
            pnl_pct = ((current_price / entry_price) - 1) * 100
        else:
            pnl_pct = ((entry_price / current_price) - 1) * 100

        # ============================================================
        # BREAK-EVEN SHIFT after +1R — single biggest win-rate booster
        # ============================================================
        # When a trade has gained 1R (i.e., 1× the original stop distance),
        # automatically move the stop loss to the entry price. From that
        # moment forward, the trade can only either profit or close at
        # break-even — it CANNOT lose money. Wins booked at break-even
        # don't count as losses, so the win-rate floor rises significantly.
        # SAFETY: only shifts UP (longs) or DOWN (shorts) — never weakens.
        try:
            _orig_stop = trade.get("stop_loss_price") or 0
            _entry_p = float(entry_price or 0)
            if _entry_p > 0 and _orig_stop > 0:
                # Compute the original 1R distance (set at entry)
                _r_distance_pct = abs(_entry_p - _orig_stop) / _entry_p * 100
                if _r_distance_pct > 0 and pnl_pct >= _r_distance_pct:
                    # Move stop to break-even (entry price)
                    if direction == "long" and _orig_stop < _entry_p:
                        try:
                            from predictions.models import update_trade_stop
                            update_trade_stop(trade["id"], round(_entry_p, 2))
                            logger.info(
                                f"BREAK-EVEN SHIFT: {ticker} long +{pnl_pct:.1f}% >= 1R "
                                f"({_r_distance_pct:.1f}%) — stop moved ${_orig_stop:.2f}->${_entry_p:.2f}"
                            )
                        except Exception:
                            pass
                    elif direction == "short" and _orig_stop > _entry_p:
                        try:
                            from predictions.models import update_trade_stop
                            update_trade_stop(trade["id"], round(_entry_p, 2))
                            logger.info(
                                f"BREAK-EVEN SHIFT: {ticker} short +{pnl_pct:.1f}% >= 1R "
                                f"({_r_distance_pct:.1f}%) — stop moved ${_orig_stop:.2f}->${_entry_p:.2f}"
                            )
                        except Exception:
                            pass
        except Exception as _be_err:
            logger.debug(f"break-even shift soft-fail {ticker}: {_be_err}")

        # ============================================================
        # TIME-STOP DISCIPLINE — kill stagnant trades
        # ============================================================
        # If a trade hasn't reached +1R within 3 trading days, exit it
        # at break-even (or current price). Cuts the "death by 1000 cuts"
        # of small losers slowly bleeding capital. Frees cash for fresh
        # high-conviction setups instead of being trapped in dead money.
        try:
            _entry_dt = datetime.fromisoformat(trade.get("entry_date", ""))
            _days_held = (datetime.now() - _entry_dt).days
            _orig_stop = trade.get("stop_loss_price") or 0
            _entry_p = float(entry_price or 0)
            if _days_held >= 3 and _entry_p > 0 and _orig_stop > 0:
                _r_distance_pct = abs(_entry_p - _orig_stop) / _entry_p * 100
                # Only time-stop if the trade has not made meaningful progress
                if pnl_pct < _r_distance_pct * 0.5 and not should_close:
                    should_close = True
                    close_reason = (
                        f"TIME-STOP: held {_days_held}d, only +{pnl_pct:.1f}% "
                        f"(needed +{_r_distance_pct*0.5:.1f}% to keep, "
                        f"+{_r_distance_pct:.1f}% to hit 1R) — freeing capital"
                    )
        except Exception:
            pass

        # ADAPTIVE ATR TRAILING STOP — dynamic stop that ratchets with price
        # Replaces static stop with volatility-scaled trailing stop
        stop_loss = trade.get("stop_loss_price", 0)
        try:
            atr = _calculate_stock_atr(ticker, period=14)
            if atr > 0:
                # Base stop distance: 2.5 × ATR (standard chandelier exit)
                base_multiplier = 2.5

                # Vol scaling based on ATR relative to typical range
                # High ATR (>5%) = volatile → tighter multiplier to protect faster
                # Low ATR (<2%) = stable → wider multiplier, let it breathe
                vol_scale = 1.0
                if atr > 0.05:
                    vol_scale = 0.75   # Volatile stock → tighter stop
                elif atr < 0.015:
                    vol_scale = 1.25   # Stable stock → wider stop

                stop_distance = current_price * atr * base_multiplier * vol_scale

                if direction == "long":
                    # Trailing stop ratchets UP — never moves down
                    new_trail = current_price - stop_distance
                    if stop_loss:
                        stop_loss = max(stop_loss, new_trail)
                    else:
                        stop_loss = new_trail
                else:
                    # Short: trailing stop ratchets DOWN — never moves up
                    new_trail = current_price + stop_distance
                    if stop_loss:
                        stop_loss = min(stop_loss, new_trail)
                    else:
                        stop_loss = new_trail

                # Update the stored stop loss for next cycle
                try:
                    from predictions.models import update_trade_stop
                    update_trade_stop(trade["id"], round(stop_loss, 2))
                except Exception:
                    pass  # update_trade_stop may not exist yet — graceful fallback
        except Exception:
            pass  # Fall back to original static stop

        if stop_loss and direction == "long" and current_price <= stop_loss:
            should_close = True
            close_reason = f"Trailing stop hit (${stop_loss:.2f})"
        elif stop_loss and direction == "short" and current_price >= stop_loss:
            should_close = True
            close_reason = f"Trailing stop hit (${stop_loss:.2f})"

        # Check target
        target = trade.get("target_price", 0)
        if target and direction == "long" and current_price >= target:
            should_close = True
            close_reason = f"Target hit (${target:.2f})"
        elif target and direction == "short" and current_price <= target:
            should_close = True
            close_reason = f"Target hit (${target:.2f})"

        # Check hold duration
        try:
            entry_date = datetime.fromisoformat(trade["entry_date"])
            days_held = (datetime.now() - entry_date).days
            max_hold = trade.get("hold_duration_days", DEFAULT_HOLD_DAYS)
            if days_held >= max_hold:
                should_close = True
                close_reason = f"Hold duration expired ({days_held}/{max_hold} days)"
        except Exception:
            pass

        # EARNINGS SHIELD: Close positions if earnings are imminent (next 1 day)
        if not should_close:
            try:
                t = yf.Ticker(ticker)
                cal = t.calendar
                if cal is not None:
                    next_earn = None
                    if isinstance(cal, dict) and cal.get("Earnings Date"):
                        ed = cal["Earnings Date"]
                        next_earn = pd.Timestamp(ed[0] if isinstance(ed, list) else ed)
                    elif isinstance(cal, pd.DataFrame) and not cal.empty:
                        next_earn = pd.Timestamp(cal.iloc[0, 0]) if len(cal.columns) > 0 else None
                    if next_earn:
                        if next_earn.tzinfo is not None:
                            next_earn = next_earn.tz_localize(None)
                        days_to_earn = (next_earn - pd.Timestamp.now()).days
                        if 0 <= days_to_earn <= 1:
                            should_close = True
                            close_reason = f"EARNINGS SHIELD: earnings in {days_to_earn} day(s) — closing to avoid gap risk"
            except Exception:
                pass

        # BEAR regime protection: close losing longs at -4% (was -2% — too tight)
        if not should_close and regime == "BEAR" and direction == "long" and pnl_pct < -4:
            should_close = True
            close_reason = f"BEAR regime protection — losing long ({pnl_pct:+.1f}%)"

        # QUICK-CUT RULE: Shorts losing >3% in first 2 days = bad trade
        if not should_close and direction == "short" and pnl_pct < -3:
            try:
                entry_date = datetime.fromisoformat(trade["entry_date"])
                days_held = (datetime.now() - entry_date).days
                if days_held <= 2:
                    should_close = True
                    close_reason = f"QUICK-CUT: short losing {pnl_pct:+.1f}% in {days_held} days"
            except Exception:
                pass

        # SHORTS MAX LOSS: Never let a short lose more than 5%
        if not should_close and direction == "short" and pnl_pct < -5:
            should_close = True
            close_reason = f"SHORT MAX LOSS: down {pnl_pct:+.1f}% — hard cap"

        # AUTONOMOUS TRAILING PROFIT PROTECTION (ATR-based, per-stock)
        stock_atr = _calculate_stock_atr(ticker)
        trail_start_pct = stock_atr * 100 * 2  # Start trailing at 2x daily ATR
        trail_start_pct = max(3.0, min(trail_start_pct, 12.0))  # Clamp 3-12%

        if not should_close and pnl_pct > trail_start_pct:
            try:
                _throttle()
                hist_df = yf.download(ticker, period="1mo", progress=False)
                if hist_df is not None and len(hist_df) >= 3:
                    hist_closes = _safe_col(hist_df, "Close").values.astype(float)
                    if direction == "long":
                        peak_price = float(np.max(hist_closes))
                        peak_pnl = ((peak_price / entry_price) - 1) * 100
                    else:
                        trough_price = float(np.min(hist_closes))
                        peak_pnl = ((entry_price / trough_price) - 1) * 100

                    if peak_pnl >= 20:
                        trail_pct = 0.65
                    elif peak_pnl >= 12:
                        trail_pct = 0.55
                    else:
                        trail_pct = 0.45
                    trail_level = peak_pnl * trail_pct
                    if peak_pnl >= trail_start_pct and pnl_pct < trail_level:
                        should_close = True
                        close_reason = (
                            f"SMART TRAIL: ATR={stock_atr*100:.1f}%, peaked at {peak_pnl:+.1f}%, "
                            f"trail at {trail_level:+.1f}%, now {pnl_pct:+.1f}%"
                        )
            except Exception:
                pass

        # AUTONOMOUS TAKE PROFIT — 20%+ is exceptional, lock it
        if not should_close and pnl_pct >= 20:
            should_close = True
            close_reason = f"AUTO PROFIT LOCK: up {pnl_pct:+.1f}% — exceptional gain secured"

        # REMOVED: "SELL AT HIGHS" intraday trigger — killed best trades at +3%

        if should_close:
            try:
                _smart_close_trade(trade, current_price)  # auto-handles options
                # Cash updated atomically in close_paper_trade()
                closed.append({
                    "ticker": ticker,
                    "direction": direction,
                    "entry_price": entry_price,
                    "exit_price": round(current_price, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "reason": close_reason,
                })
                logger.warning(f"EXIT: {ticker} {direction} | {pnl_pct:+.1f}% | {close_reason}")
            except Exception as e:
                logger.error(f"Failed to close {ticker}: {e}")

    # Save snapshot if we closed anything
    if closed:
        try:
            cash = get_cash()  # Refresh atomic cash after all closes
            positions_value = sum(
                current_prices.get(t["ticker"], t["entry_price"]) * t["shares"]
                for t in get_open_trades()
            )
            total_value = cash + positions_value
            cum_ret = ((total_value / ORIGINAL_CAPITAL) - 1) * 100
            # ROUTE THROUGH TRUTH ENGINE — bulletproof sp500 + carry-forward
            try:
                from predictions.truth_engine import safe_save_snapshot as _safe_snap_ec
                _safe_snap_ec()
            except Exception:
                save_portfolio_snapshot(total_value, cash, positions_value, 0, cum_ret, 0, 0, len(get_open_trades()))
        except Exception:
            pass

    return {"closed": closed, "checked": len(open_trades)}
