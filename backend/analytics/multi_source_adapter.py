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
import gzip
import zlib
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
    # Only advertise encodings the stdlib can actually decode. Brotli ("br") is
    # NOT decodable without a third-party package, so requesting it silently
    # yielded unparseable compressed bytes (broke finviz/stockanalysis scrapes).
    "Accept-Encoding": "gzip, deflate",
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


def _decompress(raw: bytes, content_encoding: str) -> bytes:
    """Decompress a response body per its Content-Encoding. Falls back to the
    raw bytes if decoding fails (never raises)."""
    if not raw or not content_encoding:
        return raw
    enc = content_encoding.lower().strip()
    try:
        if enc == "gzip":
            return gzip.decompress(raw)
        if enc == "deflate":
            try:
                return zlib.decompress(raw)
            except zlib.error:
                # Raw deflate stream (no zlib header)
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception as e:
        logger.debug(f"MultiSource _decompress failed ({enc}): {e}")
    return raw


def _http_get(url: str, headers: dict = None, timeout: int = _HTTP_TIMEOUT) -> Optional[bytes]:
    """Simple urllib GET. Returns decoded (decompressed) bytes or None."""
    try:
        req = urllib.request.Request(url, headers=headers or _BROWSER_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return _decompress(raw, resp.headers.get("Content-Encoding", ""))
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
#  YAHOO FINANCE V8/V7 DIRECT  (no API key — bypasses yfinance library)
#  The yfinance library can fail due to version issues, curl_cffi errors,
#  or Python environment problems. Hitting Yahoo's raw JSON API with urllib
#  is a completely independent code path — if yfinance breaks, this still works.
# ============================================================

def _fetch_yahoo_v8_quote(ticker: str) -> Optional[dict]:
    """Hit Yahoo Finance v8 chart API directly, bypassing the yfinance library.
    Tries query1 then query2. No crumb required for chart range queries."""
    for host in ("query1", "query2"):
        try:
            url = (
                f"https://{host}.finance.yahoo.com/v8/finance/chart/"
                f"{urllib.parse.quote(ticker.upper())}"
                "?interval=1d&range=5d&includePrePost=false"
            )
            headers = {**_JSON_HEADERS, "Referer": "https://finance.yahoo.com/"}
            raw = _http_get(url, headers=headers, timeout=10)
            if not raw:
                continue
            data = json.loads(raw)
            result = (data.get("chart") or {}).get("result") or []
            if not result:
                continue
            meta = result[0].get("meta") or {}
            price = meta.get("regularMarketPrice")
            if price is None:
                price = meta.get("chartPreviousClose")
            if not price or float(price) <= 0:
                continue
            price = float(price)
            prev = float(meta.get("chartPreviousClose") or meta.get("previousClose") or 0)
            change_pct = round(((price - prev) / prev) * 100, 2) if prev > 0 else 0.0
            return {
                "price": round(price, 2),
                "change_pct": change_pct,
                "prev_close": round(prev, 2),
            }
        except Exception as e:
            logger.debug(f"Yahoo v8 direct ({host}) failed for {ticker}: {e}")
    return None


def yahoo_direct_quote(ticker: str) -> Optional[dict]:
    """Yahoo Finance v8 direct API (no yfinance library). Returns {price, change_pct} or None."""
    return _run_with_timeout(lambda: _fetch_yahoo_v8_quote(ticker), timeout=12)


def _fetch_yahoo_v7_batch(symbols: list) -> dict:
    """Yahoo Finance v7 quote API for batch. One HTTP call for all symbols.
    Returns {symbol: {price, change_pct}} dict. No crumb needed for basic fields."""
    try:
        sym_str = ",".join(s.upper() for s in symbols[:100])
        for host in ("query1", "query2"):
            url = (
                f"https://{host}.finance.yahoo.com/v7/finance/quote"
                f"?symbols={urllib.parse.quote(sym_str)}"
                "&fields=regularMarketPrice,regularMarketPreviousClose,regularMarketChangePercent"
                "&formatted=false"
            )
            headers = {**_JSON_HEADERS, "Referer": "https://finance.yahoo.com/"}
            raw = _http_get(url, headers=headers, timeout=12)
            if not raw:
                continue
            data = json.loads(raw)
            result = (data.get("quoteResponse") or {}).get("result") or []
            if not result:
                continue
            out = {}
            for q in result:
                sym = (q.get("symbol") or "").upper()
                price = float(q.get("regularMarketPrice") or 0)
                if sym and price > 0:
                    prev = float(q.get("regularMarketPreviousClose") or 0)
                    chg = float(q.get("regularMarketChangePercent") or 0)
                    out[sym] = {"price": round(price, 2), "change_pct": round(chg, 2)}
            if out:
                logger.info(f"Yahoo v7 batch ({host}): {len(out)}/{len(symbols)} resolved")
                return out
    except Exception as e:
        logger.debug(f"Yahoo v7 batch failed: {e}")
    return {}


def yahoo_direct_quote_batch(symbols: list, max_workers: int = 10, timeout: int = 20) -> dict:
    """
    Batch Yahoo direct quotes. Tries v7 batch (one HTTP call) first, then fills
    gaps with concurrent threaded v8 chart calls for anything v7 missed.
    Never raises. Returns {symbol: {price, change_pct}}.
    """
    if not symbols:
        return {}
    out = {}

    # v7 batch — most efficient (one HTTP call for all symbols)
    try:
        v7_result = _run_with_timeout(lambda: _fetch_yahoo_v7_batch(symbols), timeout=15)
        if v7_result:
            out.update(v7_result)
    except Exception:
        pass

    # v8 threaded fallback for anything v7 missed
    still_missing = [s for s in symbols if s.upper() not in out]
    if still_missing:
        capped = [s.upper() for s in still_missing[:60]]
        results = [None] * len(capped)

        def _fetch_v8(idx, ticker):
            try:
                results[idx] = _fetch_yahoo_v8_quote(ticker)
            except Exception:
                pass

        threads = []
        for i, sym in enumerate(capped):
            t = threading.Thread(target=_fetch_v8, args=(i, sym), daemon=True)
            threads.append(t)
            t.start()
            if (i + 1) % max_workers == 0:
                time.sleep(0.05)
        for t in threads:
            t.join(timeout=timeout)
        for i, sym in enumerate(capped):
            r = results[i]
            if r and r.get("price") and float(r["price"]) > 0:
                out[sym] = r

    if out:
        logger.info(f"Yahoo direct batch: {len(out)}/{len(symbols)} symbols resolved")
    return out


# ============================================================
#  TWELVE DATA  (free key: TWELVE_DATA_KEY env var, 800 credits/day)
#  Batch-capable: one call for up to 120 symbols (highly efficient).
# ============================================================

_TWELVE_DATA_KEY = lambda: os.environ.get("TWELVE_DATA_KEY", "").strip()


def _fetch_twelvedata_batch(symbols: list) -> dict:
    """Batch price fetch from Twelve Data. One API call for all symbols (max 120)."""
    key = _TWELVE_DATA_KEY()
    if not key or not symbols:
        return {}
    try:
        sym_str = ",".join(s.upper() for s in symbols[:120])
        url = (
            f"https://api.twelvedata.com/price"
            f"?symbol={urllib.parse.quote(sym_str)}&apikey={key}"
        )
        data = _http_get_json(url)
        if not data:
            return {}
        out = {}
        if len(symbols) == 1:
            sym = symbols[0].upper()
            if data.get("status") != "error":
                price = float(data.get("price") or 0)
                if price > 0:
                    out[sym] = {"price": round(price, 2), "change_pct": 0.0}
        else:
            for sym_key, val in data.items():
                if isinstance(val, dict) and val.get("price"):
                    try:
                        price = float(val["price"])
                        if price > 0:
                            out[sym_key.upper()] = {"price": round(price, 2), "change_pct": 0.0}
                    except (TypeError, ValueError):
                        pass
        if out:
            logger.info(f"Twelve Data batch: {len(out)}/{len(symbols)} symbols resolved")
        return out
    except Exception as e:
        logger.debug(f"Twelve Data batch failed: {e}")
    return {}


def twelvedata_quote_batch(symbols: list) -> dict:
    """Batch quotes from Twelve Data. Key-gated (800/day). Never raises."""
    if not _TWELVE_DATA_KEY():
        return {}
    return _run_with_timeout(lambda: _fetch_twelvedata_batch(symbols), timeout=15)


def _fetch_twelvedata_quote(ticker: str) -> Optional[dict]:
    """Single quote from Twelve Data."""
    result = _fetch_twelvedata_batch([ticker])
    return result.get(ticker.upper())


def _fetch_twelvedata_historical(ticker: str, period: str = "6mo") -> Optional[pd.DataFrame]:
    """Historical OHLCV from Twelve Data (consumes ~30 credits per call)."""
    key = _TWELVE_DATA_KEY()
    if not key:
        return None
    try:
        days = _PERIOD_DAYS.get(period, 182)
        outputsize = min(days, 5000)
        url = (
            f"https://api.twelvedata.com/time_series"
            f"?symbol={urllib.parse.quote(ticker.upper())}"
            f"&interval=1day&outputsize={outputsize}&apikey={key}"
        )
        data = _http_get_json(url)
        if not data or data.get("status") == "error":
            return None
        values = data.get("values", [])
        if len(values) < 5:
            return None
        records = []
        for row in values:
            try:
                records.append({
                    "Date": pd.to_datetime(row["datetime"]),
                    "Open": float(row.get("open") or 0),
                    "High": float(row.get("high") or 0),
                    "Low": float(row.get("low") or 0),
                    "Close": float(row.get("close") or 0),
                    "Volume": int(float(row.get("volume") or 0)),
                })
            except (KeyError, ValueError, TypeError):
                continue
        if len(records) < 5:
            return None
        df = pd.DataFrame(records).set_index("Date").sort_index()
        logger.info(f"Twelve Data historical: {ticker} ({len(df)} rows)")
        return df
    except Exception as e:
        logger.debug(f"Twelve Data historical failed for {ticker}: {e}")
    return None


def get_twelvedata_historical(ticker: str, period: str = "6mo") -> Optional[pd.DataFrame]:
    """Historical data from Twelve Data. Returns None if key not set or call fails."""
    if not _TWELVE_DATA_KEY():
        return None
    return _run_with_timeout(lambda: _fetch_twelvedata_historical(ticker, period), timeout=16)


# ============================================================
#  POLYGON.IO  (free key: POLYGON_KEY env var — delayed data, no daily cap)
#  Batch snapshot: one call for up to 50 tickers.
# ============================================================

_POLYGON_KEY = lambda: os.environ.get("POLYGON_KEY", "").strip()


def _fetch_polygon_batch(symbols: list) -> dict:
    """Batch snapshot from Polygon.io. Returns {symbol: {price, change_pct}}."""
    key = _POLYGON_KEY()
    if not key or not symbols:
        return {}
    try:
        tickers_str = ",".join(s.upper() for s in symbols[:50])
        url = (
            "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers"
            f"?tickers={urllib.parse.quote(tickers_str)}&apiKey={key}"
        )
        data = _http_get_json(url)
        if not data or not data.get("tickers"):
            return {}
        out = {}
        for item in data["tickers"]:
            sym = (item.get("ticker") or "").upper()
            if not sym:
                continue
            day = item.get("day") or {}
            price = float(day.get("c") or 0)
            if price <= 0:
                last = item.get("lastTrade") or {}
                price = float(last.get("p") or 0)
            if price <= 0:
                continue
            prev_day = item.get("prevDay") or {}
            prev_close = float(prev_day.get("c") or 0)
            change_pct = round(((price - prev_close) / prev_close) * 100, 2) if prev_close > 0 else 0.0
            out[sym] = {"price": round(price, 2), "change_pct": change_pct}
        if out:
            logger.info(f"Polygon batch: {len(out)}/{len(symbols)} symbols resolved")
        return out
    except Exception as e:
        logger.debug(f"Polygon batch failed: {e}")
    return {}


def polygon_quote_batch(symbols: list) -> dict:
    """Batch snapshots from Polygon.io. Key-gated (delayed data). Never raises."""
    if not _POLYGON_KEY():
        return {}
    return _run_with_timeout(lambda: _fetch_polygon_batch(symbols), timeout=15)


def _fetch_polygon_quote(ticker: str) -> Optional[dict]:
    """Single quote from Polygon.io."""
    result = _fetch_polygon_batch([ticker])
    return result.get(ticker.upper())


def _fetch_polygon_historical(ticker: str, period: str = "6mo") -> Optional[pd.DataFrame]:
    """Historical OHLCV from Polygon.io aggs endpoint (adjusted, delayed)."""
    key = _POLYGON_KEY()
    if not key:
        return None
    try:
        days = _PERIOD_DAYS.get(period, 182)
        end = datetime.now()
        start = end - timedelta(days=days)
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/{urllib.parse.quote(ticker.upper())}/range/1/day"
            f"/{start.strftime('%Y-%m-%d')}/{end.strftime('%Y-%m-%d')}"
            f"?adjusted=true&sort=asc&limit=5000&apiKey={key}"
        )
        data = _http_get_json(url)
        if not data or data.get("status") == "ERROR" or not data.get("results"):
            return None
        results = data["results"]
        if len(results) < 5:
            return None
        records = []
        for bar in results:
            try:
                records.append({
                    "Date": pd.to_datetime(bar["t"], unit="ms"),
                    "Open": float(bar.get("o") or 0),
                    "High": float(bar.get("h") or 0),
                    "Low": float(bar.get("l") or 0),
                    "Close": float(bar.get("c") or 0),
                    "Volume": int(bar.get("v") or 0),
                })
            except (KeyError, ValueError, TypeError):
                continue
        if len(records) < 5:
            return None
        df = pd.DataFrame(records).set_index("Date").sort_index()
        logger.info(f"Polygon historical: {ticker} ({len(df)} rows)")
        return df
    except Exception as e:
        logger.debug(f"Polygon historical failed for {ticker}: {e}")
    return None


