"""
AI Stock Analyst — Pro-level trading knowledge engine.

Answers any stock/trading question by combining:
1. Live market data from Yahoo Finance
2. Technical analysis (EMA, RSI, MACD, pivot points, Bollinger Bands)
3. News sentiment from CNN, CNBC, Yahoo Finance
4. Deep trading knowledge base (strategies, concepts, risk management)

Think of this as a senior quant analyst at Renaissance Technologies.
"""

import re
from analysis.report import generate_full_report
from analysis.market_data import get_stock_info
from analysis.news_sentiment import get_market_news, get_stock_sentiment
from analysis.extras import get_daily_picks

# Common ticker patterns
TICKER_PATTERN = re.compile(r'\b([A-Z]{1,5})\b')

# Known tickers to avoid false positives
COMMON_WORDS = {
    "I", "A", "THE", "AND", "OR", "IS", "IT", "AT", "TO", "IN", "ON",
    "FOR", "OF", "BY", "AN", "BE", "DO", "IF", "MY", "NO", "UP", "SO",
    "AS", "AM", "ARE", "WAS", "HAS", "HAD", "HIS", "HER", "HOW", "WHO",
    "WHY", "CAN", "ALL", "NEW", "NOW", "OLD", "OUR", "OUT", "OWN", "SAY",
    "SHE", "TOO", "USE", "WAY", "YOU", "DAY", "GET", "HIM", "MAY", "NOT",
    "ONE", "SET", "TOP", "TWO", "BIG", "END", "FAR", "FEW", "GOT", "LET",
    "PUT", "RUN", "ANY", "BUY", "SELL", "HOLD", "WHAT", "WHEN", "TELL",
    "GOOD", "BEST", "LONG", "THAT", "THIS", "WITH", "FROM", "WILL",
    "MUCH", "MOST", "RISK", "HIGH", "LOW", "RSI", "EMA", "SMA", "MACD",
    "PE", "EPS", "IPO", "ETF", "GDP", "CPI", "FED", "SEC", "CEO", "CFO",
    "AI", "US", "UK", "EU", "USA", "P", "S", "R", "VOL", "AVG", "MAX",
    "MIN", "VS", "PRO", "Q", "K", "M", "B", "T", "GO", "OK",
}

# Known valid tickers (subset for quick matching)
KNOWN_TICKERS = {
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NVDA", "TSLA",
    "BRK", "JPM", "JNJ", "UNH", "V", "MA", "PG", "HD", "XOM", "CVX",
    "BAC", "WFC", "KO", "PEP", "COST", "WMT", "MRK", "ABBV", "LLY",
    "AVGO", "TMO", "ABT", "MCD", "ACN", "ADBE", "CRM", "NFLX", "AMD",
    "INTC", "QCOM", "TXN", "CSCO", "IBM", "GE", "CAT", "BA", "HON",
    "DIS", "NKE", "SBUX", "GS", "MS", "BLK", "SCHW", "AXP",
    "PYPL", "SQ", "COIN", "UBER", "ABNB", "SNOW", "PLTR", "RIVN",
    "LCID", "NIO", "BABA", "JD", "PDD", "TSM", "ASML", "ARM",
    "SPY", "QQQ", "DIA", "IWM", "VTI", "VOO", "ARKK",
}


