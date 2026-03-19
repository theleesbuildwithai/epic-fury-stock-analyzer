"""
Technical Analysis Engine — the brain of our stock analyzer.

This calculates all the math-based indicators that traders use to
figure out if a stock might go up or down. Think of these like
"health stats" for a stock — just like a doctor checks your heart rate
and blood pressure, we check RSI, MACD, and Bollinger Bands.
"""

import pandas as pd
import numpy as np
from scipy.stats import norm


def calculate_sma(prices: list, window: int) -> list:
    """
    Simple Moving Average — the average price over the last N days.
    Smooths out the noise so you can see the real trend.
    """
    series = pd.Series(prices)
    sma = series.rolling(window=window).mean()
    return [round(x, 2) if not pd.isna(x) else None for x in sma]


def calculate_ema(prices: list, span: int) -> list:
    """
    Exponential Moving Average — like SMA but gives more weight to recent prices.
    Reacts faster to price changes than SMA.
    """
    series = pd.Series(prices)
    ema = series.ewm(span=span, adjust=False).mean()
    return [round(x, 2) for x in ema]


def calculate_rsi(prices: list, period: int = 14) -> list:
    """
    Relative Strength Index (RSI) — measures if a stock is overbought or oversold.
    - Above 70 = overbought (might go down)
    - Below 30 = oversold (might go up)
    - Around 50 = neutral

    Uses Wilder's smoothing method (industry standard, matches TradingView/Bloomberg).
    """
    series = pd.Series(prices)
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)

    # Wilder's smoothing: first value is SMA, then exponential with alpha=1/period
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return [round(x, 2) if not pd.isna(x) else None for x in rsi]


def calculate_macd(prices: list) -> dict:
    """
    MACD (Moving Average Convergence Divergence) — shows momentum shifts.
    When the MACD line crosses above the signal line = bullish signal.
    When it crosses below = bearish signal.
    """
    series = pd.Series(prices)
    ema_12 = series.ewm(span=12, adjust=False).mean()
    ema_26 = series.ewm(span=26, adjust=False).mean()
    macd_line = ema_12 - ema_26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    histogram = macd_line - signal_line

    return {
        "macd_line": [round(x, 4) for x in macd_line],
        "signal_line": [round(x, 4) for x in signal_line],
        "histogram": [round(x, 4) for x in histogram],
    }


def calculate_bollinger_bands(prices: list, window: int = 20, num_std: float = 2.0) -> dict:
    """
    Bollinger Bands — creates an envelope around the price.
    When price touches the upper band = might be too high.
    When price touches the lower band = might be too low.
    When the bands squeeze tight = big move coming soon.
    """
    series = pd.Series(prices)
    sma = series.rolling(window=window).mean()
    std = series.rolling(window=window).std()
    upper = sma + (std * num_std)
    lower = sma - (std * num_std)

    return {
        "upper": [round(x, 2) if not pd.isna(x) else None for x in upper],
        "middle": [round(x, 2) if not pd.isna(x) else None for x in sma],
        "lower": [round(x, 2) if not pd.isna(x) else None for x in lower],
    }


def calculate_support_resistance(prices: list, window: int = 20) -> dict:
    """
    Support and Resistance — key price levels where the stock tends to bounce.
    Support = floor (price bounces up from here)
    Resistance = ceiling (price bounces down from here)
    """
    if len(prices) < window:
        return {"support": [], "resistance": []}

    series = pd.Series(prices)
    supports = []
    resistances = []

    for i in range(window, len(prices) - window):
        segment = prices[i - window:i + window + 1]
        current = prices[i]
        if current == min(segment):
            supports.append(round(current, 2))
        if current == max(segment):
            resistances.append(round(current, 2))

    # Deduplicate nearby levels (within 1% of each other)
    supports = _dedupe_levels(supports)
    resistances = _dedupe_levels(resistances)

    return {
        "support": supports[-3:] if len(supports) > 3 else supports,
        "resistance": resistances[-3:] if len(resistances) > 3 else resistances,
    }


def _dedupe_levels(levels: list, threshold: float = 0.01) -> list:
    """Remove levels that are within threshold % of each other."""
    if not levels:
        return []
    result = [levels[0]]
    for level in levels[1:]:
        if abs(level - result[-1]) / result[-1] > threshold:
            result.append(level)
    return result


