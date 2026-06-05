"""
Risk Engine — VaR, ES, Beta, Exposure, Concentration.

All metrics computed defensively. Annualization factors:
- Trading days per year: 252
- VaR/ES report 1-day horizon
- Sharpe annualized by sqrt(252)
"""
import math
from typing import Optional
from .nan_helpers import safe_float, safe_div, percentile, scrub_nan

TRADING_DAYS_PER_YEAR = 252
RISK_FREE_RATE_ANNUAL = 0.05  # 5% — adjust to current 3-month T-bill
RISK_FREE_DAILY = RISK_FREE_RATE_ANNUAL / TRADING_DAYS_PER_YEAR


def var_historical(returns: list, confidence: float = 0.95) -> Optional[float]:
    """
    Historical VaR: empirical p-th percentile of returns.

    Args:
        returns: list of daily returns (decimal, e.g. -0.02 = -2%)
        confidence: e.g. 0.95 for VaR_95, 0.99 for VaR_99

    Returns:
        VaR as positive percentage loss. None if insufficient data.

    Math:
        VaR_95 = -percentile(returns, 5%)   (5th percentile is worst 5%)
    """
    if not returns or len(returns) < 20:
        return None
    p = (1 - confidence) * 100  # 5% for 95% VaR
    pct = percentile(returns, p)
    if pct is None:
        return None
    return -pct  # negative return → positive loss


def var_parametric(returns: list, confidence: float = 0.95) -> Optional[float]:
    """
    Parametric VaR: μ - z*σ assuming normality.

    Args:
        returns: list of daily returns
        confidence: e.g. 0.95 for VaR_95

    Returns:
        VaR as positive percentage loss.

    Math:
        VaR_95 = -(μ - 1.645 * σ)  where 1.645 = inverse normal CDF at 5%
        VaR_99 = -(μ - 2.326 * σ)
    """
    if not returns or len(returns) < 20:
        return None
    n = len(returns)
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(variance)
    # Z-scores for common confidences
    z_map = {0.90: 1.282, 0.95: 1.645, 0.99: 2.326, 0.999: 3.090}
    z = z_map.get(round(confidence, 3), 1.645)
    return -(mean - z * std)


def expected_shortfall(returns: list, confidence: float = 0.95) -> Optional[float]:
    """
    Expected Shortfall (CVaR): mean of returns below VaR.

    Args:
        returns: list of daily returns
        confidence: e.g. 0.95 for ES_95

    Returns:
        ES as positive percentage loss (avg loss in tail).

    Math:
        ES_95 = -E[r | r <= VaR_95]  i.e. mean of worst 5%
    """
    if not returns or len(returns) < 20:
        return None
    p = (1 - confidence) * 100
    threshold = percentile(returns, p)
    if threshold is None:
        return None
    tail = [r for r in returns if r <= threshold]
    if not tail:
        return None
    return -(sum(tail) / len(tail))


def beta_to_market(portfolio_returns: list, market_returns: list) -> Optional[float]:
    """
    Beta = Cov(p, m) / Var(m).

    Args:
        portfolio_returns: portfolio daily returns
        market_returns: market (e.g. SPY) daily returns, SAME length

    Returns:
        Beta. 1.0 = matches market; >1 = more volatile; <1 = less.

    Math:
        β = Σ((p_i - p̄)(m_i - m̄)) / Σ((m_i - m̄)²)
    """
    if not portfolio_returns or not market_returns:
        return None
    n = min(len(portfolio_returns), len(market_returns))
    if n < 20:
        return None
    p = portfolio_returns[:n]
    m = market_returns[:n]
    p_mean = sum(p) / n
    m_mean = sum(m) / n
    cov = sum((p[i] - p_mean) * (m[i] - m_mean) for i in range(n)) / (n - 1)
    var_m = sum((m[i] - m_mean) ** 2 for i in range(n)) / (n - 1)
    return safe_div(cov, var_m, None)


