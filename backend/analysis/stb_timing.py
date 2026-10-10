"""
STB Technical Timing, Price-Integrity and Event-Risk layer.

WHY THIS EXISTS
---------------
The STB (Stocks To Buy) engine screens for *fundamentally* sound compounders:
revenue growth, ROE, PEG, 12-month momentum, 50/200d trend, volatility cap.
That answers "is this a good business to own for 6-12 weeks?"

It does NOT answer "is today a good day to pay this price?"

A stock can pass every fundamental screen and still be a poor entry right now:
extended 3 ATRs above its 20-day mean, printing into the upper Bollinger band,
lagging SPY on a 20-day basis, or sitting in front of a known event. Buying
there produces an immediate drawdown even when the thesis is correct. This
module closes that gap: it scores SHORT-TERM ENTRY QUALITY, converts it into
an ACTIONABLE ENTRY PLAN, and flags EVENT RISK.

ZERO RATE-LIMIT SURFACE  (hard requirement)
-------------------------------------------
This module performs NO network I/O of any kind. There are no imports of
requests/urllib/yfinance and no data-source adapters. It operates exclusively
on price arrays the STB engine has ALREADY fetched and validated. Adding this
layer therefore cannot contribute a single additional outbound request, and
cannot get the system throttled or blocked. Keep it that way: if a future
signal needs new data, the caller must fetch it and pass it in.

PRICE-CORRUPTION DEFENCE  (hard requirement)
--------------------------------------------
This system has a documented history of batch-download MultiIndex scrambles
assigning one ticker's price series to another. A timing score computed on the
WRONG series is worse than no score: it is confident, specific and false.
`check_price_integrity()` therefore runs before any scoring, and refuses to
rate a symbol when the series:
    * does not match the engine's independently validated spot price,
    * is flat-lined or stale (repeated identical closes),
    * contains a physically implausible single-session move, or
    * has no dispersion at all.
On any failure the result is a neutral UNRATED with ZERO tilts, so a corrupt
series can never promote or demote a pick. Fail closed, never fail loud.

SIGNAL SELECTION RATIONALE
--------------------------
Short-horizon effects with the most durable out-of-sample evidence, weighted
by the robustness of that evidence:

  trend_stack      1.6  Price > MA20 > MA50 > MA200. Moving-average trend
                        filters are the most replicated equity timing result
                        (Faber 2007; Moskowitz-Ooi-Pedersen 2012).
  extension        1.5  Distance above MA20 in ATR units. Short-horizon
                        reversal dominates at the 1-20 day scale (Jegadeesh
                        1990; Lehmann 1990). Buying extended is the primary
                        way a correct thesis still loses money early.
  pullback         1.3  RSI(2) inside an established uptrend — buy weakness,
                        not strength, for short-horizon entries (Connors 2008).
  near_52w_high    1.2  Proximity to the 52-week high. One of the strongest
                        documented cross-sectional predictors of forward
                        return (George & Hwang 2004). Stocks near highs keep
                        working; stocks far below them keep lagging.
  trend_quality    1.1  R-squared of a log-price regression. Separates a clean
                        trend from noise; low R2 = chop, where stops get hit
                        by randomness and there is no edge.
  rel_strength     1.0  20-day return vs benchmark. Relative laggards keep
                        lagging over short horizons.
  macd_hist        0.9  MACD histogram level and slope. Momentum confirmation.
  bollinger_pos    0.9  %B over 20 days. At/above the upper band is a poor
                        entry; mid-band inside an uptrend is a good one.
  support_distance 0.9  Distance to the nearest 60-day support in ATR units.
                        Defines where the real stop sits and therefore the
                        true risk of the trade, not a round-number guess.
  vol_regime       0.8  10d vs 60d realised vol. A volatility spike raises the
                        probability of near-term adverse excursion.
  drawdown_risk    0.8  Current drawdown from the 20-day high.
  vol_skew         0.7  Upside vs downside semi-deviation. Prefer names whose
                        volatility is upside-skewed (Sortino logic).
  volume_trend     0.6  OBV slope — accumulation vs distribution.
  gap_risk         0.6  Frequency of large overnight gaps. Gap-prone names
                        blow through stops, so a stop cannot be trusted.

EVENT RISK
----------
Only DETERMINISTIC, calendar-derivable events are modelled. We deliberately do
NOT guess at CPI or FOMC dates: those move year to year and a wrong date is
worse than no date. What is encoded:

  * Non-farm payrolls  — first Friday of the month (BLS standing rule).
  * Monthly OPEX       — third Friday of the month.
  * Triple witching    — third Friday of Mar/Jun/Sep/Dec.
  * Quarter-end        — final three sessions of Mar/Jun/Sep/Dec (rebalancing).

WEATHER / SEASONAL EXPOSURE
---------------------------
Weather genuinely moves specific sectors, but a live weather feed is NOT an
approved data source for this system and would add outbound requests. Instead
we encode STRUCTURAL SEASONAL EXPOSURE WINDOWS, which are calendar-
deterministic and require no feed. This flags that a holding is entering a
weather-sensitive period; it is explicitly NOT a forecast:

  * Atlantic hurricane season  Jun 1 - Nov 30 (NOAA), peak Aug-Oct
                               -> Insurance/Financials, Gulf-coast Energy,
                                  Utilities, Real Estate.
  * Winter heating season      Nov 1 - Mar 31 -> Utilities, Energy (nat gas).
  * Summer cooling peak        Jun - Aug      -> Utilities.
  * Holiday retail season      Nov - Dec      -> Consumer Cyclical/Defensive.

OUTPUT IS ADVISORY AND CAPPED. The caller applies `score_tilt` (bounded +/-10)
and `conf_tilt` (bounded -6..+3). This layer re-ranks and sizes; it does not
fabricate conviction and cannot single-handedly invent a pick.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import numpy as np

__all__ = [
    "compute_timing",
    "assess_event_risk",
    "check_price_integrity",
    "GRADE_ORDER",
    "SIGNAL_WEIGHTS",
]

# Ordered worst -> best so callers can sort or compare grades.
GRADE_ORDER = ["AVOID", "WAIT", "ACCUMULATE", "BUY_NOW"]

SIGNAL_WEIGHTS = {
    "trend_stack": 1.6,
    "extension": 1.5,
    "pullback": 1.3,
    "near_52w_high": 1.2,
    "trend_quality": 1.1,
    "rel_strength": 1.0,
    "macd_hist": 0.9,
    "bollinger_pos": 0.9,
    "support_distance": 0.9,
    "vol_regime": 0.8,
    "drawdown_risk": 0.8,
    "vol_skew": 0.7,
    "volume_trend": 0.6,
    "gap_risk": 0.6,
}

# Fraction of total weight that must actually be computable before we publish
# a grade. Below this the read is built on too few signals to be trustworthy.
_MIN_WEIGHT_COVERAGE = 0.55

# Minimum closes required before we will score timing at all. Below this the
# short-horizon indicators are noise and we return an explicit "no opinion".
_MIN_BARS = 60

# Tilt bounds. These are the hard contract with the STB engine.
_SCORE_TILT_CAP = 10.0
_CONF_TILT_MIN = -6
_CONF_TILT_MAX = 3

# Price-integrity thresholds.
_PRICE_MATCH_TOL = 0.05   # series last close must be within 5% of spot
_IMPLAUSIBLE_MOVE = 0.60  # single-session move beyond this = corrupt series
_MAX_STALE_FRAC = 0.30    # >30% repeated closes in the window = stale feed


# ─────────────────────────────────────────────────────────────────────────────
# Array hygiene
# ─────────────────────────────────────────────────────────────────────────────
def _clean(arr) -> np.ndarray:
    """Coerce anything to a finite, strictly-positive 1-D float array.

    Non-finite and non-positive prices are DROPPED rather than imputed: a zero
    or NaN close is corrupt data, and interpolating over it would silently
    manufacture a price that never traded.
    """
    try:
        a = np.asarray(arr, dtype=float).ravel()
    except Exception:
        return np.array([], dtype=float)
    if a.size == 0:
        return a
    a = a[np.isfinite(a)]
    return a[a > 0]


def _clean_nonneg(arr) -> np.ndarray:
    """Same as _clean but allows zero (volume can legitimately be zero)."""
    try:
        a = np.asarray(arr, dtype=float).ravel()
    except Exception:
        return np.array([], dtype=float)
    if a.size == 0:
        return a
    a = a[np.isfinite(a)]
    return a[a >= 0]


def _finite(x, default=None):
    """Return float(x) when finite, else `default`."""
    try:
        f = float(x)
    except Exception:
        return default
    return f if math.isfinite(f) else default


def _r(x, nd=2):
    """Round for JSON, mapping non-finite to None."""
    f = _finite(x)
    return None if f is None else round(f, nd)


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


# ─────────────────────────────────────────────────────────────────────────────
# Price integrity — runs BEFORE any scoring
# ─────────────────────────────────────────────────────────────────────────────
def check_price_integrity(closes, expected_price=None, tol: float = _PRICE_MATCH_TOL) -> dict:
    """Verify a close series is internally coherent and matches the spot price.

    The decisive check is `expected_price`: the STB engine independently
    validates every pick's spot price through multi-source quotes. If the
    cached history disagrees with that validated price by more than `tol`, the
    history belongs to a different ticker or a different era. Either way it
    must not drive a buy decision.

    Returns {"ok": bool, "reason": str, "last": float|None, "divergence_pct":
    float|None}. Never raises.
    """
    out = {"ok": False, "reason": "", "last": None, "divergence_pct": None}
    try:
        c = _clean(closes)
        if c.size < _MIN_BARS:
            out["reason"] = "insufficient_history"
            return out

        last = float(c[-1])
        out["last"] = round(last, 4)

        # 1. Does the history agree with the engine's validated spot price?
        exp = _finite(expected_price)
        if exp is not None and exp > 0:
            div = abs(last - exp) / exp
            out["divergence_pct"] = round(div * 100.0, 2)
            if div > tol:
                out["reason"] = "price_mismatch"
                return out

        # 2. Flat-lined / stale feed: no movement at all in the recent window.
        w = c[-min(60, c.size):]
        if float(np.std(w)) <= 0:
            out["reason"] = "zero_variance"
            return out
        if w.size >= 20 and np.all(w[-20:] == w[-1]):
            out["reason"] = "flatlined"
            return out
        dup_frac = float(np.mean(np.diff(w) == 0.0)) if w.size > 1 else 1.0
        if dup_frac > _MAX_STALE_FRAC:
            out["reason"] = "stale_repeated_closes"
            return out

        # 3. Physically implausible session move. yfinance closes are split
        #    adjusted, so a >60% single-session move in this universe (large
        #    cap, >$10, <80% annualised vol) indicates a contaminated series
        #    rather than a real print.
        rets = np.abs(np.diff(w) / w[:-1])
        rets = rets[np.isfinite(rets)]
        if rets.size and float(np.max(rets)) > _IMPLAUSIBLE_MOVE:
            out["reason"] = "implausible_move"
            return out

        out["ok"] = True
        return out
    except Exception:
        return {"ok": False, "reason": "integrity_check_error",
                "last": None, "divergence_pct": None}


# ─────────────────────────────────────────────────────────────────────────────
# Indicator primitives (close-only, dependency-light)
# ─────────────────────────────────────────────────────────────────────────────
def _sma(a: np.ndarray, n: int):
    if a.size < n or n <= 0:
        return None
    return _finite(np.mean(a[-n:]))


def _ema_series(a: np.ndarray, n: int):
    """Full EMA series, seeded with the first value (pandas `adjust=False`)."""
    if a.size == 0 or n <= 0:
        return None
    k = 2.0 / (n + 1.0)
    out = np.empty(a.size, dtype=float)
    out[0] = a[0]
    for i in range(1, a.size):
        out[i] = a[i] * k + out[i - 1] * (1.0 - k)
    return out


def _rsi(a: np.ndarray, n: int):
    """Wilder RSI over the trailing window. None when history is too short."""
    if a.size < n + 1 or n <= 0:
        return None
    d = np.diff(a[-(n + 1):])
    gain = float(np.mean(np.clip(d, 0.0, None)))
    loss = float(np.mean(np.clip(-d, 0.0, None)))
    if loss <= 1e-12:
        # No down moves in the window. RSI is 100 by definition when there is
        # upside; a flat line carries no information, so report neutral.
        return 100.0 if gain > 1e-12 else 50.0
    return _finite(100.0 - 100.0 / (1.0 + gain / loss))


def _atr_proxy(a: np.ndarray, n: int = 20):
    """Close-to-close ATR proxy: mean absolute daily change over `n` bars.

    Highs/lows are not available in the STB price cache. Close-to-close
    understates true range by roughly 20-30%, which makes the extension guard
    slightly CONSERVATIVE (it reports more ATRs of extension than a true ATR
    would) and makes ATR-derived stops slightly TIGHTER. The first bias is
    desirable for an entry filter; the second is corrected in the entry plan,
    which widens the stop by an explicit factor.
    """
    if a.size < n + 1 or n <= 0:
        return None
    v = _finite(np.mean(np.abs(np.diff(a[-(n + 1):]))))
    if v is None or v <= 0:
        return None
    return v


def _ann_vol(a: np.ndarray, n: int):
    """Annualised realised volatility (%) over the last `n` returns."""
    if a.size < n + 1 or n <= 0:
        return None
    r = np.diff(a[-(n + 1):]) / a[-(n + 1):-1]
    r = r[np.isfinite(r)]
    if r.size < 2:
        return None
    return _finite(float(np.std(r)) * math.sqrt(252.0) * 100.0)


def _macd_hist(a: np.ndarray):
    """Return (histogram_now, histogram_prev) for the standard 12/26/9 MACD."""
    if a.size < 35:
        return None, None
    e12 = _ema_series(a, 12)
    e26 = _ema_series(a, 26)
    if e12 is None or e26 is None:
        return None, None
    macd = e12 - e26
    sig = _ema_series(macd, 9)
    if sig is None:
        return None, None
    hist = macd - sig
    if hist.size < 2:
        return None, None
    return _finite(hist[-1]), _finite(hist[-2])


def _reg_r2_slope(a: np.ndarray, n: int = 40):
    """R-squared and slope of a least-squares fit on log prices.

    R2 measures how much of the move is trend versus noise. A high-R2 uptrend
    is tradeable; a low-R2 drift is chop, where short-horizon entries have no
    edge and stops get hit by randomness.
    """
    if a.size < n or n < 5:
        return None, None
    y = np.log(a[-n:])
    if not np.all(np.isfinite(y)):
        return None, None
    x = np.arange(n, dtype=float)
    xm, ym = x.mean(), y.mean()
    sxx = float(np.sum((x - xm) ** 2))
    syy = float(np.sum((y - ym) ** 2))
    if sxx <= 0 or syy <= 0:
        return None, None
    sxy = float(np.sum((x - xm) * (y - ym)))
    return _clamp(_finite((sxy * sxy) / (sxx * syy), 0.0), 0.0, 1.0), _finite(sxy / sxx, 0.0)


def _obv_slope(closes: np.ndarray, volumes: np.ndarray, n: int = 20):
    """Slope of On-Balance Volume over `n` bars, scaled to [-1, 1].

    Positive = accumulation on up days; negative = distribution.
    """
    m = min(closes.size, volumes.size)
    if m < n + 1 or n < 5:
        return None
    c = closes[-m:]
    v = volumes[-m:]
    obv = np.concatenate(([0.0], np.cumsum(np.sign(np.diff(c)) * v[1:])))
    seg = obv[-n:]
    avg_v = float(np.mean(v[-n:]))
    if not math.isfinite(avg_v) or avg_v <= 0:
        return None
    x = np.arange(seg.size, dtype=float)
    xm = x.mean()
    sxx = float(np.sum((x - xm) ** 2))
    if sxx <= 0:
        return None
    slope = float(np.sum((x - xm) * (seg - seg.mean())) / sxx)
    return _clamp(_finite(slope / avg_v, 0.0), -1.0, 1.0)


def _nearest_support(a: np.ndarray, n: int = 60):
    """Lowest swing low below spot within the trailing `n` sessions.

    A swing low is a local minimum over a +/-2 session window. Falling back to
    the window minimum keeps the signal available on smooth series.
    """
    if a.size < max(n, 10):
        return None
    w = a[-n:]
    px = float(a[-1])
    lows = []
    for i in range(2, w.size - 2):
        v = float(w[i])
        if v <= float(np.min(w[i - 2:i + 3])) and v < px:
            lows.append(v)
    if lows:
        return max(lows)          # nearest support beneath us
    lo = float(np.min(w))
    return lo if lo < px else None


# ─────────────────────────────────────────────────────────────────────────────
# Event risk — deterministic calendar only
# ─────────────────────────────────────────────────────────────────────────────
def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    """`nth` occurrence (1-based) of `weekday` (Mon=0) in the given month."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (nth - 1))


