"""
VIX Guard — multi-tier defense against rate-limited / inflated VIX readings.

Problem this solves:
    yfinance occasionally rate-limits or returns stale/corrupted VIX values.
    When the system reads VIX = 65 (when real is 25), the regime engine
    classifies CRISIS, cuts exposure to 0.30x, and refuses trades.
    Result: false-CRISIS at market open = lost first hour of trading.

Defense layers:
    1. yfinance fetch (the normal path)
    2. Absolute bounds (5 ≤ VIX ≤ 100) — anything else is corruption
    3. Move-magnitude check (max 20-point move OR 50% move in single fetch)
       relative to last-known-good if cached < 12 hours
    4. Persistent last-known-good in trading_state (survives restarts)
    5. Hardcoded neutral fallback (20.0) if everything else fails

Output: (value, source, confidence) tuple. Source tells you which layer
fired. Confidence is HIGH (fresh fetch passed all checks), MEDIUM (used
last-known-good), or LOW (hardcoded fallback).
"""
import time
import math
from typing import Optional


# === Sanity bounds ===
VIX_ABSOLUTE_MIN = 5.0
VIX_ABSOLUTE_MAX = 100.0
VIX_MAX_SINGLE_FETCH_MOVE_PCT = 50.0   # >50% jump in one fetch = suspicious
VIX_MAX_SINGLE_FETCH_MOVE_ABS = 20.0   # >20 pt jump in one fetch = suspicious

# === Persistent storage keys ===
LAST_GOOD_VIX_KEY = "vix_guard_last_known_good"
LAST_GOOD_VIX_TS_KEY = "vix_guard_last_known_good_ts"

# === Cache freshness ===
LAST_GOOD_TRUST_HOURS = 12.0  # Trust cached last-good for 12h
HARDCODED_NEUTRAL = 20.0       # If all else fails, neutral assumption