def gross_exposure(positions: list, nav: float) -> dict:
    """
    Gross / Net / Beta-adjusted exposure.

    positions: list of dicts with {direction, shares, current_price, beta}
    nav: total portfolio NAV

    Returns dict with:
        long_dollars, short_dollars, gross_dollars, net_dollars,
        gross_pct_nav, net_pct_nav, beta_adjusted_pct_nav
    """
    long_d = 0.0
    short_d = 0.0
    beta_long_d = 0.0
    beta_short_d = 0.0
    for p in positions:
        try:
            shares = safe_float(p.get("shares"), 0)
            price = safe_float(p.get("current_price") or p.get("entry_price"), 0)
            direction = str(p.get("direction", "long")).lower()
            beta = safe_float(p.get("beta"), 1.0)
            value = shares * price
            if direction == "long":
                long_d += value
                beta_long_d += value * beta
            else:
                short_d += value
                beta_short_d += value * beta
        except Exception:
            continue
    gross = long_d + short_d
    net = long_d - short_d
    beta_adj = beta_long_d - beta_short_d
    return {
        "long_dollars": round(long_d, 2),
        "short_dollars": round(short_d, 2),
        "gross_dollars": round(gross, 2),
        "net_dollars": round(net, 2),
        "gross_pct_nav": round(safe_div(gross, nav, 0) * 100, 2),
        "net_pct_nav": round(safe_div(net, nav, 0) * 100, 2),
        "beta_adjusted_dollars": round(beta_adj, 2),
        "beta_adjusted_pct_nav": round(safe_div(beta_adj, nav, 0) * 100, 2),
    }


def concentration_hhi(position_weights: list) -> float:
    """
    Herfindahl-Hirschman Index: Σ(weight²).

    weights: list of absolute position weights (sum to 1.0 ideally)

    Returns:
        HHI in [0, 1]. 1.0 = fully concentrated; 1/N = perfectly diversified.

    Interpretation:
        < 0.1 = diversified
        0.1-0.2 = moderate concentration
        > 0.2 = high concentration
    """
    if not position_weights:
        return 0.0
    total = sum(abs(w) for w in position_weights)
    if total == 0:
        return 0.0
    normalized = [abs(w) / total for w in position_weights]
    return sum(w ** 2 for w in normalized)


def top_n_weight(position_weights: list, n: int = 5) -> float:
    """Sum of top-N largest absolute weights as % of gross."""
    if not position_weights:
        return 0.0
    abs_weights = sorted([abs(w) for w in position_weights], reverse=True)
    total = sum(abs_weights)
    if total == 0:
        return 0.0
    return sum(abs_weights[:n]) / total * 100


def sector_exposure(positions: list, nav: float) -> list:
    """
    Returns list of dicts per sector with long/short/gross/net/pct_nav.
    Marks-to-market.
    """
    by_sector = {}
    for p in positions:
        sec = (p.get("sector") or "Unknown").strip() or "Unknown"
        direction = str(p.get("direction", "long")).lower()
        shares = safe_float(p.get("shares"), 0)
        price = safe_float(p.get("current_price") or p.get("entry_price"), 0)
        value = shares * price
        s = by_sector.setdefault(sec, {"long": 0.0, "short": 0.0})
        if direction == "long":
            s["long"] += value
        else:
            s["short"] += value
    result = []
    for sec, vals in by_sector.items():
        gross = vals["long"] + vals["short"]
        net = vals["long"] - vals["short"]
        result.append({
            "sector": sec,
            "long_dollars": round(vals["long"], 2),
            "short_dollars": round(vals["short"], 2),
            "gross_dollars": round(gross, 2),
            "net_dollars": round(net, 2),
            "pct_nav": round(safe_div(gross, nav, 0) * 100, 2),
        })
    result.sort(key=lambda x: x["gross_dollars"], reverse=True)
    return result


# === Portfolio-level performance ratios ===

def sharpe_ratio(returns: list, rf_daily: float = RISK_FREE_DAILY) -> Optional[float]:
    """Annualized Sharpe = (mean(excess) / std(excess)) * sqrt(252)."""
    if not returns or len(returns) < 20:
        return None
    excess = [r - rf_daily for r in returns]
    n = len(excess)
    mean = sum(excess) / n
    var = sum((r - mean) ** 2 for r in excess) / (n - 1)
    std = math.sqrt(var)
    if std == 0:
        return None
    sharpe = (mean / std) * math.sqrt(TRADING_DAYS_PER_YEAR)
    # Clamp to ±10 — anything more is measurement artifact
    return max(-10.0, min(10.0, sharpe))


def sortino_ratio(returns: list, rf_daily: float = RISK_FREE_DAILY) -> Optional[float]:
    """
    Annualized Sortino = mean(excess) / downside_dev * sqrt(252)
    Penalizes downside vol only.
    """
    if not returns or len(returns) < 20:
        return None
    excess = [r - rf_daily for r in returns]
    n = len(excess)
    mean = sum(excess) / n
    downside = [min(0, r) for r in excess]
    dn_var = sum(d ** 2 for d in downside) / n
    dn_std = math.sqrt(dn_var)
    if dn_std == 0:
        return None
    sortino = (mean / dn_std) * math.sqrt(TRADING_DAYS_PER_YEAR)
    return max(-10.0, min(10.0, sortino))