def _extract_ticker(question):
    """Extract a stock ticker from the question."""
    q_upper = question.upper()

    # Direct mention check
    for ticker in KNOWN_TICKERS:
        if ticker in q_upper.split():
            return ticker

    # Company name mapping
    company_map = {
        "apple": "AAPL", "microsoft": "MSFT", "google": "GOOGL",
        "alphabet": "GOOGL", "amazon": "AMZN", "meta": "META",
        "facebook": "META", "nvidia": "NVDA", "tesla": "TSLA",
        "jpmorgan": "JPM", "jp morgan": "JPM", "johnson": "JNJ",
        "walmart": "WMT", "costco": "COST", "netflix": "NFLX",
        "adobe": "ADBE", "salesforce": "CRM", "starbucks": "SBUX",
        "disney": "DIS", "nike": "NKE", "boeing": "BA",
        "goldman": "GS", "goldman sachs": "GS", "morgan stanley": "MS",
        "berkshire": "BRK-B", "coca cola": "KO", "coca-cola": "KO",
        "pepsi": "PEP", "pepsico": "PEP", "mcdonalds": "MCD",
        "mcdonald's": "MCD", "home depot": "HD", "exxon": "XOM",
        "chevron": "CVX", "pfizer": "PFE", "merck": "MRK",
        "intel": "INTC", "amd": "AMD", "qualcomm": "QCOM",
        "cisco": "CSCO", "ibm": "IBM", "caterpillar": "CAT",
        "uber": "UBER", "airbnb": "ABNB", "coinbase": "COIN",
        "paypal": "PYPL", "palantir": "PLTR", "snowflake": "SNOW",
        "rivian": "RIVN", "lucid": "LCID", "nio": "NIO",
        "alibaba": "BABA", "taiwan semi": "TSM", "tsmc": "TSM",
        "broadcom": "AVGO", "eli lilly": "LLY", "lilly": "LLY",
        "abbvie": "ABBV", "amgen": "AMGN",
        "spy": "SPY", "s&p": "SPY", "s&p 500": "SPY",
        "nasdaq": "QQQ", "dow": "DIA",
    }

    q_lower = question.lower()
    for name, ticker in company_map.items():
        if name in q_lower:
            return ticker

    # Pattern match for uppercase words that look like tickers
    words = q_upper.split()
    for word in words:
        clean = re.sub(r'[^A-Z]', '', word)
        if clean and 1 <= len(clean) <= 5 and clean not in COMMON_WORDS and clean in KNOWN_TICKERS:
            return clean

    return None


def _detect_question_type(question):
    """Classify the type of question being asked."""
    q = question.lower()

    if any(w in q for w in ["should i buy", "buy or sell", "is it a buy", "should i invest", "worth buying", "good buy", "good investment"]):
        return "buy_sell_recommendation"

    if any(w in q for w in ["hold", "how long", "when to sell", "exit", "take profit", "hold duration"]):
        return "hold_duration"

    if any(w in q for w in ["price target", "target price", "where is it going", "price prediction", "forecast", "predict"]):
        return "price_target"

    if any(w in q for w in ["rsi", "macd", "ema", "sma", "bollinger", "moving average", "technical", "indicator"]):
        return "technical_analysis"

    if any(w in q for w in ["pivot", "support", "resistance", "level"]):
        return "support_resistance"

    if any(w in q for w in ["news", "sentiment", "headline", "event", "current event", "macro"]):
        return "news_sentiment"

    if any(w in q for w in ["risk", "risky", "volatile", "volatility", "safe", "dangerous"]):
        return "risk_assessment"

    if any(w in q for w in ["compare", "vs", "versus", "better", "which one"]):
        return "comparison"

    if any(w in q for w in ["top pick", "best stock", "recommend", "suggestion", "what to buy", "symbols to buy"]):
        return "top_picks"

    if any(w in q for w in ["what is", "what are", "explain", "how does", "how do", "define", "meaning", "tell me about"]):
        return "education"

    if any(w in q for w in ["market", "overall", "today", "how is the market", "market doing"]):
        return "market_overview"

    if any(w in q for w in ["dividend", "yield", "income", "payout"]):
        return "dividend"

    if any(w in q for w in ["earnings", "revenue", "profit", "quarter", "report"]):
        return "earnings"

    if any(w in q for w in ["sector", "industry", "which sector"]):
        return "sector"

    return "general"


