"""
Alt-Data Factors — SEC EDGAR, Reddit, Google Trends, FRED, news NLP.

Free-data factors that diversify beyond price/volume signals.
All scaffolded with HTTP fetch + parse. Some are heavy (NLP) so
they're stubbed for fetcher + a normalization scoring layer.
"""
import re
import math
from typing import Optional
from .nan_helpers import safe_float, clamp


# === FRED Macro Data ===

def fetch_fred_series(series_id: str, api_key: str = None) -> Optional[dict]:
    """
    Fetch FRED time series (Federal Reserve Economic Data).

    Free API. Key required: https://fred.stlouisfed.org/docs/api/api_key.html

    Common series:
        DFF      Federal Funds Effective Rate
        UNRATE   Unemployment Rate
        CPIAUCSL CPI All Urban
        T10Y2Y   10Y/2Y Yield Spread
        DCOILWTICO Crude Oil
        DEXJPUS  Yen/USD
        DGS10    10Y Treasury Yield
    """
    import urllib.request
    import json as _json
    if not api_key:
        return {"error": "FRED_API_KEY not set"}
    url = (f"https://api.stlouisfed.org/fred/series/observations?"
           f"series_id={series_id}&api_key={api_key}&file_type=json&"
           f"observation_start=2020-01-01&sort_order=desc&limit=500")
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = _json.loads(resp.read())
        return data
    except Exception as e:
        return {"error": str(e)[:200]}


def yield_curve_factor(t10y2y_value: float) -> dict:
    """
    Yield curve as factor signal.

    Inverted yield curve (negative spread) = recession signal historically.

    Returns:
        {regime, factor_signal} where signal in [-1, 1].
    """
    s = safe_float(t10y2y_value, 0.5)
    if s < -0.5:
        return {"regime": "DEEPLY_INVERTED", "factor_signal": -1.0,
                "interpretation": "strong recession signal"}
    if s < 0:
        return {"regime": "INVERTED", "factor_signal": -0.5,
                "interpretation": "recession signal"}
    if s < 0.5:
        return {"regime": "FLAT", "factor_signal": 0,
                "interpretation": "neutral"}
    if s < 1.5:
        return {"regime": "NORMAL", "factor_signal": 0.5,
                "interpretation": "expansionary"}
    return {"regime": "STEEP", "factor_signal": 1.0,
            "interpretation": "strong expansionary"}


def vix_term_structure_factor(vix_spot: float, vix_3m: float) -> dict:
    """
    VIX term structure = backwardation (spot > 3m) signals fear.

    Returns signal in [-1, 1].
    """
    spot = safe_float(vix_spot, 20)
    far = safe_float(vix_3m, 22)
    if far <= 0:
        return {"factor_signal": 0}
    ratio = spot / far
    if ratio > 1.10:
        return {"factor_signal": -1.0, "regime": "STRONG_BACKWARDATION",
                "interpretation": "elevated near-term fear, mean reversion likely"}
    if ratio > 1.0:
        return {"factor_signal": -0.5, "regime": "BACKWARDATION",
                "interpretation": "near-term fear"}
    if ratio > 0.92:
        return {"factor_signal": 0.0, "regime": "FLAT", "interpretation": "neutral"}
    return {"factor_signal": 0.5, "regime": "CONTANGO",
            "interpretation": "normal forward curve"}


# === SEC EDGAR ===

def fetch_sec_filings(ticker: str, form_type: str = "8-K", limit: int = 5) -> list:
    """
    Fetch recent SEC filings.

    EDGAR is free. URL pattern:
    https://data.sec.gov/submissions/CIK<paddedCIK>.json

    Returns list of recent filing metadata.
    """
    import urllib.request
    import json as _json
    # CIK lookup (cached separately in production)
    headers = {"User-Agent": "Epic Fury Research research@epicfurytrading.com"}
    try:
        # Use ticker ticker lookup
        url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}&type={form_type}&dateb=&owner=include&count={limit}&action=getcompany"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode()
        # Parse out filing dates and links (lightweight)
        filings = []
        for m in re.finditer(r'(\d{4}-\d{2}-\d{2}).*?(/Archives/edgar/data/\S+?index\.html?)', html):
            filings.append({"date": m.group(1), "url": "https://www.sec.gov" + m.group(2)})
            if len(filings) >= limit:
                break
        return filings
    except Exception as e:
        return [{"error": str(e)[:200]}]


