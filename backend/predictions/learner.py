"""
Self-Learning System — how the Sentinel Quant hedge fund gets smarter over time.

After every batch of trades, this system:
  1. Analyzes which factors are working (win rate, Sharpe by factor)
  2. Analyzes sector-level performance (what sectors are we best at?)
  3. Analyzes regime-level performance (bull vs bear vs sideways)
  4. Auto-adjusts factor weights (higher Sharpe = more weight)
  5. Generates a "System Intelligence" report

The key insight: markets change, so a static model will decay.
This system continuously adapts to what's actually working NOW.

Weight updates happen after every 10 closed trades (minimum sample).
"""

import numpy as np
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

MIN_TRADES_FOR_UPDATE = 10  # Don't adjust weights with fewer trades
WEIGHT_ADJUSTMENT_RATE = 0.30  # 30% per cycle — faster learning given higher trade volume
MIN_WEIGHT = 0.05  # No factor can go below 5%
MAX_WEIGHT = 0.40  # No factor can go above 40%
RECENCY_DECAY = 0.95  # Recent trades matter more: each older trade has 5% less influence

# Minimum |contribution| (z*weight) for a factor to be considered "material"
# for a trade. Below this, the trade is not credited to that factor — otherwise
# every factor gets the same trade attribution and we can't differentiate
# which factors are actually predictive. Tuned so factors driving < ~1% of
# the composite score are filtered out.
FACTOR_MATERIAL_THRESHOLD = 0.01


FACTOR_NAMES = [
    "momentum", "value", "quality", "low_vol", "rsi2", "volume",
    "smart_money", "relative_strength", "bb_squeeze", "vwap",
    "hurst", "autocorr", "stat_arb", "kurtosis",
    "vol_compression", "mtf_alignment",
    "earnings_drift", "vpoc", "ichimoku", "sector_rotation",
    "candlestick",
    # AUDIT FIX m6 — was 21; picks engine reports total_factors=22 because
    # it also emits fundamental_value. Include here so learner credit and
    # weight tracking cover the full factor set (no silent off-by-one).
    "fundamental_value",
]


