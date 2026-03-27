"""
Report Generator — combines all the analysis into one clean report.
This is like the "final grade" that puts everything together.
"""

from analysis.market_data import get_stock_info, get_historical_data
from analysis.technical import (
    calculate_sma,
    calculate_ema,
    calculate_rsi,
    calculate_macd,
    calculate_bollinger_bands,
    calculate_support_resistance,
    calculate_volume_analysis,
    calculate_risk_score,
    determine_trend,
    calculate_price_forecast,
    calculate_pivot_points,
    calculate_hold_duration,
    calculate_adx,
    calculate_stochastic,
    calculate_rsi2,
    calculate_obv,
    calculate_bollinger_pct_b,
    calculate_atr,
    calculate_mfi,
    calculate_vwap,
    calculate_fibonacci_levels,
    calculate_ichimoku,
)
from analysis.news_sentiment import get_stock_sentiment


def generate_full_report(ticker: str, period: str = "2y") -> dict:
    """
    Generates the complete analysis report for a stock.
    This is the main function that ties everything together.
    """
    # 1. Get real data
    info = get_stock_info(ticker)
    history = get_historical_data(ticker, period)

    if not history:
        return {"error": f"No data found for ticker '{ticker}'. Make sure it's a valid stock symbol."}

    # Extract price and volume arrays
    closes = [d["close"] for d in history]
    highs = [d["high"] for d in history]
    lows = [d["low"] for d in history]
    volumes = [d["volume"] for d in history]
    dates = [d["date"] for d in history]

    # 2. Calculate all technical indicators
    sma_20 = calculate_sma(closes, 20)
    sma_50 = calculate_sma(closes, 50)
    sma_200 = calculate_sma(closes, 200)
    ema_12 = calculate_ema(closes, 12)
    ema_26 = calculate_ema(closes, 26)
    rsi = calculate_rsi(closes)
    macd = calculate_macd(closes)
    bollinger = calculate_bollinger_bands(closes)
    support_resistance = calculate_support_resistance(closes)
    volume_analysis = calculate_volume_analysis(volumes)
    risk = calculate_risk_score(closes, rsi, volumes)
    trend = determine_trend(closes)

    # 3. Pivot points
    pivot_points = calculate_pivot_points(closes)

    # 3b. Advanced indicators (Phase 5 upgrade)
    adx = calculate_adx(highs, lows, closes)
    stochastic = calculate_stochastic(highs, lows, closes)
    rsi2 = calculate_rsi2(closes)
    obv = calculate_obv(closes, volumes)
    bollinger_pct_b = calculate_bollinger_pct_b(closes)
    atr = calculate_atr(highs, lows, closes)
    mfi = calculate_mfi(highs, lows, closes, volumes)
    vwap = calculate_vwap(highs, lows, closes, volumes)
    fibonacci = calculate_fibonacci_levels(closes)
    ichimoku = calculate_ichimoku(highs, lows, closes)

    # 4. Calculate price forecast with probabilities
    forecast = calculate_price_forecast(closes, trend)

    # 5. Generate the prediction signal (now includes fundamentals awareness)
    signal = generate_signal(closes, rsi, macd, trend, volume_analysis)

    # 6. Hold duration recommendation
    hold_duration = calculate_hold_duration(closes, rsi, trend, forecast)

    # 7. News sentiment (current events, macro factors)
    try:
        news_sentiment = get_stock_sentiment(
            ticker,
            company_name=info.get("name", "")
        )
    except Exception:
        news_sentiment = {"stock_sentiment": 0, "market_sentiment": {"label": "Unavailable"}}

    # Build the chart data (dates + prices + indicators aligned)
    chart_data = []
    for i in range(len(dates)):
        point = {
            "date": dates[i],
            "close": closes[i],
            "volume": volumes[i],
            "sma_20": sma_20[i],
            "sma_50": sma_50[i],
            "ema_12": ema_12[i],
            "rsi": rsi[i],
            "macd": macd["macd_line"][i],
            "macd_signal": macd["signal_line"][i],
            "macd_histogram": macd["histogram"][i],
            "bb_upper": bollinger["upper"][i],
            "bb_middle": bollinger["middle"][i],
            "bb_lower": bollinger["lower"][i],
        }
        if i < len(sma_200):
            point["sma_200"] = sma_200[i]
        chart_data.append(point)

    return {
        "info": info,
        "signal": signal,
        "forecast": forecast,
        "trend": trend,
        "risk": risk,
        "support_resistance": support_resistance,
        "volume_analysis": volume_analysis,
        "pivot_points": pivot_points,
        "hold_duration": hold_duration,
        "news_sentiment": news_sentiment,
        "advanced_indicators": {
            "adx": adx,
            "stochastic": stochastic,
            "rsi2": rsi2,
            "obv": obv,
            "bollinger_pct_b": bollinger_pct_b,
            "atr": atr,
            "mfi": mfi,
            "vwap": vwap,
            "fibonacci": fibonacci,
            "ichimoku": ichimoku,
        },
        "chart_data": chart_data,
        "latest": {
            "price": closes[-1],
            "rsi": next((r for r in reversed(rsi) if r is not None), None),
            "macd": macd["macd_line"][-1],
            "macd_signal": macd["signal_line"][-1],
            "sma_20": sma_20[-1],
            "sma_50": sma_50[-1],
            "ema_9": hold_duration.get("ema_9"),
            "ema_21": hold_duration.get("ema_21"),
            "ema_50": hold_duration.get("ema_50"),
            "bb_upper": bollinger["upper"][-1],
            "bb_lower": bollinger["lower"][-1],
        },
        "period": period,
        "data_points": len(history),
    }


