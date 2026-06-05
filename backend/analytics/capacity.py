"""
Capacity Engine — ADV scaling, liquidity score, slippage.

Tells the system how big it can size positions before slippage eats
the alpha.
"""
import math
from typing import Optional
from .nan_helpers import safe_float, safe_div, clamp


def adv_scaled_slippage_bps(order_dollars: float, adv_dollars: float,
                              base_bps: float = 5.0) -> float:
    """
    Slippage scales as square-root of participation rate.

    slip_bps = base_bps + 10 * sqrt(participation_rate * 100)

    Where participation = order_dollars / ADV.

    Math intuition: 5% ADV order ≈ 7bps extra slippage.
                    50% ADV order ≈ 23bps extra (large).
    """
    base = safe_float(base_bps, 5.0)
    if adv_dollars <= 0:
        return base
    participation = order_dollars / adv_dollars
    if participation <= 0:
        return base
    extra = 10 * math.sqrt(participation * 100)
    return base + extra


def liquidity_score(order_dollars: float, adv_dollars: float) -> dict:
    """
    Liquidity score: how feasible is this trade?

    GREEN  (< 2% ADV)   — no impact
    YELLOW (2-5% ADV)   — some impact, OK
    ORANGE (5-10% ADV)  — large, slippage real
    RED    (> 10% ADV)  — too large, skip or split

    Returns:
        {participation_pct, status, slippage_bps_est}
    """
    if adv_dollars <= 0:
        return {"participation_pct": None, "status": "UNKNOWN",
                "slippage_bps_est": None}
    pct = order_dollars / adv_dollars * 100
    bps = adv_scaled_slippage_bps(order_dollars, adv_dollars)
    if pct < 2:
        status = "GREEN"
    elif pct < 5:
        status = "YELLOW"
    elif pct < 10:
        status = "ORANGE"
    else:
        status = "RED"
    return {
        "participation_pct": round(pct, 2),
        "status": status,
        "slippage_bps_est": round(bps, 1),
    }


def capacity_ceiling(adv_dollars: float, max_participation_pct: float = 5.0) -> float:
    """
    Max single-trade size before liquidity flags.

    Returns dollars.
    """
    return safe_float(adv_dollars, 0) * (max_participation_pct / 100.0)


def strategy_capacity_estimate(avg_adv_dollars: float, n_picks: int,
                                 max_participation_pct: float = 5.0) -> dict:
    """
    Estimate total strategy AUM ceiling.

    capacity = N * avg_ADV * max_participation
    With overhead for diversification across many positions.

    Returns rough AUM ceiling.
    """
    if avg_adv_dollars <= 0 or n_picks <= 0:
        return {"capacity_dollars": None}
    per_trade_cap = avg_adv_dollars * (max_participation_pct / 100.0)
    total_cap = per_trade_cap * n_picks
    # Apply 0.7 overhead factor for cross-correlation
    realistic_cap = total_cap * 0.7
    return {
        "per_trade_capacity_dollars": round(per_trade_cap, 0),
        "raw_total_capacity": round(total_cap, 0),
        "realistic_aum_ceiling_dollars": round(realistic_cap, 0),
    }


def vwap_execution_estimate(order_dollars: float, adv_dollars: float,
                              hours_remaining: float = 6.5) -> dict:
    """
    Estimate VWAP execution outcome.

    For a 6.5-hour market day, VWAP execution spreads the order over the day.
    Larger orders → more spread → more risk → more cost.

    Returns:
        {participation_pct, est_completion_pct, est_slippage_bps}
    """
    if adv_dollars <= 0:
        return {"error": "no ADV data"}
    full_day_participation = order_dollars / adv_dollars
    hourly_target = order_dollars / hours_remaining
    completion_pct = min(100, (hourly_target / (adv_dollars / 6.5)) * 100)
    slip = adv_scaled_slippage_bps(order_dollars, adv_dollars) * 0.7  # VWAP improves
    return {
        "full_day_participation_pct": round(full_day_participation * 100, 2),
        "estimated_completion_pct": round(completion_pct, 1),
        "estimated_vwap_slippage_bps": round(slip, 1),
    }


def implementation_shortfall(decision_price: float, executed_price: float,
                               shares: int, direction: str = "long") -> dict:
    """
    Implementation Shortfall (IS) = (decision_price - executed_price) * shares
    for longs.

    Decomposes:
        - Delay cost (decision to first execution)
        - Trading cost (during execution)
        - Opportunity cost (unfilled portion)
    """
    decision = safe_float(decision_price, 0)
    executed = safe_float(executed_price, 0)
    if decision == 0 or executed == 0:
        return {"error": "missing prices"}
    sign = 1 if direction.lower() == "long" else -1
    shortfall_dollars = (decision - executed) * sign * shares
    shortfall_bps = (executed - decision) / decision * 10000 * sign
    return {
        "decision_price": decision,
        "executed_price": executed,
        "shortfall_dollars": round(shortfall_dollars, 2),
        "shortfall_bps": round(shortfall_bps, 1),
        "adverse_to_us": shortfall_dollars < 0,
    }


def spread_aware_size(bid: float, ask: float, target_dollars: float,
                       max_spread_bps: float = 50) -> dict:
    """
    Refuse or reduce sizing based on bid-ask spread.

    If spread > max_spread_bps of mid-price, skip the trade entirely
    (spread eats the alpha).
    """
    bid_f = safe_float(bid, 0)
    ask_f = safe_float(ask, 0)
    if bid_f <= 0 or ask_f <= 0 or ask_f < bid_f:
        return {"sizing_dollars": 0, "skip": True, "reason": "invalid quotes"}
    mid = (bid_f + ask_f) / 2
    spread_bps = (ask_f - bid_f) / mid * 10000
    if spread_bps > max_spread_bps:
        return {
            "spread_bps": round(spread_bps, 1),
            "sizing_dollars": 0,
            "skip": True,
            "reason": f"spread {spread_bps:.1f}bps > {max_spread_bps}bps limit",
        }
    return {
        "spread_bps": round(spread_bps, 1),
        "sizing_dollars": target_dollars,
        "skip": False,
        "reason": "within spread tolerance",
    }
