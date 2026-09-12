"""
Safety + shape tests for the /api/analyze-extras/{ticker} route.

This route feeds the analyze-page Quant Analytics section (earnings calendar +
reaction history). It is FAIL-ISOLATED by contract: it must ALWAYS return
HTTP 200 with a well-formed body and NEVER raise, so a Yahoo/earnings outage
can never break the analyze page.

Every upstream (past-earnings fetch + live yfinance next-date pull) is
monkeypatched — no network. Standalone script, no pytest, no httpx: the route
handler is called directly with a fake Request (rate-limit neutralized).

Guarantees locked in:
  - happy path: 200, earnings.{next_date, days_until, history[]} well-formed
  - history rows scrubbed: NaN surprise -> None, junk rows dropped
  - days_until derived correctly from next_date
  - total upstream failure -> 200 with earnings:null (never 500, never raises)
  - output is NaN/inf-clean everywhere
  - invalid ticker is handled gracefully (no 500)

Run:  python3 test_analyze_extras.py
"""
import sys
import math
from datetime import datetime, timedelta

import main

# Neutralize the per-IP rate limiter for the test (deterministic, no throttling).
main.check_rate_limit = lambda *a, **k: None

import predictions.earnings_drift as ed


class _FakeReq:
    """Minimal stand-in for a Starlette Request — only .client.host is read."""
    class _C:
        host = "127.0.0.1"
    client = _C()


def call(ticker):
    """Invoke the route handler directly (no ASGI/httpx)."""
    return main.analyze_extras(_FakeReq(), ticker)


class _Resp:
    """Adapts the dict return into a .status_code/.json() shape the asserts use.
    The handler contract is: always returns a dict (200), never raises."""
    def __init__(self, body):
        self._b = body
        self.status_code = 200

    def json(self):
        return self._b


class _Client:
    def get(self, path):
        # path like /api/analyze-extras/<ticker>
        from fastapi import HTTPException
        tk = path.rsplit("/", 1)[-1]
        try:
            return _Resp(call(tk))
        except HTTPException as he:  # graceful 4xx (e.g. bad ticker) — NOT a crash
            r = _Resp({"detail": he.detail})
            r.status_code = he.status_code
            return r
        except Exception as e:  # contract violation — surface as a failing "500"
            r = _Resp({"_raised": str(e)})
            r.status_code = 500
            return r


client = _Client()

_failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{('  -> ' + detail) if detail else ''}")
    if not cond:
        _failures.append(name)


def _all_finite(obj):
    if isinstance(obj, float):
        return math.isfinite(obj)
    if isinstance(obj, dict):
        return all(_all_finite(v) for v in obj.values())
    if isinstance(obj, list):
        return all(_all_finite(v) for v in obj)
    return True


def _bust_cache(ticker):
    # Drop both cache layers so each scenario computes fresh.
    try:
        main._analyze_cache.pop(f"extras:{ticker}", None)
    except Exception:
        pass
    try:
        from predictions.models import set_trading_state as _sts
        key = f"analyze_extras:{ticker}:{datetime.utcnow().strftime('%Y-%m-%d')}"
        _sts(key, "")
    except Exception:
        pass


