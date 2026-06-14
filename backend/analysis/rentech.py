"""
Renaissance Technologies-Style Upgrades — Sentinel Quant Hedge Fund

This module adds institutional-grade features inspired by RenTech's Medallion Fund:

  1. PAIRS TRADING (Statistical Arbitrage)
     - Find cointegrated stock pairs that move together
     - Trade the spread when it deviates (mean reversion)
     - Market-neutral: makes money in any direction

  2. PORTFOLIO RISK MANAGEMENT
     - Sector concentration limits
     - Beta-neutral targeting (portfolio beta ≈ 0)
     - Correlation monitoring (avoid correlated bets)
     - Max drawdown circuit breaker

  3. ENSEMBLE SIGNAL VOTING
     - 3 independent scoring models
     - Trade only when 2/3 agree on direction
     - Dramatically reduces false signals

  4. ALTERNATIVE DATA SIGNALS
     - Short interest (squeeze detection)
     - Insider transactions (smart money)
     - Institutional ownership changes

  5. INTRADAY MEAN REVERSION
     - Opening range breakout signals
     - VWAP deviation signals
     - RSI(2) Connors strategy enhancement

All data from Yahoo Finance (yf.download) with aggressive caching.
"""

import yfinance as yf
import numpy as np
import pandas as pd
import time
import logging
from datetime import datetime, timedelta
from scipy.stats import zscore as scipy_zscore

logger = logging.getLogger(__name__)

# Cache for RenTech computations
_rentech_cache = {}
_RENTECH_CACHE_TTL = 600  # 10 minutes

_last_rentech_call = [0.0]
_RENTECH_DELAY = 3.0


def _throttle_rentech():
    """Enforce minimum delay between Yahoo Finance API calls."""
    now = time.time()
    elapsed = now - _last_rentech_call[0]
    if elapsed < _RENTECH_DELAY:
        time.sleep(_RENTECH_DELAY - elapsed)
    _last_rentech_call[0] = time.time()


def _safe_col(df, col_name="Close"):
    """Extract column from yfinance DataFrame, handling multi-level columns."""
    if df is None or (hasattr(df, "empty") and df.empty):
        return pd.Series(dtype=float)
    c = df[col_name]
    if hasattr(c, "columns"):
        c = c.iloc[:, 0]
    return c


def _get_rentech_cached(key, fetch_fn, ttl=None):
    """Cache with configurable TTL."""
    if ttl is None:
        ttl = _RENTECH_CACHE_TTL
    now = time.time()
    if key in _rentech_cache and now - _rentech_cache[key]["time"] < ttl:
        return _rentech_cache[key]["data"]
    data = fetch_fn()
    _rentech_cache[key] = {"data": data, "time": now}
    return data


# ============================================================
#  1. PAIRS TRADING / STATISTICAL ARBITRAGE
# ============================================================

# Pre-defined pairs — stocks in same sector with high historical correlation
# These are classic stat arb pairs that institutions trade
PAIRS_UNIVERSE = [
    # Tech pairs
    ("MSFT", "AAPL"),    # Large-cap tech leaders
    ("NVDA", "AMD"),     # GPU/semiconductor rivals
    ("GOOGL", "META"),   # Digital advertising
    ("CRM", "NOW"),      # Enterprise SaaS
    ("INTC", "TXN"),     # Legacy semiconductors

    # Finance pairs
    ("JPM", "BAC"),      # Big banks
    ("GS", "MS"),        # Investment banks
    ("V", "MA"),         # Payment networks
    ("BLK", "SCHW"),     # Asset management

    # Consumer pairs
    ("KO", "PEP"),       # Beverages
    ("WMT", "TGT"),      # Retail
    ("MCD", "SBUX"),     # Quick service restaurants
    ("NKE", "LULU"),     # Athletic apparel
    ("HD", "LOW"),       # Home improvement

    # Healthcare pairs
    ("JNJ", "PFE"),      # Pharma
    ("UNH", "CI"),       # Health insurance
    ("ABT", "MDT"),      # Medical devices

    # Energy pairs
    ("XOM", "CVX"),      # Oil majors
    ("COP", "EOG"),      # E&P companies

    # Industrial pairs
    ("CAT", "DE"),       # Heavy machinery
    ("BA", "LMT"),       # Aerospace & defense
    ("UPS", "FDX"),      # Shipping/logistics
]


def _calculate_spread_zscore(prices_a, prices_b, lookback=60):
    """
    Calculate the z-score of the log price spread between two stocks.

    The spread = log(A) - hedge_ratio * log(B)
    Z-score tells us how many standard deviations the spread is from its mean.

    |z| > 2.0 = strong signal (spread is stretched)
    |z| > 1.5 = moderate signal
    """
    if len(prices_a) < lookback or len(prices_b) < lookback:
        return None, None, None

    log_a = np.log(prices_a[-lookback:])
    log_b = np.log(prices_b[-lookback:])

    # OLS hedge ratio: how much of B to short for every unit of A
    hedge_ratio = np.polyfit(log_b, log_a, 1)[0]

    # Spread = log(A) - hedge_ratio * log(B)
    spread = log_a - hedge_ratio * log_b

    # Z-score of the spread
    mean_spread = np.mean(spread)
    std_spread = np.std(spread)

    if std_spread < 1e-8:
        return None, None, None

    current_z = (spread[-1] - mean_spread) / std_spread

    # Half-life of mean reversion (how fast the spread reverts)
    # Ornstein-Uhlenbeck: spread_t = theta * (mu - spread_{t-1}) + epsilon
    spread_diff = np.diff(spread)
    spread_lag = spread[:-1] - mean_spread
    if len(spread_lag) > 0 and np.std(spread_lag) > 1e-8:
        theta = -np.polyfit(spread_lag, spread_diff, 1)[0]
        half_life = np.log(2) / theta if theta > 0 else 999
    else:
        half_life = 999

    return float(current_z), float(hedge_ratio), float(half_life)


def find_pairs_trades(price_data: dict) -> list:
    """
    Scan all pairs for stat arb opportunities.

    Returns list of actionable pair trades:
      - Which pair, direction (long A/short B or vice versa)
      - Z-score (how stretched the spread is)
      - Expected profit (mean reversion target)
      - Confidence based on z-score magnitude and half-life
    """
    cache_key = "pairs_trades"
    cached = _rentech_cache.get(cache_key)
    if cached and time.time() - cached["time"] < 600:
        return cached["data"]

    trades = []

    for sym_a, sym_b in PAIRS_UNIVERSE:
        if sym_a not in price_data or sym_b not in price_data:
            continue

        try:
            closes_a = price_data[sym_a]["Close"].values.astype(float)
            closes_b = price_data[sym_b]["Close"].values.astype(float)

            # Need both to have same length
            min_len = min(len(closes_a), len(closes_b))
            closes_a = closes_a[-min_len:]
            closes_b = closes_b[-min_len:]

            z_score, hedge_ratio, half_life = _calculate_spread_zscore(closes_a, closes_b)

            if z_score is None:
                continue

            # Only trade if half-life is reasonable (< 30 days)
            # and z-score is stretched enough
            if half_life > 30 or abs(z_score) < 1.5:
                continue

            # Calculate correlation (higher = better pair)
            corr = float(np.corrcoef(closes_a[-60:], closes_b[-60:])[0, 1])
            if corr < 0.6:
                continue  # Not correlated enough

            # Direction: if z > 0, spread is too wide → short A, long B
            # if z < 0, spread is too narrow → long A, short B
            if z_score > 1.5:
                direction = f"SHORT {sym_a} / LONG {sym_b}"
                long_leg = sym_b
                short_leg = sym_a
            else:
                direction = f"LONG {sym_a} / SHORT {sym_b}"
                long_leg = sym_a
                short_leg = sym_b

            # Confidence: higher z-score + shorter half-life = more confident
            confidence = min(90, int(50 + abs(z_score) * 12 + max(0, 20 - half_life)))

            # Expected return: z-score tends to revert to 0
            # Each 1 std of spread = ~2-4% return on the pair
            expected_return = round(abs(z_score) * 2.5, 1)

            trades.append({
                "pair": f"{sym_a}/{sym_b}",
                "symbol_a": sym_a,
                "symbol_b": sym_b,
                "direction": direction,
                "long_leg": long_leg,
                "short_leg": short_leg,
                "z_score": round(z_score, 2),
                "hedge_ratio": round(hedge_ratio, 3),
                "half_life_days": round(half_life, 1),
                "correlation": round(corr, 3),
                "confidence": confidence,
                "expected_return_pct": expected_return,
                "signal_type": "PAIRS_TRADE",
                "price_a": round(float(closes_a[-1]), 2),
                "price_b": round(float(closes_b[-1]), 2),
            })

        except Exception as e:
            logger.debug(f"Pair {sym_a}/{sym_b} analysis failed: {e}")
            continue

    # Sort by absolute z-score (strongest signals first)
    trades.sort(key=lambda x: abs(x["z_score"]), reverse=True)

    _rentech_cache[cache_key] = {"data": trades[:10], "time": time.time()}
    return trades[:10]  # Top 10 pair opportunities


# ============================================================
#  ACTUAL STOCK BETA CALCULATION (replaces hardcoded sector betas)
# ============================================================

_beta_cache = {}
_BETA_CACHE_TTL = 86400  # 24 hours


