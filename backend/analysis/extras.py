"""
Extra Resources — provides data for the stock ticker banner,
daily top picks, and upcoming earnings calendar.

Uses yfinance carefully with throttling. Caches aggressively
to avoid hitting Yahoo Finance rate limits.
"""

import yfinance as yf
import time
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from scipy.stats import norm
import pytz

# Shared cache
_extras_cache = {}
def _market_ttl():
    """60s cache during market hours, 1hr otherwise (data doesn't change when closed)"""
    from datetime import datetime
    et = pytz.timezone("US/Eastern")
    now = datetime.now(et)
    t = now.hour * 60 + now.minute
    # Market hours: 9:30 AM (570 min) to 4:00 PM (960 min) Eastern
    return 60 if (570 <= t <= 960) else 3600

_extras_cache_ttl = 60  # Default, overridden by _market_ttl()
_last_api_call = [0.0]
_API_DELAY = 3.0


def _get_market_holidays(year):
    """Return set of known US stock market holidays for a given year."""
    from datetime import date
    holidays = set()
    # New Year's Day
    holidays.add(date(year, 1, 1))
    # MLK Day (3rd Monday of January)
    d = date(year, 1, 1)
    mondays = 0
    while mondays < 3:
        if d.weekday() == 0:
            mondays += 1
            if mondays == 3:
                holidays.add(d)
        d += timedelta(days=1)
    # Presidents' Day (3rd Monday of February)
    d = date(year, 2, 1)
    mondays = 0
    while mondays < 3:
        if d.weekday() == 0:
            mondays += 1
            if mondays == 3:
                holidays.add(d)
        d += timedelta(days=1)
    # Good Friday (2 days before Easter Sunday)
    # Easter calculation (anonymous Gregorian algorithm)
    a = year % 19
    b = year // 100
    c = year % 100
    d_val = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d_val - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    easter = date(year, month, day)
    good_friday = easter - timedelta(days=2)
    holidays.add(good_friday)
    # Memorial Day (last Monday of May)
    d = date(year, 5, 31)
    while d.weekday() != 0:
        d -= timedelta(days=1)
    holidays.add(d)
    # Juneteenth
    holidays.add(date(year, 6, 19))
    # Independence Day
    holidays.add(date(year, 7, 4))
    # Labor Day (1st Monday of September)
    d = date(year, 9, 1)
    while d.weekday() != 0:
        d += timedelta(days=1)
    holidays.add(d)
    # Thanksgiving (4th Thursday of November)
    d = date(year, 11, 1)
    thursdays = 0
    while thursdays < 4:
        if d.weekday() == 3:
            thursdays += 1
            if thursdays == 4:
                holidays.add(d)
        d += timedelta(days=1)
    # Christmas
    holidays.add(date(year, 12, 25))
    return holidays


def is_market_open():
    """Check if US stock market is currently open (Mon-Fri, 9:30 AM - 4:00 PM ET)."""
    et = pytz.timezone("US/Eastern")
    now = datetime.now(et)
    # Weekday check (0=Mon, 4=Fri)
    if now.weekday() > 4:
        return False
    t = now.hour * 60 + now.minute
    # Market hours: 9:30 AM (570) to 4:00 PM (960) ET
    return 570 <= t <= 960


def _throttle():
    now = time.time()
    elapsed = now - _last_api_call[0]
    if elapsed < _API_DELAY:
        time.sleep(_API_DELAY - elapsed)
    _last_api_call[0] = time.time()


def _get_cached(key, fetch_fn, ttl=None):
    if ttl is None:
        ttl = _market_ttl()
    now = time.time()
    if key in _extras_cache and now - _extras_cache[key]["time"] < ttl:
        return _extras_cache[key]["data"]
    data = fetch_fn()
    _extras_cache[key] = {"data": data, "time": now}
    return data


# --- Banner tickers ---

BANNER_SYMBOLS = [
    # Major Indexes
    ("^GSPC", "S&P 500"),
    ("^IXIC", "Nasdaq"),
    ("^DJI", "Dow Jones"),
    # Commodities & Bonds
    ("GC=F", "Gold"),
    ("CL=F", "Crude Oil"),
    ("^TNX", "10Y Treasury"),
    # Top stocks by market cap
    ("AAPL", "Apple"),
    ("MSFT", "Microsoft"),
    ("NVDA", "NVIDIA"),
    ("AMZN", "Amazon"),
    ("GOOGL", "Alphabet"),
    ("META", "Meta"),
    ("TSLA", "Tesla"),
    ("BRK-B", "Berkshire"),
    ("AVGO", "Broadcom"),
    ("JPM", "JPMorgan"),
    ("LLY", "Eli Lilly"),
    ("V", "Visa"),
    ("UNH", "UnitedHealth"),
    ("WMT", "Walmart"),
    ("XOM", "Exxon"),
    ("NFLX", "Netflix"),
    ("AMD", "AMD"),
    ("CRM", "Salesforce"),
    ("COST", "Costco"),
    ("BA", "Boeing"),
    ("DIS", "Disney"),
    ("COIN", "Coinbase"),
]