def calculate_volume_analysis(volumes: list) -> dict:
    """
    Volume Analysis — tells you if moves are "real" or not.
    Big price move + high volume = real move.
    Big price move + low volume = possibly fake-out.
    """
    if not volumes:
        return {}

    avg_volume = int(np.mean(volumes))
    recent_avg = int(np.mean(volumes[-5:])) if len(volumes) >= 5 else avg_volume
    latest_volume = volumes[-1]

    volume_ratio = round(latest_volume / avg_volume, 2) if avg_volume > 0 else 0
    volume_trend = "increasing" if recent_avg > avg_volume * 1.1 else (
        "decreasing" if recent_avg < avg_volume * 0.9 else "stable"
    )
    unusual = latest_volume > avg_volume * 1.5

    return {
        "latest_volume": latest_volume,
        "avg_volume_20d": avg_volume,
        "recent_avg_5d": recent_avg,
        "volume_ratio": volume_ratio,
        "volume_trend": volume_trend,
        "unusual_volume": unusual,
    }


def calculate_risk_score(prices: list, rsi_values: list, volumes: list) -> dict:
    """
    Risk Score — a composite 1-10 score of how risky this stock is right now.
    Higher score = more risky.

    Factors:
    - Volatility (how much the price jumps around)
    - RSI extremes (overbought/oversold)
    - Trend strength (trending or choppy?)
    """
    if len(prices) < 20:
        return {"score": 5, "label": "Moderate", "factors": []}

    factors = []

    # 1. Volatility (standard deviation of daily returns)
    returns = pd.Series(prices).pct_change().dropna()
    volatility = returns.std() * np.sqrt(252) * 100  # Annualized
    vol_score = min(10, max(1, int(volatility / 5)))
    factors.append({"name": "Volatility", "score": vol_score,
                     "detail": f"{volatility:.1f}% annualized"})

    # 2. RSI extremes
    latest_rsi = next((r for r in reversed(rsi_values) if r is not None), 50)
    rsi_score = 1
    if latest_rsi > 80 or latest_rsi < 20:
        rsi_score = 9
    elif latest_rsi > 70 or latest_rsi < 30:
        rsi_score = 7
    elif latest_rsi > 60 or latest_rsi < 40:
        rsi_score = 4
    else:
        rsi_score = 2
    factors.append({"name": "RSI Extreme", "score": rsi_score,
                     "detail": f"RSI at {latest_rsi:.1f}"})

    # 3. Trend consistency
    sma_20 = pd.Series(prices).rolling(20).mean().iloc[-1]
    price = prices[-1]
    trend_deviation = abs(price - sma_20) / sma_20 * 100
    trend_score = min(10, max(1, int(trend_deviation / 2)))
    factors.append({"name": "Trend Deviation", "score": trend_score,
                     "detail": f"{trend_deviation:.1f}% from 20-day avg"})

    # Composite score (weighted average)
    composite = (vol_score * 0.4 + rsi_score * 0.35 + trend_score * 0.25)
    composite = round(min(10, max(1, composite)), 1)

    label = "Low" if composite <= 3 else "Moderate" if composite <= 6 else "High" if composite <= 8 else "Very High"

    return {
        "score": composite,
        "label": label,
        "factors": factors,
    }


def determine_trend(prices: list) -> dict:
    """
    Determines the overall trend direction and strength.
    Uses the relationship between short-term and long-term moving averages.
    """
    if len(prices) < 50:
        return {"direction": "insufficient_data", "strength": 0}

    sma_20 = pd.Series(prices).rolling(20).mean().iloc[-1]
    sma_50 = pd.Series(prices).rolling(50).mean().iloc[-1]
    current_price = prices[-1]

    if current_price > sma_20 > sma_50:
        direction = "bullish"
        strength = min(100, int(((current_price - sma_50) / sma_50) * 100 * 5))
    elif current_price < sma_20 < sma_50:
        direction = "bearish"
        strength = min(100, int(((sma_50 - current_price) / sma_50) * 100 * 5))
    elif current_price > sma_20:
        direction = "slightly_bullish"
        strength = min(100, int(((current_price - sma_20) / sma_20) * 100 * 10))
    elif current_price < sma_20:
        direction = "slightly_bearish"
        strength = min(100, int(((sma_20 - current_price) / sma_20) * 100 * 10))
    else:
        direction = "neutral"
        strength = 0

    return {
        "direction": direction,
        "strength": min(100, max(0, strength)),
        "price_vs_sma20": round(((current_price / sma_20) - 1) * 100, 2),
        "price_vs_sma50": round(((current_price / sma_50) - 1) * 100, 2),
        "sma20": round(sma_20, 2),
        "sma50": round(sma_50, 2),
    }


