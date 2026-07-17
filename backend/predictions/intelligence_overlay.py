"""
Level 6: Intelligence Overlay — composite scorer that wires the
Level 1-5 signals into pick generation + position sizing.

DESIGN GOALS:
  1. Pick generation should consume per-ticker confidence multipliers
     from ALL learning layers (postmortem, regime playbook, earnings
     drift, cross-asset, stock learning, regime drift).
  2. Position sizing should consume risk-reduction factors (event
     calendar, correlation matrix).
  3. NEVER block a trade. NEVER zero a size. NEVER pause cycles.
  4. Soft-fail to NEUTRAL (1.0) on any module error so a bug in any
     learning layer cannot break trading.

HARD SAFETY CONTRACT:
  - Confidence multiplier ALWAYS bounded to [0.5, 1.5]
  - Size factor ALWAYS bounded to [0.5, 1.0]  (only reduces, never zeroes)
  - Single env-var KILL SWITCH: DISABLE_INTELLIGENCE_OVERLAY=1 disables
    everything (returns neutral 1.0 for both)
  - Every signal extraction wrapped in its own try/except so one
    broken signal cannot poison the others
  - Every public function returns a default that's safe to multiply
    even on total failure

PUBLIC API:
  compute_pick_overlay(ticker, sector, regime, signal_score, direction)
    -> {"multiplier": float in [0.5, 1.5], "components": {...}}
  compute_size_factor()
    -> {"factor": float in [0.5, 1.0], "components": {...}}
  is_overlay_enabled() -> bool
  get_overlay_status() -> dict (for monitoring/debug)
"""

import logging
import os
import time
import threading

logger = logging.getLogger(__name__)

# Hard kill switch — env var or trading_state row
KILL_SWITCH_ENV = "DISABLE_INTELLIGENCE_OVERLAY"

# Per-component weight (additive contributions, all clamped to ±limits)
# These are intentionally MODEST so no single signal can dominate.
COMPONENT_LIMITS = {
    "stock_learning":   0.15,  # ±15% from per-ticker hit rate
    "regime_playbook":  0.10,  # ±10% from "what worked in this regime"
    "loss_postmortem": -0.20,  # ONLY penalty (never boost)
    "cross_asset":      0.10,  # ±10% from macro tilt
    "earnings_drift":   0.10,  # ±10% from PEAD pattern
    "regime_drift":    -0.10,  # ONLY penalty when alarm active
}

# Multiplier bounds (final result is bounded to these)
MULTIPLIER_MIN = 0.50
MULTIPLIER_MAX = 1.50

# Size factor bounds (only reduces — never above 1.0, never below 0.5)
SIZE_FACTOR_MIN = 0.50
SIZE_FACTOR_MAX = 1.00

# Sector sector ETF -> internal sector name mapping for regime-playbook lookup
_SECTOR_TO_ETF = {
    "Technology": "XLK", "Financial Services": "XLF", "Energy": "XLE",
    "Healthcare": "XLV", "Consumer Defensive": "XLP",
    "Consumer Cyclical": "XLY", "Industrials": "XLI",
    "Utilities": "XLU", "Materials": "XLB", "Real Estate": "XLRE",
    "Communication Services": "XLC", "Communications": "XLC",
    "Communication Services": "XLC",
}


def is_overlay_enabled() -> bool:
    """Check kill switch. Returns True only if overlay should run."""
    try:
        val = os.environ.get(KILL_SWITCH_ENV, "0").strip().lower()
        if val in ("1", "true", "yes", "on"):
            return False
        return True
    except Exception:
        return True  # default ON (fail-open)


# -------- Per-signal extractors (each returns delta in [-limit, +limit]) --------

def _delta_stock_learning(ticker: str) -> float:
    """Convert per-ticker confidence_adj (0.5-1.3) into delta around 1.0."""
    try:
        from predictions.stock_learning import get_stock_stats
        stats = get_stock_stats(ticker)
        if not stats.get("ok"):
            return 0.0
        adj = float(stats.get("confidence_adj") or 1.0)
        # Map [0.5, 1.3] -> [-0.5, +0.3]; clamp to component limit
        delta = adj - 1.0
        limit = COMPONENT_LIMITS["stock_learning"]
        return max(-limit, min(limit, delta))
    except Exception:
        return 0.0


