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
    "MIN", "VS", "PRO", "Q", "K", "M", "B", "T", "GO", "OK", "LIKE",
    "MAKE", "TAKE", "GIVE", "JUST", "KNOW", "THINK", "ALSO", "BACK",
    "ONLY", "COME", "WANT", "LOOK", "SOME", "OVER", "SUCH", "THAN",
    "FIND", "HERE", "MANY", "WELL", "VERY", "WHEN", "WHAT", "YOUR",
    "THEM", "BEEN", "HAVE", "CALL", "EACH", "MADE", "MOVE", "WORK",
    "NEED", "HELP", "LINE", "TURN", "KEEP", "TALK", "REAL", "LAST",
    "NEXT", "OPEN", "SAME", "YEAR", "DOES", "DOWN", "CASH", "GAIN",
    "LOSS", "WIN", "SAFE", "PICK", "STOP", "EVER", "PLAY", "RATE",
    "FEEL", "SURE", "SHOW", "TERM", "FUND", "PAID", "GROW", "PLAN",
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
    "SOFI", "HOOD", "RBLX", "SNAP", "PINS", "SHOP", "SE", "MELI",
    "CRWD", "ZS", "NET", "DDOG", "MDB", "PANW", "FTNT", "OKTA",
    "SMCI", "MRVL", "ON", "LRCX", "KLAC", "AMAT", "MU",
    "LMT", "RTX", "NOC", "GD", "PFE", "BMY", "AMGN", "GILD",
    "SLB", "HAL", "OXY", "DVN", "FANG", "EOG",
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
        "spy": "SPY", "s&p": "SPY", "s&p 500": "SPY", "s&p500": "SPY",
        "nasdaq": "QQQ", "dow": "DIA", "dow jones": "DIA",
        "crowdstrike": "CRWD", "datadog": "DDOG", "mongodb": "MDB",
        "palo alto": "PANW", "fortinet": "FTNT", "cloudflare": "NET",
        "shopify": "SHOP", "roblox": "RBLX", "snapchat": "SNAP", "snap": "SNAP",
        "pinterest": "PINS", "sofi": "SOFI", "robinhood": "HOOD",
        "supermicro": "SMCI", "micron": "MU", "marvell": "MRVL",
        "lockheed": "LMT", "raytheon": "RTX", "northrop": "NOC",
        "schlumberger": "SLB", "halliburton": "HAL", "occidental": "OXY",
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

    if any(w in q for w in [
        "should i buy", "buy or sell", "is it a buy", "should i invest",
        "worth buying", "good buy", "good investment", "should i get",
        "is it worth", "would you buy", "do you recommend", "is it undervalued",
        "is it overvalued", "is it cheap", "good time to buy", "time to buy",
        "go long", "go short", "bullish or bearish", "calls or puts",
        "good entry", "entry point", "accumulate", "add to my position",
    ]):
        return "buy_sell_recommendation"

    if any(w in q for w in [
        "hold", "how long", "when to sell", "exit", "take profit",
        "hold duration", "sell it", "keep holding", "too late",
        "time to get out", "time to exit", "bag hold", "cut losses",
        "when should i sell", "lock in profits", "stop loss",
    ]):
        return "hold_duration"

    if any(w in q for w in [
        "price target", "target price", "where is it going", "price prediction",
        "forecast", "predict", "where will", "how high", "how far",
        "upside", "downside", "potential", "expected price", "fair value",
        "what will the price be", "going to go up", "going to go down",
        "going to crash", "going to moon", "going to drop", "going to rise",
    ]):
        return "price_target"

    if any(w in q for w in [
        "rsi", "macd", "ema", "sma", "bollinger", "moving average", "technical",
        "indicator", "chart", "pattern", "fibonacci", "fib", "golden cross",
        "death cross", "crossover", "divergence", "overbought", "oversold",
        "stochastic", "ichimoku", "adx", "atr", "obv",
    ]):
        return "technical_analysis"

    if any(w in q for w in [
        "pivot", "support", "resistance", "level", "key level",
        "floor", "ceiling", "breakout point", "breakdown",
    ]):
        return "support_resistance"

    if any(w in q for w in [
        "news", "sentiment", "headline", "event", "current event", "macro",
        "what happened", "what's happening", "what's going on", "whats going on",
        "why is it down", "why is it up", "why did it drop", "why did it crash",
        "why is the market", "tariff", "inflation", "interest rate", "fed",
        "war", "recession", "crash", "rally", "sell off", "selloff",
        "economic", "geopolitical", "policy", "regulation", "earnings report",
    ]):
        return "news_sentiment"

    if any(w in q for w in [
        "risk", "risky", "volatile", "volatility", "safe", "dangerous",
        "how safe", "how risky", "beta", "drawdown", "max loss",
        "worst case", "downside risk", "protect", "hedge",
    ]):
        return "risk_assessment"

    if any(w in q for w in [
        "compare", "vs", "versus", "better", "which one",
        "difference between", "or should i", "which is better",
        "pick between", "choose between",
    ]):
        return "comparison"

    if any(w in q for w in [
        "top pick", "best stock", "recommend", "suggestion", "what to buy",
        "symbols to buy", "what should i buy", "any good stocks",
        "stock picks", "hot stocks", "best investment", "top stocks",
        "what are you buying", "what would you buy", "strongest stocks",
        "best performers", "winners", "momentum stocks",
    ]):
        return "top_picks"

    if any(w in q for w in [
        "what is", "what are", "explain", "how does", "how do", "define",
        "meaning", "tell me about", "teach me", "learn about", "understand",
        "how to", "what does", "why do", "tutorial", "guide", "basics",
        "beginner", "introduction", "101", "for dummies",
    ]):
        return "education"

    if any(w in q for w in [
        "market", "overall", "today", "how is the market", "market doing",
        "market outlook", "bull market", "bear market", "correction",
        "market crash", "market rally", "indices", "index",
        "s&p", "nasdaq", "dow jones", "market sentiment",
        "premarket", "after hours", "futures",
    ]):
        return "market_overview"

    if any(w in q for w in [
        "dividend", "yield", "income", "payout", "dividend stock",
        "passive income", "high yield", "dividend aristocrat",
    ]):
        return "dividend"

    if any(w in q for w in [
        "earnings", "revenue", "profit", "quarter", "report",
        "beat estimates", "missed", "guidance", "eps",
        "income statement", "balance sheet", "cash flow",
        "financial", "fundamentals", "p/e", "pe ratio",
        "valuation", "market cap", "book value",
    ]):
        return "earnings"

    if any(w in q for w in [
        "sector", "industry", "which sector", "sector rotation",
        "tech stocks", "energy stocks", "healthcare stocks",
        "financial stocks", "bank stocks", "defense stocks",
        "ai stocks", "ev stocks", "crypto stocks", "cannabis",
        "real estate", "reit", "utilities", "consumer",
    ]):
        return "sector"

    if any(w in q for w in [
        "portfolio", "allocat", "diversif", "rebalance", "position size",
        "how much should i invest", "how much to put in", "weight",
        "concentration", "spread my money", "split my investment",
    ]):
        return "portfolio"

    if any(w in q for w in [
        "option", "call", "put", "strike", "expir", "premium",
        "iron condor", "straddle", "strangle", "covered call",
        "protective put", "spread", "theta", "delta", "gamma", "vega",
        "implied volatility", "iv", "greeks",
    ]):
        return "options"

    if any(w in q for w in [
        "crypto", "bitcoin", "ethereum", "btc", "eth", "blockchain",
        "defi", "web3", "nft",
    ]):
        return "crypto"

    if any(w in q for w in [
        "strategy", "strategies", "approach", "method", "system",
        "trading plan", "playbook", "setup", "edge",
        "swing trad", "day trad", "scalp", "position trad",
        "momentum", "mean reversion", "trend follow",
        "value invest", "growth invest", "income invest",
    ]):
        return "strategy"

    if any(w in q for w in [
        "money", "invest", "stock", "trade", "trading", "finance",
        "wealth", "retire", "saving", "capital", "return",
        "profit", "loss", "gain", "growth", "income",
    ]):
        return "general_finance"

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
    "support_resistance": {
        "name": "Support & Resistance",
        "explanation": "Support is a price level where buying pressure exceeds selling pressure, preventing the price from falling further. Resistance is where selling pressure exceeds buying, preventing the price from rising. These levels form because traders remember past prices — institutional orders cluster at round numbers and previous highs/lows.",
        "pro_tip": "The more times a level is tested, the weaker it becomes. A level that's been tested 4-5 times will likely break. When support breaks, it becomes resistance and vice versa."
    },
    "candlestick": {
        "name": "Candlestick Patterns",
        "explanation": "Candlestick patterns reveal buyer/seller psychology. Key reversal patterns: Hammer (bullish reversal at bottom), Shooting Star (bearish at top), Engulfing (large candle swallows previous), Doji (indecision). Continuation: Three White Soldiers (bullish), Three Black Crows (bearish). Always confirm with volume.",
        "pro_tip": "Single candlestick patterns are unreliable alone. The strongest signals combine a candlestick pattern + support/resistance + volume confirmation. A hammer at a key support level with high volume is one of the most reliable buy signals."
    },
    "fibonacci": {
        "name": "Fibonacci Retracements",
        "explanation": "Fibonacci levels (23.6%, 38.2%, 50%, 61.8%, 78.6%) mark potential reversal zones during pullbacks. Draw from a swing low to swing high (uptrend) or swing high to swing low (downtrend). The 61.8% level (Golden Ratio) is the most important — institutional algorithms target this level heavily.",
        "pro_tip": "The 'golden pocket' between 61.8% and 65% is where the highest probability reversals occur. Combine with RSI oversold for the best entries."
    },
}