def _last_business_days(year: int, month: int, count: int):
    """The final `count` weekdays of a month (market holidays not modelled)."""
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    d = nxt - timedelta(days=1)
    out = []
    while len(out) < count and d.month == month:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out


# Sector -> seasonal/weather exposure windows. Months inclusive; windows that
# wrap the year-end are expressed as two ranges.
_SEASONAL_WINDOWS = [
    {
        "name": "Atlantic hurricane season",
        "months": [(6, 11)],
        "peak": [(8, 10)],
        "sectors": {"Financial Services", "Energy", "Utilities", "Real Estate"},
        "note": "Catastrophe-loss and Gulf-coast refining/throughput exposure",
    },
    {
        "name": "Winter heating season",
        "months": [(11, 12), (1, 3)],
        "peak": [(12, 12), (1, 2)],
        "sectors": {"Utilities", "Energy"},
        "note": "Heating-degree-day demand drives nat-gas and utility load",
    },
    {
        "name": "Summer cooling peak",
        "months": [(6, 8)],
        "peak": [(7, 8)],
        "sectors": {"Utilities"},
        "note": "Cooling-degree-day load and grid-stress exposure",
    },
    {
        "name": "Holiday retail season",
        "months": [(11, 12)],
        "peak": [(11, 12)],
        "sectors": {"Consumer Cyclical", "Consumer Defensive"},
        "note": "Weather-sensitive foot traffic and holiday comp sales",
    },
]


