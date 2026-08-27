"""
Ad-hoc simulation: does the options desk (a) still surface picks after the new
data-integrity gates, and (b) produce DIVERSIFIED structures (long call, long put,
cash-secured put, covered call) — not "just buy calls"?

Zero network. Reuses the exact monkeypatch hooks from test_options_recommendation.py
(analyze_watchlist_stock / quote / fundamentals / macro / STB / chain / strike /
expiry / earnings) so it exercises the REAL build_options_recommendation gate stack.

Run:  python3 sim_options_diversity.py
"""
import sys
import types
from collections import Counter

import analysis.quant_engine as q
import analytics.multi_source_adapter as msa
import predictions.options_engine as oe
import predictions.options_recommendation as orx

# ── per-ticker mutable read (set before each build) ──────────────────────────
_CUR = {"signal": "HOLD", "conf": 60, "score": 0.0, "price": 100.0, "regime": "BULL"}
_NEWS = {"sentiment": "NEUTRAL", "article_count": 0}
_EARN = {"days": None}


def _fake_analyze(symbol):
    return {
        "signal": _CUR["signal"], "confidence": _CUR["conf"],
        "composite_score": _CUR["score"], "regime": _CUR["regime"],
        "price": _CUR["price"], "sector": "Technology",
        "data_quality": "ok",
        "technicals": {"ema_trend": "Bullish", "above_200sma": True, "above_50ema": True},
        "factors": {"rsi14": {"value": 55}, "momentum": {"value": 8.0},
                    "volatility": {"value": 25.0}, "volume_trend": {"label": "Rising"}},
    }


def _fake_quote_batch(symbols):
    return {s: {"price": float(_CUR["price"]), "change_pct": 0.0} for s in symbols}


def _fake_fundamentals(symbol):
    return {"trailingPE": 25, "forwardPE": 22, "eps": 6.0, "beta": 1.1,
            "dividendYield": 0.005, "marketCap": 2e12, "fiftyTwoWeekHigh": 120,
            "fiftyTwoWeekLow": 80, "_source": "sim"}


def _fake_nlp(symbol, *a, **k):
    return {"overall_sentiment": _NEWS["sentiment"],
            "overall_score": 0.4 if _NEWS["sentiment"] == "BULLISH" else (-0.4 if _NEWS["sentiment"] == "BEARISH" else 0.0),
            "confidence": 65, "article_count": _NEWS["article_count"]}


def _fake_cas():
    return {"risk_appetite": "RISK_ON"}


def _fake_stb(force=False):
    return {"long_picks": []}


def _fake_fetch_chain(symbol, *a, **k):
    return {"symbol": symbol, "chains": [{"expiry": "2026-09-18"}], "expiries": ["2026-09-18"]}


def _fake_select_expiration(chain_data, hold_days, sig):
    return "2026-09-18"


def _fake_select_strike(chain, px, opt_type, conf, score):
    return {"strike": float(round(px)), "premium": round(px * 0.05, 2), "dte": 42, "expiry": "2026-09-18",
            "delta_est": 0.55 if opt_type == "call" else -0.55, "iv": 0.30,
            "moneyness_label": "ATM", "volume": 800, "open_interest": 1200}


def _fake_next_earnings_days(symbol):
    return _EARN["days"]


_fake_ra = types.ModuleType("analysis.rentech_advanced")
_fake_ra.nlp_ticker_sentiment = _fake_nlp
sys.modules["analysis.rentech_advanced"] = _fake_ra

q.analyze_watchlist_stock = _fake_analyze
q.get_cross_asset_signals = _fake_cas
q.generate_fundamental_picks = _fake_stb
msa.multi_source_quote_batch = _fake_quote_batch
msa.get_fundamentals_any_source = _fake_fundamentals
oe.fetch_option_chain = _fake_fetch_chain
oe.select_expiration = _fake_select_expiration
oe.select_strike = _fake_select_strike
orx._next_earnings_days = _fake_next_earnings_days


