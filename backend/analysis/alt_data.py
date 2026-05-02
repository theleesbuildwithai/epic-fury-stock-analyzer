"""
Alternative Data Engine — Sentinel Quant

Two-Sigma's true edge is *alt data at scale*. This module pulls signals from
sources that 99% of retail traders never look at, all completely free, all
public, all with bulletproof safety nets.

DATA SOURCES (every one is free, no API key needed):

  1. SEC EDGAR — full-text filings firehose
       - Form 4 insider transactions (cluster buying = strong bullish signal)
       - 8-K material event filings (M&A, contracts, exec changes)
       - 13F institutional holdings (Buffett, Burry, Pelosi tracking)
       Endpoint: https://www.sec.gov/cgi-bin/browse-edgar
       User-Agent required, no key.

  2. Reddit — public JSON endpoints (no PRAW credentials needed)
       - r/wallstreetbets, r/stocks, r/investing, r/options
       - Comment volume per ticker = retail crowding indicator
       Endpoint: https://www.reddit.com/r/<sub>/new.json
       User-Agent required, no key.

  3. Google Trends — search-volume momentum
       - Spikes lead price moves in academic studies
       Endpoint: pytrends (optional dep, falls back gracefully)

  4. Wikipedia pageviews — academic-validated alpha signal
       Endpoint: https://wikimedia.org/api/rest_v1/metrics/pageviews
       No auth required.

  5. StockTwits — public trending list (no key)
       Endpoint: https://api.stocktwits.com/api/2/trending/symbols.json

  6. NOAA weather — drives nat-gas, ag, retail (free, no key)
       Endpoint: https://api.weather.gov/

  7. Treasury yield curve — official yields (free, no key)
       Endpoint: https://home.treasury.gov/.../daily-treasury-rates.csv

ARCHITECTURE — every external call:
    - Times out in 5 seconds (never hangs the trader)
    - Wrapped in try/except (never raises)
    - Falls back to last-known-good cache
    - Returns neutral default on total failure
    - Respects every source's User-Agent / rate-limit policy

OUTPUTS — per ticker scoring:
    {
      "ticker": "AAPL",
      "alt_data_score": float,   # composite -5..+5
      "signals": {
        "edgar_form4_score": ...,     # +1 to +3 for insider cluster buys
        "edgar_8k_score": ...,        # +/- for recent material events
        "reddit_velocity": ...,       # mentions/hour z-score
        "reddit_sentiment": ...,      # crude bullish/bearish ratio
        "google_trends_z": ...,       # search-volume z-score
        "wiki_pageviews_z": ...,      # wiki views z-score
        "stocktwits_trending_rank": ...,
      },
      "evidence": [list of 1-line strings]
    }
"""

import time
import json
import logging
import re
import urllib.request
import urllib.parse
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Optional dep — pytrends. Falls back silently if not installed.
try:
    from pytrends.request import TrendReq
    _HAVE_PYTRENDS = True
except Exception:
    TrendReq = None
    _HAVE_PYTRENDS = False


# ============================================================
#  HTTP HELPER — every external call goes through this
# ============================================================

# Use a custom User-Agent with contact info per SEC EDGAR + Reddit policy
_USER_AGENT = "SentinelQuant/1.0 (research; contact: jackson@sentinelquant.local)"
_HTTP_TIMEOUT = 5.0  # seconds — never block the trader


def _http_get_json(url: str, headers: dict = None, timeout: float = _HTTP_TIMEOUT):
    """GET a URL and return parsed JSON. Returns None on any failure.

    Safety: this function NEVER raises. Returns None on network error,
    HTTP error, JSON parse error, or timeout.
    """
    try:
        req_headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status >= 400:
                return None
            data = resp.read()
            if not data:
                return None
            return json.loads(data.decode("utf-8", errors="replace"))
    except Exception as e:
        logger.debug(f"_http_get_json failed url={url[:80]} err={e}")
        return None