def _delta_regime_playbook(sector: str) -> float:
    """+10% if sector is top-3 in current matched playbook, -10% if bottom-3.

    Uses 10-min module-level cache so 50 picks in one cycle trigger
    AT MOST ONE call to get_current_match (which does heavy yfinance).
    """
    try:
        sector_etf = _SECTOR_TO_ETF.get(sector)
        if not sector_etf:
            return 0.0
        match = _cached_regime_match()
        if not match or not match.get("ok"):
            return 0.0
        recs = match.get("recommendations", {}) or {}
        buys = {s.get("ticker") for s in recs.get("buy_sectors", []) or []}
        avoids = {s.get("ticker") for s in recs.get("avoid_sectors", []) or []}
        limit = COMPONENT_LIMITS["regime_playbook"]
        if sector_etf in buys:
            return limit
        if sector_etf in avoids:
            return -limit
        return 0.0
    except Exception:
        return 0.0


# 10-min module-level cache for the expensive get_current_match call.
# Pick generation runs ~50 picks per cycle — without this cache, each
# pick triggers a fresh yfinance fetch and we'd block for 5+ minutes.
_REGIME_MATCH_CACHE_TTL = 600   # 10 min
_regime_match_cache = {"data": None, "ts": 0}
_regime_match_lock = threading.Lock()


def _cached_regime_match() -> dict:
    """10-min cached wrapper around regime_playbook.get_current_match.
    NEVER raises — returns None on any error.

    CRITICAL BUG FIX v2 (post-timeout diagnosis): on cache miss, this
    is called FROM INSIDE pick generation (50 picks per cycle). The
    underlying get_current_match() does a 300d SPY yfinance download
    that can take 30-60s under rate-limiting. With 50 picks running
    sequentially and the FIRST one triggering the heavy fetch,
    generate_quant_picks would exceed App Runner's 120s edge timeout.

    NEW BEHAVIOR: cache miss returns None IMMEDIATELY. Pick generation
    NEVER blocks on yfinance for the overlay. The cache is warmed
    lazily in a background thread (spawned once per cache miss) so
    NEXT cycle has fresh data.

    Also bug fix v1 still in place: only cache successful responses
    so a failed fetch doesn't poison the cache."""
    try:
        with _regime_match_lock:
            now = time.time()
            cached = _regime_match_cache.get("data")
            ts = _regime_match_cache.get("ts", 0)
            warming = _regime_match_cache.get("warming", False)
            # Only return cached if it's a SUCCESSFUL response in TTL
            if (cached and isinstance(cached, dict) and cached.get("ok")
                    and (now - ts) < _REGIME_MATCH_CACHE_TTL):
                return cached
            # Cache miss: spawn ONE background warmer if not already warming
            if not warming:
                _regime_match_cache["warming"] = True
                try:
                    threading.Thread(target=_warm_regime_match_cache,
                                      daemon=True,
                                      name="overlay-warm-regime").start()
                except Exception:
                    _regime_match_cache["warming"] = False
        # NEVER block the pick-gen hot path — return None immediately
        return None
    except Exception:
        return None


def _warm_regime_match_cache():
    """Background warmer — fetches get_current_match() and stores in
    cache. Called once per cache miss. NEVER raises."""
    try:
        from predictions.regime_playbook import get_current_match
        fresh = get_current_match()
        if fresh and isinstance(fresh, dict) and fresh.get("ok"):
            with _regime_match_lock:
                _regime_match_cache["data"] = fresh
                _regime_match_cache["ts"] = time.time()
                _regime_match_cache["warming"] = False
        else:
            with _regime_match_lock:
                _regime_match_cache["warming"] = False
    except Exception:
        try:
            with _regime_match_lock:
                _regime_match_cache["warming"] = False
        except Exception:
            pass


def _delta_loss_postmortem(ticker: str, sector: str, regime: str,
                            signal_score: float) -> float:
    """If this profile matches an active loss filter, return penalty."""
    try:
        from predictions.loss_postmortem import check_against_loss_filters
        result = check_against_loss_filters(ticker, regime, sector, signal_score)
        if result.get("reject"):
            # Return the (negative) penalty — note loss_postmortem uses
            # "reject" semantics; we convert that to a confidence penalty
            return COMPONENT_LIMITS["loss_postmortem"]   # negative
        return 0.0
    except Exception:
        return 0.0


# Module-level cache for cross_asset (also yfinance-heavy)
_cross_asset_cache = {"data": None, "ts": 0, "warming": False}
_cross_asset_lock = threading.Lock()
_CROSS_ASSET_CACHE_TTL = 1800  # 30 min