def get_banner_data():
    """Get current prices and daily changes for banner tickers.

    Source priority (per fetch):
      1. Yahoo Finance batch (primary — full data with prev close)
      2. CNBC for any tickers Yahoo dropped (safety net)

    Always returns the most recent trading day's data. CNBC fallback ensures
    no tickers are missing from the banner even when Yahoo rate-limits.
    """
    def fetch():
        symbols = [s[0] for s in BANNER_SYMBOLS]
        symbol_to_name = {s[0]: s[1] for s in BANNER_SYMBOLS}
        results_by_symbol = {}  # symbol -> entry dict
        as_of_date = None

        # 1) PRIMARY: Yahoo Finance batches of 10
        # (Yahoo gives us prev close + current — best data for change calc.)
        batch_size = 10
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i + batch_size]
            _throttle()
            try:
                df = yf.download(batch, period="5d", progress=False, group_by="ticker")
            except Exception:
                continue

            if df is None or df.empty:
                continue

            for symbol in batch:
                try:
                    if isinstance(df.columns, pd.MultiIndex):
                        if symbol in df.columns.get_level_values(0):
                            close_series = df[(symbol, "Close")].dropna()
                        else:
                            continue
                    else:
                        if "Close" in df.columns:
                            close_series = df["Close"].dropna()
                        else:
                            continue

                    if close_series is None or len(close_series) < 2:
                        continue

                    current = float(close_series.iloc[-1])
                    prev = float(close_series.iloc[-2])
                    change = current - prev
                    change_pct = (change / prev) * 100

                    if as_of_date is None:
                        try:
                            as_of_date = str(close_series.index[-1].date())
                        except Exception:
                            pass

                    results_by_symbol[symbol] = {
                        "symbol": symbol,
                        "name": symbol_to_name[symbol],
                        "price": round(current, 2),
                        "change": round(change, 2),
                        "change_pct": round(change_pct, 2),
                        "_source": "yahoo",
                    }
                except Exception:
                    continue

        # 2) SAFETY NET: For any symbol Yahoo dropped, try CNBC.
        # CNBC uses different symbol formats than Yahoo for indices/commodities/share-classes.
        # Translate Yahoo → CNBC, fetch, then translate back to canonical Yahoo symbols.
        YAHOO_TO_CNBC = {
            "^GSPC": ".SPX",
            "^IXIC": ".IXIC",
            "^DJI": ".DJI",
            "^TNX": "US10Y",
            "GC=F": "@GC.1",
            "CL=F": "@CL.1",
            "BRK-B": "BRK.B",
        }
        missing = [s for s in symbols if s not in results_by_symbol]
        if missing:
            cnbc_request = [YAHOO_TO_CNBC.get(s, s) for s in missing]
            cnbc_to_yahoo = {YAHOO_TO_CNBC.get(s, s): s for s in missing}
            try:
                cnbc_data = cnbc_quote_batch(cnbc_request)
                # Map CNBC-format keys back to Yahoo-format symbols
                cnbc_data = {cnbc_to_yahoo.get(k, k): v for k, v in cnbc_data.items()}
                for sym, val in cnbc_data.items():
                    # CNBC gives us price + change_pct directly. Compute "change"
                    # in dollars from those: change = price - (price / (1 + pct/100)).
                    try:
                        price = val["price"]
                        change_pct = val["change_pct"]
                        prev_price = price / (1.0 + (change_pct / 100.0)) if (1.0 + change_pct / 100.0) != 0 else price
                        change_dollars = price - prev_price
                    except Exception:
                        price = val.get("price", 0)
                        change_pct = val.get("change_pct", 0)
                        change_dollars = 0
                    results_by_symbol[sym] = {
                        "symbol": sym,
                        "name": symbol_to_name.get(sym, sym),
                        "price": round(price, 2),
                        "change": round(change_dollars, 2),
                        "change_pct": round(change_pct, 2),
                        "_source": "cnbc",
                    }
            except Exception:
                pass  # CNBC fallback failure is non-fatal — just leave gaps

        # Build final list in canonical order, drop the internal _source field
        # from output but keep it in a separate "sources" summary for debugging.
        results = []
        sources_used = {"yahoo": 0, "cnbc": 0}
        for sym, name in BANNER_SYMBOLS:
            if sym in results_by_symbol:
                entry = dict(results_by_symbol[sym])
                src = entry.pop("_source", "yahoo")
                sources_used[src] = sources_used.get(src, 0) + 1
                results.append(entry)

        return {
            "tickers": results,
            "market_open": is_market_open(),
            "as_of": as_of_date,
            "sources": sources_used,
            "missing_count": len(BANNER_SYMBOLS) - len(results),
        }

    return _get_cached("banner_data", fetch)


