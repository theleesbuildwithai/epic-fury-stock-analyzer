"""
Data Shield — backend/analytics/data_shield.py

Bulletproof yfinance wrapper with multi-layer fallbacks.
Every function here is designed to NEVER crash the caller:
  - Exponential backoff retry (3 attempts, 1s → 2s → 4s)
  - Stooq fallback when yfinance fails or returns garbage
  - Multi-source fallback (stockanalysis, finviz, AV, Tiingo, FMP)
  - In-memory TTL cache (no redundant API calls)
  - Corruption detection (price sanity bounds)
  - Persistent DB cache (trading_state) for critical data

Design principle: trade latency for reliability.
If yfinance is down, Stooq takes over transparently.
If Stooq is also down, multi_source_adapter tries 5 more sources.
If all live sources fail, return last cached value with staleness flag.

Usage:
    from analytics.data_shield import safe_download, safe_ticker_info

    df = safe_download("AAPL", period="6mo")   # Never raises
    info = safe_ticker_info("AAPL")             # Never raises
"""

import time
import logging
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ============================================================
#  IN-MEMORY CACHE
# ============================================================
_mem_cache: dict = {}  # key → {df, ts, source}

# TTLs
_TTL_PRICE_DATA = 300    # 5 min — price data (live market)
_TTL_MACRO = 600         # 10 min — VIX, futures, macro
_TTL_INFO = 86400        # 24 hours — fundamental info
_TTL_STALE_ACCEPT = 7200 # 2 hours — accept stale on full outage


def _cache_get(key: str, ttl: float) -> Optional[dict]:
    entry = _mem_cache.get(key)
    if entry and (time.time() - entry["ts"]) < ttl:
        return entry
    return None


def _cache_set(key: str, df, source: str):
    _mem_cache[key] = {"df": df, "ts": time.time(), "source": source}


def _cache_get_stale(key: str, max_age: float = _TTL_STALE_ACCEPT) -> Optional[dict]:
    """Accept stale cache entry during outages."""
    entry = _mem_cache.get(key)
    if entry and (time.time() - entry["ts"]) < max_age:
        return entry
    return None


# ============================================================
#  CORRUPTION DETECTION
# ============================================================

def _is_price_df_valid(df, ticker: str = "") -> bool:
    """Check that a price DataFrame is not corrupt."""
    try:
        if df is None or df.empty:
            return False
        if len(df) < 3:
            return False

        # Extract close column safely
        close_col = df.get("Close")
        if close_col is None:
            return False

        if hasattr(close_col, "columns"):
            closes = close_col.iloc[:, 0].dropna().values.astype(float)
        else:
            closes = close_col.dropna().values.astype(float)

        if len(closes) < 3:
            return False

        last_close = float(closes[-1])

        # Absolute bounds: no stock should be $0 or $1,000,000
        if not (0.01 < last_close < 500000):
            logger.warning(f"DataShield: {ticker} price {last_close} out of absolute bounds")
            return False

        # Single-day move sanity: >100% up or >60% down in one day = data glitch
        if len(closes) >= 2:
            prior = float(closes[-2])
            if prior > 0:
                ratio = last_close / prior
                if ratio > 2.0 or ratio < 0.4:
                    logger.warning(f"DataShield: {ticker} single-day move {ratio:.2f}x — likely data glitch")
                    return False

        # No NaN in last 5 rows
        if np.any(np.isnan(closes[-min(5, len(closes)):])):
            return False

        return True

    except Exception:
        return False


# ============================================================
#  STOOQ FALLBACK DOWNLOADER
# ============================================================

_STOOQ_TICKER_MAP = {
    "^GSPC": "^spx",
    "^VIX": "^vix",
    "^TNX": "^tnx",
    "^IRX": "^irx",
    "^VIX3M": "^vix3m",
    "GC=F": "gc.f",
    "CL=F": "cl.f",
    "ES=F": "es.f",
    "NQ=F": "nq.f",
    "BTC-USD": "btc-usd.v",
}


def _stooq_ticker(ticker: str) -> str:
    return _STOOQ_TICKER_MAP.get(ticker, ticker.lower().replace("-", ".").replace("^", "^"))