def _warm_cross_asset_cache():
    """Background warmer for cross_asset. NEVER raises."""
    try:
        from predictions.cross_asset import get_sector_recommendation
        fresh = get_sector_recommendation()
        if fresh and isinstance(fresh, dict) and fresh.get("ok"):
            with _cross_asset_lock:
                _cross_asset_cache["data"] = fresh
                _cross_asset_cache["ts"] = time.time()
                _cross_asset_cache["warming"] = False
        else:
            with _cross_asset_lock:
                _cross_asset_cache["warming"] = False
    except Exception:
        try:
            with _cross_asset_lock:
                _cross_asset_cache["warming"] = False
        except Exception:
            pass


def _cached_cross_asset() -> dict:
    """Cache-only cross_asset wrapper — never blocks pick gen."""
    try:
        with _cross_asset_lock:
            now = time.time()
            cached = _cross_asset_cache.get("data")
            ts = _cross_asset_cache.get("ts", 0)
            warming = _cross_asset_cache.get("warming", False)
            if (cached and isinstance(cached, dict) and cached.get("ok")
                    and (now - ts) < _CROSS_ASSET_CACHE_TTL):
                return cached
            if not warming:
                _cross_asset_cache["warming"] = True
                try:
                    threading.Thread(target=_warm_cross_asset_cache,
                                      daemon=True,
                                      name="overlay-warm-crossasset").start()
                except Exception:
                    _cross_asset_cache["warming"] = False
        return None
    except Exception:
        return None


def _delta_cross_asset(sector: str, direction: str) -> float:
    """Map cross-asset tilt to a sector-aware delta.
    Uses cache-only — never blocks pick generation on yfinance."""
    try:
        rec = _cached_cross_asset()
        if not rec or not rec.get("ok"):
            return 0.0
        tilt = rec.get("sector_tilt", "balanced")
        regime = rec.get("regime")
        limit = COMPONENT_LIMITS["cross_asset"]

        # Risk-off → defensives win; risk-on → growth wins
        defensive_sectors = {"Utilities", "Consumer Defensive", "Healthcare"}
        growth_sectors = {"Technology", "Communication Services", "Communications",
                          "Communication Services", "Consumer Cyclical"}

        if tilt == "avoid_tech_long_duration_assets" and sector in growth_sectors:
            return -limit if direction == "long" else +limit/2
        if tilt == "favor_growth_tech" and sector in growth_sectors:
            return +limit if direction == "long" else -limit/2
        if tilt == "favor_defensive_utilities_staples":
            if sector in defensive_sectors and direction == "long":
                return +limit
            if sector in growth_sectors and direction == "long":
                return -limit/2
        if regime == "risk_off" and direction == "long" and sector in growth_sectors:
            return -limit/2
        return 0.0
    except Exception:
        return 0.0


def _delta_earnings_drift(ticker: str, direction: str) -> float:
    """PEAD pattern: positive drift -> boost long, penalize short.

    CRITICAL: uses cache_only=True so a cache miss returns NEUTRAL
    (1.0) immediately instead of triggering a 5-10s yfinance fetch.
    Pick generation must NEVER block on yfinance — earnings-drift
    cache is warmed by separate /api/earnings-drift/{ticker} calls."""
    try:
        from predictions.earnings_drift import predict_drift
        pred = predict_drift(ticker, cache_only=True)
        if not pred.get("ok"):
            return 0.0
        signal = float(pred.get("confidence_signal") or 1.0)
        # signal in [0.9, 1.1] -> delta in [-0.1, +0.1]
        delta = signal - 1.0
        if direction == "short":
            delta = -delta  # positive drift bad for shorts
        limit = COMPONENT_LIMITS["earnings_drift"]
        return max(-limit, min(limit, delta))
    except Exception:
        return 0.0


def _delta_regime_drift() -> float:
    """If recent high-severity regime drift alarm, apply penalty."""
    try:
        from predictions.regime_drift import get_recent_alarms
        alarms = get_recent_alarms(hours=24)
        if not alarms.get("ok"):
            return 0.0
        recent = alarms.get("alarms", []) or []
        # Penalty if any high-severity alarm in last 24h
        for a in recent:
            if a.get("severity") == "high":
                return COMPONENT_LIMITS["regime_drift"]   # negative
        if recent:
            # Medium alarm = half penalty
            return COMPONENT_LIMITS["regime_drift"] / 2
        return 0.0
    except Exception:
        return 0.0


# -------- Public API --------