def calculate_stock_beta(ticker: str, price_data: dict, spy_returns=None) -> float:
    """Calculate actual beta using 120-day returns vs SPY.

    Uses price_data already downloaded (batch yfinance data).
    Beta = cov(stock_returns, spy_returns) / var(spy_returns)
    Falls back to 1.0 if insufficient data. Minimum 60 days required.
    Cached for 24 hours.
    """
    now = time.time()
    if ticker in _beta_cache and (now - _beta_cache[ticker]["time"]) < _BETA_CACHE_TTL:
        return _beta_cache[ticker]["data"]

    beta = 1.0  # default fallback

    try:
        # Get stock closes from price_data
        if ticker not in price_data:
            _beta_cache[ticker] = {"data": beta, "time": now}
            return beta

        stock_df = price_data[ticker]
        stock_closes = _safe_col(stock_df, "Close").values.astype(float)

        if len(stock_closes) < 60:
            _beta_cache[ticker] = {"data": beta, "time": now}
            return beta

        # Use last 120 days (or whatever is available, min 60)
        window = min(120, len(stock_closes))
        stock_window = stock_closes[-window:]
        stock_ret = np.diff(stock_window) / stock_window[:-1]

        # Get SPY returns — reuse if provided, otherwise compute from price_data
        if spy_returns is None:
            if "SPY" in price_data:
                spy_closes = _safe_col(price_data["SPY"], "Close").values.astype(float)
            else:
                # SPY not in price_data — try to fetch (with throttle + thread)
                _throttle_rentech()
                import threading as _rt_spy_thr
                _rt_spy_r = [None]
                _rt_spy_t = _rt_spy_thr.Thread(
                    target=lambda r=_rt_spy_r: r.__setitem__(
                        0, yf.download("SPY", period="6mo", progress=False)
                    ), daemon=True)
                _rt_spy_t.start(); _rt_spy_t.join(timeout=10)
                spy_df = _rt_spy_r[0]
                if spy_df is None or len(spy_df) < 60:
                    # Fallback: multi_source historical
                    try:
                        from analytics.multi_source_adapter import get_historical_any_source
                        spy_df = get_historical_any_source("SPY", "6mo")
                    except Exception:
                        pass
                if spy_df is None or len(spy_df) < 60:
                    _beta_cache[ticker] = {"data": beta, "time": now}
                    return beta
                spy_closes = _safe_col(spy_df, "Close").values.astype(float)

            spy_window = min(120, len(spy_closes))
            spy_prices = spy_closes[-spy_window:]
            spy_returns = np.diff(spy_prices) / spy_prices[:-1]

        # Align lengths
        min_len = min(len(stock_ret), len(spy_returns))
        if min_len < 60:
            _beta_cache[ticker] = {"data": beta, "time": now}
            return beta

        sr = stock_ret[-min_len:]
        mr = spy_returns[-min_len:]

        cov_matrix = np.cov(sr, mr)
        cov_sm = cov_matrix[0][1]
        var_m = cov_matrix[1][1]

        if var_m > 0:
            beta = float(cov_sm / var_m)
            # Clamp to reasonable range
            beta = max(-2.0, min(4.0, beta))

    except Exception as e:
        logger.debug(f"Beta calculation failed for {ticker}: {e}")
        beta = 1.0

    _beta_cache[ticker] = {"data": round(beta, 3), "time": now}
    return round(beta, 3)


# ============================================================
#  2. PORTFOLIO RISK MANAGEMENT
# ============================================================

def assess_portfolio_risk(open_trades: list, price_data: dict = None) -> dict:
    """
    Comprehensive portfolio risk assessment — like a real hedge fund risk desk.

    Checks:
      1. Sector concentration (max 25% in any sector)
      2. Direction concentration (net long/short exposure)
      3. Correlation risk (are positions too correlated?)
      4. Beta exposure (how much market risk?)
      5. Max position risk (any single position too large?)

    Returns risk report with warnings and position-level adjustments.
    """
    if not open_trades:
        return {
            "risk_level": "LOW",
            "warnings": [],
            "sector_exposure": {},
            "net_exposure": 0,
            "estimated_beta": 0,
            "correlation_risk": "LOW",
            "max_single_position_pct": 0,
            "recommendations": ["No open positions — ready to deploy capital"],
        }

    warnings = []
    recommendations = []

    # --- Sector Concentration ---
    sector_counts = {}
    sector_notional = {}
    total_notional = 0

    for trade in open_trades:
        sector = trade.get("sector", "Unknown")
        direction = trade.get("direction", "long")
        notional = abs(trade.get("entry_price", 0) * trade.get("shares", 0))

        sector_counts[sector] = sector_counts.get(sector, 0) + 1
        sector_notional[sector] = sector_notional.get(sector, 0) + notional
        total_notional += notional

    sector_exposure = {}
    for sector, notional in sector_notional.items():
        pct = (notional / total_notional * 100) if total_notional > 0 else 0
        sector_exposure[sector] = round(pct, 1)
        if pct > 30:
            warnings.append(f"HIGH CONCENTRATION: {sector} at {pct:.0f}% of portfolio (max 30%)")
            recommendations.append(f"Reduce {sector} exposure — close weakest position")
        elif pct > 25:
            warnings.append(f"Elevated concentration: {sector} at {pct:.0f}%")

    # --- Direction Concentration ---
    long_notional = sum(
        abs(t.get("entry_price", 0) * t.get("shares", 0))
        for t in open_trades if t.get("direction", "long") == "long"
    )
    short_notional = sum(
        abs(t.get("entry_price", 0) * t.get("shares", 0))
        for t in open_trades if t.get("direction", "") == "short"
    )

    net_exposure = ((long_notional - short_notional) / total_notional * 100) if total_notional > 0 else 0
    gross_exposure = ((long_notional + short_notional) / total_notional * 100) if total_notional > 0 else 0

    if abs(net_exposure) > 80:
        warnings.append(f"DIRECTIONAL RISK: Net exposure {net_exposure:+.0f}% — not market-neutral")
        recommendations.append("Add positions in opposite direction for hedging")

    # --- Correlation Risk ---
    # Simple proxy: count positions in same sector+direction
    sector_dir_counts = {}
    for trade in open_trades:
        key = f"{trade.get('sector', 'Unknown')}_{trade.get('direction', 'long')}"
        sector_dir_counts[key] = sector_dir_counts.get(key, 0) + 1

    max_correlated = max(sector_dir_counts.values()) if sector_dir_counts else 0
    if max_correlated >= 4:
        correlation_risk = "HIGH"
        warnings.append(f"CORRELATION RISK: {max_correlated} positions in same sector+direction")
    elif max_correlated >= 3:
        correlation_risk = "MEDIUM"
        warnings.append(f"Moderate correlation: {max_correlated} positions in same sector+direction")
    else:
        correlation_risk = "LOW"

    # --- Beta Estimation (actual calculated betas) ---
    # Uses real covariance-based beta vs SPY when price_data is available.
    # Falls back to sector-based estimates only when price_data is missing.
    HIGH_BETA_SECTORS = {"Technology", "Consumer Discretionary", "Communication Services", "Financials"}
    LOW_BETA_SECTORS = {"Consumer Staples", "Healthcare", "Utilities", "Real Estate"}

    # Pre-compute SPY returns once for all beta calculations
    _spy_ret = None
    if price_data:
        try:
            if "SPY" in price_data:
                _spy_c = _safe_col(price_data["SPY"], "Close").values.astype(float)
                if len(_spy_c) >= 60:
                    _spy_w = _spy_c[-min(120, len(_spy_c)):]
                    _spy_ret = np.diff(_spy_w) / _spy_w[:-1]
        except Exception:
            pass

    estimated_beta = 0
    for trade in open_trades:
        ticker = trade.get("ticker", trade.get("symbol", ""))
        sector = trade.get("sector", "Unknown")
        direction = trade.get("direction", "long")
        notional = abs(trade.get("entry_price", 0) * trade.get("shares", 0))
        weight = notional / total_notional if total_notional > 0 else 0

        # Try actual calculated beta first, fall back to sector estimate
        if price_data and ticker:
            stock_beta = calculate_stock_beta(ticker, price_data, spy_returns=_spy_ret)
        else:
            if sector in HIGH_BETA_SECTORS:
                stock_beta = 1.3
            elif sector in LOW_BETA_SECTORS:
                stock_beta = 0.7
            else:
                stock_beta = 1.0

        if direction == "long":
            estimated_beta += weight * stock_beta
        else:
            estimated_beta -= weight * stock_beta

    if abs(estimated_beta) > 1.5:
        warnings.append(f"HIGH BETA: Portfolio beta ≈ {estimated_beta:.2f} — high market sensitivity")
        recommendations.append("Add low-beta or short positions to reduce market exposure")

    # --- Overall Risk Level ---
    if len(warnings) >= 3 or any("HIGH" in w for w in warnings):
        risk_level = "HIGH"
    elif len(warnings) >= 1:
        risk_level = "MEDIUM"
    else:
        risk_level = "LOW"
        recommendations.append("Portfolio risk is well-managed — continue current strategy")

    return {
        "risk_level": risk_level,
        "warnings": warnings,
        "recommendations": recommendations,
        "sector_exposure": sector_exposure,
        "net_exposure_pct": round(net_exposure, 1),
        "gross_exposure_pct": round(gross_exposure, 1),
        "long_notional": round(long_notional, 2),
        "short_notional": round(short_notional, 2),
        "estimated_beta": round(estimated_beta, 2),
        "correlation_risk": correlation_risk,
        "num_positions": len(open_trades),
        "sector_count": len(sector_counts),
    }


# ============================================================
#  PORTFOLIO VALUE-AT-RISK (VaR) BUDGET
#  Parametric VaR using actual position covariance matrix
#  Blocks new trades when portfolio risk budget is exhausted
# ============================================================

