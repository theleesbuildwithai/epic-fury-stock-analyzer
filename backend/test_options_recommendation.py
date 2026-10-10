"""
Regression tests for the options recommendation layer
(predictions.options_recommendation.build_options_recommendation).

These call the REAL composition function with every upstream data source
monkeypatched (directional signal, trusted quote, fundamentals, news/NLP,
macro regime, STB picks, and the shared options_engine chain/strike/expiry
selectors). No network, no pytest — pure standalone script.

Guarantees locked in:
  BUY signal            -> CALL order ticket (BUY TO OPEN)
  SELL signal           -> PUT  order ticket
  HOLD / NEUTRAL        -> withheld (no conviction)
  confidence < floor    -> withheld
  empty option chain    -> withheld
  illiquid (no strike)  -> withheld
  analysis vs live px disagreement > tol -> withheld (corrupt-price guard)
  news strongly against a marginal signal -> withheld (news gate)
  order-ticket math: breakeven, max loss (= premium x 100), cost
  all order-ticket fields present on an actionable rec
  news / macro-regime / STB awareness surfaced on the rec
  output is NaN/inf-clean everywhere
  the function NEVER raises (returns actionable:false on any internal failure)

Run:  python3 test_options_recommendation.py
"""
import sys
import math
import types

# Import the REAL modules first (quant_engine needs the real analysis.rentech
# at load time), then inject a lightweight fake analysis.rentech_advanced so the
# layer's lazy NLP import resolves to deterministic sentiment (no heavy stack).
import analysis.quant_engine as q
import analytics.multi_source_adapter as msa
import predictions.options_engine as oe
import predictions.options_recommendation as orx

_NEWS = {"sentiment": "BULLISH", "article_count": 8}


def _fake_nlp(symbol, *a, **k):
    return {
        "overall_sentiment": _NEWS["sentiment"],
        "overall_score": 0.4 if _NEWS["sentiment"] == "BULLISH" else -0.4,
        "confidence": 70,
        "article_count": _NEWS["article_count"],
    }


_fake_ra = types.ModuleType("analysis.rentech_advanced")
_fake_ra.nlp_ticker_sentiment = _fake_nlp
sys.modules["analysis.rentech_advanced"] = _fake_ra

# ── Mutable test state ───────────────────────────────────────────────────────
_STATE = {"signal": "BUY", "conf": 80, "score": 5.0, "price": 100.0, "regime": "BULL",
          "raise": False}
_TRUSTED = {"px": 100.0}
_REGIME = {"risk_appetite": "RISK_ON"}
_STB = {"picks": [{"ticker": "TESTX", "fundamental_score": 85, "revenue_growth_pct": 20,
                   "roe_pct": 30, "peg_ratio": 1.2, "earnings_growth_pct": 25,
                   "momentum_12m_pct": 18, "sector": "Technology"}]}
_STRIKE = {"return_none": False}
_CHAIN = {"empty": False}
_EARN = {"days": None}        # None => no known earnings date (zero-API cache miss)
_PREMIUM = {"value": 5.00}    # controllable premium for the premium-sanity net


def _fake_analyze(symbol):
    if _STATE["raise"]:
        raise RuntimeError("boom")
    return {
        "signal": _STATE["signal"], "confidence": _STATE["conf"],
        "composite_score": _STATE["score"], "regime": _STATE["regime"],
        "price": _STATE["price"], "sector": "Technology",
        "technicals": {"ema_trend": "Bullish", "above_200sma": True, "above_50ema": True},
        "factors": {"rsi14": {"value": 55}, "momentum": {"value": 8.0},
                    "volatility": {"value": 25.0}, "volume_trend": {"label": "Rising"}},
    }


def _fake_quote_batch(symbols):
    px = _TRUSTED["px"]
    if px is None:
        return {}
    return {s: {"price": float(px), "change_pct": 0.0} for s in symbols}


def _fake_fundamentals(symbol):
    return {"trailingPE": 25, "forwardPE": 22, "eps": 6.0, "beta": 1.1,
            "dividendYield": 0.005, "marketCap": 2e12, "fiftyTwoWeekHigh": 120,
            "fiftyTwoWeekLow": 80, "_source": "test"}


def _fake_cas():
    return {"risk_appetite": _REGIME["risk_appetite"]}


def _fake_stb(force=False):
    return {"long_picks": list(_STB["picks"])}


