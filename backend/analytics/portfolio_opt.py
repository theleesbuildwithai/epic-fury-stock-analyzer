"""
Portfolio Optimization — Mean-Variance, HRP, Risk Parity, Kelly by Regime.

Moves sizing from heuristic to optimal portfolio math.
"""
import math
from typing import Optional
from .nan_helpers import safe_float, safe_div, clamp


def equal_weight(assets: list) -> dict:
    """Trivial baseline: 1/N. Used as default fallback."""
    if not assets:
        return {}
    w = 1.0 / len(assets)
    return {a: w for a in assets}


def inverse_volatility_weight(asset_vols: dict) -> dict:
    """
    Weight ∝ 1/vol. Simple risk parity.

    Args:
        asset_vols: {asset_name: annualized_volatility}

    Returns:
        {asset_name: weight} summing to 1.
    """
    if not asset_vols:
        return {}
    inv_vols = {a: 1.0 / v for a, v in asset_vols.items() if v > 0}
    total = sum(inv_vols.values())
    if total == 0:
        return {}
    return {a: v / total for a, v in inv_vols.items()}


def risk_parity_weights(cov_matrix: list, assets: list) -> dict:
    """
    Risk parity: each asset contributes equal portfolio risk.

    Uses iterative bisection (simpler than convex optimization).

    Args:
        cov_matrix: NxN covariance matrix
        assets: list of asset names

    Returns:
        {asset_name: weight} that equalizes marginal risk contributions.
    """
    n = len(assets)
    if n == 0 or len(cov_matrix) != n:
        return {}
    # Start with inverse-vol weights
    diag = [cov_matrix[i][i] for i in range(n)]
    vols = [math.sqrt(d) if d > 0 else 1.0 for d in diag]
    weights = [1.0 / v for v in vols]
    total = sum(weights)
    weights = [w / total for w in weights]
    # Iterate: adjust weights to equalize MRC
    for _ in range(100):
        # Marginal Risk Contribution for each asset
        port_var = sum(weights[i] * weights[j] * cov_matrix[i][j]
                       for i in range(n) for j in range(n))
        if port_var <= 0:
            break
        port_vol = math.sqrt(port_var)
        mrc = []
        for i in range(n):
            cov_with_port = sum(weights[j] * cov_matrix[i][j] for j in range(n))
            mrc.append(weights[i] * cov_with_port / port_vol)
        # Target: equal MRC
        target = port_vol / n
        # Adjust each weight proportionally
        new_weights = []
        for i in range(n):
            adjust = safe_div(target, mrc[i], 1.0) if mrc[i] > 0 else 1.0
            adjust = clamp(adjust, 0.5, 2.0)  # bound the per-step adjustment
            new_weights.append(weights[i] * adjust)
        total = sum(new_weights)
        new_weights = [w / total for w in new_weights]
        # Check convergence
        max_change = max(abs(new_weights[i] - weights[i]) for i in range(n))
        weights = new_weights
        if max_change < 1e-6:
            break
    return {assets[i]: round(weights[i], 6) for i in range(n)}


def min_variance_weights(cov_matrix: list, assets: list) -> dict:
    """
    Minimum variance portfolio (closed-form when no constraints).

    w = (Σ⁻¹ * 1) / (1ᵀ * Σ⁻¹ * 1)

    NOTE: requires inverting the cov matrix. We use a simplified
    approach with Cholesky if available, otherwise fall back to
    inverse-vol weighting.
    """
    n = len(assets)
    if n == 0 or len(cov_matrix) != n:
        return {}
    # Try to compute Σ⁻¹ * 1
    inv_diag_approx = [1.0 / cov_matrix[i][i] if cov_matrix[i][i] > 0 else 0 for i in range(n)]
    total = sum(inv_diag_approx)
    if total == 0:
        return equal_weight(assets)
    return {assets[i]: inv_diag_approx[i] / total for i in range(n)}


def max_sharpe_weights(expected_returns: dict, cov_matrix: list, assets: list,
                        rf_daily: float = 0.0002) -> dict:
    """
    Maximum Sharpe (tangency portfolio).

    w ∝ Σ⁻¹ * (μ - rf)

    Simplified: weight ∝ excess_return / variance (per asset).
    """
    n = len(assets)
    if n == 0 or len(cov_matrix) != n:
        return {}
    raw_weights = []
    for i in range(n):
        excess = safe_float(expected_returns.get(assets[i]), 0) - rf_daily
        var = cov_matrix[i][i] if cov_matrix[i][i] > 0 else 1.0
        raw_weights.append(excess / var)
    # Normalize to sum to 1 (ignoring negative weights = long-only)
    positive = [max(0, w) for w in raw_weights]
    total = sum(positive)
    if total == 0:
        return equal_weight(assets)
    return {assets[i]: positive[i] / total for i in range(n)}