# --- Sector Heatmap ---

SECTOR_ETFS = [
    ("XLK", "Technology"),
    ("XLF", "Financials"),
    ("XLV", "Healthcare"),
    ("XLE", "Energy"),
    ("XLY", "Consumer Disc."),
    ("XLP", "Consumer Staples"),
    ("XLI", "Industrials"),
    ("XLB", "Materials"),
    ("XLRE", "Real Estate"),
    ("XLU", "Utilities"),
    ("XLC", "Communication"),
]

# Per-sector "last known good" cache. Survives transient fetch failures so
# the heatmap never loses sectors when a single fetch hiccups.
# Format: {symbol: {"price": float, "change_pct": float, "fetched_at": iso_str, "source": "cnbc"|"yfinance"}}
_sector_last_good = {}


def cnbc_quote_batch(symbols):
    """Fetch quote data from CNBC's public quote API for any symbols.

    Works for: stocks, ETFs, indices (^GSPC, ^IXIC, ^DJI), commodities (GC=F, CL=F).

    Returns dict keyed by symbol: {symbol: {"price": float, "change_pct": float}}.
    Returns empty dict on any failure — caller should fall back to yfinance.

    Used as a backup when Yahoo Finance is rate-limited or returning partial data.
    Same data shape as yfinance — drop-in replacement at the value level.

    APPROVED DATA SOURCES per project standards: Yahoo Finance, CNBC, CNN, Bloomberg.
    """
    import requests as _requests
    import json as _json
    out = {}
    try:
        # CNBC quote API — pipe-separated symbols
        url = "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
        params = {
            "symbols": "|".join(symbols),
            "requestMethod": "itv",
            "noform": "1",
            "partnerId": "20",
            "fund": "1",
            "exthrs": "1",
            "output": "json",
        }
        headers = {
            # CNBC blocks default Python user-agents
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.cnbc.com/",
        }
        resp = _requests.get(url, params=params, headers=headers, timeout=8)
        if resp.status_code != 200:
            return out

        # Response is sometimes wrapped in JSONP-like text — try multiple parse paths
        text = resp.text.strip()
        data = None
        try:
            data = resp.json()
        except Exception:
            # Some CNBC responses come as text — try stripping JSONP wrappers
            try:
                # Strip leading "callback(" and trailing ")" if present
                if text.startswith("(") and text.endswith(")"):
                    text = text[1:-1]
                data = _json.loads(text)
            except Exception:
                return out

        # CNBC structure: FormattedQuoteResult.FormattedQuote (list)
        quotes = []
        try:
            if isinstance(data, dict):
                fqr = data.get("FormattedQuoteResult") or {}
                fq = fqr.get("FormattedQuote") if isinstance(fqr, dict) else None
                if isinstance(fq, list):
                    quotes = fq
                elif isinstance(fq, dict):
                    quotes = [fq]
        except Exception:
            return out

        for q in quotes:
            try:
                sym = (q.get("symbol") or "").upper()
                if not sym or sym not in symbols:
                    continue
                # CNBC fields: 'last' (price), 'change_pct' or 'changePct'
                price_raw = q.get("last") or q.get("lastTradePrice") or q.get("price")
                change_pct_raw = (q.get("change_pct") or q.get("changePct")
                                  or q.get("ChangePct") or q.get("percentChange"))
                if price_raw is None or change_pct_raw is None:
                    continue
                # Strip any non-numeric chars (CNBC sometimes sends "+0.45" or "0.45%")
                price_str = str(price_raw).replace(",", "").replace("$", "").strip()
                change_str = str(change_pct_raw).replace("%", "").replace("+", "").strip()
                price = float(price_str)
                change_pct = float(change_str)
                out[sym] = {"price": round(price, 2), "change_pct": round(change_pct, 2)}
            except Exception:
                # Skip individual malformed quote, keep going
                continue
    except Exception as e:
        # Network/timeout/anything — caller falls back to yfinance
        try:
            import logging as _lg
            _lg.getLogger(__name__).debug(f"CNBC sector fetch failed: {e}")
        except Exception:
            pass
    return out


