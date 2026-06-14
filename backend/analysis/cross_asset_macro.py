"""
Cross-Asset Macro Signal Engine — Sentinel Quant

Two-Sigma-style macro overlay. Other markets lead equities by hours-to-days —
this module ingests the leading markets and converts them into actionable
signals: regime classification, sector tilts, exposure modifier, and a
risk-on/risk-off score.

What it tracks (all free, all from Yahoo Finance):

  RATES & CURVE
    ^IRX  — 13-week T-Bill yield
    ^FVX  — 5-year Treasury yield
    ^TNX  — 10-year Treasury yield
    ^TYX  — 30-year Treasury yield
    TLT   — 20+ year Treasury bond ETF
    Yield-curve slope (10Y - 2Y proxy via ^TNX - ^IRX) → recession proxy

  CREDIT
    HYG   — High-yield corporate bond ETF
    LQD   — Investment-grade corporate bond ETF
    HYG/LQD ratio → credit-stress gauge

  VOLATILITY TERM STRUCTURE
    ^VIX9D — 9-day implied volatility
    ^VIX   — 30-day implied volatility (the "fear index")
    ^VIX3M — 3-month implied volatility
    Backwardation (VIX9D > VIX > VIX3M) → acute fear, mean-reversion buying op
    Contango (VIX9D < VIX < VIX3M) → calm, momentum regime

  CURRENCY
    DX-Y.NYB — Dollar Index spot
    UUP      — Dollar Index ETF (fallback if DX-Y not available)
    EURUSD=X, USDJPY=X — major pairs

  COMMODITIES
    GLD — Gold ETF (fear / inflation hedge)
    SLV — Silver ETF (industrial + monetary)
    USO — Crude oil ETF
    UNG — Natural gas ETF
    DBA — Agriculture ETF
    CPER — Copper ETF (global growth proxy)

  GLOBAL EQUITIES
    EFA — Developed-markets ex-US (Two Sigma watches this for global beta)
    EEM — Emerging-markets equities
    FXI — China large-cap

Outputs:
    {
      "macro_regime": "RISK_ON"|"RISK_OFF"|"NEUTRAL",
      "exposure_modifier": float,             # multiply target exposure by this
      "sector_tilts": {sector: score (-2..+2)},
      "leading_signals": {asset: {momentum, level, signal}},
      "vix_term_structure": {state, ratio_9d_30d, ratio_30d_3m},
      "yield_curve": {slope, state, ten_year, two_year_proxy},
      "credit_stress": {ratio, state, hyg_change, lqd_change},
      "ok": bool,
      "data_age_seconds": int,
    }

Safety nets:
    - Per-asset try/except, no asset can crash the engine
    - Last-known-good cache returned if entire fetch fails
    - Returns ok:False (and a neutral default) on total failure
    - Exposure modifier always clamped to [0.5, 1.2]
    - Sector tilts always clamped to [-2.0, +2.0]
    - All numeric outputs validated for NaN / inf
"""

import yfinance as yf
import numpy as np
import pandas as pd
import time
import logging
import math
from datetime import datetime

logger = logging.getLogger(__name__)

# ============================================================
#  ASSET UNIVERSE
# ============================================================

RATES_TICKERS = ["^IRX", "^FVX", "^TNX", "^TYX", "TLT"]
CREDIT_TICKERS = ["HYG", "LQD"]
VIX_TERM_TICKERS = ["^VIX9D", "^VIX", "^VIX3M"]
CURRENCY_TICKERS = ["DX-Y.NYB", "UUP", "EURUSD=X", "USDJPY=X"]
COMMODITY_TICKERS = ["GLD", "SLV", "USO", "UNG", "DBA", "CPER"]
GLOBAL_EQUITY_TICKERS = ["EFA", "EEM", "FXI"]

ALL_TICKERS = (
    RATES_TICKERS
    + CREDIT_TICKERS
    + VIX_TERM_TICKERS
    + CURRENCY_TICKERS
    + COMMODITY_TICKERS
    + GLOBAL_EQUITY_TICKERS
)

