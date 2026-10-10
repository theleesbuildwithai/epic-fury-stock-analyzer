"""
Regression tests for the /api/daily-summary watchlist leg.

Why this file exists: in production this leg returned [] on EVERY request.
The whole watchlist panel on the Daily Summary page was permanently empty
while the movers list directly above it rendered fine. Cause: the movers leg
falls back to CNBC when the Yahoo bulk download fails, and this leg did not —
it just returned []. Yahoo's bulk download fails reliably from the App Runner
IP, so "fallback missing" meant "always empty".

What is locked in here:
  - Yahoo dead -> CNBC fallback carries the full watchlist
  - CNBC symbol translation round-trips (BRK.B -> BRK-B)
  - fallback leaves week/month/RSI as None rather than fabricating them
    off a single quote
  - both sources dead -> [] and never a guessed row
  - a zero/None price from CNBC is dropped, never rendered
  - an empty result is not cached for the full TTL (one bad fetch must not
    blank the panel for five minutes)

No network: yfinance and cnbc_quote_batch are both monkeypatched.
Standalone script, no pytest.

Run:  python3 test_daily_summary_watchlist.py
"""
import sys

import analysis.extras as ex

_failures = []


def check(name, cond, extra=""):
    if cond:
        print("PASS  " + name)
    else:
        _failures.append(name)
        print("FAIL  " + name + ((" -> " + str(extra)) if extra else ""))


class _DeadYF:
    """Stands in for a Yahoo bulk endpoint that is blocked or rate-limited."""
    @staticmethod
    def download(*a, **k):
        raise RuntimeError("yahoo blocked")


