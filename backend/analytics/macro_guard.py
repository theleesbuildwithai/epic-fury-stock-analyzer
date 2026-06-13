"""
Macro Guard — validates oil / gold / treasury yields with multi-source
fallback. Same defense pattern as vix_guard.

Morning check found: oil = 0, gold = 0, 10Y yield = 0. macro_v2
reading was therefore RISK_ON despite picks engine BEAR — internal
inconsistency from corrupted inputs.

This module:
1. Multi-source fetch (yfinance primary, Stooq fallback)
2. Absolute bounds validation per instrument
3. Move-magnitude sanity check vs last-known-good
4. Persistence in trading_state for cold-start safety
5. Day-over-day shock detection (NFP/CPI/FOMC-style surprises)

The economic shock detector specifically catches what the overnight
check exposed: hot jobs report → +172K payrolls → yields jump sharply
at the open. We can't read NFP directly without paid data, but we
CAN detect the yields jump and adjust regime accordingly.
"""
import time
from typing import Optional, Tuple


# === Instrument-specific sanity bounds ===
BOUNDS = {
    "oil_wti":   (10.0, 200.0,   "CL=F"),   # Crude futures: $10-$200/bbl
    "gold":      (500.0, 5000.0, "GC=F"),   # Gold: $500-$5000/oz
    "10y_yield": (0.5, 10.0,     "^TNX"),   # 10Y yield: 0.5%-10%
    "5y_yield":  (0.5, 10.0,     "^FVX"),
    "3m_yield":  (0.0, 12.0,     "^IRX"),
    "dxy":       (60.0, 180.0,   "DX-Y.NYB"),  # Dollar index
    "spy":       (50.0, 1500.0,  "SPY"),       # S&P proxy
}

# Maximum reasonable single-day move (percent of previous value)
# These are tuned for catching corruption, not normal moves
MAX_DAILY_MOVE_PCT = {
    "oil_wti":   15.0,   # Oil rarely moves > 15% in a day
    "gold":      8.0,    # Gold rarely moves > 8%
    "10y_yield": 20.0,   # 20% move = 50bps on 2.5% yield = NFP shock territory
    "5y_yield":  20.0,
    "3m_yield":  25.0,
    "dxy":       3.0,    # DXY is stable
    "spy":       10.0,   # SPY rarely moves > 10% in a day
}

# Hours we trust the last-known-good cache
TRUST_HOURS = 24.0


def get_macro_safe(key: str) -> dict:
    """
    Validated macro value. Returns dict with value, source, confidence,
    and shock flag if day-over-day move is abnormal.

    Args:
        key: one of "oil_wti", "gold", "10y_yield", "5y_yield", "3m_yield",
             "dxy", "spy"

    Returns:
        {
            "value": float | None,
            "source": str,
            "confidence": "HIGH" | "MEDIUM" | "LOW",
            "raw_attempted": float | None,
            "rejected_reason": str | None,
            "day_change_pct": float | None,
            "shock_detected": bool,
            "shock_reason": str | None,  # if shock detected
        }
    """
    if key not in BOUNDS:
        return _result(None, "unknown_key", "LOW",
                       rejected_reason=f"unsupported key: {key}")
    lo, hi, ticker = BOUNDS[key]

    # Load last-known-good
    last_good, last_good_ts = _load_last_good(key)
    age_hours = ((time.time() - last_good_ts) / 3600.0
                 if last_good_ts > 0 else float("inf"))

    # Try multi-source fetch
    raw, fetch_error = _multi_source_fetch(ticker, key)

    # Case: nothing fetched
    if raw is None:
        if age_hours < TRUST_HOURS and last_good > 0:
            return _result(last_good, "cached_last_good", "MEDIUM",
                           raw_attempted=None,
                           rejected_reason=f"no_data ({fetch_error})")
        return _result(None, "no_data_no_cache", "LOW",
                       raw_attempted=None,
                       rejected_reason=f"complete_failure ({fetch_error})")

    # Case: bounds rejection
    if raw < lo or raw > hi:
        if age_hours < TRUST_HOURS and last_good > 0:
            return _result(last_good, "cached_last_good", "MEDIUM",
                           raw_attempted=raw,
                           rejected_reason=f"oob [{lo}, {hi}]")
        return _result(None, "oob_no_cache", "LOW",
                       raw_attempted=raw,
                       rejected_reason=f"oob_no_cache [{lo}, {hi}]")

    # Case: shock detection (raw passed bounds, compare to last-good)
    shock_detected = False
    shock_reason = None
    day_change_pct = None
    if last_good > 0 and age_hours < 48:
        day_change_pct = (raw - last_good) / last_good * 100.0
        threshold = MAX_DAILY_MOVE_PCT.get(key, 100.0)
        if abs(day_change_pct) > threshold:
            # This might be a real macro shock (NFP, CPI surprise) OR data
            # corruption. Be conservative: USE last_known_good for safety,
            # but flag the shock for the regime engine to consider.
            shock_detected = True
            shock_reason = (f"abnormal_move {day_change_pct:+.1f}% "
                            f"exceeds {threshold}%")
            return _result(last_good, "cached_last_good_shock", "MEDIUM",
                           raw_attempted=raw,
                           rejected_reason=shock_reason,
                           shock_detected=True,
                           shock_reason=shock_reason,
                           day_change_pct=day_change_pct)

    # Case: reading passed everything — persist and return
    _persist_last_good(key, raw)
    return _result(raw, "live_fresh", "HIGH",
                   raw_attempted=raw,
                   day_change_pct=day_change_pct)