def _compute_factor_stats(trades: list) -> dict:
    """Compute per-factor performance stats with TIME-DECAY WEIGHTED SHARPE.

    Same algorithm used by both analyze_factor_performance (overall) and
    analyze_factor_performance_by_regime (per-regime subset).

    Recent trades count more heavily (RECENCY_DECAY = 0.95 per step back).
    The weighted Sharpe uses weighted mean and weighted std so a factor
    that worked great 6 months ago but stopped working last month gets
    a properly low Sharpe — instead of being inflated by old wins.

    SAFETY: returns empty per-factor stats for any factor with 0 trades
    or any computation error. Never raises.
    """
    if not trades:
        return {f: {"total_trades": 0, "win_rate": 0, "avg_return": 0, "sharpe": 0}
                for f in FACTOR_NAMES}

    n_trades = len(trades)
    recency_weights = [RECENCY_DECAY ** (n_trades - 1 - i) for i in range(n_trades)]
    factor_perf = {
        f: {"wins": 0.0, "losses": 0.0, "returns": [], "weights": [],
            "contributions": [], "material_trades": 0}
        for f in FACTOR_NAMES
    }

    for i, trade in enumerate(trades):
        try:
            pnl_pct = float(trade.get("pnl_pct", 0) or 0)
        except (TypeError, ValueError):
            continue
        # WINSORIZE outliers to [-20%, +20%]. A single -99% blowup (e.g. an
        # option going to zero) would otherwise dominate a factor's Sharpe and
        # crush its weight to MIN_WEIGHT based on one bad event. Capping at
        # ±20% preserves directional signal while preventing one tail event
        # from overpowering decades of normal-magnitude trades.
        if pnl_pct > 20.0:
            pnl_pct = 20.0
        elif pnl_pct < -20.0:
            pnl_pct = -20.0
        w = recency_weights[i]

        # Trade direction: 'long' means we bought; 'short' means we sold.
        # For a long trade, a factor with POSITIVE contribution agreed
        # (it said "go up"). For a short trade, a factor with NEGATIVE
        # contribution agreed (it said "go down"). A factor that AGREED
        # and the trade was profitable was right. A factor that AGREED
        # and the trade lost was wrong. The unified formula:
        #   trade_dir_sign  = +1 long, -1 short
        #   factor_agreed   = sign(contribution) * trade_dir_sign > 0
        #   factor_was_right= factor_agreed XOR (pnl < 0)
        # Equivalently: aligned_pnl = sign(contribution) * trade_dir_sign * pnl
        # is positive iff the factor was directionally right.
        direction_raw = str(trade.get("direction") or "long").strip().lower()
        trade_dir_sign = -1.0 if direction_raw in ("short", "sell", "puts", "put") else 1.0

        try:
            factors = json.loads(trade.get("factors_used", "{}") or "{}")
        except Exception:
            factors = {}
        if not isinstance(factors, dict) or not factors:
            continue

        for factor_name in FACTOR_NAMES:
            factor_data = factors.get(factor_name, {})
            if not isinstance(factor_data, dict):
                factor_data = {}
            try:
                contribution = float(factor_data.get("contribution", 0) or 0)
            except (TypeError, ValueError, AttributeError):
                contribution = 0.0
            abs_contrib = abs(contribution)
            # Only credit this factor for this trade if it was a material
            # driver of the score. Otherwise every factor accumulates every
            # trade and they all show identical stats — making "learning"
            # meaningless. The recency weight is multiplied by abs_contrib
            # so a factor that drove the trade strongly gets more credit
            # than one that barely contributed.
            if abs_contrib < FACTOR_MATERIAL_THRESHOLD:
                # Track contribution even if not material, so avg_contribution
                # reflects the full distribution.
                factor_perf[factor_name]["contributions"].append(contribution)
                continue
            effective_w = w * abs_contrib
            contrib_sign = 1.0 if contribution >= 0 else -1.0
            # aligned_pnl > 0  ⇨  factor's direction was correct
            # aligned_pnl < 0  ⇨  factor's direction was wrong
            # Works correctly for BOTH long and short trades.
            aligned_pnl = contrib_sign * trade_dir_sign * pnl_pct
            factor_perf[factor_name]["contributions"].append(contribution)
            factor_perf[factor_name]["returns"].append(aligned_pnl)
            factor_perf[factor_name]["weights"].append(effective_w)
            factor_perf[factor_name]["material_trades"] += 1
            if aligned_pnl > 0:
                factor_perf[factor_name]["wins"] += effective_w
            else:
                factor_perf[factor_name]["losses"] += effective_w

    results = {}
    for factor_name, data in factor_perf.items():
        total_w = data["wins"] + data["losses"]
        if total_w == 0 or not data["returns"]:
            results[factor_name] = {
                "total_trades": 0,
                "win_rate": 0,
                "avg_return": 0,
                "sharpe": 0,
            }
            continue

        try:
            returns = np.array(data["returns"], dtype=float)
            weights = np.array(data["weights"], dtype=float)
            sum_w = float(np.sum(weights))
            if sum_w <= 0:
                # All zero weights (impossible but defensive)
                weighted_avg = float(np.mean(returns))
                weighted_std = float(np.std(returns)) if len(returns) > 1 else 1.0
            else:
                # Time-decay weighted mean and std
                weighted_avg = float(np.sum(weights * returns) / sum_w)
                if len(returns) > 1:
                    weighted_var = float(np.sum(weights * (returns - weighted_avg) ** 2) / sum_w)
                    weighted_std = float(np.sqrt(max(0.0, weighted_var)))
                else:
                    weighted_std = 1.0

            # Sharpe (annualized assuming ~20 trades per year)
            # Floor std at 0.1 to prevent numerical blowup when a factor's
            # aligned returns are too tightly clustered (which would push the
            # Sharpe into the billions and dominate target-weight allocation
            # purely from low variance). 0.1% return-volatility is well below
            # any realistic factor's noise floor.
            safe_std = max(weighted_std, 0.1)
            sharpe = (weighted_avg / safe_std) * np.sqrt(20)
            # Hard clamp Sharpe to ±5 — any real factor with sharpe > 5
            # annualized is almost certainly a measurement artifact, not skill.
            sharpe = max(-5.0, min(5.0, sharpe))
            win_rate = (data["wins"] / total_w) * 100 if total_w > 0 else 0

            results[factor_name] = {
                "total_trades": int(data.get("material_trades", 0)),
                "win_rate": round(win_rate, 1),
                "avg_return": round(weighted_avg, 2),
                "sharpe": round(sharpe, 2),
                "avg_contribution": round(float(np.mean(data["contributions"])), 4)
                    if data["contributions"] else 0,
            }
        except Exception:
            # Defensive: any per-factor numeric error gets zeroed out, doesn't crash the whole thing
            results[factor_name] = {
                "total_trades": 0, "win_rate": 0, "avg_return": 0, "sharpe": 0,
            }
    return results