def get_polygon_historical(ticker: str, period: str = "6mo") -> Optional[pd.DataFrame]:
    """Historical data from Polygon.io. Returns None if key not set or call fails."""
    if not _POLYGON_KEY():
        return None
    return _run_with_timeout(lambda: _fetch_polygon_historical(ticker, period), timeout=16)


# ============================================================
#  COMBINED RESOLVERS — used by data_shield + market_data
# ============================================================

def get_price_any_source(ticker: str) -> Optional[float]:
    """
    Try every source for a current price. Returns first valid price or None.

    Chain: Yahoo direct → stockanalysis → finviz → Twelve Data → Polygon → FMP → price cache
    """
    # 0. Yahoo Finance v8/v7 direct (no key — independent of yfinance library)
    try:
        result = yahoo_direct_quote(ticker)
        if result and result.get("price") and float(result["price"]) > 0:
            price = float(result["price"])
            logger.info(f"MultiSource price for {ticker}: yahoo_direct={price}")
            return price
    except Exception:
        pass

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

    # 3. Twelve Data (optional key, 800/day)
    try:
        if _TWELVE_DATA_KEY():
            result = _run_with_timeout(lambda: _fetch_twelvedata_quote(ticker), timeout=12)
            if result and result.get("price") and float(result["price"]) > 0:
                price = float(result["price"])
                logger.info(f"MultiSource price for {ticker}: twelvedata={price}")
                return price
    except Exception:
        pass

    # 4. Polygon (optional key, delayed data)
    try:
        if _POLYGON_KEY():
            result = _run_with_timeout(lambda: _fetch_polygon_quote(ticker), timeout=12)
            if result and result.get("price") and float(result["price"]) > 0:
                price = float(result["price"])
                logger.info(f"MultiSource price for {ticker}: polygon={price}")
                return price
    except Exception:
        pass

    # 5. FMP (optional key, 250/day)
    try:
        fmp = get_fmp_quote(ticker)
        if fmp and fmp.get("currentPrice") and float(fmp["currentPrice"]) > 0:
            price = float(fmp["currentPrice"])
            logger.info(f"MultiSource price for {ticker}: fmp={price}")
            return price
    except Exception:
        pass

    # 6. Persistent price cache — last resort, survives full outages and deploys
    try:
        from analytics.price_cache import get_cached_price
        cached = get_cached_price(ticker)
        if cached and cached.get("price") and float(cached["price"]) > 0:
            age = time.time() - cached.get("ts", 0)
            logger.warning(f"MultiSource price for {ticker}: price_cache (stale {age:.0f}s old)")
            return float(cached["price"])
    except Exception:
        pass

    return None


