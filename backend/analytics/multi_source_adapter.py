"""
Multi-Source Adapter — analytics/multi_source_adapter.py

Free data source safety nets beyond yfinance, Stooq, CNBC, and Finnhub.
Every function here:
  - NEVER raises an exception — returns None on any failure
  - Thread-timeout guarded (8-12s per source)
  - Logs at DEBUG so failures are traceable without polluting INFO

Sources (no API key required):
  - stockanalysis.com  — price, fundamentals (scrape __NEXT_DATA__)
  - finviz.com         — price, fundamentals (scrape snapshot table)

Sources (free API key, optional — set env vars):
  - Alpha Vantage      — historical OHLCV (ALPHA_VANTAGE_KEY, 25 calls/day free)
  - Tiingo             — historical OHLCV (TIINGO_KEY, 500 calls/day free)
  - Financial Modeling Prep — quote + historical (FMP_KEY, 250 calls/day free)

Integration points:
  - data_shield.safe_download()      Layer 3b after Stooq
  - data_shield.safe_ticker_info()   Layer 2b after yfinance fails
  - market_data.get_historical_data() Tier 2.7 after yf.Ticker.history
  - market_data.get_stock_info()      Fallback after yfinance fails
"""

import os
import json
import time
import logging
import threading
import re
import urllib.request
import urllib.parse
from typing import Optional
import pandas as pd
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}
_JSON_HEADERS = {
    **_BROWSER_HEADERS,
    "Accept": "application/json, */*;q=0.8",
}

_HTTP_TIMEOUT = 9  # seconds per urllib call


def _run_with_timeout(fn, timeout: int = 12):
    """Run fn() in a daemon thread; return result[0] or None if timeout/exception."""
    result = [None]

    def _wrap():
        try:
            result[0] = fn()
        except Exception as e:
            logger.debug(f"MultiSource _run_with_timeout inner exception: {e}")

    t = threading.Thread(target=_wrap, daemon=True)
    t.start()
    t.join(timeout=timeout)
    return result[0]


def _http_get(url: str, headers: dict = None, timeout: int = _HTTP_TIMEOUT) -> Optional[bytes]:
    """Simple urllib GET. Returns raw bytes or None."""
    try:
        req = urllib.request.Request(url, headers=headers or _BROWSER_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        logger.debug(f"MultiSource _http_get failed ({url[:80]}): {e}")
        return None


def _http_get_json(url: str, headers: dict = None) -> Optional[dict]:
    """GET JSON from url; returns parsed dict/list or None."""
    raw = _http_get(url, headers=headers or _JSON_HEADERS)
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return None


# ============================================================
#  STOCKANALYSIS.COM  (no API key)
# ============================================================

def _parse_stockanalysis_nextdata(html: str) -> Optional[dict]:
    """Extract the Next.js __NEXT_DATA__ JSON from a stockanalysis.com page."""
    try:
        match = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            html, re.DOTALL
        )
        if not match:
            return None
        return json.loads(match.group(1))
    except Exception:
        return None


def _fetch_stockanalysis_price(ticker: str) -> Optional[float]:
    """Scrape current price from stockanalysis.com."""
    raw = _http_get(f"https://stockanalysis.com/stocks/{ticker.lower()}/")
    if not raw:
        return None
    try:
        html = raw.decode("utf-8", errors="replace")
        page = _parse_stockanalysis_nextdata(html)
        if not page:
            return None
        props = page.get("props", {}).get("pageProps", {})
        # Path varies by page version — try all known locations
        for path in [
            lambda p: p.get("quote", {}).get("price"),
            lambda p: p.get("data", {}).get("price"),
            lambda p: p.get("info", {}).get("price"),
            lambda p: p.get("stockData", {}).get("price"),
            lambda p: p.get("initialData", {}).get("price"),
        ]:
            try:
                v = path(props)
                if v is not None and float(v) > 0:
                    return float(v)
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"StockAnalysis price parse failed for {ticker}: {e}")
    return None


def get_stockanalysis_price(ticker: str) -> Optional[float]:
    """Current price from stockanalysis.com. Returns None on any failure."""
    return _run_with_timeout(lambda: _fetch_stockanalysis_price(ticker), timeout=12)


