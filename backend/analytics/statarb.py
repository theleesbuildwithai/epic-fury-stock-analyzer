"""
Statistical Arbitrage — pairs cointegration, mean reversion, Hurst.
"""
import math
from typing import Optional
from .nan_helpers import safe_float, safe_div, percentile


def hurst_exponent(prices: list) -> Optional[float]:
    """
    Hurst exponent: classifies time series as trending vs mean-reverting.

    H < 0.5: mean-reverting (stat arb opportunity)
    H = 0.5: random walk
    H > 0.5: trending (momentum)

    Computed via rescaled range (R/S) analysis.
    """
    if not prices or len(prices) < 100:
        return None
    log_returns = []
    for i in range(1, len(prices)):
        if prices[i-1] > 0:
            log_returns.append(math.log(prices[i] / prices[i-1]))
    if len(log_returns) < 50:
        return None
    # Compute R/S for multiple sub-period lengths
    lengths = [10, 20, 40, 80] if len(log_returns) >= 80 else [10, 20]
    log_n = []
    log_rs = []
    for n in lengths:
        rss = []
        for start in range(0, len(log_returns) - n, n):
            slice_ = log_returns[start:start + n]
            mean = sum(slice_) / n
            adj = [r - mean for r in slice_]
            cum = [sum(adj[:i+1]) for i in range(n)]
            r = max(cum) - min(cum)
            var = sum((x - mean) ** 2 for x in slice_) / n
            s = math.sqrt(var)
            if s > 0:
                rss.append(r / s)
        if rss:
            log_n.append(math.log(n))
            log_rs.append(math.log(sum(rss) / len(rss)))
    if len(log_n) < 2:
        return None
    # Linear regression slope = Hurst exponent
    n_pts = len(log_n)
    mean_x = sum(log_n) / n_pts
    mean_y = sum(log_rs) / n_pts
    num = sum((log_n[i] - mean_x) * (log_rs[i] - mean_y) for i in range(n_pts))
    den = sum((log_n[i] - mean_x) ** 2 for i in range(n_pts))
    return safe_div(num, den, None)


def mean_reversion_halflife(prices: list) -> Optional[float]:
    """
    Half-life of mean reversion via AR(1) on price deviations.

    Math:
        Δp_t = κ*(μ - p_t) + ε
        half_life = ln(2) / κ

    Returns half-life in periods (days).
    """
    if not prices or len(prices) < 20:
        return None
    mean_p = sum(prices) / len(prices)
    deviations = [p - mean_p for p in prices]
    # Lag the series for AR(1)
    n = len(deviations) - 1
    y = deviations[1:]  # next
    x = deviations[:-1]  # current
    # OLS slope: β = Σxy / Σx²
    sum_xy = sum(x[i] * y[i] for i in range(n))
    sum_xx = sum(xi ** 2 for xi in x)
    beta = safe_div(sum_xy, sum_xx, None)
    if beta is None or beta >= 1 or beta <= -1:
        return None
    # κ = -ln(β) for stable mean reversion (β ∈ (0, 1))
    if beta <= 0:
        return None
    try:
        kappa = -math.log(beta)
        if kappa <= 0:
            return None
        return math.log(2) / kappa
    except (ValueError, ZeroDivisionError):
        return None


# === Pairs cointegration ===