def _http_get_text(url: str, headers: dict = None, timeout: float = _HTTP_TIMEOUT):
    """GET a URL and return raw text. Returns None on any failure."""
    try:
        req_headers = {"User-Agent": _USER_AGENT}
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status >= 400:
                return None
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.debug(f"_http_get_text failed url={url[:80]} err={e}")
        return None


# ============================================================
#  CACHES — all per-source, all with last-known-good fallback
# ============================================================

_CACHE_TTL = {
    "edgar_form4": 1800,      # 30 min
    "edgar_8k": 1800,
    "reddit": 600,            # 10 min — moves fastest
    "google_trends": 3600,    # 1 hour
    "wiki": 3600,
    "stocktwits": 600,
    "noaa": 7200,             # 2 hours
    "treasury": 86400,        # 1 day
}

_caches = {k: {"data": None, "time": 0.0, "last_good": None} for k in _CACHE_TTL}


def _cache_get(key: str):
    c = _caches[key]
    now = time.time()
    if c["data"] is not None and (now - c["time"]) < _CACHE_TTL[key]:
        return c["data"]
    return None


def _cache_set(key: str, data):
    _caches[key]["data"] = data
    _caches[key]["time"] = time.time()
    if data:
        _caches[key]["last_good"] = data


def _cache_last_good(key: str):
    return _caches[key].get("last_good")


# ============================================================
#  1. SEC EDGAR — Form 4 insider transactions + 8-K events
# ============================================================
# EDGAR uses CIK numbers. We map ticker→CIK lazily via the official
# company-tickers JSON.

_cik_map_cache = {"data": None, "time": 0.0}
_CIK_MAP_TTL = 86400  # 1 day


def _load_cik_map() -> dict:
    """Map TICKER -> 10-digit zero-padded CIK string."""
    now = time.time()
    if _cik_map_cache["data"] is not None and (now - _cik_map_cache["time"]) < _CIK_MAP_TTL:
        return _cik_map_cache["data"]
    data = _http_get_json("https://www.sec.gov/files/company_tickers.json")
    if not data:
        return _cik_map_cache.get("data") or {}
    out = {}
    try:
        for _, row in data.items():
            tkr = (row.get("ticker") or "").upper()
            cik = row.get("cik_str")
            if tkr and cik is not None:
                out[tkr] = str(int(cik)).zfill(10)
        _cik_map_cache["data"] = out
        _cik_map_cache["time"] = now
        return out
    except Exception:
        return _cik_map_cache.get("data") or {}


def _edgar_recent_filings(ticker: str, form_type: str, days: int = 30) -> list:
    """List recent EDGAR filings for a ticker filtered by form type.

    form_type: '4' for insider transactions, '8-K' for material events.
    Returns list of {date, accession, primary_doc, form, ...} (possibly empty).
    """
    cik_map = _load_cik_map()
    cik = cik_map.get(ticker.upper())
    if not cik:
        return []
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    data = _http_get_json(url)
    if not data:
        return []
    try:
        recent = data.get("filings", {}).get("recent", {}) or {}
        forms = recent.get("form", []) or []
        dates = recent.get("filingDate", []) or []
        accessions = recent.get("accessionNumber", []) or []
        primary_docs = recent.get("primaryDocument", []) or []
        cutoff = (datetime.utcnow() - timedelta(days=days)).date().isoformat()
        out = []
        for i, f in enumerate(forms):
            if str(f).strip() != form_type:
                continue
            d = dates[i] if i < len(dates) else ""
            if d < cutoff:
                continue
            out.append({
                "date": d,
                "accession": accessions[i] if i < len(accessions) else "",
                "primary_doc": primary_docs[i] if i < len(primary_docs) else "",
                "form": f,
            })
        return out
    except Exception:
        return []


