"""
News Sentiment Engine v2 — Institutional-grade news intelligence.

Upgrades over v1:
  - WEIGHTED keyword scoring (crash = -3, decline = -1)
  - Reuters RSS feeds for breaking geopolitical/military news
  - Tariff/trade war specific intelligence layer
  - Recency weighting (newer headlines score higher)
  - Multi-source confirmation (same story from 2+ sources = stronger signal)
  - Sector-specific sentiment tracking
  - 8 CNN feeds covering world, politics, Middle East, Asia, Europe
  - Bloomberg RSS for financial markets

All data from approved sources: Yahoo Finance, CNN, CNBC, Reuters, Bloomberg.
"""

import urllib.request
import xml.etree.ElementTree as ET
import re
import time
import ssl
import logging
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)

_news_cache = {}
_NEWS_CACHE_TTL = 300  # 5 minutes

# ============================================================
#  WEIGHTED Sentiment Keywords (strength 1-3)
#  3 = extreme signal, 2 = strong, 1 = mild
# ============================================================
BULLISH_WEIGHTED = {
    # Extreme bullish (weight 3)
    "surge": 3, "soar": 3, "skyrocket": 3, "all-time high": 3, "record high": 3,
    "boom": 3, "breakout": 3, "blowout earnings": 3, "massive rally": 3,
    # Strong bullish (weight 2)
    "rally": 2, "jump": 2, "beat": 2, "record": 2, "upgrade": 2,
    "outperform": 2, "strong earnings": 2, "revenue beat": 2,
    "bullish": 2, "breakthrough": 2, "milestone": 2, "stimulus": 2,
    "rate cut": 2, "fed cut": 2, "dovish": 2, "merger": 2,
    "acquisition": 2,
    # Mild bullish (weight 1)
    "gain": 1, "rise": 1, "buy": 1, "growth": 1, "profit": 1,
    "positive": 1, "optimistic": 1, "upbeat": 1, "recovery": 1,
    "expansion": 1, "deal": 1, "innovation": 1, "demand": 1,
    "hiring": 1, "job growth": 1, "consumer spending": 1, "GDP growth": 1,
}

BEARISH_WEIGHTED = {
    # Extreme bearish (weight 3)
    "crash": 3, "plunge": 3, "collapse": 3, "meltdown": 3, "freefall": 3,
    "bankruptcy": 3, "default": 3, "panic": 3, "circuit breaker": 3,
    "bear market": 3, "recession confirmed": 3, "depression": 3,
    # Strong bearish (weight 2)
    "drop": 2, "sell-off": 2, "downgrade": 2, "warning": 2,
    "recession": 2, "crisis": 2, "inflation": 2, "rate hike": 2,
    "hawkish": 2, "tariff": 2, "sanctions": 2, "war": 2,
    "conflict": 2, "correction": 2, "volatility spike": 2,
    "fraud": 2, "investigation": 2, "layoffs": 2, "mass layoff": 2,
    "supply chain crisis": 2, "debt ceiling": 2,
    # Mild bearish (weight 1)
    "fall": 1, "decline": 1, "sell": 1, "loss": 1, "miss": 1,
    "underperform": 1, "weak": 1, "negative": 1, "pessimistic": 1,
    "slump": 1, "cut": 1, "tension": 1, "geopolitical": 1,
    "shutdown": 1, "fear": 1, "lawsuit": 1, "recall": 1,
    "supply chain": 1, "layoff": 1,
}

MACRO_EVENTS = [
    "inflation", "interest rate", "fed", "federal reserve", "treasury",
    "gdp", "unemployment", "jobs report", "cpi", "ppi", "fomc",
    "tariff", "trade war", "sanctions", "oil price", "commodity",
    "china", "russia", "ukraine", "middle east", "geopolitical",
    "debt ceiling", "government shutdown", "election", "regulation",
    "nuclear", "invasion", "nato", "iran", "israel",
    "fomc minutes", "rate decision", "consumer price index",
    "durable goods", "pharma tariff", "drug tariff", "section 232",
    "military operation", "ground invasion", "liberation day",
]

# ============================================================
#  TARIFF / TRADE WAR Intelligence
# ============================================================
TARIFF_KEYWORDS = [
    "tariff", "tariffs", "trade war", "trade deal", "trade talks",
    "import duty", "export ban", "trade deficit", "trade surplus",
    "retaliatory tariff", "counter-tariff", "trade restriction",
    "customs duty", "trade agreement", "trade negotiation",
    "reciprocal tariff", "liberation day", "trade policy",
    "section 232", "pharma tariff", "drug tariff", "pharmaceutical tariff",
    "steel tariff", "aluminum tariff", "copper tariff",
]

# Which sectors get hit by tariffs and how
TARIFF_SECTOR_IMPACTS = {
    # Sector names MUST match Yahoo Finance / SECTOR_MAP keys used in quant_engine.py
    "escalation": {  # New/higher tariffs announced
        "Technology": -1.5,           # Supply chain disruption, chip tariffs
        "Consumer Cyclical": -1.5,    # Import costs rise (was: Consumer Discretionary)
        "Industrials": -1.0,          # Manufacturing input costs
        "Materials": -0.5,            # Steel/aluminum tariffs cut both ways
        "Communication Services": -0.5,  # (was: Communication)
        "Energy": +0.5,               # Domestic energy benefits
        "Consumer Defensive": +0.5,   # Domestic producers benefit (was: Consumer Staples)
        "Healthcare": -1.0,           # Pharma tariffs (100% on imported drugs)
        "Utilities": +0.3,            # Defensive/domestic
        "Financial Services": -0.5,   # Trade uncertainty (was: Financials)
        "Real Estate": 0,
        "ETF": 0,
    },
    "de_escalation": {  # Trade deal, tariff removal
        "Technology": +1.5,
        "Consumer Cyclical": +1.0,    # (was: Consumer Discretionary)
        "Industrials": +1.0,
        "Materials": +0.5,
        "Communication Services": +0.5,  # (was: Communication)
        "Financial Services": +0.5,   # (was: Financials)
        "Energy": 0,
        "Consumer Defensive": 0,      # (was: Consumer Staples)
        "Healthcare": 0,
        "Utilities": 0,
        "Real Estate": +0.3,
        "ETF": 0,
    },
}

