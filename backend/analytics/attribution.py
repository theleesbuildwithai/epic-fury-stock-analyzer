"""
Performance Attribution — Brinson, Factor, Sector.

Decomposes portfolio P&L into:
- Allocation effect (which sectors we were overweight/underweight)
- Selection effect (which stocks within each sector we picked)
- Interaction effect (allocation × selection)
"""
import math
from typing import Optional
from .nan_helpers import safe_float, safe_div


def brinson_attribution(portfolio_weights: dict, portfolio_returns: dict,
                          benchmark_weights: dict, benchmark_returns: dict) -> dict:
    """
    Brinson-Hood-Beebower (1986) attribution.

    Decomposes excess return into:
        Allocation = Σ (wp_i - wb_i) * rb_i
        Selection  = Σ wb_i * (rp_i - rb_i)
        Interaction = Σ (wp_i - wb_i) * (rp_i - rb_i)

    Args:
        portfolio_weights: {sector: weight}
        portfolio_returns: {sector: return_pct}
        benchmark_weights: {sector: weight}
        benchmark_returns: {sector: return_pct}

    Returns:
        {allocation_effect, selection_effect, interaction_effect, total_excess}
    """
    sectors = set(portfolio_weights.keys()) | set(benchmark_weights.keys())
    alloc = 0.0
    select = 0.0
    interact = 0.0
    for sec in sectors:
        wp = safe_float(portfolio_weights.get(sec), 0)
        wb = safe_float(benchmark_weights.get(sec), 0)
        rp = safe_float(portfolio_returns.get(sec), 0)
        rb = safe_float(benchmark_returns.get(sec), 0)
        alloc += (wp - wb) * rb
        select += wb * (rp - rb)
        interact += (wp - wb) * (rp - rb)
    return {
        "allocation_effect_pct": round(alloc, 4),
        "selection_effect_pct": round(select, 4),
        "interaction_effect_pct": round(interact, 4),
        "total_excess_pct": round(alloc + select + interact, 4),
    }


def factor_pnl_attribution(closed_trades: list, factor_names: list) -> dict:
    """
    P&L attributed to each factor.

    For each closed trade, splits its P&L proportionally to factor contributions.

    Returns {factor_name: cumulative_attributed_pnl_dollars}.
    """
    import json
    attribution = {f: 0.0 for f in factor_names}
    for t in closed_trades:
        try:
            pnl = safe_float(t.get("pnl_dollars"), 0)
            if pnl == 0:
                continue
            factors_raw = t.get("factors_used") or "{}"
            factors = json.loads(factors_raw) if isinstance(factors_raw, str) else factors_raw
            if not isinstance(factors, dict):
                continue
            total_contrib = 0.0
            contribs = {}
            for f in factor_names:
                fdata = factors.get(f, {})
                c = abs(safe_float(
                    fdata.get("contribution") if isinstance(fdata, dict) else fdata, 0
                ))
                if c > 0:
                    contribs[f] = c
                    total_contrib += c
            if total_contrib == 0:
                continue
            for f, c in contribs.items():
                attribution[f] += pnl * (c / total_contrib)
        except Exception:
            continue
    return {f: round(v, 2) for f, v in attribution.items()}


def sector_attribution(closed_trades: list) -> dict:
    """
    P&L by sector. Includes win/loss counts.
    """
    sector_data = {}
    for t in closed_trades:
        sec = (t.get("sector") or "Unknown").strip() or "Unknown"
        if sec not in sector_data:
            sector_data[sec] = {"pnl": 0.0, "wins": 0, "losses": 0, "trades": 0}
        pnl = safe_float(t.get("pnl_dollars"), 0)
        sector_data[sec]["pnl"] += pnl
        sector_data[sec]["trades"] += 1
        if pnl > 0:
            sector_data[sec]["wins"] += 1
        elif pnl < 0:
            sector_data[sec]["losses"] += 1
    # Compute win rate per sector
    for sec, d in sector_data.items():
        d["win_rate_pct"] = (d["wins"] / d["trades"] * 100) if d["trades"] > 0 else 0
        d["pnl"] = round(d["pnl"], 2)
    return sector_data


