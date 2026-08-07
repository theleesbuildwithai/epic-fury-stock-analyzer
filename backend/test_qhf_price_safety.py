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
        q._quant_last_good.clear()  # isolate each case from anti-flap reuse
    except Exception:
        pass
    return q.analyze_watchlist_stock("TESTX")


def _run_with_df(df, trusted):
    """Same as _run but drives the engine with a pre-built DataFrame (lets a
    test control the DatetimeIndex and per-column values directly)."""
    _TRUSTED["px"] = trusted
    q.yf.download = lambda *a, **k: df
    try:
        q._quant_cache.clear()
        q._quant_last_good.clear()  # isolate each case from anti-flap reuse
    except Exception:
        pass
    return q.analyze_watchlist_stock("TESTX")


def _make_df_future(closes, days_ahead):
    """Build a DataFrame whose LAST bar is dated `days_ahead` in the future."""
    n = len(closes)
    end = pd.Timestamp.now().normalize() + pd.Timedelta(days=days_ahead)
    idx = pd.bdate_range(end=end, periods=n)
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {"Open": closes, "High": closes * 1.005, "Low": closes * 0.995,
         "Close": closes, "Volume": np.full(n, 1_000_000.0)},
        index=idx,
    )


def _make_df_vol(closes, volume):
    """Build a DataFrame with a caller-supplied (possibly zero) volume level."""
    df = _make_df(closes)
    df["Volume"] = float(volume)
    return df


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

    # A genuinely directional series whose NATURAL last bar already yields a
    # strong LONG call (moderate RSI, above EMAs, positive momentum). Using a
    # quote == the natural last close makes anchoring a no-op, so the ONLY
    # difference between the quote/no-quote runs is the NET 8 governor itself.
    rng2 = np.random.default_rng(3)
    updrift = list(100.0 * np.exp(np.cumsum(rng2.normal(0.004, 0.010, 120))))
    up_last = float(updrift[-1])

    # 17. NET 8 governor — NO trusted quote caps a directional call at exactly 75,
    #     while the IDENTICAL series WITH a corroborating quote is uncapped (94).
    #     Same direction, strictly lower confidence -> proves the governor bites.
    r_q = _run(updrift, up_last)   # quote == last -> no anchor move, uncapped
    r_nq = _run(updrift, None)     # no quote -> data-quality cap 75
    check("NET8 no-quote governor caps at <=75",
          is_ok(r_q) and is_ok(r_nq)
          and r_q.get("direction") == r_nq.get("direction") != "NEUTRAL"
          and r_q.get("confidence") > 75
          and r_nq.get("confidence") == 75
          and r_nq.get("confidence") < r_q.get("confidence"),
          f"quote_conf={r_q.get('confidence')} noquote_conf={r_nq.get('confidence')}")

    # 18. NET 8 governor — thin history (<120 rows) caps a directional call at <=80.
    thin_series = updrift[-90:]    # 90 rows, still an uptrend
    r = _run(thin_series, float(thin_series[-1]))
    check("NET8 thin-history governor caps at <=80",
          is_ok(r) and r.get("direction") != "NEUTRAL" and r.get("confidence") <= 80,
          f"conf={r.get('confidence')} dir={r.get('direction')}")

    # 19. Volume-integrity — a zero-volume feed neutralizes the smart-money read
    #     ("Volume data unavailable") and the governor caps confidence at <=80,
    #     yet a legit price shape is still allowed to score (not withheld).
    r = _run_with_df(_make_df_vol(updrift, 0.0), up_last)
    _sm = (r.get("factors", {}).get("smart_money", {}) or {}).get("label")
    check("volume-degenerate neutralized + governed",
          is_ok(r) and _sm == "Volume data unavailable"
          and (r.get("direction") == "NEUTRAL" or r.get("confidence") <= 80),
          f"smart_money={_sm} conf={r.get('confidence')} dir={r.get('direction')}")

    # 20. Date-index integrity — a scrambled index (duplicate date + descending
    #     order) is repaired (dedupe keep-last + sort) and scores the same as the
    #     clean series, rather than reading scrambled momentum/EMA.
    _clean_df = _make_df(updrift)
    _scr_df = pd.concat([_clean_df, _clean_df.iloc[[5]]]).iloc[::-1]  # dup + reversed
    r_clean = _run_with_df(_clean_df, up_last)
    r_scr = _run_with_df(_scr_df, up_last)
    check("date-index dedupe/sort repair matches clean",
          is_ok(r_scr) and r_scr.get("direction") == r_clean.get("direction")
          and abs((r_scr.get("price") or 0) - (r_clean.get("price") or 0)) <= 0.01,
          f"scr_dir={r_scr.get('direction')} clean_dir={r_clean.get('direction')} "
          f"scr_px={r_scr.get('price')} clean_px={r_clean.get('price')}")

    # 21. Future-dated bar — a last bar dated ahead of today is impossible for real
    #     market data (corrupt/misparsed feed) and must be withheld.
    r = _run_with_df(_make_df_future(updrift, 10), up_last)
    check("future-dated bar withheld", is_withheld(r), r.get("note", ""))

    # 22. TREND-CONTEXT accuracy — a CONFIRMED, OVERBOUGHT uptrend must NOT read
    #     HOLD. This is the "everything is HOLD" root cause (e.g. MSFT +30%/mo,
    #     RSI>70, top-of-band BB): mean-reversion must not cancel a real trend.
    up_ob = list(np.linspace(300, 400, 120))  # smooth strong uptrend -> RSI/BB hot
    r = _run(up_ob, 400.0)
    check("confirmed overbought uptrend is LONG (not HOLD)",
          is_ok(r) and r.get("direction") == "LONG" and r.get("signal") in ("BUY", "STRONG BUY"),
          f"signal={r.get('signal')} dir={r.get('direction')} score={r.get('composite_score')}")

    # 23. TREND-CONTEXT symmetry — a CONFIRMED, OVERSOLD downtrend must NOT be
    #     rescued into a BUY (falling knife). Oversold RSI/low BB inside a
    #     confirmed downtrend is trend continuation, not a dip to buy.
    dn_os = list(np.linspace(400, 300, 120))  # smooth strong downtrend -> RSI/BB cold
    r = _run(dn_os, 300.0)
    check("confirmed oversold downtrend is SHORT (not BUY-rescued)",
          is_ok(r) and r.get("direction") == "SHORT" and r.get("signal") in ("SELL", "STRONG SELL"),
          f"signal={r.get('signal')} dir={r.get('direction')} score={r.get('composite_score')}")

    # --- ANTI-FLAP: a transient upstream data outage must reuse the last
    #     validated signal (live-anchored) instead of dropping to HOLD — but ONLY
    #     when it is safe to do so. Each case seeds a validated LONG, then forces a
    #     withhold-triggering fetch while keeping _quant_last_good intact. ---

    # 24. Transient outage + stable live price -> REUSE the validated signal.
    q._quant_cache.clear(); q._quant_last_good.clear()
    r_seed = _run(updrift, up_last)                 # validated LONG -> stored last-good
    _TRUSTED["px"] = up_last                         # live quote unchanged (stable)
    q.yf.download = lambda *a, **k: _make_df([up_last * 1.6] * 120)  # corrupt -> net1 withhold
    q._quant_cache.clear()                           # expire 10-min cache, KEEP last-good
    r = q.analyze_watchlist_stock("TESTX")
    check("anti-flap reuses last validated signal on transient outage",
          r.get("data_quality") == "stale_reused"
          and r.get("direction") == r_seed.get("direction") != "NEUTRAL"
          and abs((r.get("price") or 0) - up_last) <= 0.01,
          f"dq={r.get('data_quality')} dir={r.get('direction')} px={r.get('price')}")

    # 25. Outage + MATERIAL (>10%) live move -> do NOT reuse; safe withhold.
    q._quant_cache.clear(); q._quant_last_good.clear()
    _run(updrift, up_last)                           # validated LONG @ up_last
    _TRUSTED["px"] = up_last * 1.30                  # live gapped +30% (material)
    q.yf.download = lambda *a, **k: _make_df([up_last * 3.0] * 120)  # corrupt -> withhold
    q._quant_cache.clear()
    r = q.analyze_watchlist_stock("TESTX")
    check("anti-flap does NOT reuse across a material (>10%) move", is_withheld(r),
          f"dq={r.get('data_quality')} note={r.get('note')}")

    # 26. Outage + NO trusted quote -> cannot verify price, so safe withhold.
    q._quant_cache.clear(); q._quant_last_good.clear()
    _run(updrift, up_last)                           # validated LONG stored
    _TRUSTED["px"] = None                            # live quote unavailable
    q.yf.download = lambda *a, **k: _make_df(list(np.linspace(360, 365, 119)) + [600.0])
    q._quant_cache.clear()
    r = q.analyze_watchlist_stock("TESTX")
    check("anti-flap requires a trusted quote (else safe withhold)", is_withheld(r),
          f"dq={r.get('data_quality')}")

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
