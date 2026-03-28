"""
Self-Learning System — how the Epic Fury hedge fund gets smarter over time.

After every batch of trades, this system:
  1. Analyzes which factors are working (win rate, Sharpe by factor)
  2. Analyzes sector-level performance (what sectors are we best at?)
  3. Analyzes regime-level performance (bull vs bear vs sideways)
  4. Auto-adjusts factor weights (higher Sharpe = more weight)
  5. Generates a "System Intelligence" report

The key insight: markets change, so a static model will decay.
This system continuously adapts to what's actually working NOW.

Weight updates happen after every 20 closed trades (minimum sample).
"""

import numpy as np
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

MIN_TRADES_FOR_UPDATE = 20  # Don't adjust weights with fewer trades
WEIGHT_ADJUSTMENT_RATE = 0.15  # How much to shift weights (15% per cycle)
MIN_WEIGHT = 0.05  # No factor can go below 5%
MAX_WEIGHT = 0.40  # No factor can go above 40%


def analyze_factor_performance() -> dict:
    """
    Analyze how each scoring factor has performed in actual trades.

    For each factor (momentum, value, quality, low_vol, rsi2, volume):
      - Win rate: % of trades where the factor's contribution was
        positive and the trade was profitable
      - Average return when factor was the dominant driver
      - Sharpe ratio of returns attributed to the factor

    Returns:
        dict of factor_name → performance metrics
    """
    from predictions.models import get_closed_trades

    closed = get_closed_trades(limit=500)
    if not closed:
        return {"message": "No closed trades to analyze", "factors": {}}

    factor_names = ["momentum", "value", "quality", "low_vol", "rsi2", "volume"]
    factor_perf = {f: {"wins": 0, "losses": 0, "returns": [], "contributions": []}
                   for f in factor_names}

    for trade in closed:
        pnl_pct = trade.get("pnl_pct", 0) or 0
        is_win = pnl_pct > 0

        # Parse factors_used JSON
        try:
            factors = json.loads(trade.get("factors_used", "{}") or "{}")
        except Exception:
            factors = {}

        if not factors:
            continue

        for factor_name in factor_names:
            factor_data = factors.get(factor_name, {})
            contribution = factor_data.get("contribution", 0)

            factor_perf[factor_name]["contributions"].append(contribution)
            factor_perf[factor_name]["returns"].append(pnl_pct)

            if is_win:
                factor_perf[factor_name]["wins"] += 1
            else:
                factor_perf[factor_name]["losses"] += 1

    # Calculate metrics
    results = {}
    for factor_name, data in factor_perf.items():
        total = data["wins"] + data["losses"]
        if total == 0:
            results[factor_name] = {
                "total_trades": 0,
                "win_rate": 0,
                "avg_return": 0,
                "sharpe": 0,
            }
            continue

        returns = data["returns"]
        win_rate = data["wins"] / total * 100
        avg_return = float(np.mean(returns))
        std_return = float(np.std(returns)) if len(returns) > 1 else 1

        # Sharpe (annualized assuming ~20 trades per year)
        sharpe = (avg_return / (std_return + 1e-10)) * np.sqrt(20)

        results[factor_name] = {
            "total_trades": total,
            "win_rate": round(win_rate, 1),
            "avg_return": round(avg_return, 2),
            "sharpe": round(sharpe, 2),
            "avg_contribution": round(float(np.mean(data["contributions"])), 4)
                if data["contributions"] else 0,
        }

    return {
        "factors": results,
        "total_trades_analyzed": len(closed),
        "timestamp": datetime.now().isoformat(),
    }


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


def auto_adjust_weights() -> dict:
    """
    Auto-adjust factor weights based on actual performance.

    Algorithm:
      1. Calculate Sharpe ratio for each factor
      2. Factors with higher Sharpe get more weight
      3. Weights shift by WEIGHT_ADJUSTMENT_RATE (15%) per cycle
      4. Enforce MIN_WEIGHT (5%) and MAX_WEIGHT (40%) bounds
      5. Normalize to sum = 1.0
      6. Only run after MIN_TRADES_FOR_UPDATE (20) trades

    This is the core of the self-learning system.
    """
    from predictions.models import get_signal_weights, update_signal_weight, get_closed_trades

    closed = get_closed_trades(limit=500)
    if len(closed) < MIN_TRADES_FOR_UPDATE:
        return {
            "updated": False,
            "reason": f"Need {MIN_TRADES_FOR_UPDATE} trades, have {len(closed)}",
        }

    # Get current weights
    current_weights = get_signal_weights()
    factor_perf = analyze_factor_performance()

    if not factor_perf.get("factors"):
        return {"updated": False, "reason": "No factor data available"}

    # Calculate target weights based on Sharpe ratios
    factors = factor_perf["factors"]
    sharpes = {}
    for name in current_weights:
        if name in factors and factors[name]["total_trades"] >= 5:
            sharpes[name] = max(0, factors[name]["sharpe"])  # Floor at 0
        else:
            sharpes[name] = 0.5  # Default for unknown factors

    # Normalize Sharpe to get target weight allocation
    total_sharpe = sum(sharpes.values()) + 1e-10
    target_weights = {name: sharpe / total_sharpe for name, sharpe in sharpes.items()}

    # Blend current weights toward target weights (gradual adjustment)
    new_weights = {}
    for name in current_weights:
        current = current_weights[name]
        target = target_weights.get(name, current)
        # Move WEIGHT_ADJUSTMENT_RATE toward target
        new = current + WEIGHT_ADJUSTMENT_RATE * (target - current)
        # Enforce bounds
        new = max(MIN_WEIGHT, min(MAX_WEIGHT, new))
        new_weights[name] = new

    # Normalize to sum = 1.0
    total = sum(new_weights.values())
    new_weights = {k: round(v / total, 4) for k, v in new_weights.items()}

    # Save updated weights
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

    return {
        "updated": True,
        "previous_weights": current_weights,
        "new_weights": new_weights,
        "sharpe_scores": {k: round(v, 2) for k, v in sharpes.items()},
        "trades_analyzed": len(closed),
        "timestamp": datetime.now().isoformat(),
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
        entry = t.get("entry_time")
        exit_t = t.get("exit_time")
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

    Returns:
        dict with sector_penalties, regime_direction_blocks, confidence_cap
    """
    mistakes = analyze_mistakes()
    adjustments = {
        "sector_penalties": {},      # sector -> confidence penalty
        "blocked_combos": [],        # ["BEAR_LONG", etc.] — combos to avoid
        "confidence_cap": 95,        # max confidence (lower if overconfident)
        "tighten_stops": False,      # if True, use tighter stop-loss
    }

    patterns = mistakes.get("mistake_patterns", {})

    # Penalize weak sectors
    for sector in patterns.get("weak_sectors", []):
        adjustments["sector_penalties"][sector] = -10  # -10% confidence for bad sectors

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
    report["total_closed_trades"] = len(closed)

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

    return report
