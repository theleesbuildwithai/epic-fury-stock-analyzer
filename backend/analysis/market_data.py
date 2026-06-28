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
        import threading as _dr_thr
        _dr_r = [None]
        _dr_t = _dr_thr.Thread(
            target=lambda r=_dr_r, t=ticker, p=period: r.__setitem__(
                0, yf.download(t, period=p, progress=False)),
            daemon=True)
        _dr_t.start(); _dr_t.join(timeout=10)
        return _dr_r[0]
    except Exception:
        return None


def get_stock_info(ticker: str) -> dict:
    """Get basic info about a stock (name, price, market cap, etc.)."""

    def fetch():
        import threading as _si_thr
        info = None

        # Attempt 1: try stock.info with retry (8s thread timeout each)
        for attempt in range(2):
            try:
                _throttle()
                _si_r = [None]
                _si_t = _si_thr.Thread(
                    target=lambda r=_si_r, t=ticker: r.__setitem__(0, yf.Ticker(t).info),
                    daemon=True)
                _si_t.start(); _si_t.join(timeout=8)
                raw = _si_r[0]
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

        # Attempt 3: multi-source fallback (yahoo_direct → stockanalysis → finviz → twelvedata → polygon → fmp)
        if info is None:
            try:
                from analytics.multi_source_adapter import get_fundamentals_any_source
                ms = get_fundamentals_any_source(ticker)
                if ms and ms.get("currentPrice"):
                    info = {
                        "shortName": ms.get("shortName", ticker.upper()),
                        "regularMarketPrice": float(ms["currentPrice"]),
                        "previousClose": ms.get("previousClose") or 0,
                        "regularMarketOpen": 0,
                        "regularMarketDayHigh": ms.get("fiftyTwoWeekHigh") or 0,
                        "regularMarketDayLow": ms.get("fiftyTwoWeekLow") or 0,
                        "regularMarketVolume": int(ms.get("volume") or 0),
                        "marketCap": ms.get("marketCap") or 0,
                        "trailingPE": ms.get("trailingPE") or 0,
                        "beta": ms.get("beta") or 0,
                        "sector": ms.get("sector") or "N/A",
                        "industry": ms.get("industry") or "N/A",
                        "fiftyTwoWeekHigh": ms.get("fiftyTwoWeekHigh") or 0,
                        "fiftyTwoWeekLow": ms.get("fiftyTwoWeekLow") or 0,
                        "averageVolume": int(ms.get("avgVolume") or 0),
                        "dividendYield": ms.get("dividendYield") or 0,
                    }
            except Exception:
                pass

        # Attempt 4: persistent price cache — last resort, survives full outages and deploys
        if info is None:
            try:
                from analytics.price_cache import get_cached_price
                cached = get_cached_price(ticker)
                if cached and cached.get("price") and float(cached["price"]) > 0:
                    info = {
                        "shortName": ticker.upper(),
                        "regularMarketPrice": float(cached["price"]),
                    }
            except Exception:
                pass

        if info is None:
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


# Persistent cache of last successful historical pull, keyed by ticker.
# Used as a fallback when every fresh yfinance attempt fails — better to
# show last-known data with a stale warning than 404 the user. This is
# what closed the "AMAT analyze returns 404" issue.
_last_good_history = {}   # key: (ticker, period) -> {"data": list, "ts": float}

# --- S3-BACKED SEED for _last_good_history (added 2026-05-31) ---
# Pre-seeds the stale fallback across deploys so cold-start has data
# immediately.  Without this, the FIRST yfinance call per ticker after
# a deploy had no stale fallback to use — that's how AAPL/HPE/NVDA
# 404'd while AMAT (which Tier-2.5 could refresh) worked.
#
# Storage strategy:
#   - ONE compact JSON blob in trading_state, key "historical_seed_v1"
#   - Capped at 200 (ticker, period) entries, 60 records each
#   - ~150-300 KB total — well within trading_state row size limits
#   - Writes are throttled to once per 5 minutes to avoid SQLite churn
#   - Reads happen ONCE per process at first get_historical_data() call
import json as _seed_json
_seed_load_done = False
_seed_save_throttle = 0.0
_SEED_KEY = "historical_seed_v1"
_SEED_SAVE_INTERVAL = 300       # write at most once per 5 min
_SEED_MAX_RECORDS_PER_TICKER = 60
_SEED_MAX_TICKERS = 200