def _fetch_sectors_from_yfinance(symbols, symbol_to_name):
    """Fallback: fetch sectors from yfinance batch (the original code path).

    Returns dict keyed by symbol: {symbol: {"price": float, "change_pct": float, "as_of_date": str|None}}.
    """
    out = {}
    as_of_date = None
    _throttle()
    try:
        df = yf.download(symbols, period="5d", progress=False, group_by="ticker")
    except Exception:
        return out, as_of_date

    if df is None or df.empty:
        return out, as_of_date

    for symbol in symbols:
        try:
            if isinstance(df.columns, pd.MultiIndex):
                if symbol not in df.columns.get_level_values(0):
                    continue
                close_series = df[(symbol, "Close")].dropna()
            else:
                continue

            if close_series is None or len(close_series) < 2:
                continue

            current = float(close_series.iloc[-1])
            prev = float(close_series.iloc[-2])
            change_pct = ((current / prev) - 1) * 100

            if as_of_date is None:
                try:
                    as_of_date = str(close_series.index[-1].date())
                except Exception:
                    pass

            out[symbol] = {
                "price": round(current, 2),
                "change_pct": round(change_pct, 2),
            }
        except Exception:
            continue
    return out, as_of_date


def get_sector_heatmap():
    """Get performance for each S&P 500 sector via SPDR ETFs.

    Source priority (per fetch):
      1. CNBC quote API (primary — faster, more reliable for ETFs)
      2. yfinance batch (fallback if CNBC fails or returns partial)
      3. Per-sector last-known-good cache (so partial outages don't drop sectors)

    Always returns all 11 sectors when at least one source has cached them.
    """
    def fetch():
        symbols = [s[0] for s in SECTOR_ETFS]
        symbol_to_name = {s[0]: s[1] for s in SECTOR_ETFS}
        sectors_by_symbol = {}  # symbol -> dict
        as_of_date = None
        sources_used = []

        # 1) Try CNBC primary (it's faster and more reliable than yfinance batch
        #    specifically for sector ETF data)
        cnbc = cnbc_quote_batch(symbols)
        if cnbc:
            sources_used.append(f"cnbc({len(cnbc)})")
            for sym, val in cnbc.items():
                sectors_by_symbol[sym] = {**val, "_source": "cnbc"}

        # 2) For any sector still missing, fall back to yfinance
        missing = [s for s in symbols if s not in sectors_by_symbol]
        if missing:
            yf_data, yf_as_of = _fetch_sectors_from_yfinance(missing, symbol_to_name)
            if yf_data:
                sources_used.append(f"yfinance({len(yf_data)})")
                if yf_as_of and not as_of_date:
                    as_of_date = yf_as_of
                for sym, val in yf_data.items():
                    sectors_by_symbol[sym] = {**val, "_source": "yfinance"}

        # 3) For any STILL missing, use last-known-good cache
        still_missing = [s for s in symbols if s not in sectors_by_symbol]
        if still_missing:
            for sym in still_missing:
                cached = _sector_last_good.get(sym)
                if cached:
                    sectors_by_symbol[sym] = {
                        "price": cached["price"],
                        "change_pct": cached["change_pct"],
                        "_source": f"cache_{cached.get('source', 'unknown')}",
                        "_stale_at": cached.get("fetched_at"),
                    }

        # 4) Update last-known-good cache for everything we got fresh
        try:
            now_iso = datetime.now().isoformat()
            for sym, val in sectors_by_symbol.items():
                src = val.get("_source", "")
                if src in ("cnbc", "yfinance"):
                    _sector_last_good[sym] = {
                        "price": val["price"],
                        "change_pct": val["change_pct"],
                        "fetched_at": now_iso,
                        "source": src,
                    }
        except Exception:
            pass

        # Build final list in canonical order, sorted by performance
        sectors = []
        for sym, name in SECTOR_ETFS:
            if sym in sectors_by_symbol:
                v = sectors_by_symbol[sym]
                entry = {
                    "symbol": sym,
                    "name": name,
                    "price": v["price"],
                    "change_pct": v["change_pct"],
                }
                # Pass through staleness flag if from cache
                if v.get("_source", "").startswith("cache_"):
                    entry["stale"] = True
                    entry["stale_at"] = v.get("_stale_at")
                sectors.append(entry)

        sectors.sort(key=lambda x: x["change_pct"], reverse=True)
        return {
            "sectors": sectors,
            "market_open": is_market_open(),
            "as_of": as_of_date,
            "generated_at": datetime.now().isoformat(),
            "sources": sources_used,
            "missing_count": 11 - len(sectors),
        }

    return _get_cached("sector_heatmap", fetch, ttl=300)


# --- Top 15 Daily Picks ---