def calmar_ratio(returns: list) -> Optional[float]:
    """
    Calmar = annualized return / max drawdown.
    Higher = better risk-adjusted return.
    """
    if not returns or len(returns) < 20:
        return None
    ann_ret = (1 + sum(returns) / len(returns)) ** TRADING_DAYS_PER_YEAR - 1
    mdd = max_drawdown(returns)
    if mdd is None or mdd == 0:
        return None
    return safe_div(ann_ret, mdd, None)


def omega_ratio(returns: list, threshold: float = 0.0) -> Optional[float]:
    """
    Omega = Σ(returns > threshold) / |Σ(returns < threshold)|.
    No assumption of normality.
    """
    if not returns:
        return None
    upside = sum(max(0, r - threshold) for r in returns)
    downside = sum(max(0, threshold - r) for r in returns)
    if downside == 0:
        return None
    return safe_div(upside, downside, None)


def ulcer_index(prices: list) -> Optional[float]:
    """
    Ulcer Index: penalizes drawdown depth AND duration.
    Lower = smoother equity curve.

    Math:
        UI = sqrt(mean( ((peak - price) / peak * 100)² ))
    """
    if not prices or len(prices) < 2:
        return None
    peak = prices[0]
    drawdowns = []
    for p in prices:
        if p > peak:
            peak = p
        dd_pct = (peak - p) / peak * 100 if peak > 0 else 0
        drawdowns.append(dd_pct ** 2)
    mean_sq = sum(drawdowns) / len(drawdowns)
    return math.sqrt(mean_sq)


def max_drawdown(returns: list) -> Optional[float]:
    """
    Max drawdown as positive percentage.
    Returns: list of daily returns (decimal).
    """
    if not returns:
        return None
    cum = 1.0
    peak = 1.0
    mdd = 0.0
    for r in returns:
        cum *= (1 + r)
        if cum > peak:
            peak = cum
        dd = (peak - cum) / peak
        if dd > mdd:
            mdd = dd
    return mdd  # as decimal, e.g. 0.15 = 15%


def drawdown_duration(returns: list) -> dict:
    """
    Current drawdown depth + days since last peak.

    Defensive: clamp current_drawdown to [0, 60]. Anything beyond is a
    data artifact (a paper portfolio with stop losses cannot lose 60%+
    in a 60-day window). Returns 0 for empty input.
    """
    if not returns:
        return {"current_drawdown_pct": 0.0, "days_since_peak": 0}
    cum = 1.0
    peak = 1.0
    peak_idx = 0
    for i, r in enumerate(returns):
        cum *= (1 + r)
        if cum > peak:
            peak = cum
            peak_idx = i
    current_dd = (peak - cum) / peak * 100 if peak > 0 else 0
    # Sanity clamp
    if current_dd < 0:
        current_dd = 0.0
    if current_dd > 60:
        # Likely data artifact — return 0 rather than mislead the brake
        current_dd = 0.0
    return {
        "current_drawdown_pct": round(current_dd, 2),
        "days_since_peak": len(returns) - peak_idx - 1,
    }


def underwater_curve(returns: list) -> list:
    """
    Returns list of {day, drawdown_pct} showing drawdown over time.
    Each point = how far below running peak we are.
    """
    if not returns:
        return []
    series = []
    cum = 1.0
    peak = 1.0
    for i, r in enumerate(returns):
        cum *= (1 + r)
        if cum > peak:
            peak = cum
        dd = (peak - cum) / peak * 100 if peak > 0 else 0
        series.append({"day": i, "drawdown_pct": round(-dd, 4)})  # negative for chart
    return series


# === Higher moments ===

def realized_volatility(returns: list, annualize: bool = True) -> Optional[float]:
    """Annualized standard deviation of returns."""
    if not returns or len(returns) < 5:
        return None
    n = len(returns)
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(var)
    if annualize:
        std *= math.sqrt(TRADING_DAYS_PER_YEAR)
    return std


def vol_of_vol(returns: list, window: int = 20) -> Optional[float]:
    """
    Vol-of-vol = stdev of rolling realized vol.
    Increasing vol-of-vol = regime instability.
    """
    if not returns or len(returns) < window * 2:
        return None
    vols = []
    for i in range(window, len(returns) + 1):
        slice_ = returns[i - window:i]
        vol = realized_volatility(slice_, annualize=False)
        if vol is not None:
            vols.append(vol)
    if len(vols) < 2:
        return None
    mean = sum(vols) / len(vols)
    var = sum((v - mean) ** 2 for v in vols) / (len(vols) - 1)
    return math.sqrt(var)