def detect_macro_shocks() -> dict:
    """
    Run all macro guards and return shock summary.
    Used by the regime engine + risk system.

    Returns:
        {
            "any_shock": bool,
            "rate_shock_bps": float | None,  # 10Y yield day move in bps
            "oil_shock_pct": float | None,
            "gold_shock_pct": float | None,
            "spy_shock_pct": float | None,
            "shocks_detected": list of {key, reason, change_pct},
            "regime_modifier": str,  # ADD_BEAR | ADD_BULL | NONE
            "summary": str,
        }
    """
    keys = ["oil_wti", "gold", "10y_yield", "spy"]
    results = {k: get_macro_safe(k) for k in keys}
    shocks = []
    for k, r in results.items():
        if r.get("shock_detected"):
            shocks.append({
                "key": k,
                "reason": r.get("shock_reason"),
                "change_pct": r.get("day_change_pct"),
            })

    # Rate-shock specifically (NFP-style)
    rate_shock_bps = None
    rate_result = results.get("10y_yield", {})
    if rate_result.get("day_change_pct") is not None:
        last_yield = rate_result.get("raw_attempted") or rate_result.get("value")
        # Defensive: skip math if last_yield is None or day_change_pct
        # would cause divide-by-zero. The day_change_pct guard means
        # we have a number, but raw_attempted can still be None if the
        # value came from cached fallback only.
        if (last_yield is not None and last_yield > 0
                and rate_result.get("day_change_pct") != -100):
            try:
                prev_yield = last_yield / (1 + rate_result["day_change_pct"] / 100)
                rate_shock_bps = (last_yield - prev_yield) * 100  # 1% = 100bps
            except (ZeroDivisionError, TypeError):
                rate_shock_bps = None

    # Determine regime modifier
    regime_modifier = "NONE"
    summary_parts = []
    if rate_shock_bps and rate_shock_bps > 10:
        regime_modifier = "ADD_BEAR"
        summary_parts.append(
            f"RATE SHOCK: 10Y +{rate_shock_bps:.0f}bps "
            f"(hot data → equity selloff risk)"
        )
    elif rate_shock_bps and rate_shock_bps < -10:
        regime_modifier = "ADD_BULL"
        summary_parts.append(
            f"RATE RELIEF: 10Y {rate_shock_bps:.0f}bps "
            f"(cool data → equity rally tailwind)"
        )

    spy_result = results.get("spy", {})
    if spy_result.get("day_change_pct") and spy_result["day_change_pct"] < -3:
        if regime_modifier == "NONE":
            regime_modifier = "ADD_BEAR"
        summary_parts.append(
            f"SPY DOWN {spy_result['day_change_pct']:.1f}% — risk-off"
        )

    summary = " | ".join(summary_parts) if summary_parts else "no_shocks"
    return {
        "any_shock": len(shocks) > 0,
        "rate_shock_bps": round(rate_shock_bps, 1) if rate_shock_bps else None,
        "oil_shock_pct": results.get("oil_wti", {}).get("day_change_pct"),
        "gold_shock_pct": results.get("gold", {}).get("day_change_pct"),
        "spy_shock_pct": spy_result.get("day_change_pct"),
        "shocks_detected": shocks,
        "regime_modifier": regime_modifier,
        "summary": summary,
        "all_values": {k: results[k].get("value") for k in keys},
        "all_sources": {k: results[k].get("source") for k in keys},
    }


