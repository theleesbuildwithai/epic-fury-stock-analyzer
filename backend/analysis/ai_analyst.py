"""
AI Stock Analyst — Powered by Claude (Anthropic).

Answers ANY question by combining:
1. Claude AI for natural language understanding and reasoning
2. Live market data from Yahoo Finance
3. Technical analysis (EMA, RSI, MACD, pivot points)
4. News sentiment from CNN, CNBC, Yahoo Finance

This is a real AI — it can answer anything, from stock analysis to general knowledge.
"""

import os
import re
import json
import traceback
from datetime import datetime

# Try to import anthropic — graceful fallback if not available
try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

from analysis.report import generate_full_report
from analysis.news_sentiment import get_market_news, get_stock_sentiment
from analysis.extras import get_daily_picks

# Known tickers for extraction
KNOWN_TICKERS = {
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NVDA", "TSLA",
    "JPM", "JNJ", "UNH", "V", "MA", "PG", "HD", "XOM", "CVX",
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

COMMON_WORDS = {
    "I", "A", "THE", "AND", "OR", "IS", "IT", "AT", "TO", "IN", "ON",
    "FOR", "OF", "BY", "AN", "BE", "DO", "IF", "MY", "NO", "UP", "SO",
    "AS", "AM", "ARE", "WAS", "HAS", "HAD", "HOW", "WHO", "WHY", "CAN",
    "ALL", "NEW", "NOW", "OLD", "BUY", "SELL", "HOLD", "WHAT", "WHEN",
    "GOOD", "BEST", "LONG", "THAT", "THIS", "WITH", "FROM", "WILL",
    "AI", "US", "UK", "EU", "USA", "VS", "GO", "OK", "NOT", "YOU",
}

COMPANY_MAP = {
    "apple": "AAPL", "microsoft": "MSFT", "google": "GOOGL",
    "alphabet": "GOOGL", "amazon": "AMZN", "meta": "META",
    "facebook": "META", "nvidia": "NVDA", "tesla": "TSLA",
    "netflix": "NFLX", "costco": "COST", "walmart": "WMT",
    "disney": "DIS", "nike": "NKE", "starbucks": "SBUX",
    "paypal": "PYPL", "coinbase": "COIN", "uber": "UBER",
    "airbnb": "ABNB", "snowflake": "SNOW", "palantir": "PLTR",
    "rivian": "RIVN", "lucid": "LCID", "shopify": "SHOP",
    "crowdstrike": "CRWD", "cloudflare": "NET", "datadog": "DDOG",
    "amd": "AMD", "intel": "INTC", "broadcom": "AVGO",
    "boeing": "BA", "caterpillar": "CAT", "coca-cola": "KO",
    "coke": "KO", "pepsi": "PEP", "mcdonald": "MCD",
    "mcdonalds": "MCD", "goldman": "GS", "jp morgan": "JPM",
    "jpmorgan": "JPM", "bank of america": "BAC", "pfizer": "PFE",
    "exxon": "XOM", "chevron": "CVX",
}


def _extract_ticker(question):
    """Extract a stock ticker from the question."""
    q_lower = question.lower()
    q_upper = question.upper()

    # Check company names first
    for name, ticker in COMPANY_MAP.items():
        if name in q_lower:
            return ticker

    # Check known tickers in the words
    for word in q_upper.split():
        clean = re.sub(r'[^A-Z]', '', word)
        if clean in KNOWN_TICKERS and clean not in COMMON_WORDS:
            return clean

    return None


def _get_market_context(ticker=None):
    """Gather live market data to give Claude as context."""
    context_parts = []
    data_used = []

    # Get news headlines
    try:
        news = get_market_news()
        headlines = news.get("headlines", [])[:10]
        if headlines:
            news_text = "\n".join([f"- {h.get('title', '')} ({h.get('source', '')})" for h in headlines])
            context_parts.append(f"LATEST MARKET NEWS (live):\n{news_text}")
            data_used.append("Live market news")
    except Exception:
        pass

    # Get stock-specific data if ticker found
    if ticker:
        try:
            report = generate_full_report(ticker)
            if report and "error" not in report:
                price = report.get("current_price", "N/A")
                change = report.get("change_percent", 0)
                signal = report.get("signal", "N/A")
                confidence = report.get("confidence", 0)
                rsi = report.get("rsi", "N/A")
                risk = report.get("risk_score", "N/A")

                # EMA data
                ema9 = report.get("ema_9", "N/A")
                ema21 = report.get("ema_21", "N/A")
                ema50 = report.get("ema_50", "N/A")

                # Pivot points
                pivots = report.get("pivot_points", {})
                pivot_text = ""
                if pivots:
                    pivot_text = f"\nPivot Points: S3={pivots.get('s3','N/A')}, S2={pivots.get('s2','N/A')}, S1={pivots.get('s1','N/A')}, Pivot={pivots.get('pivot','N/A')}, R1={pivots.get('r1','N/A')}, R2={pivots.get('r2','N/A')}, R3={pivots.get('r3','N/A')}"

                # MACD
                macd = report.get("macd", "N/A")
                macd_signal = report.get("macd_signal", "N/A")
                macd_hist = report.get("macd_histogram", "N/A")

                # Forecast
                forecast = report.get("forecast", {})
                forecast_text = ""
                if forecast:
                    f7 = forecast.get("7_day", {})
                    f30 = forecast.get("30_day", {})
                    forecast_text = f"\nForecast: 7-day bull={f7.get('bull_target','N/A')} bear={f7.get('bear_target','N/A')} prob_up={f7.get('probability_up','N/A')}% | 30-day bull={f30.get('bull_target','N/A')} bear={f30.get('bear_target','N/A')} prob_up={f30.get('probability_up','N/A')}%"

                # Hold duration
                hold = report.get("hold_duration", {})
                hold_text = ""
                if hold:
                    hold_text = f"\nHold Duration Recommendation: {hold.get('label','N/A')} ({hold.get('days','N/A')} days) - Reasoning: {', '.join(hold.get('reasoning', []))}"

                # Company info
                name = report.get("company_name", ticker)
                sector = report.get("sector", "N/A")
                market_cap = report.get("market_cap", "N/A")

                stock_context = f"""LIVE DATA FOR {ticker} ({name}):
Sector: {sector} | Market Cap: {market_cap}
Current Price: ${price} | Change: {change}%
Signal: {signal} (Confidence: {confidence}%)
RSI: {rsi} | Risk Score: {risk}/10
EMA 9: {ema9} | EMA 21: {ema21} | EMA 50: {ema50}
MACD: {macd} | Signal: {macd_signal} | Histogram: {macd_hist}{pivot_text}{forecast_text}{hold_text}"""

                context_parts.append(stock_context)
                data_used.append(f"Live analysis of {ticker}")
        except Exception as e:
            context_parts.append(f"Note: Could not fetch live data for {ticker}: {str(e)}")

        # Stock-specific news sentiment
        try:
            sentiment = get_stock_sentiment(ticker)
            if sentiment and sentiment.get("headlines"):
                sent_headlines = sentiment["headlines"][:5]
                sent_text = "\n".join([f"- [{h.get('sentiment','neutral')}] {h.get('title','')}" for h in sent_headlines])
                context_parts.append(f"\n{ticker} NEWS SENTIMENT:\nOverall: {sentiment.get('overall_sentiment', 'N/A')} (Score: {sentiment.get('sentiment_score', 0)})\n{sent_text}")
                data_used.append(f"{ticker} news sentiment")
        except Exception:
            pass

    # Get daily picks for context
    try:
        picks = get_daily_picks()
        if picks and picks.get("picks"):
            top5 = picks["picks"][:5]
            picks_text = "\n".join([
                f"- {p.get('ticker','?')} ${p.get('price','?')} | {p.get('signal','?')} | RSI: {p.get('rsi','?')} | Hold: {p.get('hold_duration',{}).get('label','?')}"
                for p in top5
            ])
            context_parts.append(f"\nTODAY'S TOP PICKS:\n{picks_text}")
            data_used.append("Daily stock screening")
    except Exception:
        pass

    context_parts.append(f"\nCurrent date/time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    return "\n\n".join(context_parts), data_used


def _ask_claude(question, market_context, ticker=None):
    """Send the question to Claude with market context."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None, "No API key configured"

    client = anthropic.Anthropic(api_key=api_key)

    system_prompt = """You are the AI Stock Analyst for Epic Fury Stock Analyzer — a hedge fund-grade trading platform. You think like a senior quantitative analyst at Renaissance Technologies or Citadel.

YOUR CAPABILITIES:
- You can answer ANY question — stocks, trading, investing, market concepts, general knowledge, time zones, math, anything
- For stock questions, you have LIVE market data provided below — always reference the actual numbers
- You give specific, actionable advice with real data points
- You format responses with bold (**text**) for emphasis and use bullet points
- Keep responses focused and clear — no unnecessary padding

PERSONALITY:
- Professional but approachable — like talking to a smart friend who works at a hedge fund
- Confident and direct — give clear opinions backed by data
- Use actual numbers from the live data when available
- If the user asks about a stock, reference the real-time RSI, EMA, MACD, pivot points, etc.
- If asked a non-stock question (time, weather, general knowledge), answer it naturally

FORMATTING RULES:
- Use **bold** for key terms, tickers, and important values
- Use bullet points (- ) for lists
- Use --- for section dividers
- Keep paragraphs short
- Always end with a disclaimer line: "---\n*This is for educational purposes only. Not financial advice. Always do your own research.*"

IMPORTANT: You have access to LIVE market data below. Use it. Don't make up prices or data — use the actual numbers provided."""

    user_message = f"""LIVE MARKET DATA:
{market_context}

---

USER QUESTION: {question}"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            system=system_prompt,
            messages=[
                {"role": "user", "content": user_message}
            ]
        )
        return response.content[0].text, None
    except Exception as e:
        return None, str(e)


def answer_question(question):
    """Main entry point — answer any question using Claude + live data."""
    ticker = _extract_ticker(question)
    market_context, data_used = _get_market_context(ticker)

    # Try Claude first
    if ANTHROPIC_AVAILABLE:
        answer, error = _ask_claude(question, market_context, ticker)
        if answer:
            return {
                "answer": answer,
                "ticker": ticker,
                "question_type": "ai_powered",
                "data_used": data_used,
            }
        elif error:
            # If Claude fails, fall back to basic response with market data
            return _fallback_answer(question, ticker, market_context, data_used, error)
    else:
        return _fallback_answer(question, ticker, market_context, data_used, "Claude API not installed")


def _fallback_answer(question, ticker, market_context, data_used, error_info=""):
    """Fallback when Claude API is unavailable — give a basic but useful response."""
    parts = []

    if "api key" in error_info.lower() or "no api key" in error_info.lower():
        parts.append("**Note:** AI chat is being configured. In the meantime, here's what I have from live data:\n")
    elif error_info:
        parts.append(f"**Note:** AI temporarily unavailable ({error_info}). Here's live data instead:\n")

    if ticker:
        try:
            report = generate_full_report(ticker)
            if report and "error" not in report:
                price = report.get("current_price", "N/A")
                signal = report.get("signal", "N/A")
                confidence = report.get("confidence", 0)
                rsi = report.get("rsi", "N/A")
                change = report.get("change_percent", 0)
                parts.append(f"**{ticker}** — ${price} ({'+' if change >= 0 else ''}{change}%)")
                parts.append(f"**Signal:** {signal} (Confidence: {confidence}%)")
                parts.append(f"**RSI:** {rsi}")

                hold = report.get("hold_duration", {})
                if hold:
                    parts.append(f"**Hold Duration:** {hold.get('label', 'N/A')} ({hold.get('days', 'N/A')} days)")

                forecast = report.get("forecast", {})
                f30 = forecast.get("30_day", {})
                if f30:
                    parts.append(f"**30-Day Forecast:** Bull target ${f30.get('bull_target', 'N/A')} | Bear target ${f30.get('bear_target', 'N/A')}")
        except Exception:
            parts.append(f"Could not fetch data for {ticker}")
    else:
        # No ticker — show general market info
        try:
            news = get_market_news()
            headlines = news.get("headlines", [])[:5]
            if headlines:
                parts.append("**Latest Market Headlines:**")
                for h in headlines:
                    parts.append(f"- {h.get('title', '')} ({h.get('source', '')})")
        except Exception:
            pass

        try:
            picks = get_daily_picks()
            if picks and picks.get("picks"):
                parts.append("\n**Today's Top Picks:**")
                for p in picks["picks"][:5]:
                    parts.append(f"- **{p.get('ticker','')}** ${p.get('price','')} | {p.get('signal','')} | RSI: {p.get('rsi','')}")
        except Exception:
            pass

    if not parts:
        parts.append("I couldn't process that question. Try asking about a specific stock like \"Should I buy AAPL?\" or \"What are today's top picks?\"")

    parts.append("\n---\n*This is for educational purposes only. Not financial advice. Always do your own research.*")

    return {
        "answer": "\n".join(parts),
        "ticker": ticker,
        "question_type": "fallback",
        "data_used": data_used,
    }
