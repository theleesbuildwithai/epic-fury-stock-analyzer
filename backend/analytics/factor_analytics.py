"""
Factor Analytics — per-factor IC, Sharpe, half-life, attribution.

The core engine for "which factors are working, which aren't."
Outputs UPGRADE/HOLD/DOWNGRADE/KILL verdicts that the Phase-3
auto-upgrade loop consumes to mutate factor weights.
"""
import math
import json
from typing import Optional
from .nan_helpers import safe_float, safe_div, scrub_nan
from .risk_engine import TRADING_DAYS_PER_YEAR, sharpe_ratio


# === Information Coefficient ===

def spearman_rank_correlation(x: list, y: list) -> Optional[float]:
    """
    Spearman ρ = Pearson on rank-transformed data.
    Range [-1, 1]. Robust to outliers.
    Returns None if <10 obs or zero variance.
    """
    if not x or not y or len(x) != len(y) or len(x) < 10:
        return None
    # Convert to ranks (avg rank for ties)
    def rank(values):
        sorted_idx = sorted(range(len(values)), key=lambda i: values[i])
        ranks = [0.0] * len(values)
        for r, idx in enumerate(sorted_idx):
            ranks[idx] = r + 1
        return ranks
    rx = rank(x)
    ry = rank(y)
    n = len(rx)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = math.sqrt(sum((r - mx) ** 2 for r in rx))
    dy = math.sqrt(sum((r - my) ** 2 for r in ry))
    return safe_div(num, dx * dy, None)


def information_coefficient(factor_signals: list, forward_returns: list) -> Optional[float]:
    """
    IC = Spearman rank correlation between factor signal and forward return.
    Standard quant convention.

    Args:
        factor_signals: list of factor signal values at trade entry
        forward_returns: list of N-day forward returns matching same trades

    Returns:
        IC in [-1, 1]. Positive = factor predicts up; negative = anti-predicts.
        Production-grade alpha: IC > 0.05 sustained.
    """
    return spearman_rank_correlation(factor_signals, forward_returns)


def ic_half_life(ic_series: list) -> Optional[float]:
    """
    Half-life of IC signal via AR(1) decay.

    Math:
        Fit IC_t = ρ * IC_{t-1} + ε
        half_life = ln(2) / -ln(ρ)   (only meaningful if 0 < ρ < 1)

    Returns half-life in periods (days), or None if no mean reversion.
    """
    if not ic_series or len(ic_series) < 10:
        return None
    # AR(1) coefficient via OLS: lag y[t-1] on y[t]
    n = len(ic_series) - 1
    if n < 2:
        return None
    y = ic_series[1:]
    x = ic_series[:-1]
    mx = sum(x) / n
    my = sum(y) / n
    num = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    den = sum((x[i] - mx) ** 2 for i in range(n))
    rho = safe_div(num, den, None)
    if rho is None or rho <= 0 or rho >= 1:
        return None
    try:
        return math.log(2) / -math.log(rho)
    except (ValueError, ZeroDivisionError):
        return None


# === Per-factor stats ===