# ============================================================
#  CACHE & THROTTLING
# ============================================================

_macro_cache = {"data": None, "time": 0.0, "last_good": None}
_MACRO_CACHE_TTL = 600  # 10 min — macro doesn't change minute-to-minute
_last_yf_call = [0.0]
_YF_DELAY = 2.5


def _throttle():
    now = time.time()
    elapsed = now - _last_yf_call[0]
    if elapsed < _YF_DELAY:
        time.sleep(_YF_DELAY - elapsed)
    _last_yf_call[0] = time.time()


def _safe_float(x, default=None):
    try:
        v = float(x)
        if not math.isfinite(v):
            return default
        return v
    except Exception:
        return default


# ============================================================
#  YFINANCE FETCH (batched, fault-tolerant)
# ============================================================

def _extract_close_series(df, sym):
    """Pull a clean Close series for one symbol from a multi-index frame."""
    try:
        if df is None or len(df) == 0:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            if sym not in df.columns.get_level_values(0):
                return None
            s = df[sym]["Close"].dropna()
        else:
            if "Close" not in df.columns:
                return None
            s = df["Close"].dropna()
        if len(s) < 5:
            return None
        return s.values.astype(float)
    except Exception:
        return None


def _fetch_all() -> dict:
    """Download all macro tickers in batches. Returns {sym: closes_array}."""
    out = {}
    # Batch by 8 to stay below Yahoo's silent caps
    import threading as _cam_thr
    batch_size = 8
    for i in range(0, len(ALL_TICKERS), batch_size):
        batch = ALL_TICKERS[i:i + batch_size]
        try:
            _throttle()
            _cam_r = [None]
            _cam_t = _cam_thr.Thread(
                target=lambda r=_cam_r, b=batch: r.__setitem__(
                    0, yf.download(b, period="3mo", interval="1d", progress=False,
                                   group_by="ticker", auto_adjust=True, threads=False)),
                daemon=True)
            _cam_t.start(); _cam_t.join(timeout=15)
            df = _cam_r[0]
            if df is None or len(df) == 0:
                continue
            for sym in batch:
                arr = _extract_close_series(df, sym)
                if arr is not None:
                    out[sym] = arr
        except Exception as e:
            logger.debug(f"cross_asset_macro fetch batch failed: {e}")
            continue

    # Fallback: per-symbol multi-source for any gaps
    missing = [s for s in ALL_TICKERS if s not in out]
    if missing:
        try:
            from analytics.multi_source_adapter import get_historical_any_source
            for sym in missing:
                try:
                    df2 = get_historical_any_source(sym, "3mo")
                    if df2 is not None and len(df2) >= 5:
                        arr = df2["Close"].dropna().values
                        if arr is not None and len(arr) >= 5:
                            out[sym] = arr
                except Exception:
                    continue
        except Exception:
            pass
    return out


# ============================================================
#  SIGNAL CALCULATIONS — all pure-numpy, no external deps
# ============================================================

def _momentum_pct(arr: np.ndarray, lookback: int) -> float:
    """Percent change over `lookback` trading days. Returns 0.0 if not enough."""
    if arr is None or len(arr) < lookback + 1:
        return 0.0
    base = arr[-lookback - 1]
    if base is None or abs(base) < 1e-10:
        return 0.0
    return float((arr[-1] / base - 1.0) * 100.0)


def _zscore(arr: np.ndarray, lookback: int = 60) -> float:
    """Z-score of the last point versus the trailing window."""
    if arr is None or len(arr) < 5:
        return 0.0
    win = arr[-min(lookback, len(arr)):]
    s = float(np.std(win))
    if s < 1e-10:
        return 0.0
    return float((win[-1] - np.mean(win)) / s)


def _level(arr: np.ndarray):
    if arr is None or len(arr) == 0:
        return None
    return _safe_float(arr[-1])


# ============================================================
#  REGIME CALCULATIONS
# ============================================================

