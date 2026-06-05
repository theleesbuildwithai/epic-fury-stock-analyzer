"""
Walk-Forward + Validation — deflated Sharpe, CPCV, stress tests.

THE most important quant validation. Distinguishes real edge from
in-sample fit. Without this, every "good" factor is fake until proven OOS.
"""
import math
from typing import Optional
from .nan_helpers import safe_float, safe_div
from .risk_engine import sharpe_ratio, TRADING_DAYS_PER_YEAR


def deflated_sharpe(observed_sharpe: float, n_trials: int, n_observations: int,
                     skewness: float = 0.0, kurtosis: float = 3.0) -> Optional[float]:
    """
    Deflated Sharpe Ratio (López de Prado 2014).

    Corrects observed Sharpe for:
        - Multiple testing bias (n_trials)
        - Sample size (n_observations)
        - Skewness and kurtosis of returns

    Math (simplified):
        DSR = SR_observed - expected_max_SR_under_null

        Where E[max SR | N trials] ≈ √(2 ln(N)) * σ_SR

        And σ_SR = √( (1 + 0.5*SR² - skew*SR + (kurt-3)/4*SR²) / (T - 1) )

    Returns DSR. If < 0, observed Sharpe is likely noise.
    """
    if n_trials < 1 or n_observations < 30:
        return None
    sr = safe_float(observed_sharpe, 0.0)
    skew = safe_float(skewness, 0.0)
    kurt = safe_float(kurtosis, 3.0)
    # Variance of Sharpe estimator
    sr_variance = (1 + 0.5 * sr * sr - skew * sr + (kurt - 3) / 4 * sr * sr) / (n_observations - 1)
    if sr_variance <= 0:
        return None
    sr_std = math.sqrt(sr_variance)
    # Expected max Sharpe under null with N trials (Boruvka 2018)
    expected_max = sr_std * math.sqrt(2 * math.log(max(n_trials, 2)))
    return sr - expected_max


def probabilistic_sharpe(observed_sharpe: float, benchmark_sharpe: float,
                          n_observations: int, skewness: float = 0.0,
                          kurtosis: float = 3.0) -> Optional[float]:
    """
    Probabilistic Sharpe Ratio (PSR): P(SR_true > SR_benchmark).

    Returns probability in [0, 1] that the true Sharpe exceeds benchmark.
    > 0.95 = statistically significant skill.
    """
    if n_observations < 30:
        return None
    sr = safe_float(observed_sharpe, 0.0)
    bench = safe_float(benchmark_sharpe, 0.0)
    skew = safe_float(skewness, 0.0)
    kurt = safe_float(kurtosis, 3.0)
    sr_variance = (1 + 0.5 * sr * sr - skew * sr + (kurt - 3) / 4 * sr * sr) / (n_observations - 1)
    if sr_variance <= 0:
        return None
    sr_std = math.sqrt(sr_variance)
    z = (sr - bench) / sr_std
    # CDF of standard normal using erf
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def bonferroni_correction(p_values: list, alpha: float = 0.05) -> dict:
    """
    Bonferroni multi-test correction. The naive way to control FWER.

    Adjusted significance threshold = alpha / N tests.
    Conservative — Holm-Bonferroni is better but more complex.
    """
    if not p_values:
        return {"adjusted_alpha": alpha, "significant": []}
    n = len(p_values)
    adjusted = alpha / n
    significant = [(i, p) for i, p in enumerate(p_values) if p < adjusted]
    return {
        "adjusted_alpha": adjusted,
        "n_tests": n,
        "significant": significant,
        "n_significant": len(significant),
    }


# === Walk-forward harness ===