def insider_form4_signal(form4_filings: list) -> dict:
    """
    Form-4: insider transactions. Net buying = bullish signal.

    Each filing has: insider name, transaction (P=purchase, S=sale),
    shares, value.

    Returns aggregated signal in [-1, 1] over last 30 days.
    """
    if not form4_filings:
        return {"factor_signal": 0, "n_filings": 0}
    buy_value = 0.0
    sell_value = 0.0
    for f in form4_filings:
        action = f.get("action", "")
        value = safe_float(f.get("value"), 0)
        if action == "P":  # Purchase
            buy_value += value
        elif action == "S":  # Sale
            sell_value += abs(value)
    total = buy_value + sell_value
    if total == 0:
        return {"factor_signal": 0, "n_filings": len(form4_filings)}
    net_score = (buy_value - sell_value) / total
    return {
        "factor_signal": clamp(net_score, -1, 1),
        "buy_dollars": buy_value,
        "sell_dollars": sell_value,
        "n_filings": len(form4_filings),
    }


# === News + Sentiment ===

def simple_news_sentiment(headlines: list) -> float:
    """
    Simple bag-of-words sentiment. Real version uses FinBERT.

    Returns signal in [-1, 1].
    """
    if not headlines:
        return 0.0
    positive_words = {
        "beat", "surge", "rally", "growth", "profit", "expand", "upgrade",
        "outperform", "exceed", "strong", "buy", "bullish", "soar", "jump",
        "win", "raises", "raised", "boost", "record", "high", "milestone",
    }
    negative_words = {
        "miss", "decline", "fall", "plunge", "drop", "loss", "weak", "downgrade",
        "underperform", "lower", "cut", "sell", "bearish", "concern", "warn",
        "warning", "risk", "fraud", "investigation", "lawsuit", "bankruptcy",
        "layoff", "fire", "crash",
    }
    pos = 0
    neg = 0
    for h in headlines:
        text = (h or "").lower()
        words = re.findall(r'\b\w+\b', text)
        for w in words:
            if w in positive_words:
                pos += 1
            elif w in negative_words:
                neg += 1
    total = pos + neg
    if total == 0:
        return 0.0
    return (pos - neg) / total


def reddit_wsb_sentiment(ticker: str) -> Optional[dict]:
    """
    Reddit WSB sentiment via PRAW or web scraping.

    Free with rate limits. Real implementation uses praw library.
    Stubbed here — needs PRAW + reddit API credentials.

    Returns:
        {mention_count_24h, sentiment_score (-1 to 1), bullish_pct}
    """
    # PRODUCTION: use praw library:
    # reddit = praw.Reddit(client_id=..., client_secret=..., user_agent=...)
    # subreddit = reddit.subreddit('wallstreetbets')
    # mentions = []
    # for submission in subreddit.search(ticker, time_filter='day', limit=100):
    #     mentions.append(submission.title + ' ' + submission.selftext)
    return {
        "_status": "stub_needs_praw_library",
        "mention_count_24h": None,
        "sentiment_score": None,
        "bullish_pct": None,
    }


# === Google Trends ===

def google_trends_factor(ticker: str) -> Optional[dict]:
    """
    Google Trends search interest as factor.

    Real implementation uses pytrends library.
    Surge in searches > 50% week-over-week = retail attention signal.
    Sustained surge often precedes squeezes (GME pattern).
    """
    return {
        "_status": "stub_needs_pytrends",
        "weekly_change_pct": None,
        "factor_signal": None,
    }


# === Wikipedia pageviews ===

def wikipedia_pageviews_factor(ticker: str, company_name: str) -> Optional[dict]:
    """
    Wikipedia pageview anomalies. Free API.

    Surge in pageviews can precede big moves (research from Preis et al).
    """
    import urllib.request
    import json as _json
    if not company_name:
        return None
    try:
        # Last 30 days of pageviews for company's Wikipedia page
        article = company_name.replace(" ", "_")
        url = (f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
               f"en.wikipedia/all-access/user/{article}/daily/"
               f"20250501/20250601")
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = _json.loads(resp.read())
        items = data.get("items", [])
        if not items:
            return None
        views = [it.get("views", 0) for it in items]
        if not views:
            return None
        recent_mean = sum(views[-7:]) / 7 if len(views) >= 7 else sum(views) / len(views)
        baseline = sum(views[:-7]) / max(1, len(views) - 7) if len(views) > 7 else recent_mean
        change_pct = ((recent_mean - baseline) / baseline * 100) if baseline > 0 else 0
        return {
            "recent_7d_avg_views": int(recent_mean),
            "baseline_avg_views": int(baseline),
            "change_pct": round(change_pct, 1),
            "factor_signal": clamp(change_pct / 100, -1, 1),
        }
    except Exception as e:
        return {"error": str(e)[:200]}