def main_test():
    ex.yf = _DeadYF
    ex._throttle = lambda: None

    # --- Yahoo dead, CNBC healthy -----------------------------------------
    ex.cnbc_quote_batch = lambda syms: {
        s: {"price": 100.0 + i, "change_pct": 3.5 - i} for i, s in enumerate(syms)
    }
    rows = ex._get_watchlist_summary(["AAPL", "MSFT", "BRK-B"])

    check("yahoo dead -> CNBC fallback returns every name", len(rows) == 3, len(rows))
    check("CNBC symbol translation round-trips (BRK.B -> BRK-B)",
          {r["symbol"] for r in rows} == {"AAPL", "MSFT", "BRK-B"},
          {r["symbol"] for r in rows})
    check("fallback prices are populated and positive",
          all(isinstance(r["price"], float) and r["price"] > 0 for r in rows))
    check("fallback does NOT fabricate week/month/RSI from one quote",
          all(r["week_change_pct"] is None and r["month_change_pct"] is None
              and r["rsi"] is None for r in rows))
    check("fallback rows are flagged quote_only",
          all(r.get("quote_only") is True for r in rows))
    check("fallback derives a signal from the day move",
          rows[0]["signal"] == "Strong Day - Monitor", rows[0]["signal"])

    # --- History recovered from the warm last-good cache -------------------
    # A quote carries no history, so without this the "needs attention" sort
    # (RSI extremes) is dead for every name whenever Yahoo's bulk call fails,
    # which in production is always.
    import numpy as np
    import pandas as pd
    import analysis.quant_engine as qe

    saved_lastgood = qe._PRICE_DATA_LASTGOOD
    try:
        # A clean 40-bar uptrend ending near 100.
        up = np.linspace(80.0, 99.0, 40)
        qe._PRICE_DATA_LASTGOOD = {
            "AAPL": {"df": pd.DataFrame({"Close": up})},
            "SCRAMBLED": {"df": pd.DataFrame({"Close": up * 0.3})},  # wrong ticker's series
            "SHORT": {"df": pd.DataFrame({"Close": up[:5]})},        # not enough bars
        }

        d = ex._lastgood_derived("AAPL", 100.0)
        check("lastgood: RSI recovered from warm cache",
              isinstance(d["rsi"], float) and 0 <= d["rsi"] <= 100, d["rsi"])
        check("lastgood: uptrend reads overbought",
              d["rsi"] > 70, d["rsi"])
        check("lastgood: week/month change computed",
              isinstance(d["week_change_pct"], float)
              and isinstance(d["month_change_pct"], float), d)
        check("lastgood: returns anchored to the LIVE price, not the last close",
              d["week_change_pct"] > 0 and d["month_change_pct"] > 0, d)

        check("lastgood: scrambled series rejected by integrity gate",
              ex._lastgood_derived("SCRAMBLED", 100.0)
              == {"week_change_pct": None, "month_change_pct": None, "rsi": None})
        check("lastgood: too few bars -> no derived fields",
              ex._lastgood_derived("SHORT", 100.0)["rsi"] is None)
        check("lastgood: unknown ticker -> no derived fields",
              ex._lastgood_derived("NOPE", 100.0)["rsi"] is None)

        # End to end: Yahoo dead, CNBC alive, history filled from the cache.
        ex.cnbc_quote_batch = lambda syms: {s: {"price": 100.0, "change_pct": 0.4} for s in syms}
        rows2 = ex._get_watchlist_summary(["AAPL", "NOPE"])
        by = {r["symbol"]: r for r in rows2}
        check("fallback + warm cache -> AAPL row has RSI",
              by.get("AAPL", {}).get("rsi") is not None, by.get("AAPL"))
        check("fallback with no cached history -> fields stay None, row still shown",
              "NOPE" in by and by["NOPE"]["rsi"] is None, by.get("NOPE"))
        check("RSI extreme overrides the day-move signal",
              by["AAPL"]["signal"] == "Overbought - Consider Selling",
              by["AAPL"]["signal"])
    finally:
        qe._PRICE_DATA_LASTGOOD = saved_lastgood

    # --- Both sources dead -------------------------------------------------
    ex.cnbc_quote_batch = lambda syms: {}
    check("both sources dead -> [] and never a guessed row",
          ex._get_watchlist_summary(["AAPL"]) == [])

    # --- Corrupt quote values ---------------------------------------------
    ex.cnbc_quote_batch = lambda syms: {
        "AAPL": {"price": 0, "change_pct": 1},
        "MSFT": {"price": None, "change_pct": 1},
    }
    check("zero/None price dropped rather than rendered",
          ex._get_watchlist_summary(["AAPL", "MSFT"]) == [])

    # --- An empty result must not stick for the TTL ------------------------
    # Mirrors the call site in get_daily_summary: on an empty result the cache
    # entry is dropped so the next request retries instead of serving [].
    ex._extras_cache.clear()
    ex.cnbc_quote_batch = lambda syms: {}
    calls = [0]
    key = "daily_summary_wl:AAPL"

    def _fetch():
        calls[0] += 1
        return ex._get_watchlist_summary(["AAPL"])

    for _ in range(2):
        ex._get_cached(key, _fetch, ttl=300)
        if not ex._extras_cache.get(key, {}).get("data"):
            ex._extras_cache.pop(key, None)
    check("empty result not cached -> next request refetches", calls[0] == 2, calls[0])

    # A non-empty result SHOULD be cached, or we have just undone the
    # rate-limit fix this cache exists to provide.
    ex._extras_cache.clear()
    ex.cnbc_quote_batch = lambda syms: {s: {"price": 50.0, "change_pct": 0.1} for s in syms}
    calls[0] = 0
    for _ in range(3):
        out = ex._get_cached(key, _fetch, ttl=300)
        if not ex._extras_cache.get(key, {}).get("data"):
            ex._extras_cache.pop(key, None)
    check("non-empty result IS cached -> one upstream call for three requests",
          calls[0] == 1 and len(out) == 1, calls[0])

    print("\n" + ("ALL TESTS PASSED" if not _failures
                  else "FAILURES: " + ", ".join(_failures)))
    return 0 if not _failures else 1


if __name__ == "__main__":
    sys.exit(main_test())