def _month_in(ranges, month: int) -> bool:
    return any(lo <= month <= hi for lo, hi in ranges)


def assess_event_risk(sector=None, news=None, earnings_days=None, now=None) -> dict:
    """Score known, calendar-deterministic event risk around an entry.

    Args:
        sector:        GICS-style sector string (may be None/unknown).
        news:          The STB news overlay dict, i.e. {"sentiment": "..."}.
        earnings_days: Days until the next report, or None if unknown.
        now:           `date`/`datetime` override for testing. Defaults to today.

    Returns a dict that is always well-formed:
        {"level": LOW|MODERATE|ELEVATED|HIGH, "points": int,
         "conf_penalty": int (<=0), "items": [str, ...]}
    Never raises.
    """
    try:
        if isinstance(now, datetime):
            today = now.date()
        elif isinstance(now, date):
            today = now
        else:
            today = date.today()

        pts = 0
        items = []

        # ── Earnings proximity ───────────────────────────────────────────────
        # The STB engine already hard-drops anything inside 14 days. This
        # grades the 15-45 day band, where the report still lands inside a
        # 6-12 week hold and therefore still matters.
        ed = _finite(earnings_days)
        if ed is not None and ed >= 0:
            ed = int(ed)
            if ed < 15:
                pts += 3
                items.append(f"Earnings in {ed}d — binary event inside the entry window")
            elif ed <= 21:
                pts += 2
                items.append(f"Earnings in {ed}d — report lands early in the hold")
            elif ed <= 30:
                pts += 1
                items.append(f"Earnings in {ed}d — report lands inside the hold")
            elif ed <= 45:
                items.append(f"Earnings in {ed}d — inside a 6-12 week hold")

        # ── Company news ─────────────────────────────────────────────────────
        lbl = str((news or {}).get("sentiment") or "").upper() if isinstance(news, dict) else ""
        if lbl == "VERY_NEGATIVE":
            pts += 2
            items.append("Headline flow very negative — elevated headline risk")
        elif lbl == "NEGATIVE":
            pts += 1
            items.append("Headline flow negative — monitor")

        # ── Deterministic macro calendar (next 5 sessions) ───────────────────
        macro_pts = 0
        for ahead in range(0, 6):
            d = today + timedelta(days=ahead)
            if d.weekday() >= 5:
                continue
            if d == _nth_weekday(d.year, d.month, 4, 1):       # first Friday
                macro_pts = max(macro_pts, 1)
                items.append(f"Payrolls release {d.isoformat()} — index-level volatility")
            if d == _nth_weekday(d.year, d.month, 4, 3):       # third Friday
                if d.month in (3, 6, 9, 12):
                    macro_pts = max(macro_pts, 2)
                    items.append(f"Triple witching {d.isoformat()} — expiry flow distorts price")
                else:
                    macro_pts = max(macro_pts, 1)
                    items.append(f"Monthly opex {d.isoformat()} — pinning and expiry flow")
            if d.month in (3, 6, 9, 12) and d in _last_business_days(d.year, d.month, 3):
                macro_pts = max(macro_pts, 1)
                items.append(f"Quarter-end {d.isoformat()} — index rebalancing flow")
        pts += min(2, macro_pts)

        # ── Structural seasonal / weather exposure (NOT a forecast) ──────────
        sec = (sector or "").strip()
        if sec:
            for w in _SEASONAL_WINDOWS:
                if sec in w["sectors"] and _month_in(w["months"], today.month):
                    peak = _month_in(w["peak"], today.month)
                    pts += 1 if peak else 0
                    items.append(f"{w['name']}{' (peak)' if peak else ''} — {w['note']}")

        pts = int(max(0, pts))
        if pts >= 5:
            level, pen = "HIGH", -2
        elif pts >= 3:
            level, pen = "ELEVATED", -1
        elif pts >= 1:
            level, pen = "MODERATE", 0
        else:
            level, pen = "LOW", 0

        return {"level": level, "points": pts, "conf_penalty": pen, "items": items[:6]}
    except Exception:
        # Fail-safe: an exception here must never cost a pick its listing.
        return {"level": "LOW", "points": 0, "conf_penalty": 0, "items": []}