def analyze_factor_performance() -> dict:
    """
    Analyze how each scoring factor has performed in actual trades.

    For each factor (momentum, value, quality, low_vol, rsi2, volume, ...):
      - Win rate (time-decay weighted: recent wins count more)
      - Average return (time-decay weighted)
      - Sharpe ratio of returns (time-decay weighted — both mean and std)

    The Sharpe is annualized assuming ~20 trades per year. Time-decay
    means a factor that's stopped working in the last month will show
    a low Sharpe even if it had years of good history before that.

    Returns:
        dict of factor_name → performance metrics
    """
    from predictions.models import get_closed_trades

    closed = get_closed_trades(limit=500)
    if not closed:
        return {"message": "No closed trades to analyze", "factors": {}}

    results = _compute_factor_stats(closed)
    # SELF-TEST: detect the "all factors identical" bug that broke learning
    # silently for weeks.
    try:
        _stats = [(s.get("win_rate"), s.get("sharpe")) for n, s in results.items()
                  if s.get("total_trades", 0) > 0]
        if len(_stats) >= 5:
            from collections import Counter
            most_common, count = Counter(_stats).most_common(1)[0]
            if count / len(_stats) >= 0.9 and most_common != (0, 0):
                logger.warning(
                    "LEARNING SELF-TEST FAIL: %d/%d factors have identical "
                    "(win_rate, sharpe)=%s — credit logic may be broken.",
                    count, len(_stats), most_common,
                )
    except Exception as _ste:
        logger.debug(f"Learning self-test failed (non-fatal): {_ste}")

    # STRENGTHS & WEAKNESSES: explicit lists the UI / system intelligence can
    # render directly. Requires a factor have enough material trades to be
    # meaningful (>= 5) — random noise on tiny samples shouldn't get labeled
    # as a strength or weakness.
    strengths = []
    weaknesses = []
    try:
        scored = []
        for name, s in results.items():
            if s.get("total_trades", 0) >= 5:
                scored.append({
                    "factor": name,
                    "sharpe": float(s.get("sharpe", 0) or 0),
                    "win_rate": float(s.get("win_rate", 0) or 0),
                    "avg_return": float(s.get("avg_return", 0) or 0),
                    "total_trades": int(s.get("total_trades", 0) or 0),
                })
        # Strengths: Sharpe > 0.5 AND win_rate > 50%
        for f in scored:
            if f["sharpe"] > 0.5 and f["win_rate"] > 50:
                strengths.append({
                    **f,
                    "label": "STRENGTH",
                    "reason": (f"Sharpe {f['sharpe']:+.2f} with "
                               f"{f['win_rate']:.0f}% win rate over "
                               f"{f['total_trades']} trades"),
                })
        # Weaknesses: Sharpe < -0.5 OR (win_rate < 40% AND sharpe < 0)
        for f in scored:
            if f["sharpe"] < -0.5 or (f["win_rate"] < 40 and f["sharpe"] < 0):
                weaknesses.append({
                    **f,
                    "label": "WEAKNESS",
                    "reason": (f"Sharpe {f['sharpe']:+.2f} with only "
                               f"{f['win_rate']:.0f}% win rate over "
                               f"{f['total_trades']} trades"),
                })
        # Rank: strongest first, weakest first (most-negative Sharpe first)
        strengths.sort(key=lambda x: x["sharpe"], reverse=True)
        weaknesses.sort(key=lambda x: x["sharpe"])
    except Exception as _swe:
        logger.debug(f"Strengths/weaknesses extraction failed (non-fatal): {_swe}")

    return {
        "factors": results,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "total_trades_analyzed": len(closed),
        "timestamp": datetime.now().isoformat(),
    }


def analyze_factor_performance_by_regime() -> dict:
    """Per-regime factor performance — used to compute regime-specific weights.

    Splits closed trades by regime_at_entry (BULL / BEAR / SIDEWAYS / etc.)
    and computes time-decay weighted factor stats separately for each regime.

    Only regimes with >= MIN_TRADES_FOR_UPDATE trades are included — others
    don't have enough sample to learn from yet.

    Returns: {regime: {factor_name: {sharpe, win_rate, ...}}}
    Empty dict if no closed trades.
    """
    from predictions.models import get_closed_trades

    closed = get_closed_trades(limit=500)
    if not closed:
        return {}

    by_regime = {}
    for t in closed:
        regime = (t.get("regime_at_entry") or "Unknown").upper()
        if regime not in by_regime:
            by_regime[regime] = []
        by_regime[regime].append(t)

    result = {}
    for regime, trades in by_regime.items():
        if len(trades) < MIN_TRADES_FOR_UPDATE:
            continue  # Need minimum sample per regime
        result[regime] = _compute_factor_stats(trades)
    return result


def analyze_sector_performance() -> dict:
    """Which sectors are we best at trading?"""
    from predictions.models import get_closed_trades

    closed = get_closed_trades(limit=500)
    if not closed:
        return {"sectors": {}}

    sector_stats = {}
    for trade in closed:
        sector = trade.get("sector") or "Unknown"
        if sector not in sector_stats:
            sector_stats[sector] = {"wins": 0, "total": 0, "returns": []}

        pnl = trade.get("pnl_pct", 0) or 0
        sector_stats[sector]["total"] += 1
        sector_stats[sector]["returns"].append(pnl)
        if pnl > 0:
            sector_stats[sector]["wins"] += 1

    results = {}
    for sector, data in sector_stats.items():
        if data["total"] < 3:
            continue
        results[sector] = {
            "total_trades": data["total"],
            "win_rate": round(data["wins"] / data["total"] * 100, 1),
            "avg_return": round(float(np.mean(data["returns"])), 2),
            "best_trade": round(float(max(data["returns"])), 2),
            "worst_trade": round(float(min(data["returns"])), 2),
        }

    # Sort by win rate
    sorted_sectors = dict(sorted(results.items(), key=lambda x: x[1]["win_rate"], reverse=True))

    return {
        "sectors": sorted_sectors,
        "best_sector": max(results, key=lambda k: results[k]["win_rate"]) if results else None,
        "worst_sector": min(results, key=lambda k: results[k]["win_rate"]) if results else None,
    }