def _fake_fetch_chain(symbol, *a, **k):
    if _CHAIN["empty"]:
        return {"symbol": symbol, "chains": [], "expiries": []}
    return {"symbol": symbol, "chains": [{"expiry": "2026-09-18"}], "expiries": ["2026-09-18"]}


def _fake_select_expiration(chain_data, hold_days, sig):
    return "2026-09-18"


def _fake_select_strike(chain, px, opt_type, conf, score):
    if _STRIKE["return_none"]:
        return None
    return {"strike": float(round(px)), "premium": _PREMIUM["value"], "dte": 42, "expiry": "2026-09-18",
            "delta_est": 0.55 if opt_type == "call" else -0.55, "iv": 0.30,
            "moneyness_label": "ATM", "volume": 800, "open_interest": 1200}


def _fake_next_earnings_days(symbol):
    return _EARN["days"]


# Entry-timing read. `grade=None` models the real-world cache miss (no cached
# close history for the name) -> the layer must behave as if there is no read.
_TIMING = {"grade": None, "score": 70.0, "ok": True}


def _fake_timing_read(symbol, ref_px, earn_days):
    g = _TIMING["grade"]
    if g is None:
        return None
    if not _TIMING["ok"]:
        return {"ok": False, "reason": "price_mismatch", "grade": "UNRATED",
                "timing_score": None, "entry_plan": None, "score_tilt": 0.0,
                "conf_tilt": 0, "signals": [], "grade_caps": []}
    return {
        "ok": True, "reason": "", "grade": g, "raw_grade": g,
        "timing_score": _TIMING["score"], "grade_caps": [], "signals": [],
        "bull_count": 8, "bear_count": 1, "coverage": 1.0,
        "extension_atr": 0.8, "near_term_risk_pct": 1.9,
        "entry_plan": {"ideal_entry": 99.5, "entry_zone_low": 98.0,
                       "entry_zone_high": 100.0, "structural_stop": 95.0,
                       "stop_pct": 5.0, "size_factor": 1.0, "note": "n/a"},
        "score_tilt": 5.0, "conf_tilt": 3,
        "event_risk": {"level": "LOW", "points": 0, "conf_penalty": 0, "items": []},
        "integrity": {"ok": True, "reason": "", "divergence_pct": 0.0},
        "summary": "test read",
    }


# ── Wire the fakes onto the real modules the layer imports lazily ────────────
q.analyze_watchlist_stock = _fake_analyze
q.get_cross_asset_signals = _fake_cas
q.generate_fundamental_picks = _fake_stb
msa.multi_source_quote_batch = _fake_quote_batch
msa.get_fundamentals_any_source = _fake_fundamentals
oe.fetch_option_chain = _fake_fetch_chain
oe.select_expiration = _fake_select_expiration
oe.select_strike = _fake_select_strike
orx._next_earnings_days = _fake_next_earnings_days  # zero-API earnings source (deterministic)
orx._timing_read = _fake_timing_read                # zero-API entry-timing source


def _reset():
    """Restore default happy-path state and clear the anti-flap cache so each
    case is isolated (a good cached rec must not mask a withhold assertion)."""
    _STATE.update(signal="BUY", conf=80, score=5.0, price=100.0, regime="BULL")
    _STATE["raise"] = False
    _TRUSTED["px"] = 100.0
    _REGIME["risk_appetite"] = "RISK_ON"
    _NEWS.update(sentiment="BULLISH", article_count=8)
    _STRIKE["return_none"] = False
    _CHAIN["empty"] = False
    _EARN["days"] = None
    _PREMIUM["value"] = 5.00
    _TIMING.update(grade=None, score=70.0, ok=True)
    orx._last_good_rec.clear()


def _build(sym="TESTX"):
    return orx.build_options_recommendation(sym)


def _all_finite(obj):
    if isinstance(obj, float):
        return math.isfinite(obj)
    if isinstance(obj, dict):
        return all(_all_finite(v) for v in obj.values())
    if isinstance(obj, list):
        return all(_all_finite(v) for v in obj)
    return True


_failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{('  -> ' + detail) if detail else ''}")
    if not cond:
        _failures.append(name)