# ─────────────────────────────────────────────────────────────────────────────
# Individual timing signals
#
# Every signal returns either None (not computable -> dropped and the weights
# renormalise) or {"p": 0..1, "state": BULL|BEAR|NEUTRAL, "detail": str}
# where `p` is ENTRY QUALITY: 1.0 = ideal entry, 0.0 = worst entry.
# ─────────────────────────────────────────────────────────────────────────────
def _sig_trend_stack(c):
    px = float(c[-1])
    ma20, ma50, ma200 = _sma(c, 20), _sma(c, 50), _sma(c, 200)
    if ma20 is None or ma50 is None:
        return None
    checks, passed, parts = 0, 0, []
    checks += 1
    if px > ma20:
        passed += 1
        parts.append("price>20d")
    checks += 1
    if ma20 > ma50:
        passed += 1
        parts.append("20d>50d")
    if ma200 is not None:
        checks += 1
        if ma50 > ma200:
            passed += 1
            parts.append("50d>200d")
        checks += 1
        if px > ma200:
            passed += 1
            parts.append("price>200d")
    p = passed / float(checks)
    state = "BULL" if p >= 0.75 else "BEAR" if p <= 0.25 else "NEUTRAL"
    return {"p": p, "state": state,
            "detail": (", ".join(parts) if parts else "no MA conditions met") + f" ({passed}/{checks})"}


def _sig_extension(c):
    """Distance above the 20-day mean in ATRs — the core 'do not pay up' guard.

    Entering more than ~2.5 ATRs above the mean carries a materially higher
    chance of an immediate adverse excursion even inside a healthy uptrend.
    """
    ma20, atr = _sma(c, 20), _atr_proxy(c, 20)
    if ma20 is None or atr is None:
        return None
    ext = (float(c[-1]) - ma20) / atr
    if ext <= 0.5:
        p, state = 1.0, "BULL"
    elif ext <= 1.5:
        p, state = 0.85, "BULL"
    elif ext <= 2.5:
        p, state = 0.55, "NEUTRAL"
    elif ext <= 3.5:
        p, state = 0.25, "BEAR"
    else:
        p, state = 0.0, "BEAR"
    return {"p": p, "state": state, "detail": f"{ext:+.1f} ATR vs 20d mean",
            "value": _r(ext)}


