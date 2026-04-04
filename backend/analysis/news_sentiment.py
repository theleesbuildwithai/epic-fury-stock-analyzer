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

# --- Geopolitical Risk Intelligence ---
# Keywords that signal military/geopolitical escalation (high severity)
GEO_MILITARY_KEYWORDS = [
    "invasion", "ground invasion", "air strike", "airstrike", "missile",
    "bombing", "troops deployed", "military operation", "martial law",
    "declaration of war", "nuclear", "mobilization", "offensive",
    "ceasefire violated", "boots on the ground", "naval blockade",
    "special military operation", "drone strike", "escalation",
    "artillery", "casualties", "combat", "warzone", "occupied",
]

# Keywords that signal geopolitical tension (medium severity)
GEO_TENSION_KEYWORDS = [
    "sanctions", "embargo", "trade ban", "diplomatic crisis", "expel diplomat",
    "recall ambassador", "border tension", "territorial dispute",
    "cyberattack", "cyber warfare", "hack", "espionage",
    "proxy war", "insurgency", "coup", "regime change",
    "humanitarian crisis", "refugee", "evacuation", "no-fly zone",
    "arms deal", "weapons shipment", "military aid", "nato",
    "ceasefire", "peace talks", "negotiations collapse",
]

# Regions and countries to track
GEO_HOTSPOTS = [
    "iran", "iraq", "israel", "gaza", "palestine", "lebanon", "hezbollah",
    "russia", "ukraine", "crimea", "donbas", "nato",
    "china", "taiwan", "south china sea", "north korea", "pyongyang",
    "yemen", "houthi", "red sea", "strait of hormuz",
    "syria", "libya", "sudan", "ethiopia", "somalia",
]

# Sector impacts from geopolitical events
GEO_SECTOR_IMPACTS = {
    # Military escalation → defense stocks up, energy up (supply fear), travel down
    "military": {
        "Industrials": +1.5,    # defense contractors (LMT, RTX, NOC, GD)
        "Energy": +2.0,         # oil supply disruption fear
        "Materials": +1.0,      # gold/commodities flight to safety
        "Utilities": +0.5,      # defensive safe haven
        "Consumer Staples": +0.5,  # defensive
        "Healthcare": +0.5,     # defensive
        "Technology": -1.0,     # risk-off hurts growth
        "Consumer Discretionary": -1.5,  # spending pullback
        "Communication": -0.5,  # risk-off
        "Financials": -0.5,     # uncertainty
        "Real Estate": -0.5,    # uncertainty
        "ETF": 0,
    },
    # Tension (sanctions/cyber) → milder version
    "tension": {
        "Industrials": +0.5,
        "Energy": +1.0,
        "Materials": +0.5,
        "Utilities": +0.3,
        "Consumer Staples": +0.3,
        "Healthcare": +0.3,
        "Technology": -0.5,
        "Consumer Discretionary": -0.5,
        "Communication": -0.3,
        "Financials": -0.3,
        "Real Estate": -0.3,
        "ETF": 0,
    },
}

# Specific ticker boosts during military events
GEO_TICKER_BOOSTS = {
    # Defense contractors
    "LMT": +3,   # Lockheed Martin
    "RTX": +3,   # Raytheon
    "NOC": +2.5, # Northrop Grumman
    "GD": +2.5,  # General Dynamics
    "BA": +1.5,  # Boeing (defense arm)
    "LHX": +2,   # L3Harris
    # Energy (supply disruption)
    "XOM": +1.5, # ExxonMobil
    "CVX": +1.5, # Chevron
    "COP": +1.5, # ConocoPhillips
    "OXY": +1,   # Occidental
    "HAL": +1,   # Halliburton
    # Gold miners (safe haven)
    "NEM": +2,   # Newmont
    "GOLD": +2,  # Barrick Gold
    "GLD": +1.5, # Gold ETF
    # Cybersecurity (cyber escalation)
    "CRWD": +1.5, # CrowdStrike
    "PANW": +1.5, # Palo Alto Networks
    "FTNT": +1,   # Fortinet
    # Negatively impacted
    "AAL": -1.5, # Airlines
    "DAL": -1.5,
    "UAL": -1.5,
    "CCL": -1.5, # Cruise lines
    "RCL": -1.5,
    "MAR": -1,   # Hotels
    "HLT": -1,
}


