"""
News Sentiment Engine — Fetches headlines from Yahoo Finance, CNN, and CNBC RSS feeds.
Analyzes sentiment to factor into buy/sell decisions like a hedge fund would.

Uses keyword-based sentiment scoring (fast, no ML dependencies needed).
Caches results to avoid excessive fetching.
"""

import urllib.request
import xml.etree.ElementTree as ET
import re
import time
import ssl
from datetime import datetime

_news_cache = {}
_NEWS_CACHE_TTL = 300  # 5 minutes

# Sentiment keyword dictionaries (hedge fund style)
BULLISH_KEYWORDS = [
    "surge", "soar", "rally", "gain", "jump", "rise", "beat", "record",
    "upgrade", "outperform", "buy", "growth", "profit", "revenue beat",
    "strong earnings", "positive", "optimistic", "boom", "upbeat",
    "recovery", "expansion", "bullish", "breakthrough", "milestone",
    "stimulus", "rate cut", "fed cut", "dovish", "deal", "merger",
    "acquisition", "innovation", "demand", "hiring", "job growth",
    "consumer spending", "GDP growth", "all-time high", "breakout",
]

BEARISH_KEYWORDS = [
    "crash", "plunge", "drop", "fall", "decline", "sell", "loss",
    "miss", "downgrade", "underperform", "warning", "recession",
    "layoff", "cut", "weak", "negative", "pessimistic", "slump",
    "crisis", "default", "bankruptcy", "inflation", "rate hike",
    "hawkish", "tariff", "sanctions", "war", "conflict", "tension",
    "geopolitical", "shutdown", "debt ceiling", "bear market",
    "correction", "volatility spike", "fear", "panic", "sell-off",
    "investigation", "fraud", "lawsuit", "recall", "supply chain",
]

MACRO_EVENTS = [
    "inflation", "interest rate", "fed", "federal reserve", "treasury",
    "gdp", "unemployment", "jobs report", "cpi", "ppi", "fomc",
    "tariff", "trade war", "sanctions", "oil price", "commodity",
    "china", "russia", "ukraine", "middle east", "geopolitical",
    "debt ceiling", "government shutdown", "election", "regulation",
]


def _fetch_rss(url, timeout=10):
    """Fetch and parse an RSS feed, return list of (title, link, pub_date)."""
    try:
        ctx = ssl.create_default_context()
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; EpicFuryBot/1.0)"
        })
        resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
        xml_data = resp.read().decode("utf-8", errors="replace")
        root = ET.fromstring(xml_data)

        items = []
        # Standard RSS format
        for item in root.findall(".//item"):
            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            pub_date = item.findtext("pubDate", "").strip()
            if title:
                items.append({"title": title, "link": link, "pub_date": pub_date})

        return items
    except Exception:
        return []


def _score_headline(title):
    """Score a headline for sentiment. Returns -1 to +1."""
    title_lower = title.lower()
    bull_count = sum(1 for kw in BULLISH_KEYWORDS if kw in title_lower)
    bear_count = sum(1 for kw in BEARISH_KEYWORDS if kw in title_lower)

    total = bull_count + bear_count
    if total == 0:
        return 0.0

    return round((bull_count - bear_count) / total, 2)


def _is_macro_event(title):
    """Check if headline relates to macroeconomic events."""
    title_lower = title.lower()
    return any(kw in title_lower for kw in MACRO_EVENTS)


def _is_relevant_to_ticker(title, ticker, company_name=""):
    """Check if a headline is relevant to a specific stock."""
    title_lower = title.lower()
    ticker_lower = ticker.lower()

    if ticker_lower in title_lower:
        return True

    if company_name:
        # Check first word of company name (e.g., "Apple" from "Apple Inc")
        first_word = company_name.split()[0].lower() if company_name else ""
        if first_word and len(first_word) > 2 and first_word in title_lower:
            return True

    return False