PICK_CANDIDATES = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "NFLX",
    "JPM", "V", "MA", "UNH", "JNJ", "PG", "HD",
    "CRM", "ADBE", "AMD", "INTC", "QCOM",
    "PFE", "ABBV", "MRK", "LLY", "BMY",
    "XOM", "CVX", "COP", "SLB",
    "BA", "CAT", "HON", "GE",
    "DIS", "CMCSA", "NFLX", "SBUX", "MCD", "NKE",
    "WMT", "COST", "TGT",
    "GS", "MS", "BAC", "WFC", "C",
    "NEE", "DUK", "SO",
    "AMT", "PLD", "SPG",
]


def get_daily_picks():
    """
    Symbols to Buy — hedge fund grade stock screening.
    Uses EMA crossovers, RSI, MACD, pivot points, and momentum.
    Calculates recommended hold duration and entry/exit timing.
    Cached for 1 hour.
    """
    def fetch():
        picks = []

        # Download all candidates at once (1 API call)
        _throttle()
        try:
            df = yf.download(PICK_CANDIDATES, period="3mo", progress=False, group_by="ticker")
        except Exception:
            return {"picks": [], "generated_at": datetime.now().isoformat(), "error": "Could not fetch data"}

        if df is None or df.empty:
            return {"picks": [], "generated_at": datetime.now().isoformat(), "error": "No data available"}

        for symbol in PICK_CANDIDATES:
            try:
                ticker_df = df[symbol] if symbol in df.columns.get_level_values(0) else None
                if ticker_df is None or ticker_df.empty or len(ticker_df) < 30:
                    continue

                closes = ticker_df["Close"].dropna().values.astype(float)
                if len(closes) < 30:
                    continue

                current = closes[-1]
                series = pd.Series(closes)

                # RSI (Wilder's smoothing)
                deltas = np.diff(closes)
                gains = np.where(deltas > 0, deltas, 0)
                losses = np.where(deltas < 0, -deltas, 0)
                avg_gain = np.mean(gains[-14:])
                avg_loss = np.mean(losses[-14:])
                rs = avg_gain / avg_loss if avg_loss > 0 else 100
                rsi = 100 - (100 / (1 + rs))

                # EMA crossovers (9, 21, 50)
                ema_9 = float(series.ewm(span=9, adjust=False).mean().iloc[-1])
                ema_21 = float(series.ewm(span=21, adjust=False).mean().iloc[-1])
                ema_50 = float(series.ewm(span=50, adjust=False).mean().iloc[-1]) if len(closes) >= 50 else ema_21

                # MACD
                ema_12 = series.ewm(span=12, adjust=False).mean()
                ema_26 = series.ewm(span=26, adjust=False).mean()
                macd_line = float((ema_12 - ema_26).iloc[-1])
                signal_line = float((ema_12 - ema_26).ewm(span=9, adjust=False).mean().iloc[-1])
                macd_bullish = macd_line > signal_line

                # Momentum (20-day return)
                momentum = ((closes[-1] / closes[-20]) - 1) * 100 if len(closes) >= 20 else 0

                # Volatility
                log_returns = np.log(closes[1:] / closes[:-1])
                vol = float(np.std(log_returns)) * np.sqrt(252) * 100

                # Pivot points
                high_20 = max(closes[-20:])
                low_20 = min(closes[-20:])
                pivot = (high_20 + low_20 + current) / 3
                above_pivot = current > pivot

                # --- Multi-factor scoring ---
                score = 0
                signal = "Hold"
                reasons = []
                action = "Hold"

                # RSI factor
                if rsi < 25:
                    score += 3
                    reasons.append(f"Deeply oversold (RSI {rsi:.0f})")
                elif rsi < 35:
                    score += 2
                    reasons.append(f"Oversold zone (RSI {rsi:.0f})")
                elif rsi > 75:
                    score -= 3
                    reasons.append(f"Extremely overbought (RSI {rsi:.0f})")
                elif rsi > 65:
                    score -= 1
                    reasons.append(f"Overbought (RSI {rsi:.0f})")

                # EMA alignment factor
                if current > ema_9 > ema_21 > ema_50:
                    score += 3
                    reasons.append("Perfect bullish EMA stack")
                elif current > ema_9 > ema_21:
                    score += 2
                    reasons.append("Bullish EMA alignment")
                elif current < ema_9 < ema_21 < ema_50:
                    score -= 2
                    reasons.append("Bearish EMA alignment")

                # MACD factor
                if macd_bullish:
                    score += 1
                    reasons.append("MACD bullish crossover")
                else:
                    score -= 1

                # Momentum factor
                if momentum > 8:
                    score += 2
                    reasons.append(f"Strong momentum (+{momentum:.1f}%)")
                elif momentum > 2:
                    score += 1
                    reasons.append(f"Positive momentum (+{momentum:.1f}%)")
                elif momentum < -8:
                    score -= 2
                    reasons.append(f"Weak momentum ({momentum:.1f}%)")

                # Pivot point factor
                if above_pivot:
                    score += 1
                    reasons.append("Trading above pivot point")
                else:
                    score -= 1

                # Volatility factor
                if vol < 25:
                    score += 1
                elif vol > 50:
                    score -= 1
                    reasons.append(f"High volatility risk ({vol:.0f}%)")

                # Determine action and signal
                if score >= 5:
                    signal = "Strong Buy"
                    action = "Buy Now"
                elif score >= 3:
                    signal = "Strong Buy"
                    action = "Buy"
                elif score >= 1:
                    signal = "Buy"
                    action = "Buy"
                elif score <= -3:
                    signal = "Strong Sell"
                    action = "Sell"
                elif score <= -1:
                    signal = "Sell"
                    action = "Sell"
                else:
                    action = "Hold"

                # Hold duration calculation (simplified for picks)
                hold_days = 14  # base 2 weeks
                if rsi < 30:
                    hold_days += 21  # oversold: hold for recovery
                if current > ema_9 > ema_21 > ema_50:
                    hold_days += 14  # strong trend: ride it
                if vol > 40:
                    hold_days = max(7, hold_days - 7)  # high vol: shorter
                if vol < 20:
                    hold_days += 14  # low vol: safe to hold longer
                hold_days = max(7, min(90, hold_days))

                if hold_days <= 10:
                    hold_label = "1-2 Weeks"
                elif hold_days <= 21:
                    hold_label = "2-3 Weeks"
                elif hold_days <= 35:
                    hold_label = "1 Month"
                elif hold_days <= 60:
                    hold_label = "1-2 Months"
                else:
                    hold_label = "2-3 Months"

                # Probability estimate
                daily_mean = float(np.mean(log_returns))
                daily_std = float(np.std(log_returns))
                tf_mean = daily_mean * 30
                tf_std = daily_std * np.sqrt(30)
                prob_up_30d = float(1 - norm.cdf(0, loc=tf_mean, scale=tf_std)) * 100

                # Entry price guidance
                entry = "At market" if score >= 3 else f"Near ${round(ema_21, 2)}" if score >= 1 else "Avoid"
                target = round(current * (1 + (prob_up_30d / 100) * 0.1), 2) if action == "Buy" or action == "Buy Now" else round(current * 0.95, 2)
                stop_loss = round(current * 0.95, 2) if action != "Sell" else None

                picks.append({
                    "rank": 0,
                    "symbol": symbol,
                    "price": round(current, 2),
                    "rsi": round(rsi, 1),
                    "momentum_20d": round(momentum, 2),
                    "volatility": round(vol, 1),
                    "signal": signal,
                    "action": action,
                    "score": score,
                    "prob_up_30d": round(prob_up_30d, 1),
                    "hold_days": hold_days,
                    "hold_label": hold_label,
                    "entry": entry,
                    "target": target,
                    "stop_loss": stop_loss,
                    "ema_9": round(ema_9, 2),
                    "ema_21": round(ema_21, 2),
                    "pivot": round(pivot, 2),
                    "reasons": reasons[:4],
                })
            except Exception:
                continue

        # Sort by score descending, take top 15
        picks.sort(key=lambda x: x["score"], reverse=True)
        top_15 = picks[:15]
        for i, p in enumerate(top_15):
            p["rank"] = i + 1

        return {
            "picks": top_15,
            "generated_at": datetime.now().isoformat(),
            "total_analyzed": len(picks),
        }

    return _get_cached("daily_picks", fetch, ttl=3600)  # 1 hour cache


