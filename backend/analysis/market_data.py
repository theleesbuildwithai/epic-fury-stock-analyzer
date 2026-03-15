"""
Fetches real stock data from Yahoo Finance.
Think of this as the "data collector" — it goes out to Yahoo Finance
and brings back real prices, volumes, and company info.
"""

import yfinance as yf
from datetime import datetime, timedelta
from functools import lru_cache
import time

# Cache to avoid hitting Yahoo Finance too often (5 min cache)
_cache = {}
_cache_ttl = 300  # 5 minutes


def _get_cached(key, fetch_fn):
    """Simple cache that expires after 5 minutes."""
    now = time.time()
    if key in _cache and now - _cache[key]["time"] < _cache_ttl:
        return _cache[key]["data"]
    data = fetch_fn()
    _cache[key] = {"data": data, "time": now}
    return data


def get_stock_info(ticker: str) -> dict:
    """Get basic info about a stock (name, price, market cap, etc.)."""
    def fetch():
        stock = yf.Ticker(ticker)
        # Try to get info, retry once if rate-limited
        for attempt in range(2):
            try:
                info = stock.info
                if info:
                    break
            except Exception:
                if attempt == 0:
                    time.sleep(2)
                    continue
                # Fall back to using history data for basic info
                hist = stock.history(period="5d")
                if hist.empty:
                    raise
                last = hist.iloc[-1]
                info = {
                    "shortName": ticker.upper(),
                    "regularMarketPrice": round(last["Close"], 2),
                    "previousClose": round(hist.iloc[-2]["Close"], 2) if len(hist) > 1 else 0,
                    "regularMarketOpen": round(last["Open"], 2),
                    "regularMarketDayHigh": round(last["High"], 2),
                    "regularMarketDayLow": round(last["Low"], 2),
                    "regularMarketVolume": int(last["Volume"]),
                }
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
    """
    def fetch():
        stock = yf.Ticker(ticker)
        df = stock.history(period=period)
        if df.empty:
            return []
        records = []
        for date, row in df.iterrows():
            records.append({
                "date": date.strftime("%Y-%m-%d"),
                "open": round(row["Open"], 2),
                "high": round(row["High"], 2),
                "low": round(row["Low"], 2),
                "close": round(row["Close"], 2),
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