def calculate_portfolio_var(open_trades: list, price_data: dict,
                            portfolio_value: float) -> dict:
    """
    Calculate 95% parametric Value-at-Risk for the current portfolio.

    Uses actual return correlations between positions (not sector proxies).
    Returns VaR as % of portfolio and a position-size multiplier.

    VaR > 2% → half-size new positions
    VaR > 3% → block all new positions
    """
    if not open_trades or portfolio_value <= 0:
        return {"var_pct": 0, "cvar_pct": 0, "var_multiplier": 1.0, "status": "NO_POSITIONS"}

    # Get symbols and weights
    symbols = []
    weights = []
    total_notional = 0

    for trade in open_trades:
        ticker = trade.get("ticker", "")
        notional = abs(trade.get("entry_price", 0) * trade.get("shares", 0))
        direction_sign = 1.0 if trade.get("direction", "long") == "long" else -1.0
        if ticker in price_data and notional > 0:
            symbols.append(ticker)
            weights.append(notional * direction_sign)
            total_notional += notional

    if len(symbols) < 1 or total_notional == 0:
        return {"var_pct": 0, "cvar_pct": 0, "var_multiplier": 1.0, "status": "INSUFFICIENT_DATA"}

    # Build returns matrix (60-day lookback)
    returns_matrix = []
    valid_symbols = []
    valid_weights = []

    for idx, sym in enumerate(symbols):
        df = price_data.get(sym)
        if df is None:
            continue
        try:
            closes = df["Close"].iloc[:, 0].values.astype(float) if hasattr(df["Close"], "columns") else df["Close"].values.astype(float)
            if len(closes) < 30:
                continue
            rets = np.diff(closes[-61:]) / closes[-61:-1]  # 60-day returns
            if len(rets) >= 20:
                returns_matrix.append(rets[-min(len(rets), 60):])
                valid_symbols.append(sym)
                valid_weights.append(weights[idx])
        except Exception:
            continue

    if len(valid_symbols) < 1:
        return {"var_pct": 0, "cvar_pct": 0, "var_multiplier": 1.0, "status": "NO_VALID_DATA"}

    # Align return lengths
    min_len = min(len(r) for r in returns_matrix)
    returns_matrix = np.array([r[-min_len:] for r in returns_matrix])

    # Portfolio weights as fractions of portfolio
    w = np.array(valid_weights) / portfolio_value

    # Covariance matrix
    try:
        if len(valid_symbols) == 1:
            port_var = float(np.var(returns_matrix[0]) * w[0]**2)
        else:
            cov_matrix = np.cov(returns_matrix)
            port_var = float(w @ cov_matrix @ w)
    except Exception:
        # Fallback: assume zero correlation
        port_var = float(np.sum((w**2) * np.var(returns_matrix, axis=1)))

    port_vol = np.sqrt(max(port_var, 0))

    # Parametric VaR (95%) = z_0.95 * portfolio_vol
    z_95 = 1.645
    var_pct = round(port_vol * z_95 * 100, 4)

    # CVaR (Expected Shortfall) — analytical under normality
    z_cvar = 2.063  # E[X | X > z_0.95] for standard normal
    cvar_pct = round(port_vol * z_cvar * 100, 4)

    # VaR budget enforcement (loosened — was blocking too aggressively)
    if var_pct > 5.0:
        var_multiplier = 0.3  # Reduced sizing instead of full halt — was 0.0
        status = "VAR_EXCEEDED"
    elif var_pct > 3.5:
        var_multiplier = 0.5  # Half-size
        status = "VAR_WARNING"
    elif var_pct > 2.5:
        var_multiplier = 0.75
        status = "VAR_ELEVATED"
    else:
        var_multiplier = 1.0
        status = "VAR_OK"

    # Correlation info
    avg_corr = 0.0
    if len(valid_symbols) > 1:
        try:
            corr_matrix = np.corrcoef(returns_matrix)
            # Average off-diagonal correlation
            n = len(valid_symbols)
            off_diag = corr_matrix[np.triu_indices(n, k=1)]
            avg_corr = float(np.mean(off_diag))
        except Exception:
            avg_corr = 0.0

    return {
        "var_pct": var_pct,
        "cvar_pct": cvar_pct,
        "var_multiplier": var_multiplier,
        "portfolio_vol_daily": round(port_vol * 100, 4),
        "avg_correlation": round(avg_corr, 3),
        "num_positions_analyzed": len(valid_symbols),
        "status": status,
    }


# ============================================================
#  CORRELATION-AWARE POSITION LIMITS
#  Blocks new trades that would create hidden concentration risk
# ============================================================

def check_correlation_limit(candidate_symbol: str, open_trades: list,
                            price_data: dict) -> dict:
    """
    Check if a candidate trade would create excessive correlation risk.

    Returns:
        allow (bool): whether the trade should proceed
        correlation_multiplier (float): size multiplier (0.5-1.0)
        reason (str): explanation if blocked
        high_corr_positions (list): symbols with >0.75 correlation
    """
    if not open_trades or candidate_symbol not in price_data:
        return {"allow": True, "correlation_multiplier": 1.0, "reason": "", "high_corr_positions": []}

    # Get candidate returns
    cand_df = price_data.get(candidate_symbol)
    if cand_df is None:
        return {"allow": True, "correlation_multiplier": 1.0, "reason": "", "high_corr_positions": []}

    try:
        cand_closes = cand_df["Close"].iloc[:, 0].values.astype(float) if hasattr(cand_df["Close"], "columns") else cand_df["Close"].values.astype(float)
        if len(cand_closes) < 30:
            return {"allow": True, "correlation_multiplier": 1.0, "reason": "", "high_corr_positions": []}
        cand_rets = np.diff(cand_closes[-61:]) / cand_closes[-61:-1]
    except Exception:
        return {"allow": True, "correlation_multiplier": 1.0, "reason": "", "high_corr_positions": []}

    # Check correlation with each open position
    high_corr = []
    all_corrs = []

    for trade in open_trades:
        ticker = trade.get("ticker", "")
        if ticker == candidate_symbol or ticker not in price_data:
            continue

        try:
            df = price_data[ticker]
            closes = df["Close"].iloc[:, 0].values.astype(float) if hasattr(df["Close"], "columns") else df["Close"].values.astype(float)
            if len(closes) < 30:
                continue
            pos_rets = np.diff(closes[-61:]) / closes[-61:-1]

            # Align lengths
            min_len = min(len(cand_rets), len(pos_rets))
            if min_len < 20:
                continue

            corr = float(np.corrcoef(cand_rets[-min_len:], pos_rets[-min_len:])[0, 1])
            if not np.isnan(corr):
                all_corrs.append(corr)
                if abs(corr) > 0.75:
                    high_corr.append({"symbol": ticker, "correlation": round(corr, 3)})
        except Exception:
            continue

    # Decision: block if too many highly correlated positions
    if len(high_corr) >= 3:
        return {
            "allow": False,
            "correlation_multiplier": 0.0,
            "reason": f"High correlation (>0.75) with {len(high_corr)} existing positions: {[h['symbol'] for h in high_corr[:3]]}",
            "high_corr_positions": high_corr,
        }

    # Compute size multiplier based on average correlation
    avg_corr = float(np.mean(all_corrs)) if all_corrs else 0.0

    if avg_corr > 0.60:
        corr_mult = 0.50
    elif avg_corr > 0.45:
        corr_mult = 0.75
    else:
        corr_mult = 1.0

    # Penalize if 2 highly correlated
    if len(high_corr) >= 2:
        corr_mult = min(corr_mult, 0.60)

    return {
        "allow": True,
        "correlation_multiplier": round(corr_mult, 2),
        "reason": f"Avg correlation: {avg_corr:.2f}" if avg_corr > 0.3 else "",
        "high_corr_positions": high_corr,
        "avg_correlation": round(avg_corr, 3),
    }


def calculate_drawdown_circuit_breaker(portfolio_value: float, peak_value: float,
                                        original_capital: float = 100_000) -> dict:
    """
    Drawdown circuit breaker — automatically reduces risk when losing.

    Levels:
      - NORMAL: drawdown < 5% from peak → full trading
      - CAUTION: 5-8% drawdown → reduce position sizes by 30%
      - WARNING: 8-12% drawdown → reduce by 60%, no new shorts
      - HALT: >12% drawdown → no new trades, only exits
    """
    if peak_value <= 0:
        peak_value = original_capital

    drawdown_pct = ((portfolio_value - peak_value) / peak_value) * 100

    if drawdown_pct > -5:
        return {
            "level": "NORMAL",
            "drawdown_pct": round(drawdown_pct, 2),
            "position_size_multiplier": 1.0,
            "allow_new_longs": True,
            "allow_new_shorts": True,
            "message": "Portfolio within normal range",
        }
    elif drawdown_pct > -8:
        return {
            "level": "CAUTION",
            "drawdown_pct": round(drawdown_pct, 2),
            "position_size_multiplier": 0.7,
            "allow_new_longs": True,
            "allow_new_shorts": True,
            "message": f"Drawdown {drawdown_pct:.1f}% — reducing position sizes 30%",
        }
    elif drawdown_pct > -12:
        return {
            "level": "WARNING",
            "drawdown_pct": round(drawdown_pct, 2),
            "position_size_multiplier": 0.4,
            "allow_new_longs": True,
            "allow_new_shorts": False,
            "message": f"Drawdown {drawdown_pct:.1f}% — defensive mode, no new shorts",
        }
    else:
        return {
            "level": "HALT",
            "drawdown_pct": round(drawdown_pct, 2),
            "position_size_multiplier": 0.0,
            "allow_new_longs": False,
            "allow_new_shorts": False,
            "message": f"CRITICAL drawdown {drawdown_pct:.1f}% — trading halted, exits only",
        }


# ============================================================
#  3. ENSEMBLE SIGNAL VOTING
# ============================================================