# --- Earnings Calendar ---


def get_earnings_calendar():
    """
    Get upcoming earnings for major stocks in the next 14 days.
    Uses a fast batch approach: checks a small set of high-priority stocks
    with earnings_dates instead of calendar (more reliable).
    Also extends to 14 days for better coverage.
    Cached for 6 hours.
    """
    def fetch():
        today = datetime.now().date()
        week_end = today + timedelta(days=14)

        # Only check ~15 stocks at a time to stay fast (15 × 3s = 45s max)
        # These are the biggest market-moving earnings reporters
        priority_stocks = [
            ("AAPL", "Apple Inc"),
            ("MSFT", "Microsoft"),
            ("GOOGL", "Alphabet"),
            ("AMZN", "Amazon"),
            ("META", "Meta Platforms"),
            ("NVDA", "NVIDIA"),
            ("TSLA", "Tesla"),
            ("NFLX", "Netflix"),
            ("JPM", "JPMorgan Chase"),
            ("BAC", "Bank of America"),
            ("UNH", "UnitedHealth"),
            ("JNJ", "Johnson & Johnson"),
            ("XOM", "Exxon Mobil"),
            ("WMT", "Walmart"),
            ("HD", "Home Depot"),
            ("NKE", "Nike"),
            ("FDX", "FedEx"),
            ("MU", "Micron Technology"),
            ("ADBE", "Adobe"),
            ("CRM", "Salesforce"),
            ("COST", "Costco"),
            ("DIS", "Walt Disney"),
            ("BA", "Boeing"),
            ("GS", "Goldman Sachs"),
            ("V", "Visa"),
        ]

        upcoming = []

        for symbol, name in priority_stocks:
            try:
                _throttle()
                stock = yf.Ticker(symbol)

                # Try earnings_dates first (more reliable than calendar)
                try:
                    ed_df = stock.earnings_dates
                    if ed_df is not None and not ed_df.empty:
                        for idx in ed_df.index:
                            try:
                                if hasattr(idx, 'date'):
                                    ed = idx.date()
                                else:
                                    ed = pd.Timestamp(idx).date()

                                if today <= ed <= week_end:
                                    eps_est = None
                                    rev_est = None
                                    try:
                                        if "EPS Estimate" in ed_df.columns:
                                            val = ed_df.loc[idx, "EPS Estimate"]
                                            if pd.notna(val):
                                                eps_est = round(float(val), 2)
                                    except Exception:
                                        pass

                                    upcoming.append({
                                        "symbol": symbol,
                                        "name": name,
                                        "date": ed.isoformat(),
                                        "day_of_week": ed.strftime("%A"),
                                        "eps_estimate": eps_est,
                                        "revenue_estimate": rev_est,
                                    })
                                    break  # Only need the next earnings date
                            except Exception:
                                continue
                        continue  # Move to next stock
                except Exception:
                    pass

                # Fallback: try calendar
                try:
                    cal = stock.calendar
                    if cal and isinstance(cal, dict):
                        ed_raw = cal.get("Earnings Date")
                        if ed_raw:
                            if isinstance(ed_raw, list) and len(ed_raw) > 0:
                                ed_raw = ed_raw[0]
                            if hasattr(ed_raw, 'date'):
                                ed = ed_raw.date()
                            elif isinstance(ed_raw, str):
                                ed = datetime.strptime(ed_raw[:10], "%Y-%m-%d").date()
                            else:
                                continue

                            if today <= ed <= week_end:
                                upcoming.append({
                                    "symbol": symbol,
                                    "name": name,
                                    "date": ed.isoformat(),
                                    "day_of_week": ed.strftime("%A"),
                                    "eps_estimate": round(float(cal.get("Earnings Average", 0)), 2) if cal.get("Earnings Average") else None,
                                    "revenue_estimate": None,
                                })
                except Exception:
                    continue

            except Exception:
                continue

        # Sort by date
        upcoming.sort(key=lambda x: x["date"])

        return {
            "earnings": upcoming,
            "week_start": today.isoformat(),
            "week_end": week_end.isoformat(),
            "stocks_checked": len(priority_stocks),
            "generated_at": datetime.now().isoformat(),
        }

    return _get_cached("earnings_calendar", fetch, ttl=21600)  # 6 hour cache