# Trading knowledge base
TRADING_KNOWLEDGE = {
    "rsi": {
        "name": "Relative Strength Index (RSI)",
        "explanation": "RSI measures momentum on a 0-100 scale. Below 30 = oversold (potential buy), above 70 = overbought (potential sell). We use Wilder's smoothing method (14-period), the same as Bloomberg and TradingView. RSI divergence (price making new highs but RSI declining) is a powerful reversal signal used by institutional traders.",
        "pro_tip": "Don't just buy when RSI < 30. Wait for RSI to cross BACK above 30 — that confirms the reversal. In strong trends, RSI can stay overbought for weeks."
    },
    "ema": {
        "name": "Exponential Moving Average (EMA)",
        "explanation": "EMA gives more weight to recent prices than SMA, making it more responsive. The 9/21/50 EMA stack is a key institutional signal. When price > EMA 9 > EMA 21 > EMA 50, that's a perfect bullish alignment. The opposite is bearish. EMA crossovers (9 crossing above 21) generate buy/sell signals.",
        "pro_tip": "The 21-day EMA is the 'line in the sand' for swing traders. If price can't hold above the 21 EMA, the trend is weakening. Institutions watch the 50 and 200 EMA religiously."
    },
    "macd": {
        "name": "MACD (Moving Average Convergence Divergence)",
        "explanation": "MACD shows the relationship between the 12-period and 26-period EMA. The signal line is a 9-period EMA of MACD. When MACD crosses above signal = bullish. The histogram shows momentum acceleration — growing histogram bars mean the trend is strengthening.",
        "pro_tip": "The most powerful signal is MACD divergence: price making new lows but MACD making higher lows. This preceded major reversals in 2020 and 2022."
    },
    "pivot_points": {
        "name": "Pivot Points",
        "explanation": "Pivot points are calculated from previous period's high, low, and close. They create support (S1, S2, S3) and resistance (R1, R2, R3) levels. Floor traders at the CME have used these since the 1950s. Price tends to gravitate toward the pivot level and bounce off support/resistance levels.",
        "pro_tip": "If the market opens above the pivot, bias is bullish. Below the pivot, bias is bearish. The R2/S2 levels are where most reversals happen — only strong momentum breaks through R3/S3."
    },
    "bollinger_bands": {
        "name": "Bollinger Bands",
        "explanation": "Bollinger Bands are a volatility envelope around a 20-period SMA, using 2 standard deviations. When bands squeeze tight, a big move is coming (Bollinger Squeeze). Price touching the upper band isn't necessarily a sell — in strong uptrends, price 'walks the band.' Mean reversion trades work when price touches the outer band and RSI confirms overbought/oversold.",
        "pro_tip": "The Bollinger Band Width indicator tells you exactly when volatility is at historical lows. Combined with volume analysis, it predicts breakouts before they happen."
    },
    "risk_management": {
        "name": "Risk Management",
        "explanation": "Professional traders risk 1-2% of portfolio per trade maximum. Use stop losses at key technical levels (below support, below EMA 21). Position sizing formula: Risk Amount / (Entry - Stop Loss) = Number of Shares. Never average down on a losing trade unless the fundamental thesis hasn't changed.",
        "pro_tip": "The Kelly Criterion is used by hedge funds to size positions: f = (bp - q) / b, where b = odds, p = win probability, q = lose probability. Most funds use half-Kelly for safety."
    },
    "market_structure": {
        "name": "Market Structure",
        "explanation": "Markets move in trends (higher highs/higher lows for uptrend, lower highs/lower lows for downtrend). A break of structure (BOS) occurs when price breaks a key swing point. This is how institutional traders identify trend changes. Combined with order blocks and fair value gaps, it forms the basis of Smart Money Concepts.",
        "pro_tip": "Look for change of character (CHoCH) — when an uptrend makes its first lower low. That's where smart money starts selling."
    },
    "volume_analysis": {
        "name": "Volume Analysis",
        "explanation": "Volume confirms price moves. Rising price + rising volume = strong trend. Rising price + falling volume = weak rally (distribution). Big volume spikes at support = institutional buying. The Volume Weighted Average Price (VWAP) is the most important level for institutional traders — it's where the 'fair price' is.",
        "pro_tip": "Watch for volume climax days (3x+ average volume with wide range). These often mark trend exhaustion and reversals."
    },
    "options": {
        "name": "Options Trading",
        "explanation": "Options give the right (not obligation) to buy (call) or sell (put) at a specific price (strike) by a date (expiration). Greeks: Delta = directional exposure, Gamma = rate of delta change, Theta = time decay, Vega = volatility sensitivity. Implied volatility (IV) is what the market expects — high IV = expensive options.",
        "pro_tip": "Sell options when IV is high (earnings, events). Buy options when IV is low and you expect a move. The most profitable strategy used by market makers is selling out-of-the-money puts on quality stocks."
    },
    "valuation": {
        "name": "Stock Valuation",
        "explanation": "P/E ratio = Price / Earnings per share. Forward P/E uses estimated future earnings. PEG ratio = P/E / Growth rate (PEG < 1 = undervalued). Price-to-Sales for growth stocks. DCF (Discounted Cash Flow) is the gold standard — it values a company based on future cash flows discounted to present value. EV/EBITDA removes debt effects.",
        "pro_tip": "Compare a stock's P/E to its 5-year average AND its sector average. A 'cheap' tech stock at 25x P/E might be expensive if the sector trades at 20x."
    },
    "sector_rotation": {
        "name": "Sector Rotation",
        "explanation": "Different sectors outperform at different points in the economic cycle. Early expansion: Technology, Consumer Discretionary. Mid-cycle: Industrials, Materials. Late cycle: Energy, Healthcare. Recession: Utilities, Consumer Staples, Healthcare. Monitor the yield curve — an inverted curve (2Y > 10Y) has preceded every recession since 1955.",
        "pro_tip": "When the Fed pivots from tightening to easing, growth stocks (tech) typically outperform value stocks by 15-20% over the following 12 months."
    },
}


