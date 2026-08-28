"""Standalone tests for the STB display-only fundamentals enrichment.

No pytest, no network. Monkeypatches the source adapter functions and asserts:
  - multi-tier fetch: finviz primary, stockanalysis backfill of valuation
  - validation drops out-of-bounds (corrupt) scrapes
  - fill-only: never overwrites existing good values
  - evidence reasons are built from real numbers
  - non-blocking launcher runs and completes; single-flight guard holds
  - fail-safe: never raises even when every source throws
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import analytics.multi_source_adapter as msa
from analysis import quant_engine as qe

# Keep the AWS-reachable fallback tiers (CNBC, Yahoo) OFF by default so the suite
# stays hermetic (no network). Individual tests opt in by reassigning these.
qe._stb_fetch_cnbc_fundamentals = lambda t: {}
qe._stb_fetch_yahoo_info = lambda t: {}

PASS = 0
FAIL = 0


def check(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}")


# ── 1. Multi-tier fetch: finviz rich + stockanalysis valuation backfill ──────
def test_multi_tier_fetch():
    print("\n[1] multi-tier fetch (finviz rich, stockanalysis backfill)")

    def fake_finviz(t):
        # finviz has quality/growth but MISSING pe/fwd_pe here
        return {
            "trailingPE": None, "forwardPE": None, "peg": 1.2,
            "roe": 28.0, "revenue_growth": 12.0, "earnings_growth": 18.0,
            "profit_margins": 22.0, "debt_equity": 0.4,
        }

    def fake_sa(t):
        return {"trailingPE": 19.5, "forwardPE": 17.2}

    orig_fv, orig_sa = msa.get_finviz_snapshot, msa.get_stockanalysis_fundamentals
    msa.get_finviz_snapshot = fake_finviz
    msa.get_stockanalysis_fundamentals = fake_sa
    try:
        d = qe._stb_fetch_fundamentals_multi("AAPL")
    finally:
        msa.get_finviz_snapshot = orig_fv
        msa.get_stockanalysis_fundamentals = orig_sa

    check(d["roe_pct"] == 28.0, "finviz ROE captured")
    check(d["revenue_growth_pct"] == 12.0, "finviz revenue growth captured")
    check(d["profit_margin_pct"] == 22.0, "finviz margin captured")
    check(d["debt_equity"] == 0.4, "finviz debt/equity captured")
    check(d["peg_ratio"] == 1.2, "finviz PEG captured")
    check(d["pe"] == 19.5, "stockanalysis backfilled trailing P/E")
    check(d["fwd_pe"] == 17.2, "stockanalysis backfilled forward P/E")


# ── 2. Validation drops corrupt scrapes ──────────────────────────────────────
def test_validation():
    print("\n[2] validation drops out-of-bounds values")

    def fake_finviz(t):
        return {
            "trailingPE": 99999.0,   # absurd -> dropped
            "forwardPE": 17.0,       # ok
            "peg": 1.0,
            "roe": -9999.0,          # absurd -> dropped
            "revenue_growth": 10.0,
            "earnings_growth": 5.0,
            "profit_margins": 20.0,
            "debt_equity": 0.5,
        }

    orig_fv, orig_sa = msa.get_finviz_snapshot, msa.get_stockanalysis_fundamentals
    msa.get_finviz_snapshot = fake_finviz
    msa.get_stockanalysis_fundamentals = lambda t: {}  # no backfill
    try:
        d = qe._stb_fetch_fundamentals_multi("XXX")
    finally:
        msa.get_finviz_snapshot = orig_fv
        msa.get_stockanalysis_fundamentals = orig_sa

    check(d["pe"] is None, "absurd P/E 99999 dropped")
    check(d["roe_pct"] is None, "absurd ROE -9999 dropped")
    check(d["fwd_pe"] == 17.0, "valid forward P/E kept")
    check(d["revenue_growth_pct"] == 10.0, "valid revenue growth kept")


# ── 3. Fill-only: never overwrites existing good values ──────────────────────
def test_fill_only():
    print("\n[3] enrichment fills None only, never overwrites")

    def fake_finviz(t):
        return {
            "trailingPE": 30.0, "forwardPE": 25.0, "peg": 2.0,
            "roe": 15.0, "revenue_growth": 9.0, "earnings_growth": 11.0,
            "profit_margins": 16.0, "debt_equity": 0.6,
        }

    picks = [{
        "ticker": "MSFT",
        "pe": 12.0,            # already set -> must NOT be overwritten
        "roe_pct": None,
        "revenue_growth_pct": None,
        "profit_margin_pct": None,
        "reasons": ["Existing reason"],
    }]

    orig_fv, orig_sa = msa.get_finviz_snapshot, msa.get_stockanalysis_fundamentals
    msa.get_finviz_snapshot = fake_finviz
    msa.get_stockanalysis_fundamentals = lambda t: {}
    qe._STB_ENRICH_CACHE.clear()
    try:
        qe._enrich_stb_fundamentals(picks)
    finally:
        msa.get_finviz_snapshot = orig_fv
        msa.get_stockanalysis_fundamentals = orig_sa

    p = picks[0]
    check(p["pe"] == 12.0, "existing P/E preserved (not overwritten)")
    check(p["roe_pct"] == 15.0, "None ROE filled from source")
    check(p["revenue_growth_pct"] == 9.0, "None revenue growth filled")
    check(any("ROE" in r for r in p["reasons"]), "evidence reason built from real number")
    check("Existing reason" in p["reasons"], "existing reason retained")


# ── 4. Non-blocking launcher completes; single-flight guard ──────────────────
def test_launcher():
    print("\n[4] daemon launcher runs, mutates in place, single-flight")

    def fake_finviz(t):
        return {
            "trailingPE": 20.0, "forwardPE": 18.0, "peg": 1.1,
            "roe": 25.0, "revenue_growth": 14.0, "earnings_growth": 20.0,
            "profit_margins": 25.0, "debt_equity": 0.3,
        }

    picks = [{
        "ticker": "NVDA", "pe": None, "roe_pct": None,
        "revenue_growth_pct": None, "profit_margin_pct": None, "reasons": [],
    }]

    orig_fv, orig_sa = msa.get_finviz_snapshot, msa.get_stockanalysis_fundamentals
    msa.get_finviz_snapshot = fake_finviz
    msa.get_stockanalysis_fundamentals = lambda t: {}
    qe._STB_ENRICH_CACHE.clear()
    qe._STB_ENRICH_INFLIGHT[0] = False
    try:
        qe._launch_stb_enrichment(picks)
        # wait for the daemon thread to finish (bounded)
        deadline = time.time() + 5
        while qe._STB_ENRICH_INFLIGHT[0] and time.time() < deadline:
            time.sleep(0.05)
    finally:
        msa.get_finviz_snapshot = orig_fv
        msa.get_stockanalysis_fundamentals = orig_sa

    check(qe._STB_ENRICH_INFLIGHT[0] is False, "single-flight guard reset after run")
    check(picks[0]["roe_pct"] == 25.0, "launcher mutated pick dict in place")
    check(picks[0]["pe"] == 20.0, "launcher filled P/E in place")


# ── 5. Fail-safe: every source throws -> no raise, fields stay None ──────────
def test_fail_safe():
    print("\n[5] fail-safe: sources throw -> never raises")

    def boom(t):
        raise RuntimeError("source down")

    picks = [{
        "ticker": "TSLA", "pe": None, "roe_pct": None,
        "revenue_growth_pct": None, "profit_margin_pct": None, "reasons": [],
    }]

    orig_fv, orig_sa = msa.get_finviz_snapshot, msa.get_stockanalysis_fundamentals
    msa.get_finviz_snapshot = boom
    msa.get_stockanalysis_fundamentals = boom
    qe._STB_ENRICH_CACHE.clear()
    raised = False
    try:
        qe._enrich_stb_fundamentals(picks)
    except Exception:
        raised = True
    finally:
        msa.get_finviz_snapshot = orig_fv
        msa.get_stockanalysis_fundamentals = orig_sa

    check(not raised, "enrichment never raised despite source failure")
    check(picks[0]["roe_pct"] is None, "fields stay None on total failure")


# ── 6. AWS-reachable fallback tiers: CNBC then Yahoo fill when finviz is blocked ──
def test_cnbc_yahoo_fallback():
    print("\n[6] CNBC + Yahoo fallback fill when finviz/stockanalysis are blocked")

    # Simulate the production case: finviz + stockanalysis return nothing (blocked
    # from the App Runner IP), so the CNBC + Yahoo tiers must carry the fields.
    orig = (msa.get_finviz_snapshot, msa.get_stockanalysis_fundamentals,
            qe._stb_fetch_cnbc_fundamentals, qe._stb_fetch_yahoo_info)
    msa.get_finviz_snapshot = lambda t: {}
    msa.get_stockanalysis_fundamentals = lambda t: {}
    # CNBC supplies valuation + quality (but not growth/PEG)
    qe._stb_fetch_cnbc_fundamentals = lambda t: {
        "pe": 20.1, "fwd_pe": 13.0, "roe_pct": 12.5,
        "profit_margin_pct": 9.3, "debt_equity": 0.16,
    }
    # Yahoo backfills the growth + PEG fields CNBC does not carry
    qe._stb_fetch_yahoo_info = lambda t: {
        "trailingPE": 99.0,           # must NOT overwrite CNBC's 20.1 (fill-only)
        "pegRatio": 1.4,
        "revenueGrowth": 0.11,        # decimal -> 11%
        "earningsGrowth": 0.20,       # decimal -> 20%
    }
    try:
        d = qe._stb_fetch_fundamentals_multi("XOM")
    finally:
        (msa.get_finviz_snapshot, msa.get_stockanalysis_fundamentals,
         qe._stb_fetch_cnbc_fundamentals, qe._stb_fetch_yahoo_info) = orig

    check(d["pe"] == 20.1, "CNBC P/E filled (finviz blocked)")
    check(d["fwd_pe"] == 13.0, "CNBC forward P/E filled")
    check(d["roe_pct"] == 12.5, "CNBC ROE filled")
    check(d["profit_margin_pct"] == 9.3, "CNBC margin filled")
    check(d["debt_equity"] == 0.16, "CNBC debt/equity filled")
    check(d["revenue_growth_pct"] == 11.0, "Yahoo revenue growth backfilled (decimal->pct)")
    check(d["earnings_growth_pct"] == 20.0, "Yahoo earnings growth backfilled")
    check(d["peg_ratio"] == 1.4, "Yahoo PEG backfilled")


if __name__ == "__main__":
    test_multi_tier_fetch()
    test_validation()
    test_fill_only()
    test_launcher()
    test_fail_safe()
    test_cnbc_yahoo_fallback()
    print(f"\n==== STB ENRICHMENT: {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)