# --- Daily AI Summary ---

SUMMARY_STOCKS = [s[0] for s in BANNER_SYMBOLS if not s[0].startswith("^")]


def get_daily_summary(watchlist_tickers=None):
    """
    Daily AI Summary — top gainers, biggest losers among S&P 500 big caps,
    plus watchlist summary for the user's stocks.
    Cached for 5 minutes during market hours, 15 min otherwise.
    """
    def fetch():
        _throttle()
        try:
            df = yf.download(SUMMARY_STOCKS, period="5d", progress=False, group_by="ticker")
        except Exception:
            return {"error": "Could not fetch market data", "gainers": [], "losers": [], "watchlist_summary": []}

        if df is None or df.empty:
            return {"error": "No data available", "gainers": [], "losers": [], "watchlist_summary": []}

        symbol_to_name = {s[0]: s[1] for s in BANNER_SYMBOLS}
        movers = []

        for symbol in SUMMARY_STOCKS:
            try:
                if isinstance(df.columns, pd.MultiIndex):
                    if symbol not in df.columns.get_level_values(0):
                        continue
                    close_series = df[(symbol, "Close")].dropna()
                else:
                    continue

                if close_series is None or len(close_series) < 2:
                    continue

                current = float(close_series.iloc[-1])
                prev = float(close_series.iloc[-2])
                change = current - prev
                change_pct = (change / prev) * 100

                movers.append({
                    "symbol": symbol,
                    "name": symbol_to_name.get(symbol, symbol),
                    "price": round(current, 2),
                    "change": round(change, 2),
                    "change_pct": round(change_pct, 2),
                })
            except Exception:
                continue

        # Sort for gainers and losers
        movers.sort(key=lambda x: x["change_pct"], reverse=True)
        gainers = movers[:10]
        losers = sorted(movers, key=lambda x: x["change_pct"])[:10]

        # Market overview
        total_up = sum(1 for m in movers if m["change_pct"] > 0)
        total_down = sum(1 for m in movers if m["change_pct"] < 0)
        avg_change = sum(m["change_pct"] for m in movers) / len(movers) if movers else 0

        if avg_change > 0.5:
            market_mood = "Bullish"
        elif avg_change > 0:
            market_mood = "Slightly Bullish"
        elif avg_change > -0.5:
            market_mood = "Slightly Bearish"
        else:
            market_mood = "Bearish"

        # Determine the actual last TRADING date (skip weekends & major holidays)
        trading_date = None
        try:
            # First try to get it from the actual price data
            for symbol in SUMMARY_STOCKS:
                if isinstance(df.columns, pd.MultiIndex) and symbol in df.columns.get_level_values(0):
                    cs = df[(symbol, "Close")].dropna()
                    if len(cs) >= 1:
                        raw_date = cs.index[-1].date()
                        # If the date falls on a weekend, walk back to Friday
                        from datetime import timedelta
                        while raw_date.weekday() >= 5:  # 5=Sat, 6=Sun
                            raw_date -= timedelta(days=1)
                        # Check for major US market holidays (Good Friday, etc.)
                        us_holidays = _get_market_holidays(raw_date.year)
                        while raw_date in us_holidays or raw_date.weekday() >= 5:
                            raw_date -= timedelta(days=1)
                        trading_date = str(raw_date)
                        break
        except Exception:
            pass

        result = {
            "gainers": gainers,
            "losers": losers,
            "market_overview": {
                "total_stocks": len(movers),
                "advancing": total_up,
                "declining": total_down,
                "avg_change_pct": round(avg_change, 2),
                "mood": market_mood,
            },
            "trading_date": trading_date,
            "market_open": is_market_open(),
            "generated_at": datetime.now().isoformat(),
        }

        return result

    summary = _get_cached("daily_summary", fetch, ttl=300)

    # Add watchlist summary if tickers provided
    if watchlist_tickers:
        wl_tickers = [t.strip().upper() for t in watchlist_tickers.split(",") if t.strip()]
        if wl_tickers:
            wl_summary = _get_watchlist_summary(wl_tickers)
            summary = dict(summary)  # copy so we don't mutate cache
            summary["watchlist_summary"] = wl_summary

    return summary