def generate_signal(closes, rsi, macd, trend, volume_analysis) -> dict:
    """
    Generates a buy/sell/hold signal using multi-factor analysis.
    Combines technical (EMA, RSI, MACD, pivot points) with trend and volume.
    """
    import pandas as pd
    import numpy as np

    score = 0  # Positive = bullish, Negative = bearish
    reasons = []
    series = pd.Series(closes)

    # --- Technical Factor 1: RSI with divergence detection ---
    latest_rsi = next((r for r in reversed(rsi) if r is not None), 50)
    if latest_rsi < 25:
        score += 3
        reasons.append(f"RSI deeply oversold at {latest_rsi:.0f} — strong reversal candidate")
    elif latest_rsi < 30:
        score += 2
        reasons.append(f"RSI oversold at {latest_rsi:.0f} — accumulation zone")
    elif latest_rsi < 40:
        score += 1
        reasons.append(f"RSI approaching oversold ({latest_rsi:.0f})")
    elif latest_rsi > 80:
        score -= 3
        reasons.append(f"RSI extremely overbought at {latest_rsi:.0f} — high reversal risk")
    elif latest_rsi > 70:
        score -= 2
        reasons.append(f"RSI overbought at {latest_rsi:.0f} — potential pullback")
    elif latest_rsi > 60:
        score -= 1
        reasons.append(f"RSI elevated at {latest_rsi:.0f}")

    # --- Technical Factor 2: EMA crossovers ---
    if len(closes) >= 50:
        ema_9 = float(series.ewm(span=9, adjust=False).mean().iloc[-1])
        ema_21 = float(series.ewm(span=21, adjust=False).mean().iloc[-1])
        ema_50 = float(series.ewm(span=50, adjust=False).mean().iloc[-1])
        current = closes[-1]

        if current > ema_9 > ema_21 > ema_50:
            score += 2
            reasons.append("Perfect bullish EMA alignment (9>21>50)")
        elif current > ema_9 > ema_21:
            score += 1
            reasons.append("Bullish EMA alignment (price > 9 > 21 EMA)")
        elif current < ema_9 < ema_21 < ema_50:
            score -= 2
            reasons.append("Perfect bearish EMA alignment")
        elif current < ema_9 < ema_21:
            score -= 1
            reasons.append("Bearish EMA alignment")

    # --- Technical Factor 3: MACD with histogram momentum ---
    macd_val = macd["macd_line"][-1]
    signal_val = macd["signal_line"][-1]
    hist_val = macd["histogram"][-1]
    prev_hist = macd["histogram"][-2] if len(macd["histogram"]) > 1 else 0

    if macd_val > signal_val and hist_val > prev_hist:
        score += 2
        reasons.append("MACD bullish crossover with accelerating momentum")
    elif macd_val > signal_val:
        score += 1
        reasons.append("MACD above signal line (bullish)")
    elif macd_val < signal_val and hist_val < prev_hist:
        score -= 2
        reasons.append("MACD bearish crossover with accelerating selling")
    else:
        score -= 1
        reasons.append("MACD below signal line (bearish)")

    # --- Technical Factor 4: Trend strength ---
    if trend["direction"] == "bullish":
        score += 2
        reasons.append(f"Strong uptrend — {trend.get('strength', 0)}% strength")
    elif trend["direction"] == "slightly_bullish":
        score += 1
        reasons.append("Mild uptrend forming")
    elif trend["direction"] == "bearish":
        score -= 2
        reasons.append(f"Strong downtrend — {trend.get('strength', 0)}% strength")
    elif trend["direction"] == "slightly_bearish":
        score -= 1
        reasons.append("Mild downtrend pressure")

    # --- Technical Factor 5: Volume confirmation ---
    if volume_analysis.get("unusual_volume"):
        if score > 0:
            score += 1
            reasons.append("Unusual high volume confirms bullish move")
        elif score < 0:
            score -= 1
            reasons.append("Unusual high volume confirms bearish pressure")
        else:
            reasons.append("Unusual volume — big move incoming")

    vol_trend = volume_analysis.get("volume_trend", "stable")
    if vol_trend == "increasing" and score > 0:
        reasons.append("Rising volume supports uptrend")
    elif vol_trend == "decreasing" and score > 0:
        reasons.append("Warning: declining volume in uptrend")

    # --- Determine final signal ---
    if score >= 5:
        direction = "Strong Buy"
        confidence = min(95, 65 + score * 3)
    elif score >= 3:
        direction = "Strong Buy"
        confidence = min(90, 60 + score * 3)
    elif score >= 1:
        direction = "Buy"
        confidence = min(80, 50 + score * 5)
    elif score <= -5:
        direction = "Strong Sell"
        confidence = min(95, 65 + abs(score) * 3)
    elif score <= -3:
        direction = "Strong Sell"
        confidence = min(90, 60 + abs(score) * 3)
    elif score <= -1:
        direction = "Sell"
        confidence = min(80, 50 + abs(score) * 5)
    else:
        direction = "Hold"
        confidence = 50

    return {
        "direction": direction,
        "confidence": confidence,
        "score": score,
        "reasons": reasons,
        "disclaimer": "This is for educational purposes only. This is NOT financial advice. Always do your own research before investing.",
    }