def engle_granger_cointegration(series_a: list, series_b: list) -> dict:
    """
    Engle-Granger two-step cointegration test.

    1. Regress a on b: a = α + β*b + residuals
    2. Test residuals for stationarity (ADF approximation)

    Returns:
        {
            beta: hedge ratio (for trading α-β*B as pair),
            mean_residual, std_residual,
            current_z_score: how far residuals are from mean (in std),
            adf_approx: rough stationarity indicator
        }
    """
    if not series_a or not series_b or len(series_a) != len(series_b):
        return {"error": "length mismatch"}
    n = len(series_a)
    if n < 60:
        return {"error": "insufficient data"}
    mean_a = sum(series_a) / n
    mean_b = sum(series_b) / n
    # OLS regression
    sum_xy = sum((series_b[i] - mean_b) * (series_a[i] - mean_a) for i in range(n))
    sum_xx = sum((series_b[i] - mean_b) ** 2 for i in range(n))
    beta = safe_div(sum_xy, sum_xx, None)
    if beta is None:
        return {"error": "regression failed"}
    alpha = mean_a - beta * mean_b
    residuals = [series_a[i] - alpha - beta * series_b[i] for i in range(n)]
    mean_res = sum(residuals) / n
    std_res = math.sqrt(sum((r - mean_res) ** 2 for r in residuals) / (n - 1))
    current_z = (residuals[-1] - mean_res) / std_res if std_res > 0 else 0
    # Simplified ADF: lag-1 autocorrelation of residuals
    lag_res = residuals[:-1]
    next_res = residuals[1:]
    mean_lag = sum(lag_res) / len(lag_res)
    mean_next = sum(next_res) / len(next_res)
    autocorr_num = sum((lag_res[i] - mean_lag) * (next_res[i] - mean_next) for i in range(len(lag_res)))
    autocorr_den_a = sum((r - mean_lag) ** 2 for r in lag_res)
    autocorr_den_b = sum((r - mean_next) ** 2 for r in next_res)
    autocorr = safe_div(autocorr_num, math.sqrt(autocorr_den_a * autocorr_den_b), 1.0)
    # If autocorr near 1 = random walk, near 0 = stationary
    is_cointegrated = autocorr < 0.85
    return {
        "alpha": round(alpha, 4),
        "beta": round(beta, 4),
        "mean_residual": round(mean_res, 4),
        "std_residual": round(std_res, 4),
        "current_z_score": round(current_z, 3),
        "residual_autocorrelation": round(autocorr, 3),
        "is_cointegrated": is_cointegrated,
        "trade_signal": (
            "ENTER_LONG_A_SHORT_B" if current_z < -2 else
            "ENTER_SHORT_A_LONG_B" if current_z > 2 else
            "EXIT_PAIR" if abs(current_z) < 0.5 else
            "HOLD"
        ),
    }


def find_cointegrated_pairs(price_series_dict: dict, min_corr: float = 0.7) -> list:
    """
    Scan all pairs for cointegration. Returns pairs that pass.

    Args:
        price_series_dict: {ticker: [prices]}
        min_corr: prefilter to skip clearly uncorrelated pairs

    Returns:
        list of {ticker_a, ticker_b, beta, current_z_score, ...}
    """
    tickers = list(price_series_dict.keys())
    results = []
    for i, ta in enumerate(tickers):
        for tb in tickers[i+1:]:
            sa = price_series_dict[ta]
            sb = price_series_dict[tb]
            if len(sa) != len(sb) or len(sa) < 60:
                continue
            # Quick correlation prefilter
            n = len(sa)
            mean_a = sum(sa) / n
            mean_b = sum(sb) / n
            cov = sum((sa[i] - mean_a) * (sb[i] - mean_b) for i in range(n))
            var_a = sum((x - mean_a) ** 2 for x in sa)
            var_b = sum((x - mean_b) ** 2 for x in sb)
            corr = safe_div(cov, math.sqrt(var_a * var_b), 0)
            if abs(corr) < min_corr:
                continue
            result = engle_granger_cointegration(sa, sb)
            if result.get("is_cointegrated"):
                result["ticker_a"] = ta
                result["ticker_b"] = tb
                result["correlation"] = round(corr, 3)
                results.append(result)
    return sorted(results, key=lambda x: abs(x.get("current_z_score", 0)), reverse=True)


# === Autocorrelation testing ===

def lag_n_autocorrelation(series: list, lag: int = 1) -> Optional[float]:
    """
    Autocorrelation at given lag. Useful for detecting non-randomness.
    """
    if not series or len(series) < lag + 10:
        return None
    n = len(series)
    lag_a = series[:n - lag]
    lag_b = series[lag:]
    mean = sum(series) / n
    num = sum((lag_a[i] - mean) * (lag_b[i] - mean) for i in range(len(lag_a)))
    den = sum((x - mean) ** 2 for x in series)
    return safe_div(num, den, None)