def analyze_sector_performance_by_direction() -> dict:
    """
    Per-direction sector analysis — critical for fixing short side performance.
    Returns separate stats for longs and shorts in each sector so the system
    can block bad-short sectors without blocking good-long sectors (and vice versa).
    """
    from predictions.models import get_closed_trades

    closed = get_closed_trades(limit=500)
    if not closed:
        return {"long_sectors": {}, "short_sectors": {}}

    by_direction = {"long": {}, "short": {}}
    for trade in closed:
        sector = trade.get("sector") or "Unknown"
        direction = (trade.get("direction") or "long").lower()
        if direction not in by_direction:
            continue
        if sector not in by_direction[direction]:
            by_direction[direction][sector] = {"wins": 0, "total": 0, "returns": []}

        pnl = trade.get("pnl_pct", 0) or 0
        by_direction[direction][sector]["total"] += 1
        by_direction[direction][sector]["returns"].append(pnl)
        if pnl > 0:
            by_direction[direction][sector]["wins"] += 1

    def _summarize(stats_dict):
        result = {}
        for sector, data in stats_dict.items():
            if data["total"] < 3:
                continue
            result[sector] = {
                "total_trades": data["total"],
                "win_rate": round(data["wins"] / data["total"] * 100, 1),
                "avg_return": round(float(np.mean(data["returns"])), 2),
            }
        return dict(sorted(result.items(), key=lambda x: x[1]["win_rate"], reverse=True))

    return {
        "long_sectors": _summarize(by_direction["long"]),
        "short_sectors": _summarize(by_direction["short"]),
    }


def analyze_regime_performance() -> dict:
    """How do we perform in different market regimes?"""
    from predictions.models import get_closed_trades

    closed = get_closed_trades(limit=500)
    if not closed:
        return {"regimes": {}}

    regime_stats = {}
    for trade in closed:
        regime = trade.get("regime_at_entry") or "Unknown"
        if regime not in regime_stats:
            regime_stats[regime] = {"wins": 0, "total": 0, "returns": []}

        pnl = trade.get("pnl_pct", 0) or 0
        regime_stats[regime]["total"] += 1
        regime_stats[regime]["returns"].append(pnl)
        if pnl > 0:
            regime_stats[regime]["wins"] += 1

    results = {}
    for regime, data in regime_stats.items():
        if data["total"] < 3:
            continue
        results[regime] = {
            "total_trades": data["total"],
            "win_rate": round(data["wins"] / data["total"] * 100, 1),
            "avg_return": round(float(np.mean(data["returns"])), 2),
        }

    return {"regimes": results}


def _compute_target_weights_from_factors(current_weights: dict, factor_stats: dict) -> tuple:
    """Compute target weights from factor Sharpe ratios. Pure function — no DB writes.

    Returns: (target_weights dict, sharpes dict used for the computation)
    """
    sharpes = {}
    for name in current_weights:
        if name in factor_stats and factor_stats[name].get("total_trades", 0) >= 5:
            sharpes[name] = max(0, factor_stats[name].get("sharpe", 0))
        else:
            sharpes[name] = 0.5  # Default for factors with insufficient data
    total_sharpe = sum(sharpes.values()) + 1e-10
    target_weights = {name: sharpe / total_sharpe for name, sharpe in sharpes.items()}
    return target_weights, sharpes


def _walk_forward_unstable_factors(closed_trades: list) -> set:
    """WALK-FORWARD VALIDATION — flag factors whose recent performance
    contradicts their historical performance.

    Algorithm:
      - Split closed trades into TRAIN (older 70%) and TEST (newer 30%)
      - Compute factor Sharpe on each set independently
      - A factor is UNSTABLE if both Sharpes are meaningful (>0.5 in magnitude)
        but disagree in sign (one positive, one negative)
      - Unstable factors should NOT have their weights bumped — keep them flat

    This is the safety gate that prevents fitting to noise. A factor that
    won big in TRAIN but lost in TEST is overfit and shouldn't drive trades.

    Returns: set of factor_name strings that should be held at current weight.
    Returns empty set if there's not enough data to validate (fail-safe — don't
    block updates when we can't validate).
    """
    n = len(closed_trades)
    if n < 30:
        # Not enough data to do meaningful walk-forward — don't block any updates
        return set()

    split = max(20, int(n * 0.7))
    train_trades = closed_trades[:split]   # older — already-known patterns
    test_trades = closed_trades[split:]    # newer — out-of-sample test
    if len(test_trades) < 10:
        return set()

    try:
        train_stats = _compute_factor_stats(train_trades)
        test_stats = _compute_factor_stats(test_trades)
    except Exception as e:
        logger.warning(f"Walk-forward stats computation failed: {e}")
        return set()

    unstable = set()
    for fname in FACTOR_NAMES:
        train_s = train_stats.get(fname, {}).get("sharpe", 0)
        test_s = test_stats.get(fname, {}).get("sharpe", 0)
        # Both Sharpes meaningful AND directionally disagree → unstable
        if abs(train_s) > 0.5 and abs(test_s) > 0.5:
            if (train_s > 0) != (test_s > 0):
                unstable.add(fname)
    return unstable