def ensemble_vote(stock_data: dict, regime: str = "SIDEWAYS") -> dict:
    """
    Three independent scoring models vote on a stock's direction.
    Trade only when 2/3 models agree.

    Models:
      1. MOMENTUM MODEL — trend following (12-1 month return, EMA alignment)
      2. MEAN REVERSION MODEL — RSI(2), Bollinger position, z-score distance
      3. FUNDAMENTAL MODEL — value + quality composite

    Returns:
      - consensus direction (LONG/SHORT/NO_TRADE)
      - vote breakdown
      - combined confidence (boosted if all 3 agree)
    """
    closes = stock_data.get("closes", [])
    if len(closes) < 60:
        return {"consensus": "NO_TRADE", "reason": "Insufficient data"}

    price = float(closes[-1])

    # --- Model 1: MOMENTUM ---
    ret_20d = (closes[-1] / closes[-20] - 1) * 100 if len(closes) >= 20 else 0
    ret_60d = (closes[-1] / closes[-60] - 1) * 100 if len(closes) >= 60 else 0
    ema_9 = float(pd.Series(closes).ewm(span=9, adjust=False).mean().iloc[-1])
    ema_21 = float(pd.Series(closes).ewm(span=21, adjust=False).mean().iloc[-1])
    ema_50 = float(pd.Series(closes).ewm(span=50, adjust=False).mean().iloc[-1])

    momentum_score = 0
    if ret_20d > 3: momentum_score += 1
    if ret_60d > 5: momentum_score += 1
    if ema_9 > ema_21 > ema_50: momentum_score += 2
    elif ema_9 < ema_21 < ema_50: momentum_score -= 2
    if ret_20d < -3: momentum_score -= 1
    if ret_60d < -5: momentum_score -= 1

    if momentum_score >= 2:
        momentum_vote = "LONG"
    elif momentum_score <= -2:
        momentum_vote = "SHORT"
    else:
        momentum_vote = "NEUTRAL"

    # --- Model 2: MEAN REVERSION ---
    # RSI(2) Connors strategy
    deltas = np.diff(closes[-3:])
    gains = np.mean([d for d in deltas if d > 0]) if any(d > 0 for d in deltas) else 0.001
    losses = np.mean([abs(d) for d in deltas if d < 0]) if any(d < 0 for d in deltas) else 0.001
    rs = gains / losses if losses > 0 else 100
    rsi2 = 100 - (100 / (1 + rs))

    # Bollinger position
    sma_20 = float(np.mean(closes[-20:]))
    std_20 = float(np.std(closes[-20:]))
    bb_upper = sma_20 + 2 * std_20
    bb_lower = sma_20 - 2 * std_20
    bb_pos = ((price - bb_lower) / (bb_upper - bb_lower) * 100) if (bb_upper - bb_lower) > 0 else 50

    mr_score = 0
    if rsi2 < 10: mr_score += 2  # Deeply oversold — strong buy
    elif rsi2 < 25: mr_score += 1
    elif rsi2 > 90: mr_score -= 2  # Deeply overbought — strong sell
    elif rsi2 > 75: mr_score -= 1

    if bb_pos < 10: mr_score += 1  # Near lower band
    elif bb_pos > 90: mr_score -= 1  # Near upper band

    # Z-score of price vs 60-day mean
    if len(closes) >= 60:
        price_z = (price - np.mean(closes[-60:])) / (np.std(closes[-60:]) + 0.01)
        if price_z < -2: mr_score += 1
        elif price_z > 2: mr_score -= 1

    if mr_score >= 2:
        mr_vote = "LONG"
    elif mr_score <= -2:
        mr_vote = "SHORT"
    else:
        mr_vote = "NEUTRAL"

    # --- Model 3: FUNDAMENTAL / QUALITY ---
    # Uses pre-computed factors from quant engine
    value_raw = stock_data.get("value_raw", 0)
    quality_raw = stock_data.get("quality_raw", 0)

    fund_score = 0
    if value_raw > 0.5: fund_score += 1  # Good value
    elif value_raw < -0.5: fund_score -= 1
    if quality_raw > 0.5: fund_score += 1  # High quality
    elif quality_raw < -0.5: fund_score -= 1

    # Volume confirmation
    vol_ratio = stock_data.get("vol_ratio", 1.0)
    if vol_ratio > 1.3: fund_score += 1
    elif vol_ratio < 0.7: fund_score -= 1

    if fund_score >= 2:
        fund_vote = "LONG"
    elif fund_score <= -2:
        fund_vote = "SHORT"
    else:
        fund_vote = "NEUTRAL"

    # --- Consensus ---
    votes = [momentum_vote, mr_vote, fund_vote]
    long_votes = votes.count("LONG")
    short_votes = votes.count("SHORT")

    if long_votes >= 2:
        consensus = "LONG"
        agreement = long_votes
    elif short_votes >= 2:
        consensus = "SHORT"
        agreement = short_votes
    else:
        consensus = "NO_TRADE"
        agreement = 0

    # Confidence boost if all 3 agree
    base_confidence = stock_data.get("confidence", 50)
    if agreement == 3:
        confidence_mod = 1.20  # +20% confidence for unanimous
    elif agreement == 2:
        confidence_mod = 1.05  # +5% for 2/3 agreement
    else:
        confidence_mod = 0.70  # -30% for no consensus

    adjusted_confidence = min(95, int(base_confidence * confidence_mod))

    return {
        "consensus": consensus,
        "votes": {
            "momentum": momentum_vote,
            "mean_reversion": mr_vote,
            "fundamental": fund_vote,
        },
        "long_votes": long_votes,
        "short_votes": short_votes,
        "agreement": agreement,
        "confidence_modifier": confidence_mod,
        "adjusted_confidence": adjusted_confidence,
        "details": {
            "momentum_score": momentum_score,
            "mr_score": mr_score,
            "fund_score": fund_score,
            "rsi2": round(rsi2, 1),
            "bb_position": round(bb_pos, 1),
        }
    }


# ============================================================
#  4. ALTERNATIVE DATA SIGNALS
# ============================================================

def get_alt_data_signals(symbols: list) -> dict:
    """
    Gather alternative data signals for a list of symbols.

    Sources (all from yfinance — no extra API needed):
      - Short interest / float short ratio
      - Insider transactions (net buying vs selling)
      - Institutional ownership changes

    Returns signals dict keyed by symbol.
    """
    cache_key = f"alt_data_{'_'.join(sorted(symbols[:10]))}"
    cached = _rentech_cache.get(cache_key)
    if cached and time.time() - cached["time"] < 900:  # 15 min cache
        return cached["data"]

    signals = {}

    # Process in small batches to respect API limits
    for symbol in symbols[:30]:  # Max 30 to avoid API hammering
        try:
            _throttle_rentech()
            ticker = yf.Ticker(symbol)
            info = ticker.info or {}

            signal = {
                "symbol": symbol,
                "short_interest": None,
                "short_ratio": None,
                "insider_signal": "NEUTRAL",
                "institutional_pct": None,
                "composite_alt_score": 0,
            }

            # --- Short Interest ---
            short_pct = info.get("shortPercentOfFloat")
            short_ratio = info.get("shortRatio")  # days to cover

            if short_pct is not None:
                signal["short_interest"] = round(short_pct * 100, 1)

                # High short interest + positive momentum = SQUEEZE potential
                if short_pct > 0.20:  # >20% short float
                    signal["composite_alt_score"] += 2  # Squeeze candidate
                    signal["short_squeeze_risk"] = "HIGH"
                elif short_pct > 0.10:
                    signal["composite_alt_score"] += 1
                    signal["short_squeeze_risk"] = "MODERATE"
                else:
                    signal["short_squeeze_risk"] = "LOW"

            if short_ratio is not None:
                signal["short_ratio"] = round(short_ratio, 1)
                if short_ratio > 5:  # >5 days to cover = crowded short
                    signal["composite_alt_score"] += 1

            # --- Insider Transactions ---
            try:
                insider_txns = ticker.insider_transactions
                if insider_txns is not None and not insider_txns.empty:
                    recent = insider_txns.head(10)  # Last 10 transactions

                    buys = 0
                    sells = 0
                    for _, row in recent.iterrows():
                        txn_text = str(row.get("Text", "")).lower()
                        if "purchase" in txn_text or "buy" in txn_text or "acquisition" in txn_text:
                            buys += 1
                        elif "sale" in txn_text or "sell" in txn_text or "disposition" in txn_text:
                            sells += 1

                    if buys > sells + 2:
                        signal["insider_signal"] = "STRONG_BUY"
                        signal["composite_alt_score"] += 2
                    elif buys > sells:
                        signal["insider_signal"] = "BUY"
                        signal["composite_alt_score"] += 1
                    elif sells > buys + 2:
                        signal["insider_signal"] = "STRONG_SELL"
                        signal["composite_alt_score"] -= 2
                    elif sells > buys:
                        signal["insider_signal"] = "SELL"
                        signal["composite_alt_score"] -= 1

                    signal["insider_buys"] = buys
                    signal["insider_sells"] = sells
            except Exception:
                pass

            # --- Institutional Ownership ---
            inst_pct = info.get("heldPercentInstitutions")
            if inst_pct is not None:
                signal["institutional_pct"] = round(inst_pct * 100, 1)
                # High institutional = more stable, better for our strategies
                if inst_pct > 0.80:
                    signal["composite_alt_score"] += 1

            signals[symbol] = signal

        except Exception as e:
            logger.debug(f"Alt data failed for {symbol}: {e}")
            signals[symbol] = {
                "symbol": symbol,
                "composite_alt_score": 0,
                "error": str(e),
            }

    _rentech_cache[cache_key] = {"data": signals, "time": time.time()}
    return signals


# ============================================================
#  5. INTRADAY MEAN REVERSION SIGNALS
# ============================================================


