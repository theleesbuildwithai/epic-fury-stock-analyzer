"""
Finnhub Adapter — primary price source with graceful fallback.

Finnhub gives us a guaranteed 60 calls/min on the free tier with a clear
error code if exceeded (vs Yahoo's silent random rate-limiting). When a
FINNHUB_API_KEY env var is set, this becomes TIER 0 in the price lookup
chain, ahead of Yahoo. When the key is not set, this module is a no-op
and existing behavior is preserved.

SAFETY:
  - Returns {} on any error (no exceptions ever propagate)
  - 12-second per-call timeout
  - Process-local rate-limit guard (55/min ceiling — under the 60 cap)
  - 30-second response cache per symbol
  - When key missing, is_enabled() returns False and all calls return {}
"""

import logging
import os
import time
import threading
from collections import deque
from typing import Optional
import urllib.request
import urllib.parse
import json

logger = logging.getLogger(__name__)

FINNHUB_BASE = "https://finnhub.io/api/v1"
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()
FINNHUB_TIMEOUT = 12  # seconds
FINNHUB_BUDGET = 55  # under 60/min cap to leave headroom

_call_times = deque()
_quote_cache = {}  # ticker -> {"price": float, "ts": float}
_QUOTE_CACHE_TTL = 30  # seconds
_lock = threading.Lock()


def is_enabled() -> bool:
    """True only when API key is configured. When False, all functions
    return empty dicts so the existing yfinance/CNBC/Stooq fallback
    chain runs unchanged."""
    return bool(FINNHUB_KEY)


def _under_budget() -> bool:
    """Soft rate-limit guard. Trims old call timestamps and returns
    True if we're under the per-minute budget."""
    try:
        now = time.time()
        cutoff = now - 60.0
        with _lock:
            while _call_times and _call_times[0] < cutoff:
                _call_times.popleft()
            return len(_call_times) < FINNHUB_BUDGET
    except Exception:
        return True  # fail open — let the call proceed


def _record_call():
    """Record a call timestamp for rate-limit accounting."""
    try:
        with _lock:
            _call_times.append(time.time())
    except Exception:
        pass


def get_quote(ticker: str) -> dict:
    """Fetch a single quote. Returns {"price": float, "prev_close": float,
    "high": float, "low": float, "open": float, "ts": int} or {} on any
    failure. Uses a 30-second per-symbol cache.
    """
    if not is_enabled() or not ticker:
        return {}
    try:
        # Cache check
        now = time.time()
        cached = _quote_cache.get(ticker)
        if cached and (now - cached.get("ts", 0)) < _QUOTE_CACHE_TTL:
            return {
                "price": cached["price"],
                "prev_close": cached.get("prev_close"),
                "high": cached.get("high"),
                "low": cached.get("low"),
                "open": cached.get("open"),
                "ts": cached.get("ts"),
                "cached": True,
                "source": "finnhub",
            }

        if not _under_budget():
            return {}

        url = (f"{FINNHUB_BASE}/quote?symbol={urllib.parse.quote(ticker)}"
               f"&token={FINNHUB_KEY}")
        req = urllib.request.Request(url, headers={"User-Agent": "sentinel-quant/1.0"})
        _record_call()
        with urllib.request.urlopen(req, timeout=FINNHUB_TIMEOUT) as resp:
            body = resp.read()
        data = json.loads(body)
        # Finnhub returns: c (current), h (high), l (low), o (open),
        # pc (prev close), t (epoch). c=0 means no data.
        price = float(data.get("c") or 0)
        if price <= 0:
            return {}
        record = {
            "price": price,
            "prev_close": float(data.get("pc") or 0) or None,
            "high": float(data.get("h") or 0) or None,
            "low": float(data.get("l") or 0) or None,
            "open": float(data.get("o") or 0) or None,
            "ts": now,
        }
        # Cache
        try:
            _quote_cache[ticker] = record
        except Exception:
            pass
        return {**record, "cached": False, "source": "finnhub"}
    except Exception as e:
        logger.debug(f"finnhub get_quote({ticker}) failed: {e}")
        return {}


def get_prices_batch(tickers: list) -> dict:
    """Fetch quotes for multiple tickers. Returns {ticker: price} dict.
    Skips tickers that fail; never raises. Caller must be prepared for
    a partial result.

    SAFETY: budget-limited, cached, sequential (Finnhub doesn't have a
    batch quote endpoint on free tier). For >50 tickers per cycle this
    will skip some — caller should fall back to yfinance for rest.
    """
    if not is_enabled() or not tickers:
        return {}
    out = {}
    try:
        # Cap at 30 per call to leave room for other features
        for ticker in list(tickers)[:30]:
            try:
                q = get_quote(ticker)
                if q.get("price"):
                    out[ticker] = q["price"]
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"finnhub batch failed: {e}")
    return out


def get_status() -> dict:
    """Health snapshot for the admin endpoint."""
    try:
        now = time.time()
        cutoff = now - 60.0
        recent = sum(1 for t in _call_times if t >= cutoff)
        return {
            "enabled": is_enabled(),
            "key_configured": bool(FINNHUB_KEY),
            "calls_last_60s": recent,
            "budget_per_min": FINNHUB_BUDGET,
            "budget_remaining": max(0, FINNHUB_BUDGET - recent),
            "cached_symbols": len(_quote_cache),
        }
    except Exception as e:
        return {"enabled": False, "error": str(e)[:120]}