def get_fundamentals_any_source(ticker: str) -> Optional[dict]:
    """
    Try every source for fundamentals (price + metadata).
    Returns first successful dict with currentPrice > 0, or None.

    Chain: stockanalysis → finviz → fmp → Twelve Data (price only) → Polygon (price only) → price cache
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

    # 3. FMP (optional key, 250/day)
    try:
        info = get_fmp_quote(ticker)
        if info and info.get("currentPrice") and float(info["currentPrice"]) > 0:
            return info
    except Exception:
        pass

    # 4. Twelve Data price — minimal dict so callers still get a valid price
    try:
        if _TWELVE_DATA_KEY():
            result = _run_with_timeout(lambda: _fetch_twelvedata_quote(ticker), timeout=12)
            if result and result.get("price") and float(result["price"]) > 0:
                return {
                    "currentPrice": float(result["price"]),
                    "shortName": ticker.upper(),
                    "_source": "twelvedata",
                }
    except Exception:
        pass

    # 5. Polygon price (optional key, delayed)
    try:
        if _POLYGON_KEY():
            result = _run_with_timeout(lambda: _fetch_polygon_quote(ticker), timeout=12)
            if result and result.get("price") and float(result["price"]) > 0:
                return {
                    "currentPrice": float(result["price"]),
                    "shortName": ticker.upper(),
                    "_source": "polygon",
                }
    except Exception:
        pass

    # 6. Yahoo direct (no key) — price only
    try:
        result = yahoo_direct_quote(ticker)
        if result and result.get("price") and float(result["price"]) > 0:
            return {
                "currentPrice": float(result["price"]),
                "shortName": ticker.upper(),
                "_source": "yahoo_direct",
            }
    except Exception:
        pass

    # 7. Persistent price cache — absolute last resort
    try:
        from analytics.price_cache import get_cached_price
        cached = get_cached_price(ticker)
        if cached and cached.get("price") and float(cached["price"]) > 0:
            return {
                "currentPrice": float(cached["price"]),
                "shortName": ticker.upper(),
                "_source": f"price_cache({cached.get('source','?')})",
            }
    except Exception:
        pass

    return None


def _last_close_value(df) -> Optional[float]:
    """Last close of an OHLCV frame, defensive against MultiIndex columns.
    Returns None if it cannot be read as a positive number."""
    try:
        if df is None or getattr(df, "empty", True):
            return None
        d = df
        if isinstance(d.columns, pd.MultiIndex):
            d = d.copy()
            d.columns = d.columns.get_level_values(0)
        if "Close" not in d.columns:
            return None
        v = float(pd.Series(d["Close"]).dropna().values[-1])
        return v if (v > 0 and v == v and v != float("inf")) else None
    except Exception:
        return None


def validate_ohlcv(df, trusted_px: Optional[float] = None,
                   ticker: str = "", tol: float = 0.30) -> bool:
    """
    SOURCE-BOUNDARY DATA VALIDATOR (2026-08-07).

    A free historical source occasionally returns a grossly wrong series for a
    ticker (e.g. V at $3.30 vs a live $362.50, or a split-glitched / frozen
    frame). Such a series silently corrupts every downstream indicator. This
    validator is the single gate every historical frame must pass before it is
    trusted. It is deliberately CONSERVATIVE: it only rejects data that is
    unambiguously wrong, so it can never reject a legitimate price series.

    Rejects when the frame:
      - is missing / empty / has < 5 rows / has no Close column;
      - contains a non-positive, NaN, or infinite close;
      - is frozen (every close identical);
      - has a last close that deviates > 60% from the trailing-20 median
        (catches corruption / bad split ticks; legitimate 20-bar moves are
        far smaller than this);
      - (only when trusted_px is supplied) has a last close more than `tol`
        away from the live/trusted quote.
    """
    try:
        if df is None or getattr(df, "empty", True):
            return False
        d = df
        if isinstance(d.columns, pd.MultiIndex):
            d = d.copy()
            d.columns = d.columns.get_level_values(0)
        if "Close" not in d.columns:
            return False
        closes = pd.Series(d["Close"]).dropna()
        if len(closes) < 5:
            return False
        vals = closes.astype(float).values
        # non-positive / NaN / inf
        if not all(v > 0 and v == v and v != float("inf") and v != float("-inf")
                   for v in vals):
            return False
        # frozen series (no variation at all is not a real market)
        if float(vals.max()) == float(vals.min()):
            return False
        last = float(vals[-1])
        # last close as a wild outlier vs the recent trailing median
        tail = vals[-20:] if len(vals) >= 20 else vals
        import numpy as _np
        med = float(_np.median(tail))
        if med > 0 and abs(last - med) / med > 0.60:
            logger.debug(
                f"validate_ohlcv reject {ticker}: last {last:.4f} vs trailing "
                f"median {med:.4f} (>60% off — likely corruption)")
            return False
        # agreement with the trusted live quote, when we have one
        if trusted_px is not None and float(trusted_px) > 0:
            if abs(last - float(trusted_px)) / float(trusted_px) > tol:
                logger.debug(
                    f"validate_ohlcv reject {ticker}: last {last:.4f} vs trusted "
                    f"quote {float(trusted_px):.4f} (> {int(tol*100)}% off)")
                return False
        return True
    except Exception:
        return False


def get_historical_any_source(ticker: str, period: str = "6mo",
                              trusted_px: Optional[float] = None) -> Optional[pd.DataFrame]:
    """
    Try every source for historical OHLCV data and return the first frame that
    passes validate_ohlcv (structural sanity + agreement with trusted_px when
    supplied). Returns None if nothing validates.

    Chain: tiingo → alpha_vantage → fmp → twelve_data → polygon
    All except Twelve Data and Polygon require optional env-var API keys.

    trusted_px (optional): a live/trusted quote. When given, ONLY a frame whose
    last close agrees with it (within tolerance) is returned — a source that
    disagrees is skipped so the next source gets a chance, and if none agree we
    return None (the caller's safety nets then withhold rather than act on a
    corrupt series). When NOT given (quote feed also down), the first
    structurally-sane frame is returned as a best effort.
    """
    _sources = (
        ("tiingo", get_tiingo_historical),
        ("alphavantage", get_alphavantage_historical),
        ("fmp", get_fmp_historical),
        ("twelvedata", get_twelvedata_historical),
        ("polygon", get_polygon_historical),
    )

    # Pass 1: return the first frame that FULLY validates.
    _structural_fallback = None
    for _name, _fn in _sources:
        try:
            df = _fn(ticker, period)
        except Exception:
            continue
        if df is None or len(df) < 5:
            continue
        if validate_ohlcv(df, trusted_px=trusted_px, ticker=ticker):
            logger.info(
                f"MultiSource historical for {ticker}: {_name} "
                f"({len(df)} rows, validated)")
            return df
        # Remember a structurally-sane frame (ignoring the trusted-price check)
        # only as a last resort for when NO trusted quote is available.
        if _structural_fallback is None and validate_ohlcv(df, trusted_px=None,
                                                           ticker=ticker):
            _structural_fallback = (_name, df)

    # Pass 2: no source agreed with the trusted quote.
    #   - If we had a trusted quote, every frame disagreed with it → the data is
    #     unreliable; return None so the caller withholds (never act on it).
    #   - If we had NO trusted quote, fall back to the best structurally-sane
    #     frame (preserves prior best-effort behaviour, minus obvious corruption).
    if trusted_px is None and _structural_fallback is not None:
        _name, df = _structural_fallback
        logger.info(
            f"MultiSource historical for {ticker}: {_name} "
            f"({len(df)} rows, structural-only, no trusted quote)")
        return df

    if trusted_px is not None:
        logger.warning(
            f"MultiSource historical for {ticker}: no source agreed with the "
            f"trusted quote {float(trusted_px):.2f} — withholding data")
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
    Combined batch quote across all sources. 9-layer safety net.

    Layers (in order):
      1. Yahoo direct v7 batch (one HTTP call for all) → v8 threaded fallback
      2. stockanalysis.com concurrent threads (no key)
      3. finviz.com concurrent threads (no key)
      4. Twelve Data batch (one HTTP call, key-gated, 800/day)
      5. Polygon batch snapshot (one HTTP call, key-gated, delayed)
      6. FMP individual quotes (key-gated, 250/day)
      7. Persistent price cache (last resort — survives full outages)

    Returns: {symbol: {"price": float, "change_pct": float}}
    Drop-in replacement for cnbc_quote_batch / stooq_quote_batch.
    Never raises.
    """
    if not symbols:
        return {}
    syms_upper = [s.upper() for s in symbols]
    out = {}

    # 1. Yahoo Finance direct (v7 batch → threaded v8 fallback)
    try:
        yh_data = yahoo_direct_quote_batch(syms_upper)
        out.update(yh_data)
    except Exception as e:
        logger.debug(f"multi_source_quote_batch yahoo_direct failed: {e}")

    # 2. stockanalysis.com for any still-missing
    still_missing = [s for s in syms_upper if s not in out]
    if still_missing:
        try:
            sa_data = stockanalysis_quote_batch(still_missing)
            out.update(sa_data)
        except Exception as e:
            logger.debug(f"multi_source_quote_batch stockanalysis failed: {e}")

    # 3. finviz.com for any still-missing
    still_missing = [s for s in syms_upper if s not in out]
    if still_missing:
        try:
            fv_data = finviz_quote_batch(still_missing)
            out.update(fv_data)
        except Exception as e:
            logger.debug(f"multi_source_quote_batch finviz failed: {e}")

    # 4. Twelve Data batch (one call, key-gated)
    still_missing = [s for s in syms_upper if s not in out]
    if still_missing and _TWELVE_DATA_KEY():
        try:
            td_data = twelvedata_quote_batch(still_missing)
            out.update(td_data)
        except Exception as e:
            logger.debug(f"multi_source_quote_batch twelvedata failed: {e}")

    # 5. Polygon batch snapshot (one call, key-gated, delayed data)
    still_missing = [s for s in syms_upper if s not in out]
    if still_missing and _POLYGON_KEY():
        try:
            pg_data = polygon_quote_batch(still_missing)
            out.update(pg_data)
        except Exception as e:
            logger.debug(f"multi_source_quote_batch polygon failed: {e}")

    # 6. FMP individual quotes (key-gated, 250/day)
    still_missing = [s for s in syms_upper if s not in out]
    if still_missing and _FMP_KEY():
        for sym in still_missing[:20]:
            try:
                fmp = _run_with_timeout(lambda t=sym: _fetch_fmp_quote(t), timeout=10)
                if fmp and fmp.get("currentPrice") and float(fmp["currentPrice"]) > 0:
                    out[sym] = {"price": round(float(fmp["currentPrice"]), 2), "change_pct": 0.0}
            except Exception:
                pass

    # 7. Persistent price cache — absolute last resort for any still-missing
    still_missing = [s for s in syms_upper if s not in out]
    if still_missing:
        try:
            from analytics.price_cache import get_cached_price
            for sym in still_missing:
                cached = get_cached_price(sym)
                if cached and cached.get("price") and float(cached["price"]) > 0:
                    out[sym] = {
                        "price": float(cached["price"]),
                        "change_pct": 0.0,
                        "_stale": True,
                    }
        except Exception:
            pass

    # Persist all fresh (non-stale) results to price cache for future outages
    if out:
        try:
            from analytics.price_cache import update_price_cache
            fresh = {
                sym: val["price"] for sym, val in out.items()
                if not val.get("_stale") and val.get("price")
            }
            if fresh:
                update_price_cache(fresh, source="multi_source")
        except Exception:
            pass

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

    # Yahoo Finance direct v8 (no key — bypasses yfinance library)
    t0 = time.time()
    yh_result = _run_with_timeout(lambda: _fetch_yahoo_v8_quote("SPY"), timeout=12)
    yh_ok = bool(yh_result and yh_result.get("price") and float(yh_result["price"]) > 0)
    out["yahoo_direct"] = {
        "ok": yh_ok,
        "latency_s": round(time.time() - t0, 2),
        "status": "HEALTHY" if yh_ok else "DOWN",
        "key_required": False,
        "note": "Direct Yahoo v8/v7 API, independent of yfinance library",
    }

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

    # Twelve Data — just check key presence (don't burn daily quota on health check)
    out["twelvedata"] = {
        "ok": bool(_TWELVE_DATA_KEY()),
        "status": "CONFIGURED" if _TWELVE_DATA_KEY() else "NO_KEY",
        "key_required": True,
        "free_quota": "800 credits/day",
        "note": "Batch-capable: 1 call for up to 120 symbols",
    }

    # Polygon.io
    out["polygon"] = {
        "ok": bool(_POLYGON_KEY()),
        "status": "CONFIGURED" if _POLYGON_KEY() else "NO_KEY",
        "key_required": True,
        "free_quota": "unlimited (delayed data)",
        "note": "Batch snapshot: 1 call for up to 50 symbols",
    }

    # Alpha Vantage
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

    # Persistent price cache
    try:
        from analytics.price_cache import get_cache_status
        cache_status = get_cache_status()
        out["price_cache"] = {
            "ok": cache_status.get("total_tickers", 0) > 0,
            "status": "ACTIVE" if cache_status.get("total_tickers", 0) > 0 else "EMPTY",
            "key_required": False,
            **cache_status,
        }
    except Exception:
        out["price_cache"] = {"ok": False, "status": "ERROR"}

    return out
