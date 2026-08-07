"""
Regression tests for the QHF per-ticker price-safety system
(analysis.quant_engine.analyze_watchlist_stock).

These call the REAL function with the network data sources monkeypatched, so
they exercise production code directly (no logic mirror that can drift). They
lock in the "zero bogus signal / zero wrong price" guarantees:

  net1  price history diverges >25% from the live quote          -> withhold
  net2  intra-series glitch (>50% day / vs 5d median)            -> withhold
  net3  >20x split-corrupted range                               -> withhold
  net3b last bar >7 calendar days stale                          -> withhold
  anchor body corroborates live within 8% -> anchor last to live -> OK@live
        body does NOT corroborate live                           -> withhold
  net4  final price forced to within 2% of the live quote
  net6  no NaN/inf may appear anywhere in the published result

Run:  python3 test_qhf_price_safety.py   (no pytest needed)
"""
import sys
import math
import numpy as np
import pandas as pd

import analysis.quant_engine as q
import analytics.multi_source_adapter as msa
import analytics.data_shield as ds

# --- Neutralize everything except the code under test -------------------------
q._throttle = lambda: None
q._get_sector_with_fallback = lambda s: "Technology"
q.detect_market_regime = lambda: {"regime": "SIDEWAYS", "confidence": 50}
q.get_macro_overlay = lambda: {"sector_adjustments": {}}
# Force tier-2/3 fallbacks to contribute nothing so the controlled tier-1 frame
# is always the sole candidate — keeps each case deterministic.
msa.get_historical_any_source = lambda *a, **k: None
ds.safe_download = lambda *a, **k: None

_TRUSTED = {"px": None}


def _fake_quote_batch(symbols):
    px = _TRUSTED["px"]
    if px is None:
        return {}
    return {s: {"price": float(px), "change_pct": 0.0} for s in symbols}


msa.multi_source_quote_batch = _fake_quote_batch


def _make_df(closes, end_offset_days=0):
    n = len(closes)
    end = pd.Timestamp.now().normalize() - pd.Timedelta(days=end_offset_days)
    idx = pd.bdate_range(end=end, periods=n)
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "Open": closes,
            "High": closes * 1.005,
            "Low": closes * 0.995,
            "Close": closes,
            "Volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )


def _run(closes, trusted, end_offset_days=0):
    """Patch the trusted quote + tier-1 feed, clear cache, call the real fn."""
    _TRUSTED["px"] = trusted
    df = _make_df(closes, end_offset_days)
    q.yf.download = lambda *a, **k: df
    try:
        q._quant_cache.clear()
    except Exception:
        pass
    return q.analyze_watchlist_stock("TESTX")


def _all_finite(obj):
    if isinstance(obj, float):
        return math.isfinite(obj)
    if isinstance(obj, dict):
        return all(_all_finite(v) for v in obj.values())
    if isinstance(obj, list):
        return all(_all_finite(v) for v in obj)
    return True


# --- Assertions ---------------------------------------------------------------
_failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    if not cond:
        _failures.append(name)
    print(f"{status}  {name}{('  -> ' + detail) if detail else ''}")


def is_withheld(r):
    return (
        r.get("analyzed") is True
        and r.get("signal") == "HOLD"
        and r.get("direction") == "NEUTRAL"
        and r.get("confidence") == 40
        and r.get("data_quality") == "unreliable_price_data"
    )


def is_ok(r):
    return r.get("analyzed") is True and r.get("data_quality") != "unreliable_price_data"