def calculate_pivot_points(prices: list) -> dict:
    """
    Classic Pivot Points — used by institutional traders to identify
    key intraday and swing trade levels.

    Standard pivot: P = (H + L + C) / 3
    Support/Resistance levels derived from pivot.
    """
    if len(prices) < 2:
        return {}

    # Use last trading day's OHLC approximation from close prices
    # For more accuracy we'd need actual OHLC, but closes work for swing levels
    recent = prices[-20:]
    high = max(recent)
    low = min(recent)
    close = prices[-1]

    pivot = round((high + low + close) / 3, 2)
    r1 = round(2 * pivot - low, 2)
    r2 = round(pivot + (high - low), 2)
    r3 = round(high + 2 * (pivot - low), 2)
    s1 = round(2 * pivot - high, 2)
    s2 = round(pivot - (high - low), 2)
    s3 = round(low - 2 * (high - pivot), 2)

    # Fibonacci pivot points
    fib_r1 = round(pivot + 0.382 * (high - low), 2)
    fib_r2 = round(pivot + 0.618 * (high - low), 2)
    fib_r3 = round(pivot + 1.0 * (high - low), 2)
    fib_s1 = round(pivot - 0.382 * (high - low), 2)
    fib_s2 = round(pivot - 0.618 * (high - low), 2)
    fib_s3 = round(pivot - 1.0 * (high - low), 2)

    # Price position relative to pivot
    position = "above" if close > pivot else "below" if close < pivot else "at"

    return {
        "pivot": pivot,
        "r1": r1, "r2": r2, "r3": r3,
        "s1": s1, "s2": s2, "s3": s3,
        "fib_r1": fib_r1, "fib_r2": fib_r2, "fib_r3": fib_r3,
        "fib_s1": fib_s1, "fib_s2": fib_s2, "fib_s3": fib_s3,
        "position": position,
        "high_20d": high,
        "low_20d": low,
    }