TARIFF_ESCALATION_WORDS = [
    "new tariff", "raise tariff", "increase tariff", "impose tariff",
    "retaliatory", "counter-tariff", "trade war escalat", "ban import",
    "export restriction", "trade restriction", "tariff hike",
    "reciprocal tariff", "liberation day", "section 232",
    "100% tariff", "pharma tariff", "drug import tariff",
]
TARIFF_DEESCALATION_WORDS = [
    "trade deal", "trade agreement", "remove tariff", "lower tariff",
    "tariff relief", "trade talks progress", "trade negotiation success",
    "tariff exemption", "tariff pause", "trade truce",
]

# ============================================================
#  GEOPOLITICAL Risk Intelligence
# ============================================================
GEO_MILITARY_KEYWORDS = [
    # Direct military actions
    "invasion", "ground invasion", "air strike", "airstrike", "missile",
    "bombing", "troops deployed", "military operation", "martial law",
    "declaration of war", "nuclear", "mobilization", "offensive",
    "ceasefire violated", "boots on the ground", "naval blockade",
    "special military operation", "drone strike", "escalation",
    "artillery", "casualties", "combat", "warzone", "occupied",
    "military strike", "retaliation strike", "carpet bombing",
    "ground offensive", "siege", "shelling", "ammunition",
    # Expanded military terms
    "ballistic missile", "cruise missile", "intercontinental",
    "chemical weapon", "biological weapon", "weapons of mass",
    "military incursion", "border clash", "armed conflict",
    "war crime", "genocide", "ethnic cleansing", "mass atrocity",
    "aircraft carrier", "naval fleet", "submarine", "warship",
    "fighter jet", "stealth bomber", "military convoy",
    "special forces", "commando raid", "covert operation",
    "rocket attack", "mortar", "landmine", "IED", "ambush",
    "hostage", "kidnapped", "prisoner of war", "POW",
    "coup attempt", "military junta", "state of emergency",
    "martial law declared", "conscription", "draft",
    "nuclear test", "uranium enrichment", "plutonium",
    "dirty bomb", "radiological", "EMP", "electromagnetic pulse",
]

GEO_TENSION_KEYWORDS = [
    # Diplomatic/political
    "sanctions", "embargo", "trade ban", "diplomatic crisis", "expel diplomat",
    "recall ambassador", "border tension", "territorial dispute",
    "cyberattack", "cyber warfare", "hack", "espionage",
    "proxy war", "insurgency", "coup", "regime change",
    "humanitarian crisis", "refugee", "evacuation", "no-fly zone",
    "arms deal", "weapons shipment", "military aid", "nato",
    "negotiations collapse", "negotiations failed", "talks failed",
    "ceasefire ended", "ceasefire expired", "ceasefire collapsed",
    "deal fell apart", "ceasefire deadline", "deal expired",
    "peace deal collapsed", "truce ended", "truce expired",
    "troop buildup", "military exercise", "show of force",
    "diplomatic expulsion", "asset freeze", "travel ban",
    # Expanded tension terms
    "trade war", "economic warfare", "currency war", "devaluation",
    "debt crisis", "default", "sovereign debt", "credit downgrade",
    "capital flight", "bank run", "financial contagion",
    "election crisis", "contested election", "political instability",
    "protest", "riot", "civil unrest", "revolution", "uprising",
    "assassination", "political assassination", "regime collapse",
    "diplomatic breakdown", "ultimatum", "threat of force",
    "naval provocation", "airspace violation", "territorial incursion",
    "intelligence leak", "spy scandal", "disinformation campaign",
    "oil embargo", "energy crisis", "gas pipeline", "pipeline attack",
    "supply chain disruption", "port closure", "shipping lane",
    "food crisis", "famine", "water conflict", "resource war",
    "pandemic", "outbreak", "quarantine", "lockdown",
    "terrorist attack", "terrorism", "extremist", "radicalization",
    "cyber espionage", "ransomware", "infrastructure attack",
    "election interference", "propaganda", "information warfare",
]

GEO_HOTSPOTS = [
    # Middle East
    "iran", "iraq", "israel", "gaza", "palestine", "lebanon", "hezbollah",
    "yemen", "houthi", "red sea", "strait of hormuz",
    "syria", "persian gulf", "west bank", "tehran", "riyadh",
    "saudi arabia", "qatar", "bahrain", "oman", "kuwait",
    "jordan", "egypt", "suez canal",
    # Europe/Russia
    "russia", "ukraine", "crimea", "donbas", "nato", "moscow",
    "belarus", "moldova", "transnistria", "baltic", "kaliningrad",
    "finland", "sweden", "arctic",
    # Asia/Pacific
    "china", "taiwan", "south china sea", "north korea", "pyongyang",
    "beijing", "hong kong", "xinjiang", "tibet",
    "kashmir", "india", "pakistan", "afghanistan",
    "myanmar", "philippines", "vietnam", "indonesia",
    "korean peninsula", "demilitarized zone", "dmz",
    # Africa
    "libya", "sudan", "ethiopia", "somalia", "niger", "mali",
    "congo", "mozambique", "sahel", "boko haram",
    # Americas
    "venezuela", "cuba", "colombia", "mexico cartel",
    "panama canal", "haiti",
]