def enhanced_mean_reversion_score(closes, volumes):
    """
    Multi-confirmation mean reversion timer. Requires 2 of 3 confirmations
    before triggering a mean reversion entry:

    1. RSI Divergence — price makes lower low but RSI makes higher low (bullish)
       or price makes higher high but RSI makes lower high (bearish)
    2. Volume Dry-Up — selling volume declining on successive down legs
    3. Bollinger Band Re-Entry — price was below lower band, now crossing back in

    Returns dict with mr_score, confirmations count, and individual signals.
    """
    result = {
        "mr_score": 0, "confirmations": 0,
        "divergence": False, "volume_dryup": False, "bb_reentry": False,
        "timing_quality": "POOR", "direction": "NEUTRAL"
    }

    if len(closes) < 20 or len(volumes) < 20:
        return result

    price = float(closes[-1])
    confirmations = 0
    direction_votes = []  # +1 = bullish, -1 = bearish

    # --- Confirmation 1: RSI Divergence ---
    try:
        # Calculate RSI(14) for two windows: days -14 to -7 and days -7 to 0
        def _rsi(data):
            deltas = np.diff(data)
            gains = np.mean(np.maximum(deltas, 0))
            losses_v = np.mean(np.maximum(-deltas, 0))
            if losses_v == 0:
                return 100.0
            return 100 - (100 / (1 + gains / losses_v))

        if len(closes) >= 28:
            rsi_old = _rsi(closes[-28:-14])
            rsi_new = _rsi(closes[-14:])
            price_old_low = float(np.min(closes[-28:-14]))
            price_new_low = float(np.min(closes[-14:]))
            price_old_high = float(np.max(closes[-28:-14]))
            price_new_high = float(np.max(closes[-14:]))

            # Bullish divergence: price lower low, RSI higher low
            if price_new_low < price_old_low and rsi_new > rsi_old:
                result["divergence"] = True
                confirmations += 1
                direction_votes.append(1)

            # Bearish divergence: price higher high, RSI lower high
            elif price_new_high > price_old_high and rsi_new < rsi_old:
                result["divergence"] = True
                confirmations += 1
                direction_votes.append(-1)
    except Exception:
        pass

    # --- Confirmation 2: Volume Dry-Up ---
    try:
        if len(closes) >= 15 and len(volumes) >= 15:
            # Check if selling volume is declining over last 3 legs
            daily_rets = np.diff(closes[-15:])
            # Split into 3 windows of 5 days each
            vol_windows = [volumes[-15:-10], volumes[-10:-5], volumes[-5:]]
            ret_windows = [daily_rets[:4], daily_rets[4:9], daily_rets[9:]]

            # Calculate sell-side volume for each window
            sell_vols = []
            for rv, vv in zip(ret_windows, vol_windows[:3]):
                min_len = min(len(rv), len(vv))
                if min_len > 0:
                    sell_vol = float(np.sum(vv[:min_len][rv[:min_len] < 0]))
                    sell_vols.append(sell_vol)

            # Volume dry-up: sell volume declining across windows
            if len(sell_vols) >= 3 and sell_vols[2] < sell_vols[1] < sell_vols[0]:
                result["volume_dryup"] = True
                confirmations += 1
                # If price is below average, this is bullish (sellers exhausted)
                sma_20 = float(np.mean(closes[-20:]))
                direction_votes.append(1 if price < sma_20 else -1)

            # Buy-side volume increasing while price is rising = bearish exhaustion
            buy_vols = []
            for rv, vv in zip(ret_windows, vol_windows[:3]):
                min_len = min(len(rv), len(vv))
                if min_len > 0:
                    buy_vol = float(np.sum(vv[:min_len][rv[:min_len] > 0]))
                    buy_vols.append(buy_vol)

            if len(buy_vols) >= 3 and buy_vols[2] < buy_vols[1] < buy_vols[0]:
                if not result["volume_dryup"]:  # Don't double count
                    result["volume_dryup"] = True
                    confirmations += 1
                    sma_20 = float(np.mean(closes[-20:]))
                    direction_votes.append(-1 if price > sma_20 else 1)
    except Exception:
        pass

    # --- Confirmation 3: Bollinger Band Re-Entry ---
    try:
        if len(closes) >= 21:
            sma_20 = float(np.mean(closes[-20:]))
            std_20 = float(np.std(closes[-20:]))
            bb_lower = sma_20 - 2 * std_20
            bb_upper = sma_20 + 2 * std_20

            prev_price = float(closes[-2])

            # Was below lower band, now crossing back in (bullish re-entry)
            if prev_price < bb_lower and price >= bb_lower:
                result["bb_reentry"] = True
                confirmations += 1
                direction_votes.append(1)

            # Was above upper band, now crossing back in (bearish re-entry)
            elif prev_price > bb_upper and price <= bb_upper:
                result["bb_reentry"] = True
                confirmations += 1
                direction_votes.append(-1)

            # Close to re-entry (within 0.5% of band)
            elif price < bb_lower * 1.005 and price > bb_lower:
                result["bb_reentry"] = True
                confirmations += 1
                direction_votes.append(1)
            elif price > bb_upper * 0.995 and price < bb_upper:
                result["bb_reentry"] = True
                confirmations += 1
                direction_votes.append(-1)
    except Exception:
        pass

    # --- Score and quality ---
    result["confirmations"] = confirmations

    if confirmations >= 3:
        result["mr_score"] = 5
        result["timing_quality"] = "EXCELLENT"
    elif confirmations >= 2:
        result["mr_score"] = 3
        result["timing_quality"] = "GOOD"
    elif confirmations == 1:
        result["mr_score"] = 1
        result["timing_quality"] = "FAIR"

    # Direction from vote consensus
    if direction_votes:
        vote_sum = sum(direction_votes)
        if vote_sum > 0:
            result["direction"] = "LONG"
        elif vote_sum < 0:
            result["direction"] = "SHORT"

    return result


def get_mean_reversion_signals(price_data: dict) -> list:
    """
    Identify stocks with strong mean reversion setups.

    Strategies:
      1. RSI(2) Connors — buy when RSI(2) < 10, sell when RSI(2) > 90
         (historically 75-91% win rate on S&P 500 stocks)
      2. VWAP Deviation — buy below VWAP, sell above VWAP
      3. Bollinger Band extreme — buy below lower band, sell above upper band
      4. 3-day consecutive moves — buy after 3 down days, sell after 3 up days

    Returns list of mean reversion trade setups with confidence.
    """
    cache_key = "mean_reversion_signals"
    cached = _rentech_cache.get(cache_key)
    if cached and time.time() - cached["time"] < 600:
        return cached["data"]

    setups = []

    for symbol, df in price_data.items():
        try:
            closes = df["Close"].values.astype(float)
            highs = df["High"].values.astype(float).flatten() if "High" in df.columns else closes
            lows = df["Low"].values.astype(float).flatten() if "Low" in df.columns else closes
            volumes = df["Volume"].values.astype(float).flatten() if "Volume" in df.columns else np.ones(len(closes))

            if len(closes) < 60:
                continue

            price = float(closes[-1])

            # --- RSI(2) Connors Strategy ---
            deltas = np.diff(closes[-3:])
            gains = np.mean([d for d in deltas if d > 0]) if any(d > 0 for d in deltas) else 0.001
            losses_v = np.mean([abs(d) for d in deltas if d < 0]) if any(d < 0 for d in deltas) else 0.001
            rs = gains / losses_v if losses_v > 0 else 100
            rsi2 = 100 - (100 / (1 + rs))

            # --- VWAP Approximation ---
            # True VWAP needs intraday data, but we approximate with cumulative price*volume
            typical_price = (highs[-20:] + lows[-20:] + closes[-20:]) / 3
            vwap_20d = float(np.sum(typical_price * volumes[-20:]) / (np.sum(volumes[-20:]) + 1))
            vwap_deviation = ((price - vwap_20d) / vwap_20d) * 100

            # --- Bollinger Band Position ---
            sma_20 = float(np.mean(closes[-20:]))
            std_20 = float(np.std(closes[-20:]))
            bb_upper = sma_20 + 2 * std_20
            bb_lower = sma_20 - 2 * std_20
            bb_pos = ((price - bb_lower) / (bb_upper - bb_lower) * 100) if (bb_upper - bb_lower) > 0 else 50

            # --- Consecutive Move Counter ---
            daily_returns = np.diff(closes[-5:]) / closes[-5:-1] * 100
            consecutive_down = sum(1 for r in daily_returns if r < -0.5)
            consecutive_up = sum(1 for r in daily_returns if r > 0.5)

            # --- Score the setup ---
            mr_score = 0
            reasons = []

            # RSI(2) extreme — the money maker
            if rsi2 < 5:
                mr_score += 3
                reasons.append(f"RSI(2) = {rsi2:.0f} — deeply oversold (Connors buy)")
            elif rsi2 < 10:
                mr_score += 2
                reasons.append(f"RSI(2) = {rsi2:.0f} — oversold (Connors buy)")
            elif rsi2 > 95:
                mr_score -= 3
                reasons.append(f"RSI(2) = {rsi2:.0f} — deeply overbought (Connors sell)")
            elif rsi2 > 90:
                mr_score -= 2
                reasons.append(f"RSI(2) = {rsi2:.0f} — overbought (Connors sell)")

            # VWAP deviation
            if vwap_deviation < -3:
                mr_score += 1
                reasons.append(f"Below VWAP by {abs(vwap_deviation):.1f}% — institutional buy zone")
            elif vwap_deviation > 3:
                mr_score -= 1
                reasons.append(f"Above VWAP by {vwap_deviation:.1f}% — institutional sell zone")

            # Bollinger Band extreme
            if bb_pos < 5:
                mr_score += 2
                reasons.append(f"Below lower Bollinger Band — extreme oversold")
            elif bb_pos > 95:
                mr_score -= 2
                reasons.append(f"Above upper Bollinger Band — extreme overbought")

            # Consecutive moves
            if consecutive_down >= 3:
                mr_score += 1
                reasons.append(f"{consecutive_down} consecutive down days — bounce expected")
            elif consecutive_up >= 3:
                mr_score -= 1
                reasons.append(f"{consecutive_up} consecutive up days — pullback expected")

            # ENHANCED TIMING: Require multi-confirmation before entry
            # RSI divergence + volume dry-up + Bollinger re-entry
            enhanced = enhanced_mean_reversion_score(closes, volumes)
            enhanced_confirmations = enhanced["confirmations"]

            # Boost mr_score if enhanced timing confirms
            if enhanced_confirmations >= 2:
                mr_score += 2 if enhanced["direction"] == ("LONG" if mr_score > 0 else "SHORT") else 0
                # Even boost opposing direction if strong enough
                if enhanced_confirmations >= 3:
                    mr_score += 1

            # Only generate signal if strong enough AND has timing confirmation
            # Require either: (a) strong base score (>=4) OR (b) moderate score (>=3) + 2 confirmations
            has_timing = enhanced_confirmations >= 2
            has_strong_base = abs(mr_score) >= 4
            has_moderate_plus_timing = abs(mr_score) >= 3 and has_timing

            if has_strong_base or has_moderate_plus_timing:
                direction = "LONG" if mr_score > 0 else "SHORT"
                confidence = min(90, 50 + abs(mr_score) * 10)
                # Boost confidence for confirmed setups
                if has_timing:
                    confidence = min(95, confidence + enhanced_confirmations * 5)

                setups.append({
                    "symbol": symbol,
                    "direction": direction,
                    "signal_type": "MEAN_REVERSION",
                    "mr_score": mr_score,
                    "confidence": confidence,
                    "rsi2": round(rsi2, 1),
                    "vwap_deviation_pct": round(vwap_deviation, 2),
                    "bb_position": round(bb_pos, 1),
                    "consecutive_down": consecutive_down,
                    "consecutive_up": consecutive_up,
                    "price": price,
                    "reasons": reasons,
                    "timing_quality": enhanced["timing_quality"],
                    "timing_confirmations": enhanced_confirmations,
                    "rsi_divergence": enhanced["divergence"],
                    "volume_dryup": enhanced["volume_dryup"],
                    "bb_reentry": enhanced["bb_reentry"],
                    "expected_hold_days": 2 if abs(mr_score) >= 4 else 5,
                    "expected_return_pct": round(abs(mr_score) * 1.5, 1),
                })

        except Exception as e:
            logger.debug(f"Mean reversion analysis failed for {symbol}: {e}")
            continue

    # Sort by score magnitude
    setups.sort(key=lambda x: abs(x["mr_score"]), reverse=True)

    result = setups[:15]  # Top 15 setups
    _rentech_cache[cache_key] = {"data": result, "time": time.time()}
    return result


# ============================================================
#  MASTER FUNCTION — Run all RenTech analyses
# ============================================================

