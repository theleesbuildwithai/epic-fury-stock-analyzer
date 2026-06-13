"""
Persistent Last-Known Price Cache — analytics/price_cache.py

When every live data source fails (rate limits, outages, bot detection),
this module provides last-known prices persisted in the DB (trading_state).

Design:
  - One JSON blob in trading_state, key "price_cache_v1"
  - Format: {ticker: {"price": float, "ts": float, "source": str}}
  - Capped at 500 tickers (watchlist + portfolio + universe)
  - In-memory mirror for O(1) reads without DB hit on every call
  - Writes throttled to once per 60 seconds to avoid SQLite churn
  - Reads happen ONCE per process at first call (lazy-loaded)

Usage:
    from analytics.price_cache import update_price_cache, get_cached_price

    update_price_cache({"AAPL": 291.13, "MSFT": 450.22}, source="yfinance")
    cached = get_cached_price("AAPL")  # {"price": 291.13, "ts": ..., "source": "yfinance"}
"""

import time
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_CACHE_KEY = "price_cache_v1"
_MAX_TICKERS = 500
_SAVE_INTERVAL = 60   # write to DB at most once per minute

_mem: dict = {}       # ticker.upper() → {price, ts, source}
_loaded = False
_last_save = 0.0


def _load():
    """Lazy-load from trading_state on first call. Runs once per process."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        from predictions.models import get_trading_state
        raw = get_trading_state(_CACHE_KEY, "")
        if raw:
            data = json.loads(raw)
            if isinstance(data, dict):
                _mem.update(data)
                logger.info(f"PriceCache: loaded {len(_mem)} entries from DB")
    except Exception as e:
        logger.debug(f"PriceCache: load failed (non-fatal): {e}")


def _save():
    """Snapshot _mem to trading_state. Throttled to once per minute."""
    global _last_save
    now = time.time()
    if now - _last_save < _SAVE_INTERVAL:
        return
    _last_save = now
    try:
        from predictions.models import set_trading_state
        # Keep only the most-recently-seen tickers to cap row size
        trimmed = dict(
            sorted(_mem.items(), key=lambda kv: -(kv[1].get("ts") or 0))[:_MAX_TICKERS]
        )
        set_trading_state(_CACHE_KEY, json.dumps(trimmed, default=str))
        logger.debug(f"PriceCache: saved {len(trimmed)} entries to DB")
    except Exception as e:
        logger.debug(f"PriceCache: save failed (non-fatal): {e}")


def update_price_cache(prices: dict, source: str = "unknown"):
    """
    Update the cache with fresh prices.

    Args:
        prices: {ticker: float} dict of raw prices
        source: string label for the data source ("yfinance", "finnhub", etc.)
    """
    _load()
    now = time.time()
    updated = 0
    for ticker, price in prices.items():
        try:
            p = float(price)
            if p > 0:
                _mem[ticker.upper()] = {"price": p, "ts": now, "source": source}
                updated += 1
        except (TypeError, ValueError):
            pass
    if updated:
        _save()


def get_cached_price(ticker: str) -> Optional[dict]:
    """
    Return {price, ts, source} for ticker or None if not cached.
    Never raises.
    """
    _load()
    return _mem.get(ticker.upper())


def get_all_cached_prices() -> dict:
    """Return full cache dict: {ticker: {price, ts, source}}. Never raises."""
    _load()
    return dict(_mem)


def get_cache_status() -> dict:
    """Health summary for the admin endpoint."""
    _load()
    now = time.time()
    fresh = sum(1 for v in _mem.values() if (now - v.get("ts", 0)) < 3600)
    stale = len(_mem) - fresh
    return {
        "total_tickers": len(_mem),
        "fresh_1h": fresh,
        "stale_over_1h": stale,
        "last_save_s_ago": round(now - _last_save, 0) if _last_save else None,
    }
