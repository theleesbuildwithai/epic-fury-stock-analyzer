"""
Regime Engine — HMM, volatility classifier, trend classifier, breadth.

Detects market regimes and conditions trading on them. Currently the
system reacts to regime; this anticipates regime transitions.

Output: regime label + transition probabilities + regime-conditioned
multipliers for exposure, confidence, and stop sizing.
"""
import math
from typing import Optional
from .nan_helpers import safe_float, safe_div, clamp


def classify_volatility_regime(vix_level: float, vix_history: list = None) -> dict:
    """
    Volatility regime: LOW / NORMAL / ELEVATED / CRISIS.

    Buckets:
        LOW:      VIX < 15  (complacency, mean-reversion edge)
        NORMAL:   VIX 15-22 (typical momentum environment)
        ELEVATED: VIX 22-30 (defensive, smaller positions)
        CRISIS:   VIX > 30  (only A+ picks, tight stops)
    """
    v = safe_float(vix_level, 20.0)
    if v < 15:
        return {"regime": "LOW", "vix": v, "exposure_mult": 1.0,
                "confidence_shift": -2, "stop_mult": 1.0}
    elif v < 22:
        return {"regime": "NORMAL", "vix": v, "exposure_mult": 1.0,
                "confidence_shift": 0, "stop_mult": 1.0}
    elif v < 30:
        return {"regime": "ELEVATED", "vix": v, "exposure_mult": 0.75,
                "confidence_shift": +3, "stop_mult": 1.2}
    else:
        return {"regime": "CRISIS", "vix": v, "exposure_mult": 0.5,
                "confidence_shift": +8, "stop_mult": 1.5}


def classify_trend_regime(prices: list) -> dict:
    """
    Trend regime via 20/50/200-day EMA stack.

    BULL:     price > EMA20 > EMA50 > EMA200
    BEAR:     price < EMA20 < EMA50 < EMA200
    SIDEWAYS: mixed
    """
    if not prices or len(prices) < 200:
        return {"regime": "UNKNOWN", "confidence": 0}
    ema20 = exponential_moving_average(prices, 20)
    ema50 = exponential_moving_average(prices, 50)
    ema200 = exponential_moving_average(prices, 200)
    if not all([ema20, ema50, ema200]):
        return {"regime": "UNKNOWN", "confidence": 0}
    last = prices[-1]
    if last > ema20 > ema50 > ema200:
        return {"regime": "BULL", "confidence": 90}
    if last < ema20 < ema50 < ema200:
        return {"regime": "BEAR", "confidence": 90}
    # Partial alignment
    if last > ema50 and ema50 > ema200:
        return {"regime": "BULL", "confidence": 55}
    if last < ema50 and ema50 < ema200:
        return {"regime": "BEAR", "confidence": 55}
    return {"regime": "SIDEWAYS", "confidence": 40}


def exponential_moving_average(values: list, period: int) -> Optional[float]:
    """EMA of last `period` values. Returns last EMA value."""
    if not values or len(values) < period:
        return None
    alpha = 2 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = alpha * v + (1 - alpha) * ema
    return ema


def classify_breadth_regime(advance_decline_ratio: float = None,
                             new_highs: int = None, new_lows: int = None) -> dict:
    """
    Breadth regime: % of stocks confirming the move.

    HEALTHY:   AD ratio > 1.5 or high/low > 5
    NEUTRAL:   AD 0.7-1.5
    DETERIORATING: AD < 0.7 or low/high > 3
    """
    if advance_decline_ratio is None:
        if new_highs and new_lows and new_lows > 0:
            advance_decline_ratio = new_highs / new_lows
        else:
            return {"regime": "UNKNOWN"}
    ad = safe_float(advance_decline_ratio, 1.0)
    if ad > 1.5:
        return {"regime": "HEALTHY", "ad_ratio": ad}
    elif ad < 0.7:
        return {"regime": "DETERIORATING", "ad_ratio": ad}
    return {"regime": "NEUTRAL", "ad_ratio": ad}


# === Hidden Markov Model regime detector ===