def _vix_term_structure(prices: dict) -> dict:
    """Detect contango (calm) vs backwardation (acute fear)."""
    out = {"state": "unknown", "ratio_9d_30d": None, "ratio_30d_3m": None,
           "vix9d": None, "vix": None, "vix3m": None, "ok": False}
    v9 = _level(prices.get("^VIX9D"))
    v = _level(prices.get("^VIX"))
    v3 = _level(prices.get("^VIX3M"))
    out["vix9d"], out["vix"], out["vix3m"] = v9, v, v3

    if v is None or v <= 0:
        return out
    out["ok"] = True

    if v9 and v > 0:
        out["ratio_9d_30d"] = round(v9 / v, 3)
    if v and v3 and v3 > 0:
        out["ratio_30d_3m"] = round(v / v3, 3)

    # Backwardation: short-dated vol > long-dated → acute fear
    if v9 and v3 and v9 > v > v3:
        out["state"] = "backwardation_strong"  # buy the dip / VIX-mean-revert
    elif (out["ratio_30d_3m"] or 0) > 1.05:
        out["state"] = "backwardation"
    elif v9 and v3 and v9 < v < v3:
        out["state"] = "contango_strong"  # calm momentum regime
    elif (out["ratio_30d_3m"] or 0) < 0.95:
        out["state"] = "contango"
    else:
        out["state"] = "flat"
    return out


def _yield_curve(prices: dict) -> dict:
    """Yield curve slope. Inverted = recession warning."""
    out = {"state": "unknown", "slope_bps": None, "ten_year": None,
           "short_proxy": None, "ok": False}
    ten = _level(prices.get("^TNX"))
    short = _level(prices.get("^IRX"))  # 13-week yield as 2y proxy
    five = _level(prices.get("^FVX"))
    out["ten_year"] = ten
    out["short_proxy"] = short

    if ten is None or short is None:
        # Fall back to 5y if 10y missing
        if ten is None:
            ten = five
        if ten is None or short is None:
            return out

    out["ok"] = True
    slope = (ten - short) * 100.0  # convert from yield-points to bps-ish
    out["slope_bps"] = round(float(slope), 1)
    if slope < -25:
        out["state"] = "deeply_inverted"     # severe recession warning
    elif slope < 0:
        out["state"] = "inverted"
    elif slope < 25:
        out["state"] = "flat"
    elif slope < 100:
        out["state"] = "normal"
    else:
        out["state"] = "steep"               # bullish for banks, growth
    return out


def _credit_stress(prices: dict) -> dict:
    """HYG/LQD ratio. Falling ratio = credit stress (bearish equities)."""
    out = {"state": "unknown", "ratio": None, "ratio_change_5d_pct": None,
           "hyg_5d_pct": None, "lqd_5d_pct": None, "ok": False}
    hyg = prices.get("HYG")
    lqd = prices.get("LQD")
    if hyg is None or lqd is None or len(hyg) < 6 or len(lqd) < 6:
        return out
    out["ok"] = True
    ratio_now = float(hyg[-1] / lqd[-1]) if lqd[-1] else None
    ratio_then = float(hyg[-6] / lqd[-6]) if lqd[-6] else None
    out["ratio"] = round(ratio_now, 4) if ratio_now else None
    if ratio_now and ratio_then:
        out["ratio_change_5d_pct"] = round((ratio_now / ratio_then - 1.0) * 100.0, 2)
    out["hyg_5d_pct"] = round(_momentum_pct(hyg, 5), 2)
    out["lqd_5d_pct"] = round(_momentum_pct(lqd, 5), 2)

    chg = out.get("ratio_change_5d_pct") or 0.0
    if chg < -1.5:
        out["state"] = "stress_high"        # credit deteriorating fast
    elif chg < -0.5:
        out["state"] = "stress_moderate"
    elif chg > 1.5:
        out["state"] = "easing_strong"      # risk-on
    elif chg > 0.5:
        out["state"] = "easing"
    else:
        out["state"] = "stable"
    return out


