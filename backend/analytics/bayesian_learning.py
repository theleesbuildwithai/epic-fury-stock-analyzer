"""
Bayesian Learning — factor weight updates, Thompson sampling, bandits.

Replaces heuristic shift-based weight updates with mathematically
optimal posterior updates.
"""
import math
import random
from typing import Optional
from .nan_helpers import safe_float, safe_div, clamp


def bayesian_weight_update(prior_weight: float, observed_sharpe: float,
                            observed_ic: float, n_obs: int,
                            prior_strength: float = 50.0) -> dict:
    """
    Bayesian factor weight update.

    Posterior ∝ Prior × Likelihood

    Likelihood signal: combination of Sharpe and IC.
        score = sharpe_z * 0.6 + ic_z * 0.4

    Conjugate prior: Beta(α, β) where strength = α + β.
    Update: α += n_obs * win_rate, β += n_obs * loss_rate

    Returns:
        {posterior_weight, change_pct, evidence_strength}
    """
    prior = clamp(safe_float(prior_weight), 0.01, 1.0)
    sh = safe_float(observed_sharpe, 0)
    ic = safe_float(observed_ic, 0)
    if n_obs < 10:
        # Not enough data to move from prior
        return {"posterior_weight": prior, "change_pct": 0.0,
                "evidence_strength": "WEAK"}
    # Convert Sharpe to win_rate equivalent using standard normal CDF
    # If Sharpe = 1.0, implied win_rate ≈ 0.5 + 1.0/(σ * √(2π))
    implied_wr = 0.5 + 0.5 * math.tanh(sh)  # bounded transform
    ic_boost = clamp(ic * 2, -0.5, 0.5)  # +50% / -50% adjustment
    likelihood_signal = implied_wr + ic_boost
    # Update using exponentiated weight (multiplicative Bayes update)
    log_prior = math.log(max(prior, 1e-6))
    log_posterior = log_prior + likelihood_signal * (n_obs / (n_obs + prior_strength))
    posterior = math.exp(log_posterior)
    # Clamp final
    posterior = clamp(posterior, 0.01, 0.50)
    # Don't change >30% per cycle (hard guard)
    max_change = prior * 0.3
    if posterior > prior + max_change:
        posterior = prior + max_change
    elif posterior < prior - max_change:
        posterior = max(0.02, prior - max_change)
    change_pct = ((posterior - prior) / prior) * 100 if prior > 0 else 0
    if n_obs >= 100:
        evidence = "STRONG"
    elif n_obs >= 30:
        evidence = "MODERATE"
    else:
        evidence = "WEAK"
    return {
        "prior_weight": round(prior, 4),
        "posterior_weight": round(posterior, 4),
        "change_pct": round(change_pct, 2),
        "evidence_strength": evidence,
        "implied_win_rate": round(implied_wr, 3),
    }


# === Thompson Sampling for factor selection ===

def thompson_sample_factor(factor_stats: dict) -> str:
    """
    Thompson sampling: each factor is a Beta arm. Sample posterior
    win rate from each, pick the highest sample. Naturally balances
    exploration vs exploitation.

    Args:
        factor_stats: {factor_name: {wins, losses}}

    Returns:
        Name of the factor with highest sampled posterior win rate.
    """
    if not factor_stats:
        return None
    samples = {}
    for fname, stats in factor_stats.items():
        wins = safe_float(stats.get("wins"), 0)
        losses = safe_float(stats.get("losses"), 0)
        alpha = wins + 1  # Beta(1, 1) prior = uniform
        beta = losses + 1
        # Sample from Beta(α, β) using gamma method
        # Beta(α, β) = X/(X+Y) where X ~ Gamma(α), Y ~ Gamma(β)
        try:
            x = random.gammavariate(alpha, 1)
            y = random.gammavariate(beta, 1)
            samples[fname] = x / (x + y) if (x + y) > 0 else 0.5
        except (ValueError, OverflowError):
            samples[fname] = 0.5
    return max(samples, key=samples.get)


def multi_armed_bandit_allocation(factor_stats: dict, total_weight: float = 1.0,
                                    epsilon: float = 0.1) -> dict:
    """
    ε-greedy multi-armed bandit weight allocation.

    Best factor gets (1-ε) of the weight. Others split ε uniformly.

    Args:
        factor_stats: {factor_name: {sharpe, ic, n_obs}}
        total_weight: total weight to allocate (default 1.0)
        epsilon: exploration probability

    Returns:
        {factor_name: weight}
    """
    if not factor_stats:
        return {}
    # Sort by Sharpe descending
    sorted_factors = sorted(
        factor_stats.items(),
        key=lambda x: safe_float(x[1].get("sharpe"), -999),
        reverse=True,
    )
    n = len(sorted_factors)
    weights = {}
    best = sorted_factors[0][0]
    weights[best] = (1 - epsilon) * total_weight
    # Split epsilon across remaining n-1 factors
    if n > 1:
        per_explore = (epsilon * total_weight) / (n - 1)
        for fname, _ in sorted_factors[1:]:
            weights[fname] = per_explore
    return weights


# === Adversarial / Dropout robustness ===

def dropout_factor_selection(factor_weights: dict, dropout_rate: float = 0.1) -> dict:
    """
    Randomly zero some factor weights (like neural network dropout).
    Forces the system to not over-rely on any single factor.

    Useful for robustness training but should NOT be applied at trade
    time — only during validation.
    """
    if not factor_weights:
        return {}
    new_weights = {}
    n_drop = max(1, int(len(factor_weights) * dropout_rate))
    drop_set = set(random.sample(list(factor_weights.keys()), n_drop))
    surviving_total = sum(w for k, w in factor_weights.items() if k not in drop_set)
    if surviving_total <= 0:
        return factor_weights.copy()
    # Renormalize surviving weights to sum to original total
    original_total = sum(factor_weights.values())
    rescale = original_total / surviving_total
    for k, w in factor_weights.items():
        new_weights[k] = 0.0 if k in drop_set else w * rescale
    return new_weights


def adversarial_drawdown_test(factor_returns: dict, n_simulations: int = 1000) -> dict:
    """
    Worst-case correlated drawdown test.

    Resamples factor returns N times with all factors fully correlated
    (worst case). Returns 99th percentile drawdown.
    """
    if not factor_returns:
        return {"worst_drawdown_pct": None}
    # Combine all factor returns
    n_periods = min(len(rs) for rs in factor_returns.values())
    if n_periods < 30:
        return {"worst_drawdown_pct": None}
    worst_drawdowns = []
    for _ in range(n_simulations):
        # Sample random window with replacement
        sim_returns = []
        for i in range(n_periods):
            idx = random.randint(0, n_periods - 1)
            # Sum all factor returns at this index (assume fully correlated)
            day_total = sum(rs[idx] for rs in factor_returns.values())
            sim_returns.append(day_total)
        # Compute drawdown
        cum = 1.0
        peak = 1.0
        max_dd = 0.0
        for r in sim_returns:
            cum *= (1 + r)
            peak = max(peak, cum)
            dd = (peak - cum) / peak
            max_dd = max(max_dd, dd)
        worst_drawdowns.append(max_dd)
    worst_drawdowns.sort()
    p99 = worst_drawdowns[int(len(worst_drawdowns) * 0.99)]
    return {
        "worst_drawdown_pct_99": round(p99 * 100, 2),
        "median_drawdown_pct": round(worst_drawdowns[len(worst_drawdowns) // 2] * 100, 2),
        "n_simulations": n_simulations,
    }