def answer_question(question: str) -> dict:
    """
    Main function — takes a question and returns a pro-level analysis.
    Handles ANY free-form question about stocks, trading, investing, or markets.
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

    elif q_type == "buy_sell_recommendation" and not stock_data and not ticker:
        # No ticker mentioned — give general advice + top picks
        response_parts.append(_answer_general_buy_question(q_lower))
        try:
            picks = get_daily_picks()
            if picks and picks.get("picks"):
                response_parts.append("\n**Today's Top Picks:**")
                for p in picks["picks"][:5]:
                    response_parts.append(
                        f"  **{p['symbol']}** — ${p['price']} | {p['signal']} | "
                        f"Hold: {p.get('hold_label', 'N/A')} | RSI: {p['rsi']}"
                    )
                data_used.append("Live multi-factor stock screening")
        except Exception:
            pass

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

    elif q_type == "technical_analysis" and not stock_data:
        # Education about technical analysis concepts
        response_parts.append(_answer_technical_education(q_lower))

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

    elif q_type == "risk_assessment" and not stock_data:
        response_parts.append(_answer_risk_education(q_lower))

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
                response_parts.append(f"Bullish: {market.get('bullish_pct', 0)}% | Bearish: {market.get('bearish_pct', 0)}% | Neutral: {market.get('neutral_pct', 0)}%\n")
                if news.get("headlines"):
                    response_parts.append("**Top Headlines:**")
                    for h in news["headlines"][:8]:
                        sent = "+" if h["sentiment"] > 0 else "-" if h["sentiment"] < 0 else "~"
                        response_parts.append(f"  [{sent}] {h['title']} ({h['source']})")
                if news.get("macro_events"):
                    response_parts.append("\n**Macro Events:**")
                    for h in news["macro_events"][:5]:
                        response_parts.append(f"  - {h['title']} ({h['source']})")
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
            if news.get("headlines"):
                response_parts.append("\n**Top Headlines:**")
                for h in news["headlines"][:5]:
                    sent = "+" if h["sentiment"] > 0 else "-" if h["sentiment"] < 0 else "~"
                    response_parts.append(f"  [{sent}] {h['title']} ({h['source']})")
            data_used.append("Live news sentiment analysis")
        except Exception:
            response_parts.append("Market data temporarily unavailable.")

    elif q_type == "education":
        response_parts.append(_answer_education(q_lower))

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

    elif q_type == "comparison":
        response_parts.append(_answer_comparison(q_lower, question))

    elif q_type == "portfolio":
        response_parts.append(_answer_portfolio(q_lower))

    elif q_type == "options":
        response_parts.append(_answer_options_education(q_lower))

    elif q_type == "crypto":
        response_parts.append(_answer_crypto(q_lower))

    elif q_type == "sector":
        response_parts.append(_answer_sector(q_lower))
        try:
            news = get_market_news()
            if news.get("macro_events"):
                response_parts.append("\n**Current Macro Events:**")
                for h in news["macro_events"][:3]:
                    response_parts.append(f"  - {h['title']} ({h['source']})")
            data_used.append("Live macro news")
        except Exception:
            pass

    elif q_type == "strategy":
        response_parts.append(_answer_strategy(q_lower))

    elif q_type == "general_finance":
        response_parts.append(_answer_general_finance(q_lower))
        # If there's a ticker, supplement with data
        if stock_data:
            signal = stock_data["signal"]
            latest = stock_data["latest"]
            hold = stock_data["hold_duration"]
            response_parts.append(f"\n**{ticker} Quick Look:**")
            response_parts.append(f"Price: ${latest['price']} | Signal: {signal['direction']} ({signal['confidence']}%)")
            response_parts.append(f"RSI: {latest['rsi']} | Hold: {hold.get('label', 'N/A')} | Risk: {stock_data['risk']['score']}/10")

    else:
        # GENERAL CATCH-ALL — always try to give a meaningful response
        if stock_data:
            # If we have stock data, give a comprehensive overview
            signal = stock_data["signal"]
            latest = stock_data["latest"]
            hold = stock_data["hold_duration"]
            risk = stock_data["risk"]
            forecast = stock_data.get("forecast", {})
            news = stock_data.get("news_sentiment", {})
            pivot = stock_data.get("pivot_points", {})

            response_parts.append(f"**{ticker} — Comprehensive Analysis:**\n")
            response_parts.append(f"**Signal: {signal['direction']}** (Confidence: {signal['confidence']}%)")
            response_parts.append(f"Price: ${latest['price']} | RSI: {latest['rsi']}")
            response_parts.append(f"Trend: {stock_data['trend']['direction'].replace('_', ' ').title()}")
            response_parts.append(f"Hold Duration: {hold.get('label', 'N/A')} (~{hold.get('days', '?')} days)")
            response_parts.append(f"Risk: {risk['score']}/10 ({risk['label']})\n")

            if signal.get("reasons"):
                response_parts.append("**Key Factors:**")
                for r in signal["reasons"][:4]:
                    response_parts.append(f"  - {r}")

            if pivot:
                response_parts.append(f"\n**Pivot Levels:** S1 ${pivot.get('s1')} | Pivot ${pivot.get('pivot')} | R1 ${pivot.get('r1')}")

            if forecast and "forecasts" in forecast:
                f30 = next((f for f in forecast["forecasts"] if f["days"] == 30), None)
                if f30:
                    response_parts.append(f"\n**30-Day Forecast:** {f30['prob_up']}% chance up | Bull ${f30['targets']['bull']['price']} | Bear ${f30['targets']['bear']['price']}")

            market_sent = news.get("market_sentiment", {}).get("label", "")
            if market_sent:
                response_parts.append(f"\n**Market Sentiment:** {market_sent}")
        else:
            # No stock data, no specific type — give a smart general response
            response_parts.append(_answer_anything(q_lower, question))
            # Pull news to supplement
            try:
                news = get_market_news()
                if news.get("headlines"):
                    response_parts.append("\n**Latest Market Headlines:**")
                    for h in news["headlines"][:4]:
                        sent = "+" if h["sentiment"] > 0 else "-" if h["sentiment"] < 0 else "~"
                        response_parts.append(f"  [{sent}] {h['title']} ({h['source']})")
                    data_used.append("Live news feed")
            except Exception:
                pass

    # Add disclaimer
    response_parts.append("\n---\n*This is for educational purposes only. Not financial advice. Always do your own research.*")

    return {
        "answer": "\n".join(response_parts),
        "ticker": ticker,
        "question_type": q_type,
        "data_used": data_used,
    }


def _answer_education(q_lower):
    """Handle education questions — check knowledge base first, then give general answers."""
    # Check all knowledge base topics
    for key, info in TRADING_KNOWLEDGE.items():
        if key in q_lower or info["name"].lower() in q_lower:
            return f"**{info['name']}**\n\n{info['explanation']}\n\n**Pro Tip:** {info['pro_tip']}"

    # Additional topic matching
    topic_answers = {
        "short selling": (
            "**Short Selling**\n\n"
            "Short selling means borrowing shares and selling them, hoping to buy back at a lower price. "
            "You profit from the difference. Example: Short 100 shares at $50, buy back at $40 = $1,000 profit.\n\n"
            "**Risks:** Unlimited loss potential (stock can go to infinity). Short squeezes happen when "
            "too many shorts are forced to cover, driving the price up explosively (GameStop 2021).\n\n"
            "**Pro Tip:** Check the short interest ratio (days to cover). Above 5 days = high squeeze risk."
        ),
        "dollar cost averaging": (
            "**Dollar Cost Averaging (DCA)**\n\n"
            "Invest a fixed amount at regular intervals regardless of price. This removes emotion and "
            "timing risk. Example: $500/month into SPY. When prices are low, you buy more shares. "
            "When high, you buy fewer. Over time, your average cost is lower than the average price.\n\n"
            "**Pro Tip:** DCA into quality ETFs (VOO, QQQ) is one of the most reliable wealth-building "
            "strategies. It outperforms 80% of active fund managers over 10+ years."
        ),
        "market order": (
            "**Order Types Explained**\n\n"
            "- **Market Order:** Execute immediately at the best available price. Fast but you don't control the price.\n"
            "- **Limit Order:** Only execute at your specified price or better. You control the price but might not get filled.\n"
            "- **Stop Order:** Triggers a market order when price hits your stop level. Used for stop losses.\n"
            "- **Stop Limit:** Triggers a limit order at the stop price. More control but might not fill in fast markets.\n\n"
            "**Pro Tip:** Always use limit orders for options and low-volume stocks. Market orders can have terrible fills on illiquid names."
        ),
        "etf": (
            "**Exchange-Traded Funds (ETFs)**\n\n"
            "ETFs are baskets of stocks that trade like a single stock. They provide instant diversification.\n\n"
            "- **SPY/VOO:** Tracks S&P 500 (500 largest US companies)\n"
            "- **QQQ:** Tracks Nasdaq 100 (tech-heavy)\n"
            "- **DIA:** Tracks Dow Jones (30 blue chips)\n"
            "- **IWM:** Tracks Russell 2000 (small caps)\n"
            "- **VTI:** Total US stock market\n"
            "- **ARKK:** Innovation/disruptive tech (higher risk)\n\n"
            "**Pro Tip:** Expense ratios matter over time. VOO (0.03%) saves you thousands vs actively managed funds (1%+)."
        ),
        "margin": (
            "**Margin Trading**\n\n"
            "Margin lets you borrow money from your broker to buy more shares. 2:1 margin means $10K cash gives you "
            "$20K buying power. This amplifies both gains AND losses.\n\n"
            "**Margin Call:** If your account equity drops below the maintenance requirement (usually 25-30%), "
            "your broker forces you to deposit more money or sell positions. This often happens at the worst time.\n\n"
            "**Pro Tip:** Professional traders rarely use more than 1.5:1 leverage. Hedge funds that blow up "
            "almost always have excessive leverage (LTCM used 25:1)."
        ),
        "bear market": (
            "**Bear Market Survival Guide**\n\n"
            "A bear market is a 20%+ decline from recent highs. Average bear market lasts 9.6 months and "
            "drops 36%. They feel terrible, but they're normal — there have been 26 bear markets since 1929.\n\n"
            "**What to do:**\n"
            "1. Don't panic sell — most damage is done selling at the bottom\n"
            "2. Rotate to defensive sectors (Utilities, Healthcare, Consumer Staples)\n"
            "3. Increase cash position gradually, don't go all cash\n"
            "4. DCA into quality names at discounted prices\n"
            "5. Watch the VIX — extreme fear (VIX > 40) often marks bottoms\n\n"
            "**Pro Tip:** Every bear market in history has been followed by a new all-time high. Time in market beats timing the market."
        ),
        "bull market": (
            "**Bull Market Strategy**\n\n"
            "A bull market is a sustained 20%+ rise from recent lows. The average bull market lasts 2.7 years "
            "with 112% gains. The current playbook:\n\n"
            "1. Stay invested — don't try to time the top\n"
            "2. Favor growth and momentum stocks\n"
            "3. Use pullbacks to the 21 EMA as buying opportunities\n"
            "4. Gradually take profits at key resistance levels\n"
            "5. Trail stop losses to lock in gains\n\n"
            "**Pro Tip:** Bull markets climb a 'wall of worry.' If everyone is bullish, that's when you should be cautious."
        ),
    }

    for topic, answer in topic_answers.items():
        if topic in q_lower:
            return answer

    # Beginner check
    if any(w in q_lower for w in ["beginner", "start", "new to", "just starting", "getting started", "learn"]):
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

    # Strategy check
    if any(w in q_lower for w in ["strategy", "strategies"]):
        return _answer_strategy(q_lower)

    # Fallback — still give a smart response based on keywords
    return _answer_anything(q_lower, q_lower)


def _answer_technical_education(q_lower):
    """Give education about technical analysis when no ticker is provided."""
    for key, info in TRADING_KNOWLEDGE.items():
        if key in q_lower or info["name"].lower() in q_lower:
            return f"**{info['name']}**\n\n{info['explanation']}\n\n**Pro Tip:** {info['pro_tip']}"

    return (
        "**Technical Analysis Overview:**\n\n"
        "Technical analysis uses price and volume data to predict future movement. The key indicators:\n\n"
        "1. **RSI (Relative Strength Index)** — Momentum oscillator (0-100). < 30 = oversold, > 70 = overbought\n"
        "2. **EMA (Exponential Moving Average)** — 9/21/50 stack shows trend direction and strength\n"
        "3. **MACD** — Shows trend momentum and crossover signals\n"
        "4. **Bollinger Bands** — Volatility envelopes that predict breakouts\n"
        "5. **Pivot Points** — Support/resistance levels from previous period data\n\n"
        "To see these indicators for a specific stock, ask me something like:\n"
        "- \"Show me the technicals for AAPL\"\n"
        "- \"What's NVDA's RSI?\"\n\n"
        "**Pro Tip:** No single indicator is reliable alone. Institutional traders combine 2-3 confirming signals before taking a position."
    )


def _answer_risk_education(q_lower):
    """Answer risk-related questions without a specific ticker."""
    return (
        "**Understanding Investment Risk:**\n\n"
        "Risk in trading is measured several ways:\n\n"
        "1. **Beta** — How much a stock moves vs the market. Beta 1.5 = 50% more volatile than S&P 500\n"
        "2. **Volatility** — Standard deviation of returns. Higher = more risk\n"
        "3. **Max Drawdown** — Largest peak-to-trough decline. Shows worst-case scenario\n"
        "4. **Sharpe Ratio** — Risk-adjusted return. > 1 = good, > 2 = excellent\n"
        "5. **Value at Risk (VaR)** — Maximum expected loss at a given confidence level\n\n"
        "**Risk Management Rules:**\n"
        "- Risk no more than 1-2% of portfolio per trade\n"
        "- Always use stop losses\n"
        "- Diversify across sectors and asset classes\n"
        "- Position size = Risk Amount / (Entry - Stop Loss)\n\n"
        "To check the risk of a specific stock, ask me:\n"
        "- \"How risky is TSLA?\"\n"
        "- \"What's the risk score for NVDA?\"\n\n"
        "**Pro Tip:** The biggest risk is not volatility — it's permanent capital loss. Focus on quality companies with strong balance sheets."
    )


def _answer_general_buy_question(q_lower):
    """Answer general buy/sell questions when no ticker is specified."""
    return (
        "**What to Look For When Buying Stocks:**\n\n"
        "Here's how a quant analyst evaluates a stock:\n\n"
        "**Technical Signals (timing the entry):**\n"
        "- RSI below 40 (not overbought)\n"
        "- Price above EMA 21 and EMA 50 (uptrend confirmed)\n"
        "- MACD crossing above signal line (momentum turning up)\n"
        "- Volume above average on green days\n\n"
        "**Fundamental Signals (picking the right stock):**\n"
        "- Reasonable P/E ratio vs sector average\n"
        "- Revenue and earnings growing quarter over quarter\n"
        "- Strong balance sheet (low debt-to-equity)\n"
        "- Positive analyst sentiment\n\n"
        "**Macro Signals (reading the environment):**\n"
        "- Check Fed policy direction (rate cuts = bullish)\n"
        "- Monitor inflation data (CPI, PCE)\n"
        "- Watch geopolitical risks (tariffs, conflicts)\n"
        "- Track sector rotation trends\n\n"
        "**Want specific picks?** Ask me \"What are today's top picks?\" or \"Should I buy AAPL?\""
    )


def _answer_comparison(q_lower, question):
    """Handle comparison questions between stocks."""
    # Try to extract two tickers
    tickers_found = []
    q_upper = question.upper()
    for ticker in KNOWN_TICKERS:
        if ticker in q_upper.split():
            tickers_found.append(ticker)

    if len(tickers_found) >= 2:
        results = []
        for t in tickers_found[:2]:
            try:
                data = generate_full_report(t, period="1y")
                if "error" not in data:
                    results.append((t, data))
            except Exception:
                pass

        if len(results) == 2:
            parts = [f"**{results[0][0]} vs {results[1][0]} — Head to Head:**\n"]
            for t, d in results:
                signal = d["signal"]
                risk = d["risk"]
                hold = d["hold_duration"]
                forecast = d.get("forecast", {})
                f30 = None
                if forecast and "forecasts" in forecast:
                    f30 = next((f for f in forecast["forecasts"] if f["days"] == 30), None)

                parts.append(f"**{t}:**")
                parts.append(f"  Price: ${d['latest']['price']} | Signal: {signal['direction']} ({signal['confidence']}%)")
                parts.append(f"  RSI: {d['latest']['rsi']} | Trend: {d['trend']['direction'].replace('_', ' ')}")
                parts.append(f"  Risk: {risk['score']}/10 | Hold: {hold.get('label', 'N/A')}")
                if f30:
                    parts.append(f"  30-Day Up Prob: {f30['prob_up']}%")
                parts.append("")

            # Winner
            s0 = results[0][1]["signal"]["confidence"]
            s1 = results[1][1]["signal"]["confidence"]
            r0 = results[0][1]["risk"]["score"]
            r1 = results[1][1]["risk"]["score"]
            better = results[0][0] if (s0 > s1 and r0 <= r1) else results[1][0] if (s1 > s0 and r1 <= r0) else "Both have different strengths"
            if isinstance(better, str) and "Both" not in better:
                parts.append(f"**Edge:** {better} has a stronger signal with manageable risk.")
            else:
                parts.append(f"**Verdict:** Both have different profiles — {results[0][0]} and {results[1][0]} each have their own strengths. Check the detailed analysis for each.")

            return "\n".join(parts)

    return (
        "**How to Compare Stocks:**\n\n"
        "When comparing two stocks, look at these factors:\n\n"
        "1. **Signal Strength** — Which has a stronger buy/sell signal?\n"
        "2. **Risk Score** — Which has lower risk for similar upside?\n"
        "3. **RSI** — Is one oversold (better entry) vs overbought?\n"
        "4. **Trend** — Which has stronger momentum?\n"
        "5. **Sector** — Are they in the same sector or diversified?\n"
        "6. **Volatility** — Which has more stable returns?\n\n"
        "To compare specific stocks, ask me:\n"
        "- \"AAPL vs MSFT — which is better?\"\n"
        "- \"Compare NVDA and AMD\""
    )


def _answer_portfolio(q_lower):
    """Answer portfolio allocation questions."""
    return (
        "**Portfolio Allocation — Institutional Framework:**\n\n"
        "**Conservative (Low Risk):**\n"
        "- 60% Large-cap ETFs (VOO, SPY)\n"
        "- 20% Bonds (BND, TLT)\n"
        "- 10% International (VXUS)\n"
        "- 10% Cash/Short-term\n\n"
        "**Balanced (Moderate Risk):**\n"
        "- 50% Large-cap stocks/ETFs\n"
        "- 20% Growth stocks (tech, innovation)\n"
        "- 15% International/Emerging markets\n"
        "- 10% Bonds\n"
        "- 5% Alternative (REITs, commodities)\n\n"
        "**Aggressive (High Risk, High Reward):**\n"
        "- 40% Growth stocks (NVDA, TSLA, META type)\n"
        "- 30% Large-cap core (AAPL, MSFT, GOOGL)\n"
        "- 15% Small/mid-cap\n"
        "- 10% International\n"
        "- 5% Speculative (PLTR, COIN, etc.)\n\n"
        "**Position Sizing Rules:**\n"
        "- No single stock > 10% of portfolio\n"
        "- No single sector > 30% of portfolio\n"
        "- Keep 5-10% cash for opportunities\n\n"
        "**Pro Tip:** Rebalance quarterly. Sell winners that exceed target weight, buy laggards that dropped below. "
        "This forces 'buy low, sell high' systematically."
    )


def _answer_options_education(q_lower):
    """Answer options trading questions."""
    # Check knowledge base first
    if "options" in TRADING_KNOWLEDGE:
        info = TRADING_KNOWLEDGE["options"]
        base = f"**{info['name']}**\n\n{info['explanation']}\n\n**Pro Tip:** {info['pro_tip']}"
    else:
        base = ""

    specifics = ""
    if any(w in q_lower for w in ["covered call", "covered"]):
        specifics = (
            "\n\n**Covered Calls:**\n"
            "Own 100 shares + sell a call option against them. You collect premium income "
            "but cap your upside at the strike price. Best used on stocks you're neutral to slightly bullish on. "
            "Institutional investors use this to generate 2-5% extra annual return on their holdings."
        )
    elif any(w in q_lower for w in ["iron condor", "condor"]):
        specifics = (
            "\n\n**Iron Condor:**\n"
            "Sell a call spread AND a put spread simultaneously. You profit when the stock stays within a range. "
            "Max profit = premium collected. Works best in low-volatility environments. "
            "This is a bread-and-butter strategy for market makers."
        )
    elif any(w in q_lower for w in ["straddle", "strangle"]):
        specifics = (
            "\n\n**Straddle vs Strangle:**\n"
            "Both are volatility plays (you profit from a big move in either direction).\n"
            "- Straddle: Buy call + put at same strike. More expensive but profits faster.\n"
            "- Strangle: Buy call + put at different strikes. Cheaper but needs a bigger move.\n"
            "Best used before earnings or big events when IV is expected to spike."
        )

    return base + specifics if base else (
        "**Options Trading Basics:**\n\n"
        "Options give you the right to buy (call) or sell (put) a stock at a set price by a certain date.\n\n"
        "**Key Concepts:**\n"
        "- **Call Option:** Right to BUY at strike price — profits when stock goes UP\n"
        "- **Put Option:** Right to SELL at strike price — profits when stock goes DOWN\n"
        "- **Premium:** The price you pay for the option\n"
        "- **Expiration:** When the option expires worthless if not exercised\n"
        "- **Greeks:** Delta (direction), Theta (time decay), Vega (volatility), Gamma (delta acceleration)\n\n"
        "**Pro Tip:** 80% of options expire worthless. The odds favor sellers. Start with covered calls on stocks you already own."
        + specifics
    )


def _answer_crypto(q_lower):
    """Answer crypto-related questions."""
    return (
        "**Cryptocurrency Insights:**\n\n"
        "Crypto is a high-risk, high-reward asset class. Key principles:\n\n"
        "**Bitcoin (BTC):**\n"
        "- Digital gold / store of value narrative\n"
        "- 4-year halving cycle drives price cycles\n"
        "- Institutional adoption increasing (ETFs, corporate treasuries)\n"
        "- Correlates with risk-on assets (tech stocks) and inversely with DXY (dollar strength)\n\n"
        "**Ethereum (ETH):**\n"
        "- Platform for DeFi, NFTs, and smart contracts\n"
        "- Proof of Stake since 2022 (reduced supply issuance)\n"
        "- Developer ecosystem is the largest in crypto\n\n"
        "**Trading Crypto in the Stock Market:**\n"
        "- COIN (Coinbase) — crypto exchange stock\n"
        "- Bitcoin ETFs — track BTC price without holding crypto\n"
        "- MARA, RIOT — Bitcoin mining stocks\n\n"
        "**Risk Warning:** Crypto volatility is 3-5x that of stocks. Never invest more than you can afford to lose. "
        "Use small position sizes (5-10% of portfolio max for crypto).\n\n"
        "**Pro Tip:** Watch Bitcoin dominance. When BTC dominance falls, altcoins outperform (alt season). "
        "When dominance rises, rotate back to BTC."
    )


def _answer_sector(q_lower):
    """Answer sector-specific questions."""
    sector_info = {
        "tech": (
            "**Technology Sector Analysis:**\n\n"
            "Tech is the largest S&P 500 sector (~30% weight). Key dynamics:\n\n"
            "- **AI/Semiconductor boom:** NVDA, AMD, AVGO, ASML driving growth\n"
            "- **Cloud/SaaS:** MSFT, AMZN, GOOGL, CRM — recurring revenue models\n"
            "- **Consumer tech:** AAPL, META — strong cash flows and buybacks\n\n"
            "**When to be bullish tech:** Falling interest rates, strong earnings, AI adoption accelerating\n"
            "**When to be cautious:** Rising rates, regulatory crackdowns, valuation multiples stretched\n\n"
            "**Pro Tip:** In late cycle, rotate from high-beta tech to cash-flow positive tech (AAPL, MSFT over SNOW, PLTR)."
        ),
        "energy": (
            "**Energy Sector Analysis:**\n\n"
            "Energy stocks track oil/gas prices and are late-cycle performers:\n\n"
            "- **Majors:** XOM, CVX — diversified, strong dividends\n"
            "- **E&P:** EOG, DVN, FANG — higher leverage to oil prices\n"
            "- **Services:** SLB, HAL — benefit from drilling activity\n\n"
            "**Key drivers:** OPEC production cuts, geopolitical risk, global demand, energy transition timeline\n\n"
            "**Pro Tip:** Watch the crack spread (refining margin) and WTI contango/backwardation for timing signals."
        ),
        "health": (
            "**Healthcare Sector Analysis:**\n\n"
            "Healthcare is defensive with growth potential:\n\n"
            "- **Big Pharma:** LLY, MRK, ABBV, PFE — patent cliffs and pipeline matter\n"
            "- **Biotech:** AMGN, GILD, BIIB — binary event risk from drug approvals\n"
            "- **Insurance:** UNH — largest healthcare company by market cap\n"
            "- **Medical Devices:** ABT, TMO — steady growth with aging population\n\n"
            "**Pro Tip:** Healthcare outperforms during recessions. Rotate into healthcare when the yield curve inverts."
        ),
        "ai": (
            "**AI Stocks — The 2024-2026 Theme:**\n\n"
            "AI is reshaping every sector. The key beneficiaries:\n\n"
            "- **Chips:** NVDA (GPUs), AMD (MI300), AVGO (custom chips), ASML (lithography)\n"
            "- **Cloud/Infrastructure:** MSFT (Azure+OpenAI), GOOGL (Gemini), AMZN (AWS Bedrock)\n"
            "- **Software:** CRM (Einstein), ADBE (Firefly), PLTR (AIP), NOW (AI agents)\n"
            "- **Data:** MDB, SNOW, DDOG — data infrastructure for AI\n\n"
            "**Risk:** AI stocks trade at high multiples. Any slowdown in AI spend growth could cause sharp corrections.\n\n"
            "**Pro Tip:** Follow the capex. When MSFT, GOOGL, META, AMZN increase AI infrastructure spending, "
            "that's bullish for the entire AI supply chain."
        ),
        "defense": (
            "**Defense & Aerospace Sector:**\n\n"
            "Defense stocks benefit from geopolitical tension and government spending:\n\n"
            "- **Primes:** LMT, RTX, NOC, GD — large, stable government contractors\n"
            "- Revenue visibility is high (multi-year contracts)\n"
            "- Bipartisan support for defense spending\n\n"
            "**Pro Tip:** Defense stocks are uncorrelated with the broader market, making them good portfolio diversifiers."
        ),
    }

    for key, answer in sector_info.items():
        if key in q_lower:
            return answer

    # Default sector rotation overview
    return TRADING_KNOWLEDGE["sector_rotation"]["explanation"] + "\n\n**Pro Tip:** " + TRADING_KNOWLEDGE["sector_rotation"]["pro_tip"]


def _answer_strategy(q_lower):
    """Answer trading strategy questions."""
    strategy_details = {
        "swing": (
            "**Swing Trading Strategy:**\n\n"
            "Hold positions 1-4 weeks, targeting 5-15% moves.\n\n"
            "**Entry Rules:**\n"
            "1. Stock in uptrend (above EMA 21 and 50)\n"
            "2. RSI pulls back to 40-50 range (buying the dip)\n"
            "3. Price bounces off EMA 21 or key support\n"
            "4. MACD turning positive or crossing signal line\n"
            "5. Volume expanding on bounce day\n\n"
            "**Exit Rules:**\n"
            "- Take profit at resistance or R1/R2 pivot level\n"
            "- Stop loss below EMA 50 or recent swing low\n"
            "- Trail stop to break-even after 5% gain\n\n"
            "**Pro Tip:** The best swing trades happen after a 3-5 day pullback in a strong uptrend. "
            "Monday/Tuesday entries tend to outperform as institutional buying picks up mid-week."
        ),
        "day trad": (
            "**Day Trading Framework:**\n\n"
            "Day trading requires intense focus and discipline. Most day traders lose money.\n\n"
            "**Rules:**\n"
            "1. Never hold overnight\n"
            "2. Focus on 2-3 stocks max per day\n"
            "3. Risk < 1% of account per trade\n"
            "4. Trade the first 30 min and last 30 min (highest volume)\n"
            "5. Use 1-minute and 5-minute charts\n\n"
            "**Key levels:** VWAP (volume weighted average price), previous day's high/low, premarket high/low\n\n"
            "**Pro Tip:** Paper trade for at least 3 months before risking real money. If you can't be profitable "
            "in a simulator, you won't be profitable with real money."
        ),
        "momentum": (
            "**Momentum Trading:**\n\n"
            "Buy stocks showing strong upward momentum, sell when momentum fades.\n\n"
            "**Momentum Signals:**\n"
            "- RSI above 50 and rising\n"
            "- Price above all major EMAs (9 > 21 > 50)\n"
            "- Volume above 20-day average\n"
            "- Making new 52-week highs\n"
            "- Relative strength vs S&P 500 positive\n\n"
            "**Exit Signals:**\n"
            "- RSI divergence (price up but RSI down)\n"
            "- EMA crossover (9 crosses below 21)\n"
            "- Volume drying up on rallies\n\n"
            "**Pro Tip:** Momentum works best in trending markets. In choppy/sideways markets, switch to mean reversion."
        ),
        "value": (
            "**Value Investing (Warren Buffett Approach):**\n\n"
            "Buy quality companies trading below their intrinsic value and hold for the long term.\n\n"
            "**Value Criteria:**\n"
            "- P/E below sector average\n"
            "- PEG ratio < 1 (growth at a reasonable price)\n"
            "- Strong balance sheet (low debt/equity < 0.5)\n"
            "- Consistent revenue and earnings growth\n"
            "- High return on equity (ROE > 15%)\n"
            "- Durable competitive advantage (moat)\n\n"
            "**Pro Tip:** The best value opportunities appear during market panics. Keep a watchlist of quality "
            "stocks and buy when they drop 20-30% on market-wide fear (not company-specific problems)."
        ),
    }

    for key, answer in strategy_details.items():
        if key in q_lower:
            return answer

    return (
        "**Trading Strategies — Overview:**\n\n"
        "1. **Swing Trading** — Hold 1-4 weeks, target 5-15% moves using EMA/RSI setups\n"
        "2. **Momentum Trading** — Buy strongest stocks making new highs, ride the trend\n"
        "3. **Mean Reversion** — Buy oversold (RSI < 30), sell overbought (RSI > 70)\n"
        "4. **Value Investing** — Buy undervalued stocks (low P/E, strong fundamentals), hold long term\n"
        "5. **Breakout Trading** — Buy when price breaks above resistance with volume confirmation\n"
        "6. **Trend Following** — EMA stack alignment (9 > 21 > 50 = bullish)\n\n"
        "Ask me about any specific strategy for detailed entry/exit rules and pro tips!\n\n"
        "**Pro Tip:** The best strategy is one you can follow consistently. Backtesting on historical data "
        "is essential before trading real money."
    )


def _answer_general_finance(q_lower):
    """Handle general finance questions that don't fit other categories."""
    topic_answers = {
        "retire": (
            "**Retirement Investing:**\n\n"
            "- **401k/IRA:** Max out tax-advantaged accounts first\n"
            "- **Age-based allocation:** Rule of thumb: 110 - your age = stock allocation %\n"
            "- **At 25:** 85% stocks (growth), 15% bonds\n"
            "- **At 45:** 65% stocks (balanced), 35% bonds\n"
            "- **At 60:** 50% stocks (conservative), 40% bonds, 10% cash\n\n"
            "**The Power of Compound Growth:**\n"
            "$500/month at 10% annual return:\n"
            "- 10 years: $102,000\n"
            "- 20 years: $383,000\n"
            "- 30 years: $1,130,000\n\n"
            "**Pro Tip:** Start as early as possible. 10 years of compounding is worth more than doubling your monthly contribution."
        ),
        "tax": (
            "**Tax-Efficient Investing:**\n\n"
            "- **Long-term capital gains** (held > 1 year): 0-20% tax rate\n"
            "- **Short-term capital gains** (held < 1 year): Taxed as ordinary income (up to 37%)\n"
            "- **Tax-loss harvesting:** Sell losers to offset gains\n"
            "- **Wash sale rule:** Can't buy back the same stock within 30 days of selling at a loss\n\n"
            "**Pro Tip:** Hold winners for at least 1 year to qualify for long-term rates. "
            "This alone can save you 10-20% on your investment gains."
        ),
        "wealth": (
            "**Building Wealth — The Framework:**\n\n"
            "1. **Emergency fund first** — 6 months of expenses in high-yield savings\n"
            "2. **Max out tax-advantaged accounts** — 401k, IRA, HSA\n"
            "3. **Index fund core** — 60-70% in VOO/QQQ for steady growth\n"
            "4. **Satellite positions** — 20-30% in individual stocks you've researched\n"
            "5. **Never stop learning** — Markets evolve; your strategy should too\n\n"
            "**Pro Tip:** Automate your investing. Set up automatic monthly investments. "
            "This removes emotion and takes advantage of dollar cost averaging."
        ),
    }

    for key, answer in topic_answers.items():
        if key in q_lower:
            return answer

    return (
        "**Smart Investing Principles:**\n\n"
        "1. **Diversify** — Don't put all eggs in one basket. Mix sectors, asset classes, and geographies\n"
        "2. **Know your risk tolerance** — Only take risk you can sleep with at night\n"
        "3. **Have a plan** — Entry, exit, position size decided BEFORE you trade\n"
        "4. **Use data, not emotions** — RSI, EMA, fundamentals > gut feelings\n"
        "5. **Stay informed** — Follow macro events (Fed, inflation, earnings season)\n\n"
        "I can help with specific stocks, strategies, or market analysis. Try asking:\n"
        "- \"Should I buy NVDA?\"\n"
        "- \"What are the best stocks right now?\"\n"
        "- \"How is the market doing?\"\n"
        "- \"Explain swing trading\""
    )