def _sig_pullback(c):
    """RSI(2) read, interpreted relative to the prevailing 50-day trend.

    In an uptrend a low RSI(2) is an opportunity, not a warning. Outside an
    uptrend the identical reading is just weakness and earns no credit.
    """
    r2, ma50 = _rsi(c, 2), _sma(c, 50)
    if r2 is None or ma50 is None:
        return None
    if float(c[-1]) > ma50:
        if r2 <= 15:
            p, state, lab = 1.0, "BULL", "deep pullback in uptrend — prime entry"
        elif r2 <= 40:
            p, state, lab = 0.85, "BULL", "pullback in uptrend — good entry"
        elif r2 <= 75:
            p, state, lab = 0.6, "NEUTRAL", "mid-range"
        elif r2 <= 95:
            p, state, lab = 0.35, "NEUTRAL", "short-term overbought"
        else:
            p, state, lab = 0.1, "BEAR", "stretched — poor entry"
    else:
        if r2 <= 15:
            p, state, lab = 0.3, "BEAR", "oversold but below 50d — falling knife"
        elif r2 <= 60:
            p, state, lab = 0.35, "BEAR", "weak — below 50d"
        else:
            p, state, lab = 0.25, "BEAR", "bounce into a downtrend"
    return {"p": p, "state": state, "detail": f"RSI(2) {r2:.0f} — {lab}",
            "value": _r(r2, 1)}


def _sig_near_52w_high(c):
    """Proximity to the 52-week high (George & Hwang 2004).

    Stocks trading near their 52-week high continue to outperform; those far
    below it continue to lag. Being AT the high is strong, not dangerous —
    extension risk is handled separately by `_sig_extension`.
    """
    if c.size < 120:
        return None
    w = c[-min(252, c.size):]
    hi = float(np.max(w))
    if hi <= 0:
        return None
    pct = float(c[-1]) / hi * 100.0
    if pct >= 97:
        p, state, lab = 1.0, "BULL", "at 52-week highs"
    elif pct >= 90:
        p, state, lab = 0.9, "BULL", "within 10% of 52-week high"
    elif pct >= 80:
        p, state, lab = 0.6, "NEUTRAL", "10-20% below 52-week high"
    elif pct >= 65:
        p, state, lab = 0.3, "BEAR", "20-35% below 52-week high"
    else:
        p, state, lab = 0.05, "BEAR", "deeply below 52-week high"
    return {"p": p, "state": state, "detail": f"{pct:.0f}% of 52w high — {lab}",
            "value": _r(pct, 1)}


def _sig_trend_quality(c):
    r2, slope = _reg_r2_slope(c, 40)
    if r2 is None or slope is None:
        return None
    if slope > 0:
        p = 0.5 + 0.5 * r2
        state = "BULL" if r2 >= 0.4 else "NEUTRAL"
        lab = "clean uptrend" if r2 >= 0.6 else "uptrend with noise" if r2 >= 0.3 else "choppy drift"
    else:
        p = 0.4 * (1.0 - r2)
        state = "BEAR" if r2 >= 0.3 else "NEUTRAL"
        lab = "persistent downtrend" if r2 >= 0.6 else "drifting lower"
    return {"p": _clamp(p, 0.0, 1.0), "state": state,
            "detail": f"40d R2 {r2:.2f} — {lab}", "value": _r(r2)}


def _sig_rel_strength(c, bench):
    if bench is None or bench.size < 21 or c.size < 21:
        return None
    sr = (float(c[-1]) / float(c[-21]) - 1.0) * 100.0
    br = (float(bench[-1]) / float(bench[-21]) - 1.0) * 100.0
    if not (math.isfinite(sr) and math.isfinite(br)):
        return None
    spread = sr - br
    if spread >= 5:
        p, state = 1.0, "BULL"
    elif spread >= 0:
        p, state = 0.75, "BULL"
    elif spread >= -5:
        p, state = 0.45, "NEUTRAL"
    elif spread >= -12:
        p, state = 0.2, "BEAR"
    else:
        p, state = 0.0, "BEAR"
    return {"p": p, "state": state,
            "detail": f"20d {sr:+.1f}% vs benchmark {br:+.1f}% ({spread:+.1f} pts)",
            "value": _r(spread, 1)}


def _sig_macd(c):
    h, hp = _macd_hist(c)
    if h is None or hp is None:
        return None
    rising = h > hp
    if h > 0 and rising:
        p, state, lab = 1.0, "BULL", "positive and expanding"
    elif h > 0:
        p, state, lab = 0.6, "NEUTRAL", "positive but fading"
    elif rising:
        p, state, lab = 0.5, "NEUTRAL", "negative but improving"
    else:
        p, state, lab = 0.1, "BEAR", "negative and deteriorating"
    return {"p": p, "state": state, "detail": f"MACD histogram {lab}", "value": _r(h, 3)}


def _sig_bollinger(c):
    if c.size < 20:
        return None
    w = c[-20:]
    mid, sd = float(np.mean(w)), float(np.std(w))
    if not math.isfinite(sd) or sd <= 0:
        return None
    upper, lower = mid + 2 * sd, mid - 2 * sd
    pb = _finite((float(c[-1]) - lower) / (upper - lower))
    if pb is None:
        return None
    if pb >= 1.0:
        p, state, lab = 0.05, "BEAR", "at/above upper band — poor entry"
    elif pb >= 0.85:
        p, state, lab = 0.3, "NEUTRAL", "near upper band"
    elif pb >= 0.35:
        p, state, lab = 1.0, "BULL", "mid-band — healthy entry zone"
    elif pb >= 0.1:
        p, state, lab = 0.7, "NEUTRAL", "lower half of the band"
    else:
        p, state, lab = 0.45, "NEUTRAL", "at/below lower band"
    return {"p": p, "state": state, "detail": f"%B {pb:.2f} — {lab}", "value": _r(pb)}