def calculate_hold_duration(prices: list, rsi_values: list, trend: dict, forecast: dict) -> dict:
    """
    Recommended Hold Duration — calculates optimal holding period
    based on technical indicators, trend strength, and forecast data.

    Minimum 1 week (investing, not day trading).
    Uses EMA crossover signals, RSI mean reversion timing,
    and forecast probability windows.
    """
    if len(prices) < 60:
        return {"days": 30, "label": "1 Month", "reasoning": "Insufficient data for precise timing"}

    series = pd.Series(prices)
    latest_rsi = next((r for r in reversed(rsi_values) if r is not None), 50)

    # EMA analysis for timing
    ema_9 = series.ewm(span=9, adjust=False).mean()
    ema_21 = series.ewm(span=21, adjust=False).mean()
    ema_50 = series.ewm(span=50, adjust=False).mean()

    ema_9_val = float(ema_9.iloc[-1])
    ema_21_val = float(ema_21.iloc[-1])
    ema_50_val = float(ema_50.iloc[-1])
    current = prices[-1]

    reasons = []
    hold_days = 14  # Start with 2-week default (minimum investing timeframe)

    # Factor 1: RSI-based timing
    if latest_rsi < 25:
        # Deeply oversold — hold longer for recovery
        hold_days += 30
        reasons.append(f"RSI deeply oversold ({latest_rsi:.0f}) — hold for full recovery")
    elif latest_rsi < 35:
        hold_days += 14
        reasons.append(f"RSI oversold ({latest_rsi:.0f}) — hold for bounce")
    elif latest_rsi > 75:
        # Overbought — shorter hold, take profits soon
        hold_days = max(7, hold_days - 7)
        reasons.append(f"RSI overbought ({latest_rsi:.0f}) — consider taking profits")
    elif latest_rsi > 65:
        hold_days = max(7, hold_days - 3)
        reasons.append(f"RSI elevated ({latest_rsi:.0f}) — watch for pullback")

    # Factor 2: EMA alignment (trend strength)
    if current > ema_9_val > ema_21_val > ema_50_val:
        # Perfect bullish alignment — ride the trend
        hold_days += 21
        reasons.append("Strong EMA alignment (9>21>50) — ride the uptrend")
    elif current < ema_9_val < ema_21_val < ema_50_val:
        # Perfect bearish — hold short or avoid
        hold_days = max(7, hold_days - 7)
        reasons.append("Bearish EMA alignment — shorter hold recommended")
    elif current > ema_21_val:
        hold_days += 7
        reasons.append("Price above 21-day EMA — moderate uptrend")

    # Factor 3: Trend strength from main analysis
    direction = trend.get("direction", "neutral")
    strength = trend.get("strength", 0)
    if direction == "bullish" and strength > 60:
        hold_days += 14
        reasons.append(f"Strong bullish trend ({strength}% strength)")
    elif direction == "bearish" and strength > 60:
        hold_days = max(7, hold_days - 7)
        reasons.append(f"Strong bearish trend — minimize exposure")

    # Factor 4: Volatility-based adjustment
    returns = series.pct_change().dropna()
    vol = float(returns.std()) * np.sqrt(252) * 100
    if vol > 50:
        # High volatility — shorter hold to manage risk
        hold_days = max(7, int(hold_days * 0.7))
        reasons.append(f"High volatility ({vol:.0f}%) — shorter hold to manage risk")
    elif vol < 20:
        # Low volatility — can hold longer
        hold_days += 14
        reasons.append(f"Low volatility ({vol:.0f}%) — safe for longer hold")

    # Factor 5: Best forecast window
    if forecast and "forecasts" in forecast:
        best_prob = 0
        best_tf = None
        for fc in forecast["forecasts"]:
            if fc["prob_up"] > best_prob and fc["days"] >= 7:
                best_prob = fc["prob_up"]
                best_tf = fc
        if best_tf and best_prob > 55:
            reasons.append(f"Best probability window: {best_tf['timeframe']} ({best_prob}% up)")

    # Ensure minimum 7 days
    hold_days = max(7, min(180, hold_days))

    # Generate label
    if hold_days <= 10:
        label = "1-2 Weeks"
    elif hold_days <= 21:
        label = "2-3 Weeks"
    elif hold_days <= 35:
        label = "1 Month"
    elif hold_days <= 60:
        label = "1-2 Months"
    elif hold_days <= 95:
        label = "2-3 Months"
    else:
        label = "3-6 Months"

    # Entry/exit guidance
    entry_guidance = "Buy at current levels"
    exit_guidance = f"Target exit in ~{hold_days} days"

    if latest_rsi > 70:
        entry_guidance = "Wait for pullback before entering"
    elif latest_rsi < 30:
        entry_guidance = "Strong entry point — oversold"

    if current < ema_21_val:
        entry_guidance = "Wait for price to reclaim 21-day EMA"

    return {
        "days": hold_days,
        "label": label,
        "entry_guidance": entry_guidance,
        "exit_guidance": exit_guidance,
        "reasoning": reasons[:4],
        "ema_9": round(ema_9_val, 2),
        "ema_21": round(ema_21_val, 2),
        "ema_50": round(ema_50_val, 2),
    }