def auto_adjust_weights() -> dict:
    """
    Auto-adjust factor weights based on actual performance.

    Algorithm:
      1. Calculate time-decay weighted Sharpe for each factor (recent trades count more)
      2. WALK-FORWARD VALIDATION: flag factors whose train and test Sharpe disagree
      3. Factors with higher Sharpe get more weight (target_weight ~ Sharpe)
      4. STABLE factors: shift WEIGHT_ADJUSTMENT_RATE toward target
      5. UNSTABLE factors (failed walk-forward): keep current weight (no change)
      6. Enforce MIN_WEIGHT / MAX_WEIGHT bounds, normalize to sum = 1.0
      7. ALSO compute per-regime weights and store them (NOT consumed by picks
         tonight — wired into picks generation in a future deploy)

    This is the core of the self-learning system. The walk-forward gate prevents
    overfitting to noise; per-regime tracking lets future weights adapt to the
    market state.
    """
    from predictions.models import (
        get_signal_weights, update_signal_weight, get_closed_trades,
        update_regime_factor_weight,
    )

    closed = get_closed_trades(limit=500)
    if len(closed) < MIN_TRADES_FOR_UPDATE:
        return {
            "updated": False,
            "reason": f"Need {MIN_TRADES_FOR_UPDATE} trades, have {len(closed)}",
        }

    # ============================================================
    # GLOBAL (cross-regime) factor weight update — what picks USE today
    # ============================================================
    current_weights = get_signal_weights()
    factor_perf = analyze_factor_performance()

    if not factor_perf.get("factors"):
        return {"updated": False, "reason": "No factor data available"}

    factors = factor_perf["factors"]
    target_weights, sharpes = _compute_target_weights_from_factors(current_weights, factors)

    # WALK-FORWARD VALIDATION — identify unstable factors (whose recent and
    # historical performance disagree). These keep their current weight.
    unstable = _walk_forward_unstable_factors(closed)
    if unstable:
        logger.warning(
            f"WALK-FORWARD GATE: {len(unstable)} factors flagged as unstable "
            f"(train/test Sharpe sign disagree): {sorted(unstable)}"
        )

    # Blend current weights toward target — but UNSTABLE factors stay flat
    new_weights = {}
    for name in current_weights:
        current = current_weights[name]
        if name in unstable:
            new_weights[name] = current  # walk-forward gate: hold steady
            continue
        target = target_weights.get(name, current)
        new = current + WEIGHT_ADJUSTMENT_RATE * (target - current)
        new = max(MIN_WEIGHT, min(MAX_WEIGHT, new))
        new_weights[name] = new

    # Normalize to sum = 1.0
    total = sum(new_weights.values())
    new_weights = {k: round(v / total, 4) for k, v in new_weights.items()}

    # Save updated GLOBAL weights (these are what picks consume)
    for name, weight in new_weights.items():
        perf = factors.get(name, {})
        update_signal_weight(
            factor_name=name,
            weight=weight,
            win_rate=perf.get("win_rate", 0),
            avg_return=perf.get("avg_return", 0),
            sharpe=perf.get("sharpe", 0),
            total_trades=perf.get("total_trades", 0),
        )

    # ============================================================
    # PER-REGIME factor weights — COMPUTED AND STORED ONLY.
    # NOT consumed by picks generation yet. This lets data accumulate
    # so a future deploy can switch to regime-aware picks safely.
    # Wrapped in try/except so per-regime failure cannot break the
    # global weight update above.
    # ============================================================
    regimes_updated = []
    try:
        regime_perf = analyze_factor_performance_by_regime()
        for regime, regime_factors in regime_perf.items():
            try:
                # Compute target weights for this regime independently
                regime_targets, _ = _compute_target_weights_from_factors(
                    current_weights, regime_factors
                )
                # Blend toward target (slower adjustment for per-regime since
                # the per-regime sample is smaller)
                regime_new = {}
                for name in current_weights:
                    cur = current_weights[name]
                    target = regime_targets.get(name, cur)
                    new = cur + (WEIGHT_ADJUSTMENT_RATE * 0.5) * (target - cur)
                    new = max(MIN_WEIGHT, min(MAX_WEIGHT, new))
                    regime_new[name] = new
                # Normalize
                rt = sum(regime_new.values())
                if rt > 0:
                    regime_new = {k: round(v / rt, 4) for k, v in regime_new.items()}
                # Persist
                for name, weight in regime_new.items():
                    perf = regime_factors.get(name, {})
                    update_regime_factor_weight(
                        regime=regime,
                        factor_name=name,
                        weight=weight,
                        win_rate=perf.get("win_rate", 0),
                        sharpe=perf.get("sharpe", 0),
                        total_trades=perf.get("total_trades", 0),
                    )
                regimes_updated.append({
                    "regime": regime,
                    "trades": sum(f.get("total_trades", 0) for f in regime_factors.values()) // max(1, len(regime_factors)),
                })
            except Exception as e:
                logger.debug(f"Per-regime weight update failed for {regime}: {e}")
    except Exception as e:
        logger.warning(f"Per-regime learning cycle failed (non-fatal): {e}")

    return {
        "updated": True,
        "previous_weights": current_weights,
        "new_weights": new_weights,
        "sharpe_scores": {k: round(v, 2) for k, v in sharpes.items()},
        "trades_analyzed": len(closed),
        "walk_forward_unstable": sorted(unstable),
        "regimes_updated": regimes_updated,
        "timestamp": datetime.now().isoformat(),
        "_note_per_regime": "Per-regime weights computed and stored but NOT yet consumed by picks generation",
    }