# ============================================================
# Internals
# ============================================================

def _multi_source_fetch(ticker: str, key: str) -> tuple:
    """Tries yfinance first, then Stooq fallback. Returns (value, error)."""
    # Source 1: yfinance
    val, err = _try_yfinance(ticker)
    if val is not None:
        return val, None
    # Source 2: Stooq fallback
    val_s, err_s = _try_stooq(ticker, key)
    if val_s is not None:
        return val_s, None
    return None, f"yfinance:{err} | stooq:{err_s}"


def _try_yfinance(ticker: str) -> tuple:
    import math as _m
    try:
        import yfinance as yf
        import threading as _mg_thr
        _mg_r = [None]
        _mg_t = _mg_thr.Thread(
            target=lambda r=_mg_r, t=ticker: r.__setitem__(0, yf.download(t, period="5d", progress=False)),
            daemon=True)
        _mg_t.start(); _mg_t.join(timeout=10)
        df = _mg_r[0]
        if df is None or df.empty:
            return None, "empty"
        close = df["Close"]
        if hasattr(close, "iloc"):
            if hasattr(close, "columns"):
                close = close.iloc[:, 0]
            val = float(close.dropna().iloc[-1])
            # Reject NaN/Inf FIRST — bounds checks silently pass NaN.
            if _m.isnan(val) or _m.isinf(val):
                return None, f"nan_or_inf:{val}"
            # Catch obvious corruption (the VIX=7499 pattern)
            if val < 0 or val > 100000:
                return None, f"corrupt:{val:.0f}"
            return val, None
        return None, "no_iloc"
    except Exception as e:
        return None, f"err:{str(e)[:40]}"


def _try_stooq(ticker: str, key: str) -> tuple:
    """Stooq fallback. Maps our ticker to Stooq's URL convention.
    Same NaN/corruption defenses as yfinance path."""
    import math as _m
    # Stooq uses different ticker symbols
    stooq_map = {
        "CL=F": "cl.f",
        "GC=F": "gc.f",
        "^TNX": "^tnx",
        "^FVX": "^fvx",
        "^IRX": "^irx",
        "DX-Y.NYB": "dx.f",
        "SPY": "spy.us",
    }
    stooq_sym = stooq_map.get(ticker)
    if not stooq_sym:
        return None, f"no_stooq_mapping:{ticker}"
    try:
        import urllib.request
        url = f"https://stooq.com/q/l/?s={stooq_sym}&f=sd2t2ohlcv&h&e=csv"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 Epic Fury macro guard"
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            content = resp.read().decode().strip()
        lines = content.split("\n")
        if len(lines) < 2:
            return None, "stooq_no_data"
        cols = lines[1].split(",")
        if len(cols) < 7:
            return None, "stooq_malformed"
        close_str = cols[6].strip()
        if close_str in ("N/D", "", "0", "nan", "NaN"):
            return None, "stooq_no_close"
        val = float(close_str)
        if _m.isnan(val) or _m.isinf(val):
            return None, f"stooq_nan_or_inf:{val}"
        if val < 0 or val > 100000:
            return None, f"stooq_corrupt:{val:.0f}"
        return val, None
    except Exception as e:
        return None, f"stooq_err:{str(e)[:40]}"


def _load_last_good(key: str) -> tuple:
    """Returns (value, timestamp). 0,0 if not set."""
    try:
        from predictions.models import get_trading_state
        v = get_trading_state(f"macro_guard_{key}", "")
        ts = get_trading_state(f"macro_guard_{key}_ts", "")
        if v and ts:
            return float(v), float(ts)
    except Exception:
        pass
    return 0.0, 0.0


def _persist_last_good(key: str, value: float):
    try:
        from predictions.models import set_trading_state
        set_trading_state(f"macro_guard_{key}", str(value))
        set_trading_state(f"macro_guard_{key}_ts", str(time.time()))
    except Exception:
        pass


def _result(value, source: str, confidence: str,
            raw_attempted=None, rejected_reason=None,
            shock_detected=False, shock_reason=None,
            day_change_pct=None) -> dict:
    return {
        "value": (round(float(value), 4)
                  if value is not None else None),
        "source": source,
        "confidence": confidence,
        "raw_attempted": (round(float(raw_attempted), 4)
                          if raw_attempted is not None else None),
        "rejected_reason": rejected_reason,
        "shock_detected": shock_detected,
        "shock_reason": shock_reason,
        "day_change_pct": (round(day_change_pct, 2)
                            if day_change_pct is not None else None),
    }
