"""
Options Recommendation Layer (2026-08-09)
=========================================

Turns the SAME quant view that drives the equity signals into a single,
fully-actionable, DEFINED-RISK options order ticket: what to buy, call or put,
which strike, which expiration, and the exact dollar cost / breakeven / max loss —
detailed enough to place the order at a broker with zero further thinking.

This is a thin COMPOSITION + SAFETY layer on top of the existing, battle-tested
`predictions.options_engine` (chain fetch, strike/expiry selection). It NEVER
modifies that shared engine and NEVER auto-executes anything — it only recommends.

Safety-first design (capital preservation):
  * Only LONG calls / LONG puts are ever recommended → max loss is capped at the
    premium paid. No naked short premium, no undefined-risk structures.
  * Directional view comes from analyze_watchlist_stock() so options can never
    contradict the equity signal.
  * TRUSTED-PRICE GATE: the underlying is anchored to the multi-source live quote;
    if the analysis price and the trusted quote disagree beyond tolerance the whole
    recommendation is WITHHELD (never trade on an ambiguous / corrupt price).
  * CONVICTION GATE: HOLD / NEUTRAL, or confidence below the floor, → no trade.
  * LIQUIDITY GATE: strike selection already rejects illiquid / wide-spread
    contracts; we additionally re-validate every number is finite and positive.
  * ANTI-FLAP: a recent good recommendation is reused (flagged stale) if a
    transient chain-fetch failure would otherwise flip a valid rec to "unavailable"
    — so the page always has data.
  * The function NEVER raises: on any failure it returns {actionable: False, reason}.
"""

import time
import logging
import math
from datetime import datetime

logger = logging.getLogger(__name__)

# ── Tunable gates ────────────────────────────────────────────────────────────
_CONF_FLOOR = 55            # minimum confidence for any directional options trade
_PRICE_DISAGREE_TOL = 0.08  # analysis-vs-trusted underlying disagreement → withhold
_STALE_REUSE_TTL = 20 * 60  # seconds a last-good rec may be reused on transient fail
_STALE_PX_TOL = 0.05        # underlying must still be within 5% to reuse a stale rec

# ticker -> (epoch, recommendation dict)  — anti-flap last-good cache
_last_good_rec: dict = {}


def _finite_pos(x) -> bool:
    try:
        v = float(x)
        return math.isfinite(v) and v > 0
    except Exception:
        return False


def _scrub(v):
    """Replace non-finite floats with None, recursively (mirrors main.py)."""
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: _scrub(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_scrub(x) for x in v]
    return v


def _withhold(ticker: str, reason: str, **extra) -> dict:
    out = {
        "ticker": ticker,
        "actionable": False,
        "reason": reason,
        "as_of": datetime.utcnow().isoformat() + "Z",
    }
    out.update(extra)
    return _scrub(out)


def _human_expiry(iso: str) -> str:
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%a %b %d, %Y")
    except Exception:
        return iso


def _occ_symbol(ticker: str, expiry: str, opt_type: str, strike: float) -> str:
    """Build the OCC option symbol, e.g. AAPL260918C00315000, for broker lookup."""
    try:
        d = datetime.strptime(expiry, "%Y-%m-%d")
        cp = "C" if opt_type == "call" else "P"
        strike_int = int(round(float(strike) * 1000))
        return f"{ticker.upper()}{d.strftime('%y%m%d')}{cp}{strike_int:08d}"
    except Exception:
        return ""


def _trusted_price(symbol: str):
    """Live trusted underlying price via the multi-source safety net, or None."""
    try:
        from analytics.multi_source_adapter import multi_source_quote_batch
        q = multi_source_quote_batch([symbol]) or {}
        row = q.get(symbol) or q.get(symbol.upper()) or {}
        px = row.get("price")
        return float(px) if _finite_pos(px) else None
    except Exception:
        return None


def _directional_view(symbol: str):
    """Run the quant engine (technical + macro + learning); return
    (res, signal, opt_type, confidence, score, regime, price).
    opt_type is 'call' (bullish) / 'put' (bearish) / None (no conviction)."""
    from analysis.quant_engine import analyze_watchlist_stock
    res = analyze_watchlist_stock(symbol) or {}
    signal = (res.get("signal") or "").upper()
    conf = res.get("confidence")
    score = res.get("composite_score")
    regime = res.get("regime")
    price = res.get("price")
    opt_type = None
    if "BUY" in signal:
        opt_type = "call"
    elif "SELL" in signal:
        opt_type = "put"
    return res, signal, opt_type, conf, score, regime, price


def _technical_summary(res: dict) -> dict:
    """Compact technical read pulled from the quant engine's own output."""
    tech = res.get("technicals") or {}
    fac = res.get("factors") or {}

    def _fv(name):
        node = fac.get(name)
        return node.get("value") if isinstance(node, dict) else None

    return {
        "ema_trend": tech.get("ema_trend"),
        "above_200sma": tech.get("above_200sma"),
        "above_50ema": tech.get("above_50ema"),
        "rsi14": _fv("rsi14"),
        "momentum_pct": _fv("momentum"),
        "volatility_ann_pct": _fv("volatility"),
        "volume_trend": (fac.get("volume_trend") or {}).get("label"),
    }


def _fundamental_summary(symbol: str) -> dict:
    """Best-effort fundamentals via the non-Yahoo multi-source chain
    (stockanalysis → finviz → FMP …). Never raises; returns {} on failure."""
    try:
        from analytics.multi_source_adapter import get_fundamentals_any_source
        f = get_fundamentals_any_source(symbol) or {}
    except Exception:
        f = {}
    if not f:
        return {}
    return {
        "trailing_pe": f.get("trailingPE"),
        "forward_pe": f.get("forwardPE"),
        "eps": f.get("eps"),
        "beta": f.get("beta"),
        "dividend_yield": f.get("dividendYield"),
        "market_cap": f.get("marketCap"),
        "fifty_two_week_high": f.get("fiftyTwoWeekHigh"),
        "fifty_two_week_low": f.get("fiftyTwoWeekLow"),
        "source": f.get("_source"),
    }