GEO_SECTOR_IMPACTS = {
    # Sector names MUST match Yahoo Finance / SECTOR_MAP keys used in quant_engine.py:
    # "Consumer Defensive" (not Staples), "Consumer Cyclical" (not Discretionary),
    # "Financial Services" (not Financials), "Communication Services" (not Communication)
    "military": {
        "Industrials": +1.5, "Energy": +2.0, "Materials": +1.0,
        "Utilities": +0.5, "Consumer Defensive": +0.5, "Healthcare": +0.5,
        "Technology": -1.0, "Consumer Cyclical": -1.5,
        "Communication Services": -0.5, "Financial Services": -0.5, "Real Estate": -0.5, "ETF": 0,
    },
    "tension": {
        "Industrials": +0.5, "Energy": +1.0, "Materials": +0.5,
        "Utilities": +0.3, "Consumer Defensive": +0.3, "Healthcare": +0.3,
        "Technology": -0.5, "Consumer Cyclical": -0.5,
        "Communication Services": -0.3, "Financial Services": -0.3, "Real Estate": -0.3, "ETF": 0,
    },
}

GEO_TICKER_BOOSTS = {
    "LMT": +3, "RTX": +3, "NOC": +2.5, "GD": +2.5, "BA": +1.5, "LHX": +2,
    "XOM": +1.5, "CVX": +1.5, "COP": +1.5, "OXY": +1, "HAL": +1,
    "NEM": +2, "GOLD": +2, "GLD": +1.5,
    "CRWD": +1.5, "PANW": +1.5, "FTNT": +1,
    "AAL": -1.5, "DAL": -1.5, "UAL": -1.5,
    "CCL": -1.5, "RCL": -1.5, "MAR": -1, "HLT": -1,
}


# ============================================================
#  RSS Fetching
# ============================================================
def _fetch_rss(url, timeout=10):
    """Fetch and parse an RSS feed, return list of (title, link, pub_date)."""
    try:
        ctx = ssl.create_default_context()
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; SentinelQuantBot/2.0)"
        })
        resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
        xml_data = resp.read().decode("utf-8", errors="replace")
        root = ET.fromstring(xml_data)

        items = []
        for item in root.findall(".//item"):
            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            pub_date = item.findtext("pubDate", "").strip()
            if title:
                items.append({"title": title, "link": link, "pub_date": pub_date})
        return items
    except Exception as e:
        logger.warning(f"RSS fetch failed for {url}: {e}")
        return []


# ============================================================
#  WEIGHTED Scoring (v2 upgrade)
# ============================================================
def _score_headline(title):
    """
    Score a headline for sentiment using WEIGHTED keywords.
    Returns -1.0 to +1.0, where magnitude reflects strength.
    """
    title_lower = title.lower()
    bull_score = 0
    bear_score = 0

    for kw, weight in BULLISH_WEIGHTED.items():
        if kw in title_lower:
            bull_score += weight

    for kw, weight in BEARISH_WEIGHTED.items():
        if kw in title_lower:
            bear_score += weight

    total = bull_score + bear_score
    if total == 0:
        return 0.0

    # Weighted ratio: ranges from -1 to +1
    return round((bull_score - bear_score) / total, 2)


def _get_headline_age_hours(pub_date_str):
    """Parse pub_date and return hours since publication. Returns 999 if unparseable."""
    if not pub_date_str:
        return 999
    try:
        pub_dt = parsedate_to_datetime(pub_date_str)
        now = datetime.now(pub_dt.tzinfo) if pub_dt.tzinfo else datetime.now()
        delta = now - pub_dt
        return delta.total_seconds() / 3600
    except Exception:
        return 999


def _recency_weight(hours_old):
    """
    Weight multiplier based on how recent the headline is.
    < 1 hour: 2.0x, < 6 hours: 1.5x, < 24 hours: 1.0x, older: 0.5x
    """
    if hours_old < 1:
        return 2.0
    elif hours_old < 6:
        return 1.5
    elif hours_old < 24:
        return 1.0
    else:
        return 0.5


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
        first_word = company_name.split()[0].lower() if company_name else ""
        if first_word and len(first_word) > 2 and first_word in title_lower:
            return True

    return False