def compute_per_factor_stats(closed_trades: list, factor_name: str,
                              forward_window: int = 5) -> dict:
    """
    Compute IC, Sharpe, hit_rate, cumulative P&L, turnover for one factor.

    closed_trades: list of trade dicts with:
        - factors_used: JSON string with factor contributions
        - pnl_pct, pnl_dollars: trade outcome
        - direction: long/short
        - exit_date

    Returns dict with all per-factor metrics.
    """
    signals = []
    fwd_returns = []
    factor_pnls = []
    hits = 0
    material_trades = 0
    for t in closed_trades:
        try:
            factors_raw = t.get("factors_used") or "{}"
            factors = json.loads(factors_raw) if isinstance(factors_raw, str) else factors_raw
            if not isinstance(factors, dict):
                continue
            fdata = factors.get(factor_name, {})
            contribution = safe_float(
                fdata.get("contribution") if isinstance(fdata, dict) else fdata,
                0.0,
            )
            if abs(contribution) < 0.01:  # immaterial
                continue
            material_trades += 1
            pnl = safe_float(t.get("pnl_pct"), 0)
            direction = str(t.get("direction", "long")).lower()
            dir_sign = 1.0 if direction == "long" else -1.0
            contrib_sign = 1.0 if contribution >= 0 else -1.0
            # Aligned PnL: positive iff factor predicted direction correctly
            aligned = contrib_sign * dir_sign * pnl
            signals.append(contribution)
            fwd_returns.append(pnl / 100.0)  # to decimal for IC
            factor_pnls.append(aligned)
            if aligned > 0:
                hits += 1
        except Exception:
            continue
    if material_trades == 0:
        return {
            "factor": factor_name,
            "total_trades": 0,
            "ic_spearman": None,
            "sharpe": None,
            "hit_rate_pct": None,
            "cum_pnl_attributed": 0.0,
            "avg_pnl_per_trade": 0.0,
            "evidence_strength": "WEAK",
        }
    ic = spearman_rank_correlation(signals, fwd_returns)
    factor_returns_decimal = [p / 100.0 for p in factor_pnls]
    sh = sharpe_ratio(factor_returns_decimal)
    hit_rate = hits / material_trades * 100
    cum_pnl = sum(factor_pnls)
    avg_pnl = cum_pnl / material_trades
    if material_trades >= 100:
        evidence = "STRONG"
    elif material_trades >= 30:
        evidence = "MODERATE"
    else:
        evidence = "WEAK"
    return {
        "factor": factor_name,
        "total_trades": material_trades,
        "ic_spearman": safe_float(ic),
        "sharpe": safe_float(sh),
        "hit_rate_pct": round(hit_rate, 1),
        "cum_pnl_attributed": round(cum_pnl, 2),
        "avg_pnl_per_trade": round(avg_pnl, 2),
        "evidence_strength": evidence,
    }


# === Verdict logic ===

def factor_verdict(stats: dict) -> dict:
    """
    UPGRADE / HOLD / DOWNGRADE / KILL decision.

    Inputs the output of compute_per_factor_stats.
    Returns dict with verdict + proposed_weight_multiplier.
    """
    obs = stats.get("total_trades", 0)
    ic = stats.get("ic_spearman")
    sh = stats.get("sharpe")
    if obs < 30 or ic is None or sh is None:
        return {"verdict": "HOLD", "weight_multiplier": 1.0, "rationale": "insufficient data"}
    if ic > 0.03 and sh > 0.5:
        return {"verdict": "UPGRADE", "weight_multiplier": 1.30, "rationale": f"IC={ic:.3f}, Sharpe={sh:.2f}"}
    if ic > 0 and sh > 0:
        return {"verdict": "HOLD", "weight_multiplier": 1.0, "rationale": "marginally positive"}
    if obs > 90 and ic < -0.02 and sh < -1:
        return {"verdict": "KILL", "weight_multiplier": 0.0, "rationale": f"IC={ic:.3f}, Sharpe={sh:.2f}, {obs} obs"}
    if ic < 0 or sh < -0.5:
        return {"verdict": "DOWNGRADE", "weight_multiplier": 0.70, "rationale": f"IC={ic:.3f}, Sharpe={sh:.2f}"}
    return {"verdict": "HOLD", "weight_multiplier": 1.0, "rationale": "neutral"}


# === Regime-conditioned per-factor stats ===