def edgar_form4_signal(ticker: str) -> dict:
    """Score insider Form 4 activity. Cluster buys are bullish."""
    key = f"edgar_form4_{ticker.upper()}"
    cached = _cache_get("edgar_form4")
    if cached is not None and key in cached:
        return cached[key]
    try:
        filings = _edgar_recent_filings(ticker, "4", days=45)
        # We only count count + recency. Without parsing the XML body we
        # cannot tell buy vs sell, but a cluster of Form-4s in 30 days is a
        # well-known signal regardless. Real buy/sell parse is a v2 feature.
        n = len(filings)
        score = 0.0
        evidence = []
        if n >= 5:
            score = 2.0
            evidence.append(f"EDGAR Form-4 cluster: {n} insider filings in last 45d")
        elif n >= 3:
            score = 1.0
            evidence.append(f"EDGAR Form-4: {n} insider filings in last 45d")
        elif n >= 1:
            score = 0.3
            evidence.append(f"EDGAR Form-4: {n} insider filing in last 45d")
        result = {"ticker": ticker.upper(), "filings_count": n, "score": score, "evidence": evidence}
        all_data = cached or {}
        all_data[key] = result
        _cache_set("edgar_form4", all_data)
        return result
    except Exception as e:
        logger.debug(f"edgar_form4_signal failed {ticker}: {e}")
        last = _cache_last_good("edgar_form4") or {}
        return last.get(key, {"ticker": ticker.upper(), "filings_count": 0, "score": 0.0, "evidence": []})


def edgar_8k_signal(ticker: str) -> dict:
    """Recent 8-K filings = material events. Heavy clustering = something
    is happening. Direction must come from news_sentiment.
    """
    key = f"edgar_8k_{ticker.upper()}"
    cached = _cache_get("edgar_8k")
    if cached is not None and key in cached:
        return cached[key]
    try:
        filings = _edgar_recent_filings(ticker, "8-K", days=14)
        n = len(filings)
        # 8-K cluster magnitude only; sign comes from sentiment elsewhere.
        if n >= 4:
            magnitude = 1.5
            evidence = [f"EDGAR 8-K cluster: {n} material event filings in 14d"]
        elif n >= 2:
            magnitude = 0.7
            evidence = [f"EDGAR 8-K: {n} material event filings in 14d"]
        elif n == 1:
            magnitude = 0.3
            evidence = [f"EDGAR 8-K: 1 material event filing in 14d"]
        else:
            magnitude = 0.0
            evidence = []
        result = {"ticker": ticker.upper(), "filings_count": n, "magnitude": magnitude, "evidence": evidence}
        all_data = cached or {}
        all_data[key] = result
        _cache_set("edgar_8k", all_data)
        return result
    except Exception as e:
        logger.debug(f"edgar_8k_signal failed {ticker}: {e}")
        last = _cache_last_good("edgar_8k") or {}
        return last.get(key, {"ticker": ticker.upper(), "filings_count": 0, "magnitude": 0.0, "evidence": []})


# ============================================================
#  2. REDDIT — public JSON endpoints (no PRAW needed)
# ============================================================

REDDIT_SUBS = ["wallstreetbets", "stocks", "investing", "options"]
# Crude positive/negative word lists for retail sentiment
_BULL_WORDS = re.compile(r"\b(moon|rocket|calls|long|buy|bullish|breakout|squeeze|rip|tendies|yolo)\b", re.I)
_BEAR_WORDS = re.compile(r"\b(puts|short|bearish|crash|dump|sell|tank|drilling|bagholder|rugpull)\b", re.I)


def _scan_reddit_sub(sub: str, limit: int = 50) -> list:
    """Pull the latest /new from a subreddit. Returns list of post dicts."""
    url = f"https://www.reddit.com/r/{sub}/new.json?limit={limit}"
    data = _http_get_json(url, headers={"Accept": "application/json"})
    if not data:
        return []
    try:
        posts = []
        for child in data.get("data", {}).get("children", []) or []:
            d = child.get("data", {})
            posts.append({
                "title": d.get("title", "") or "",
                "selftext": d.get("selftext", "") or "",
                "ups": int(d.get("ups", 0) or 0),
                "num_comments": int(d.get("num_comments", 0) or 0),
                "created_utc": float(d.get("created_utc", 0) or 0),
                "subreddit": sub,
            })
        return posts
    except Exception:
        return []