def _download_from_stooq(ticker: str, period: str = "6mo") -> Optional[pd.DataFrame]:
    """Download from Stooq as yfinance fallback. Returns df or None."""
    try:
        import pandas_datareader.data as pdr

        # Convert period to start/end dates
        period_days = {
            "5d": 5, "1mo": 30, "3mo": 90, "6mo": 182,
            "1y": 365, "2y": 730, "5y": 1825,
        }
        days = period_days.get(period, 182)
        end = datetime.now()
        start = end - timedelta(days=days)

        stooq_sym = _stooq_ticker(ticker)
        df = pdr.DataReader(stooq_sym, "stooq", start=start, end=end)
        if df is not None and not df.empty:
            # Rename columns to match yfinance format
            df = df.rename(columns=str.title)
            df = df.sort_index()
            logger.info(f"DataShield: Stooq fallback succeeded for {ticker} ({len(df)} rows)")
            return df
    except Exception as e:
        logger.debug(f"DataShield: Stooq fallback failed for {ticker}: {e}")
    return None


# ============================================================
#  RETRY WRAPPER
# ============================================================

def _download_yfinance_with_retry(ticker: str, period: str = "6mo",
                                   max_attempts: int = 3) -> Optional[pd.DataFrame]:
    """yfinance download with exponential backoff retry + 10s thread timeout per attempt."""
    import yfinance as yf
    import threading as _ds_thr

    delay = 1.0
    for attempt in range(max_attempts):
        try:
            _ds_r = [None]
            _ds_t = _ds_thr.Thread(
                target=lambda r=_ds_r, t=ticker, p=period: r.__setitem__(
                    0, yf.download(t, period=p, progress=False, auto_adjust=True)
                ), daemon=True)
            _ds_t.start(); _ds_t.join(timeout=10)
            df = _ds_r[0]
            if df is not None and not df.empty:
                return df
            logger.debug(f"DataShield: yfinance empty for {ticker}, attempt {attempt + 1}")
        except Exception as e:
            logger.debug(f"DataShield: yfinance error for {ticker} attempt {attempt + 1}: {e}")

        if attempt < max_attempts - 1:
            time.sleep(delay)
            delay *= 2.0  # exponential backoff

    return None


# ============================================================
#  MAIN PUBLIC API
# ============================================================

def safe_download(ticker: str, period: str = "6mo", ttl: Optional[float] = None,
                  require_rows: int = 5) -> Optional[pd.DataFrame]:
    """
    Download price data with full safety net.

    Layers:
      1. In-memory TTL cache (instant)
      2. yfinance with 3-attempt retry + backoff
      3. Stooq fallback
      3b. Multi-source fallback (Tiingo/AV/FMP historical, if keys configured)
      4. Stale cache acceptance (2h window during outages)

    Returns:
        DataFrame if any source succeeds, None if all fail.
        Never raises an exception.
    """
    try:
        effective_ttl = ttl or _TTL_PRICE_DATA
        cache_key = f"{ticker}|{period}"

        # Layer 1: fresh cache
        cached = _cache_get(cache_key, effective_ttl)
        if cached:
            return cached["df"]

        # Layer 2: yfinance with retry
        df = _download_yfinance_with_retry(ticker, period)
        if df is not None and _is_price_df_valid(df, ticker):
            _cache_set(cache_key, df, "yfinance")
            return df

        if df is not None and not df.empty and not _is_price_df_valid(df, ticker):
            logger.warning(f"DataShield: yfinance data for {ticker} failed corruption check")

        # Layer 3: Stooq fallback
        df_stooq = _download_from_stooq(ticker, period)
        if df_stooq is not None and len(df_stooq) >= require_rows:
            _cache_set(cache_key, df_stooq, "stooq")
            return df_stooq

        # Layer 3b: Multi-source historical fallback (Tiingo/AV/FMP — key-gated)
        try:
            from analytics.multi_source_adapter import get_historical_any_source
            df_ms = get_historical_any_source(ticker, period)
            if df_ms is not None and len(df_ms) >= require_rows:
                logger.info(f"DataShield: multi-source historical succeeded for {ticker}")
                _cache_set(cache_key, df_ms, "multi_source")
                return df_ms
        except Exception as _ms_e:
            logger.debug(f"DataShield: multi-source historical error for {ticker}: {_ms_e}")

        # Layer 4: Accept stale cache
        stale = _cache_get_stale(cache_key)
        if stale:
            logger.warning(f"DataShield: Serving stale cache for {ticker} (age={time.time()-stale['ts']:.0f}s)")
            return stale["df"]

        logger.error(f"DataShield: All sources failed for {ticker}")
        return None

    except Exception as e:
        logger.error(f"DataShield: Unexpected error for {ticker}: {e}")
        return None