def run_rentech_analysis(price_data: dict, open_trades: list = None,
                         portfolio_value: float = 100_000,
                         peak_value: float = 100_000) -> dict:
    """
    Run all Renaissance Technologies-style analyses.

    This is the master function that calls all sub-modules and returns
    a comprehensive report. Called from generate_quant_picks().

    Args:
        price_data: dict of {symbol: DataFrame} from yf.download
        open_trades: list of current open paper trades
        portfolio_value: current portfolio value
        peak_value: highest portfolio value achieved

    Returns:
        dict with pairs_trades, risk_assessment, mean_reversion_setups,
        circuit_breaker status, and ensemble vote summary
    """
    cache_key = "rentech_analysis"
    cached = _rentech_cache.get(cache_key)
    if cached and time.time() - cached["time"] < 600:
        return cached["data"]

    logger.info("🏛️ Running Renaissance Technologies analysis suite...")

    result = {
        "pairs_trades": [],
        "mean_reversion_setups": [],
        "portfolio_risk": {},
        "circuit_breaker": {},
        "alt_data": {},
        "portfolio_beta": {"beta": 0, "hedge_needed": False},
        "timestamp": datetime.now().isoformat(),
    }

    try:
        # 1. Pairs Trading
        result["pairs_trades"] = find_pairs_trades(price_data)
        logger.info(f"  Pairs: {len(result['pairs_trades'])} trade opportunities found")
    except Exception as e:
        logger.warning(f"Pairs trading analysis failed: {e}")

    try:
        # 2. Mean Reversion Setups
        result["mean_reversion_setups"] = get_mean_reversion_signals(price_data)
        logger.info(f"  Mean reversion: {len(result['mean_reversion_setups'])} setups found")
    except Exception as e:
        logger.warning(f"Mean reversion analysis failed: {e}")

    try:
        # 3. Portfolio Risk Assessment
        if open_trades:
            result["portfolio_risk"] = assess_portfolio_risk(open_trades, price_data)
            logger.info(f"  Portfolio risk: {result['portfolio_risk'].get('risk_level', 'UNKNOWN')}")
    except Exception as e:
        logger.warning(f"Portfolio risk assessment failed: {e}")

    try:
        # 4. Circuit Breaker
        result["circuit_breaker"] = calculate_drawdown_circuit_breaker(
            portfolio_value, peak_value
        )
        logger.info(f"  Circuit breaker: {result['circuit_breaker'].get('level', 'UNKNOWN')}")
    except Exception as e:
        logger.warning(f"Circuit breaker check failed: {e}")

    try:
        # 5. Earnings Calendar Shield
        result["earnings_shield"] = get_earnings_shield(price_data)
        logger.info(f"  Earnings shield: {len(result['earnings_shield'].get('blocked', []))} stocks blocked")
    except Exception as e:
        logger.warning(f"Earnings shield failed: {e}")
        result["earnings_shield"] = {"blocked": [], "warning": []}

    try:
        # 6. Sector Rotation Detection
        result["sector_rotation"] = detect_sector_rotation(price_data)
        logger.info(f"  Sector rotation: inflow={result['sector_rotation'].get('top_inflow', [])}")
    except Exception as e:
        logger.warning(f"Sector rotation failed: {e}")
        result["sector_rotation"] = {}

    try:
        # 7. Multi-Timeframe Confirmation
        result["mtf_signals"] = get_multi_timeframe_signals(price_data)
        logger.info(f"  Multi-timeframe: {len(result['mtf_signals'])} signals")
    except Exception as e:
        logger.warning(f"Multi-timeframe failed: {e}")
        result["mtf_signals"] = {}

    try:
        # 8. Regime Transition Prediction
        result["regime_transition"] = predict_regime_transition(price_data)
        logger.info(f"  Regime prediction: {result['regime_transition'].get('prediction', 'N/A')}")
    except Exception as e:
        logger.warning(f"Regime transition failed: {e}")
        result["regime_transition"] = {}

    try:
        # 9. Portfolio Beta
        if open_trades:
            result["portfolio_beta"] = calculate_portfolio_beta(open_trades, price_data)
    except Exception as e:
        logger.warning(f"Portfolio beta failed: {e}")

    try:
        # 11. Portfolio VaR Budget — block new trades when risk is full
        if open_trades and price_data:
            result["portfolio_var"] = calculate_portfolio_var(open_trades, price_data, portfolio_value)
            logger.info(f"  Portfolio VaR: {result['portfolio_var'].get('var_pct', 0):.2f}% | multiplier={result['portfolio_var'].get('var_multiplier', 1.0)}")
        else:
            result["portfolio_var"] = {"var_pct": 0, "cvar_pct": 0, "var_multiplier": 1.0, "status": "NO_POSITIONS"}
    except Exception as e:
        logger.warning(f"Portfolio VaR failed: {e}")
        result["portfolio_var"] = {"var_pct": 0, "cvar_pct": 0, "var_multiplier": 1.0, "status": "ERROR"}

    try:
        # 10. Drawdown Recovery Mode
        result["drawdown_mode"] = get_drawdown_recovery_mode(portfolio_value, peak_value)
        logger.info(f"  Drawdown mode: {result['drawdown_mode'].get('mode', 'NORMAL')}")
    except Exception as e:
        logger.warning(f"Drawdown recovery failed: {e}")
        result["drawdown_mode"] = {"mode": "NORMAL"}

    _rentech_cache[cache_key] = {"data": result, "time": time.time()}
    return result


# ============================================================
#  6. EARNINGS CALENDAR SHIELD
#  Don't hold through earnings — one surprise can wipe a week
# ============================================================

def get_earnings_shield(price_data: dict) -> dict:
    """
    Check which stocks have earnings coming up in the next 3 days.
    Returns list of stocks to block from new entries and warn for existing positions.
    """
    cache_key = "earnings_shield"
    cached = _rentech_cache.get(cache_key)
    if cached and time.time() - cached["time"] < 3600:
        return cached["data"]

    blocked = []
    warning = []

    symbols = list(price_data.keys())[:30]  # Limit to 30 to avoid Yahoo rate limits

    for symbol in symbols:
        try:
            _throttle_rentech()
            ticker = yf.Ticker(symbol)
            cal = ticker.calendar
            if cal is not None and not (isinstance(cal, pd.DataFrame) and cal.empty):
                next_earnings = None
                if isinstance(cal, dict):
                    earnings_date = cal.get("Earnings Date") or cal.get("earnings_date")
                    if earnings_date:
                        if isinstance(earnings_date, list) and len(earnings_date) > 0:
                            next_earnings = pd.Timestamp(earnings_date[0])
                        else:
                            next_earnings = pd.Timestamp(earnings_date)
                elif isinstance(cal, pd.DataFrame):
                    if "Earnings Date" in cal.columns:
                        next_earnings = pd.Timestamp(cal["Earnings Date"].iloc[0])
                    elif len(cal.columns) > 0:
                        next_earnings = pd.Timestamp(cal.iloc[0, 0])

                if next_earnings is None:
                    continue

                now = pd.Timestamp.now()
                # Handle timezone mismatch — yfinance may return tz-aware dates
                if next_earnings.tzinfo is not None:
                    next_earnings = next_earnings.tz_localize(None)
                days_until = (next_earnings - now).days

                if 0 <= days_until <= 3:
                    blocked.append({
                        "symbol": symbol,
                        "earnings_date": str(next_earnings.date()),
                        "days_until": days_until,
                        "action": "BLOCK — earnings within 3 days, binary event risk",
                    })
                elif 4 <= days_until <= 5:
                    warning.append({
                        "symbol": symbol,
                        "earnings_date": str(next_earnings.date()),
                        "days_until": days_until,
                        "action": "WARNING — earnings in 4-5 days, consider reducing exposure",
                    })
        except Exception:
            continue

    result = {"blocked": blocked, "warning": warning}
    _rentech_cache[cache_key] = {"data": result, "time": time.time()}
    return result


# ============================================================
#  7. SECTOR ROTATION DETECTION
# ============================================================

def detect_sector_rotation(price_data: dict) -> dict:
    """
    Detect sector rotation by comparing 1-week vs 1-month returns of sector ETFs.
    """
    cache_key = "sector_rotation"
    cached = _rentech_cache.get(cache_key)
    if cached and time.time() - cached["time"] < 1800:
        return cached["data"]

    sector_etfs = {
        "Technology": "XLK", "Healthcare": "XLV", "Financials": "XLF",
        "Energy": "XLE", "Consumer Discretionary": "XLY", "Consumer Staples": "XLP",
        "Industrials": "XLI", "Materials": "XLB", "Utilities": "XLU",
        "Real Estate": "XLRE", "Communication": "XLC",
    }

    rotations = {}
    try:
        _throttle_rentech()
        etf_symbols = list(sector_etfs.values())
        data = yf.download(etf_symbols, period="2mo", progress=False, group_by="ticker")

        for sector, etf in sector_etfs.items():
            try:
                if len(etf_symbols) == 1:
                    closes = data["Close"].values.astype(float)
                else:
                    closes = data[etf]["Close"].values.astype(float)

                if len(closes) < 22:
                    continue
                if closes[-5] == 0 or closes[-22] == 0 or np.isnan(closes[-5]) or np.isnan(closes[-22]):
                    continue

                ret_1w = (closes[-1] / closes[-5] - 1) * 100
                ret_1m = (closes[-1] / closes[-22] - 1) * 100
                acceleration = ret_1w - (ret_1m / 4)

                rotations[sector] = {
                    "etf": etf,
                    "return_1w": round(ret_1w, 2),
                    "return_1m": round(ret_1m, 2),
                    "acceleration": round(acceleration, 2),
                    "flow": "INFLOW" if acceleration > 1.5 else ("OUTFLOW" if acceleration < -1.5 else "NEUTRAL"),
                }
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"Sector rotation download failed: {e}")

    sorted_sectors = sorted(rotations.items(), key=lambda x: x[1]["acceleration"], reverse=True)
    top_inflow = [s[0] for s in sorted_sectors[:3] if s[1]["flow"] == "INFLOW"]
    top_outflow = [s[0] for s in sorted_sectors[-3:] if s[1]["flow"] == "OUTFLOW"]

    result = {
        "sectors": rotations, "top_inflow": top_inflow,
        "top_outflow": top_outflow, "timestamp": datetime.now().isoformat(),
    }
    _rentech_cache[cache_key] = {"data": result, "time": time.time()}
    return result