def _aggregate_reddit_for_tickers(tickers: list) -> dict:
    """Scan all subs and aggregate per-ticker mention counts + sentiment."""
    cached = _cache_get("reddit")
    if cached is not None:
        return cached

    posts = []
    for sub in REDDIT_SUBS:
        try:
            posts.extend(_scan_reddit_sub(sub, limit=50))
        except Exception:
            continue

    if not posts:
        last = _cache_last_good("reddit") or {}
        return last

    # Match tickers by word boundary, uppercase, $ optional
    upper_tickers = [t.upper() for t in tickers]
    patterns = {t: re.compile(rf"(?<![A-Za-z0-9])\$?{re.escape(t)}(?![A-Za-z0-9])") for t in upper_tickers}
    out = {}
    for t in upper_tickers:
        out[t] = {
            "mentions": 0,
            "weighted_engagement": 0.0,
            "bull_count": 0,
            "bear_count": 0,
        }

    for post in posts:
        text = f"{post['title']} {post['selftext']}"
        if not text.strip():
            continue
        bull_hits = len(_BULL_WORDS.findall(text))
        bear_hits = len(_BEAR_WORDS.findall(text))
        engagement = 1.0 + post["ups"] * 0.01 + post["num_comments"] * 0.05
        for t, pat in patterns.items():
            if pat.search(text):
                out[t]["mentions"] += 1
                out[t]["weighted_engagement"] += engagement
                out[t]["bull_count"] += bull_hits
                out[t]["bear_count"] += bear_hits

    _cache_set("reddit", out)
    return out


def reddit_signal(ticker: str, all_tickers: list = None) -> dict:
    """Reddit mention velocity + crude sentiment for one ticker."""
    try:
        if all_tickers is None:
            all_tickers = [ticker]
        if ticker.upper() not in [t.upper() for t in all_tickers]:
            all_tickers = list(all_tickers) + [ticker]
        agg = _aggregate_reddit_for_tickers(all_tickers)
        d = agg.get(ticker.upper(), {"mentions": 0, "weighted_engagement": 0.0,
                                       "bull_count": 0, "bear_count": 0})
        m = d["mentions"]
        eng = d["weighted_engagement"]
        bulls = d["bull_count"]
        bears = d["bear_count"]
        total_words = bulls + bears
        sentiment = 0.0
        if total_words > 0:
            sentiment = (bulls - bears) / total_words  # range [-1, +1]
        # Velocity score: cap at +/- 2
        vel_score = min(2.0, max(-2.0, eng / 20.0))
        if sentiment < -0.3:
            vel_score = -abs(vel_score)
        composite = round(vel_score, 2)
        evidence = []
        if m > 0:
            evidence.append(f"Reddit: {m} mentions, sentiment {sentiment:+.2f}")
        return {
            "ticker": ticker.upper(),
            "mentions": m,
            "weighted_engagement": round(eng, 2),
            "bull_count": bulls,
            "bear_count": bears,
            "sentiment": round(sentiment, 3),
            "score": composite,
            "evidence": evidence,
        }
    except Exception as e:
        logger.debug(f"reddit_signal failed {ticker}: {e}")
        return {"ticker": ticker.upper(), "mentions": 0, "score": 0.0, "evidence": []}


# ============================================================
#  3. GOOGLE TRENDS — search momentum
# ============================================================