def _answer_anything(q_lower, question):
    """
    Ultimate fallback — give a meaningful response to ANY question.
    Never return a generic 'here's what I can do' message.
    Instead, analyze the question and provide real value.
    """
    # Check knowledge base one more time
    for key, info in TRADING_KNOWLEDGE.items():
        if key in q_lower:
            return f"**{info['name']}**\n\n{info['explanation']}\n\n**Pro Tip:** {info['pro_tip']}"

    # Greeting / casual
    if any(w in q_lower for w in ["hello", "hi", "hey", "sup", "what's up", "whats up", "yo"]):
        return (
            "**Hey! I'm your AI Stock Analyst.**\n\n"
            "I analyze stocks in real-time using the same indicators institutional traders use — "
            "RSI, EMA crossovers, MACD, pivot points, and live news sentiment.\n\n"
            "What would you like to know? You can ask me literally anything about:\n"
            "- Any specific stock (\"Should I buy NVDA?\")\n"
            "- Market conditions (\"How is the market?\")\n"
            "- Trading strategies (\"Explain swing trading\")\n"
            "- Stock picks (\"What should I buy today?\")\n"
            "- Portfolio advice (\"How should I allocate $10K?\")\n"
            "- Or any other investing question!"
        )

    # Thank you
    if any(w in q_lower for w in ["thank", "thanks", "appreciate"]):
        return (
            "You're welcome! I'm here to help with any stock or market questions. "
            "Keep asking — the more you learn, the better your trading decisions will be.\n\n"
            "Remember: knowledge is the best hedge against risk."
        )

    # Opinion / what do you think
    if any(w in q_lower for w in ["what do you think", "your opinion", "your thoughts", "do you think"]):
        # Try to give market outlook
        try:
            news = get_market_news()
            market = news.get("market_sentiment", {})
            parts = ["**Current Market View:**\n"]
            parts.append(f"Market Sentiment: {market.get('label', 'Mixed')} (Score: {market.get('score', 0)})")
            parts.append(f"Bullish signals: {market.get('bullish_pct', 0)}% | Bearish: {market.get('bearish_pct', 0)}%\n")

            if market.get("score", 0) > 0.2:
                parts.append("The overall bias is cautiously bullish. Look for pullbacks to enter quality names.")
            elif market.get("score", 0) < -0.2:
                parts.append("The market is showing bearish signals. Consider reducing exposure or adding hedges.")
            else:
                parts.append("The market is mixed/choppy. Be selective and wait for clear setups before entering.")

            parts.append("\nFor a specific stock analysis, mention a ticker and I'll give you detailed data!")
            return "\n".join(parts)
        except Exception:
            pass

    # Money amount questions ($10k, $1000, etc.)
    if any(w in q_lower for w in ["$", "dollar", "money", "invest", "1000", "5000", "10000", "100000", "10k", "50k", "100k"]):
        return _answer_portfolio(q_lower)

    # When / timing questions
    if any(w in q_lower for w in ["when should", "best time", "right time", "too late", "timing"]):
        return (
            "**Market Timing — What the Data Says:**\n\n"
            "Studies show that time IN the market beats timing THE market:\n"
            "- Missing the 10 best days over 20 years cuts returns by over 50%\n"
            "- The best days often come right after the worst days\n"
            "- Dollar cost averaging removes timing risk entirely\n\n"
            "**But if you want to time entries:**\n"
            "1. Buy when RSI < 40 on a stock in an uptrend\n"
            "2. Buy at support levels (pivot S1/S2)\n"
            "3. Buy after a 3-5 day pullback with volume declining\n"
            "4. Buy when VIX spikes above 30 (fear = opportunity)\n\n"
            "**Pro Tip:** It's never too late to start investing, but it CAN be too late to enter a specific trade. "
            "Ask me about a specific stock and I'll tell you if the entry is still good."
        )

    # Why questions
    if q_lower.startswith("why"):
        return (
            "**Great question!**\n\n"
            "Markets move based on several forces:\n\n"
            "1. **Earnings & Revenue** — Companies that beat expectations go up, those that miss go down\n"
            "2. **Interest Rates** — Fed rate decisions affect all asset prices\n"
            "3. **Sentiment** — Fear and greed drive short-term moves\n"
            "4. **Technical Levels** — Support/resistance, moving averages create self-fulfilling zones\n"
            "5. **Macro Events** — Inflation data, employment, GDP, geopolitical events\n\n"
            "For a specific analysis, mention a stock ticker and I'll pull live data to explain what's happening!"
        )

    # Catch absolutely everything else — still give value
    return (
        "**Let me help you with that!**\n\n"
        "I'm your AI Stock Analyst powered by live market data. Here's what I can analyze in real-time:\n\n"
        "**Stock Analysis:**\n"
        "- \"Should I buy AAPL?\" — Buy/sell signals with EMA, RSI, MACD\n"
        "- \"How long should I hold TSLA?\" — Data-driven hold duration\n"
        "- \"Is NVDA risky?\" — Risk score with detailed breakdown\n"
        "- \"Price target for AMZN\" — Probabilistic price forecasts\n\n"
        "**Market Intelligence:**\n"
        "- \"How is the market today?\" — Live sentiment from CNN, CNBC, Yahoo Finance\n"
        "- \"What are today's top picks?\" — Algorithmically screened stocks to buy\n"
        "- \"What's the news?\" — Latest headlines with sentiment scoring\n\n"
        "**Education:**\n"
        "- \"Explain RSI\" — Trading concepts with pro tips\n"
        "- \"What's a good strategy?\" — Detailed trading strategies\n"
        "- \"How to build a portfolio\" — Institutional allocation frameworks\n\n"
        "Just type any question — I'll give you a data-driven answer!"
    )
