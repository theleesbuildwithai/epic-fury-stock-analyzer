"""
Cross-Asset Signal Engine — adds the macro context the system has
been missing. Tracks 5 key cross-asset benchmarks and synthesizes
them into a daily risk-on / risk-off read:

  - 10Y Treasury yield (proxy: TLT — inverse of yield)
  - Dollar Index (proxy: UUP)
  - VIX (^VIX)
  - Gold (GLD)
  - Bitcoin (BTC-USD)

HISTORICAL SIGNALS:
  - 10Y yield rising sharply  -> high-multiple tech crushed
  - DXY rallying              -> international stocks weak
  - VIX > 25                  -> defensive sectors win, growth gets hit
  - Gold rallying with VIX    -> haven bid, risk-off
  - BTC rolling over          -> risk-off coming (crypto leads tech)

OUTPUT:
  get_current_signals() returns a dict with:
    - regime_label: "risk_on" / "neutral" / "risk_off"
    - signals: per-asset state + recent move
    - sector_tilt: recommended tilt (e.g. "rates_up_avoid_tech")

SAFETY:
  - All yfinance calls wrapped, soft-fail to {}
  - 30-min in-memory cache to avoid hammering yfinance
  - Returns ok=False on any failure, never raises
"""

import logging
import time
import threading
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Asset universe — proxies that work on free yfinance tier
CROSS_ASSETS = {
    "TLT":     {"name": "20Y Treasury (TLT)", "category": "rates"},
    "UUP":     {"name": "US Dollar Index (UUP)", "category": "dollar"},
    "^VIX":    {"name": "VIX", "category": "vol"},
    "GLD":     {"name": "Gold (GLD)", "category": "gold"},
    "BTC-USD": {"name": "Bitcoin", "category": "crypto"},
}

_cache = {"data": None, "ts": 0}
_CACHE_TTL = 1800   # 30 minutes
_lock = threading.Lock()


def _safe_recent_data(ticker: str, days: int = 30) -> dict:
    """Pull the last `days` of close prices. Returns {"closes": [...],
    "latest": float, "prev": float, "pct_5d": float, "pct_20d": float} or {}.
    Never raises."""
    try:
        import yfinance as yf
        import threading as _ca_thr
        end = datetime.utcnow()
        start = end - timedelta(days=int(days) + 5)  # buffer for holidays
        _ca_r = [None]
        _ca_t = _ca_thr.Thread(
            target=lambda r=_ca_r, t=ticker, s=start, e=end: r.__setitem__(
                0, yf.download(t, start=s.strftime("%Y-%m-%d"),
                               end=e.strftime("%Y-%m-%d"),
                               progress=False, auto_adjust=True, threads=False)),
            daemon=True)
        _ca_t.start(); _ca_t.join(timeout=10)
        df = _ca_r[0]
        if df is None or df.empty:
            return {}
        closes = df["Close"].dropna().values.tolist()
        if isinstance(closes[0], list):  # multi-column edge case
            closes = [float(c[0]) for c in closes]
        else:
            closes = [float(c) for c in closes]
        if len(closes) < 5:
            return {}
        latest = float(closes[-1])
        prev = float(closes[-2])
        pct_1d = (latest / prev - 1) * 100 if prev > 0 else 0
        pct_5d = ((latest / closes[-6] - 1) * 100
                  if len(closes) >= 6 and closes[-6] > 0 else 0)
        pct_20d = ((latest / closes[0] - 1) * 100
                   if len(closes) >= 1 and closes[0] > 0 else 0)
        return {
            "latest": round(latest, 4),
            "prev": round(prev, 4),
            "pct_1d": round(pct_1d, 2),
            "pct_5d": round(pct_5d, 2),
            "pct_20d": round(pct_20d, 2),
            "samples": len(closes),
        }
    except Exception as e:
        logger.debug(f"_safe_recent_data {ticker} soft-fail: {e}")
    # Fallback: multi-source historical adapter
    try:
        from analytics.multi_source_adapter import get_historical_any_source
        _period = f"{int(days)}d" if days <= 365 else "1y"
        df2 = get_historical_any_source(ticker, _period)
        if df2 is not None and len(df2) >= 5:
            closes2 = df2["Close"].dropna().values.tolist()
            closes2 = [float(c) for c in closes2]
            if len(closes2) >= 2:
                latest = float(closes2[-1])
                prev = float(closes2[-2])
                pct_1d = (latest / prev - 1) * 100 if prev > 0 else 0
                pct_5d = ((latest / closes2[-6] - 1) * 100
                          if len(closes2) >= 6 and closes2[-6] > 0 else 0)
                pct_20d = ((latest / closes2[0] - 1) * 100
                           if closes2[0] > 0 else 0)
                return {
                    "latest": round(latest, 4),
                    "prev": round(prev, 4),
                    "pct_1d": round(pct_1d, 2),
                    "pct_5d": round(pct_5d, 2),
                    "pct_20d": round(pct_20d, 2),
                    "samples": len(closes2),
                }
    except Exception:
        pass
    return {}