def kurtosis(returns: list) -> Optional[float]:
    """
    Excess kurtosis (κ - 3). Positive = fat tails.
    Normal distribution = 0.
    """
    if not returns or len(returns) < 4:
        return None
    n = len(returns)
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / n
    if var == 0:
        return None
    std = math.sqrt(var)
    m4 = sum((r - mean) ** 4 for r in returns) / n
    return (m4 / std ** 4) - 3


# === Snapshot helper ===

def portfolio_risk_snapshot(returns: list, positions: list, nav: float,
                            market_returns: list = None) -> dict:
    """
    Full risk snapshot for /api/factor-analytics endpoint.

    returns: portfolio daily returns
    positions: list of position dicts
    nav: current NAV
    market_returns: SPY returns same length as portfolio_returns
    """
    var_95_h = var_historical(returns, 0.95)
    var_95_p = var_parametric(returns, 0.95)
    var_99_h = var_historical(returns, 0.99)
    es_95 = expected_shortfall(returns, 0.95)
    es_99 = expected_shortfall(returns, 0.99)
    beta = beta_to_market(returns, market_returns) if market_returns else None
    exposure = gross_exposure(positions, nav)
    # Concentration weights
    abs_values = [abs(safe_float(p.get("shares"), 0) *
                      safe_float(p.get("current_price") or p.get("entry_price"), 0))
                  for p in positions]
    hhi = concentration_hhi(abs_values)
    top5 = top_n_weight(abs_values, 5)
    # Performance ratios
    sharpe = sharpe_ratio(returns)
    sortino = sortino_ratio(returns)
    calmar = calmar_ratio(returns)
    omega = omega_ratio(returns)
    mdd = max_drawdown(returns)
    dd_dur = drawdown_duration(returns)
    vol = realized_volatility(returns)
    vov = vol_of_vol(returns)
    kurt = kurtosis(returns)
    # Defensive output bounds — if the input series has anomalies that
    # slipped past filtering, cap the displayed metrics at sane levels
    # so the UI doesn't show "your portfolio could lose 76% tomorrow"
    # when reality is "the math hit garbage data."
    def _bound(v, lo, hi):
        if v is None: return None
        try:
            f = float(v)
            if f < lo or f > hi: return None
            return f
        except (TypeError, ValueError):
            return None

    # Single-day VaR can't reasonably exceed 20% for an equity portfolio.
    # Anything more = bad data; return None and the UI will show "N/A".
    var95h_pct = _bound(var_95_h * 100 if var_95_h else None, 0, 20)
    var95p_pct = _bound(var_95_p * 100 if var_95_p else None, 0, 20)
    var99h_pct = _bound(var_99_h * 100 if var_99_h else None, 0, 30)
    es95_pct  = _bound(es_95 * 100 if es_95 else None, 0, 30)
    es99_pct  = _bound(es_99 * 100 if es_99 else None, 0, 40)
    # Max DD can't reasonably exceed 60% for a paper portfolio with
    # stop losses — anything more = bad data.
    mdd_pct = _bound(mdd * 100 if mdd else None, 0, 60)
    # Sharpe / Sortino already clamped to ±10 inside ratio functions;
    # this is belt-and-suspenders.
    sh = _bound(sharpe, -10, 10)
    so = _bound(sortino, -10, 10)
    return scrub_nan({
        "var_95_historical_pct": safe_float(var95h_pct),
        "var_95_parametric_pct": safe_float(var95p_pct),
        "var_99_historical_pct": safe_float(var99h_pct),
        "es_95_pct": safe_float(es95_pct),
        "es_99_pct": safe_float(es99_pct),
        "beta_to_spy_60d": safe_float(_bound(beta, -3, 3)),
        "max_drawdown_pct": safe_float(mdd_pct),
        "current_drawdown_pct": dd_dur["current_drawdown_pct"],
        "days_since_peak": dd_dur["days_since_peak"],
        "sharpe_annualized": safe_float(sh),
        "sortino_annualized": safe_float(so),
        "calmar": safe_float(_bound(calmar, -50, 50)),
        "omega": safe_float(_bound(omega, 0, 50)),
        "realized_vol_annualized": safe_float(_bound(vol, 0, 5)),
        "vol_of_vol": safe_float(_bound(vov, 0, 5)),
        "kurtosis_excess": safe_float(_bound(kurt, -10, 50)),
        "concentration_hhi": round(hhi, 4),
        "top5_weight_pct": round(top5, 2),
        "exposure": exposure,
        "sector_exposure": sector_exposure(positions, nav),
        "underwater_curve_30d": underwater_curve(returns[-30:]) if returns else [],
        "n_returns_used": len(returns),  # transparency for diagnostics
    })