def answer_question(question: str) -> dict:
    """
    Main function — takes a question and returns a pro-level analysis.
    """
    ticker = _extract_ticker(question)
    q_type = _detect_question_type(question)
    q_lower = question.lower()

    response_parts = []
    data_used = []

    # --- If a specific stock is mentioned, pull live data ---
    stock_data = None
    if ticker:
        try:
            stock_data = generate_full_report(ticker, period="2y")
            if "error" not in stock_data:
                data_used.append(f"Live analysis of {ticker}")
            else:
                stock_data = None
        except Exception:
            stock_data = None

    # --- Generate response based on question type ---

    if q_type == "buy_sell_recommendation" and stock_data:
        signal = stock_data["signal"]
        hold = stock_data["hold_duration"]
        risk = stock_data["risk"]
        forecast = stock_data.get("forecast", {})
        info = stock_data["info"]
        pivot = stock_data.get("pivot_points", {})
        news = stock_data.get("news_sentiment", {})

        response_parts.append(f"**{ticker} — {signal['direction']}** (Confidence: {signal['confidence']}%)\n")
        response_parts.append(f"Current Price: ${stock_data['latest']['price']}")
        response_parts.append(f"Recommended Hold: {hold.get('label', 'N/A')} (~{hold.get('days', '?')} days)")
        response_parts.append(f"Entry: {hold.get('entry_guidance', 'N/A')}")
        response_parts.append(f"Risk Score: {risk['score']}/10 ({risk['label']})\n")

        if signal.get("reasons"):
            response_parts.append("**Why this signal:**")
            for r in signal["reasons"][:5]:
                response_parts.append(f"  - {r}")

        if pivot:
            response_parts.append(f"\n**Key Levels:** Pivot ${pivot.get('pivot')}, Support ${pivot.get('s1')}, Resistance ${pivot.get('r1')}")

        if forecast and "forecasts" in forecast:
            f30 = next((f for f in forecast["forecasts"] if f["days"] == 30), None)
            if f30:
                response_parts.append(f"\n**30-Day Outlook:** {f30['prob_up']}% probability of going up")
                response_parts.append(f"  Bull target: ${f30['targets']['bull']['price']} ({f30['targets']['bull']['pct']:+.1f}%)")
                response_parts.append(f"  Bear target: ${f30['targets']['bear']['price']} ({f30['targets']['bear']['pct']:+.1f}%)")

        market_sent = news.get("market_sentiment", {}).get("label", "")
        if market_sent:
            response_parts.append(f"\n**Market Sentiment:** {market_sent}")

    elif q_type == "hold_duration" and stock_data:
        hold = stock_data["hold_duration"]
        response_parts.append(f"**{ticker} — Hold Duration: {hold.get('label', 'N/A')}** (~{hold.get('days', '?')} days)\n")
        response_parts.append(f"Entry: {hold.get('entry_guidance', 'N/A')}")
        response_parts.append(f"Exit: {hold.get('exit_guidance', 'N/A')}\n")
        if hold.get("reasoning"):
            response_parts.append("**Analysis:**")
            for r in hold["reasoning"]:
                response_parts.append(f"  - {r}")
        response_parts.append(f"\n**EMA Levels:** 9-day: ${hold.get('ema_9')}, 21-day: ${hold.get('ema_21')}, 50-day: ${hold.get('ema_50')}")

    elif q_type == "price_target" and stock_data:
        forecast = stock_data.get("forecast", {})
        if forecast and "forecasts" in forecast:
            response_parts.append(f"**{ticker} Price Targets:**\n")
            response_parts.append(f"Current: ${forecast['current_price']}")
            response_parts.append(f"Volatility: {forecast['annualized_volatility']}% (annual), {forecast['recent_volatility']}% (recent)\n")
            for fc in forecast["forecasts"]:
                t = fc["targets"]
                response_parts.append(f"**{fc['timeframe']}:** Bull ${t['bull']['price']} ({t['bull']['pct']:+.1f}%) | Base ${t['base']['price']} ({t['base']['pct']:+.1f}%) | Bear ${t['bear']['price']} ({t['bear']['pct']:+.1f}%) — {fc['prob_up']}% chance up")

    elif q_type == "technical_analysis" and stock_data:
        latest = stock_data["latest"]
        trend = stock_data["trend"]
        hold = stock_data["hold_duration"]
        response_parts.append(f"**{ticker} Technical Analysis:**\n")
        response_parts.append(f"Price: ${latest['price']}")
        response_parts.append(f"RSI (14): {latest['rsi']}")
        response_parts.append(f"MACD: {latest['macd']:.4f} | Signal: {latest['macd_signal']:.4f}")
        response_parts.append(f"SMA 20: ${latest['sma_20']} | SMA 50: ${latest['sma_50']}")
        response_parts.append(f"EMA 9: ${hold.get('ema_9')} | EMA 21: ${hold.get('ema_21')} | EMA 50: ${hold.get('ema_50')}")
        response_parts.append(f"Bollinger: Upper ${latest['bb_upper']} | Lower ${latest['bb_lower']}")
        response_parts.append(f"Trend: {trend['direction'].replace('_', ' ').title()} ({trend['strength']}% strength)")
        response_parts.append(f"\nPrice vs 20-day MA: {trend['price_vs_sma20']:+.2f}%")
        response_parts.append(f"Price vs 50-day MA: {trend['price_vs_sma50']:+.2f}%")

    elif q_type == "support_resistance" and stock_data:
        pivot = stock_data.get("pivot_points", {})
        sr = stock_data.get("support_resistance", {})
        if pivot:
            response_parts.append(f"**{ticker} Key Levels:**\n")
            response_parts.append(f"**Pivot Points:**")
            response_parts.append(f"  R3: ${pivot.get('r3')} | R2: ${pivot.get('r2')} | R1: ${pivot.get('r1')}")
            response_parts.append(f"  Pivot: ${pivot.get('pivot')}")
            response_parts.append(f"  S1: ${pivot.get('s1')} | S2: ${pivot.get('s2')} | S3: ${pivot.get('s3')}")
            response_parts.append(f"\nPrice is {pivot.get('position', '?')} the pivot")
            response_parts.append(f"20-day range: ${pivot.get('low_20d')} — ${pivot.get('high_20d')}")
        if sr:
            if sr.get("support"):
                response_parts.append(f"\n**Historical Support:** {', '.join(f'${s}' for s in sr['support'])}")
            if sr.get("resistance"):
                response_parts.append(f"**Historical Resistance:** {', '.join(f'${r}' for r in sr['resistance'])}")

    elif q_type == "risk_assessment" and stock_data:
        risk = stock_data["risk"]
        forecast = stock_data.get("forecast", {})
        response_parts.append(f"**{ticker} Risk Assessment:**\n")
        response_parts.append(f"Risk Score: {risk['score']}/10 ({risk['label']})")
        response_parts.append(f"Annualized Volatility: {forecast.get('annualized_volatility', 'N/A')}%")
        response_parts.append(f"Recent Volatility: {forecast.get('recent_volatility', 'N/A')}%\n")
        if risk.get("factors"):
            response_parts.append("**Risk Factors:**")
            for f in risk["factors"]:
                response_parts.append(f"  - {f['name']}: {f['score']}/10 — {f['detail']}")

    elif q_type == "news_sentiment":
        if ticker and stock_data:
            news = stock_data.get("news_sentiment", {})
            response_parts.append(f"**{ticker} News Sentiment:**\n")
            market = news.get("market_sentiment", {})
            response_parts.append(f"Market Sentiment: {market.get('label', 'N/A')} (Score: {market.get('score', 0)})")
            response_parts.append(f"Bullish: {market.get('bullish_pct', 0)}% | Bearish: {market.get('bearish_pct', 0)}%\n")
            if news.get("stock_headlines"):
                response_parts.append(f"**{ticker} Headlines:**")
                for h in news["stock_headlines"][:5]:
                    sent = "+" if h["sentiment"] > 0 else "-" if h["sentiment"] < 0 else "~"
                    response_parts.append(f"  [{sent}] {h['title']} ({h['source']})")
            if news.get("macro_events"):
                response_parts.append("\n**Macro Events:**")
                for h in news["macro_events"][:5]:
                    response_parts.append(f"  - {h['title']} ({h['source']})")
        else:
            try:
                news = get_market_news()
                response_parts.append("**Market News Sentiment:**\n")
                market = news.get("market_sentiment", {})
                response_parts.append(f"Overall: {market.get('label', 'N/A')}")
                response_parts.append(f"Bullish: {market.get('bullish_pct', 0)}% | Bearish: {market.get('bearish_pct', 0)}%\n")
                if news.get("headlines"):
                    response_parts.append("**Top Headlines:**")
                    for h in news["headlines"][:8]:
                        sent = "+" if h["sentiment"] > 0 else "-" if h["sentiment"] < 0 else "~"
                        response_parts.append(f"  [{sent}] {h['title']} ({h['source']})")
                data_used.append("Live news from Yahoo Finance, CNN, CNBC")
            except Exception:
                response_parts.append("Unable to fetch news at this time.")

    elif q_type == "top_picks":
        try:
            picks = get_daily_picks()
            if picks and picks.get("picks"):
                response_parts.append("**Symbols to Buy — Today's Top Picks:**\n")
                for p in picks["picks"][:10]:
                    response_parts.append(
                        f"  **#{p['rank']} {p['symbol']}** — ${p['price']} | {p['signal']} | "
                        f"Hold: {p.get('hold_label', 'N/A')} | RSI: {p['rsi']} | "
                        f"30D Up: {p['prob_up_30d']}%"
                    )
                    if p.get("reasons"):
                        response_parts.append(f"    Reasons: {', '.join(p['reasons'][:2])}")
                response_parts.append(f"\nAnalyzed {picks.get('total_analyzed', '?')} stocks using EMA, RSI, MACD, pivot points, and momentum.")
                data_used.append("Live multi-factor stock screening")
            else:
                response_parts.append("Unable to generate picks right now. Try again in a moment.")
        except Exception:
            response_parts.append("Stock screening is temporarily unavailable.")

    elif q_type == "market_overview":
        try:
            news = get_market_news()
            market = news.get("market_sentiment", {})
            response_parts.append("**Market Overview:**\n")
            response_parts.append(f"Sentiment: {market.get('label', 'N/A')} (Score: {market.get('score', 0)})")
            response_parts.append(f"Headlines analyzed: {market.get('total_analyzed', 0)}")
            response_parts.append(f"Bullish: {market.get('bullish_pct', 0)}% | Bearish: {market.get('bearish_pct', 0)}% | Neutral: {market.get('neutral_pct', 0)}%\n")
            if news.get("macro_events"):
                response_parts.append("**Key Macro Events:**")
                for h in news["macro_events"][:5]:
                    response_parts.append(f"  - {h['title']} ({h['source']})")
            data_used.append("Live news sentiment analysis")
        except Exception:
            response_parts.append("Market data temporarily unavailable.")

    elif q_type == "education":
        # Check knowledge base
        for key, info in TRADING_KNOWLEDGE.items():
            if key in q_lower or info["name"].lower() in q_lower:
                response_parts.append(f"**{info['name']}**\n")
                response_parts.append(info["explanation"])
                response_parts.append(f"\n**Pro Tip:** {info['pro_tip']}")
                break
        else:
            # General education response
            response_parts.append(_get_general_education(q_lower))

    elif q_type == "dividend" and stock_data:
        info = stock_data["info"]
        response_parts.append(f"**{ticker} Dividend Info:**\n")
        div_yield = info.get("dividend_yield", 0)
        if div_yield:
            response_parts.append(f"Dividend Yield: {div_yield * 100:.2f}%")
        else:
            response_parts.append(f"{ticker} does not currently pay a dividend.")
        response_parts.append(f"P/E Ratio: {info.get('pe_ratio', 'N/A')}")
        response_parts.append(f"Beta: {info.get('beta', 'N/A')}")

    elif q_type == "earnings" and stock_data:
        info = stock_data["info"]
        response_parts.append(f"**{ticker} Fundamentals:**\n")
        response_parts.append(f"P/E Ratio: {info.get('pe_ratio', 'N/A')}")
        response_parts.append(f"Market Cap: ${info.get('market_cap', 0):,.0f}" if info.get('market_cap') else "Market Cap: N/A")
        response_parts.append(f"52-Week High: ${info.get('fifty_two_week_high', 'N/A')}")
        response_parts.append(f"52-Week Low: ${info.get('fifty_two_week_low', 'N/A')}")
        response_parts.append(f"Beta: {info.get('beta', 'N/A')}")
        response_parts.append(f"Sector: {info.get('sector', 'N/A')}")

    else:
        # General response — try to be helpful
        if stock_data:
            signal = stock_data["signal"]
            latest = stock_data["latest"]
            hold = stock_data["hold_duration"]
            response_parts.append(f"**{ticker} Quick Analysis:**\n")
            response_parts.append(f"Price: ${latest['price']} | Signal: {signal['direction']} ({signal['confidence']}%)")
            response_parts.append(f"RSI: {latest['rsi']} | Trend: {stock_data['trend']['direction'].replace('_', ' ')}")
            response_parts.append(f"Hold: {hold.get('label', 'N/A')} | Risk: {stock_data['risk']['score']}/10\n")
            if signal.get("reasons"):
                for r in signal["reasons"][:3]:
                    response_parts.append(f"  - {r}")
        else:
            response_parts.append(_get_general_education(q_lower))

    # Add disclaimer
    response_parts.append("\n---\n*This is for educational purposes only. Not financial advice. Always do your own research.*")

    return {
        "answer": "\n".join(response_parts),
        "ticker": ticker,
        "question_type": q_type,
        "data_used": data_used,
    }