def analyze_mistakes() -> dict:
    """
    Learn from past mistakes — the most important intelligence upgrade.

    Analyzes every losing trade to find PATTERNS in what went wrong:
      1. Bad sector timing (e.g., went long tech when yields were rising)
      2. Wrong direction (longs in bear, shorts in bull)
      3. Held too long (didn't cut losses fast enough)
      4. Overconfident (high confidence but lost)
      5. Correlated losses (multiple losses in same sector/timeframe)

    Returns specific rules the system should follow to avoid repeating mistakes.
    """
    from predictions.models import get_closed_trades

    closed = get_closed_trades(limit=500)
    if not closed:
        return {"lessons": [], "mistake_patterns": {}, "total_losses": 0}

    losers = [t for t in closed if (t.get("pnl_pct", 0) or 0) < 0]
    winners = [t for t in closed if (t.get("pnl_pct", 0) or 0) > 0]

    if not losers:
        return {"lessons": ["No losses yet — system is performing perfectly"], "total_losses": 0}

    total = len(closed)
    loss_count = len(losers)
    avg_loss = float(np.mean([t.get("pnl_pct", 0) or 0 for t in losers]))
    avg_win = float(np.mean([t.get("pnl_pct", 0) or 0 for t in winners])) if winners else 0
    worst_loss = min([t.get("pnl_pct", 0) or 0 for t in losers])

    lessons = []
    mistake_patterns = {}

    # --- Pattern 1: Sector-specific losses ---
    sector_losses = {}
    sector_wins = {}
    for t in losers:
        s = t.get("sector") or "Unknown"
        sector_losses[s] = sector_losses.get(s, 0) + 1
    for t in winners:
        s = t.get("sector") or "Unknown"
        sector_wins[s] = sector_wins.get(s, 0) + 1

    bad_sectors = []
    for sector, loss_n in sector_losses.items():
        win_n = sector_wins.get(sector, 0)
        total_sector = loss_n + win_n
        if total_sector >= 3 and loss_n / total_sector > 0.65:
            bad_sectors.append(sector)
            lessons.append(
                f"AVOID {sector}: {loss_n}/{total_sector} trades lost "
                f"({round(loss_n / total_sector * 100)}% loss rate) — reduce confidence for this sector"
            )
    mistake_patterns["weak_sectors"] = bad_sectors

    # --- Pattern 2: Direction mistakes by regime ---
    regime_dir_losses = {}
    regime_dir_total = {}
    for t in closed:
        regime = t.get("regime_at_entry") or "Unknown"
        direction = t.get("direction") or "Unknown"
        key = f"{regime}_{direction}"
        regime_dir_total[key] = regime_dir_total.get(key, 0) + 1
        if (t.get("pnl_pct", 0) or 0) < 0:
            regime_dir_losses[key] = regime_dir_losses.get(key, 0) + 1

    bad_combos = []
    for key, loss_n in regime_dir_losses.items():
        total_n = regime_dir_total.get(key, 1)
        if total_n >= 3 and loss_n / total_n > 0.70:
            bad_combos.append(key)
            regime, direction = key.rsplit("_", 1)
            lessons.append(
                f"STOP going {direction} in {regime} regime: "
                f"{loss_n}/{total_n} trades lost ({round(loss_n / total_n * 100)}%)"
            )
    mistake_patterns["bad_regime_direction_combos"] = bad_combos

    # --- Pattern 3: Overconfidence analysis ---
    high_conf_losses = [t for t in losers if (t.get("signal_score", 0) or 0) > 5]
    if high_conf_losses and len(high_conf_losses) >= 3:
        overconf_rate = len(high_conf_losses) / len(losers) * 100
        lessons.append(
            f"OVERCONFIDENCE DETECTED: {len(high_conf_losses)} high-confidence trades lost "
            f"({overconf_rate:.0f}% of all losses) — reduce confidence threshold"
        )
        mistake_patterns["overconfidence_issue"] = True
    else:
        mistake_patterns["overconfidence_issue"] = False

    # --- Pattern 4: Holding too long ---
    long_hold_losses = []
    quick_win_avg = []
    for t in losers:
        entry = t.get("entry_date")
        exit_t = t.get("exit_date")
        if entry and exit_t:
            try:
                from datetime import datetime as dt_class
                e_time = dt_class.fromisoformat(entry) if isinstance(entry, str) else entry
                x_time = dt_class.fromisoformat(exit_t) if isinstance(exit_t, str) else exit_t
                hold_hours = (x_time - e_time).total_seconds() / 3600
                if hold_hours > 48:  # Held more than 2 days
                    long_hold_losses.append(hold_hours)
            except Exception:
                continue

    if long_hold_losses and len(long_hold_losses) >= 3:
        avg_hold = np.mean(long_hold_losses)
        lessons.append(
            f"CUT LOSSES FASTER: {len(long_hold_losses)} losing trades held for avg "
            f"{avg_hold:.0f}hrs — consider tighter stop-loss or shorter hold period"
        )
        mistake_patterns["holding_too_long"] = True
    else:
        mistake_patterns["holding_too_long"] = False

    # --- Pattern 5: Biggest individual mistakes ---
    worst_trades = sorted(losers, key=lambda t: t.get("pnl_pct", 0) or 0)[:5]
    worst_details = []
    for t in worst_trades:
        worst_details.append({
            "ticker": t.get("ticker", "?"),
            "direction": t.get("direction", "?"),
            "pnl_pct": round(t.get("pnl_pct", 0) or 0, 2),
            "sector": t.get("sector", "?"),
            "regime": t.get("regime_at_entry", "?"),
        })
    mistake_patterns["worst_trades"] = worst_details

    # --- Summary stats ---
    return {
        "total_trades": total,
        "total_losses": loss_count,
        "loss_rate": round(loss_count / total * 100, 1),
        "avg_loss_pct": round(avg_loss, 2),
        "avg_win_pct": round(avg_win, 2),
        "win_loss_ratio": round(abs(avg_win / avg_loss), 2) if avg_loss != 0 else 0,
        "worst_loss_pct": round(worst_loss, 2),
        "lessons": lessons,
        "mistake_patterns": mistake_patterns,
        "timestamp": datetime.now().isoformat(),
    }


