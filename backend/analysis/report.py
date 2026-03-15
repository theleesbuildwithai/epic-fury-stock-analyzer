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
)


def generate_full_report(ticker: str, period: str = "1y") -> dict:
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

    # 3. Generate the prediction signal
    signal = generate_signal(closes, rsi, macd, trend, volume_analysis)

    # 4. Build the chart data (dates + prices + indicators aligned)
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
        "trend": trend,
        "risk": risk,
        "support_resistance": support_resistance,
        "volume_analysis": volume_analysis,
        "chart_data": chart_data,
        "latest": {
            "price": closes[-1],
            "rsi": next((r for r in reversed(rsi) if r is not None), None),
            "macd": macd["macd_line"][-1],
            "macd_signal": macd["signal_line"][-1],
            "sma_20": sma_20[-1],
            "sma_50": sma_50[-1],
            "bb_upper": bollinger["upper"][-1],
            "bb_lower": bollinger["lower"][-1],
        },
        "period": period,
        "data_points": len(history),
    }


def generate_signal(closes, rsi, macd, trend, volume_analysis) -> dict:
    """
    Generates a buy/sell/hold signal based on all indicators.
    This is NOT financial advice — it's a learning tool!
    """
    score = 0  # Positive = bullish, Negative = bearish
    reasons = []

    # RSI signal
    latest_rsi = next((r for r in reversed(rsi) if r is not None), 50)
    if latest_rsi < 30:
        score += 2
        reasons.append("RSI is below 30 (oversold — could bounce up)")
    elif latest_rsi < 40:
        score += 1
        reasons.append("RSI is getting low (approaching oversold)")
    elif latest_rsi > 70:
        score -= 2
        reasons.append("RSI is above 70 (overbought — could pull back)")
    elif latest_rsi > 60:
        score -= 1
        reasons.append("RSI is getting high (approaching overbought)")

    # MACD signal
    macd_val = macd["macd_line"][-1]
    signal_val = macd["signal_line"][-1]
    if macd_val > signal_val:
        score += 1
        reasons.append("MACD is above signal line (bullish momentum)")
    else:
        score -= 1
        reasons.append("MACD is below signal line (bearish momentum)")

    # Trend signal
    if trend["direction"] in ("bullish",):
        score += 2
        reasons.append("Strong uptrend (price > 20-day > 50-day average)")
    elif trend["direction"] == "slightly_bullish":
        score += 1
        reasons.append("Mild uptrend (price above 20-day average)")
    elif trend["direction"] == "bearish":
        score -= 2
        reasons.append("Strong downtrend (price < 20-day < 50-day average)")
    elif trend["direction"] == "slightly_bearish":
        score -= 1
        reasons.append("Mild downtrend (price below 20-day average)")

    # Volume confirmation
    if volume_analysis.get("unusual_volume"):
        reasons.append("Unusual volume detected (1.5x+ average)")

    # Determine signal
    if score >= 3:
        direction = "Strong Buy"
        confidence = min(95, 60 + score * 5)
    elif score >= 1:
        direction = "Buy"
        confidence = min(80, 50 + score * 5)
    elif score <= -3:
        direction = "Strong Sell"
        confidence = min(95, 60 + abs(score) * 5)
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