def safe_batch_download(tickers: list, period: str = "6mo",
                        group_by: str = "ticker") -> Optional[pd.DataFrame]:
    """
    Batch download with automatic retry and fallback.
    Falls back to individual downloads if batch fails.
    Never raises an exception.
    """
    import yfinance as yf

    if not tickers:
        return None

    try:
        import threading as _sbd_thr
        # Try batch first (most efficient)
        for attempt in range(2):
            try:
                _sbd_r = [None]
                _sbd_t = _sbd_thr.Thread(
                    target=lambda r=_sbd_r, tk=tickers, p=period, g=group_by: r.__setitem__(
                        0, yf.download(tk, period=p, progress=False, group_by=g, auto_adjust=True)),
                    daemon=True)
                _sbd_t.start(); _sbd_t.join(timeout=15)
                df = _sbd_r[0]
                if df is not None and not df.empty:
                    return df
            except Exception as e:
                logger.debug(f"DataShield batch attempt {attempt + 1} failed: {e}")
                if attempt == 0:
                    time.sleep(2.0)

        # Fallback: individual downloads for each ticker
        logger.warning(f"DataShield: batch download failed, trying individual for {len(tickers)} tickers")
        frames = {}
        for tick in tickers[:20]:  # cap at 20 to avoid excessive calls
            df_i = safe_download(tick, period=period)
            if df_i is not None and not df_i.empty:
                frames[tick] = df_i

        if frames:
            # Reconstruct multi-level DataFrame
            combined = pd.concat(
                {tick: df[["Close", "Open", "High", "Low", "Volume"]]
                 for tick, df in frames.items()
                 if all(c in df.columns for c in ["Close", "Open", "High", "Low", "Volume"])},
                axis=1
            )
            if group_by == "ticker":
                combined.columns = pd.MultiIndex.from_tuples(
                    [(tick, col) for tick, col in combined.columns]
                )
            return combined

    except Exception as e:
        logger.error(f"DataShield batch download total failure: {e}")

    return None


def safe_ticker_info(ticker: str) -> dict:
    """
    Fetch ticker fundamental info with caching.
    Returns empty dict on any failure. Never raises.

    Layers:
      1. In-memory cache
      2. yfinance (2-attempt retry, 5s timeout each)
      2b. multi_source_adapter (stockanalysis → finviz → fmp)
      3. Stale cache (72h window)
    """
    import yfinance as yf

    cache_key = f"info|{ticker}"
    cached = _cache_get(cache_key, _TTL_INFO)
    if cached:
        return cached["df"] or {}

    for attempt in range(2):
        try:
            import threading as _dsi_thr
            _dsi_r = [{}]
            _dsi_t = _dsi_thr.Thread(
                target=lambda r=_dsi_r, t=ticker: r.__setitem__(0, yf.Ticker(t).info or {}),
                daemon=True)
            _dsi_t.start(); _dsi_t.join(timeout=5)
            info = _dsi_r[0]
            if info:
                _cache_set(cache_key, info, "yfinance")
                return info
        except Exception as e:
            logger.debug(f"DataShield ticker info {ticker} attempt {attempt + 1}: {e}")
            if attempt == 0:
                time.sleep(1.5)

    # Layer 2b: multi-source fundamentals fallback (yahoo_direct → stockanalysis → finviz → twelvedata → polygon → fmp)
    try:
        from analytics.multi_source_adapter import get_fundamentals_any_source
        ms_info = get_fundamentals_any_source(ticker)
        if ms_info:
            logger.info(f"DataShield: multi-source fundamentals for {ticker} via {ms_info.get('_source','?')}")
            _cache_set(cache_key, ms_info, ms_info.get("_source", "multi_source"))
            return ms_info
    except Exception as _ms_e:
        logger.debug(f"DataShield: multi-source fundamentals error for {ticker}: {_ms_e}")

    # Layer 2c: Yahoo Finance direct API (no library, independent code path)
    try:
        from analytics.multi_source_adapter import yahoo_direct_quote
        yh = yahoo_direct_quote(ticker)
        if yh and yh.get("price") and float(yh["price"]) > 0:
            yh_info = {"regularMarketPrice": float(yh["price"]), "shortName": ticker.upper()}
            _cache_set(cache_key, yh_info, "yahoo_direct")
            return yh_info
    except Exception as _yh_e:
        logger.debug(f"DataShield: yahoo_direct info error for {ticker}: {_yh_e}")

    # Layer 2d: Persistent price cache — survives full outages and deploys
    try:
        from analytics.price_cache import get_cached_price
        cached = get_cached_price(ticker)
        if cached and cached.get("price") and float(cached["price"]) > 0:
            cached_info = {"regularMarketPrice": float(cached["price"]), "shortName": ticker.upper()}
            return cached_info
    except Exception:
        pass

    # Return stale if available
    stale = _cache_get_stale(cache_key, _TTL_INFO * 3)
    if stale:
        return stale["df"] or {}

    return {}