def get_mistake_adjustments() -> dict:
    """
    Returns real-time adjustments based on learned mistakes.
    Used by paper_trader to avoid repeating errors.

    Now includes per-direction sector penalties so a sector that's bad for shorts
    but good for longs gets penalized correctly per side, instead of blanket-blocking.
    """
    mistakes = analyze_mistakes()
    adjustments = {
        "sector_penalties": {},          # sector -> confidence penalty (legacy, both directions)
        "long_sector_penalties": {},     # sector -> penalty for LONG trades only
        "short_sector_penalties": {},    # sector -> penalty for SHORT trades only
        "blocked_combos": [],            # ["BEAR_LONG", etc.] — combos to avoid
        "confidence_cap": 95,            # max confidence (lower if overconfident)
        "tighten_stops": False,          # if True, use tighter stop-loss
    }

    patterns = mistakes.get("mistake_patterns", {})

    # Legacy blanket sector penalty (kept for backwards compatibility)
    for sector in patterns.get("weak_sectors", []):
        adjustments["sector_penalties"][sector] = -15  # -15% (was -10) — stronger learning

    # PER-DIRECTION sector penalties — fixes the short-side problem
    # Sectors with <40% win rate for a given direction get penalized for THAT direction only
    try:
        by_dir = analyze_sector_performance_by_direction()
        for sector, stats in by_dir.get("long_sectors", {}).items():
            wr = stats.get("win_rate", 50)
            n = stats.get("total_trades", 0)
            if n >= 5 and wr < 40:
                # Scale penalty: worse win rate = larger penalty, capped at -20
                penalty = max(-20, -10 - int((40 - wr) / 2))
                adjustments["long_sector_penalties"][sector] = penalty
        for sector, stats in by_dir.get("short_sectors", {}).items():
            wr = stats.get("win_rate", 50)
            n = stats.get("total_trades", 0)
            if n >= 5 and wr < 40:
                penalty = max(-20, -10 - int((40 - wr) / 2))
                adjustments["short_sector_penalties"][sector] = penalty
    except Exception:
        pass  # Per-direction is enhancement; falls back to legacy if it fails

    # Don't completely block regime/direction combos, but heavily penalize
    for combo in patterns.get("bad_regime_direction_combos", []):
        adjustments["blocked_combos"].append(combo)

    # Cap confidence if overconfident
    if patterns.get("overconfidence_issue"):
        adjustments["confidence_cap"] = 80

    # Tighten stops if holding too long
    if patterns.get("holding_too_long"):
        adjustments["tighten_stops"] = True

    return adjustments