def _classify_regime_from_signals(signals: dict) -> dict:
    """Pure synthesizer — takes the per-asset signals and produces
    a single regime label + sector tilt recommendation.

    Decision rules (conservative + interpretable):
      VIX > 25 OR VIX up >40% in 20d        -> stress
      TLT down >5% in 20d (yields up sharp)  -> rates_up
      UUP up >3% in 20d (dollar strong)      -> dxy_strong
      GLD up >5% AND VIX > 22                -> haven_bid
      BTC down >15% in 20d                   -> crypto_breakdown

      stress + rates_up + dxy_strong        -> risk_off
      no negative signals                    -> risk_on
      otherwise                              -> neutral
    """
    flags = []
    notes = []

    vix = signals.get("^VIX") or {}
    tlt = signals.get("TLT") or {}
    uup = signals.get("UUP") or {}
    gld = signals.get("GLD") or {}
    btc = signals.get("BTC-USD") or {}

    if vix.get("latest", 0) > 25:
        flags.append("vix_elevated")
        notes.append(f"VIX at {vix['latest']:.1f} (>25 = stress regime)")
    if vix.get("pct_20d", 0) > 40:
        flags.append("vix_spike")
        notes.append(f"VIX +{vix['pct_20d']:.0f}% in 20d (rapidly rising fear)")

    if tlt.get("pct_20d", 0) < -5:
        flags.append("rates_up")
        notes.append(f"TLT {tlt['pct_20d']:.1f}% in 20d (yields rising sharp — "
                     f"avoid high-multiple tech)")
    elif tlt.get("pct_20d", 0) > 5:
        flags.append("rates_down")
        notes.append(f"TLT +{tlt['pct_20d']:.1f}% in 20d (yields falling — "
                     f"growth tailwind)")

    if uup.get("pct_20d", 0) > 3:
        flags.append("dxy_strong")
        notes.append(f"UUP +{uup['pct_20d']:.1f}% in 20d (strong dollar — "
                     f"international weak)")

    if gld.get("pct_20d", 0) > 5 and vix.get("latest", 0) > 22:
        flags.append("haven_bid")
        notes.append(f"Gold +{gld['pct_20d']:.1f}% with VIX {vix['latest']:.1f} "
                     f"(flight to safety)")

    if btc.get("pct_20d", 0) < -15:
        flags.append("crypto_breakdown")
        notes.append(f"BTC {btc['pct_20d']:.1f}% in 20d (crypto leads tech "
                     f"down — risk-off coming)")

    # Synthesize
    risk_off_flags = {"vix_elevated", "vix_spike", "rates_up", "haven_bid",
                      "crypto_breakdown", "dxy_strong"}
    n_riskoff = sum(1 for f in flags if f in risk_off_flags)
    if n_riskoff >= 3:
        regime = "risk_off"
    elif n_riskoff == 0:
        regime = "risk_on"
    else:
        regime = "neutral"

    # Sector tilt recommendation
    tilt = "balanced"
    if "rates_up" in flags:
        tilt = "avoid_tech_long_duration_assets"
    elif "rates_down" in flags and "vix_elevated" not in flags:
        tilt = "favor_growth_tech"
    elif "vix_elevated" in flags or "vix_spike" in flags:
        tilt = "favor_defensive_utilities_staples"
    elif "dxy_strong" in flags:
        tilt = "avoid_international_emerging_markets"

    return {
        "regime": regime,
        "flags": flags,
        "notes": notes,
        "sector_tilt": tilt,
        "n_riskoff_signals": n_riskoff,
    }


def get_current_signals(force_refresh: bool = False) -> dict:
    """Returns the current cross-asset snapshot. 30-minute cache.
    Soft-fails per-asset, never raises."""
    try:
        with _lock:
            if (not force_refresh and _cache.get("data")
                    and (time.time() - _cache.get("ts", 0)) < _CACHE_TTL):
                cached = dict(_cache["data"])
                cached["_cache_age_seconds"] = int(time.time() - _cache["ts"])
                cached["_cached"] = True
                return cached

        signals = {}
        succeeded = 0
        for ticker, meta in CROSS_ASSETS.items():
            data = _safe_recent_data(ticker, days=30)
            if data:
                signals[ticker] = {**meta, **data}
                succeeded += 1

        if not signals:
            return {"ok": False, "reason": "no_cross_asset_data"}

        synthesis = _classify_regime_from_signals(signals)
        result = {
            "ok": True,
            "computed_at": datetime.utcnow().isoformat(),
            "_cached": False,
            "succeeded_assets": succeeded,
            "total_assets": len(CROSS_ASSETS),
            "signals": signals,
            "synthesis": synthesis,
        }
        with _lock:
            _cache["data"] = result
            _cache["ts"] = time.time()
        return result
    except Exception as e:
        logger.warning(f"get_current_signals fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}


def get_sector_recommendation() -> dict:
    """Higher-level: just return what to favor / avoid based on
    current cross-asset state. Never raises."""
    try:
        snap = get_current_signals()
        if not snap.get("ok"):
            return {"ok": False, "reason": snap.get("reason")}
        synthesis = snap.get("synthesis", {})
        regime = synthesis.get("regime")
        tilt = synthesis.get("sector_tilt")
        return {
            "ok": True,
            "regime": regime,
            "sector_tilt": tilt,
            "rationale": "; ".join(synthesis.get("notes", [])) or "no notable signals",
            "computed_at": snap.get("computed_at"),
        }
    except Exception as e:
        logger.debug(f"get_sector_recommendation soft-fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}