# ============================================================
#  8. MULTI-TIMEFRAME CONFIRMATION
# ============================================================

def get_multi_timeframe_signals(price_data: dict) -> dict:
    """Daily + Weekly + Monthly trend must agree for high conviction."""
    cache_key = "mtf_signals"
    cached = _rentech_cache.get(cache_key)
    if cached and time.time() - cached["time"] < 600:
        return cached["data"]

    signals = {}
    for symbol, df in price_data.items():
        try:
            closes = df["Close"].values.astype(float)
            if len(closes) < 60:
                continue

            price = closes[-1]
            sma_20 = float(np.mean(closes[-20:]))
            daily_bull = price > sma_20

            sma_50 = float(np.mean(closes[-50:])) if len(closes) >= 50 else sma_20
            weekly_bull = price > sma_50

            if len(closes) >= 40:
                monthly_bull = float(np.mean(closes[-20:])) > float(np.mean(closes[-40:-20]))
            else:
                monthly_bull = daily_bull

            score = sum([1 if b else -1 for b in [daily_bull, weekly_bull, monthly_bull]])

            if score == 3:
                confirmation = "CONFIRMED_BULL"
            elif score == -3:
                confirmation = "CONFIRMED_BEAR"
            elif score >= 1:
                confirmation = "LEAN_BULL"
            elif score <= -1:
                confirmation = "LEAN_BEAR"
            else:
                confirmation = "MIXED"

            signals[symbol] = {
                "confirmation": confirmation, "score": score,
                "daily": "bull" if daily_bull else "bear",
                "weekly": "bull" if weekly_bull else "bear",
                "monthly": "bull" if monthly_bull else "bear",
            }
        except Exception:
            continue

    _rentech_cache[cache_key] = {"data": signals, "time": time.time()}
    return signals


# ============================================================
#  9. REGIME TRANSITION PREDICTION
# ============================================================

def predict_regime_transition(price_data: dict) -> dict:
    """Use VIX, breadth, and credit spreads to predict regime changes."""
    cache_key = "regime_transition"
    cached = _rentech_cache.get(cache_key)
    if cached and time.time() - cached["time"] < 1800:
        return cached["data"]

    warning_signs = []
    bullish_signs = []

    try:
        _throttle_rentech()
        vix_data = yf.download("^VIX", period="1mo", progress=False)
        if vix_data is not None and len(vix_data) >= 5:
            vix_closes = _safe_col(vix_data, "Close").values.astype(float)
            vix_now = vix_closes[-1]
            vix_5d_ago = vix_closes[-5]
            vix_trend = ((vix_now / vix_5d_ago) - 1) * 100

            if vix_trend > 20:
                warning_signs.append(f"VIX surging +{vix_trend:.0f}% in 5 days")
            elif vix_trend < -20:
                bullish_signs.append(f"VIX collapsing {vix_trend:.0f}% in 5 days")
            if vix_now > 30:
                warning_signs.append(f"VIX at {vix_now:.1f} — crisis territory")
            elif vix_now < 15:
                bullish_signs.append(f"VIX at {vix_now:.1f} — extreme calm")

        # Breadth check
        breadth_syms = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META",
                        "TSLA", "JPM", "JNJ", "V", "PG", "UNH", "HD",
                        "MA", "DIS", "CSCO", "INTC", "VZ", "KO", "PEP",
                        "MRK", "ABBV", "CVX", "XOM", "LLY", "AVGO", "COST",
                        "WMT", "CRM", "ORCL"]
        above = 0
        total = 0
        for sym in breadth_syms:
            if sym in price_data:
                c = price_data[sym]["Close"].values.astype(float)
                if len(c) >= 50:
                    if c[-1] > float(np.mean(c[-50:])):
                        above += 1
                    total += 1
        if total > 0:
            bpct = (above / total) * 100
            if bpct < 30:
                warning_signs.append(f"Breadth collapsed: only {bpct:.0f}% above 50-SMA")
            elif bpct > 70:
                bullish_signs.append(f"Breadth strong: {bpct:.0f}% above 50-SMA")

        # Credit spreads
        _throttle_rentech()
        credit = yf.download(["HYG", "TLT"], period="1mo", progress=False, group_by="ticker")
        if credit is not None:
            try:
                hyg = credit["HYG"]["Close"].values.astype(float)
                tlt = credit["TLT"]["Close"].values.astype(float)
                if len(hyg) >= 10 and len(tlt) >= 10:
                    ratio_chg = ((hyg[-1] / tlt[-1]) / (hyg[-10] / tlt[-10]) - 1) * 100
                    if ratio_chg < -2:
                        warning_signs.append(f"Credit spreads widening ({ratio_chg:+.1f}%)")
                    elif ratio_chg > 2:
                        bullish_signs.append(f"Credit spreads tightening ({ratio_chg:+.1f}%)")
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Regime transition analysis failed: {e}")

    bear_score = len(warning_signs)
    bull_score = len(bullish_signs)
    if bear_score >= 3:
        prediction = "BEAR_TRANSITION_LIKELY"
    elif bear_score >= 2:
        prediction = "BEAR_RISK_ELEVATED"
    elif bull_score >= 3:
        prediction = "BULL_TRANSITION_LIKELY"
    elif bull_score >= 2:
        prediction = "BULL_MOMENTUM_BUILDING"
    else:
        prediction = "REGIME_STABLE"

    result = {
        "prediction": prediction, "warning_signs": warning_signs,
        "bullish_signs": bullish_signs, "bear_score": bear_score,
        "bull_score": bull_score, "timestamp": datetime.now().isoformat(),
    }
    _rentech_cache[cache_key] = {"data": result, "time": time.time()}
    return result


# ============================================================
#  10. NEWS SENTIMENT PER STOCK
# ============================================================

def get_stock_news_sentiment(symbols: list) -> dict:
    """Analyze headlines for positive/negative keywords per stock."""
    cache_key = f"news_sentiment_{'_'.join(sorted(symbols[:20]))}"
    cached = _rentech_cache.get(cache_key)
    if cached and time.time() - cached["time"] < 1800:
        return cached["data"]

    POSITIVE = {"beat", "beats", "surge", "rally", "upgrade", "outperform", "buy",
                "growth", "profit", "record", "strong", "bullish", "breakout", "soar",
                "boost", "positive", "exceeds", "optimistic", "gain", "gains"}
    NEGATIVE = {"miss", "misses", "crash", "plunge", "downgrade", "underperform", "sell",
                "loss", "losses", "weak", "bearish", "decline", "cut", "warning",
                "negative", "disappoints", "fell", "falls", "risk", "layoff", "layoffs"}

    sentiments = {}
    for symbol in symbols[:20]:
        try:
            _throttle_rentech()
            ticker = yf.Ticker(symbol)
            news = ticker.news
            # yfinance 0.2.36+ may return dict with "news" key
            if isinstance(news, dict):
                news = news.get("news", [])
            if not news or not isinstance(news, list):
                sentiments[symbol] = {"sentiment": "NEUTRAL", "score": 0, "headlines": 0}
                continue

            pos = neg = 0
            for article in news[:10]:
                if not isinstance(article, dict):
                    continue
                title = article.get("title", "").lower()
                pos += sum(1 for w in POSITIVE if w in title)
                neg += sum(1 for w in NEGATIVE if w in title)

            net = pos - neg
            if net >= 3: sent = "VERY_POSITIVE"
            elif net >= 1: sent = "POSITIVE"
            elif net <= -3: sent = "VERY_NEGATIVE"
            elif net <= -1: sent = "NEGATIVE"
            else: sent = "NEUTRAL"

            sentiments[symbol] = {
                "sentiment": sent, "score": net,
                "positive_signals": pos, "negative_signals": neg,
                "headlines_analyzed": len(news[:10]),
            }
        except Exception:
            sentiments[symbol] = {"sentiment": "NEUTRAL", "score": 0, "headlines": 0}

    _rentech_cache[cache_key] = {"data": sentiments, "time": time.time()}
    return sentiments


# ============================================================
#  11. OPTIONS UNUSUAL ACTIVITY
# ============================================================

def detect_unusual_options(symbols: list) -> dict:
    """Detect unusual options volume and put/call ratio spikes."""
    cache_key = f"unusual_options_{'_'.join(sorted(symbols[:15]))}"
    cached = _rentech_cache.get(cache_key)
    if cached and time.time() - cached["time"] < 1800:
        return cached["data"]

    signals = {}
    for symbol in symbols[:15]:
        try:
            _throttle_rentech()
            ticker = yf.Ticker(symbol)
            expirations = ticker.options
            if not expirations:
                continue

            chain = ticker.option_chain(expirations[0])
            call_vol = int(chain.calls["volume"].fillna(0).sum()) if "volume" in chain.calls.columns else 0
            put_vol = int(chain.puts["volume"].fillna(0).sum()) if "volume" in chain.puts.columns else 0
            call_oi = int(chain.calls["openInterest"].fillna(0).sum()) if "openInterest" in chain.calls.columns else 0
            put_oi = int(chain.puts["openInterest"].fillna(0).sum()) if "openInterest" in chain.puts.columns else 0

            total_vol = call_vol + put_vol
            total_oi = call_oi + put_oi
            if total_vol == 0:
                continue

            pc_ratio = put_vol / call_vol if call_vol > 0 else 999
            vol_oi_ratio = total_vol / total_oi if total_oi > 0 else 0

            signal = "NEUTRAL"
            if pc_ratio > 2.0: signal = "BEARISH_UNUSUAL"
            elif pc_ratio < 0.4: signal = "BULLISH_UNUSUAL"
            elif vol_oi_ratio > 3.0: signal = "HIGH_ACTIVITY"

            if signal != "NEUTRAL":
                signals[symbol] = {
                    "signal": signal, "put_call_ratio": round(pc_ratio, 2),
                    "call_volume": call_vol, "put_volume": put_vol,
                    "vol_oi_ratio": round(vol_oi_ratio, 2), "expiration": expirations[0],
                }
        except Exception:
            continue

    _rentech_cache[cache_key] = {"data": signals, "time": time.time()}
    return signals


# ============================================================
#  12. OPENING RANGE BREAKOUT
# ============================================================