def _fetch_stockanalysis_fundamentals(ticker: str) -> Optional[dict]:
    """Scrape key fundamentals from stockanalysis.com overview page."""
    raw = _http_get(f"https://stockanalysis.com/stocks/{ticker.lower()}/")
    if not raw:
        return None
    try:
        html = raw.decode("utf-8", errors="replace")
        page = _parse_stockanalysis_nextdata(html)
        if not page:
            return None
        props = page.get("props", {}).get("pageProps", {})
        # Collect info from various possible locations
        info = {}
        for key in ("quote", "data", "info", "stockData", "initialData"):
            candidate = props.get(key)
            if isinstance(candidate, dict) and candidate:
                info.update(candidate)

        if not info:
            return None

        def _val(*keys):
            for k in keys:
                v = info.get(k)
                if v is not None:
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        return v
            return None

        result = {
            "currentPrice": _val("price"),
            "marketCap": _val("marketCap", "mktCap"),
            "trailingPE": _val("pe", "peRatio", "trailingPE"),
            "forwardPE": _val("forwardPE"),
            "eps": _val("eps"),
            "dividendYield": _val("dividendYield", "yield"),
            "fiftyTwoWeekHigh": _val("high52", "week52High", "yearHigh"),
            "fiftyTwoWeekLow": _val("low52", "week52Low", "yearLow"),
            "volume": _val("volume"),
            "avgVolume": _val("avgVolume", "averageVolume"),
            "beta": _val("beta"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "shortName": info.get("name") or ticker.upper(),
            "_source": "stockanalysis",
        }
        # Only return if we got at least a price
        if result.get("currentPrice") and float(result["currentPrice"]) > 0:
            return result
    except Exception as e:
        logger.debug(f"StockAnalysis fundamentals parse failed for {ticker}: {e}")
    return None


def get_stockanalysis_fundamentals(ticker: str) -> Optional[dict]:
    """Key fundamentals from stockanalysis.com. Returns None on failure."""
    return _run_with_timeout(lambda: _fetch_stockanalysis_fundamentals(ticker), timeout=14)


# ============================================================
#  FINVIZ.COM  (no API key)
# ============================================================

def _fetch_finviz_snapshot(ticker: str) -> Optional[dict]:
    """Scrape fundamental snapshot from finviz.com quote page."""
    raw = _http_get(
        f"https://finviz.com/quote.ashx?t={ticker.upper()}",
        headers={**_BROWSER_HEADERS, "Referer": "https://finviz.com/"},
    )
    if not raw:
        return None
    try:
        html = raw.decode("utf-8", errors="replace")

        # Extract key-value pairs from the snapshot table
        # Finviz uses: <td ...>LABEL</td><td ...>VALUE</td> in alternating cells
        cells = re.findall(r'<td[^>]*class="[^"]*snapshot-td2[^"]*"[^>]*>(.*?)</td>', html, re.DOTALL)
        # Strip HTML tags from cell text
        def strip_tags(s):
            return re.sub(r'<[^>]+>', '', s).strip()

        kv = {}
        for i in range(0, len(cells) - 1, 2):
            label = strip_tags(cells[i])
            value = strip_tags(cells[i + 1])
            if label:
                kv[label] = value

        if not kv:
            # Fallback pattern: data-boxover labels
            pairs = re.findall(
                r'<td[^>]*>\s*([A-Z][^<]{1,30}?)\s*</td>\s*<td[^>]*>\s*([^<]{1,30}?)\s*</td>',
                html
            )
            for label, value in pairs:
                kv[label.strip()] = value.strip()

        if not kv:
            return None

        def safe_num(key, *alt_keys):
            for k in [key, *alt_keys]:
                raw_v = kv.get(k, "").replace(",", "").replace("%", "").strip()
                if raw_v in ("-", "", "N/A", "-"):
                    continue
                # Handle B/M/K suffixes
                multipliers = {"B": 1e9, "M": 1e6, "K": 1e3, "T": 1e12}
                mult = 1.0
                if raw_v and raw_v[-1] in multipliers:
                    mult = multipliers[raw_v[-1]]
                    raw_v = raw_v[:-1]
                try:
                    return float(raw_v) * mult
                except ValueError:
                    continue
            return None

        price = safe_num("Price")
        if not price or price <= 0:
            return None

        return {
            "currentPrice": price,
            "trailingPE": safe_num("P/E"),
            "forwardPE": safe_num("Forward P/E", "Fwd P/E"),
            "eps": safe_num("EPS (ttm)", "EPS"),
            "beta": safe_num("Beta"),
            "volume": safe_num("Volume"),
            "avgVolume": safe_num("Avg Volume"),
            "fiftyTwoWeekHigh": safe_num("52W High"),
            "fiftyTwoWeekLow": safe_num("52W Low"),
            "dividendYield": safe_num("Dividend %", "Div Yield"),
            "marketCap": safe_num("Market Cap"),
            "shortName": ticker.upper(),
            "_source": "finviz",
        }
    except Exception as e:
        logger.debug(f"Finviz snapshot parse failed for {ticker}: {e}")
    return None


def get_finviz_snapshot(ticker: str) -> Optional[dict]:
    """Key fundamentals from finviz.com. Returns None on failure."""
    return _run_with_timeout(lambda: _fetch_finviz_snapshot(ticker), timeout=12)


def get_finviz_price(ticker: str) -> Optional[float]:
    """Current price from finviz.com. Returns None on failure."""
    snap = get_finviz_snapshot(ticker)
    if snap and snap.get("currentPrice"):
        return float(snap["currentPrice"])
    return None


# ============================================================
#  ALPHA VANTAGE  (free key: ALPHA_VANTAGE_KEY env var)
# ============================================================

_AV_KEY = lambda: os.environ.get("ALPHA_VANTAGE_KEY", "").strip()

_PERIOD_DAYS = {
    "5d": 5, "1mo": 30, "3mo": 90, "6mo": 182,
    "1y": 365, "2y": 730, "5y": 1825,
}


def _fetch_alphavantage_historical(ticker: str, period: str = "6mo") -> Optional[pd.DataFrame]:
    """Historical OHLCV from Alpha Vantage (25 calls/day on free tier)."""
    key = _AV_KEY()
    if not key:
        return None
    try:
        outputsize = "full" if period in ("1y", "2y", "5y") else "compact"
        url = (
            f"https://www.alphavantage.co/query"
            f"?function=TIME_SERIES_DAILY_ADJUSTED"
            f"&symbol={urllib.parse.quote(ticker)}"
            f"&outputsize={outputsize}"
            f"&apikey={key}"
        )
        data = _http_get_json(url)
        if not data:
            return None
        if "Note" in data or "Information" in data:
            logger.debug(f"AlphaVantage rate-limited for {ticker}")
            return None

        ts = data.get("Time Series (Daily)")
        if not ts:
            return None

        days = _PERIOD_DAYS.get(period, 182)
        cutoff = datetime.now() - timedelta(days=days)
        records = []
        for date_str, vals in sorted(ts.items()):
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                if dt < cutoff:
                    continue
                records.append({
                    "Date": dt,
                    "Open": float(vals.get("1. open", 0)),
                    "High": float(vals.get("2. high", 0)),
                    "Low": float(vals.get("3. low", 0)),
                    "Close": float(vals.get("5. adjusted close") or vals.get("4. close", 0)),
                    "Volume": int(float(vals.get("6. volume", 0))),
                })
            except (ValueError, TypeError, KeyError):
                continue

        if len(records) < 5:
            return None

        df = pd.DataFrame(records).set_index("Date").sort_index()
        logger.info(f"AlphaVantage historical succeeded for {ticker} ({len(df)} rows)")
        return df
    except Exception as e:
        logger.debug(f"AlphaVantage historical failed for {ticker}: {e}")
    return None


def get_alphavantage_historical(ticker: str, period: str = "6mo") -> Optional[pd.DataFrame]:
    """Historical data from Alpha Vantage. Returns None if key not set or call fails."""
    if not _AV_KEY():
        return None
    return _run_with_timeout(lambda: _fetch_alphavantage_historical(ticker, period), timeout=14)


# ============================================================
#  TIINGO  (free key: TIINGO_KEY env var, 500 calls/day)
# ============================================================

_TIINGO_KEY = lambda: os.environ.get("TIINGO_KEY", "").strip()


def _fetch_tiingo_historical(ticker: str, period: str = "6mo") -> Optional[pd.DataFrame]:
    """Historical OHLCV from Tiingo (500 calls/day free tier)."""
    key = _TIINGO_KEY()
    if not key:
        return None
    try:
        days = _PERIOD_DAYS.get(period, 182)
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        url = (
            f"https://api.tiingo.com/tiingo/daily/{ticker.lower()}/prices"
            f"?startDate={urllib.parse.quote(start)}&resampleFreq=daily&format=json"
        )
        headers = {**_JSON_HEADERS, "Authorization": f"Token {key}"}
        raw = _http_get(url, headers=headers)
        if not raw:
            return None
        rows = json.loads(raw)
        if not rows or not isinstance(rows, list) or len(rows) < 5:
            return None

        records = []
        for row in rows:
            try:
                records.append({
                    "Date": pd.to_datetime(row["date"]),
                    "Open": float(row.get("adjOpen") or row.get("open") or 0),
                    "High": float(row.get("adjHigh") or row.get("high") or 0),
                    "Low": float(row.get("adjLow") or row.get("low") or 0),
                    "Close": float(row.get("adjClose") or row.get("close") or 0),
                    "Volume": int(row.get("volume") or 0),
                })
            except (KeyError, ValueError, TypeError):
                continue

        if len(records) < 5:
            return None

        df = pd.DataFrame(records).set_index("Date").sort_index()
        logger.info(f"Tiingo historical succeeded for {ticker} ({len(df)} rows)")
        return df
    except Exception as e:
        logger.debug(f"Tiingo historical failed for {ticker}: {e}")
    return None


def get_tiingo_historical(ticker: str, period: str = "6mo") -> Optional[pd.DataFrame]:
    """Historical data from Tiingo. Returns None if key not set or call fails."""
    if not _TIINGO_KEY():
        return None
    return _run_with_timeout(lambda: _fetch_tiingo_historical(ticker, period), timeout=14)


# ============================================================
#  FINANCIAL MODELING PREP  (free key: FMP_KEY env var, 250/day)
# ============================================================

_FMP_KEY = lambda: os.environ.get("FMP_KEY", "").strip()
_FMP_BASE = "https://financialmodelingprep.com/api/v3"


def _fetch_fmp_quote(ticker: str) -> Optional[dict]:
    """Real-time quote from FMP (250 calls/day free tier)."""
    key = _FMP_KEY()
    if not key:
        return None
    try:
        url = f"{_FMP_BASE}/quote/{urllib.parse.quote(ticker.upper())}?apikey={key}"
        data = _http_get_json(url)
        if not data or not isinstance(data, list) or not data[0]:
            return None
        q = data[0]
        price = float(q.get("price") or 0)
        if price <= 0:
            return None
        return {
            "currentPrice": price,
            "marketCap": q.get("marketCap"),
            "trailingPE": q.get("pe"),
            "eps": q.get("eps"),
            "volume": q.get("volume"),
            "avgVolume": q.get("avgVolume"),
            "fiftyTwoWeekHigh": q.get("yearHigh"),
            "fiftyTwoWeekLow": q.get("yearLow"),
            "previousClose": q.get("previousClose"),
            "shortName": q.get("name", ticker.upper()),
            "_source": "fmp",
        }
    except Exception as e:
        logger.debug(f"FMP quote failed for {ticker}: {e}")
    return None


def get_fmp_quote(ticker: str) -> Optional[dict]:
    """Quote from FMP. Returns None if key not set or call fails."""
    if not _FMP_KEY():
        return None
    return _run_with_timeout(lambda: _fetch_fmp_quote(ticker), timeout=12)


def _fetch_fmp_historical(ticker: str, period: str = "6mo") -> Optional[pd.DataFrame]:
    """Historical prices from FMP (250 calls/day free tier)."""
    key = _FMP_KEY()
    if not key:
        return None
    try:
        days = _PERIOD_DAYS.get(period, 182)
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        url = (
            f"{_FMP_BASE}/historical-price-full/{urllib.parse.quote(ticker.upper())}"
            f"?from={start}&to={end}&apikey={key}"
        )
        data = _http_get_json(url)
        if not data:
            return None
        historical = data.get("historical", [])
        if not historical or len(historical) < 5:
            return None

        records = []
        for row in historical:
            try:
                records.append({
                    "Date": pd.to_datetime(row["date"]),
                    "Open": float(row.get("adjOpen") or row.get("open") or 0),
                    "High": float(row.get("adjHigh") or row.get("high") or 0),
                    "Low": float(row.get("adjLow") or row.get("low") or 0),
                    "Close": float(row.get("adjClose") or row.get("close") or 0),
                    "Volume": int(row.get("volume") or 0),
                })
            except (KeyError, ValueError, TypeError):
                continue

        if len(records) < 5:
            return None

        df = pd.DataFrame(records).set_index("Date").sort_index()
        logger.info(f"FMP historical succeeded for {ticker} ({len(df)} rows)")
        return df
    except Exception as e:
        logger.debug(f"FMP historical failed for {ticker}: {e}")
    return None


def get_fmp_historical(ticker: str, period: str = "6mo") -> Optional[pd.DataFrame]:
    """Historical data from FMP. Returns None if key not set or call fails."""
    if not _FMP_KEY():
        return None
    return _run_with_timeout(lambda: _fetch_fmp_historical(ticker, period), timeout=14)


# ============================================================
#  COMBINED RESOLVERS — used by data_shield + market_data
# ============================================================

def get_price_any_source(ticker: str) -> Optional[float]:
    """
    Try every no-key free source for a current price.
    Returns first valid price or None.

    Chain: stockanalysis.com → finviz.com → fmp (if key)
    """
    # 1. stockanalysis (no key)
    try:
        price = get_stockanalysis_price(ticker)
        if price and price > 0:
            logger.info(f"MultiSource price for {ticker}: stockanalysis={price}")
            return price
    except Exception:
        pass

    # 2. finviz (no key)
    try:
        price = get_finviz_price(ticker)
        if price and price > 0:
            logger.info(f"MultiSource price for {ticker}: finviz={price}")
            return price
    except Exception:
        pass

    # 3. FMP (optional key)
    try:
        fmp = get_fmp_quote(ticker)
        if fmp and fmp.get("currentPrice") and float(fmp["currentPrice"]) > 0:
            logger.info(f"MultiSource price for {ticker}: fmp={fmp['currentPrice']}")
            return float(fmp["currentPrice"])
    except Exception:
        pass

    return None


def get_fundamentals_any_source(ticker: str) -> Optional[dict]:
    """
    Try every free source for fundamentals (price + metadata).
    Returns first successful dict with currentPrice > 0, or None.

    Chain: stockanalysis → finviz → fmp (if key)
    """
    # 1. stockanalysis (no key) — richest metadata
    try:
        info = get_stockanalysis_fundamentals(ticker)
        if info and info.get("currentPrice") and float(info["currentPrice"]) > 0:
            return info
    except Exception:
        pass

    # 2. finviz (no key)
    try:
        info = get_finviz_snapshot(ticker)
        if info and info.get("currentPrice") and float(info["currentPrice"]) > 0:
            return info
    except Exception:
        pass

    # 3. FMP (optional key)
    try:
        info = get_fmp_quote(ticker)
        if info and info.get("currentPrice") and float(info["currentPrice"]) > 0:
            return info
    except Exception:
        pass

    return None


def get_historical_any_source(ticker: str, period: str = "6mo") -> Optional[pd.DataFrame]:
    """
    Try every source for historical OHLCV data.
    Returns first DataFrame with >= 5 rows, or None.

    Chain: tiingo (500/day) → alpha_vantage (25/day) → fmp (250/day)
    All require optional env-var API keys. Returns None if none configured.
    """
    # 1. Tiingo (500/day — most generous free tier)
    try:
        df = get_tiingo_historical(ticker, period)
        if df is not None and len(df) >= 5:
            logger.info(f"MultiSource historical for {ticker}: tiingo ({len(df)} rows)")
            return df
    except Exception:
        pass

    # 2. Alpha Vantage (25/day — excellent data quality)
    try:
        df = get_alphavantage_historical(ticker, period)
        if df is not None and len(df) >= 5:
            logger.info(f"MultiSource historical for {ticker}: alphavantage ({len(df)} rows)")
            return df
    except Exception:
        pass

    # 3. FMP historical (250/day)
    try:
        df = get_fmp_historical(ticker, period)
        if df is not None and len(df) >= 5:
            logger.info(f"MultiSource historical for {ticker}: fmp ({len(df)} rows)")
            return df
    except Exception:
        pass

    return None


# ============================================================
#  BATCH QUOTE FUNCTIONS — same shape as cnbc_quote_batch / stooq_quote_batch
#  Returns: {symbol: {"price": float, "change_pct": float}}
# ============================================================

def _fetch_stockanalysis_quote(ticker: str) -> Optional[dict]:
    """Single-ticker quote from stockanalysis.com. Returns {"price", "change_pct"} or None."""
    raw = _http_get(f"https://stockanalysis.com/stocks/{ticker.lower()}/")
    if not raw:
        return None
    try:
        html = raw.decode("utf-8", errors="replace")
        page = _parse_stockanalysis_nextdata(html)
        if not page:
            return None
        props = page.get("props", {}).get("pageProps", {})
        info = {}
        for key in ("quote", "data", "info", "stockData", "initialData"):
            candidate = props.get(key)
            if isinstance(candidate, dict) and candidate:
                info.update(candidate)
        if not info:
            return None

        price = None
        for k in ("price", "lastPrice", "regularMarketPrice"):
            v = info.get(k)
            if v is not None:
                try:
                    price = float(v)
                    if price > 0:
                        break
                except (TypeError, ValueError):
                    pass

        change_pct = 0.0
        for k in ("changesPercentage", "changePct", "changePercent", "change_pct", "percentChange"):
            v = info.get(k)
            if v is not None:
                try:
                    change_pct = float(str(v).replace("%", ""))
                    break
                except (TypeError, ValueError):
                    pass

        if price and price > 0:
            return {"price": round(price, 2), "change_pct": round(change_pct, 2)}
    except Exception as e:
        logger.debug(f"StockAnalysis quote parse failed for {ticker}: {e}")
    return None


def _fetch_finviz_quote(ticker: str) -> Optional[dict]:
    """Single-ticker quote from finviz.com. Returns {"price", "change_pct"} or None."""
    raw = _http_get(
        f"https://finviz.com/quote.ashx?t={ticker.upper()}",
        headers={**_BROWSER_HEADERS, "Referer": "https://finviz.com/"},
    )
    if not raw:
        return None
    try:
        html = raw.decode("utf-8", errors="replace")
        cells = re.findall(r'<td[^>]*class="[^"]*snapshot-td2[^"]*"[^>]*>(.*?)</td>', html, re.DOTALL)
        def strip_tags(s):
            return re.sub(r'<[^>]+>', '', s).strip()

        kv = {}
        for i in range(0, len(cells) - 1, 2):
            label = strip_tags(cells[i])
            value = strip_tags(cells[i + 1])
            if label:
                kv[label] = value

        price_str = kv.get("Price", "")
        change_str = kv.get("Change", "0.00%")

        price = None
        try:
            price = float(price_str.replace(",", ""))
        except (ValueError, TypeError):
            pass

        change_pct = 0.0
        try:
            change_pct = float(str(change_str).replace("%", "").replace("+", "").strip())
        except (ValueError, TypeError):
            pass

        if price and price > 0:
            return {"price": round(price, 2), "change_pct": round(change_pct, 2)}
    except Exception as e:
        logger.debug(f"Finviz quote parse failed for {ticker}: {e}")
    return None


def stockanalysis_quote_batch(symbols: list, max_workers: int = 8, timeout: int = 18) -> dict:
    """
    Batch price quotes from stockanalysis.com using concurrent threads.

    Returns: {symbol: {"price": float, "change_pct": float}}
    Symbols with failed fetches are absent from the result.
    Never raises. Caps at 40 symbols per call to prevent abuse.
    """
    out = {}
    if not symbols:
        return out
    capped = [s.upper() for s in symbols[:40]]

    results = [None] * len(capped)

    def _fetch(idx, ticker):
        try:
            results[idx] = _fetch_stockanalysis_quote(ticker)
        except Exception:
            pass

    threads = []
    # Stagger launches slightly to avoid slamming the site
    for i, sym in enumerate(capped):
        t = threading.Thread(target=_fetch, args=(i, sym), daemon=True)
        threads.append(t)
        t.start()
        if (i + 1) % max_workers == 0:
            time.sleep(0.1)  # micro-pause every batch to be polite

    for t in threads:
        t.join(timeout=timeout)

    for i, sym in enumerate(capped):
        r = results[i]
        if r and r.get("price") and float(r["price"]) > 0:
            out[sym] = r

    if out:
        logger.info(f"StockAnalysis batch: {len(out)}/{len(capped)} symbols resolved")
    return out


def finviz_quote_batch(symbols: list, max_workers: int = 6, timeout: int = 20) -> dict:
    """
    Batch price quotes from finviz.com using concurrent threads.

    Returns: {symbol: {"price": float, "change_pct": float}}
    Symbols with failed fetches are absent from the result.
    Never raises. Caps at 30 symbols per call.
    Finviz is slightly more aggressive with bot detection so we use a lower
    concurrency limit (6) and a longer per-batch pause (0.2s).
    """
    out = {}
    if not symbols:
        return out
    capped = [s.upper() for s in symbols[:30]]

    results = [None] * len(capped)

    def _fetch(idx, ticker):
        try:
            results[idx] = _fetch_finviz_quote(ticker)
        except Exception:
            pass

    threads = []
    for i, sym in enumerate(capped):
        t = threading.Thread(target=_fetch, args=(i, sym), daemon=True)
        threads.append(t)
        t.start()
        if (i + 1) % max_workers == 0:
            time.sleep(0.2)

    for t in threads:
        t.join(timeout=timeout)

    for i, sym in enumerate(capped):
        r = results[i]
        if r and r.get("price") and float(r["price"]) > 0:
            out[sym] = r

    if out:
        logger.info(f"Finviz batch: {len(out)}/{len(capped)} symbols resolved")
    return out


def multi_source_quote_batch(symbols: list) -> dict:
    """
    Combined batch quote: stockanalysis.com first, finviz for any still-missing.

    Returns: {symbol: {"price": float, "change_pct": float}}
    Drop-in replacement for cnbc_quote_batch / stooq_quote_batch.
    Never raises.
    """
    if not symbols:
        return {}
    out = {}
    try:
        sa_data = stockanalysis_quote_batch(symbols)
        out.update(sa_data)
    except Exception as e:
        logger.debug(f"multi_source_quote_batch stockanalysis failed: {e}")

    still_missing = [s for s in symbols if s.upper() not in out]
    if still_missing:
        try:
            fv_data = finviz_quote_batch(still_missing)
            out.update(fv_data)
        except Exception as e:
            logger.debug(f"multi_source_quote_batch finviz failed: {e}")

    return out


# ============================================================
#  STATUS CHECK  (used by data_shield.get_shield_status)
# ============================================================

def get_multi_source_status() -> dict:
    """
    Quick health check for all multi-source providers.
    Tests price fetch for SPY as a canary. Returns status dict.
    """
    out = {}

    # stockanalysis — test with SPY
    t0 = time.time()
    sa_price = get_stockanalysis_price("SPY")
    out["stockanalysis"] = {
        "ok": bool(sa_price and sa_price > 0),
        "latency_s": round(time.time() - t0, 2),
        "status": "HEALTHY" if (sa_price and sa_price > 0) else "DOWN",
        "key_required": False,
    }

    # finviz — test with SPY
    t0 = time.time()
    fv_price = get_finviz_price("SPY")
    out["finviz"] = {
        "ok": bool(fv_price and fv_price > 0),
        "latency_s": round(time.time() - t0, 2),
        "status": "HEALTHY" if (fv_price and fv_price > 0) else "DOWN",
        "key_required": False,
    }

    # Alpha Vantage — just check key presence (don't burn daily quota on health check)
    out["alphavantage"] = {
        "ok": bool(_AV_KEY()),
        "status": "CONFIGURED" if _AV_KEY() else "NO_KEY",
        "key_required": True,
        "free_quota": "25/day",
    }

    # Tiingo
    out["tiingo"] = {
        "ok": bool(_TIINGO_KEY()),
        "status": "CONFIGURED" if _TIINGO_KEY() else "NO_KEY",
        "key_required": True,
        "free_quota": "500/day",
    }

    # FMP
    out["fmp"] = {
        "ok": bool(_FMP_KEY()),
        "status": "CONFIGURED" if _FMP_KEY() else "NO_KEY",
        "key_required": True,
        "free_quota": "250/day",
    }

    return out