def _sig_support_distance(c):
    """Distance to the nearest 60-day support, in ATRs.

    This is where the real stop belongs. A support far below spot means the
    honest stop is wide and the position must be sized down; support just
    beneath spot means a tight, structurally sound stop.
    """
    sup, atr = _nearest_support(c, 60), _atr_proxy(c, 20)
    if sup is None or atr is None or atr <= 0:
        return None
    dist = (float(c[-1]) - sup) / atr
    if dist <= 1.5:
        p, state, lab = 1.0, "BULL", "support just below — tight structural stop"
    elif dist <= 3.0:
        p, state, lab = 0.75, "BULL", "support within reach"
    elif dist <= 5.0:
        p, state, lab = 0.4, "NEUTRAL", "support distant — wide stop required"
    else:
        p, state, lab = 0.1, "BEAR", "no support nearby — air pocket beneath"
    return {"p": p, "state": state,
            "detail": f"support ${sup:,.2f} is {dist:.1f} ATR below — {lab}",
            "value": _r(dist)}


def _sig_vol_regime(c):
    v10, v60 = _ann_vol(c, 10), _ann_vol(c, 60)
    if v10 is None or v60 is None or v60 <= 0:
        return None
    ratio = v10 / v60
    if ratio <= 0.8:
        p, state, lab = 1.0, "BULL", "compressed vs baseline"
    elif ratio <= 1.2:
        p, state, lab = 0.75, "NEUTRAL", "in line with baseline"
    elif ratio <= 1.8:
        p, state, lab = 0.35, "BEAR", "elevated vs baseline"
    else:
        p, state, lab = 0.05, "BEAR", "spiking — unstable tape"
    return {"p": p, "state": state,
            "detail": f"10d vol {v10:.0f}% vs 60d {v60:.0f}% ({ratio:.2f}x) — {lab}",
            "value": _r(ratio)}


def _sig_drawdown(c):
    if c.size < 20:
        return None
    hi = float(np.max(c[-20:]))
    if hi <= 0:
        return None
    dd = (float(c[-1]) / hi - 1.0) * 100.0
    if dd >= -2:
        p, state, lab = 0.8, "NEUTRAL", "at 20d highs"
    elif dd >= -6:
        p, state, lab = 1.0, "BULL", "shallow pullback from 20d high"
    elif dd >= -12:
        p, state, lab = 0.6, "NEUTRAL", "moderate pullback"
    elif dd >= -20:
        p, state, lab = 0.3, "BEAR", "deep pullback — trend under pressure"
    else:
        p, state, lab = 0.05, "BEAR", "severe drawdown from 20d high"
    return {"p": p, "state": state, "detail": f"{dd:.1f}% from 20d high — {lab}",
            "value": _r(dd, 1)}


def _sig_vol_skew(c):
    """Upside vs downside semi-deviation over 60 sessions (Sortino logic).

    Two names can share one volatility number while one of them expresses it
    almost entirely to the upside. For a long book that distinction is the
    difference between tolerable noise and real loss.
    """
    if c.size < 61:
        return None
    r = np.diff(c[-61:]) / c[-61:-1]
    r = r[np.isfinite(r)]
    if r.size < 30:
        return None
    up, dn = r[r > 0], r[r < 0]
    if up.size < 5 or dn.size < 5:
        return None
    u, d = float(np.std(up)), float(np.std(dn))
    if not math.isfinite(d) or d <= 0:
        return None
    ratio = u / d
    if ratio >= 1.25:
        p, state, lab = 1.0, "BULL", "volatility skewed to the upside"
    elif ratio >= 1.0:
        p, state, lab = 0.75, "BULL", "mildly upside-skewed"
    elif ratio >= 0.85:
        p, state, lab = 0.45, "NEUTRAL", "balanced"
    else:
        p, state, lab = 0.15, "BEAR", "downside moves dominate"
    return {"p": p, "state": state,
            "detail": f"up/down vol {ratio:.2f}x — {lab}", "value": _r(ratio)}


def _sig_volume(c, v):
    if v is None:
        return None
    s = _obv_slope(c, v, 20)
    if s is None:
        return None
    if s >= 0.15:
        p, state, lab = 1.0, "BULL", "accumulation"
    elif s >= 0.0:
        p, state, lab = 0.7, "NEUTRAL", "mild accumulation"
    elif s >= -0.15:
        p, state, lab = 0.4, "NEUTRAL", "mild distribution"
    else:
        p, state, lab = 0.1, "BEAR", "distribution"
    return {"p": p, "state": state, "detail": f"OBV slope {s:+.2f} — {lab}", "value": _r(s)}


def _sig_gap_risk(c):
    """Frequency of large session-over-session jumps in the last 60 bars.

    A gap-prone name cannot be risk-managed with a stop: price opens through
    it. High gap frequency therefore degrades entry quality regardless of how
    attractive the trend looks.
    """
    if c.size < 61:
        return None
    r = np.abs(np.diff(c[-61:]) / c[-61:-1])
    r = r[np.isfinite(r)]
    if r.size < 30:
        return None
    big = float(np.mean(r > 0.05)) * 100.0     # % of sessions moving >5%
    if big <= 2:
        p, state, lab = 1.0, "BULL", "orderly tape — stops are reliable"
    elif big <= 6:
        p, state, lab = 0.7, "NEUTRAL", "occasional large sessions"
    elif big <= 12:
        p, state, lab = 0.35, "BEAR", "gap-prone — stops may slip"
    else:
        p, state, lab = 0.05, "BEAR", "highly gap-prone — stops unreliable"
    return {"p": p, "state": state,
            "detail": f"{big:.0f}% of sessions moved >5% — {lab}", "value": _r(big, 1)}