# === TWR (Time-Weighted Return) ===

def time_weighted_return(daily_returns: list) -> Optional[float]:
    """
    TWR = product of (1 + r_i) - 1.

    Isolates portfolio performance from cash flows.
    Most common pro reporting standard.
    """
    if not daily_returns:
        return None
    cum = 1.0
    for r in daily_returns:
        cum *= (1 + r)
    return cum - 1


# === MWR (Money-Weighted Return / IRR) ===

def money_weighted_return(cash_flows: list, final_value: float,
                            periods: int) -> Optional[float]:
    """
    MWR / IRR = rate r such that NPV of all cash flows + final value = 0.

    Args:
        cash_flows: list of (period_index, amount) — deposits positive,
                    withdrawals negative
        final_value: portfolio value at end
        periods: total periods

    Uses Newton-Raphson iteration on Σ CF_i / (1+r)^t_i + FV / (1+r)^T = 0
    """
    if not cash_flows or periods <= 0:
        return None
    def npv(rate):
        total = -final_value / (1 + rate) ** periods
        for t, cf in cash_flows:
            total += cf / (1 + rate) ** t
        return total
    # Newton-Raphson
    r = 0.10  # initial guess 10%
    for _ in range(100):
        f = npv(r)
        f_prime = (npv(r + 0.0001) - f) / 0.0001
        if abs(f_prime) < 1e-10:
            break
        r_new = r - f / f_prime
        if abs(r_new - r) < 1e-7:
            return r_new
        r = max(-0.99, r_new)
    return r


# === Realized vs Unrealized P&L ===

def realized_vs_unrealized(closed_trades: list, open_positions: list) -> dict:
    """
    Split P&L into realized (closed) and unrealized (mark-to-market).
    """
    realized = sum(safe_float(t.get("pnl_dollars"), 0) for t in closed_trades)
    unrealized = 0.0
    for p in open_positions:
        try:
            entry = safe_float(p.get("entry_price"), 0)
            current = safe_float(p.get("current_price") or entry, entry)
            shares = safe_float(p.get("shares"), 0)
            direction = str(p.get("direction", "long")).lower()
            sign = 1.0 if direction == "long" else -1.0
            unrealized += sign * (current - entry) * shares
        except Exception:
            continue
    return {
        "realized_dollars": round(realized, 2),
        "unrealized_dollars": round(unrealized, 2),
        "total_dollars": round(realized + unrealized, 2),
    }


# === Wash sale detection ===

def detect_wash_sales(closed_trades: list) -> list:
    """
    Wash sale: IRS rule. Loss disallowed if same security bought within 30
    days before or after the loss-realizing trade.

    Returns list of trades that triggered wash sales.
    """
    from datetime import datetime
    wash_sales = []
    losses = [t for t in closed_trades
              if safe_float(t.get("pnl_dollars"), 0) < 0]
    for loss in losses:
        try:
            exit_date = datetime.fromisoformat(loss["exit_date"])
            ticker = loss["ticker"]
            # Look for purchase of same ticker within ±30 days
            for other in closed_trades:
                if other["id"] == loss["id"]:
                    continue
                if other["ticker"] != ticker:
                    continue
                if other["direction"] != loss["direction"]:
                    continue
                other_entry = datetime.fromisoformat(other["entry_date"])
                days = abs((other_entry - exit_date).days)
                if days <= 30:
                    wash_sales.append({
                        "ticker": ticker,
                        "loss_trade_id": loss["id"],
                        "loss_exit_date": loss["exit_date"],
                        "loss_amount": loss["pnl_dollars"],
                        "wash_buy_trade_id": other["id"],
                        "wash_buy_date": other["entry_date"],
                        "days_apart": days,
                    })
                    break
        except Exception:
            continue
    return wash_sales