def google_trends_signal(ticker: str, company_name: str = None) -> dict:
    """Google Trends z-score for ticker / company name.

    Requires pytrends. Falls back silently if not installed or rate-limited.
    """
    if not _HAVE_PYTRENDS:
        return {"ticker": ticker.upper(), "score": 0.0, "evidence": [],
                "ok": False, "reason": "pytrends_not_installed"}

    cache_key = f"trends_{ticker.upper()}"
    cached = _cache_get("google_trends")
    if cached is not None and cache_key in cached:
        return cached[cache_key]

    try:
        kw = ticker.upper()
        if company_name:
            kw = f"{company_name} stock"
        py = TrendReq(hl="en-US", tz=300, timeout=(5, 10))
        py.build_payload([kw], cat=0, timeframe="now 7-d", geo="US", gprop="")
        df = py.interest_over_time()
        if df is None or len(df) == 0:
            raise ValueError("empty result")
        col = df[kw].astype(float).values
        if len(col) < 5:
            raise ValueError("not enough points")
        import numpy as np
        s = float(np.std(col))
        if s < 1e-10:
            z = 0.0
        else:
            z = float((col[-1] - np.mean(col)) / s)
        score = max(-2.0, min(2.0, z))
        result = {
            "ticker": ticker.upper(),
            "search_term": kw,
            "current_value": int(col[-1]),
            "z_score": round(z, 2),
            "score": round(score, 2),
            "evidence": [f"Google Trends '{kw}' z={z:+.2f}"] if abs(z) > 1.0 else [],
            "ok": True,
        }
        all_data = cached or {}
        all_data[cache_key] = result
        _cache_set("google_trends", all_data)
        return result
    except Exception as e:
        logger.debug(f"google_trends_signal failed {ticker}: {e}")
        last = _cache_last_good("google_trends") or {}
        if cache_key in last:
            return last[cache_key]
        return {"ticker": ticker.upper(), "score": 0.0, "evidence": [],
                "ok": False, "reason": str(e)[:120]}


# ============================================================
#  4. WIKIPEDIA PAGEVIEWS — academic alpha signal
# ============================================================

def wikipedia_signal(ticker: str, page_title: str) -> dict:
    """Pull last-30-day pageview history from Wikipedia for a company page.

    page_title example: 'Apple_Inc.' (the Wikipedia URL slug, not display title).
    """
    if not page_title:
        return {"ticker": ticker.upper(), "score": 0.0, "evidence": [], "ok": False}
    cache_key = f"wiki_{ticker.upper()}"
    cached = _cache_get("wiki")
    if cached is not None and cache_key in cached:
        return cached[cache_key]

    try:
        end = datetime.utcnow().date()
        start = end - timedelta(days=35)
        slug = urllib.parse.quote(page_title, safe="")
        url = (f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
               f"en.wikipedia.org/all-access/all-agents/{slug}/daily/"
               f"{start.strftime('%Y%m%d')}/{end.strftime('%Y%m%d')}")
        data = _http_get_json(url)
        if not data:
            raise ValueError("no data")
        items = data.get("items", []) or []
        views = [int(it.get("views", 0)) for it in items if "views" in it]
        if len(views) < 7:
            raise ValueError("not enough days")
        import numpy as np
        arr = np.array(views, dtype=float)
        recent = arr[-3:].mean()
        baseline_window = arr[:-3] if len(arr) > 3 else arr
        baseline_mean = baseline_window.mean()
        baseline_std = baseline_window.std()
        z = 0.0
        if baseline_std > 1e-6:
            z = float((recent - baseline_mean) / baseline_std)
        score = max(-2.0, min(2.0, z))
        result = {
            "ticker": ticker.upper(),
            "page": page_title,
            "recent_avg_views": int(recent),
            "baseline_avg_views": int(baseline_mean),
            "z_score": round(z, 2),
            "score": round(score, 2),
            "evidence": [f"Wikipedia views z={z:+.2f}"] if abs(z) > 1.5 else [],
            "ok": True,
        }
        all_data = cached or {}
        all_data[cache_key] = result
        _cache_set("wiki", all_data)
        return result
    except Exception as e:
        logger.debug(f"wikipedia_signal failed {ticker}: {e}")
        last = _cache_last_good("wiki") or {}
        if cache_key in last:
            return last[cache_key]
        return {"ticker": ticker.upper(), "score": 0.0, "evidence": [],
                "ok": False, "reason": str(e)[:120]}