def _get_watchlist_summary(tickers):
    """Get quick summary for user's watchlist stocks."""
    results = []
    _throttle()
    try:
        df = yf.download(tickers, period="1mo", progress=False, group_by="ticker")
    except Exception:
        return []

    if df is None or df.empty:
        return []

    for symbol in tickers:
        try:
            if isinstance(df.columns, pd.MultiIndex):
                if symbol not in df.columns.get_level_values(0):
                    continue
                close_series = df[(symbol, "Close")].dropna()
            elif len(tickers) == 1:
                close_series = df["Close"].dropna()
            else:
                continue

            if close_series is None or len(close_series) < 2:
                continue

            closes = close_series.values.astype(float)
            current = closes[-1]
            prev = closes[-2]
            day_change = ((current / prev) - 1) * 100

            # Week change
            week_change = ((current / closes[-5]) - 1) * 100 if len(closes) >= 5 else day_change

            # Month change
            month_change = ((current / closes[0]) - 1) * 100

            # Simple RSI
            deltas = np.diff(closes[-15:])
            gains = np.where(deltas > 0, deltas, 0)
            losses = np.where(deltas < 0, -deltas, 0)
            avg_gain = np.mean(gains)
            avg_loss = np.mean(losses)
            rs = avg_gain / avg_loss if avg_loss > 0 else 100
            rsi = 100 - (100 / (1 + rs))

            # Simple signal
            if rsi < 30:
                signal = "Oversold - Consider Buying"
            elif rsi > 70:
                signal = "Overbought - Consider Selling"
            elif day_change > 2:
                signal = "Strong Day - Monitor"
            elif day_change < -2:
                signal = "Down Day - Watch Support"
            else:
                signal = "Neutral - Hold"

            results.append({
                "symbol": symbol,
                "price": round(current, 2),
                "day_change_pct": round(day_change, 2),
                "week_change_pct": round(week_change, 2),
                "month_change_pct": round(month_change, 2),
                "rsi": round(rsi, 1),
                "signal": signal,
            })
        except Exception:
            continue

    return results