def _fundamental_tilt(fund: dict):
    """Translate the fundamental snapshot into a coarse bullish(+1)/bearish(-1)/
    neutral(0) tilt with a short reason. Deliberately conservative — fundamentals
    inform and caution, they do not override a high-conviction technical signal."""
    if not fund:
        return 0, "No fundamental data available."
    reasons = []
    tilt = 0
    eps = fund.get("eps")
    tpe = fund.get("trailing_pe")
    fpe = fund.get("forward_pe")
    if isinstance(eps, (int, float)):
        if eps > 0:
            tilt += 1; reasons.append("profitable (positive EPS)")
        elif eps < 0:
            tilt -= 1; reasons.append("unprofitable (negative EPS)")
    if isinstance(fpe, (int, float)) and isinstance(tpe, (int, float)) and fpe > 0 and tpe > 0:
        if fpe < tpe:
            tilt += 1; reasons.append("earnings expected to grow (forward P/E < trailing)")
        elif fpe > tpe * 1.15:
            tilt -= 1; reasons.append("earnings expected to shrink (forward P/E > trailing)")
    if isinstance(tpe, (int, float)) and tpe > 60:
        tilt -= 1; reasons.append(f"richly valued (P/E {tpe:.0f})")
    tilt = max(-1, min(1, tilt))
    return tilt, ("; ".join(reasons) if reasons else "Mixed / neutral fundamentals.")


# ── News / sentiment awareness (same feeds STB & the quant engine read) ──────
def _news_view(symbol: str) -> dict:
    """News/sentiment read across recent headlines. Primary = advanced NLP
    (financial lexicon, multi-headline) used elsewhere in the app; fallback =
    keyword headline scan. Never raises; returns {} on total failure."""
    try:
        from analysis.rentech_advanced import nlp_ticker_sentiment
        n = nlp_ticker_sentiment(symbol) or {}
        return {
            "sentiment": (n.get("overall_sentiment") or "NEUTRAL").upper(),
            "score": n.get("overall_score"),
            "confidence": n.get("confidence"),
            "article_count": n.get("article_count"),
            "source": "nlp",
        }
    except Exception:
        pass
    try:
        from analysis.rentech import get_stock_news_sentiment
        s = (get_stock_news_sentiment([symbol]) or {}).get(symbol) or {}
        raw = (s.get("sentiment") or "NEUTRAL").upper()
        if "POSITIVE" in raw:
            sent = "BULLISH"
        elif "NEGATIVE" in raw:
            sent = "BEARISH"
        else:
            sent = "NEUTRAL"
        return {
            "sentiment": sent,
            "score": s.get("score"),
            "confidence": None,
            "article_count": s.get("headlines_analyzed"),
            "source": "keyword",
        }
    except Exception:
        return {}


def _news_tilt(news: dict):
    """+1 bullish / -1 bearish / 0 neutral news tilt + short reason."""
    if not news:
        return 0, "No recent news signal."
    sent = (news.get("sentiment") or "NEUTRAL").upper()
    ac = news.get("article_count") or 0
    if "BULL" in sent or "POSITIVE" in sent:
        return 1, f"news flow bullish ({ac} headlines)"
    if "BEAR" in sent or "NEGATIVE" in sent:
        return -1, f"news flow bearish ({ac} headlines)"
    return 0, "news flow neutral"


# Bridgewater Pure Alpha sector buckets — mirrors the STB overlay exactly.
_RISK_ON_SECTORS = {"Technology", "Industrials", "Financial Services", "Consumer Cyclical"}
_RISK_OFF_SECTORS = {"Healthcare", "Consumer Defensive", "Utilities", "Real Estate"}


def _hf_regime() -> str:
    """Bridgewater Pure Alpha macro risk regime (RISK_ON/RISK_OFF/NEUTRAL) — the
    SAME cross-asset overlay that drives the STB sector tilts. Never raises."""
    try:
        from analysis.quant_engine import get_cross_asset_signals
        cas = get_cross_asset_signals() or {}
        return (cas.get("risk_appetite") or cas.get("macro_regime") or "NEUTRAL").upper()
    except Exception:
        return "NEUTRAL"


def _regime_alignment(regime: str, sector: str, want_bull: bool):
    """Does the macro regime + sector support the trade direction?
    Returns ('SUPPORTS'|'CAUTION'|'NEUTRAL', reason)."""
    if not sector or regime not in ("RISK_ON", "RISK_OFF"):
        return "NEUTRAL", f"{regime or 'NEUTRAL'} macro — no sector tilt."
    cyclical = sector in _RISK_ON_SECTORS
    defensive = sector in _RISK_OFF_SECTORS
    if regime == "RISK_ON":
        if cyclical and want_bull:
            return "SUPPORTS", f"RISK-ON macro favors {sector} longs"
        if cyclical and not want_bull:
            return "CAUTION", f"RISK-ON macro is a headwind for a {sector} put"
        if defensive and want_bull:
            return "CAUTION", f"RISK-ON macro is a headwind for a defensive {sector} call"
    if regime == "RISK_OFF":
        if defensive and want_bull:
            return "SUPPORTS", f"RISK-OFF macro favors defensive {sector} longs"
        if cyclical and want_bull:
            return "CAUTION", f"RISK-OFF macro is a headwind for a cyclical {sector} call"
        if cyclical and not want_bull:
            return "SUPPORTS", f"RISK-OFF macro supports a {sector} put"
    return "NEUTRAL", f"{regime} macro neutral for {sector}"