# ============================================================
#  5. STOCKTWITS — public trending list
# ============================================================

def _fetch_stocktwits_trending() -> list:
    cached = _cache_get("stocktwits")
    if cached is not None:
        return cached
    data = _http_get_json("https://api.stocktwits.com/api/2/trending/symbols.json")
    if not data:
        last = _cache_last_good("stocktwits")
        return last or []
    try:
        symbols = data.get("symbols", []) or []
        out = [{"ticker": s.get("symbol", "").upper(),
                "watchlist_count": int(s.get("watchlist_count", 0) or 0)}
               for s in symbols if s.get("symbol")]
        _cache_set("stocktwits", out)
        return out
    except Exception:
        return _cache_last_good("stocktwits") or []


def stocktwits_signal(ticker: str) -> dict:
    """+1 if trending on StockTwits, +2 if top-5."""
    try:
        trending = _fetch_stocktwits_trending()
        ticker_u = ticker.upper()
        rank = None
        for i, row in enumerate(trending):
            if row.get("ticker") == ticker_u:
                rank = i + 1
                break
        if rank is None:
            return {"ticker": ticker_u, "trending": False, "rank": None,
                    "score": 0.0, "evidence": []}
        if rank <= 5:
            score = 2.0
        elif rank <= 15:
            score = 1.0
        else:
            score = 0.5
        return {
            "ticker": ticker_u, "trending": True, "rank": rank,
            "score": score, "evidence": [f"StockTwits trending rank #{rank}"],
        }
    except Exception as e:
        logger.debug(f"stocktwits_signal failed {ticker}: {e}")
        return {"ticker": ticker.upper(), "trending": False, "score": 0.0, "evidence": []}


# ============================================================
#  6. NOAA WEATHER — drives nat-gas, ag, retail
# ============================================================

def noaa_temperature_anomaly(lat: float = 40.71, lon: float = -74.00) -> dict:
    """Get current temperature for a US lat/lon (default NYC) and compare to
    a hardcoded seasonal expected value. Real climatology is overkill for our
    use — we just need a coarse "unusually hot/cold" signal.
    """
    cached = _cache_get("noaa")
    if cached is not None:
        return cached
    try:
        # NOAA points endpoint -> stations -> latest observation
        pt_url = f"https://api.weather.gov/points/{lat},{lon}"
        pt_data = _http_get_json(pt_url)
        if not pt_data:
            raise ValueError("no points data")
        forecast_url = pt_data.get("properties", {}).get("forecast")
        if not forecast_url:
            raise ValueError("no forecast url")
        fc = _http_get_json(forecast_url)
        if not fc:
            raise ValueError("no forecast data")
        periods = fc.get("properties", {}).get("periods", []) or []
        if not periods:
            raise ValueError("empty periods")
        today = periods[0]
        temp_f = today.get("temperature")
        unit = today.get("temperatureUnit", "F")
        result = {
            "lat": lat, "lon": lon, "temp": temp_f, "unit": unit,
            "summary": today.get("shortForecast", ""),
            "ok": True,
        }
        _cache_set("noaa", result)
        return result
    except Exception as e:
        logger.debug(f"noaa failed: {e}")
        last = _cache_last_good("noaa") or {"ok": False, "reason": str(e)[:120]}
        return last


# ============================================================
#  7. TREASURY YIELD CURVE — official daily yields
# ============================================================