def get_vix_safe() -> dict:
    """
    Multi-source VIX with sanity validation. Never raises.

    Returns:
        {
            "value": float,         # validated VIX to use
            "source": str,          # yfinance_live | cached_last_good
                                    # | hardcoded_neutral
            "confidence": str,      # HIGH | MEDIUM | LOW
            "raw_attempted": float, # what yfinance returned (or None)
            "rejected_reason": str  # why raw was rejected (or None)
        }
    """
    # === Load persisted last-known-good ===
    last_good, last_good_ts = _load_last_good()
    age_hours = ((time.time() - last_good_ts) / 3600.0
                 if last_good_ts > 0 else float("inf"))

    # === Attempt multi-source fetch (Option C: yfinance → Stooq → VXX proxy) ===
    raw = None
    fetch_error = None
    fetch_source = None

    # Source 1: yfinance ^VIX
    raw, fetch_error = _try_fetch_yfinance("^VIX")
    if raw is not None:
        fetch_source = "yfinance_vix"

    # Source 2: Stooq fallback
    if raw is None:
        raw_stooq, err_stooq = _try_fetch_stooq()
        if raw_stooq is not None:
            raw = raw_stooq
            fetch_source = "stooq_vix"
        else:
            fetch_error = f"{fetch_error} | {err_stooq}"

    # Source 3: VXX proxy DISABLED 2026-06-06.
    # The VXX/VIX scale of 0.55 was empirically wrong — VXX is a
    # futures ETF and its dollar price doesn't have a linear ratio
    # to VIX. Live test showed VXX=$21.55 → proxy returned 39.18
    # (would be read as ELEVATED/CRISIS) when real VIX was ~21.
    # Better to fall through to cached or hardcoded neutral than
    # produce a misleading value. If both yfinance and Stooq fail
    # AND we have no recent cache, the system uses HARDCODED_NEUTRAL
    # (20.0) which keeps regime in NORMAL territory.
    pass  # VXX proxy removed

    # === Case 1: No reading at all ===
    if raw is None:
        if age_hours < LAST_GOOD_TRUST_HOURS and last_good > 0:
            return _result(last_good, "cached_last_good", "MEDIUM",
                           raw_attempted=None,
                           rejected_reason=f"no_data ({fetch_error})")
        return _result(HARDCODED_NEUTRAL, "hardcoded_neutral", "LOW",
                       raw_attempted=None,
                       rejected_reason=f"no_data_and_stale_cache "
                                      f"(age={age_hours:.1f}h, {fetch_error})")

    # === Case 2: Absolute bounds check ===
    if raw < VIX_ABSOLUTE_MIN or raw > VIX_ABSOLUTE_MAX:
        if age_hours < LAST_GOOD_TRUST_HOURS and last_good > 0:
            return _result(last_good, "cached_last_good", "MEDIUM",
                           raw_attempted=raw,
                           rejected_reason=f"out_of_bounds [{VIX_ABSOLUTE_MIN},"
                                          f"{VIX_ABSOLUTE_MAX}]")
        return _result(HARDCODED_NEUTRAL, "hardcoded_neutral", "LOW",
                       raw_attempted=raw,
                       rejected_reason="out_of_bounds_no_cache")

    # === Case 3: Relative move check (vs last-known-good) ===
    # IMPORTANT: only reject the LIVE reading if (a) the move is suspicious
    # AND (b) the cached value is more plausible than the live one.
    #
    # 2026-06-06 bug fix: previously this check would reject any large
    # move regardless of direction. When VIX dropped from cached 62.6
    # (last week's crisis) to live 21.5 (recovery), the guard rejected
    # the recovery and kept reporting 62.6. We need to allow legitimate
    # recoveries from CRISIS back to normal.
    #
    # 2026-06-07 hardening: widened recovery/onset thresholds to catch
    # more edge cases. Added stale-crisis override: if the cache is in
    # CRISIS territory (>35) AND older than CRISIS_STALE_HOURS (4h),
    # distrust it completely and accept any plausible live reading.
    # Real VIX crises don't stay above 35 for days on end without being
    # reflected in yfinance — a stale crisis cache is almost certainly
    # a prior spike that has since recovered.
    CRISIS_STALE_HOURS = 1.0   # cache in crisis zone + older than 1h → distrust it
    # 2026-06-09: lowered from 4h. Each cycle refreshes the timestamp when
    # it accepts a reading, so a 4h threshold was never actually triggering —
    # the cache was perpetually "fresh" with a stale crisis value.
    if last_good > 0 and age_hours < LAST_GOOD_TRUST_HOURS:
        abs_move = abs(raw - last_good)
        pct_move = (abs_move / last_good) * 100.0

        # Layer A: stale-crisis override — if cached value is crisis-level
        # AND it's been sitting there for > 4 hours without being refreshed,
        # it's almost certainly a prior spike. Accept any sane live reading.
        stale_crisis_cache = (last_good > 35 and age_hours > CRISIS_STALE_HOURS
                              and VIX_ABSOLUTE_MIN <= raw <= VIX_ABSOLUTE_MAX)

        # Layer B: legitimate CRISIS → normal recovery
        # Wider threshold than before: cached > 30, live < 30 (was < 25)
        is_crisis_recovery = (last_good > 30 and raw < 30)

        # Layer C: spike INTO crisis is also legitimate (real shock event)
        # Wider: cached < 25, live > 35 (was: cached < 20, live > 35)
        is_crisis_onset = (last_good < 25 and raw > 35)

        if (pct_move > VIX_MAX_SINGLE_FETCH_MOVE_PCT
                or abs_move > VIX_MAX_SINGLE_FETCH_MOVE_ABS):
            if stale_crisis_cache:
                # Stale crisis cache — always trust the live reading over it
                pass  # falls through to Case 4 (accept + persist)
            elif is_crisis_recovery:
                # Legitimate mean-reversion — accept live reading
                pass  # falls through to Case 4 (accept + persist)
            elif is_crisis_onset:
                # Real shock event — accept live reading
                pass  # falls through to Case 4 (accept + persist)
            else:
                # Both values plausible AND move suspicious → reject
                return _result(last_good, "cached_last_good", "MEDIUM",
                               raw_attempted=raw,
                               rejected_reason=f"suspicious_move "
                                              f"{pct_move:.0f}% / {abs_move:.1f}pts")

    # === Case 4: Reading passed all checks — persist + return ===
    _persist_last_good(raw)
    return _result(raw, fetch_source or "yfinance_live", "HIGH",
                   raw_attempted=raw, rejected_reason=None)