def _load_seed_from_persistent():
    """Load _last_good_history from trading_state on first call.
    Runs ONCE per process.  Safe if trading_state isn't available.
    Never raises — analyze must work even with no seed."""
    global _seed_load_done
    if _seed_load_done:
        return
    _seed_load_done = True   # set first so a partial fail doesn't loop
    try:
        from predictions.models import get_trading_state
        raw = get_trading_state(_SEED_KEY, "")
        if not raw:
            return
        seed = _seed_json.loads(raw)
        for k, entry in (seed or {}).items():
            try:
                ticker, period = k.split("|", 1)
            except ValueError:
                continue
            if entry and entry.get("data"):
                _last_good_history[(ticker, period)] = {
                    "data": entry["data"],
                    "ts": float(entry.get("ts", time.time())),
                }
    except Exception:
        pass  # never block analyze on seed load


def _save_seed_to_persistent():
    """Snapshot _last_good_history into trading_state.  Throttled.
    Keeps only the most-recently-touched _SEED_MAX_TICKERS entries
    and trims each to the last _SEED_MAX_RECORDS_PER_TICKER bars to
    control row size."""
    global _seed_save_throttle
    now = time.time()
    if now - _seed_save_throttle < _SEED_SAVE_INTERVAL:
        return
    _seed_save_throttle = now
    try:
        from predictions.models import set_trading_state
        entries = sorted(
            _last_good_history.items(),
            key=lambda kv: -(kv[1].get("ts", 0) or 0),
        )[:_SEED_MAX_TICKERS]
        seed = {}
        for (ticker, period), val in entries:
            data = val.get("data") or []
            data_trim = data[-_SEED_MAX_RECORDS_PER_TICKER:] if data else []
            seed[f"{ticker}|{period}"] = {
                "data": data_trim,
                "ts": float(val.get("ts", now)),
            }
        set_trading_state(_SEED_KEY, _seed_json.dumps(seed, default=str))
    except Exception:
        pass  # never block on seed save