def calculate_price_forecast(prices: list, trend: dict) -> dict:
    """
    Price Forecast — uses historical volatility and statistical modeling
    to estimate the probability of a stock moving up or down over
    specific timeframes (7, 14, 30, 60, 90, 180 days).

    Based on geometric Brownian motion with enhancements for long-term accuracy:
    - Trend decay: short-term trend bias fades over longer horizons
    - Mean reversion: long-term drift blends toward historical market average
    - Dual volatility: recent vol for short-term, full-history vol for long-term
    """
    if len(prices) < 60:
        return {"error": "Need at least 60 days of data for reliable forecasts"}

    current_price = prices[-1]

    # Calculate daily log returns (log returns are more statistically sound)
    series = pd.Series(prices)
    log_returns = np.log(series / series.shift(1)).dropna()

    # Full-history stats
    daily_mean_full = float(log_returns.mean())
    daily_std_full = float(log_returns.std())

    # Recent 60-day stats (captures current market regime)
    recent_returns = log_returns.iloc[-60:] if len(log_returns) >= 60 else log_returns
    daily_std_recent = float(recent_returns.std())

    # Long-term market average drift (~8% annualized = ~0.000317 daily)
    market_avg_daily = np.log(1.08) / 252

    # Trend adjustment base (before decay)
    trend_dir = trend.get("direction", "neutral")
    if trend_dir == "bullish":
        trend_base = daily_std_recent * 0.15
    elif trend_dir == "slightly_bullish":
        trend_base = daily_std_recent * 0.08
    elif trend_dir == "bearish":
        trend_base = -daily_std_recent * 0.15
    elif trend_dir == "slightly_bearish":
        trend_base = -daily_std_recent * 0.08
    else:
        trend_base = 0.0

    timeframes = [
        {"label": "7 days", "days": 7},
        {"label": "14 days", "days": 14},
        {"label": "30 days", "days": 30},
        {"label": "60 days", "days": 60},
        {"label": "90 days", "days": 90},
        {"label": "180 days", "days": 180},
    ]

    forecasts = []
    for tf in timeframes:
        days = tf["days"]

        # --- Trend decay: trend influence fades with time ---
        # Full weight at 7d, half at 30d, ~15% at 90d, near-zero at 180d
        trend_decay = np.exp(-0.025 * days)
        trend_adjustment = trend_base * trend_decay

        # --- Mean reversion: blend stock's mean toward market average ---
        # Short-term (<=14d): 100% stock's own mean
        # Long-term (180d): ~60% market average, 40% stock's own mean
        reversion_weight = min(0.6, max(0.0, (days - 14) / 280))
        blended_mean = (1 - reversion_weight) * daily_mean_full + reversion_weight * market_avg_daily

        adjusted_mean = blended_mean + trend_adjustment

        # --- Dual volatility: blend recent and full-history vol ---
        # Short-term (<=14d): 80% recent, 20% full
        # Long-term (180d): 30% recent, 70% full
        recent_weight = max(0.3, 1.0 - (days / 250))
        blended_std = recent_weight * daily_std_recent + (1 - recent_weight) * daily_std_full

        # Scale mean and std to the timeframe
        tf_mean = adjusted_mean * days
        tf_std = blended_std * np.sqrt(days)

        # Probability of going up (log return > 0)
        prob_up = float(1 - norm.cdf(0, loc=tf_mean, scale=tf_std))
        prob_down = 1 - prob_up

        # Probability of specific percentage moves
        thresholds = [5, 10, 15, 20, 25] if days >= 60 else [5, 10, 15]
        prob_up_by = {}
        prob_down_by = {}
        for pct in thresholds:
            log_threshold = np.log(1 + pct / 100)
            prob_up_by[f"+{pct}%"] = round(
                float(1 - norm.cdf(log_threshold, loc=tf_mean, scale=tf_std)) * 100, 1
            )
            log_threshold_down = np.log(1 - pct / 100)
            prob_down_by[f"-{pct}%"] = round(
                float(norm.cdf(log_threshold_down, loc=tf_mean, scale=tf_std)) * 100, 1
            )

        # Price targets (percentile-based)
        bear_return = float(norm.ppf(0.25, loc=tf_mean, scale=tf_std))
        base_return = float(norm.ppf(0.50, loc=tf_mean, scale=tf_std))
        bull_return = float(norm.ppf(0.75, loc=tf_mean, scale=tf_std))

        bear_price = round(current_price * np.exp(bear_return), 2)
        base_price = round(current_price * np.exp(base_return), 2)
        bull_price = round(current_price * np.exp(bull_return), 2)

        bear_pct = round((np.exp(bear_return) - 1) * 100, 2)
        base_pct = round((np.exp(base_return) - 1) * 100, 2)
        bull_pct = round((np.exp(bull_return) - 1) * 100, 2)

        forecasts.append({
            "timeframe": tf["label"],
            "days": days,
            "prob_up": round(prob_up * 100, 1),
            "prob_down": round(prob_down * 100, 1),
            "prob_up_by": prob_up_by,
            "prob_down_by": prob_down_by,
            "targets": {
                "bull": {"price": bull_price, "pct": bull_pct},
                "base": {"price": base_price, "pct": base_pct},
                "bear": {"price": bear_price, "pct": bear_pct},
            },
        })

    return {
        "current_price": current_price,
        "annualized_volatility": round(daily_std_full * np.sqrt(252) * 100, 1),
        "recent_volatility": round(daily_std_recent * np.sqrt(252) * 100, 1),
        "daily_avg_return": round(daily_mean_full * 100, 4),
        "trend_bias": trend_dir,
        "data_points_used": len(log_returns),
        "forecasts": forecasts,
    }