def walkforward_validation(data: list, train_size: int, test_size: int,
                            strategy_fn, n_windows: int = None) -> dict:
    """
    Walk-forward validation with expanding window.

    Args:
        data: time-ordered list of observations
        train_size: initial training window size
        test_size: out-of-sample test window size
        strategy_fn: callable(train_data) -> predictor, callable(test_data) -> returns
        n_windows: how many forward steps (None = max possible)

    Returns:
        {
            "n_windows": int,
            "oos_sharpe_per_window": [...],
            "median_oos_sharpe": float,
            "consistency_ratio": float (frac of windows positive),
            "verdict": "ROBUST" | "MIXED" | "FAILED"
        }
    """
    if len(data) < train_size + test_size:
        return {"error": "insufficient data", "n_required": train_size + test_size}
    if n_windows is None:
        n_windows = (len(data) - train_size) // test_size
    n_windows = min(n_windows, (len(data) - train_size) // test_size)
    oos_sharpes = []
    for i in range(n_windows):
        train_end = train_size + i * test_size
        test_end = train_end + test_size
        if test_end > len(data):
            break
        train = data[:train_end]
        test = data[train_end:test_end]
        try:
            predictor = strategy_fn(train)
            test_returns = predictor(test) if predictor else []
            if test_returns and len(test_returns) >= 5:
                oos_sharpes.append(sharpe_ratio(test_returns))
        except Exception:
            continue
    if not oos_sharpes:
        return {"error": "no valid windows"}
    valid = [s for s in oos_sharpes if s is not None]
    if not valid:
        return {"error": "no sharpe computed"}
    median = sorted(valid)[len(valid) // 2]
    positive = sum(1 for s in valid if s > 0)
    consistency = positive / len(valid)
    if median > 0.5 and consistency > 0.6:
        verdict = "ROBUST"
    elif median > 0 and consistency > 0.5:
        verdict = "MIXED"
    else:
        verdict = "FAILED"
    return {
        "n_windows": len(valid),
        "oos_sharpe_per_window": [round(s, 3) for s in valid],
        "median_oos_sharpe": round(median, 3),
        "consistency_ratio": round(consistency, 3),
        "verdict": verdict,
    }


# === Stress test replay ===

CRISIS_PERIODS = {
    "covid_crash_2020": {
        "start": "2020-02-19", "end": "2020-03-23",
        "spy_drawdown_pct": -34.0,
        "vix_peak": 82.7,
    },
    "fed_pivot_2022": {
        "start": "2022-09-13", "end": "2022-10-13",
        "spy_drawdown_pct": -15.0,
        "vix_peak": 34.9,
    },
    "svb_2023": {
        "start": "2023-03-08", "end": "2023-03-13",
        "spy_drawdown_pct": -5.0,
        "vix_peak": 30.8,
    },
    "lehman_2008": {
        "start": "2008-09-12", "end": "2008-10-10",
        "spy_drawdown_pct": -25.0,
        "vix_peak": 76.9,
    },
    "aug_2024_yen_carry": {
        "start": "2024-08-01", "end": "2024-08-05",
        "spy_drawdown_pct": -8.0,
        "vix_peak": 65.7,
    },
}


def stress_test_portfolio(current_positions: list, market_drawdown_pct: float,
                            avg_beta: float = 1.0) -> dict:
    """
    Apply a hypothetical market drawdown to current portfolio.
    Simple linear: portfolio_pnl = beta * market_dd.

    Returns expected $loss and equity change. Field names are
    consistent across empty and populated portfolio cases so
    downstream consumers can rely on them.
    """
    if not current_positions:
        return {
            "scenario_market_dd_pct": market_drawdown_pct,
            "assumed_avg_beta": avg_beta,
            "expected_portfolio_loss_pct": 0.0,
            "expected_portfolio_loss_dollars": 0.0,
            "current_portfolio_value": 0.0,
        }
    total_value = sum(safe_float(p.get("shares"), 0) *
                       safe_float(p.get("current_price") or p.get("entry_price"), 0)
                       for p in current_positions)
    expected_pct = avg_beta * (market_drawdown_pct / 100.0)
    expected_dollars = total_value * expected_pct
    return {
        "scenario_market_dd_pct": market_drawdown_pct,
        "assumed_avg_beta": avg_beta,
        "expected_portfolio_loss_pct": round(expected_pct * 100, 2),
        "expected_portfolio_loss_dollars": round(expected_dollars, 2),
        "current_portfolio_value": round(total_value, 2),
    }


def replay_all_crises(current_positions: list, avg_beta: float = 1.0) -> dict:
    """
    Run all crisis scenarios against current portfolio.
    Returns dict of {crisis_name: stress_test_result}.
    """
    results = {}
    for name, params in CRISIS_PERIODS.items():
        results[name] = {
            **params,
            "stress_result": stress_test_portfolio(
                current_positions, params["spy_drawdown_pct"], avg_beta,
            ),
        }
    # Worst case
    worst = min(results.values(),
                key=lambda x: x["stress_result"]["expected_portfolio_loss_dollars"])
    return {
        "by_scenario": results,
        "worst_case_loss_dollars": worst["stress_result"]["expected_portfolio_loss_dollars"],
        "worst_case_scenario": [k for k, v in results.items()
                                 if v["stress_result"]["expected_portfolio_loss_dollars"] ==
                                 worst["stress_result"]["expected_portfolio_loss_dollars"]][0],
    }


# === Look-ahead bias detector ===

def check_lookahead_bias(feature_dates: list, signal_dates: list) -> dict:
    """
    Detect if features used to predict were computed AFTER the signal date.

    Returns dict with violations found.
    """
    violations = []
    for fd in feature_dates:
        for sd in signal_dates:
            if fd > sd:  # feature timestamp AFTER signal — leak
                violations.append({"feature_at": fd, "signal_at": sd})
    return {
        "n_violations": len(violations),
        "first_5_violations": violations[:5],
        "clean": len(violations) == 0,
    }