def _dollar(prices: dict) -> dict:
    """DXY momentum. Dollar up = headwind for multinationals & commodities."""
    out = {"state": "unknown", "level": None, "momentum_5d": None,
           "momentum_20d": None, "source": None, "ok": False}
    dxy = prices.get("DX-Y.NYB")
    if dxy is None or len(dxy) < 6:
        dxy = prices.get("UUP")  # ETF fallback
        out["source"] = "UUP" if dxy is not None else None
    else:
        out["source"] = "DX-Y.NYB"
    if dxy is None or len(dxy) < 6:
        return out
    out["ok"] = True
    out["level"] = round(float(dxy[-1]), 4)
    out["momentum_5d"] = round(_momentum_pct(dxy, 5), 2)
    out["momentum_20d"] = round(_momentum_pct(dxy, 20), 2)
    m5 = out["momentum_5d"]
    if m5 > 1.5:
        out["state"] = "strong_up"
    elif m5 > 0.5:
        out["state"] = "up"
    elif m5 < -1.5:
        out["state"] = "strong_down"
    elif m5 < -0.5:
        out["state"] = "down"
    else:
        out["state"] = "flat"
    return out


def _commodities(prices: dict) -> dict:
    """Per-commodity momentum + composite signals."""
    syms = ["GLD", "SLV", "USO", "UNG", "DBA", "CPER"]
    out = {"per_asset": {}, "ok": False}
    for s in syms:
        a = prices.get(s)
        if a is None or len(a) < 6:
            continue
        out["per_asset"][s] = {
            "level": round(float(a[-1]), 4),
            "momentum_5d": round(_momentum_pct(a, 5), 2),
            "momentum_20d": round(_momentum_pct(a, 20), 2),
        }
    if out["per_asset"]:
        out["ok"] = True
    return out


def _global_equities(prices: dict) -> dict:
    """EFA/EEM/FXI momentum — global beta, EM stress, China exposure."""
    syms = ["EFA", "EEM", "FXI"]
    out = {"per_asset": {}, "ok": False}
    for s in syms:
        a = prices.get(s)
        if a is None or len(a) < 6:
            continue
        out["per_asset"][s] = {
            "momentum_5d": round(_momentum_pct(a, 5), 2),
            "momentum_20d": round(_momentum_pct(a, 20), 2),
        }
    if out["per_asset"]:
        out["ok"] = True
    return out


# ============================================================
#  SECTOR TILTS — translate macro signals into sector adjustments
# ============================================================

