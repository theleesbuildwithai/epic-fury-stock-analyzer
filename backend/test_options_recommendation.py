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
    return {"strike": float(round(px)), "premium": 5.00, "dte": 42, "expiry": "2026-09-18",
            "delta_est": 0.55 if opt_type == "call" else -0.55, "iv": 0.30,
            "moneyness_label": "ATM", "volume": 800, "open_interest": 1200}


# ── Wire the fakes onto the real modules the layer imports lazily ────────────
q.analyze_watchlist_stock = _fake_analyze
q.get_cross_asset_signals = _fake_cas
q.generate_fundamental_picks = _fake_stb
msa.multi_source_quote_batch = _fake_quote_batch
msa.get_fundamentals_any_source = _fake_fundamentals
oe.fetch_option_chain = _fake_fetch_chain
oe.select_expiration = _fake_select_expiration
oe.select_strike = _fake_select_strike


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

    # 3. HOLD -> withheld (no conviction).
    _reset(); _STATE["signal"] = "HOLD"
    r = _build()
    check("HOLD withheld", r.get("actionable") is False, f"reason={r.get('reason')}")

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
               "confirmations", "options_analytics", "safety"]
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
    check("signal confluence counts all confirmations",
          r.get("signal_confluence") == "5/5" and "STB-long" in (r.get("confirmations") or []),
          f"confluence={r.get('signal_confluence')} confirms={r.get('confirmations')}")

    # 11. Output is NaN/inf-clean everywhere.
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

    print("\n" + ("ALL TESTS PASSED" if not _failures else f"FAILURES: {_failures}"))
    return 0 if not _failures else 1


if __name__ == "__main__":
    sys.exit(main())