def get_historical_data(ticker: str, period: str = "1y") -> list:
    """
    Get historical price data for a stock.
    period options: 1mo, 3mo, 6mo, 1y, 2y, 5y
    Returns a list of dicts with date, open, high, low, close, volume.

    SAFETY NETS (added after AMAT 404 incident):
      1. Retry the requested period up to 2 times with backoff
      2. Fall back to shorter periods (6mo -> 3mo -> 1mo) if the
         requested period still fails — gives the user SOMETHING to
         analyze rather than a 404
      3. Stale-cache fallback: if every fresh attempt fails, return
         the last successful pull for this ticker (any period).  This
         is logged and the caller can detect staleness via the cache
         _stale flag if needed.
    """

    # First-call seed: load any prior _last_good_history snapshot from
    # trading_state so the stale-fallback in Tier 3 has data immediately
    # after a deploy.  No-op after the first call.
    _load_seed_from_persistent()

    cache_key = f"history_{ticker}_{period}"
    now = time.time()
    # ONLY trust the in-memory cache when it holds non-empty data.
    # Caching the empty list would lock the user out for 10 minutes if
    # yfinance is briefly down — and silently skip the Tier-3 stale
    # fallback below.  This was the bug that made AMAT 404 persist.
    if cache_key in _cache:
        entry = _cache[cache_key]
        if (now - entry["time"]) < _cache_ttl and entry.get("data"):
            return entry["data"]

    # Tier 1: try the requested period with one retry
    data = []
    import threading as _hist_thr
    for attempt in range(2):
        _throttle()
        try:
            _h1_r = [None]
            _h1_t = _hist_thr.Thread(
                target=lambda r=_h1_r, t=ticker, p=period: r.__setitem__(
                    0, yf.download(t, period=p, progress=False)),
                daemon=True)
            _h1_t.start(); _h1_t.join(timeout=12)
            df = _h1_r[0]
            if df is not None and not df.empty:
                data = _df_to_records(df)
                if data:
                    break
        except Exception:
            pass
        if attempt == 0:
            time.sleep(1.5)

    # Tier 2: shorter fallback periods (6mo, 3mo, 1mo) — long windows
    # are bigger payloads and fail more often under Yahoo load.
    if not data:
        for fp in ("6mo", "3mo", "1mo"):
            if fp == period:
                continue
            _throttle()
            try:
                _h2_r = [None]
                _h2_t = _hist_thr.Thread(
                    target=lambda r=_h2_r, t=ticker, p=fp: r.__setitem__(
                        0, yf.download(t, period=p, progress=False)),
                    daemon=True)
                _h2_t.start(); _h2_t.join(timeout=10)
                df = _h2_r[0]
                if df is not None and not df.empty:
                    data = _df_to_records(df)
                    if data:
                        break
            except Exception:
                continue

    # Tier 2.5: yfinance.Ticker(...).history() uses a DIFFERENT API
    # endpoint than yf.download() — when yf.download fails on a
    # specific ticker (e.g. AMAT consistently 404'd while AAPL/HPE
    # worked), Ticker.history often still succeeds.  This was the
    # ticker-specific failure mode that caused the AMAT analyze 404.
    if not data:
        for fp in (period, "6mo", "3mo", "1mo"):
            _throttle()
            try:
                _h25_r = [None]
                _h25_t = _hist_thr.Thread(
                    target=lambda r=_h25_r, t=ticker, p=fp: r.__setitem__(
                        0, yf.Ticker(t).history(period=p, auto_adjust=True)),
                    daemon=True)
                _h25_t.start(); _h25_t.join(timeout=8)
                df = _h25_r[0]
                if df is not None and not df.empty:
                    data = _df_to_records(df)
                    if data:
                        break
            except Exception:
                continue

    # Tier 2.7: multi-source historical (Tiingo/AlphaVantage/FMP — key-gated)
    if not data:
        try:
            from analytics.multi_source_adapter import get_historical_any_source
            import pandas as _ms_pd
            df_ms = get_historical_any_source(ticker, period)
            if df_ms is not None and not df_ms.empty:
                data = _df_to_records(df_ms)
        except Exception:
            pass

    # Tier 3: serve last-known good data for ANY period of this ticker.
    # No 404 when yfinance is temporarily down.  Returns a COPY so a
    # downstream mutation can't corrupt the fallback store.
    if not data:
        for key, entry in _last_good_history.items():
            if key[0] == ticker and entry.get("data"):
                data = list(entry["data"])
                break

    if data:
        # Persist to both stores so the next call (a) hits in-memory
        # cache fast and (b) has stale fallback available later.
        _cache[cache_key] = {"data": data, "time": now}
        _last_good_history[(ticker, period)] = {"data": data, "ts": now}
        # Cross-deploy seed: throttled snapshot to trading_state.
        # This is what lets Tier 3 serve data immediately after a
        # cold deploy (the AAPL/HPE/NVDA "Stock not found" gap).
        _save_seed_to_persistent()
    return data


def _df_to_records(df) -> list:
    """Convert a yfinance DataFrame into the dict-list format the rest
    of the codebase expects. Safe against single-row frames."""
    records = []
    for date, row in df.iterrows():
        try:
            records.append({
                "date": date.strftime("%Y-%m-%d"),
                "open": round(float(row["Open"]), 2),
                "high": round(float(row["High"]), 2),
                "low": round(float(row["Low"]), 2),
                "close": round(float(row["Close"]), 2),
                "volume": int(row["Volume"]),
            })
        except (ValueError, TypeError, KeyError):
            continue  # skip malformed rows but keep the rest
    return records