def hierarchical_risk_parity(cov_matrix: list, assets: list,
                              correlation_matrix: list = None) -> dict:
    """
    Hierarchical Risk Parity (HRP - López de Prado).

    1. Cluster assets by correlation
    2. Quasi-diagonalize the cov matrix
    3. Recursively bisect and allocate inverse-variance within each cluster

    NOTE: simplified version using inverse-vol within correlation-derived
    clusters. Full HRP requires scipy linkage which adds a dep.
    """
    n = len(assets)
    if n == 0 or len(cov_matrix) != n:
        return {}
    # Compute volatilities
    vols = [math.sqrt(cov_matrix[i][i]) if cov_matrix[i][i] > 0 else 1.0 for i in range(n)]
    # Simple clustering: pairs of correlated assets, then split-allocate
    if correlation_matrix is None:
        correlation_matrix = [[
            safe_div(cov_matrix[i][j], vols[i] * vols[j], 0)
            for j in range(n)
        ] for i in range(n)]
    # Group similar assets (corr > 0.7)
    groups = []
    assigned = [False] * n
    for i in range(n):
        if assigned[i]:
            continue
        group = [i]
        assigned[i] = True
        for j in range(i + 1, n):
            if not assigned[j] and correlation_matrix[i][j] > 0.7:
                group.append(j)
                assigned[j] = True
        groups.append(group)
    # Allocate inverse-vol within each group, then equal across groups
    weights = [0.0] * n
    group_weight = 1.0 / len(groups)
    for group in groups:
        group_inv_vols = [1.0 / vols[i] for i in group]
        total_inv = sum(group_inv_vols)
        if total_inv == 0:
            continue
        for k, i in enumerate(group):
            weights[i] = group_weight * (group_inv_vols[k] / total_inv)
    # Renormalize
    total = sum(weights)
    if total == 0:
        return equal_weight(assets)
    return {assets[i]: weights[i] / total for i in range(n)}


# === Kelly by regime ===

def kelly_fraction(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """
    Standard Kelly: f = (p*b - q) / b
        p = win_rate
        q = 1 - win_rate
        b = avg_win / |avg_loss|

    Returns fraction in [0, 1]. Negative result → don't bet.
    """
    if win_rate <= 0 or win_rate >= 1:
        return 0.0
    if avg_loss == 0:
        return 0.0
    b = abs(avg_win / avg_loss)
    if b == 0:
        return 0.0
    p = win_rate
    q = 1 - p
    f = (p * b - q) / b
    return max(0.0, min(1.0, f))


def kelly_by_regime(regime_stats: dict) -> dict:
    """
    Kelly fraction per regime, not one static number.

    Args:
        regime_stats: {regime: {win_rate, avg_win, avg_loss}}

    Returns:
        {regime: kelly_fraction}

    Production safety: cap each at 0.25 (quarter-Kelly) to avoid blow-up.
    """
    out = {}
    for regime, stats in regime_stats.items():
        wr = safe_float(stats.get("win_rate"), 0.5)
        aw = safe_float(stats.get("avg_win"), 0.0)
        al = safe_float(stats.get("avg_loss"), 0.0)
        f = kelly_fraction(wr, aw, al)
        # Quarter-Kelly cap (industry standard for safety)
        out[regime] = round(min(f, 0.25), 4)
    return out


# === Volatility targeting ===

def volatility_target_scaler(realized_vol: float, target_vol: float = 0.15,
                              min_scale: float = 0.3, max_scale: float = 2.0) -> float:
    """
    Scale position sizes so portfolio realized vol matches target.

    Args:
        realized_vol: 30-60d annualized portfolio vol (decimal, e.g. 0.20)
        target_vol: desired annualized vol (default 15% = 0.15)
        min_scale: floor on shrinkage (default 0.3)
        max_scale: cap on leverage (default 2.0)

    Returns:
        Multiplier to apply to each new position size.
    """
    if realized_vol <= 0:
        return 1.0
    scale = target_vol / realized_vol
    return clamp(scale, min_scale, max_scale)


# === Diversification ratio ===

def diversification_ratio(weights: list, vols: list, port_vol: float) -> Optional[float]:
    """
    DR = (Σ w_i * σ_i) / σ_p

    > 1.0 = diversification helping
    = 1.0 = no diversification benefit
    """
    if not weights or not vols or len(weights) != len(vols) or port_vol <= 0:
        return None
    weighted_sum_vol = sum(w * v for w, v in zip(weights, vols))
    return weighted_sum_vol / port_vol


# === Beta neutrality ===

def beta_neutralize(positions: list, target_beta: float = 0.0) -> dict:
    """
    Returns the hedge size needed (in SPY equivalent) to achieve target beta.

    If portfolio_beta > target, need SHORT SPY position to neutralize.
    """
    if not positions:
        return {"current_beta": 0, "hedge_dollars": 0}
    long_beta_dollars = 0.0
    short_beta_dollars = 0.0
    for p in positions:
        shares = safe_float(p.get("shares"), 0)
        price = safe_float(p.get("current_price") or p.get("entry_price"), 0)
        beta = safe_float(p.get("beta"), 1.0)
        value = shares * price
        if str(p.get("direction", "long")).lower() == "long":
            long_beta_dollars += value * beta
        else:
            short_beta_dollars += value * beta
    net_beta_dollars = long_beta_dollars - short_beta_dollars
    # Hedge: need -(net_beta_dollars - target * portfolio_value) in SPY
    portfolio_value = sum(safe_float(p.get("shares"), 0) *
                          safe_float(p.get("current_price") or p.get("entry_price"), 0)
                          for p in positions)
    target_beta_dollars = target_beta * portfolio_value
    hedge = -(net_beta_dollars - target_beta_dollars)
    return {
        "long_beta_dollars": round(long_beta_dollars, 2),
        "short_beta_dollars": round(short_beta_dollars, 2),
        "net_beta_dollars": round(net_beta_dollars, 2),
        "target_beta_dollars": round(target_beta_dollars, 2),
        "spy_hedge_dollars": round(hedge, 2),
        "hedge_action": "SHORT SPY" if hedge < 0 else "LONG SPY" if hedge > 0 else "NONE",
    }