def main():
    # 1. BUY -> CALL, actionable, correct order-ticket math.
    _reset()
    r = _build()
    check("BUY -> CALL actionable",
          r.get("actionable") is True and r.get("option_type") == "CALL"
          and r.get("action") == "BUY TO OPEN",
          f"actionable={r.get('actionable')} type={r.get('option_type')} reason={r.get('reason')}")
    check("CALL breakeven = strike + premium",
          abs((r.get("breakeven") or 0) - (r.get("strike") + r.get("est_premium_per_share"))) < 1e-6,
          f"be={r.get('breakeven')} strike={r.get('strike')} prem={r.get('est_premium_per_share')}")
    check("max loss = premium x 100 = total cost",
          r.get("max_loss") == r.get("est_total_cost") == round(r.get("est_premium_per_share") * 100, 2),
          f"max_loss={r.get('max_loss')} cost={r.get('est_total_cost')}")

    # 2. SELL -> PUT, breakeven = strike - premium.
    _reset(); _STATE["signal"] = "SELL"; _NEWS["sentiment"] = "BEARISH"
    r = _build()
    check("SELL -> PUT actionable",
          r.get("actionable") is True and r.get("option_type") == "PUT",
          f"type={r.get('option_type')} reason={r.get('reason')}")
    check("PUT breakeven = strike - premium",
          abs((r.get("breakeven") or 0) - (r.get("strike") - r.get("est_premium_per_share"))) < 1e-6,
          f"be={r.get('breakeven')}")

    # 3. HOLD (bullish score) -> withheld (never a put against an up move).
    _reset(); _STATE["signal"] = "HOLD"
    r = _build()
    check("HOLD (bullish score) withheld", r.get("actionable") is False, f"reason={r.get('reason')}")

    # 3b. HOLD name with a clearly BEARISH composite -> tactical defined-risk PUT
    #     (watchlist stays hold-biased; the options desk takes the bearish read).
    _reset(); _STATE["signal"] = "HOLD"; _STATE["score"] = -5.0; _STATE["conf"] = None
    _NEWS.update(sentiment="BEARISH", article_count=6)
    r = _build()
    check("HOLD + bearish score -> PUT actionable",
          r.get("actionable") is True and r.get("option_type") == "PUT"
          and r.get("direction") == "BEARISH",
          f"actionable={r.get('actionable')} type={r.get('option_type')} reason={r.get('reason')}")
    strats = {s.get("structure"): s for s in (r.get("strategies") or [])}
    check("HOLD-bearish yields long-put + covered-call (all four structures reachable)",
          "LONG_PUT" in strats and "COVERED_CALL" in strats,
          f"structures={list(strats)}")

    # 3c. HOLD name only MILDLY bearish (above the put floor) -> withheld (be sure).
    _reset(); _STATE["signal"] = "HOLD"; _STATE["score"] = -0.5; _STATE["conf"] = None
    r = _build()
    check("HOLD + mildly-bearish (above floor) withheld",
          r.get("actionable") is False, f"reason={r.get('reason')}")

    # 4. Low confidence -> withheld.
    _reset(); _STATE["conf"] = 40
    r = _build()
    check("low confidence withheld", r.get("actionable") is False, f"reason={r.get('reason')}")

    # 5. Illiquid (no strike) -> withheld.
    _reset(); _STRIKE["return_none"] = True
    r = _build()
    check("illiquid (no strike) withheld", r.get("actionable") is False, f"reason={r.get('reason')}")

    # 6. Empty chain -> withheld.
    _reset(); _CHAIN["empty"] = True
    r = _build()
    check("empty chain withheld", r.get("actionable") is False, f"reason={r.get('reason')}")

    # 7. Analysis vs live price disagreement -> withheld (corrupt-price guard).
    _reset(); _STATE["price"] = 100.0; _TRUSTED["px"] = 130.0
    r = _build()
    check("price disagreement withheld", r.get("actionable") is False,
          f"reason={r.get('reason')}")

    # 8. News gate: marginal signal + contradicting news -> withheld.
    _reset(); _STATE["conf"] = 60; _STATE["score"] = 2.0
    _NEWS.update(sentiment="BEARISH", article_count=6)  # bearish news vs a BUY(call)
    r = _build()
    check("news gate withholds marginal-vs-news", r.get("actionable") is False,
          f"reason={r.get('reason')}")

    # 9. All order-ticket fields present on an actionable rec.
    _reset()
    r = _build()
    required = ["ticker", "actionable", "underlying_price", "signal", "direction",
               "confidence", "action", "option_type", "strike", "expiration",
               "expiration_human", "dte", "contracts", "contract_label", "occ_symbol",
               "est_premium_per_share", "est_cost_per_contract", "est_total_cost",
               "breakeven", "max_loss", "max_loss_pct", "delta", "iv_pct", "moneyness",
               "volume", "open_interest", "rationale", "order_instructions",
               "risk_disclosures", "technical_analysis", "fundamental_analysis",
               "fundamental_alignment", "news_analysis", "news_alignment",
               "macro_regime", "macro_alignment", "stb_context", "signal_confluence",
               "confirmations", "options_analytics", "safety", "strategies",
               "strategy_count", "opportunity_score", "opportunity_tier", "earnings"]
    missing = [k for k in required if k not in r]
    check("all order-ticket fields present", not missing, f"missing={missing}")
    check("order_instructions non-empty list",
          isinstance(r.get("order_instructions"), list) and len(r["order_instructions"]) >= 4)
    check("risk_disclosures non-empty list",
          isinstance(r.get("risk_disclosures"), list) and len(r["risk_disclosures"]) >= 4)
    check("occ symbol well-formed",
          isinstance(r.get("occ_symbol"), str) and r["occ_symbol"].startswith("TESTX")
          and ("C" in r["occ_symbol"]),
          f"occ={r.get('occ_symbol')}")

    # 10. News / macro / STB awareness surfaced and aligned.
    check("news awareness surfaced (AGREES)",
          r.get("news_alignment") == "AGREES" and (r.get("news_analysis") or {}).get("sentiment") == "BULLISH",
          f"news_alignment={r.get('news_alignment')}")
    check("macro regime surfaced + supports",
          r.get("macro_regime") == "RISK_ON" and r.get("macro_alignment") == "SUPPORTS",
          f"regime={r.get('macro_regime')} align={r.get('macro_alignment')}")
    check("STB awareness surfaced",
          (r.get("stb_context") or {}).get("in_stb_longs") is True
          and (r.get("stb_context") or {}).get("revenue_growth_pct") == 20,
          f"stb={r.get('stb_context')}")
    # Denominator is 6 on the bullish side: technical, fundamental, news, macro,
    # STB-long and entry timing. Timing is unavailable in this harness unless the
    # fake read is installed, so it correctly does NOT count as a confirmation.
    check("signal confluence counts all confirmations",
          r.get("signal_confluence") == "5/6" and "STB-long" in (r.get("confirmations") or []),
          f"confluence={r.get('signal_confluence')} confirms={r.get('confirmations')}")

    # 10b. Multi-strategy: BUY -> [LONG_CALL primary, CASH_SECURED_PUT alt].
    _reset()
    r = _build()
    strats = r.get("strategies") or []
    by_struct = {s.get("structure"): s for s in strats}
    check("BUY yields long-call + cash-secured-put",
          "LONG_CALL" in by_struct and "CASH_SECURED_PUT" in by_struct,
          f"structures={list(by_struct)}")
    lc = by_struct.get("LONG_CALL", {})
    check("LONG_CALL primary + defined risk",
          lc.get("is_primary") is True and lc.get("risk_type") == "DEFINED"
          and lc.get("action") == "BUY TO OPEN" and lc.get("net_type") == "DEBIT",
          f"lc={ {k: lc.get(k) for k in ('is_primary','risk_type','action','net_type')} }")
    check("LONG_CALL economics (debit/max_loss/breakeven)",
          lc.get("net_debit") == 500.0 and lc.get("max_loss") == 500.0
          and abs((lc.get("breakeven") or 0) - 105.0) < 1e-6 and lc.get("max_gain") is None,
          f"debit={lc.get('net_debit')} loss={lc.get('max_loss')} be={lc.get('breakeven')} gain={lc.get('max_gain')}")
    csp = by_struct.get("CASH_SECURED_PUT", {})
    check("CASH_SECURED_PUT collateralized economics",
          csp.get("risk_type") == "COLLATERALIZED" and csp.get("action") == "SELL TO OPEN"
          and csp.get("net_type") == "CREDIT" and csp.get("net_credit") == 500.0
          and csp.get("collateral_required") == 10000.0 and csp.get("requires_shares") == 0
          and csp.get("max_loss") == 9500.0 and abs((csp.get("breakeven") or 0) - 95.0) < 1e-6
          and csp.get("max_gain") == 500.0,
          f"csp={ {k: csp.get(k) for k in ('net_credit','collateral_required','max_loss','breakeven','max_gain')} }")

    # 10c. Multi-strategy: SELL -> [LONG_PUT primary, COVERED_CALL alt].
    _reset(); _STATE["signal"] = "SELL"; _NEWS["sentiment"] = "BEARISH"
    r = _build()
    strats = r.get("strategies") or []
    by_struct = {s.get("structure"): s for s in strats}
    check("SELL yields long-put + covered-call",
          "LONG_PUT" in by_struct and "COVERED_CALL" in by_struct,
          f"structures={list(by_struct)}")
    lp = by_struct.get("LONG_PUT", {})
    check("LONG_PUT economics (debit/max_gain/breakeven)",
          lp.get("risk_type") == "DEFINED" and lp.get("net_debit") == 500.0
          and lp.get("max_loss") == 500.0 and lp.get("max_gain") == 9500.0
          and abs((lp.get("breakeven") or 0) - 95.0) < 1e-6,
          f"lp={ {k: lp.get(k) for k in ('net_debit','max_loss','max_gain','breakeven')} }")
    cc = by_struct.get("COVERED_CALL", {})
    check("COVERED_CALL requires shares + collateralized",
          cc.get("risk_type") == "COLLATERALIZED" and cc.get("action") == "SELL TO OPEN"
          and cc.get("net_type") == "CREDIT" and cc.get("requires_shares") == 100
          and cc.get("collateral_required") == 0.0 and cc.get("net_credit") == 500.0
          and abs((cc.get("breakeven") or 0) - 95.0) < 1e-6,
          f"cc={ {k: cc.get(k) for k in ('requires_shares','collateral_required','net_credit','breakeven')} }")

    # 10d. Capital-preservation invariant: NEVER a naked/unlimited-risk structure.
    _reset()
    all_ok = True
    for sig, news in (("BUY", "BULLISH"), ("SELL", "BEARISH")):
        _reset(); _STATE["signal"] = sig; _NEWS["sentiment"] = news
        rr = _build()
        for s in (rr.get("strategies") or []):
            if s.get("structure") not in ("LONG_CALL", "LONG_PUT", "CASH_SECURED_PUT", "COVERED_CALL"):
                all_ok = False
            if s.get("risk_type") not in ("DEFINED", "COLLATERALIZED"):
                all_ok = False
    check("no naked/unlimited-risk structure ever emitted", all_ok)
    _reset(); r = _build()
    check("safety flags naked_short_blocked",
          (r.get("safety") or {}).get("naked_short_blocked") is True
          and (r.get("safety") or {}).get("collateralized_selling_only") is True)
    check("every strategy order/risk lists populated",
          all(isinstance(s.get("order_instructions"), list) and len(s["order_instructions"]) >= 4
              and isinstance(s.get("risk_disclosures"), list) and len(s["risk_disclosures"]) >= 3
              for s in (r.get("strategies") or [])),
          f"count={len(r.get('strategies') or [])}")

    # 10e. Earnings / IV-crush guard — blackout (≤ 3d) WITHHOLDS the long primary.
    _reset(); _EARN["days"] = 2
    r = _build()
    check("earnings blackout (≤3d) withholds long-debit",
          r.get("actionable") is False and "earnings" in (r.get("reason") or "").lower()
          and (r.get("earnings") or {}).get("in_blackout") is True,
          f"reason={r.get('reason')} earnings={r.get('earnings')}")

    # 10f. Earnings mid-window (4-21d) WARNS but does NOT withhold; surfaces the date.
    _reset(); _EARN["days"] = 10
    r = _build()
    check("earnings mid-window warns (actionable), surfaces date",
          r.get("actionable") is True and (r.get("earnings") or {}).get("next_earnings_days") == 10
          and (r.get("earnings") or {}).get("in_blackout") is False,
          f"actionable={r.get('actionable')} earnings={r.get('earnings')}")
    check("earnings mid-window adds an EARNINGS risk disclosure",
          any("EARNINGS in 10 days" in d for d in (r.get("risk_disclosures") or [])),
          f"disclosures={r.get('risk_disclosures')}")

    # 10g. No known earnings date -> no blackout, earnings block None, still actionable.
    _reset(); _EARN["days"] = None
    r = _build()
    check("no earnings date -> actionable, earnings block None",
          r.get("actionable") is True and r.get("earnings") is None,
          f"earnings={r.get('earnings')}")

    # 10h. Opportunity score + tier present, 0-100, high for a strong/liquid/confluent BUY.
    _reset()
    r = _build()
    osc = r.get("opportunity_score"); otier = r.get("opportunity_tier")
    check("opportunity score present + 0-100",
          isinstance(osc, int) and 0 <= osc <= 100, f"score={osc}")
    check("opportunity tier valid",
          otier in ("PRIME", "STRONG", "MODERATE"), f"tier={otier}")
    check("strong/liquid/5-confluence BUY scores high (PRIME/STRONG)",
          otier in ("PRIME", "STRONG") and osc >= 55, f"score={osc} tier={otier}")

    # 10i. Opportunity score DROPS for a weaker, lower-conviction, less-confluent trade.
    _reset(); _STATE["conf"] = 60; _STATE["score"] = 1.2
    _NEWS.update(sentiment="NEUTRAL", article_count=0); _REGIME["risk_appetite"] = "NEUTRAL"
    r_weak = _build()
    check("weaker trade scores lower than the strong one",
          isinstance(r_weak.get("opportunity_score"), int)
          and r_weak["opportunity_score"] < osc,
          f"weak={r_weak.get('opportunity_score')} strong={osc}")

    # 10j. Absolute price bounds — sub-$1 underlying withheld (corrupt-price guard).
    _reset(); _STATE["price"] = 0.40; _TRUSTED["px"] = 0.40
    r = _build()
    check("sub-$1 underlying withheld (price bounds)",
          r.get("actionable") is False and "bounds" in (r.get("reason") or "").lower(),
          f"reason={r.get('reason')}")

    # 10k. Absolute price bounds — absurdly high underlying withheld.
    _reset(); _STATE["price"] = 20000.0; _TRUSTED["px"] = 20000.0
    r = _build()
    check("absurd-high underlying withheld (price bounds)",
          r.get("actionable") is False and "bounds" in (r.get("reason") or "").lower(),
          f"reason={r.get('reason')}")

    # 10l. Premium sanity — premium ≥ underlying withheld (corrupt-quote guard).
    _reset(); _PREMIUM["value"] = 150.0  # premium > $100 underlying = absurd
    r = _build()
    check("premium ≥ underlying withheld (premium sanity)",
          r.get("actionable") is False and "premium" in (r.get("reason") or "").lower(),
          f"reason={r.get('reason')}")

    # 11. Output is NaN/inf-clean everywhere.
    _reset()
    r = _build()
    check("actionable rec is NaN/inf-clean", _all_finite(r))

    # 12. Never raises: force the signal engine to throw -> actionable:false dict.
    _reset(); _STATE["raise"] = True
    try:
        r = _build()
        raised = False
    except Exception as e:
        r, raised = None, True
    check("never raises on internal failure",
          (not raised) and isinstance(r, dict) and r.get("actionable") is False,
          f"raised={raised} reason={(r or {}).get('reason')}")

    # 13. Withheld payloads are also NaN/inf-clean.
    _reset(); _STATE["signal"] = "HOLD"
    r = _build()
    check("withheld rec is NaN/inf-clean", _all_finite(r))

    # ════════════════════════════════════════════════════════════════════════
    # 14. ENTRY TIMING + ALL-FOUR-STRUCTURE SCORING
    # ════════════════════════════════════════════════════════════════════════

    # 14a. All four structures are always offered (both legs of the chain are
    #      already selected, so the full menu costs nothing extra).
    _reset()
    r = _build()
    structs = {s.get("structure") for s in (r.get("strategies") or [])}
    check("all four structures offered on a BUY",
          structs == {"LONG_CALL", "LONG_PUT", "CASH_SECURED_PUT", "COVERED_CALL"},
          f"structs={sorted(structs)}")
    _reset(); _STATE["signal"] = "SELL"; _NEWS["sentiment"] = "BEARISH"
    r = _build()
    structs = {s.get("structure") for s in (r.get("strategies") or [])}
    check("all four structures offered on a SELL",
          structs == {"LONG_CALL", "LONG_PUT", "CASH_SECURED_PUT", "COVERED_CALL"},
          f"structs={sorted(structs)}")

    # 14b. Every structure carries its OWN score/tier, and alignment is honest:
    #      on a bullish read only the two up-structures are flagged aligned.
    _reset()
    r = _build()
    strats = r.get("strategies") or []
    check("every structure scored 0-100 with a tier",
          all(isinstance(s.get("structure_score"), int)
              and 0 <= s["structure_score"] <= 100
              and s.get("structure_tier") in ("PRIME", "STRONG", "MODERATE", "WEAK")
              for s in strats),
          f"scores={[(s['structure'], s.get('structure_score'), s.get('structure_tier')) for s in strats]}")
    aligned = {s["structure"] for s in strats if s.get("is_aligned")}
    check("bullish read marks only the up-structures aligned",
          aligned == {"LONG_CALL", "CASH_SECURED_PUT"}, f"aligned={sorted(aligned)}")
    check("primary is the long call on a BUY",
          [s["structure"] for s in strats if s.get("is_primary")] == ["LONG_CALL"],
          f"primary={[s['structure'] for s in strats if s.get('is_primary')]}")
    check("counter-view structures score below the desk call",
          max((s["structure_score"] for s in strats if s.get("is_aligned")), default=0)
          > max((s["structure_score"] for s in strats if not s.get("is_aligned")), default=0),
          f"scores={[(s['structure'], s['structure_score']) for s in strats]}")
    check("strategies sorted best-structure-first",
          all(strats[i]["structure_score"] >= strats[i + 1]["structure_score"]
              for i in range(len(strats) - 1)),
          f"order={[(s['structure'], s['structure_score']) for s in strats]}")
    check("best_structure mirrors the top-scored strategy",
          r.get("best_structure") == strats[0]["structure"]
          and r.get("best_structure_score") == strats[0]["structure_score"],
          f"best={r.get('best_structure')} top={strats[0]['structure']}")

    # 14c. Bearish read flips alignment to the down-structures.
    _reset(); _STATE["signal"] = "SELL"; _NEWS["sentiment"] = "BEARISH"
    r = _build()
    aligned = {s["structure"] for s in (r.get("strategies") or []) if s.get("is_aligned")}
    check("bearish read marks only the down-structures aligned",
          aligned == {"LONG_PUT", "COVERED_CALL"}, f"aligned={sorted(aligned)}")

    # 14d. HARD timing conflict: a call into an AVOID tape is withheld outright,
    #      no matter how confident the directional signal is. This is the single
    #      most important new gate — paying premium against the tape.
    _reset(); _STATE["conf"] = 95; _STATE["score"] = 6.0; _TIMING["grade"] = "AVOID"
    r = _build()
    check("AVOID tape withholds a long CALL even at max conviction",
          r.get("actionable") is False and "tape" in (r.get("reason") or "").lower(),
          f"actionable={r.get('actionable')} reason={r.get('reason')}")
    check("hard-conflict withhold still surfaces the timing read",
          (r.get("entry_timing") or {}).get("grade") == "AVOID"
          and r.get("timing_alignment") == "CONFLICTS",
          f"timing={r.get('timing_alignment')}")

    # 14e. HARD timing conflict, mirror case: a put into a BUY_NOW tape.
    _reset(); _STATE["signal"] = "SELL"; _NEWS["sentiment"] = "BEARISH"
    _STATE["conf"] = 95; _STATE["score"] = -6.0; _TIMING["grade"] = "BUY_NOW"
    r = _build()
    check("BUY_NOW tape withholds a long PUT even at max conviction",
          r.get("actionable") is False and "tape" in (r.get("reason") or "").lower(),
          f"actionable={r.get('actionable')} reason={r.get('reason')}")

    # 14f. SOFT conflict (WAIT tape for a call) only vetoes a MARGINAL signal —
    #      a strong signal survives with a caution, mirroring the other gates.
    _reset(); _STATE["conf"] = 58; _STATE["score"] = 1.5; _TIMING["grade"] = "WAIT"
    _NEWS.update(sentiment="NEUTRAL", article_count=0)
    r = _build()
    check("WAIT tape + marginal conviction withholds",
          r.get("actionable") is False and "poor" in (r.get("reason") or "").lower(),
          f"actionable={r.get('actionable')} reason={r.get('reason')}")
    _reset(); _STATE["conf"] = 90; _STATE["score"] = 5.5; _TIMING["grade"] = "WAIT"
    r = _build()
    check("WAIT tape + strong conviction stays actionable with a caution",
          r.get("actionable") is True and r.get("timing_alignment") == "CONFLICTS"
          and any("short-term tape does not support" in d
                  for d in (r.get("risk_disclosures") or [])),
          f"actionable={r.get('actionable')} align={r.get('timing_alignment')}")

    # 14g. Supporting tape adds a confirmation, lifts the score, and names the
    #      technical invalidation level so the option is not held to zero.
    _reset(); _TIMING["grade"] = "BUY_NOW"
    r_sup = _build()
    _reset(); _TIMING["grade"] = None
    r_none = _build()
    check("supporting tape counts as a confluence confirmation",
          "timing" in (r_sup.get("confirmations") or [])
          and r_sup.get("signal_confluence") == "6/6"
          and "timing" not in (r_none.get("confirmations") or []),
          f"sup={r_sup.get('signal_confluence')} none={r_none.get('signal_confluence')}")
    check("supporting tape lifts the opportunity score",
          r_sup.get("opportunity_score") > r_none.get("opportunity_score"),
          f"sup={r_sup.get('opportunity_score')} none={r_none.get('opportunity_score')}")
    check("supporting tape surfaces the technical invalidation level",
          any("technical invalidation" in d.lower() for d in (r_sup.get("risk_disclosures") or [])),
          f"disclosures={r_sup.get('risk_disclosures')}")

    # 14h. Timing fit is structure-specific — this is the whole point of scoring
    #      each structure separately. On a BUY_NOW tape the long call must beat
    #      the covered call (which caps the very move the tape is predicting);
    #      on a WAIT tape that ordering must reverse.
    _reset(); _TIMING["grade"] = "BUY_NOW"
    r = _build()
    fits = {s["structure"]: s.get("timing_fit") for s in (r.get("strategies") or [])}
    check("BUY_NOW tape: long call fits better than covered call",
          fits.get("LONG_CALL", 0) > fits.get("COVERED_CALL", 99), f"fits={fits}")
    check("BUY_NOW tape: long put is the worst fit",
          fits.get("LONG_PUT", 99) == min(fits.values()), f"fits={fits}")
    _reset(); _TIMING["grade"] = "WAIT"; _STATE["conf"] = 90; _STATE["score"] = 5.5
    r = _build()
    fits = {s["structure"]: s.get("timing_fit") for s in (r.get("strategies") or [])}
    check("WAIT tape: covered call fits better than long call",
          fits.get("COVERED_CALL", 0) > fits.get("LONG_CALL", 99), f"fits={fits}")

    # 14i. Short premium prefers RICH vol; long premium prefers CHEAP vol. The
    #      two must move in OPPOSITE directions as IV rises, or the desk would
    #      be ranking premium sales with the buyer's formula.
    _reset(); _TIMING["grade"] = "ACCUMULATE"
    _base_strike = _fake_select_strike

    def _iv_strike(iv):
        def f(chain, px, opt_type, conf, score):
            p = _base_strike(chain, px, opt_type, conf, score)
            if p:
                p["iv"] = iv
            return p
        return f

    try:
        oe.select_strike = _iv_strike(0.18)            # LOW IV
        low = {s["structure"]: s["structure_score"] for s in (_build().get("strategies") or [])}
        orx._last_good_rec.clear()
        oe.select_strike = _iv_strike(0.70)            # ELEVATED IV
        high = {s["structure"]: s["structure_score"] for s in (_build().get("strategies") or [])}
    finally:
        oe.select_strike = _base_strike
    check("rich IV scores the cash-secured put HIGHER",
          high.get("CASH_SECURED_PUT", 0) > low.get("CASH_SECURED_PUT", 99),
          f"low={low.get('CASH_SECURED_PUT')} high={high.get('CASH_SECURED_PUT')}")
    check("rich IV scores the long call LOWER",
          high.get("LONG_CALL", 99) < low.get("LONG_CALL", 0),
          f"low={low.get('LONG_CALL')} high={high.get('LONG_CALL')}")

    # 14j. Price-integrity failure (cached series disagrees with the live quote)
    #      must behave exactly like NO read: never rewarded, never punished.
    _reset(); _TIMING.update(grade="BUY_NOW", ok=False)
    r = _build()
    check("unverified timing read is treated as neutral, not bullish",
          r.get("actionable") is True and r.get("timing_alignment") == "NEUTRAL"
          and "timing" not in (r.get("confirmations") or []),
          f"align={r.get('timing_alignment')} confirms={r.get('confirmations')}")

    # 14k. The timing layer never makes the payload unsafe to render.
    for g in ("BUY_NOW", "ACCUMULATE", "WAIT", "AVOID", None):
        _reset(); _TIMING["grade"] = g; _STATE["conf"] = 90; _STATE["score"] = 5.5
        rr = _build()
        if not _all_finite(rr):
            check(f"timing payload NaN-clean (grade={g})", False)
            break
    else:
        check("timing payload NaN/inf-clean across every grade", True)
    _reset()

    print("\n" + ("ALL TESTS PASSED" if not _failures else f"FAILURES: {_failures}"))
    return 0 if not _failures else 1


if __name__ == "__main__":
    sys.exit(main())