def _sector_tilts(vix_ts, curve, credit, dollar, commodities) -> dict:
    """Convert all macro signals into per-sector score tilts in [-2, +2]."""
    tilts = {}

    def add(sector: str, delta: float):
        tilts[sector] = tilts.get(sector, 0.0) + float(delta)

    # --- Yield curve effects ---
    cs = curve.get("state", "unknown")
    if cs == "deeply_inverted":
        add("Financials", -1.0)
        add("Real Estate", -0.8)
        add("Utilities", +0.4)
        add("Consumer Staples", +0.5)
    elif cs == "inverted":
        add("Financials", -0.5)
        add("Utilities", +0.2)
    elif cs == "steep":
        add("Financials", +1.0)   # banks love a steep curve
        add("Real Estate", -0.4)
    elif cs == "normal":
        add("Financials", +0.4)

    # --- Credit stress ---
    crs = credit.get("state", "unknown")
    if crs == "stress_high":
        add("Financials", -0.8)
        add("Consumer Discretionary", -0.6)
        add("Energy", -0.4)
        add("Utilities", +0.5)
        add("Consumer Staples", +0.4)
    elif crs == "stress_moderate":
        add("Consumer Discretionary", -0.3)
        add("Utilities", +0.2)
    elif crs == "easing_strong":
        add("Financials", +0.8)
        add("Consumer Discretionary", +0.6)
        add("Real Estate", +0.5)

    # --- VIX term structure ---
    vts = vix_ts.get("state", "unknown")
    if vts == "backwardation_strong":
        # Acute fear — sellers are exhausted. Buy quality dips.
        add("Technology", +0.5)
        add("Healthcare", +0.4)
    elif vts == "contango_strong":
        # Calm — momentum regime. Cyclicals work.
        add("Industrials", +0.4)
        add("Consumer Discretionary", +0.4)

    # --- Dollar ---
    ds = dollar.get("state", "unknown")
    if ds == "strong_up":
        add("Technology", -0.6)        # weaker overseas earnings
        add("Materials", -0.7)
        add("Energy", -0.4)
        add("Industrials", -0.3)
    elif ds == "up":
        add("Materials", -0.3)
        add("Technology", -0.2)
    elif ds == "strong_down":
        add("Technology", +0.7)
        add("Materials", +0.7)
        add("Energy", +0.5)
        add("Industrials", +0.4)
    elif ds == "down":
        add("Technology", +0.3)
        add("Materials", +0.4)

    # --- Commodities ---
    per = commodities.get("per_asset", {})
    cper = per.get("CPER", {}).get("momentum_5d", 0.0)
    if cper > 2.0:                       # copper rising = global growth
        add("Industrials", +0.7)
        add("Materials", +0.6)
        add("Energy", +0.3)
    elif cper < -2.0:                    # copper falling = slowdown
        add("Industrials", -0.5)
        add("Materials", -0.5)

    uso = per.get("USO", {}).get("momentum_5d", 0.0)
    if uso > 3.0:
        add("Energy", +0.8)
        add("Industrials", -0.2)
        add("Consumer Discretionary", -0.2)  # gas-price drag
    elif uso < -3.0:
        add("Energy", -0.6)
        add("Consumer Discretionary", +0.3)

    gld = per.get("GLD", {}).get("momentum_5d", 0.0)
    if gld > 2.0:
        add("Materials", +0.4)               # gold miners
        # Gold rallying often = fear → small staples tilt
        add("Consumer Staples", +0.2)

    # --- Clamp to [-2, +2] ---
    return {k: round(max(-2.0, min(2.0, v)), 2) for k, v in tilts.items()}


# ============================================================
#  REGIME + EXPOSURE MODIFIER
# ============================================================

def _regime_and_exposure(vix_ts, curve, credit, dollar, commodities, globaleq) -> tuple:
    """Aggregate every signal into (macro_regime, exposure_modifier).

    Exposure modifier multiplies the dynamic exposure target. Range [0.5, 1.2].
    """
    score = 0.0  # positive = risk-on, negative = risk-off

    vts = vix_ts.get("state", "unknown")
    if vts == "backwardation_strong":
        score -= 1.5
    elif vts == "backwardation":
        score -= 0.7
    elif vts == "contango_strong":
        score += 1.0
    elif vts == "contango":
        score += 0.4

    cs = curve.get("state", "unknown")
    if cs == "deeply_inverted":
        score -= 1.0
    elif cs == "inverted":
        score -= 0.4
    elif cs == "steep":
        score += 0.8

    crs = credit.get("state", "unknown")
    if crs == "stress_high":
        score -= 1.5
    elif crs == "stress_moderate":
        score -= 0.5
    elif crs == "easing_strong":
        score += 1.0
    elif crs == "easing":
        score += 0.3

    ds = dollar.get("state", "unknown")
    if ds == "strong_up":
        score -= 0.5
    elif ds == "strong_down":
        score += 0.5

    # Global equities — when EFA + EEM both falling hard, risk-off
    glob = globaleq.get("per_asset", {})
    efa5 = glob.get("EFA", {}).get("momentum_5d", 0.0)
    eem5 = glob.get("EEM", {}).get("momentum_5d", 0.0)
    if efa5 < -2.0 and eem5 < -2.0:
        score -= 0.8
    elif efa5 > 2.0 and eem5 > 2.0:
        score += 0.6

    # Map score to regime
    if score >= 1.5:
        regime = "RISK_ON_STRONG"
        modifier = 1.15
    elif score >= 0.5:
        regime = "RISK_ON"
        modifier = 1.05
    elif score <= -1.5:
        regime = "RISK_OFF_STRONG"
        modifier = 0.55
    elif score <= -0.5:
        regime = "RISK_OFF"
        modifier = 0.75
    else:
        regime = "NEUTRAL"
        modifier = 1.00

    return regime, round(max(0.5, min(1.2, modifier)), 3), round(score, 2)