def get_benchmark_data(period: str = "1y") -> dict:
    """
    Get historical data for the 3 major market indices.

    Completely BYPASSES get_historical_data() and its stale-cache infrastructure
    to guarantee fresh, correct prices. Uses Ticker.history() with explicit date
    ranges (same proven approach as the regime analysis fix), multiple ETF fallbacks
    per index, and a hard price-sanity gate that rejects obviously stale data.

    Safety nets:
      - Explicit start/end dates (not period strings) — avoids yfinance period quirks
      - 3 ETF fallbacks per index (e.g. SPY → IVV → VOO for S&P 500)
      - Price sanity check: if last close < min_price, the data is rejected
      - MultiIndex flattening in case yfinance returns unexpected column structure
      - 12s thread timeout per ETF download attempt
      - Own 30-min cache keyed separately from the main _cache stale-seed path
    """
    import yfinance as _yf_bm
    import threading as _bm_thr
    from datetime import datetime as _dt_bm, timedelta as _td_bm
    import logging as _bm_log
    _bm_logger = _bm_log.getLogger(__name__)

    # Own 30-min cache — completely separate from _cache/_last_good_history
    _bm_key = f"__benchmark_{period}"
    _now = time.time()
    if _bm_key in _cache:
        _e = _cache[_bm_key]
        if _now - _e["time"] < 1800 and _e.get("data"):
            return _e["data"]

    # Multiple ETF fallbacks per index (primary → secondary → tertiary)
    # min_price = minimum acceptable last-close price; rejects stale/wrong data
    _indices = [
        ("sp500",     "S&P 500",    ["SPY", "IVV", "VOO"],   100.0),
        ("nasdaq",    "Nasdaq 100", ["QQQ", "QQQM", "ONEQ"],  50.0),
        ("dow_jones", "Dow Jones",  ["DIA"],                  100.0),
    ]

    # Explicit date range — avoids yfinance "period=" misinterpretation
    _days = {"1mo": 35, "3mo": 100, "6mo": 185, "1y": 370, "2y": 740, "5y": 1850}
    _end_dt = _dt_bm.today()
    _start_dt = _end_dt - _td_bm(days=_days.get(period, 370))
    _start_s = _start_dt.strftime("%Y-%m-%d")
    _end_s = _end_dt.strftime("%Y-%m-%d")

    result = {}

    for _name, _label, _etfs, _min_px in _indices:
        _data = []
        _sym_used = None

        for _sym in _etfs:
            try:
                _r = [None]
                _t = _bm_thr.Thread(
                    target=lambda r=_r, s=_sym: r.__setitem__(
                        0, _yf_bm.Ticker(s).history(
                            start=_start_s, end=_end_s, auto_adjust=True)),
                    daemon=True)
                _t.start(); _t.join(timeout=12)
                _df = _r[0]

                if _df is None or _df.empty or len(_df) < 10:
                    _bm_logger.warning(f"benchmark: {_sym} — empty or too short")
                    continue

                # Flatten MultiIndex if present (safety net)
                if hasattr(_df.columns, "levels"):
                    _df = _df.copy()
                    _df.columns = _df.columns.get_level_values(0)

                _records = []
                for _date, _row in _df.iterrows():
                    try:
                        _close = float(_row.get("Close", 0) or _row.iloc[3])
                        if _close <= 0:
                            continue
                        _records.append({
                            "date": _date.strftime("%Y-%m-%d"),
                            "open":   round(float(_row.get("Open",   _close)), 2),
                            "high":   round(float(_row.get("High",   _close)), 2),
                            "low":    round(float(_row.get("Low",    _close)), 2),
                            "close":  round(_close, 2),
                            "volume": int(_row.get("Volume", 0) or 0),
                        })
                    except Exception:
                        continue

                if not _records:
                    _bm_logger.warning(f"benchmark: {_sym} — _df_to_records returned empty")
                    continue

                # Hard price-sanity gate — rejects stale/adjusted-to-oblivion data
                _last_close = _records[-1]["close"]
                if _last_close < _min_px:
                    _bm_logger.warning(
                        f"benchmark: {_sym} REJECTED — last_close={_last_close} < min={_min_px} (stale data)")
                    continue

                _data = _records
                _sym_used = _sym
                _bm_logger.info(
                    f"benchmark: {_sym} OK — {len(_records)} pts, last_close={_last_close}")
                break

            except Exception as _ex:
                _bm_logger.warning(f"benchmark: {_sym} exception: {_ex}")
                continue

        if _data:
            _sp = _data[0]["close"]
            _ep = _data[-1]["close"]
            result[_name] = {
                "symbol":           _sym_used,
                "label":            f"{_label} (via {_sym_used})",
                "start_price":      _sp,
                "end_price":        _ep,
                "total_return_pct": round(((_ep - _sp) / _sp) * 100, 2),
                "data":             _data,
            }

    # Cache only when we have real data
    if result:
        _cache[_bm_key] = {"data": result, "time": _now}

    return result