# ============================================================
# Internals
# ============================================================

def _try_fetch_yfinance(ticker: str = "^VIX") -> tuple:
    """Returns (value or None, error_message). Ticker param lets us
    use this for VIX + proxies (VXX) without duplicating code."""
    import math as _m
    try:
        import yfinance as yf
        df = yf.download(ticker, period="2d", progress=False)
        if df is None or df.empty:
            return None, "empty_response"
        try:
            close = df["Close"]
            if hasattr(close, "iloc"):
                if hasattr(close, "columns"):
                    close = close.iloc[:, 0]
                val = float(close.dropna().iloc[-1])
                # CRITICAL: morning check found VIX = 7499. yfinance
                # can also return NaN/Inf from corrupted columns. Reject
                # all three before the bounds check (which would silently
                # pass NaN because NaN comparisons always return False).
                if _m.isnan(val) or _m.isinf(val):
                    return None, f"nan_or_inf:{val}"
                if val > 1000 or val < 0:
                    return None, f"scaled_corrupt:{val:.0f}"
                return val, None
        except Exception as e:
            return None, f"close_parse_error: {str(e)[:40]}"
        return None, "no_close_column"
    except Exception as e:
        return None, f"yfinance_error: {str(e)[:60]}"


def _try_fetch_stooq() -> tuple:
    """Stooq is a free alternative data source. No API key needed.
    Returns (value or None, error_message)."""
    import math as _m
    try:
        import urllib.request
        url = "https://stooq.com/q/l/?s=^vix&f=sd2t2ohlcv&h&e=csv"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 Epic Fury VIX guard"
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
        if val > 1000 or val < 0:
            return None, f"stooq_scaled_corrupt:{val:.0f}"
        return val, None
    except Exception as e:
        return None, f"stooq_error: {str(e)[:50]}"


def _try_fetch_vxx_proxy() -> tuple:
    """VXX is the iPath Series B S&P 500 VIX Short-Term Futures ETN.
    Tracks VIX futures. Used as last-resort directional indicator when
    both primary sources fail."""
    val, err = _try_fetch_yfinance("VXX")
    if val is None:
        return None, f"vxx_proxy_failed: {err}"
    # Empirical scale: VXX ~ VIX * 0.55 mid-range. Rough but useful.
    implied = val / 0.55
    if implied < VIX_ABSOLUTE_MIN or implied > VIX_ABSOLUTE_MAX:
        return None, f"vxx_implied_oob: {implied:.1f}"
    return implied, None


def _load_last_good() -> tuple:
    """Returns (value, timestamp). Defaults to (20.0, 0)."""
    try:
        from predictions.models import get_trading_state
        v_str = get_trading_state(LAST_GOOD_VIX_KEY, "")
        ts_str = get_trading_state(LAST_GOOD_VIX_TS_KEY, "")
        if not v_str or not ts_str:
            return HARDCODED_NEUTRAL, 0.0
        return float(v_str), float(ts_str)
    except Exception:
        return HARDCODED_NEUTRAL, 0.0


def _persist_last_good(value: float):
    """Save validated VIX to trading_state for future fallbacks."""
    try:
        from predictions.models import set_trading_state
        set_trading_state(LAST_GOOD_VIX_KEY, str(value))
        set_trading_state(LAST_GOOD_VIX_TS_KEY, str(time.time()))
    except Exception:
        pass  # Non-fatal — we still return the value


def _result(value: float, source: str, confidence: str,
            raw_attempted=None, rejected_reason=None) -> dict:
    """Standard return shape."""
    return {
        "value": round(float(value), 2),
        "source": source,
        "confidence": confidence,
        "raw_attempted": (round(float(raw_attempted), 2)
                          if raw_attempted is not None else None),
        "rejected_reason": rejected_reason,
    }