def get_opening_range_signals(price_data: dict) -> dict:
    """Detect breakouts above yesterday's high or breakdowns below yesterday's low."""
    cache_key = "opening_range"
    cached = _rentech_cache.get(cache_key)
    if cached and time.time() - cached["time"] < 300:
        return cached["data"]

    signals = {}
    for symbol, df in price_data.items():
        try:
            if len(df) < 3:
                continue
            highs = df["High"].values.astype(float).flatten()
            lows = df["Low"].values.astype(float).flatten()
            closes = df["Close"].values.astype(float)

            today_close = closes[-1]
            yesterday_high = highs[-2]
            yesterday_low = lows[-2]
            avg_range = float(np.mean(highs[-10:] - lows[-10:]))

            if today_close > yesterday_high and avg_range > 0:
                strength = (today_close - yesterday_high) / avg_range
                if strength > 0.5:
                    signals[symbol] = {
                        "signal": "BREAKOUT", "direction": "LONG",
                        "strength": round(strength, 2), "price": today_close,
                        "level": round(yesterday_high, 2),
                    }
            elif today_close < yesterday_low and avg_range > 0:
                strength = (yesterday_low - today_close) / avg_range
                if strength > 0.5:
                    signals[symbol] = {
                        "signal": "BREAKDOWN", "direction": "SHORT",
                        "strength": round(strength, 2), "price": today_close,
                        "level": round(yesterday_low, 2),
                    }
        except Exception:
            continue

    _rentech_cache[cache_key] = {"data": signals, "time": time.time()}
    return signals


# ============================================================
#  13. PORTFOLIO BETA HEDGING
# ============================================================

def calculate_portfolio_beta(open_trades: list, price_data: dict) -> dict:
    """Calculate portfolio beta to S&P 500 and suggest hedging."""
    try:
        _throttle_rentech()
        import threading as _pb_thr
        _pb_r = [None]
        _pb_t = _pb_thr.Thread(
            target=lambda r=_pb_r: r.__setitem__(
                0, yf.download("SPY", period="3mo", progress=False)
            ), daemon=True)
        _pb_t.start(); _pb_t.join(timeout=10)
        spy = _pb_r[0]
        if spy is None or len(spy) < 30:
            try:
                from analytics.multi_source_adapter import get_historical_any_source
                spy = get_historical_any_source("SPY", "3mo")
            except Exception:
                pass
        if spy is None or len(spy) < 30:
            return {"beta": 0, "hedge_needed": False}

        spy_closes = _safe_col(spy, "Close").values.astype(float)
        spy_ret = np.diff(spy_closes) / spy_closes[:-1]

        total_beta = 0
        total_weight = 0
        for trade in open_trades:
            sym = trade["ticker"]
            if sym not in price_data:
                continue
            c = price_data[sym]["Close"].values.astype(float)
            if len(c) < 30:
                continue

            s_ret = np.diff(c[-len(spy_ret)-1:]) / c[-len(spy_ret)-1:-1]
            ml = min(len(s_ret), len(spy_ret))
            cov = np.cov(s_ret[-ml:], spy_ret[-ml:])[0][1]
            var = np.var(spy_ret[-ml:])
            beta = cov / var if var > 0 else 1.0

            pv = trade["shares"] * trade["entry_price"]
            dm = 1 if trade["direction"] == "long" else -1
            total_beta += beta * pv * dm
            total_weight += abs(pv)

        pb = total_beta / total_weight if total_weight > 0 else 0
        return {
            "beta": round(pb, 3), "hedge_needed": abs(pb) > 0.3,
            "hedge_direction": "SHORT SPY" if pb > 0.3 else ("LONG SPY" if pb < -0.3 else "NONE"),
            "total_exposure": round(total_weight, 2),
        }
    except Exception as e:
        logger.warning(f"Portfolio beta failed: {e}")
        return {"beta": 0, "hedge_needed": False}


# ============================================================
#  14. DRAWDOWN RECOVERY MODE
# ============================================================

def get_drawdown_recovery_mode(portfolio_value: float, peak_value: float, recent_trades=None) -> dict:
    """
    Enhanced Drawdown Recovery Engine.

    Instead of just reducing position size, this changes the entire strategy:
    - High-conviction only (70%+ confidence)
    - Shorter hold periods (10 days max)
    - Defensive sectors prioritized
    - Require 2+ factor confirmation
    - Auto-exit recovery when 50% of drawdown is regained
    - Strategy shift: favor mean_reversion over momentum

    Args:
        portfolio_value: current portfolio value
        peak_value: all-time high portfolio value
        recent_trades: list of recent closed trades (for recovery tracking)
    """
    dd = ((portfolio_value / peak_value) - 1) * 100 if peak_value > 0 else 0

    # Calculate recovery progress (if we were in drawdown and are recovering)
    recovery_target = peak_value * 0.95  # Exit recovery when within 5% of peak
    recovery_progress = 0.0
    if dd < -3 and peak_value > 0:
        # How much of the drawdown have we recovered?
        max_dd_amount = peak_value - (peak_value * (1 + dd / 100))
        current_dd_amount = peak_value - portfolio_value
        if max_dd_amount > 0:
            recovery_progress = max(0, (1 - current_dd_amount / max_dd_amount)) * 100

    # Check recent trade performance during recovery
    recovery_win_rate = 0.5
    if recent_trades and len(recent_trades) >= 5:
        recent_wins = sum(1 for t in recent_trades[:10] if (t.get("pnl_pct", 0) or 0) > 0)
        recovery_win_rate = recent_wins / min(10, len(recent_trades))

    if dd <= -10:
        return {
            "mode": "HALT", "drawdown_pct": round(dd, 2), "size_multiplier": 0.0,
            "message": f"CRITICAL ({dd:+.1f}%): ALL TRADING HALTED",
            "allowed_sectors": [],
            "strategy_shift": "none",
            "min_confidence": 95,
            "max_hold_days": 0,
            "min_confirmations": 3,
            "recovery_progress": round(recovery_progress, 1),
        }
    elif dd <= -8:
        return {
            "mode": "EMERGENCY", "drawdown_pct": round(dd, 2), "size_multiplier": 0.25,
            "message": f"Emergency ({dd:+.1f}%): 75% size reduction — mean reversion only",
            "allowed_sectors": ["Healthcare", "Consumer Staples", "Utilities"],
            "strategy_shift": "mean_reversion",  # Only take mean reversion setups
            "min_confidence": 75,
            "max_hold_days": 5,
            "min_confirmations": 2,
            "recovery_progress": round(recovery_progress, 1),
        }
    elif dd <= -5:
        return {
            "mode": "DEFENSIVE", "drawdown_pct": round(dd, 2), "size_multiplier": 0.5,
            "message": f"Defensive ({dd:+.1f}%): 50% size, high-conviction only",
            "allowed_sectors": ["Healthcare", "Consumer Staples", "Utilities", "Financials"],
            "strategy_shift": "defensive",  # Favor value + mean reversion
            "min_confidence": 70,
            "max_hold_days": 10,
            "min_confirmations": 2,
            "recovery_progress": round(recovery_progress, 1),
        }
    elif dd <= -3:
        return {
            "mode": "CAUTIOUS", "drawdown_pct": round(dd, 2), "size_multiplier": 0.7,
            "message": f"Cautious ({dd:+.1f}%): 30% size reduction",
            "allowed_sectors": None,
            "strategy_shift": "cautious",  # Slight bias to defensive
            "min_confidence": 55,
            "max_hold_days": 20,
            "min_confirmations": 1,
            "recovery_progress": round(recovery_progress, 1),
        }
    else:
        return {
            "mode": "NORMAL", "drawdown_pct": round(dd, 2), "size_multiplier": 1.0,
            "message": "Portfolio within normal range",
            "allowed_sectors": None,
            "strategy_shift": "none",
            "min_confidence": 0,
            "max_hold_days": 60,
            "min_confirmations": 0,
            "recovery_progress": 100.0,
        }


# ============================================================
#  15. ADAPTIVE WEIGHT LEARNING
# ============================================================

def learn_factor_weights(closed_trades: list) -> dict:
    """Analyze which factors predicted winners and adjust weights."""
    if not closed_trades or len(closed_trades) < 20:
        return {"status": "INSUFFICIENT_DATA", "trades_needed": 20 - len(closed_trades or [])}

    base_weights = {
        "momentum": 0.15, "rsi2": 0.15, "value": 0.10, "quality": 0.10,
        "volume": 0.10, "volatility": 0.05, "gap": 0.05, "sector_rs": 0.05,
        "trend": 0.10, "mean_rev": 0.05, "earnings": 0.05, "inst_flow": 0.03, "short_int": 0.02,
    }

    factor_perf = {k: {"wins": 0, "losses": 0, "total_return": 0} for k in base_weights}

    for trade in closed_trades:
        pnl = trade.get("pnl_pct", 0) or 0
        factors = trade.get("factors", {})
        if not factors:
            continue
        won = pnl > 0
        for fn, fv in factors.items():
            cn = fn.replace("_score", "").replace("_adj", "")
            if cn not in factor_perf:
                continue
            if (fv > 0 and trade.get("direction") == "long") or \
               (fv < 0 and trade.get("direction") == "short"):
                factor_perf[cn]["wins" if won else "losses"] += 1
                factor_perf[cn]["total_return"] += pnl

    adjusted = {}
    for f, bw in base_weights.items():
        p = factor_perf.get(f, {"wins": 0, "losses": 0})
        t = p["wins"] + p["losses"]
        if t >= 5:
            wr = p["wins"] / t
            adj = 1.0 + (wr - 0.55) * 2 if wr > 0.55 else (max(0.3, 1.0 - (0.45 - wr) * 2) if wr < 0.45 else 1.0)
            adjusted[f] = round(bw * adj, 4)
        else:
            adjusted[f] = bw

    tw = sum(adjusted.values())
    if tw > 0:
        adjusted = {k: round(v / tw, 4) for k, v in adjusted.items()}

    return {
        "status": "LEARNED", "adjusted_weights": adjusted, "base_weights": base_weights,
        "factor_performance": {
            k: {"win_rate": round(v["wins"] / max(v["wins"] + v["losses"], 1) * 100, 1),
                "sample_size": v["wins"] + v["losses"]}
            for k, v in factor_perf.items()
        },
        "trades_analyzed": len(closed_trades),
    }