# ============================================================
#  PUBLIC API
# ============================================================

def _neutral_default(reason: str) -> dict:
    """Returned when everything fails. System keeps trading neutrally."""
    return {
        "ok": False,
        "reason": reason,
        "macro_regime": "NEUTRAL",
        "exposure_modifier": 1.0,
        "regime_score": 0.0,
        "sector_tilts": {},
        "vix_term_structure": {"state": "unknown", "ok": False},
        "yield_curve": {"state": "unknown", "ok": False},
        "credit_stress": {"state": "unknown", "ok": False},
        "dollar": {"state": "unknown", "ok": False},
        "commodities": {"per_asset": {}, "ok": False},
        "global_equities": {"per_asset": {}, "ok": False},
        "computed_at": datetime.utcnow().isoformat(),
        "data_age_seconds": None,
    }


def get_macro_signals() -> dict:
    """Top-level API. Always returns a dict — never raises."""
    now = time.time()
    cached = _macro_cache.get("data")
    if cached is not None and (now - _macro_cache["time"]) < _MACRO_CACHE_TTL:
        cached = dict(cached)
        cached["data_age_seconds"] = int(now - _macro_cache["time"])
        return cached

    try:
        prices = _fetch_all()
        if not prices:
            # Total fetch failure — fall back to last good
            last = _macro_cache.get("last_good")
            if last is not None:
                last = dict(last)
                last["data_age_seconds"] = int(now - _macro_cache["time"]) if _macro_cache["time"] else None
                last["reason"] = "fetch_failed_using_last_good"
                return last
            return _neutral_default("fetch_failed_no_cache")

        vix_ts = _vix_term_structure(prices)
        curve = _yield_curve(prices)
        credit = _credit_stress(prices)
        dollar = _dollar(prices)
        commod = _commodities(prices)
        globaleq = _global_equities(prices)
        tilts = _sector_tilts(vix_ts, curve, credit, dollar, commod)
        regime, modifier, regime_score = _regime_and_exposure(
            vix_ts, curve, credit, dollar, commod, globaleq
        )

        result = {
            "ok": True,
            "macro_regime": regime,
            "exposure_modifier": modifier,
            "regime_score": regime_score,
            "sector_tilts": tilts,
            "vix_term_structure": vix_ts,
            "yield_curve": curve,
            "credit_stress": credit,
            "dollar": dollar,
            "commodities": commod,
            "global_equities": globaleq,
            "computed_at": datetime.utcnow().isoformat(),
            "data_age_seconds": 0,
            "tickers_fetched": len(prices),
            "tickers_universe": len(ALL_TICKERS),
        }

        _macro_cache["data"] = result
        _macro_cache["time"] = now
        _macro_cache["last_good"] = result
        return result

    except Exception as e:
        logger.warning(f"cross_asset_macro.get_macro_signals failed: {e}")
        last = _macro_cache.get("last_good")
        if last is not None:
            last = dict(last)
            last["reason"] = f"exception_using_last_good: {e}"
            return last
        return _neutral_default(f"exception_no_cache: {e}")


def get_macro_status() -> dict:
    """Lightweight status for the API endpoint."""
    last = _macro_cache.get("data") or {}
    return {
        "engine": "cross_asset_macro_v1",
        "universe_size": len(ALL_TICKERS),
        "regime": last.get("macro_regime", "NEUTRAL"),
        "exposure_modifier": last.get("exposure_modifier", 1.0),
        "regime_score": last.get("regime_score", 0.0),
        "tickers_fetched": last.get("tickers_fetched"),
        "ok": last.get("ok", False),
        "cache_age_seconds": int(time.time() - _macro_cache["time"]) if _macro_cache["time"] else None,
    }