def _get_general_education(q_lower):
    """Provide general trading education."""
    # Check all knowledge base topics
    for key, info in TRADING_KNOWLEDGE.items():
        if key in q_lower:
            return f"**{info['name']}**\n\n{info['explanation']}\n\n**Pro Tip:** {info['pro_tip']}"

    if "beginner" in q_lower or "start" in q_lower or "new to" in q_lower:
        return (
            "**Getting Started with Investing:**\n\n"
            "1. **Start with index funds** (SPY, VOO, QQQ) — they track the market and diversify risk\n"
            "2. **Learn technical analysis** — RSI, EMA, MACD are the three pillars\n"
            "3. **Never risk more than 1-2%** of your portfolio on a single trade\n"
            "4. **Use stop losses** — always know your exit before you enter\n"
            "5. **Paper trade first** — practice with fake money before risking real money\n"
            "6. **Read the news** — macro events (Fed, inflation, tariffs) move the whole market\n\n"
            "**Pro Tip:** The S&P 500 has returned ~10% annually over the last 100 years. "
            "If you can't beat it, join it — there's no shame in index investing."
        )

    if "strategy" in q_lower or "strategies" in q_lower:
        return (
            "**Top Trading Strategies:**\n\n"
            "1. **Trend Following** — Buy when EMA 9 > 21 > 50, sell when reversed. Ride the trend.\n"
            "2. **Mean Reversion** — Buy oversold stocks (RSI < 30), sell overbought (RSI > 70).\n"
            "3. **Breakout Trading** — Buy when price breaks above resistance with volume confirmation.\n"
            "4. **Swing Trading** — Hold 1-4 weeks, use daily charts, target 5-15% moves.\n"
            "5. **Value Investing** — Buy stocks trading below intrinsic value (low P/E, strong fundamentals).\n\n"
            "**Pro Tip:** The best strategy is the one you can follow consistently. "
            "Backtesting on historical data is essential before trading real money."
        )

    return (
        "I'm your AI Stock Analyst. I can help with:\n\n"
        "- **\"Should I buy AAPL?\"** — Get buy/sell signals with reasoning\n"
        "- **\"How long should I hold TSLA?\"** — Hold duration recommendations\n"
        "- **\"What's the price target for NVDA?\"** — Price forecasts at 7-180 days\n"
        "- **\"Explain RSI\"** — Trading education from a pro\n"
        "- **\"What are today's top picks?\"** — Symbols to buy right now\n"
        "- **\"How is the market today?\"** — Live market sentiment\n"
        "- **\"Is Tesla risky?\"** — Risk assessment with scores\n"
        "- **\"What are pivot points?\"** — Learn any trading concept\n\n"
        "Ask me anything about stocks, trading, or investing!"
    )
