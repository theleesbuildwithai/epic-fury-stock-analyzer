"""
Fetches real stock data from Yahoo Finance.
Think of this as the "data collector" — it goes out to Yahoo Finance
and brings back real prices, volumes, and company info.

Uses yf.download() for historical data (bulk method, less likely to trigger
rate limits) and includes robust fallback logic for stock info.

NOTE: Modern yfinance (0.2.37+) manages its own curl_cffi session internally.
Do NOT pass a custom requests.Session — it will cause errors.
"""

import yfinance as yf
from datetime import datetime, timedelta
import time

# Cache to avoid hitting Yahoo Finance too often (10 min cache)
_cache = {}
_cache_ttl = 600  # 10 minutes

# Timestamp of last API call, used to enforce spacing between requests
_last_api_call = 0.0
_API_CALL_DELAY = 3.0  # seconds between different API calls (slower = safer)


def _throttle():
    """Ensure at least _API_CALL_DELAY seconds between Yahoo Finance calls."""
    global _last_api_call
    now = time.time()
    elapsed = now - _last_api_call
    if elapsed < _API_CALL_DELAY:
        time.sleep(_API_CALL_DELAY - elapsed)
    _last_api_call = time.time()


def _get_cached(key, fetch_fn):
    """Simple cache that expires after _cache_ttl seconds."""
    now = time.time()
    if key in _cache and now - _cache[key]["time"] < _cache_ttl:
        return _cache[key]["data"]
    data = fetch_fn()
    _cache[key] = {"data": data, "time": now}
    return data


def _download_recent(ticker: str, period: str = "5d"):
    """
    Use yf.download() to grab recent data for a single ticker.
    Returns a pandas DataFrame or None on failure.
    """
    _throttle()
    try:
        df = yf.download(
            ticker,
            period=period,
            progress=False,
        )
        return df
    except Exception:
        return None


def get_stock_info(ticker: str) -> dict:
    """Get basic info about a stock (name, price, market cap, etc.)."""

    def fetch():
        stock = yf.Ticker(ticker)
        info = None

        # Attempt 1: try stock.info with retry
        for attempt in range(2):
            try:
                _throttle()
                raw = stock.info
                if raw and (raw.get("regularMarketPrice") or raw.get("currentPrice")):
                    info = raw
                    break
            except Exception:
                if attempt == 0:
                    time.sleep(2)
                    continue

        # Attempt 2: fall back to yf.download() for basic price data
        if info is None:
            df = _download_recent(ticker, period="5d")
            if df is not None and not df.empty:
                last = df.iloc[-1]
                prev_close = round(float(df.iloc[-2]["Close"]), 2) if len(df) > 1 else 0
                info = {
                    "shortName": ticker.upper(),
                    "regularMarketPrice": round(float(last["Close"]), 2),
                    "previousClose": prev_close,
                    "regularMarketOpen": round(float(last["Open"]), 2),
                    "regularMarketDayHigh": round(float(last["High"]), 2),
                    "regularMarketDayLow": round(float(last["Low"]), 2),
                    "regularMarketVolume": int(last["Volume"]),
                }
            else:
                # Nothing worked — return an empty shell so the caller
                # doesn't crash.
                info = {"shortName": ticker.upper()}

        return {
            "ticker": ticker.upper(),
            "name": info.get("longName", info.get("shortName", ticker)),
            "sector": info.get("sector", "N/A"),
            "industry": info.get("industry", "N/A"),
            "market_cap": info.get("marketCap", 0),
            "current_price": info.get("currentPrice", info.get("regularMarketPrice", 0)),
            "previous_close": info.get("previousClose", 0),
            "open": info.get("open", info.get("regularMarketOpen", 0)),
            "day_high": info.get("dayHigh", info.get("regularMarketDayHigh", 0)),
            "day_low": info.get("dayLow", info.get("regularMarketDayLow", 0)),
            "volume": info.get("volume", info.get("regularMarketVolume", 0)),
            "avg_volume": info.get("averageVolume", 0),
            "fifty_two_week_high": info.get("fiftyTwoWeekHigh", 0),
            "fifty_two_week_low": info.get("fiftyTwoWeekLow", 0),
            "pe_ratio": info.get("trailingPE", 0),
            "dividend_yield": info.get("dividendYield", 0),
            "beta": info.get("beta", 0),
            "currency": info.get("currency", "USD"),
        }

    return _get_cached(f"info_{ticker}", fetch)


def get_historical_data(ticker: str, period: str = "1y") -> list:
    """
    Get historical price data for a stock.
    period options: 1mo, 3mo, 6mo, 1y, 2y, 5y
    Returns a list of dicts with date, open, high, low, close, volume.

    Uses yf.download() which is the bulk-download method and is less
    likely to trigger rate-limit (429) errors than Ticker.history().
    """

    def fetch():
        _throttle()
        try:
            df = yf.download(
                ticker,
                period=period,
                progress=False,
            )
        except Exception:
            return []

        if df is None or df.empty:
            return []

        records = []
        for date, row in df.iterrows():
            records.append({
                "date": date.strftime("%Y-%m-%d"),
                "open": round(float(row["Open"]), 2),
                "high": round(float(row["High"]), 2),
                "low": round(float(row["Low"]), 2),
                "close": round(float(row["Close"]), 2),
                "volume": int(row["Volume"]),
            })
        return records

    return _get_cached(f"history_{ticker}_{period}", fetch)


def get_benchmark_data(period: str = "1y") -> dict:
    """
    Get historical data for the 3 major indices so we can compare performance.
    ^GSPC = S&P 500, ^IXIC = Nasdaq, ^DJI = Dow Jones
    """
    benchmarks = {
        "sp500": "^GSPC",
        "nasdaq": "^IXIC",
        "dow_jones": "^DJI",
    }
    result = {}
    for name, symbol in benchmarks.items():
        data = get_historical_data(symbol, period)
        if data:
            start_price = data[0]["close"]
            end_price = data[-1]["close"]
            total_return = round(((end_price - start_price) / start_price) * 100, 2)
            result[name] = {
                "symbol": symbol,
                "start_price": start_price,
                "end_price": end_price,
                "total_return_pct": total_return,
                "data": data,
            }
    return result