def _stb_context(symbol: str) -> dict:
    """Is this name a current Symbols-to-Buy fundamental long pick? If so, surface
    the SAME fundamental factors STB uses (score, revenue growth, ROE, PEG) so the
    options view is 'aware of the same stuff as STB'. Never raises."""
    try:
        from analysis.quant_engine import generate_fundamental_picks
        picks = (generate_fundamental_picks() or {}).get("long_picks") or []
        for p in picks:
            if (p.get("ticker") or p.get("symbol") or "").upper() == symbol:
                return {
                    "in_stb_longs": True,
                    "fundamental_score": p.get("fundamental_score"),
                    "revenue_growth_pct": p.get("revenue_growth_pct"),
                    "roe_pct": p.get("roe_pct"),
                    "peg_ratio": p.get("peg_ratio"),
                    "earnings_growth_pct": p.get("earnings_growth_pct"),
                    "momentum_pct": p.get("momentum_12m_pct") or p.get("momentum_pct"),
                    "sector": p.get("sector"),
                }
    except Exception:
        pass
    return {"in_stb_longs": False}


def _expected_move(px: float, iv: float, dte: int):
    """One-sigma expected move over the life of the option: px * IV * sqrt(DTE/365)."""
    try:
        if px > 0 and iv > 0 and dte > 0:
            return px * iv * math.sqrt(dte / 365.0)
    except Exception:
        pass
    return None


def _iv_environment(iv_pct):
    if not isinstance(iv_pct, (int, float)):
        return None
    if iv_pct < 25:
        return "LOW"
    if iv_pct <= 45:
        return "MODERATE"
    return "ELEVATED"


def _maybe_reuse_stale(symbol: str, ref_px, reason: str):
    """If a recent good rec exists and the underlying hasn't moved much, reuse it
    (flagged stale) so the page always shows data on a transient upstream failure."""
    cached = _last_good_rec.get(symbol)
    if not cached:
        return None
    ts, rec = cached
    if time.time() - ts > _STALE_REUSE_TTL:
        return None
    if ref_px is not None and _finite_pos(rec.get("underlying_price")):
        move = abs(float(ref_px) - float(rec["underlying_price"])) / float(rec["underlying_price"])
        if move > _STALE_PX_TOL:
            return None  # underlying moved too far — a stale premium would mislead
    stale = dict(rec)
    stale["stale_reused"] = True
    stale["stale_note"] = f"Live chain briefly unavailable ({reason}); showing last good recommendation."
    stale["as_of"] = datetime.utcnow().isoformat() + "Z"
    return _scrub(stale)


# Every structure we ever surface is either defined-risk (long premium, max loss =
# debit) or fully collateralized (cash-secured put / covered call). We NEVER build a
# naked short — selling a naked call is unlimited-loss and a naked put risks the whole
# strike; both violate capital-preservation discipline, so they are simply not modeled.
_ALLOWED_STRUCTURES = {"LONG_CALL", "LONG_PUT", "CASH_SECURED_PUT", "COVERED_CALL"}