# --- Cases --------------------------------------------------------------------
def main():
    body = list(np.linspace(360, 370, 120))

    # 1. V bug: clean body, last-row glitch to $400 vs live $364.57 -> anchor.
    r = _run(body[:-1] + [400.05], 364.57)
    check("V last-row glitch anchors to live", is_ok(r) and abs(r["price"] - 364.57) <= 0.01,
          f"price={r.get('price')} signal={r.get('signal')}")

    # 2. COST: correct data, legit signal preserved, price stays live.
    r = _run(list(np.linspace(955, 949, 120))[:-1] + [949.15], 944.96)
    check("COST clean data preserved", is_ok(r) and abs(r["price"] - 944.96) <= 0.01,
          f"price={r.get('price')} signal={r.get('signal')}")

    # 3. Bad single quote 15% off a clean series -> refuse to trust it.
    r = _run(body, 315.0)
    check("bad quote (uncorroborated) withheld", is_withheld(r), r.get("note", ""))

    # 4. Uniform ~9.6% offset (<25%) -> no unverified scaling, withhold.
    r = _run(list(np.linspace(430, 438, 120)), 400.0)
    check("uniform 9.6% offset withheld", is_withheld(r), r.get("note", ""))

    # 5. Stale series: body matches live but last bar is 30 days old.
    r = _run(body, 365.0, end_offset_days=30)
    check("stale 30d series withheld", is_withheld(r), r.get("note", ""))

    # 6. Fresh series (2d old) with matching quote -> scores.
    r = _run(body, 365.0, end_offset_days=2)
    check("fresh 2d series scores", is_ok(r) and abs(r["price"] - 365.0) <= 0.01,
          f"price={r.get('price')}")

    # 7. Split-corrupted 60x range -> withhold.
    r = _run([5.0] * 60 + [300.0] * 60, 300.0)
    check("split 60x corruption withheld", is_withheld(r), r.get("note", ""))

    # 8. net1: series 37% above the live quote -> withhold.
    r = _run([500.0] * 120, 364.0)
    check("net1 >25% divergence withheld", is_withheld(r), r.get("note", ""))

    # 9. net2 single-day glitch with NO trusted quote available -> withhold.
    r = _run(list(np.linspace(360, 365, 119)) + [600.0], None)
    check("net2 single-day glitch withheld", is_withheld(r), r.get("note", ""))

    # 10. No trusted quote, clean series -> scores on last close.
    r = _run(body, None)
    check("no trusted quote still scores", is_ok(r) and abs(r["price"] - body[-1]) <= 0.01,
          f"price={r.get('price')}")

    # 11. Insufficient history (<60 rows) -> not analyzed, explicit error.
    r = _run(list(np.linspace(360, 370, 30)), 365.0)
    check("insufficient history not analyzed", r.get("analyzed") is False and bool(r.get("error")),
          f"error={r.get('error')}")

    # 12. net6: every published number is finite (no NaN/Infinity) across cases.
    r = _run(body[:-1] + [400.05], 364.57)
    check("result has no NaN/inf (net6)", _all_finite(r))

    # 13. net7: mid-series bad-tick spike that reverts the next bar -> withhold.
    bad = list(np.linspace(360, 370, 120))
    bad[100] = 500.0  # spike inside the scoring window, reverts at bad[101]
    r = _run(bad, 365.0)
    check("net7 mid-series bad-tick withheld", is_withheld(r), r.get("note", ""))

    # 14. net7: frozen feed (13 identical consecutive closes) -> withhold.
    frozen = list(np.linspace(360, 370, 120))
    for i in range(95, 108):
        frozen[i] = 365.0
    r = _run(frozen, 365.0)
    check("net7 frozen feed withheld", is_withheld(r), r.get("note", ""))

    # 15. net7: unadjusted split (persistent x0.5 level shift) -> withhold.
    split = list(np.linspace(695, 705, 100)) + list(np.linspace(348, 352, 20))
    r = _run(split, 350.0)
    check("net7 unadjusted split withheld", is_withheld(r), r.get("note", ""))

    # 16. net7 low-FP: a LEGIT +23% earnings gap that PERSISTS must NOT be flagged.
    gap = list(np.linspace(300, 310, 100)) + list(np.linspace(378, 382, 20))
    r = _run(gap, 380.0)
    check("net7 persistent earnings gap NOT flagged", is_ok(r),
          f"signal={r.get('signal')} dq={r.get('data_quality')}")

    # --- Direct scanner unit checks on a REALISTIC (noisy) series, so every
    #     corruption signature is proven live on data that resembles a real feed
    #     (nonzero volatility) rather than being shadowed by another signature. ---
    rng = np.random.default_rng(0)
    real = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.012, 120)))  # ~1.2% daily vol

    clean = q._scan_price_corruption(real)
    check("net7 clean noisy series passes", clean.get("corrupt") is False, clean.get("reason", ""))

    frozen = real.copy()
    frozen[95:108] = frozen[95]  # 13 identical consecutive closes
    fz = q._scan_price_corruption(frozen)
    check("net7 frozen signature fires", fz.get("corrupt") and "frozen" in fz.get("reason", ""),
          fz.get("reason", ""))

    split = real.copy()
    split[100:] *= 0.5  # persistent unadjusted x0.5 split, no revert
    sp = q._scan_price_corruption(split)
    check("net7 split signature fires", sp.get("corrupt") and "split" in sp.get("reason", ""),
          sp.get("reason", ""))

    tick = real.copy()
    tick[100] *= 2.0  # single bad print that reverts next bar
    bt = q._scan_price_corruption(tick)
    check("net7 bad-tick signature fires", bt.get("corrupt") and "bad-tick" in bt.get("reason", ""),
          bt.get("reason", ""))

    print("\n" + ("ALL TESTS PASSED" if not _failures else f"FAILURES: {_failures}"))
    return 0 if not _failures else 1


if __name__ == "__main__":
    sys.exit(main())