def generate_intelligence_report() -> dict:
    """
    System Intelligence Report — a comprehensive view of what the
    system has learned, its strengths, weaknesses, and changes.

    This is the "brain scan" of the hedge fund.
    """
    from predictions.models import get_signal_weights, get_closed_trades

    report = {
        "generated_at": datetime.now().isoformat(),
        "system_status": "learning",
    }

    # Factor analysis
    factor_analysis = analyze_factor_performance()
    report["factor_performance"] = factor_analysis.get("factors", {})

    # Sector analysis
    sector_analysis = analyze_sector_performance()
    report["sector_performance"] = sector_analysis

    # Regime analysis
    regime_analysis = analyze_regime_performance()
    report["regime_performance"] = regime_analysis

    # Current weights
    weights = get_signal_weights()
    report["current_weights"] = weights

    # Closed trades count
    closed = get_closed_trades(limit=500)
    report["total_closed"] = len(closed)

    # --- Generate insights (human-readable) ---
    insights = []
    strengths = []
    weaknesses = []

    # Factor insights
    factors = factor_analysis.get("factors", {})
    if factors:
        best_factor = max(factors, key=lambda k: factors[k].get("sharpe", 0))
        worst_factor = min(factors, key=lambda k: factors[k].get("sharpe", 0))

        if factors[best_factor].get("sharpe", 0) > 1:
            strengths.append(
                f"{best_factor.replace('_', ' ').title()} factor performing well "
                f"(Sharpe: {factors[best_factor]['sharpe']}, "
                f"Win Rate: {factors[best_factor]['win_rate']}%)"
            )

        if factors[worst_factor].get("sharpe", 0) < 0:
            weaknesses.append(
                f"{worst_factor.replace('_', ' ').title()} factor underperforming "
                f"(Sharpe: {factors[worst_factor]['sharpe']})"
            )

    # Sector insights
    sectors = sector_analysis.get("sectors", {})
    if sectors:
        best_sector = sector_analysis.get("best_sector")
        worst_sector = sector_analysis.get("worst_sector")
        if best_sector and sectors.get(best_sector, {}).get("win_rate", 0) > 60:
            strengths.append(
                f"Strong at trading {best_sector} "
                f"({sectors[best_sector]['win_rate']}% win rate)"
            )
        if worst_sector and sectors.get(worst_sector, {}).get("win_rate", 0) < 40:
            weaknesses.append(
                f"Struggling with {worst_sector} "
                f"({sectors[worst_sector]['win_rate']}% win rate)"
            )

    # Mistake analysis — learn from losses
    mistake_analysis = analyze_mistakes()
    report["mistake_analysis"] = mistake_analysis
    for lesson in mistake_analysis.get("lessons", []):
        weaknesses.append(lesson)
    if mistake_analysis.get("win_loss_ratio", 0) > 1.5:
        strengths.append(
            f"Good risk/reward: wins avg {mistake_analysis['avg_win_pct']}% vs "
            f"losses avg {mistake_analysis['avg_loss_pct']}%"
        )

    # Regime insights
    regimes = regime_analysis.get("regimes", {})
    for regime_name, stats in regimes.items():
        if stats["win_rate"] > 65:
            strengths.append(
                f"Good performance in {regime_name} markets "
                f"({stats['win_rate']}% win rate)"
            )
        elif stats["win_rate"] < 40:
            weaknesses.append(
                f"Poor performance in {regime_name} markets "
                f"({stats['win_rate']}% win rate)"
            )

    # Overall assessment
    if len(closed) < 20:
        insights.append("System is still in early learning phase — need more trades for reliable statistics")
        report["system_status"] = "early_learning"
    elif len(closed) < 100:
        insights.append("Building confidence — patterns emerging but sample size still growing")
        report["system_status"] = "learning"
    else:
        if any(f.get("sharpe", 0) > 1.5 for f in factors.values()):
            insights.append("System has found strong edges — continue current strategy")
            report["system_status"] = "confident"
        else:
            insights.append("System is adapting — no dominant edges yet, diversified approach is best")
            report["system_status"] = "adapting"

    # Weight change recommendations
    if len(closed) >= MIN_TRADES_FOR_UPDATE:
        insights.append(
            f"Weight adjustment eligible ({len(closed)} trades analyzed). "
            f"Auto-adjustment will shift weights toward better-performing factors."
        )

    report["insights"] = insights
    report["strengths"] = strengths
    report["weaknesses"] = weaknesses

    # Confidence calibration: predicted confidence vs actual win rate
    if closed:
        confidence_buckets = {
            "50-60": {"predicted": 0, "actual_wins": 0, "total": 0},
            "60-70": {"predicted": 0, "actual_wins": 0, "total": 0},
            "70-80": {"predicted": 0, "actual_wins": 0, "total": 0},
            "80-90": {"predicted": 0, "actual_wins": 0, "total": 0},
            "90+": {"predicted": 0, "actual_wins": 0, "total": 0},
        }

        for trade in closed:
            score = trade.get("signal_score", 0) or 0
            # Map score to approximate confidence
            conf = min(95, 50 + abs(score) * 5)
            pnl = trade.get("pnl_pct", 0) or 0

            if conf < 60:
                bucket = "50-60"
            elif conf < 70:
                bucket = "60-70"
            elif conf < 80:
                bucket = "70-80"
            elif conf < 90:
                bucket = "80-90"
            else:
                bucket = "90+"

            confidence_buckets[bucket]["total"] += 1
            confidence_buckets[bucket]["predicted"] += conf
            if pnl > 0:
                confidence_buckets[bucket]["actual_wins"] += 1

        calibration = {}
        for bucket, data in confidence_buckets.items():
            if data["total"] >= 3:
                calibration[bucket] = {
                    "avg_predicted_confidence": round(data["predicted"] / data["total"], 1),
                    "actual_win_rate": round(data["actual_wins"] / data["total"] * 100, 1),
                    "total_trades": data["total"],
                }

        report["confidence_calibration"] = calibration

    # --- STREAK ANALYSIS ---
    # Show current win/loss streak and its impact on sizing
    try:
        if len(closed) >= 3:
            streak = 0
            streak_type = "win" if (closed[0].get("pnl_pct", 0) or 0) > 0 else "loss"
            for t in closed:
                pnl = t.get("pnl_pct", 0) or 0
                if streak_type == "win" and pnl > 0:
                    streak += 1
                elif streak_type == "loss" and pnl <= 0:
                    streak += 1
                else:
                    break

            # Calculate impact
            if streak_type == "win" and streak >= 5:
                impact = f"+{min(25, (streak - 4) * 10)}% position size, -{min(8, (streak - 4) * 3)} confidence threshold"
            elif streak_type == "loss" and streak >= 3:
                impact = f"-{min(50, (streak - 2) * 15)}% position size, +{min(15, (streak - 2) * 5)} confidence threshold"
            else:
                impact = "No adjustment (streak too short)"

            report["streak_analysis"] = {
                "current_streak_type": streak_type,
                "streak_length": streak,
                "impact": impact,
                "last_10_results": [
                    {"ticker": t.get("ticker", "?"), "pnl_pct": round(t.get("pnl_pct", 0) or 0, 2)}
                    for t in closed[:10]
                ],
            }
    except Exception:
        pass

    return report