def assess_geopolitical_risk():
    """
    Scan news headlines for geopolitical/military events.
    Returns risk level, affected sectors, and ticker-specific adjustments.

    Risk levels:
      - CRITICAL: Active military conflict, invasion, major escalation
      - ELEVATED: Sanctions, tensions, cyber attacks, proxy conflicts
      - LOW: No significant geopolitical headlines
    """
    cache_key = "geo_risk"
    now = time.time()
    if cache_key in _news_cache and now - _news_cache[cache_key]["time"] < _NEWS_CACHE_TTL:
        return _news_cache[cache_key]["data"]

    # Get current headlines
    news = get_market_news()
    headlines = news.get("headlines", [])

    military_hits = []
    tension_hits = []
    hotspot_hits = set()

    for h in headlines:
        title_lower = h["title"].lower()

        # Check for military keywords (high severity)
        for kw in GEO_MILITARY_KEYWORDS:
            if kw in title_lower:
                military_hits.append({"headline": h["title"], "keyword": kw, "source": h.get("source", "")})
                break

        # Check for tension keywords (medium severity)
        for kw in GEO_TENSION_KEYWORDS:
            if kw in title_lower:
                tension_hits.append({"headline": h["title"], "keyword": kw, "source": h.get("source", "")})
                break

        # Track which hotspots are in the news
        for region in GEO_HOTSPOTS:
            if region in title_lower:
                hotspot_hits.add(region)

    # Determine risk level
    military_count = len(military_hits)
    tension_count = len(tension_hits)

    if military_count >= 3:
        risk_level = "CRITICAL"
        risk_score = min(10, 5 + military_count)
    elif military_count >= 1:
        risk_level = "CRITICAL" if military_count >= 2 else "ELEVATED"
        risk_score = min(8, 3 + military_count + tension_count)
    elif tension_count >= 3:
        risk_level = "ELEVATED"
        risk_score = min(6, 2 + tension_count)
    elif tension_count >= 1:
        risk_level = "ELEVATED"
        risk_score = min(4, 1 + tension_count)
    else:
        risk_level = "LOW"
        risk_score = 0

    # Calculate sector adjustments
    sector_adjustments = {}
    if risk_level == "CRITICAL":
        base = GEO_SECTOR_IMPACTS["military"]
        # Scale by severity
        scale = min(2.0, 1.0 + (military_count - 1) * 0.3)
        sector_adjustments = {k: round(v * scale, 1) for k, v in base.items()}
    elif risk_level == "ELEVATED":
        base = GEO_SECTOR_IMPACTS["tension"]
        scale = min(1.5, 1.0 + (tension_count - 1) * 0.2)
        sector_adjustments = {k: round(v * scale, 1) for k, v in base.items()}

    # Calculate ticker-specific adjustments
    ticker_adjustments = {}
    if risk_level in ("CRITICAL", "ELEVATED"):
        scale = 1.0 if risk_level == "CRITICAL" else 0.5
        ticker_adjustments = {k: round(v * scale, 1) for k, v in GEO_TICKER_BOOSTS.items()}

    # Check for specific regional escalations
    iran_active = any(r in hotspot_hits for r in ["iran", "iraq", "strait of hormuz"])
    russia_active = any(r in hotspot_hits for r in ["russia", "ukraine", "crimea", "nato"])
    china_active = any(r in hotspot_hits for r in ["china", "taiwan", "south china sea"])

    # Extra energy boost for Middle East / Strait of Hormuz
    if iran_active and risk_level == "CRITICAL":
        sector_adjustments["Energy"] = min(3.0, sector_adjustments.get("Energy", 0) + 1.0)
        for t in ["XOM", "CVX", "COP", "OXY"]:
            ticker_adjustments[t] = ticker_adjustments.get(t, 0) + 1.0

    result = {
        "risk_level": risk_level,
        "risk_score": risk_score,  # 0-10 scale
        "military_events": military_hits[:5],
        "tension_events": tension_hits[:5],
        "active_hotspots": sorted(list(hotspot_hits)),
        "sector_adjustments": sector_adjustments,
        "ticker_adjustments": ticker_adjustments,
        "regional_risks": {
            "middle_east": iran_active,
            "russia_ukraine": russia_active,
            "china_taiwan": china_active,
        },
        "assessed_at": datetime.now().isoformat(),
        "headlines_scanned": len(headlines),
    }

    _news_cache[cache_key] = {"data": result, "time": now}
    return result


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

    # CNN RSS — Business + World + Politics + Top Stories (catches wars, geopolitics)
    cnn_feeds = [
        "https://rss.cnn.com/rss/money_latest.rss",
        "https://rss.cnn.com/rss/money_markets.rss",
        "https://rss.cnn.com/rss/edition_world.rss",       # World news (wars, military)
        "https://rss.cnn.com/rss/cnn_allpolitics.rss",     # Politics (sanctions, policy)
        "https://rss.cnn.com/rss/cnn_topstories.rss",      # Top stories (breaking events)
        "https://rss.cnn.com/rss/edition_meast.rss",       # Middle East (Iran, Israel)
        "https://rss.cnn.com/rss/edition_asia.rss",        # Asia (China, Taiwan, NK)
        "https://rss.cnn.com/rss/edition_europe.rss",      # Europe (Russia, Ukraine, NATO)
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