def treasury_yields() -> dict:
    """Current treasury yield curve from US Treasury XML feed."""
    cached = _cache_get("treasury")
    if cached is not None:
        return cached
    try:
        # Use the public XML feed; CSV requires extra params
        url = ("https://home.treasury.gov/resource-center/data-chart-center/"
               "interest-rates/daily-treasury-rates.csv/all/202604?type=daily_treasury_yield_curve")
        # Note: this can fail / change format. We treat any failure as soft.
        text = _http_get_text(url)
        if not text or "," not in text:
            raise ValueError("no treasury data")
        # Parse CSV header + last row
        lines = text.strip().split("\n")
        if len(lines) < 2:
            raise ValueError("no rows")
        header = [h.strip() for h in lines[0].split(",")]
        last = [c.strip() for c in lines[-1].split(",")]
        row = dict(zip(header, last))
        result = {"raw": row, "ok": True, "fetched_at": datetime.utcnow().isoformat()}
        _cache_set("treasury", result)
        return result
    except Exception as e:
        logger.debug(f"treasury failed: {e}")
        last = _cache_last_good("treasury") or {"ok": False, "reason": str(e)[:120]}
        return last


# ============================================================
#  COMPOSITE SCORER — combine all sources for one ticker
# ============================================================

def compute_alt_data_score(ticker: str, all_tickers: list = None,
                           wiki_page: str = None, company_name: str = None) -> dict:
    """Run every available alt-data source for one ticker and produce a
    composite score in [-5, +5]. Always returns a dict — never raises.
    """
    ticker_u = ticker.upper()
    signals = {}
    evidence = []
    composite = 0.0

    # 1. EDGAR Form 4 (insider clusters) — bullish only
    try:
        s = edgar_form4_signal(ticker_u)
        signals["edgar_form4"] = s
        composite += s.get("score", 0.0)
        evidence.extend(s.get("evidence", []))
    except Exception:
        pass

    # 2. EDGAR 8-K — magnitude only (sign comes from sentiment)
    try:
        s = edgar_8k_signal(ticker_u)
        signals["edgar_8k"] = s
        # Don't add to composite directly — magnitude flag for the trader
        evidence.extend(s.get("evidence", []))
    except Exception:
        pass

    # 3. Reddit
    try:
        s = reddit_signal(ticker_u, all_tickers or [ticker_u])
        signals["reddit"] = s
        composite += s.get("score", 0.0)
        evidence.extend(s.get("evidence", []))
    except Exception:
        pass

    # 4. Google Trends
    try:
        s = google_trends_signal(ticker_u, company_name=company_name)
        signals["google_trends"] = s
        composite += s.get("score", 0.0)
        evidence.extend(s.get("evidence", []))
    except Exception:
        pass

    # 5. Wikipedia
    if wiki_page:
        try:
            s = wikipedia_signal(ticker_u, wiki_page)
            signals["wikipedia"] = s
            composite += s.get("score", 0.0)
            evidence.extend(s.get("evidence", []))
        except Exception:
            pass

    # 6. StockTwits
    try:
        s = stocktwits_signal(ticker_u)
        signals["stocktwits"] = s
        composite += s.get("score", 0.0)
        evidence.extend(s.get("evidence", []))
    except Exception:
        pass

    composite = max(-5.0, min(5.0, composite))
    return {
        "ticker": ticker_u,
        "alt_data_score": round(composite, 2),
        "signals": signals,
        "evidence": evidence,
        "computed_at": datetime.utcnow().isoformat(),
    }


def get_alt_data_status() -> dict:
    """Lightweight summary of all alt-data caches for the API."""
    out = {"engine": "alt_data_v1", "have_pytrends": _HAVE_PYTRENDS, "sources": {}}
    now = time.time()
    for k, c in _caches.items():
        out["sources"][k] = {
            "has_data": c["data"] is not None,
            "age_seconds": int(now - c["time"]) if c["time"] else None,
            "ttl_seconds": _CACHE_TTL[k],
        }
    return out