def per_factor_regime_split(closed_trades: list, factor_name: str) -> dict:
    """
    Sharpe per factor per regime (BULL / BEAR / SIDEWAYS).

    closed_trades must have regime_at_entry field.
    """
    by_regime = {"BULL": [], "BEAR": [], "SIDEWAYS": []}
    for t in closed_trades:
        try:
            regime = (t.get("regime_at_entry") or t.get("regime") or "").upper()
            if "BULL" in regime:
                rk = "BULL"
            elif "BEAR" in regime:
                rk = "BEAR"
            elif regime in ("SIDEWAYS", "NEUTRAL"):
                rk = "SIDEWAYS"
            else:
                continue
            factors_raw = t.get("factors_used") or "{}"
            factors = json.loads(factors_raw) if isinstance(factors_raw, str) else factors_raw
            if not isinstance(factors, dict):
                continue
            fdata = factors.get(factor_name, {})
            contribution = safe_float(
                fdata.get("contribution") if isinstance(fdata, dict) else fdata, 0.0,
            )
            if abs(contribution) < 0.01:
                continue
            pnl = safe_float(t.get("pnl_pct"), 0)
            direction = str(t.get("direction", "long")).lower()
            dir_sign = 1.0 if direction == "long" else -1.0
            contrib_sign = 1.0 if contribution >= 0 else -1.0
            aligned = contrib_sign * dir_sign * pnl / 100.0
            by_regime[rk].append(aligned)
        except Exception:
            continue
    result = {}
    for rk, vals in by_regime.items():
        if len(vals) >= 10:
            result[rk] = safe_float(sharpe_ratio(vals))
        else:
            result[rk] = None
    return result


# === Factor correlation matrix ===

def factor_correlation_matrix(closed_trades: list, factor_names: list) -> dict:
    """
    Spearman correlation between factors based on their contributions
    across all trades. Finds collinear factors (|ρ| > 0.7).

    Returns {labels: [...], matrix: [[N×N]]}.
    """
    # Build contribution series per factor
    series = {f: [] for f in factor_names}
    for t in closed_trades:
        try:
            factors_raw = t.get("factors_used") or "{}"
            factors = json.loads(factors_raw) if isinstance(factors_raw, str) else factors_raw
            if not isinstance(factors, dict):
                continue
            for f in factor_names:
                fdata = factors.get(f, {})
                c = safe_float(
                    fdata.get("contribution") if isinstance(fdata, dict) else fdata, 0.0,
                )
                series[f].append(c)
        except Exception:
            continue
    matrix = []
    for f1 in factor_names:
        row = []
        for f2 in factor_names:
            if f1 == f2:
                row.append(1.0)
            else:
                rho = spearman_rank_correlation(series[f1], series[f2])
                row.append(safe_float(rho, 0.0))
        matrix.append(row)
    return {"labels": factor_names, "matrix": matrix}


# === Top-level analytics build ===

def build_factor_analytics(closed_trades: list, factor_names: list,
                            current_weights: dict) -> dict:
    """
    Top-level orchestrator. Returns the full factor_analytics payload
    for /api/factor-analytics.
    """
    out = []
    for fname in factor_names:
        stats = compute_per_factor_stats(closed_trades, fname)
        verdict = factor_verdict(stats)
        regime_split = per_factor_regime_split(closed_trades, fname)
        current = safe_float(current_weights.get(fname), 0.0)
        proposed = current * verdict["weight_multiplier"]
        # Calculate IC half-life if we had time series data (Sprint 1 stub)
        ic_hl = None  # Will be populated when IC time series is stored daily
        out.append({
            "factor": fname,
            "cum_pnl_attributed_dollars": stats["cum_pnl_attributed"],
            "ic_60d_spearman": stats["ic_spearman"],
            "ic_half_life_days": ic_hl,
            "sharpe_60d_annualized": stats["sharpe"],
            "hit_rate_60d_pct": stats["hit_rate_pct"],
            "total_trades": stats["total_trades"],
            "avg_pnl_per_trade": stats["avg_pnl_per_trade"],
            "regime_split": regime_split,
            "verdict": verdict["verdict"],
            "verdict_rationale": verdict["rationale"],
            "current_weight": round(current, 4),
            "proposed_weight": round(proposed, 4),
            "evidence_strength": stats["evidence_strength"],
        })
    # Sort by IC descending
    out.sort(key=lambda x: (x["ic_60d_spearman"] or -999), reverse=True)
    return scrub_nan(out)