def hmm_regime_3state(returns: list, vol_window: int = 20) -> dict:
    """
    Simple 3-state HMM (BULL / BEAR / SIDEWAYS) using:
      - Rolling 20d return (trend)
      - Rolling 20d vol (volatility regime)

    NOTE: this is a lightweight HMM-style classifier, not a full Baum-Welch
    fit. Production version would use hmmlearn (adds dep).

    Returns:
        {regime, transition_probs: {to_BULL, to_BEAR, to_SIDEWAYS}}
    """
    if not returns or len(returns) < vol_window * 2:
        return {"regime": "UNKNOWN", "transition_probs": {}}
    recent = returns[-vol_window:]
    mean_ret = sum(recent) / len(recent)
    var = sum((r - mean_ret) ** 2 for r in recent) / (len(recent) - 1)
    vol = math.sqrt(var)
    # Annualize for interpretability
    ann_ret = mean_ret * 252
    ann_vol = vol * math.sqrt(252)
    # Classify
    if ann_ret > 0.10 and ann_vol < 0.20:
        regime = "BULL"
    elif ann_ret < -0.10 and ann_vol > 0.25:
        regime = "BEAR"
    elif abs(ann_ret) < 0.05:
        regime = "SIDEWAYS"
    elif ann_ret > 0:
        regime = "BULL"
    else:
        regime = "BEAR"
    # Transition probabilities (heuristic from recent vol trend)
    # If vol is rising, P(BEAR) increases
    if len(returns) >= vol_window * 3:
        prev_vol = math.sqrt(
            sum((r - mean_ret) ** 2 for r in returns[-vol_window*2:-vol_window]) /
            (vol_window - 1)
        )
        vol_change = (vol - prev_vol) / prev_vol if prev_vol > 0 else 0
    else:
        vol_change = 0
    # Base probs from current regime + vol change adjustment
    if regime == "BULL":
        p_bear = clamp(0.15 + vol_change * 0.5, 0.05, 0.50)
        p_sideways = 0.25
        p_bull = 1 - p_bear - p_sideways
    elif regime == "BEAR":
        p_bull = clamp(0.10 - vol_change * 0.3, 0.05, 0.40)
        p_sideways = 0.30
        p_bear = 1 - p_bull - p_sideways
    else:  # SIDEWAYS
        p_bull = 0.40
        p_bear = 0.30
        p_sideways = 0.30
    return {
        "regime": regime,
        "annualized_return": round(ann_ret * 100, 2),
        "annualized_vol": round(ann_vol * 100, 2),
        "vol_trend_pct": round(vol_change * 100, 2),
        "transition_probs": {
            "BULL_5d": round(p_bull, 3),
            "BEAR_5d": round(p_bear, 3),
            "SIDEWAYS_5d": round(p_sideways, 3),
        },
    }


def combined_regime(market_returns: list, vix_level: float, prices: list = None,
                     ad_ratio: float = None) -> dict:
    """
    Ensemble regime detector — combines HMM, trend, vol, breadth.

    Returns:
        {regime, confidence, components}
    """
    hmm = hmm_regime_3state(market_returns)
    vol = classify_volatility_regime(vix_level)
    trend = classify_trend_regime(prices) if prices else {"regime": "UNKNOWN"}
    breadth = classify_breadth_regime(ad_ratio) if ad_ratio else {"regime": "UNKNOWN"}
    # Vote
    votes = {"BULL": 0, "BEAR": 0, "SIDEWAYS": 0}
    if hmm["regime"] in votes:
        votes[hmm["regime"]] += 3  # HMM gets weight 3
    if vol["regime"] == "CRISIS":
        votes["BEAR"] += 2
    elif vol["regime"] == "LOW":
        votes["BULL"] += 1
    if trend["regime"] in votes:
        votes[trend["regime"]] += 2
    if breadth["regime"] == "HEALTHY":
        votes["BULL"] += 1
    elif breadth["regime"] == "DETERIORATING":
        votes["BEAR"] += 2
    winner = max(votes, key=votes.get)
    total_votes = sum(votes.values()) or 1
    confidence = int(votes[winner] / total_votes * 100)
    return {
        "regime": winner,
        "confidence_pct": confidence,
        "components": {
            "hmm": hmm,
            "volatility": vol,
            "trend": trend,
            "breadth": breadth,
        },
        "votes": votes,
    }


# === Factor auto-disable in crisis ===

def should_disable_factor_in_crisis(factor_stats: dict, current_regime: str,
                                     vix_level: float) -> bool:
    """
    Auto-disable rule: if VIX > 35 AND factor's BEAR Sharpe < 0
    AND current regime is BEAR/CRISIS, disable the factor entirely
    until VIX drops back below 25.

    Inputs:
        factor_stats: output of compute_per_factor_stats with regime_split
        current_regime: BULL / BEAR / SIDEWAYS
        vix_level: current VIX

    Returns True if factor should be disabled (weight = 0).
    """
    if vix_level < 35:
        return False
    if current_regime not in ("BEAR", "CRISIS"):
        return False
    bear_sharpe = (factor_stats.get("regime_split") or {}).get("BEAR")
    if bear_sharpe is None:
        return False
    return bear_sharpe < 0


# === Drawdown brake ===

def should_apply_drawdown_brake(current_drawdown_pct: float,
                                 threshold_pct: float = 10.0) -> dict:
    """
    Drawdown brake: cut exposure proportionally when drawdown deepens.

    -5% drawdown:  exposure_mult = 1.0 (normal)
    -10% drawdown: exposure_mult = 0.7
    -15% drawdown: exposure_mult = 0.5
    -20%+ drawdown: exposure_mult = 0.3 (defensive only)
    """
    dd = abs(safe_float(current_drawdown_pct, 0.0))
    if dd < 5:
        mult = 1.0
        status = "OK"
    elif dd < 10:
        mult = 0.85
        status = "WATCH"
    elif dd < 15:
        mult = 0.65
        status = "BRAKE_ENGAGED"
    elif dd < 20:
        mult = 0.45
        status = "DEFENSIVE"
    else:
        mult = 0.30
        status = "CRITICAL"
    return {
        "drawdown_pct": dd,
        "exposure_multiplier": mult,
        "status": status,
    }