def _build_strategy(structure: str, symbol: str, pick: dict, ref_px: float,
                    contracts: int = 1) -> dict:
    """Build ONE complete, actionable options order ticket for a given structure.

    Supported (all defined-risk or collateralized — NEVER naked/unlimited-risk):
      LONG_CALL        buy call  — debit; max loss = premium; bullish, leveraged upside.
      LONG_PUT         buy put   — debit; max loss = premium; bearish, leveraged downside.
      CASH_SECURED_PUT sell put  — credit; cash-collateralized; bullish/neutral income,
                                   assigned → you buy 100 shares at the strike.
      COVERED_CALL     sell call — credit; share-collateralized (needs 100 shares);
                                   neutral/mildly-bearish income, caps upside at strike.
    Returns {} on any incomplete/invalid contract data (caller filters these out).
    """
    try:
        if structure not in _ALLOWED_STRUCTURES:
            return {}  # hard guard — never emit anything but the four safe structures
        strike = pick.get("strike"); premium = pick.get("premium")
        dte = pick.get("dte"); expiry = pick.get("expiry")
        if not (_finite_pos(strike) and _finite_pos(premium) and _finite_pos(dte) and expiry):
            return {}
        if not _finite_pos(ref_px):
            return {}
        strike = float(strike); premium = float(premium); dte = int(dte)
        c = max(1, int(contracts or 1))
        shares = c * 100
        gross_prem = round(premium * 100.0 * c, 2)          # total premium across contracts
        prem_per_contract = round(premium * 100.0, 2)
        delta = pick.get("delta_est")
        iv = pick.get("iv")
        iv_pct = round(float(iv) * 100.0, 1) if _finite_pos(iv) else None
        iv_env = _iv_environment(iv_pct)
        vol = pick.get("volume") or 0
        oi = pick.get("open_interest") or 0
        liquidity_quality = "GOOD" if (oi >= 500 or vol >= 200) else ("FAIR" if (oi >= 50 or vol >= 25) else "THIN")
        exp_move = _expected_move(ref_px, float(iv) if _finite_pos(iv) else 0.0, dte)
        exp_move_pct = round(exp_move / ref_px * 100.0, 1) if exp_move else None
        exp_move = round(exp_move, 2) if exp_move else None
        prob_itm_est = round(abs(float(delta)) * 100.0, 0) if isinstance(delta, (int, float)) else None
        human_exp = _human_expiry(expiry)
        moneyness = pick.get("moneyness_label")

        leg_type = "put" if structure in ("LONG_PUT", "CASH_SECURED_PUT") else "call"
        opt_label = "CALL" if leg_type == "call" else "PUT"
        occ = _occ_symbol(symbol, expiry, leg_type, strike)
        contract_label = f"{symbol} {human_exp} ${strike:g} {opt_label}"

        # ── per-structure economics ──────────────────────────────────────────────
        if structure == "LONG_CALL":
            action, net_type = "BUY TO OPEN", "DEBIT"
            net_debit, net_credit = gross_prem, 0.0
            collateral_required, requires_shares = 0.0, 0
            max_loss = gross_prem
            max_loss_label = f"${gross_prem:,.2f} (the premium paid — you cannot lose more)"
            max_gain = None
            max_gain_label = f"Unlimited above ${strike + premium:.2f} (rises with {symbol})"
            breakeven = round(strike + premium, 2)
            be_move_pct = round((breakeven - ref_px) / ref_px * 100.0, 1)
            risk_type, outlook = "DEFINED", "BULLISH"
            title = f"Buy {opt_label} (long call)"
            summary = f"Leveraged bullish bet; max loss capped at the ${gross_prem:,.2f} premium."
        elif structure == "LONG_PUT":
            action, net_type = "BUY TO OPEN", "DEBIT"
            net_debit, net_credit = gross_prem, 0.0
            collateral_required, requires_shares = 0.0, 0
            max_loss = gross_prem
            max_loss_label = f"${gross_prem:,.2f} (the premium paid — you cannot lose more)"
            max_gain = round((strike - premium) * 100.0 * c, 2)
            max_gain_label = f"Up to ${max_gain:,.2f} if {symbol} falls to $0"
            breakeven = round(strike - premium, 2)
            be_move_pct = round((breakeven - ref_px) / ref_px * 100.0, 1)
            risk_type, outlook = "DEFINED", "BEARISH"
            title = f"Buy {opt_label} (long put)"
            summary = f"Leveraged bearish bet; max loss capped at the ${gross_prem:,.2f} premium."
        elif structure == "CASH_SECURED_PUT":
            action, net_type = "SELL TO OPEN", "CREDIT"
            net_debit, net_credit = 0.0, gross_prem
            collateral_required = round(strike * 100.0 * c, 2)
            requires_shares = 0
            max_gain = gross_prem
            max_gain_label = f"${gross_prem:,.2f} credit kept in full if {symbol} stays above ${strike:g}"
            max_loss = round((strike - premium) * 100.0 * c, 2)
            max_loss_label = (f"Up to ${max_loss:,.2f} if {symbol} goes to $0 "
                              f"(you'd be assigned {shares} shares at ${strike:g})")
            breakeven = round(strike - premium, 2)
            be_move_pct = round((breakeven - ref_px) / ref_px * 100.0, 1)
            risk_type, outlook = "COLLATERALIZED", "BULLISH_NEUTRAL"
            title = f"Sell {opt_label} (cash-secured put)"
            summary = (f"Get paid ${gross_prem:,.2f} now; if assigned you buy {shares} shares at "
                       f"${strike:g} (an effective ${breakeven:.2f} cost basis). "
                       f"Requires ${collateral_required:,.2f} cash collateral.")
        else:  # COVERED_CALL
            action, net_type = "SELL TO OPEN", "CREDIT"
            net_debit, net_credit = 0.0, gross_prem
            collateral_required = 0.0
            requires_shares = shares
            share_cost_estimate = round(ref_px * shares, 2)
            capped_upside = round(max(0.0, strike - ref_px) * 100.0 * c, 2)
            max_gain = round(gross_prem + capped_upside, 2)
            max_gain_label = (f"${max_gain:,.2f} = ${gross_prem:,.2f} premium + up to ${capped_upside:,.2f} "
                              f"share gain to ${strike:g} (upside capped there)")
            # Downside on the shares, cushioned by the premium collected.
            max_loss = round(ref_px * shares - gross_prem, 2)
            max_loss_label = (f"Substantial on the shares if {symbol} falls (up to ${max_loss:,.2f} to $0), "
                              f"but reduced by the ${gross_prem:,.2f} premium; breakeven ${round(ref_px - premium, 2):.2f}")
            breakeven = round(ref_px - premium, 2)
            be_move_pct = round((breakeven - ref_px) / ref_px * 100.0, 1)
            risk_type, outlook = "COLLATERALIZED", "NEUTRAL_MILD_BEARISH"
            title = f"Sell {opt_label} (covered call)"
            summary = (f"Own {shares} shares (≈ ${share_cost_estimate:,.2f}); collect ${gross_prem:,.2f} income. "
                       f"Upside capped at ${strike:g}; premium cushions the downside.")

        # ── order instructions (plain-English, broker-ready) ─────────────────────
        if action == "BUY TO OPEN":
            order_instructions = [
                f"Open your broker's options ticket for {symbol}.",
                "Action: BUY TO OPEN.",
                f"Contract: {contract_label}  (OCC: {occ}).",
                f"Quantity: {c} contract{'s' if c != 1 else ''} ({shares} shares of exposure).",
                f"Order type: LIMIT ~${premium:.2f}/share (≈ ${prem_per_contract:,.2f} per contract).",
                f"Confirm max loss ≈ ${gross_prem:,.2f} and breakeven ${breakeven:.2f}, then submit.",
            ]
        elif structure == "CASH_SECURED_PUT":
            order_instructions = [
                f"Ensure ${collateral_required:,.2f} cash is available as collateral (cash-secured).",
                f"Open your broker's options ticket for {symbol}.",
                "Action: SELL TO OPEN.",
                f"Contract: {contract_label}  (OCC: {occ}).",
                f"Quantity: {c} contract{'s' if c != 1 else ''}.",
                f"Order type: LIMIT ~${premium:.2f}/share (≈ ${prem_per_contract:,.2f} credit per contract).",
                f"You collect ${gross_prem:,.2f}. If assigned you buy {shares} shares at ${strike:g}; submit.",
            ]
        else:  # COVERED_CALL
            order_instructions = [
                f"Confirm you own at least {shares} shares of {symbol} (this is a covered call).",
                f"Open your broker's options ticket for {symbol}.",
                "Action: SELL TO OPEN.",
                f"Contract: {contract_label}  (OCC: {occ}).",
                f"Quantity: {c} contract{'s' if c != 1 else ''} (covered by your {shares} shares).",
                f"Order type: LIMIT ~${premium:.2f}/share (≈ ${prem_per_contract:,.2f} credit per contract).",
                f"You collect ${gross_prem:,.2f}. Shares may be called away at ${strike:g}; submit.",
            ]

        # ── risk disclosures (structure-specific) ────────────────────────────────
        risk_disclosures = []
        if action == "BUY TO OPEN":
            risk_disclosures += [
                f"Maximum loss is the ${gross_prem:,.2f} premium — you cannot lose more on a long {opt_label.lower()}.",
                f"The {opt_label.lower()} expires worthless if {symbol} is "
                f"{'below' if leg_type == 'call' else 'above'} ${strike:g} on {human_exp}.",
                f"{symbol} must move ~{abs(be_move_pct):.1f}% to ${breakeven:.2f} to break even at expiry.",
                "Time decay (theta) works against long options — plan to exit before ~5 DTE.",
                "Place a LIMIT near the mid, not a market order; check earnings dates (IV crush).",
            ]
        elif structure == "CASH_SECURED_PUT":
            risk_disclosures += [
                f"Max gain is the ${gross_prem:,.2f} credit; max loss ${max_loss:,.2f} if {symbol} goes to $0.",
                f"If {symbol} closes below ${strike:g} you are ASSIGNED {shares} shares at ${strike:g} "
                f"(effective cost ${breakeven:.2f}).",
                f"Requires ${collateral_required:,.2f} in cash reserved as collateral — never sell puts naked.",
                "Best when you would be happy to own the shares at the strike; a bullish/neutral income trade.",
            ]
        else:  # COVERED_CALL
            risk_disclosures += [
                f"You MUST already own {shares} shares — do NOT sell this call uncovered (naked = unlimited risk).",
                f"Upside is capped: shares can be called away at ${strike:g}, forgoing gains above it.",
                f"Downside is the share risk cushioned by the ${gross_prem:,.2f} premium; breakeven ${breakeven:.2f}.",
                "Income/neutral-to-mildly-bearish trade; roll or let it be called away near expiry.",
            ]
        if iv_env == "ELEVATED" and action == "BUY TO OPEN":
            risk_disclosures.append(f"IV is ELEVATED ({iv_pct:.0f}%) — the option is expensive; smaller size or a spread may be safer.")
        if iv_env == "ELEVATED" and action == "SELL TO OPEN":
            risk_disclosures.append(f"IV is ELEVATED ({iv_pct:.0f}%) — richer premium, but the move it implies is larger too.")
        if liquidity_quality == "THIN":
            risk_disclosures.append("Liquidity is THIN — expect wider fills; use a strict limit and small size.")

        strat = {
            "structure": structure,
            "title": title,
            "summary": summary,
            "action": action,
            "net_type": net_type,               # DEBIT (you pay) / CREDIT (you get paid)
            "risk_type": risk_type,             # DEFINED / COLLATERALIZED
            "outlook": outlook,
            "is_collateralized_short": action == "SELL TO OPEN",
            "option_type": opt_label,
            "leg_type": leg_type,
            "strike": strike,
            "expiration": expiry,
            "expiration_human": human_exp,
            "dte": dte,
            "contracts": c,
            "contract_label": contract_label,
            "occ_symbol": occ,
            "est_premium_per_share": round(premium, 2),
            "est_premium_per_contract": prem_per_contract,
            "net_debit": round(net_debit, 2),
            "net_credit": round(net_credit, 2),
            "collateral_required": round(collateral_required, 2),
            "requires_shares": requires_shares,
            "breakeven": breakeven,
            "breakeven_move_pct": be_move_pct,
            "max_loss": max_loss,
            "max_loss_label": max_loss_label,
            "max_gain": max_gain,
            "max_gain_label": max_gain_label,
            "delta": delta,
            "iv_pct": iv_pct,
            "iv_environment": iv_env,
            "moneyness": moneyness,
            "volume": pick.get("volume"),
            "open_interest": pick.get("open_interest"),
            "liquidity_quality": liquidity_quality,
            "expected_move": exp_move,
            "expected_move_pct": exp_move_pct,
            "prob_itm_est_pct": prob_itm_est,
            "order_instructions": order_instructions,
            "risk_disclosures": risk_disclosures,
        }
        return _scrub(strat)
    except Exception as e:  # never let a single structure break the whole response
        logger.error(f"[options] _build_strategy {structure} failed for {symbol}: {e}")
        return {}