# ─────────────────────────────────────────────────────────────────────────────
# Grade caps — a weighted average can hide a fatal flaw
# ─────────────────────────────────────────────────────────────────────────────
def _apply_grade_caps(grade, computed):
    """Cap the grade when a single reading is disqualifying on its own.

    A composite score is an average, and an average can be dragged back to
    respectable by many good readings while one catastrophic reading is the
    only one that will actually cost money. A stock 4 ATRs above its 20-day
    mean is a bad entry at any composite score: the other signals are all
    *describing the same melt-up* that makes it dangerous.

    These caps only ever LOWER a grade, never raise one.
    """
    caps = []

    ext = _finite((computed.get("extension") or {}).get("value"))
    if ext is not None and ext > 3.5:
        caps.append(("WAIT", f"Extended {ext:+.1f} ATR above the 20-day mean — "
                             f"mean reversion risk dominates"))

    boll = _finite((computed.get("bollinger_pos") or {}).get("value"))
    if boll is not None and boll >= 1.0 and ext is not None and ext > 2.5:
        caps.append(("WAIT", "Trading above the upper Bollinger band while "
                             "already extended — buying the top of the range"))

    vr = _finite((computed.get("vol_regime") or {}).get("value"))
    if vr is not None and vr > 1.8:
        caps.append(("ACCUMULATE", f"Short-term volatility spiking at {vr:.1f}x "
                                   f"baseline — unstable tape"))

    tsp = _finite((computed.get("trend_stack") or {}).get("p"))
    if tsp is not None and tsp <= 0.25:
        caps.append(("WAIT", "Price below its moving-average stack — "
                             "no trend support beneath the entry"))

    gap = _finite((computed.get("gap_risk") or {}).get("value"))
    if gap is not None and gap > 12:
        caps.append(("ACCUMULATE", f"Gap-prone: {gap:.0f}% of sessions moved >5% — "
                                   f"a stop cannot be relied on"))

    if not caps:
        return grade, []

    cap_idx = min(GRADE_ORDER.index(g) for g, _ in caps)
    if cap_idx >= GRADE_ORDER.index(grade):
        return grade, []          # cap is not binding

    capped = GRADE_ORDER[cap_idx]
    reasons = [r for g, r in caps if GRADE_ORDER.index(g) <= cap_idx]
    return capped, reasons


# Highest score_tilt permitted within each grade band. When a cap demotes a
# grade, the tilt is demoted with it — otherwise a capped pick would keep the
# ranking boost the cap was meant to remove.
_BAND_MAX_TILT = {
    "BUY_NOW": _SCORE_TILT_CAP,
    "ACCUMULATE": (70.0 - 55.0) / 45.0 * _SCORE_TILT_CAP,
    "WAIT": 0.0,
    "AVOID": (40.0 - 55.0) / 45.0 * _SCORE_TILT_CAP,
}


# ─────────────────────────────────────────────────────────────────────────────
# Entry plan — turns the score into an action
# ─────────────────────────────────────────────────────────────────────────────
def _build_entry_plan(c, grade):
    """Produce an actionable entry zone, structural stop and size factor.

    `size_factor` is the fraction of a full position to deploy today. This is
    the mechanism that protects capital: a technically poor entry is not
    forbidden, it is simply sized down until the tape confirms.
    """
    try:
        px = float(c[-1])
        atr = _atr_proxy(c, 20)
        ma20 = _sma(c, 20)
        if atr is None or ma20 is None or px <= 0:
            return None

        # Ideal entry: the 20-day mean, or half an ATR below spot when price is
        # already at/below the mean. Never quote an entry above the last price.
        # Round DOWN rather than to-nearest: rounding up by a cent would print
        # an "ideal entry" above spot, which is not an entry, it is a chase.
        def _floor2(x):
            return math.floor(x * 100.0) / 100.0

        ideal = _floor2(min(px, max(ma20, px - 0.5 * atr)))
        zone_lo = max(0.01, _floor2(ideal - 0.5 * atr))
        zone_hi = _floor2(min(px, ideal + 0.5 * atr))
        if zone_hi < zone_lo:
            zone_lo, zone_hi = zone_hi, zone_lo

        # Structural stop: below the nearest support, floored at 2 ATRs under
        # spot. The 1.3x widening compensates for the close-to-close ATR proxy
        # understating true range, so the stop is not artificially tight.
        # Floored at 5% because STB is a 6-12 week hold: a technical level
        # inside 5% is noise on that horizon and would hand the position to the
        # market makers. The position stop (8-12%) still governs the trade.
        sup = _nearest_support(c, 60)
        atr_stop = px - 2.0 * atr * 1.3
        stop = min(sup * 0.985, atr_stop) if sup else atr_stop
        stop = max(0.01, min(stop, px * 0.95))

        size_factor = {"BUY_NOW": 1.0, "ACCUMULATE": 0.6,
                       "WAIT": 0.3, "AVOID": 0.0}.get(grade, 0.5)

        return {
            "ideal_entry": ideal,
            "entry_zone_low": zone_lo,
            "entry_zone_high": zone_hi,
            "structural_stop": round(stop, 2),
            "stop_pct": round((px - stop) / px * 100.0, 1),
            "size_factor": size_factor,
            "note": {
                "BUY_NOW": "Full size. Entry zone is where risk/reward is best.",
                "ACCUMULATE": "Scale in — roughly 60% now, add on a pullback to the zone.",
                "WAIT": "Work a limit order in the zone. Do not chase at the market.",
                "AVOID": "No entry today. Revisit when the technicals repair.",
            }.get(grade, "Scale in."),
        }
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────
def _empty(reason: str) -> dict:
    """The 'no opinion' result: neutral tilts, so thin or suspect data can
    never punish or promote a pick."""
    return {
        "ok": False,
        "reason": reason,
        "timing_score": None,
        "grade": "UNRATED",
        "raw_grade": "UNRATED",
        "grade_caps": [],
        "signals": [],
        "bull_count": 0,
        "bear_count": 0,
        "coverage": 0.0,
        "extension_atr": None,
        "near_term_risk_pct": None,
        "entry_plan": None,
        "score_tilt": 0.0,
        "conf_tilt": 0,
        "event_risk": {"level": "LOW", "points": 0, "conf_penalty": 0, "items": []},
        "summary": "No short-term timing read — price history insufficient or unverified.",
    }