def main_test():
    # ── 1. Happy path: past history present, a future date discoverable ──────────
    _bust_cache("AAPL")
    hist = [
        {"date": "2026-01-30", "surprise_pct": 5.1},
        {"date": "2025-10-30", "surprise_pct": -2.0},
        {"date": "2025-07-31", "surprise_pct": float("nan")},  # must scrub -> None
        {"date": None, "surprise_pct": 3.0},                    # junk -> dropped
    ]
    ed._fetch_earnings_dates = lambda t, lookback_quarters=8: list(hist)

    # Patch yfinance next-date pull via a fake module so _pull finds a future date.
    future = (datetime.now() + timedelta(days=12)).strftime("%Y-%m-%d")

    class _FakeDF:
        empty = False
        index = [datetime.strptime(future, "%Y-%m-%d")]

    class _FakeTk:
        earnings_dates = _FakeDF()
        calendar = {"Earnings Date": [datetime.strptime(future, "%Y-%m-%d")]}

    import types
    fake_yf = types.ModuleType("yfinance")
    fake_yf.Ticker = lambda t: _FakeTk()
    sys.modules["yfinance"] = fake_yf

    r = client.get("/api/analyze-extras/AAPL")
    check("happy: HTTP 200", r.status_code == 200, f"status={r.status_code}")
    body = r.json()
    check("happy: ticker echoed", body.get("ticker") == "AAPL", f"body={body}")
    e = body.get("earnings")
    check("happy: earnings object present", isinstance(e, dict), f"earnings={e}")
    if isinstance(e, dict):
        check("happy: next_date discovered", e.get("next_date") == future,
              f"next_date={e.get('next_date')} expected={future}")
        check("happy: days_until ~12", e.get("days_until") in (11, 12),
              f"days_until={e.get('days_until')}")
        rows = e.get("history") or []
        check("happy: junk row dropped, 3 valid rows", len(rows) == 3, f"rows={rows}")
        by_date = {x["date"]: x for x in rows}
        check("happy: NaN surprise scrubbed to None",
              by_date.get("2025-07-31", {}).get("surprise_pct") is None,
              f"row={by_date.get('2025-07-31')}")
        check("happy: valid surprise preserved",
              by_date.get("2026-01-30", {}).get("surprise_pct") == 5.1)
    check("happy: body NaN/inf-clean", _all_finite(body))

    # ── 2. No future date, history only -> next_date/days_until None, history kept ─
    _bust_cache("MSFT")
    ed._fetch_earnings_dates = lambda t, lookback_quarters=8: [
        {"date": "2026-01-15", "surprise_pct": 1.0}]

    class _EmptyDF:
        empty = True
        index = []

    class _NoFutureTk:
        earnings_dates = _EmptyDF()
        calendar = {}

    fake_yf.Ticker = lambda t: _NoFutureTk()
    r = client.get("/api/analyze-extras/MSFT")
    check("no-future: HTTP 200", r.status_code == 200)
    e = r.json().get("earnings")
    check("no-future: earnings present with null next_date",
          isinstance(e, dict) and e.get("next_date") is None and e.get("days_until") is None
          and len(e.get("history") or []) == 1,
          f"earnings={e}")

    # ── 3. Total upstream failure -> 200 with earnings:null (never raises) ───────
    _bust_cache("FAILX")

    def _boom(*a, **k):
        raise RuntimeError("earnings source down")

    ed._fetch_earnings_dates = _boom

    def _boom_ticker(t):
        raise RuntimeError("yahoo down")

    fake_yf.Ticker = _boom_ticker
    r = client.get("/api/analyze-extras/FAILX")
    check("failure: HTTP 200 (never 500)", r.status_code == 200, f"status={r.status_code}")
    body = r.json()
    check("failure: earnings is null", body.get("earnings") is None, f"body={body}")
    check("failure: ticker still echoed", body.get("ticker") == "FAILX")
    check("failure: body NaN/inf-clean", _all_finite(body))

    # ── 4. Weird ticker input handled gracefully (no 500) ───────────────────────
    _bust_cache("BRK-B")
    ed._fetch_earnings_dates = lambda t, lookback_quarters=8: []
    fake_yf.Ticker = lambda t: _NoFutureTk()
    for tk in ("brk.b", "aapl123", "zzzz"):
        r = client.get(f"/api/analyze-extras/{tk}")
        # Graceful handling = never a 500/crash. Either a 200 with a body, or a
        # clean 4xx from ticker validation — both are acceptable, neither raises.
        check(f"weird-ticker '{tk}' handled (no 500)",
              r.status_code != 500 and "_raised" not in r.json(),
              f"status={r.status_code} body={r.json()}")

    print("\n" + ("ALL TESTS PASSED" if not _failures else f"FAILURES: {_failures}"))
    return 0 if not _failures else 1


if __name__ == "__main__":
    sys.exit(main_test())