# ============================================================
#  Main News Fetcher
# ============================================================
def get_market_news():
    """
    Fetch latest market news from Yahoo Finance, CNN (8 feeds), CNBC,
    Reuters, and Bloomberg RSS feeds.
    Returns headlines with weighted sentiment scores and recency.
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
            all_headlines.append(item)

    # CNN RSS — 8 feeds covering business, world, politics, regions
    cnn_feeds = [
        "https://rss.cnn.com/rss/money_latest.rss",
        "https://rss.cnn.com/rss/money_markets.rss",
        "https://rss.cnn.com/rss/edition_world.rss",
        "https://rss.cnn.com/rss/cnn_allpolitics.rss",
        "https://rss.cnn.com/rss/cnn_topstories.rss",
        "https://rss.cnn.com/rss/edition_meast.rss",
        "https://rss.cnn.com/rss/edition_asia.rss",
        "https://rss.cnn.com/rss/edition_europe.rss",
    ]
    for feed_url in cnn_feeds:
        items = _fetch_rss(feed_url)
        for item in items[:10]:
            item["source"] = "CNN"
            all_headlines.append(item)

    # CNBC RSS
    cnbc_feeds = [
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        "https://www.cnbc.com/id/10001147/device/rss/rss.html",
        "https://www.cnbc.com/id/10000664/device/rss/rss.html",  # World
    ]
    for feed_url in cnbc_feeds:
        items = _fetch_rss(feed_url)
        for item in items[:10]:
            item["source"] = "CNBC"
            all_headlines.append(item)

    # Reuters RSS (top news + world)
    reuters_feeds = [
        "https://feeds.reuters.com/reuters/topNews",
        "https://feeds.reuters.com/reuters/worldNews",
        "https://feeds.reuters.com/reuters/businessNews",
    ]
    for feed_url in reuters_feeds:
        items = _fetch_rss(feed_url)
        for item in items[:10]:
            item["source"] = "Reuters"
            all_headlines.append(item)

    # Bloomberg RSS
    bloomberg_feeds = [
        "https://feeds.bloomberg.com/markets/news.rss",
        "https://feeds.bloomberg.com/politics/news.rss",
    ]
    for feed_url in bloomberg_feeds:
        items = _fetch_rss(feed_url)
        for item in items[:10]:
            item["source"] = "Bloomberg"
            all_headlines.append(item)

    # Score all headlines with weighted sentiment + recency
    for h in all_headlines:
        h["sentiment"] = _score_headline(h["title"])
        h["is_macro"] = _is_macro_event(h["title"])
        h["age_hours"] = round(_get_headline_age_hours(h.get("pub_date", "")), 1)
        h["recency_weight"] = _recency_weight(h["age_hours"])
        # Weighted sentiment = raw sentiment * recency multiplier
        h["weighted_sentiment"] = round(h["sentiment"] * h["recency_weight"], 3)

    # Deduplicate by title similarity
    seen_titles = set()
    unique = []
    for h in all_headlines:
        key = h["title"][:50].lower()
        if key not in seen_titles:
            seen_titles.add(key)
            unique.append(h)

    # Multi-source confirmation: if same story from 2+ sources, boost signal
    title_sources = {}
    for h in unique:
        key = h["title"][:40].lower()
        if key not in title_sources:
            title_sources[key] = set()
        title_sources[key].add(h["source"])

    for h in unique:
        key = h["title"][:40].lower()
        source_count = len(title_sources.get(key, set()))
        if source_count >= 2:
            h["multi_source_confirmed"] = True
            h["weighted_sentiment"] = round(h["weighted_sentiment"] * 1.5, 3)
        else:
            h["multi_source_confirmed"] = False

    # Sort by weighted sentiment magnitude (strongest signals first)
    unique.sort(key=lambda x: (x["is_macro"], abs(x["weighted_sentiment"])), reverse=True)

    # Sector-specific sentiment
    sector_sentiment = _calculate_sector_sentiment(unique)

    result = {
        "headlines": unique[:50],  # Return more headlines for analysis
        "market_sentiment": _calculate_market_sentiment(unique),
        "sector_sentiment": sector_sentiment,
        "macro_events": [h for h in unique if h["is_macro"]][:15],
        "fetched_at": datetime.now().isoformat(),
        "sources": list(set(h["source"] for h in unique)),
        "total_fetched": len(unique),
    }

    _news_cache[cache_key] = {"data": result, "time": now}
    return result


# ============================================================
#  Sector-Specific Sentiment
# ============================================================
SECTOR_KEYWORDS = {
    "Technology": ["tech", "software", "ai ", "artificial intelligence", "semiconductor", "chip", "cloud", "saas", "apple", "microsoft", "google", "nvidia", "meta"],
    "Healthcare": ["healthcare", "pharma", "biotech", "drug", "fda", "vaccine", "hospital", "medical"],
    "Financials": ["bank", "financial", "credit", "lending", "mortgage", "insurance", "jpmorgan", "goldman"],
    "Energy": ["oil", "gas", "energy", "crude", "opec", "drilling", "pipeline", "renewable", "solar"],
    "Consumer Discretionary": ["retail", "amazon", "tesla", "consumer", "spending", "luxury", "travel", "airline"],
    "Consumer Staples": ["grocery", "food", "beverage", "walmart", "costco", "procter", "coca-cola"],
    "Industrials": ["manufacturing", "defense", "aerospace", "construction", "industrial", "boeing", "lockheed"],
    "Materials": ["mining", "steel", "aluminum", "gold", "copper", "lithium", "commodity"],
    "Real Estate": ["real estate", "housing", "mortgage", "reit", "property", "home sales"],
    "Utilities": ["utility", "electric", "power grid", "natural gas", "water"],
    "Communication": ["media", "streaming", "telecom", "social media", "advertising", "netflix", "disney"],
}


def _calculate_sector_sentiment(headlines):
    """Track sentiment per sector based on headline content."""
    sector_scores = {}
    for sector, keywords in SECTOR_KEYWORDS.items():
        relevant = []
        for h in headlines:
            title_lower = h["title"].lower()
            if any(kw in title_lower for kw in keywords):
                relevant.append(h["weighted_sentiment"])
        if relevant:
            avg = round(sum(relevant) / len(relevant), 3)
            sector_scores[sector] = {
                "score": avg,
                "headline_count": len(relevant),
                "label": "Bullish" if avg > 0.1 else "Bearish" if avg < -0.1 else "Neutral",
            }
    return sector_scores


def get_stock_sentiment(ticker, company_name=""):
    """Get sentiment for a specific stock by filtering market news."""
    market_news = get_market_news()

    stock_headlines = []
    for h in market_news["headlines"]:
        if _is_relevant_to_ticker(h["title"], ticker, company_name):
            stock_headlines.append(h)

    if stock_headlines:
        scores = [h["weighted_sentiment"] for h in stock_headlines]
        stock_sentiment = round(sum(scores) / len(scores), 2)
    else:
        stock_sentiment = 0.0

    return {
        "ticker": ticker,
        "stock_sentiment": stock_sentiment,
        "stock_headlines": stock_headlines[:5],
        "market_sentiment": market_news["market_sentiment"],
        "sector_sentiment": market_news.get("sector_sentiment", {}),
        "macro_events": market_news["macro_events"][:5],
        "total_headlines": len(market_news["headlines"]),
    }


def _calculate_market_sentiment(headlines):
    """Calculate overall market sentiment using weighted scores."""
    if not headlines:
        return {"score": 0, "label": "Neutral", "bullish_pct": 50, "bearish_pct": 50}

    scores = [h["weighted_sentiment"] for h in headlines]
    avg_score = sum(scores) / len(scores)

    bullish = sum(1 for s in scores if s > 0)
    bearish = sum(1 for s in scores if s < 0)
    neutral = sum(1 for s in scores if s == 0)
    total = len(scores)

    bullish_pct = round(bullish / total * 100)
    bearish_pct = round(bearish / total * 100)

    if avg_score > 0.2:
        label = "Strongly Bullish"
    elif avg_score > 0.1:
        label = "Bullish"
    elif avg_score > 0.03:
        label = "Slightly Bullish"
    elif avg_score < -0.2:
        label = "Strongly Bearish"
    elif avg_score < -0.1:
        label = "Bearish"
    elif avg_score < -0.03:
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


# ============================================================
#  Tariff / Trade War Intelligence
# ============================================================
def assess_tariff_risk():
    """
    Scan headlines for tariff/trade war activity.
    Returns risk level and sector adjustments.
    """
    cache_key = "tariff_risk"
    now = time.time()
    if cache_key in _news_cache and now - _news_cache[cache_key]["time"] < _NEWS_CACHE_TTL:
        return _news_cache[cache_key]["data"]

    news = get_market_news()
    headlines = news.get("headlines", [])

    tariff_headlines = []
    escalation_count = 0
    deescalation_count = 0

    for h in headlines:
        title_lower = h["title"].lower()

        is_tariff = any(kw in title_lower for kw in TARIFF_KEYWORDS)
        if not is_tariff:
            continue

        tariff_headlines.append(h)

        if any(kw in title_lower for kw in TARIFF_ESCALATION_WORDS):
            escalation_count += 1
        if any(kw in title_lower for kw in TARIFF_DEESCALATION_WORDS):
            deescalation_count += 1

    # Determine tariff direction
    if escalation_count > deescalation_count and escalation_count >= 2:
        tariff_direction = "ESCALATING"
        sector_adj = dict(TARIFF_SECTOR_IMPACTS["escalation"])  # defensive copy — never mutate the constant
        risk_score = min(10, 3 + escalation_count * 2)
    elif deescalation_count > escalation_count and deescalation_count >= 2:
        tariff_direction = "DE_ESCALATING"
        sector_adj = dict(TARIFF_SECTOR_IMPACTS["de_escalation"])  # defensive copy
        risk_score = 2
    elif len(tariff_headlines) > 0:
        tariff_direction = "ACTIVE"
        # Mix of escalation/de-escalation
        sector_adj = {k: round(v * 0.5, 1) for k, v in TARIFF_SECTOR_IMPACTS["escalation"].items()}
        risk_score = min(5, 1 + len(tariff_headlines))
    else:
        tariff_direction = "QUIET"
        sector_adj = {}
        risk_score = 0

    result = {
        "tariff_direction": tariff_direction,
        "risk_score": risk_score,
        "escalation_signals": escalation_count,
        "deescalation_signals": deescalation_count,
        "tariff_headlines": [{"title": h["title"], "source": h["source"]} for h in tariff_headlines[:5]],
        "sector_adjustments": sector_adj,
        "assessed_at": datetime.now().isoformat(),
    }

    _news_cache[cache_key] = {"data": result, "time": now}
    return result


# ============================================================
#  Geopolitical Risk Assessment
# ============================================================
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

    news = get_market_news()
    headlines = news.get("headlines", [])

    military_hits = []
    tension_hits = []
    ceasefire_hits = []
    hotspot_hits = set()

    # Ceasefire / peace keywords — these REDUCE risk, not increase it
    CEASEFIRE_KEYWORDS = [
        "ceasefire", "cease fire", "peace deal", "peace agreement", "peace talks",
        "peace treaty", "truce", "armistice", "de-escalation", "de-escalate",
        "diplomatic resolution", "negotiations succeed", "peace plan",
        "withdrawal of troops", "stand down", "end of hostilities",
    ]

    for h in headlines:
        title_lower = h["title"].lower()
        recency = h.get("recency_weight", 1.0)

        # Check for ceasefire/peace FIRST (takes priority)
        ceasefire_found = False
        for kw in CEASEFIRE_KEYWORDS:
            if kw in title_lower:
                ceasefire_hits.append({
                    "headline": h["title"], "keyword": kw,
                    "source": h.get("source", ""),
                    "recency_weight": recency,
                    "age_hours": h.get("age_hours", 999),
                })
                ceasefire_found = True
                break

        if ceasefire_found:
            continue  # Don't also count this as military/tension

        for kw in GEO_MILITARY_KEYWORDS:
            if kw in title_lower:
                military_hits.append({
                    "headline": h["title"], "keyword": kw,
                    "source": h.get("source", ""),
                    "recency_weight": recency,
                    "age_hours": h.get("age_hours", 999),
                })
                break

        for kw in GEO_TENSION_KEYWORDS:
            if kw in title_lower:
                tension_hits.append({
                    "headline": h["title"], "keyword": kw,
                    "source": h.get("source", ""),
                    "recency_weight": recency,
                })
                break

        for region in GEO_HOTSPOTS:
            if region in title_lower:
                hotspot_hits.add(region)

    military_count = len(military_hits)
    tension_count = len(tension_hits)
    ceasefire_count = len(ceasefire_hits)

    # Weight recent military events higher
    recent_military = sum(1 for m in military_hits if m.get("age_hours", 999) < 12)
    recent_ceasefire = sum(1 for c in ceasefire_hits if c.get("age_hours", 999) < 24)

    # CEASEFIRE OVERRIDE: If ceasefire headlines exist, dramatically reduce risk
    # Ceasefire news = the conflict is winding down, not ramping up
    if ceasefire_count >= 2 or recent_ceasefire >= 1:
        # Strong ceasefire signal — override to LOW
        risk_level = "LOW"
        risk_score = max(0, min(2, military_count - ceasefire_count))
        logger.info(f"CEASEFIRE DETECTED: {ceasefire_count} headlines — overriding risk to LOW")
    elif military_count >= 3 or recent_military >= 2:
        risk_level = "CRITICAL"
        risk_score = min(10, 5 + military_count + recent_military)
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

    sector_adjustments = {}
    if risk_level == "CRITICAL":
        base = GEO_SECTOR_IMPACTS["military"]
        scale = min(2.0, 1.0 + (military_count - 1) * 0.3)
        sector_adjustments = {k: round(v * scale, 1) for k, v in base.items()}
    elif risk_level == "ELEVATED":
        base = GEO_SECTOR_IMPACTS["tension"]
        scale = min(1.5, 1.0 + (tension_count - 1) * 0.2)
        sector_adjustments = {k: round(v * scale, 1) for k, v in base.items()}

    ticker_adjustments = {}
    if risk_level in ("CRITICAL", "ELEVATED"):
        scale = 1.0 if risk_level == "CRITICAL" else 0.5
        ticker_adjustments = {k: round(v * scale, 1) for k, v in GEO_TICKER_BOOSTS.items()}

    iran_active = any(r in hotspot_hits for r in ["iran", "iraq", "strait of hormuz", "persian gulf", "tehran"])
    russia_active = any(r in hotspot_hits for r in ["russia", "ukraine", "crimea", "nato", "moscow"])
    china_active = any(r in hotspot_hits for r in ["china", "taiwan", "south china sea", "beijing"])

    if iran_active and risk_level == "CRITICAL":
        sector_adjustments["Energy"] = min(3.0, sector_adjustments.get("Energy", 0) + 1.0)
        for t in ["XOM", "CVX", "COP", "OXY"]:
            ticker_adjustments[t] = ticker_adjustments.get(t, 0) + 1.0

    # Also integrate tariff risk into geopolitical picture
    tariff = assess_tariff_risk()
    if tariff.get("tariff_direction") in ("ESCALATING", "ACTIVE"):
        for sector, adj in tariff.get("sector_adjustments", {}).items():
            sector_adjustments[sector] = round(sector_adjustments.get(sector, 0) + adj, 1)

    # Count unique sources reporting military events
    military_sources = set(m["source"] for m in military_hits)

    result = {
        "risk_level": risk_level,
        "risk_score": risk_score,
        "ceasefire_detected": ceasefire_count > 0,
        "ceasefire_count": ceasefire_count,
        "ceasefire_headlines": [c["headline"] for c in ceasefire_hits[:5]],
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
        "tariff_risk": {
            "direction": tariff.get("tariff_direction", "QUIET"),
            "score": tariff.get("risk_score", 0),
        },
        "multi_source_confirmation": len(military_sources) >= 2,
        "sources_reporting": sorted(list(military_sources)),
        "assessed_at": datetime.now().isoformat(),
        "headlines_scanned": len(headlines),
    }

    _news_cache[cache_key] = {"data": result, "time": now}
    return result


# ============================================================
#  AUTONOMOUS GEOPOLITICAL EVENT DETECTION
# ============================================================

GEO_DEADLINE_KEYWORDS = [
    "deadline", "set to expire", "expires", "due to end", "runs out",
    "lapses", "last day", "final day", "will end", "ending",
    "set to end", "about to expire", "hours left", "days left",
    "countdown", "ticking clock", "final hours", "time running out",
    "due to expire", "slated to end", "scheduled to end",
    "will lapse", "nearing expiration", "approaching deadline",
]

GEO_EVENT_TYPES = [
    "ceasefire", "cease fire", "cease-fire", "truce", "armistice",
    "sanctions", "embargo", "trade ban",
    "treaty", "agreement", "accord", "pact", "deal",
    "talks", "negotiations", "summit", "conference",
    "election", "vote", "referendum", "runoff",
    "nuclear deal", "arms deal", "peace deal", "trade deal",
    "un resolution", "security council",
]

GEO_OUTCOME_POSITIVE = [
    "extended", "renewed", "reached", "signed", "ratified",
    "agreed", "breakthrough", "accord signed", "deal struck",
    "talks succeed", "peace reached", "deal extended",
    "sanctions lifted", "embargo lifted", "truce holds",
    "ceasefire holds", "agreement reached", "treaty signed",
]

GEO_OUTCOME_NEGATIVE = [
    "collapsed", "failed", "broke down", "expired", "walked out",
    "stalled", "rejected", "vetoed", "no deal", "talks fail",
    "deadline missed", "fell apart", "broke apart", "derailed",
    "sanctions reimposed", "deal dead", "negotiations collapse",
    "ceasefire violated", "truce broken", "agreement violated",
]


def _extract_date_from_headline(headline: str) -> tuple:
    """
    Extract an approximate date from a headline.
    Returns (date_str YYYY-MM-DD, confidence 'high'|'medium'|'low').
    Returns (None, None) if no date found.
    """
    import re
    from datetime import timedelta
    today = datetime.now()
    headline_lower = headline.lower()

    # 1. Explicit month-day: "April 15" or "April 15, 2026"
    month_names = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4,
        "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
        "oct": 10, "nov": 11, "dec": 12,
    }
    pattern = r'\b(' + '|'.join(month_names.keys()) + r')\s+(\d{1,2})(?:,?\s*(\d{4}))?\b'
    match = re.search(pattern, headline_lower)
    if match:
        month = month_names[match.group(1)]
        day = int(match.group(2))
        year = int(match.group(3)) if match.group(3) else today.year
        try:
            target = datetime(year, month, day)
            if target < today and not match.group(3):
                target = datetime(year + 1, month, day)
            return (target.strftime("%Y-%m-%d"), "high")
        except ValueError:
            pass

    # 2. Day names: "Monday", "Tuesday", etc.
    day_names = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
    }
    for day_name, day_num in day_names.items():
        if day_name in headline_lower:
            days_ahead = (day_num - today.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            target = today + timedelta(days=days_ahead)
            return (target.strftime("%Y-%m-%d"), "medium")

    # 3. Relative references
    if "tomorrow" in headline_lower:
        return ((today + timedelta(days=1)).strftime("%Y-%m-%d"), "medium")
    if "today" in headline_lower and any(kw in headline_lower for kw in GEO_DEADLINE_KEYWORDS):
        return (today.strftime("%Y-%m-%d"), "high")
    if "next week" in headline_lower:
        return ((today + timedelta(days=7)).strftime("%Y-%m-%d"), "low")
    if "this weekend" in headline_lower:
        days_to_sat = (5 - today.weekday()) % 7
        return ((today + timedelta(days=max(1, days_to_sat))).strftime("%Y-%m-%d"), "low")
    if "end of month" in headline_lower:
        import calendar
        last_day = calendar.monthrange(today.year, today.month)[1]
        return (datetime(today.year, today.month, last_day).strftime("%Y-%m-%d"), "low")
    if "end of week" in headline_lower:
        days_to_fri = (4 - today.weekday()) % 7
        return ((today + timedelta(days=max(1, days_to_fri))).strftime("%Y-%m-%d"), "low")

    return (None, None)


def detect_upcoming_events() -> list:
    """
    Scan news headlines for upcoming geopolitical deadlines.
    Requires at least 2 of 3 keyword categories: deadline + hotspot + event_type.
    """
    from datetime import timedelta
    news = get_market_news()
    headlines = news.get("headlines", []) if isinstance(news, dict) else []
    if not headlines:
        return []

    detected = {}
    today = datetime.now()

    for item in headlines:
        title = (item.get("title") or "").lower()
        if not title or len(title) < 15:
            continue

        has_deadline = any(kw in title for kw in GEO_DEADLINE_KEYWORDS)
        matched_hotspot = None
        for hotspot in GEO_HOTSPOTS:
            if hotspot in title:
                matched_hotspot = hotspot
                break
        matched_event_type = None
        for evt in GEO_EVENT_TYPES:
            if evt in title:
                matched_event_type = evt
                break

        matches = sum([has_deadline, matched_hotspot is not None, matched_event_type is not None])
        if matches < 2:
            continue

        date_str, date_confidence = _extract_date_from_headline(item.get("title", ""))
        if not date_str:
            date_str = (today + timedelta(days=7)).strftime("%Y-%m-%d")
            date_confidence = "very_low"

        try:
            event_date = datetime.strptime(date_str, "%Y-%m-%d")
            if event_date > today + timedelta(days=90):
                continue
            if event_date < today - timedelta(days=7):
                continue
        except ValueError:
            continue

        region = matched_hotspot or "unknown"
        event_type = matched_event_type or "geopolitical"
        event_key = f"{event_type.replace(' ', '_')}_{region.replace(' ', '_')}"

        if event_key in detected:
            existing = detected[event_key]
            existing["source_count"] = existing.get("source_count", 1) + 1
            if existing["source_count"] >= 2 and existing["confidence"] != "high":
                existing["confidence"] = "high"
            continue

        detected[event_key] = {
            "event_key": event_key,
            "event_type": event_type,
            "region": region,
            "estimated_date": date_str,
            "confidence": date_confidence or "low",
            "source_headline": (item.get("title") or "")[:200],
            "source_feed": item.get("source", ""),
            "source_count": 1,
        }

    return list(detected.values())


def detect_event_outcomes() -> list:
    """
    Check active geo events for outcomes (extended, renewed, collapsed, failed).
    Returns list of {event_key, outcome} for resolved events.
    """
    try:
        from predictions.models import get_active_geo_events
    except ImportError:
        return []

    active_events = get_active_geo_events()
    if not active_events:
        return []

    news = get_market_news()
    headlines = news.get("headlines", []) if isinstance(news, dict) else []
    if not headlines:
        return []

    outcomes = []
    for event in active_events:
        region = (event.get("region") or "").lower()
        if not region:
            continue

        for item in headlines:
            title = (item.get("title") or "").lower()
            if region not in title:
                continue

            if any(kw in title for kw in GEO_OUTCOME_POSITIVE):
                outcomes.append({"event_key": event["event_key"], "outcome": "positive"})
                break
            if any(kw in title for kw in GEO_OUTCOME_NEGATIVE):
                outcomes.append({"event_key": event["event_key"], "outcome": "negative"})
                break

    # Expire events 14+ days old with no outcome
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
    for event in active_events:
        if event["estimated_date"] < cutoff:
            if not any(o["event_key"] == event["event_key"] for o in outcomes):
                outcomes.append({"event_key": event["event_key"], "outcome": "expired_unknown"})

    return outcomes


# ============================================================
#  CONTEXT-AWARE GEO IMPACT ANALYZER
#  Instead of hardcoding "ceasefire ending = energy up, tech down",
#  this reads current headlines to determine the ACTUAL market impact.
#  Sometimes energy goes DOWN even when a ceasefire ends (e.g., if
#  a new deal is reached, or if the market already priced it in).
# ============================================================

# Sector sentiment keywords — what headlines say about each sector
_SECTOR_BULLISH_KEYWORDS = {
    "Energy": ["oil surges", "oil rallies", "crude jumps", "energy stocks soar", "oil prices rise",
               "opec cuts", "supply disruption", "pipeline attack", "oil embargo", "energy crisis",
               "oil spikes", "petroleum", "gas prices surge", "fuel shortage", "oil supply"],
    "Technology": ["tech rally", "tech stocks surge", "ai boom", "semiconductor demand", "tech rebound",
                   "nasdaq surges", "growth stocks rally", "innovation", "tech earnings beat",
                   "software demand", "cloud growth", "chip stocks surge"],
    "Industrials": ["defense stocks", "military spending", "defense budget", "arms deal",
                    "infrastructure bill", "manufacturing boom", "industrial production up"],
}

_SECTOR_BEARISH_KEYWORDS = {
    "Energy": ["oil drops", "oil falls", "crude drops", "oil prices fall", "oil plunges",
               "energy stocks fall", "oil glut", "supply surplus", "demand destruction",
               "oil prices drop", "crude tumbles", "oil selloff", "petroleum drops",
               "opec increase", "oil overproduction", "energy decline"],
    "Technology": ["tech selloff", "tech stocks fall", "growth stocks drop", "nasdaq drops",
                   "tech correction", "ai bubble", "tech regulation", "antitrust",
                   "tech earnings miss", "semiconductor slump", "chip stocks fall"],
    "Industrials": ["defense cuts", "military pullback", "peace dividend", "defense spending cut",
                    "manufacturing slump", "industrial slowdown"],
}

# Escalation vs de-escalation keywords
_ESCALATION_KEYWORDS = [
    "tensions rise", "escalat", "military buildup", "troops deploy", "missile launch",
    "attack", "strike", "retaliat", "war", "conflict intensif", "invaded", "bombing",
    "nuclear threat", "sanctions imposed", "trade war", "tariff", "blockade",
    "hostilities", "combat", "offensive", "mobiliz", "arms race",
]

_DEESCALATION_KEYWORDS = [
    "peace talks", "ceasefire extended", "truce", "de-escalat", "diplomacy",
    "negotiations resume", "peace deal", "agreement reached", "tensions ease",
    "ceasefire holds", "sanctions lifted", "embargo lifted", "withdraw",
    "pullback", "demilitariz", "peace accord", "diplomatic solution",
    "talks resume", "compromise", "concessions", "goodwill gesture",
]


def analyze_geo_impact_direction() -> dict:
    """
    Analyze current headlines to determine the ACTUAL direction of geo impact
    on each sector. Returns sentiment scores instead of hardcoded assumptions.

    Returns: {
        "geo_direction": "escalation" | "deescalation" | "neutral",
        "confidence": float (0-1),
        "sector_signals": {
            "Energy": float (-1 to +1, positive = bullish),
            "Technology": float,
            "Industrials": float,
        },
        "headline_evidence": [str],
        "should_override_default": bool,
    }
    """
    news = get_market_news()
    headlines = news.get("headlines", []) if isinstance(news, dict) else []
    if not headlines:
        return {
            "geo_direction": "neutral",
            "confidence": 0.0,
            "sector_signals": {},
            "headline_evidence": [],
            "should_override_default": False,
        }

    escalation_score = 0
    deescalation_score = 0
    sector_scores = {"Energy": 0.0, "Technology": 0.0, "Industrials": 0.0}
    evidence = []

    for item in headlines:
        title = (item.get("title") or "").lower()
        if not title or len(title) < 10:
            continue

        # Check escalation vs de-escalation
        for kw in _ESCALATION_KEYWORDS:
            if kw in title:
                escalation_score += 1
                break
        for kw in _DEESCALATION_KEYWORDS:
            if kw in title:
                deescalation_score += 1
                break

        # Check sector-specific sentiment
        for sector in sector_scores:
            bullish_kws = _SECTOR_BULLISH_KEYWORDS.get(sector, [])
            bearish_kws = _SECTOR_BEARISH_KEYWORDS.get(sector, [])
            for kw in bullish_kws:
                if kw in title:
                    sector_scores[sector] += 1.0
                    if len(evidence) < 5:
                        evidence.append(f"BULLISH {sector}: {title[:80]}")
                    break
            for kw in bearish_kws:
                if kw in title:
                    sector_scores[sector] -= 1.0
                    if len(evidence) < 5:
                        evidence.append(f"BEARISH {sector}: {title[:80]}")
                    break

    # Normalize sector scores to -1 to +1 range
    max_abs = max(abs(v) for v in sector_scores.values()) if any(sector_scores.values()) else 1
    if max_abs > 0:
        for sector in sector_scores:
            sector_scores[sector] = round(sector_scores[sector] / max(max_abs, 3), 2)

    # Determine overall direction
    total = escalation_score + deescalation_score
    if total == 0:
        direction = "neutral"
        confidence = 0.0
    elif escalation_score > deescalation_score * 1.5:
        direction = "escalation"
        confidence = min(1.0, escalation_score / max(total, 1))
    elif deescalation_score > escalation_score * 1.5:
        direction = "deescalation"
        confidence = min(1.0, deescalation_score / max(total, 1))
    else:
        direction = "neutral"
        confidence = 0.3

    # Override default if headlines give strong sector-specific signals
    should_override = (
        confidence >= 0.4 or
        any(abs(v) >= 0.5 for v in sector_scores.values())
    )

    return {
        "geo_direction": direction,
        "confidence": round(confidence, 2),
        "sector_signals": sector_scores,
        "headline_evidence": evidence,
        "should_override_default": should_override,
    }