def compute_timing(closes, volumes=None, benchmark_closes=None, sector=None,
                   news=None, earnings_days=None, expected_price=None,
                   now=None) -> dict:
    """Score short-term entry quality, build an entry plan, and flag event risk.

    Performs NO network I/O. Never raises.

    Args:
        closes:            Array-like of daily closes, oldest first.
        volumes:           Optional daily volumes aligned to `closes`.
        benchmark_closes:  Optional benchmark (SPY) closes for relative strength.
        sector:            Sector string, used for seasonal/weather exposure.
        news:              The STB news overlay dict for this symbol.
        earnings_days:     Days until the next earnings report, if known.
        expected_price:    The engine's independently validated spot price. When
                           supplied, a series that disagrees by more than 5% is
                           rejected as corrupt and the symbol returns UNRATED.
        now:               Date override for deterministic testing.

    Returns:
        A JSON-safe dict. On any failure it returns the neutral UNRATED result
        with zero tilts, leaving the caller's ranking untouched.
    """
    try:
        ev = assess_event_risk(sector, news, earnings_days, now)

        # ── Gate 1: price integrity. Refuse to rate a suspect series. ────────
        integrity = check_price_integrity(closes, expected_price)
        if not integrity["ok"]:
            res = _empty(integrity["reason"])
            res["event_risk"] = ev
            res["integrity"] = integrity
            res["conf_tilt"] = _clamp(int(ev.get("conf_penalty", 0)),
                                      _CONF_TILT_MIN, _CONF_TILT_MAX)
            if integrity["reason"] == "price_mismatch":
                res["summary"] = (
                    "Price history does not match the validated quote — timing "
                    "withheld to avoid scoring a corrupted series."
                )
            return res

        c = _clean(closes)
        v = _clean_nonneg(volumes) if volumes is not None else None
        if v is not None and v.size < 21:
            v = None
        b = _clean(benchmark_closes) if benchmark_closes is not None else None

        computed = {
            "trend_stack": _sig_trend_stack(c),
            "extension": _sig_extension(c),
            "pullback": _sig_pullback(c),
            "near_52w_high": _sig_near_52w_high(c),
            "trend_quality": _sig_trend_quality(c),
            "rel_strength": _sig_rel_strength(c, b),
            "macd_hist": _sig_macd(c),
            "bollinger_pos": _sig_bollinger(c),
            "support_distance": _sig_support_distance(c),
            "vol_regime": _sig_vol_regime(c),
            "drawdown_risk": _sig_drawdown(c),
            "vol_skew": _sig_vol_skew(c),
            "volume_trend": _sig_volume(c, v),
            "gap_risk": _sig_gap_risk(c),
        }

        num = den = 0.0
        signals = []
        bull = bear = 0
        for name, w in SIGNAL_WEIGHTS.items():
            s = computed.get(name)
            if not s:
                continue
            p = _finite(s.get("p"))
            if p is None:
                continue
            p = _clamp(p, 0.0, 1.0)
            num += w * p
            den += w
            if s["state"] == "BULL":
                bull += 1
            elif s["state"] == "BEAR":
                bear += 1
            signals.append({
                "name": name,
                "label": name.replace("_", " ").title(),
                "state": s["state"],
                "score": round(p * 100.0),
                "weight": w,
                "detail": s.get("detail", ""),
                "value": s.get("value"),
            })

        # ── Gate 2: signal coverage. Too few signals = no publishable grade. ─
        total_w = sum(SIGNAL_WEIGHTS.values())
        coverage = (den / total_w) if total_w > 0 else 0.0
        if den <= 0 or coverage < _MIN_WEIGHT_COVERAGE:
            res = _empty("insufficient_signal_coverage")
            res["event_risk"] = ev
            res["integrity"] = integrity
            res["coverage"] = round(coverage, 2)
            res["conf_tilt"] = _clamp(int(ev.get("conf_penalty", 0)),
                                      _CONF_TILT_MIN, _CONF_TILT_MAX)
            return res

        score = _clamp((num / den) * 100.0, 0.0, 100.0)
        if score >= 70:
            grade = "BUY_NOW"
        elif score >= 55:
            grade = "ACCUMULATE"
        elif score >= 40:
            grade = "WAIT"
        else:
            grade = "AVOID"

        # A weighted average can hide a fatal flaw: a dozen bullish readings all
        # describing the same melt-up will drag a dangerous setup back to
        # "respectable". Caps only ever lower the grade.
        raw_grade = grade
        grade, cap_reasons = _apply_grade_caps(grade, computed)

        # Expected near-term adverse excursion: one 20-day ATR as a percent of
        # price. This is the move to expect against you in normal conditions,
        # and it is what any stop must be able to absorb.
        atr = _atr_proxy(c, 20)
        px = float(c[-1])
        near_term_risk = (atr / px * 100.0) if (atr and px > 0) else None

        score_tilt = _clamp((score - 55.0) / 45.0 * _SCORE_TILT_CAP,
                            -_SCORE_TILT_CAP, _SCORE_TILT_CAP)
        # When a cap demotes the grade, demote the tilt with it, otherwise a
        # capped pick would still be promoted up the ranking by its raw score.
        band_max = _BAND_MAX_TILT.get(grade)
        if band_max is not None and score_tilt > band_max:
            score_tilt = band_max
        conf_tilt = _clamp(
            {"BUY_NOW": 3, "ACCUMULATE": 1, "WAIT": -2, "AVOID": -4}[grade]
            + int(ev.get("conf_penalty", 0)),
            _CONF_TILT_MIN, _CONF_TILT_MAX,
        )

        # Worst signals first: the user needs to see what is wrong, not what
        # is fine.
        signals.sort(key=lambda s: (s["score"], -s["weight"]))

        summary = {
            "BUY_NOW": "Technically clean entry — trend, extension and momentum align.",
            "ACCUMULATE": "Acceptable entry — scale in rather than taking full size.",
            "WAIT": "Fundamentally sound but technically poor entry — wait for a better price.",
            "AVOID": "Short-term technicals are hostile — high risk of an immediate drawdown.",
        }[grade]
        if cap_reasons:
            summary += " " + cap_reasons[0] + "."
        if ev["level"] in ("ELEVATED", "HIGH"):
            summary += f" Event risk {ev['level'].lower()}."

        return {
            "ok": True,
            "reason": "",
            "timing_score": round(score, 1),
            "grade": grade,
            "raw_grade": raw_grade,
            "grade_caps": cap_reasons,
            "signals": signals,
            "bull_count": bull,
            "bear_count": bear,
            "coverage": round(coverage, 2),
            "extension_atr": (computed["extension"] or {}).get("value"),
            "near_term_risk_pct": _r(near_term_risk, 1),
            "entry_plan": _build_entry_plan(c, grade),
            "score_tilt": round(score_tilt, 1),
            "conf_tilt": int(conf_tilt),
            "event_risk": ev,
            "integrity": integrity,
            "summary": summary,
        }
    except Exception as e:  # pragma: no cover — contract is "never raises"
        return _empty(f"error:{type(e).__name__}")