# ── Universe: a realistic v59 HOLD-biased spread across ~40 names ─────────────
# (signal, score, conf, price, earnings_days) — mirrors what the live watchlist
# emits: mostly HOLD, a handful of explicit BUY, rare explicit SELL, and a band
# of HOLD names whose composite is clearly bearish (the tactical-put branch).
SCENARIOS = [
    # explicit BUYs (bullish → long call primary + CSP alt)
    ("STRONG BUY", 4.2, 83, 210, None),
    ("BUY",        2.4, 74, 150, None),
    ("BUY",        1.6, 70, 320, None),
    ("BUY",        1.2, 68, 45,  None),
    ("BUY",        3.1, 78, 610, None),
    # explicit SELLs (bearish → long put primary + covered-call alt)
    ("SELL",      -2.6, 75, 88,  None),
    ("STRONG SELL",-4.3, 84, 33, None),
    # HOLD but clearly bearish composite (tactical-put branch, score ≤ -1.0)
    ("HOLD",      -1.4, 60, 120, None),
    ("HOLD",      -2.1, 60, 260, None),
    ("HOLD",      -3.0, 60, 55,  None),
    ("HOLD",      -1.1, 60, 500, None),
    # HOLD bullish / neutral → correctly NO trade (never a put against an up move)
    ("HOLD",       0.6, 55, 190, None),
    ("HOLD",       0.0, 50, 140, None),
    ("HOLD",       0.4, 52, 77,  None),
    ("HOLD",      -0.5, 55, 300, None),   # mild bearish, above put threshold → no trade
    # data-integrity edge cases (should be withheld by the NEW gates)
    ("BUY",        2.0, 72, 0.4, None),   # sub-$1 underlying → price-bounds gate
    ("BUY",        2.0, 72, 99000, None), # absurd underlying → price-bounds gate
    ("BUY",        3.0, 80, 200, 2),      # earnings in 2d → IV-crush blackout withhold
    ("BUY",        3.0, 80, 200, 12),     # earnings in 12d → actionable + warning
    # low-conviction BUY under the floor → conviction gate
    ("BUY",        0.9, 51, 130, None),
]


def _primary_structure(rec):
    strat = rec.get("strategies") or []
    prim = next((s for s in strat if s.get("is_primary")), strat[0] if strat else None)
    return prim


def main():
    struct_counts = Counter()
    alt_counts = Counter()
    withheld = Counter()
    actionable = 0
    warned = 0
    rows = []

    for i, (sig, score, conf, px, earn) in enumerate(SCENARIOS):
        sym = f"SIM{i:02d}"
        _CUR.update(signal=sig, score=score, conf=conf, price=px, regime="BULL")
        _NEWS.update(sentiment=("BULLISH" if score > 0 else "BEARISH" if score < 0 else "NEUTRAL"),
                     article_count=(4 if abs(score) >= 1 else 0))
        _EARN["days"] = earn
        orx._last_good_rec.clear()

        rec = orx.build_options_recommendation(sym)
        act = rec.get("actionable")
        if act:
            actionable += 1
            prim = _primary_structure(rec) or {}
            strat = rec.get("strategies") or []
            alts = [s for s in strat if not s.get("is_primary")]
            pstruct = prim.get("structure") or rec.get("option_type")
            struct_counts[pstruct] += 1
            for a in alts:
                alt_counts[a.get("structure")] += 1
            earn_blk = rec.get("earnings") or {}
            if earn_blk.get("in_blackout") is False and earn_blk.get("next_earnings_days"):
                warned += 1
            rows.append((sym, sig, score, "ACTIONABLE", pstruct,
                         "+".join(a.get("structure") for a in alts) or "-",
                         rec.get("opportunity_tier"), rec.get("opportunity_score")))
        else:
            reason = (rec.get("reason") or "")[:48]
            withheld[reason] += 1
            rows.append((sym, sig, score, "withheld", reason, "-", "-", "-"))

    print("=" * 100)
    print(f"{'SYM':6} {'SIGNAL':11} {'SCORE':>6}  {'STATUS':10} {'PRIMARY':16} {'ALT':16} {'TIER':9} {'OPP':>3}")
    print("-" * 100)
    for r in rows:
        print(f"{r[0]:6} {r[1]:11} {r[2]:>6.1f}  {r[3]:10} {str(r[4]):16} {str(r[5]):16} {str(r[6]):9} {str(r[7]):>3}")
    print("=" * 100)
    print(f"\nUniverse scanned         : {len(SCENARIOS)}")
    print(f"ACTIONABLE picks         : {actionable}")
    print(f"Withheld                 : {sum(withheld.values())}")
    print(f"\nPRIMARY structure mix    : {dict(struct_counts)}")
    print(f"ALT structure mix        : {dict(alt_counts)}")
    print(f"Earnings-window warnings : {warned}")
    print(f"\nWithhold reasons:")
    for reason, n in withheld.most_common():
        print(f"   {n:2}x  {reason}")

    # Diversity verdict
    all_structs = set(struct_counts) | set(alt_counts)
    four = {"LONG_CALL", "LONG_PUT", "CASH_SECURED_PUT", "COVERED_CALL"}
    print(f"\nStructures reached       : {sorted(all_structs)}")
    print(f"All four reachable       : {four.issubset(all_structs)}")


if __name__ == "__main__":
    main()