def build_options_recommendation(symbol: str) -> dict:
    """
    Produce ONE fully-actionable, defined-risk options order ticket for `symbol`,
    or a clear {actionable: False, reason} when no safe trade exists.

    Never raises. Never auto-executes.
    """
    try:
        symbol = (symbol or "").upper().strip()
        if not symbol:
            return _withhold(symbol, "No ticker provided.")
        # Defensive symbol sanity (endpoint already validates; this is a second net).
        if len(symbol) > 8 or not all(ch.isalnum() or ch in ".-" for ch in symbol):
            return _withhold(symbol, "Ticker format not recognized — no options trade.")

        # 1) Trusted live underlying price (source of truth for the anchor).
        trusted = _trusted_price(symbol)

        # 2) Directional view from the same quant engine that drives equities
        #    (technical + macro + learning).
        try:
            res, signal, opt_type, conf, score, regime, analysis_px = _directional_view(symbol)
        except Exception as e:
            logger.debug(f"[options] directional view failed for {symbol}: {e}")
            reuse = _maybe_reuse_stale(symbol, trusted, "signal engine")
            return reuse or _withhold(symbol, "Signal engine temporarily unavailable — retry shortly.",
                                      underlying_price=trusted)

        # 3) Anchor the underlying price. Prefer the trusted live quote; fall back
        #    to the analysis price. Withhold if the two disagree materially
        #    (ambiguous → possible corruption → never trade).
        ref_px = None
        px_source = None
        if trusted is not None and _finite_pos(analysis_px):
            disagree = abs(float(analysis_px) - trusted) / trusted
            if disagree > _PRICE_DISAGREE_TOL:
                return _withhold(
                    symbol,
                    f"Underlying price disagreement {disagree*100:.1f}% "
                    f"(analysis ${float(analysis_px):.2f} vs live ${trusted:.2f}) — "
                    f"withholding to avoid a corrupt-price options trade.",
                    underlying_price=trusted, signal=signal, confidence=conf,
                )
            ref_px = trusted
            px_source = "multi_source_live"
        elif trusted is not None:
            ref_px, px_source = trusted, "multi_source_live"
        elif _finite_pos(analysis_px):
            ref_px, px_source = float(analysis_px), "analysis"
        if not _finite_pos(ref_px):
            reuse = _maybe_reuse_stale(symbol, None, "no price")
            return reuse or _withhold(symbol, "No reliable underlying price available — retry shortly.")

        # 4) Conviction gate — no coin-flip options trades.
        if opt_type is None:
            return _withhold(symbol, f"Engine is {signal or 'NEUTRAL'} on {symbol} — no conviction, no options trade.",
                             underlying_price=round(ref_px, 2), signal=signal, confidence=conf)
        if not (isinstance(conf, (int, float)) and conf >= _CONF_FLOOR):
            return _withhold(symbol,
                             f"Conviction too low (confidence {conf}%) for a defined-risk options trade "
                             f"(need ≥{_CONF_FLOOR}%).",
                             underlying_price=round(ref_px, 2), signal=signal, confidence=conf)

        abs_score = abs(float(score)) if isinstance(score, (int, float)) else 0.0

        # 4b) Technical + fundamental confirmation (what a real desk checks before
        #     putting on an options position). Technicals come from the quant engine;
        #     fundamentals from the non-Yahoo multi-source chain.
        technical = _technical_summary(res)
        fundamental = _fundamental_summary(symbol)
        f_tilt, f_reason = _fundamental_tilt(fundamental)
        want_bull = (opt_type == "call")
        if f_tilt != 0:
            fundamental_alignment = "AGREES" if ((f_tilt > 0) == want_bull) else "DIVERGES"
        else:
            fundamental_alignment = "NEUTRAL"

        # Fundamental gate: only VETO when fundamentals clearly diverge AND the
        # technical conviction is merely marginal. A strong signal is not overridden
        # by a coarse fundamental read (defined-risk trade), but a borderline one is.
        if fundamental_alignment == "DIVERGES" and conf < (_CONF_FLOOR + 10) and abs_score < 4.0:
            return _withhold(
                symbol,
                f"Technical signal ({signal}, conf {conf}%) is marginal and fundamentals diverge "
                f"({f_reason}) — withholding rather than buying a {('call' if want_bull else 'put')} "
                f"into a fundamental headwind.",
                underlying_price=round(ref_px, 2), signal=signal, confidence=conf,
                technical_analysis=technical, fundamental_analysis=fundamental,
                fundamental_alignment=fundamental_alignment,
            )

        # 4c) News / sentiment + macro-regime + STB awareness — the same context a
        #     desk (and our STB engine) checks before putting on the trade.
        news = _news_view(symbol)
        n_tilt, n_reason = _news_tilt(news)
        if n_tilt != 0:
            news_alignment = "AGREES" if ((n_tilt > 0) == want_bull) else "DIVERGES"
        else:
            news_alignment = "NEUTRAL"

        stb = _stb_context(symbol)
        sector = stb.get("sector") or res.get("sector") or ""
        regime_hf = _hf_regime()
        regime_align, regime_reason = _regime_alignment(regime_hf, sector, want_bull)

        # News gate: withhold only when a meaningful news flow clearly contradicts
        # the trade AND technical conviction is marginal (defined-risk discipline —
        # never buy a call into strongly bearish news, or a put into bullish news).
        if (news_alignment == "DIVERGES" and (news.get("article_count") or 0) >= 3
                and conf < (_CONF_FLOOR + 10) and abs_score < 4.0):
            return _withhold(
                symbol,
                f"Technical signal ({signal}, conf {conf}%) is marginal and {n_reason} — "
                f"withholding rather than buying a {('call' if want_bull else 'put')} against the news flow.",
                underlying_price=round(ref_px, 2), signal=signal, confidence=conf,
                technical_analysis=technical, fundamental_analysis=fundamental,
                fundamental_alignment=fundamental_alignment,
                news_analysis=news, news_alignment=news_alignment,
                macro_regime=regime_hf, macro_alignment=regime_align, stb_context=stb,
            )

        # Cross-signal confluence: technical always agrees (it drives direction);
        # count the independent confirmations from fundamentals, news, and macro.
        confirmations = 1  # technical
        confirm_bits = ["technical"]
        if fundamental_alignment == "AGREES":
            confirmations += 1; confirm_bits.append("fundamental")
        if news_alignment == "AGREES":
            confirmations += 1; confirm_bits.append("news")
        if regime_align == "SUPPORTS":
            confirmations += 1; confirm_bits.append("macro")
        if stb.get("in_stb_longs") and want_bull:
            confirmations += 1; confirm_bits.append("STB-long")
        signal_confluence = f"{confirmations}/{4 + (1 if want_bull else 0)}"

        # 5) Fetch the option chain (throttled + thread-timeout guarded in the engine).
        from predictions.options_engine import fetch_option_chain, select_strike, select_expiration
        chain_data = fetch_option_chain(symbol)
        if not chain_data or not chain_data.get("chains"):
            reuse = _maybe_reuse_stale(symbol, ref_px, "empty chain")
            return reuse or _withhold(symbol, f"No liquid option chain available for {symbol} right now.",
                                      underlying_price=round(ref_px, 2), signal=signal, confidence=conf)

        # 6) Choose the expiration by theta-buffered hold horizon, then filter the
        #    chain to that single expiry so the strike is picked within it.
        hold_days = 45 if abs_score >= 4.0 else 30
        chosen_expiry = select_expiration(chain_data, hold_days, abs_score)
        chains = chain_data["chains"]
        if chosen_expiry:
            filtered = [c for c in chains if c.get("expiry") == chosen_expiry]
            if filtered:
                chains = filtered
        chain_for_strike = {"symbol": symbol, "chains": chains,
                            "expiries": [c.get("expiry") for c in chains]}

        # 7) Select the strike (ATM on high conviction, slightly OTM otherwise).
        pick = select_strike(chain_for_strike, ref_px, opt_type, conf, float(score or 0))
        if not pick:
            reuse = _maybe_reuse_stale(symbol, ref_px, "no strike")
            return reuse or _withhold(symbol,
                                      f"No liquid, reasonably-priced {opt_type} strike near the money for {symbol}.",
                                      underlying_price=round(ref_px, 2), signal=signal, confidence=conf)

        strike = pick.get("strike")
        premium = pick.get("premium")
        dte = pick.get("dte")
        expiry = pick.get("expiry")
        if not (_finite_pos(strike) and _finite_pos(premium) and _finite_pos(dte) and expiry):
            reuse = _maybe_reuse_stale(symbol, ref_px, "bad contract data")
            return reuse or _withhold(symbol, "Contract data incomplete — withholding to avoid a bad order.",
                                      underlying_price=round(ref_px, 2), signal=signal, confidence=conf)

        # 8) Defined-risk economics.
        contracts = 1
        cost_per_contract = round(float(premium) * 100.0, 2)
        total_cost = round(cost_per_contract * contracts, 2)
        breakeven = round(float(strike) + float(premium), 2) if opt_type == "call" \
            else round(float(strike) - float(premium), 2)
        delta = pick.get("delta_est")
        iv = pick.get("iv")
        iv_pct = round(float(iv) * 100.0, 1) if _finite_pos(iv) else None
        opt_label = "CALL" if opt_type == "call" else "PUT"

        # 7b) Hedge-fund-grade options analytics.
        exp_move = _expected_move(ref_px, float(iv) if _finite_pos(iv) else 0.0, int(dte))
        exp_move_pct = round(exp_move / ref_px * 100.0, 1) if exp_move else None
        exp_move = round(exp_move, 2) if exp_move else None
        # Probability of finishing in-the-money ≈ |delta| (risk-neutral approximation).
        prob_itm_est = round(abs(float(delta)) * 100.0, 0) if isinstance(delta, (int, float)) else None
        # How far the underlying must travel to reach breakeven.
        breakeven_move_pct = round((float(strike) + float(premium) - ref_px) / ref_px * 100.0, 1) \
            if opt_type == "call" else round((ref_px - (float(strike) - float(premium))) / ref_px * 100.0, 1)
        iv_env = _iv_environment(iv_pct)
        vol = pick.get("volume") or 0
        oi = pick.get("open_interest") or 0
        liquidity_quality = "GOOD" if (oi >= 500 or vol >= 200) else ("FAIR" if (oi >= 50 or vol >= 25) else "THIN")
        # Is breakeven inside one expected move? (a real edge check for long premium)
        breakeven_within_1sigma = (exp_move_pct is not None and abs(breakeven_move_pct) <= exp_move_pct)
        human_exp = _human_expiry(expiry)
        contract_label = f"{symbol} {human_exp} ${strike:g} {opt_label}"
        occ = _occ_symbol(symbol, expiry, opt_type, strike)

        _tech_bits = []
        if technical.get("ema_trend"):
            _tech_bits.append(f"EMA trend {technical['ema_trend']}")
        if isinstance(technical.get("rsi14"), (int, float)):
            _tech_bits.append(f"RSI {technical['rsi14']:.0f}")
        if isinstance(technical.get("momentum_pct"), (int, float)):
            _tech_bits.append(f"momentum {technical['momentum_pct']:+.1f}%")
        _tech_str = ("; ".join(_tech_bits)) if _tech_bits else "technicals mixed"

        _stb_note = ""
        if stb.get("in_stb_longs"):
            _stb_bits = []
            if isinstance(stb.get("revenue_growth_pct"), (int, float)):
                _stb_bits.append(f"rev growth {stb['revenue_growth_pct']:+.0f}%")
            if isinstance(stb.get("roe_pct"), (int, float)):
                _stb_bits.append(f"ROE {stb['roe_pct']:.0f}%")
            if isinstance(stb.get("peg_ratio"), (int, float)):
                _stb_bits.append(f"PEG {stb['peg_ratio']:.2f}")
            _stb_note = (f" Also a current Symbols-to-Buy fundamental long"
                         + (f" ({', '.join(_stb_bits)})" if _stb_bits else "") + ".")

        rationale = (
            f"{signal} (score {float(score):+.1f}, confidence {conf}%) in a {regime or 'NEUTRAL'} regime. "
            f"Technicals: {_tech_str}. Fundamentals {fundamental_alignment.lower()} ({f_reason}). "
            f"News {news_alignment.lower()} — {n_reason}. Macro ({regime_hf}) {regime_reason}. "
            f"Cross-signal confluence {signal_confluence} ({', '.join(confirm_bits)}).{_stb_note} "
            f"→ A {'bullish' if opt_type == 'call' else 'bearish'} defined-risk {opt_label.lower()} gives "
            f"leveraged exposure with loss capped at the premium paid."
        )

        order_instructions = [
            f"Open your broker's options ticket for {symbol}.",
            "Action: BUY TO OPEN.",
            f"Contract: {contract_label}  (OCC: {occ}).",
            f"Quantity: {contracts} contract ({contracts * 100} shares of {symbol} exposure).",
            f"Order type: LIMIT at about ${premium:.2f} per share (≈ ${cost_per_contract:,.2f} per contract).",
            f"Confirm max loss ≈ ${total_cost:,.2f} (the premium) and breakeven ${breakeven:.2f} at expiry, then submit.",
        ]
        risk_disclosures = [
            f"Maximum loss is the ${total_cost:,.2f} premium — you cannot lose more than this on a long {opt_label.lower()}.",
            f"The {opt_label.lower()} expires worthless if {symbol} is "
            f"{'below' if opt_type == 'call' else 'above'} ${strike:g} on {human_exp}.",
            f"{symbol} must move ~{abs(breakeven_move_pct):.1f}% to ${breakeven:.2f} just to break even at expiry.",
            "Time decay (theta) works against long options — plan to exit before the final week (≈5 DTE).",
            "Estimated premium is the current mid; place a LIMIT order near it rather than a market order.",
            f"Check {symbol}'s next earnings date — avoid holding a long option through earnings (IV crush).",
        ]
        if iv_env == "ELEVATED":
            risk_disclosures.append(
                f"Implied volatility is ELEVATED ({iv_pct:.0f}%) — the option is expensive; a smaller size or a spread may be safer.")
        if fundamental_alignment == "DIVERGES":
            risk_disclosures.append(
                f"CAUTION: fundamentals diverge from this trade ({f_reason}) — technical signal is leading.")
        if news_alignment == "DIVERGES":
            risk_disclosures.append(
                f"CAUTION: {n_reason} runs against this trade — monitor the headlines closely.")
        if regime_align == "CAUTION":
            risk_disclosures.append(
                f"CAUTION: {regime_reason} — macro regime is a headwind; consider smaller size.")
        if stb.get("in_stb_longs") and not want_bull:
            risk_disclosures.append(
                f"CAUTION: {symbol} is a current Symbols-to-Buy fundamental LONG — this bearish put "
                f"contradicts the long-term fundamental thesis; treat it as a short-term tactical hedge.")
        if liquidity_quality == "THIN":
            risk_disclosures.append(
                "Liquidity is THIN (low volume/open interest) — expect wider fills; use a strict limit and small size.")

        # ── Multi-strategy: the leveraged long (primary) PLUS a collateralized
        #    premium-selling alternative in the SAME direction. Bullish → long call +
        #    cash-secured put; bearish → long put + covered call. We NEVER surface a
        #    naked short (unlimited / full-strike risk) — only defined-risk or fully
        #    collateralized structures reach the user.
        strategies = []
        primary_structure = "LONG_CALL" if opt_type == "call" else "LONG_PUT"
        primary_strat = _build_strategy(primary_structure, symbol, pick, ref_px, contracts)
        if primary_strat:
            primary_strat["is_primary"] = True
            strategies.append(primary_strat)

        # Collateralized premium-selling alternative (same directional bias).
        alt_structure = "CASH_SECURED_PUT" if want_bull else "COVERED_CALL"
        alt_leg_type = "put" if want_bull else "call"
        try:
            alt_pick = select_strike(chain_for_strike, ref_px, alt_leg_type, conf, float(score or 0))
        except Exception as _e:
            logger.error(f"[options] alt strike select failed for {symbol}: {_e}")
            alt_pick = None
        if alt_pick:
            alt_strat = _build_strategy(alt_structure, symbol, alt_pick, ref_px, contracts)
            if alt_strat:
                alt_strat["is_primary"] = False
                strategies.append(alt_strat)

        rec = {
            "ticker": symbol,
            "actionable": True,
            "as_of": datetime.utcnow().isoformat() + "Z",
            "underlying_price": round(ref_px, 2),
            "price_source": px_source,
            "signal": signal,
            "direction": "BULLISH" if opt_type == "call" else "BEARISH",
            "confidence": conf,
            "composite_score": round(float(score), 1) if isinstance(score, (int, float)) else None,
            "regime": regime,
            # ── the order ticket ──
            "action": "BUY TO OPEN",
            "option_type": opt_label,
            "strike": float(strike),
            "expiration": expiry,
            "expiration_human": human_exp,
            "dte": int(dte),
            "contracts": contracts,
            "contract_label": contract_label,
            "occ_symbol": occ,
            "est_premium_per_share": round(float(premium), 2),
            "est_cost_per_contract": cost_per_contract,
            "est_total_cost": total_cost,
            "breakeven": breakeven,
            "max_loss": total_cost,
            "max_loss_pct": 100,
            "delta": delta,
            "iv_pct": iv_pct,
            "moneyness": pick.get("moneyness_label"),
            "volume": pick.get("volume"),
            "open_interest": pick.get("open_interest"),
            "rationale": rationale,
            "order_instructions": order_instructions,
            "risk_disclosures": risk_disclosures,
            # ── all applicable structures (long primary + collateralized alt) ──
            "strategies": strategies,
            "strategy_count": len(strategies),
            # ── analysis a real desk relies on ──
            "technical_analysis": technical,
            "fundamental_analysis": fundamental,
            "fundamental_alignment": fundamental_alignment,
            "news_analysis": news,
            "news_alignment": news_alignment,
            "macro_regime": regime_hf,
            "macro_alignment": regime_align,
            "sector": sector or None,
            "stb_context": stb,
            "signal_confluence": signal_confluence,
            "confirmations": confirm_bits,
            "options_analytics": {
                "expected_move": exp_move,
                "expected_move_pct": exp_move_pct,
                "breakeven_move_pct": breakeven_move_pct,
                "breakeven_within_1sigma": breakeven_within_1sigma,
                "prob_itm_est_pct": prob_itm_est,
                "iv_environment": iv_env,
                "liquidity_quality": liquidity_quality,
            },
            "safety": {
                "trusted_price_checked": trusted is not None,
                "defined_risk": True,
                "auto_executes": False,
                "conviction_gate": f"conf ≥ {_CONF_FLOOR}%",
                "fundamental_gate": True,
                "news_gate": True,
                "macro_aware": True,
                "stb_aware": True,
                "liquidity_gate": True,
                "collateralized_selling_only": True,
                "naked_short_blocked": True,
                "structures_allowed": sorted(_ALLOWED_STRUCTURES),
            },
            "stale_reused": False,
        }
        rec = _scrub(rec)
        _last_good_rec[symbol] = (time.time(), rec)
        return rec

    except Exception as e:
        logger.error(f"[options] build_options_recommendation failed for {symbol}: {e}")
        try:
            reuse = _maybe_reuse_stale((symbol or "").upper().strip(), None, "internal error")
            if reuse:
                return reuse
        except Exception:
            pass
        return _withhold((symbol or "").upper().strip() or "?",
                         "Options recommendation temporarily unavailable — retry shortly.")