def get_market_news():
    """
    Fetch latest market news from Yahoo Finance, CNN, and CNBC RSS feeds.
    Returns headlines with sentiment scores.
    """
    cache_key = "market_news"
    now = time.time()
    if cache_key in _news_cache and now - _news_cache[cache_key]["time"] < _NEWS_CACHE_TTL:
        return _news_cache[cache_key]["data"]

    all_headlines = []

    # Yahoo Finance RSS
    yahoo_feeds = [
        "https://finance.yahoo.com/news/rssindex",
        "https://finance.yahoo.com/rss/topstories",
    ]
    for feed_url in yahoo_feeds:
        items = _fetch_rss(feed_url)
        for item in items[:15]:
            item["source"] = "Yahoo Finance"
            item["sentiment"] = _score_headline(item["title"])
            item["is_macro"] = _is_macro_event(item["title"])
            all_headlines.append(item)

    # CNN Business RSS
    cnn_feeds = [
        "http://rss.cnn.com/rss/money_latest.rss",
        "http://rss.cnn.com/rss/money_markets.rss",
    ]
    for feed_url in cnn_feeds:
        items = _fetch_rss(feed_url)
        for item in items[:10]:
            item["source"] = "CNN"
            item["sentiment"] = _score_headline(item["title"])
            item["is_macro"] = _is_macro_event(item["title"])
            all_headlines.append(item)

    # CNBC RSS
    cnbc_feeds = [
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",  # Top News
        "https://www.cnbc.com/id/10001147/device/rss/rss.html",   # Markets
    ]
    for feed_url in cnbc_feeds:
        items = _fetch_rss(feed_url)
        for item in items[:10]:
            item["source"] = "CNBC"
            item["sentiment"] = _score_headline(item["title"])
            item["is_macro"] = _is_macro_event(item["title"])
            all_headlines.append(item)

    # Deduplicate by title similarity
    seen_titles = set()
    unique = []
    for h in all_headlines:
        # Simple dedup: first 50 chars
        key = h["title"][:50].lower()
        if key not in seen_titles:
            seen_titles.add(key)
            unique.append(h)

    # Sort by relevance (macro events first, then by sentiment magnitude)
    unique.sort(key=lambda x: (x["is_macro"], abs(x["sentiment"])), reverse=True)

    result = {
        "headlines": unique[:30],
        "market_sentiment": _calculate_market_sentiment(unique),
        "macro_events": [h for h in unique if h["is_macro"]][:10],
        "fetched_at": datetime.now().isoformat(),
        "sources": ["Yahoo Finance", "CNN", "CNBC"],
    }

    _news_cache[cache_key] = {"data": result, "time": now}
    return result


def get_stock_sentiment(ticker, company_name=""):
    """
    Get sentiment for a specific stock by filtering market news.
    Also returns overall market sentiment for context.
    """
    market_news = get_market_news()

    # Filter headlines relevant to this stock
    stock_headlines = []
    for h in market_news["headlines"]:
        if _is_relevant_to_ticker(h["title"], ticker, company_name):
            stock_headlines.append(h)

    # Calculate stock-specific sentiment
    if stock_headlines:
        scores = [h["sentiment"] for h in stock_headlines]
        stock_sentiment = round(sum(scores) / len(scores), 2)
    else:
        stock_sentiment = 0.0

    return {
        "ticker": ticker,
        "stock_sentiment": stock_sentiment,
        "stock_headlines": stock_headlines[:5],
        "market_sentiment": market_news["market_sentiment"],
        "macro_events": market_news["macro_events"][:5],
        "total_headlines": len(market_news["headlines"]),
    }


def _calculate_market_sentiment(headlines):
    """Calculate overall market sentiment from all headlines."""
    if not headlines:
        return {"score": 0, "label": "Neutral", "bullish_pct": 50, "bearish_pct": 50}

    scores = [h["sentiment"] for h in headlines]
    avg_score = sum(scores) / len(scores)

    bullish = sum(1 for s in scores if s > 0)
    bearish = sum(1 for s in scores if s < 0)
    neutral = sum(1 for s in scores if s == 0)
    total = len(scores)

    bullish_pct = round(bullish / total * 100)
    bearish_pct = round(bearish / total * 100)

    if avg_score > 0.15:
        label = "Bullish"
    elif avg_score > 0.05:
        label = "Slightly Bullish"
    elif avg_score < -0.15:
        label = "Bearish"
    elif avg_score < -0.05:
        label = "Slightly Bearish"
    else:
        label = "Neutral"

    return {
        "score": round(avg_score, 3),
        "label": label,
        "bullish_pct": bullish_pct,
        "bearish_pct": bearish_pct,
        "neutral_pct": round(neutral / total * 100),
        "total_analyzed": total,
    }