def compute_pick_overlay(ticker: str, sector: str, regime: str,
                          signal_score: float, direction: str = "long") -> dict:
    """Returns a confidence multiplier + per-component breakdown for one pick.

    Bounded to [0.5, 1.5]. Soft-fails to neutral 1.0 on any error.
    Each signal contributes independently — one broken signal does
    not affect the others.
    """
    try:
        if not is_overlay_enabled():
            return {"multiplier": 1.0, "components": {},
                    "disabled": True, "reason": "kill_switch"}

        components = {}
        try:
            components["stock_learning"] = _delta_stock_learning(ticker)
        except Exception:
            components["stock_learning"] = 0.0
        try:
            components["regime_playbook"] = _delta_regime_playbook(sector)
        except Exception:
            components["regime_playbook"] = 0.0
        try:
            components["loss_postmortem"] = _delta_loss_postmortem(
                ticker, sector, regime, signal_score)
        except Exception:
            components["loss_postmortem"] = 0.0
        try:
            components["cross_asset"] = _delta_cross_asset(sector, direction)
        except Exception:
            components["cross_asset"] = 0.0
        try:
            components["earnings_drift"] = _delta_earnings_drift(ticker, direction)
        except Exception:
            components["earnings_drift"] = 0.0
        try:
            components["regime_drift"] = _delta_regime_drift()
        except Exception:
            components["regime_drift"] = 0.0

        total_delta = sum(components.values())
        multiplier = 1.0 + total_delta
        # HARD bounds — never zero out, never blow up
        multiplier = max(MULTIPLIER_MIN, min(MULTIPLIER_MAX, multiplier))

        return {
            "multiplier": round(multiplier, 3),
            "total_delta": round(total_delta, 3),
            "components": {k: round(v, 3) for k, v in components.items()},
            "disabled": False,
        }
    except Exception as e:
        logger.debug(f"compute_pick_overlay {ticker} soft-fail: {e}")
        return {"multiplier": 1.0, "components": {},
                "error": str(e)[:200], "fallback": "neutral"}


def compute_size_factor() -> dict:
    """Returns a position-size multiplier in [0.5, 1.0].
    Combines event_calendar risk-adjustment + correlation warnings.
    NEVER zeroes positions. Soft-fails to 1.0 on any error.
    """
    try:
        if not is_overlay_enabled():
            return {"factor": 1.0, "components": {},
                    "disabled": True, "reason": "kill_switch"}

        components = {}

        # Event calendar (FOMC/CPI/NFP awareness)
        try:
            from predictions.event_calendar import get_risk_adjustment_factor
            ev = get_risk_adjustment_factor()
            ev_factor = float(ev.get("factor", 1.0)) if ev.get("ok") else 1.0
            components["event_calendar"] = round(ev_factor, 3)
        except Exception:
            components["event_calendar"] = 1.0

        # Correlation: if max sector weight > 50%, multiply by 0.85
        try:
            from predictions.correlation_matrix import get_concentration_summary
            corr = get_concentration_summary()
            corr_factor = 1.0
            if corr.get("ok"):
                max_sector = corr.get("max_sector", {}) or {}
                if float(max_sector.get("weight", 0) or 0) > 0.50:
                    corr_factor = 0.85
            components["correlation"] = corr_factor
        except Exception:
            components["correlation"] = 1.0

        # Compose multiplicatively (each component is already in [0.5, 1.0])
        factor = 1.0
        for v in components.values():
            factor *= max(SIZE_FACTOR_MIN, min(SIZE_FACTOR_MAX, float(v)))
        # Final bound — never zero
        factor = max(SIZE_FACTOR_MIN, min(SIZE_FACTOR_MAX, factor))

        return {
            "factor": round(factor, 3),
            "components": components,
            "disabled": False,
        }
    except Exception as e:
        logger.debug(f"compute_size_factor soft-fail: {e}")
        return {"factor": 1.0, "components": {},
                "error": str(e)[:200], "fallback": "neutral"}


def get_overlay_status() -> dict:
    """Health snapshot for the admin endpoint."""
    try:
        return {
            "enabled": is_overlay_enabled(),
            "kill_switch_env": KILL_SWITCH_ENV,
            "kill_switch_value": os.environ.get(KILL_SWITCH_ENV, "0"),
            "component_limits": COMPONENT_LIMITS,
            "multiplier_bounds": [MULTIPLIER_MIN, MULTIPLIER_MAX],
            "size_factor_bounds": [SIZE_FACTOR_MIN, SIZE_FACTOR_MAX],
        }
    except Exception as e:
        return {"enabled": True, "error": str(e)[:200]}