# ============================================================
#  HEALTH STATUS ENDPOINT
# ============================================================

def get_shield_status() -> dict:
    """
    Returns current health of all data sources.
    Used by /api/data-shield/status endpoint.
    """
    status = {
        "timestamp": datetime.now().isoformat(),
        "cache_entries": len(_mem_cache),
        "sources": {},
    }

    # Test yfinance with a quick SPY fetch — 8s thread timeout
    yf_ok = False
    yf_latency = None
    try:
        import yfinance as yf
        import threading as _spy_thr
        _spy_r = [None]
        t0 = time.time()
        _spy_t = _spy_thr.Thread(
            target=lambda r=_spy_r: r.__setitem__(0, yf.download("SPY", period="5d", progress=False)),
            daemon=True)
        _spy_t.start(); _spy_t.join(timeout=8)
        df = _spy_r[0]
        yf_latency = round(time.time() - t0, 2)
        yf_ok = df is not None and len(df) >= 3
    except Exception:
        pass

    status["sources"]["yfinance"] = {
        "ok": yf_ok,
        "latency_s": yf_latency,
        "status": "HEALTHY" if yf_ok else "DOWN",
    }

    # Test Stooq
    stooq_ok = False
    stooq_latency = None
    try:
        t0 = time.time()
        df_s = _download_from_stooq("SPY", "5d")
        stooq_latency = round(time.time() - t0, 2)
        stooq_ok = df_s is not None and len(df_s) >= 3
    except Exception:
        pass

    status["sources"]["stooq"] = {
        "ok": stooq_ok,
        "latency_s": stooq_latency,
        "status": "HEALTHY" if stooq_ok else "DOWN",
    }

    # Multi-source status (stockanalysis, finviz, AV, Tiingo, FMP)
    try:
        from analytics.multi_source_adapter import get_multi_source_status
        status["sources"]["multi_source"] = get_multi_source_status()
    except Exception as _ms_e:
        status["sources"]["multi_source"] = {"error": str(_ms_e)[:120]}

    # Overall assessment
    ms = status["sources"].get("multi_source", {})
    ms_any_ok = any(
        v.get("ok") for v in ms.values() if isinstance(v, dict)
    ) if isinstance(ms, dict) else False

    if yf_ok:
        status["overall"] = "HEALTHY"
        status["primary_source"] = "yfinance"
    elif stooq_ok:
        status["overall"] = "DEGRADED"
        status["primary_source"] = "stooq_fallback"
        status["warning"] = "yfinance down — Stooq fallback active"
    elif ms_any_ok:
        status["overall"] = "DEGRADED"
        status["primary_source"] = "multi_source_fallback"
        status["warning"] = "yfinance+Stooq down — multi-source safety nets active"
    else:
        status["overall"] = "CRITICAL"
        status["primary_source"] = "cache_only"
        status["warning"] = "All live sources down — serving cached data only"

    # Stale cache summary
    now = time.time()
    fresh = sum(1 for v in _mem_cache.values() if now - v["ts"] < _TTL_PRICE_DATA)
    stale = sum(1 for v in _mem_cache.values() if now - v["ts"] >= _TTL_PRICE_DATA)
    status["cache_fresh"] = fresh
    status["cache_stale"] = stale

    return status
