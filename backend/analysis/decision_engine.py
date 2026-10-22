"""
Decision Engine — the autonomous "what to do" layer.

Pure arithmetic over inputs the caller already holds. NO network I/O, NO
caching, NO side effects. The engine never fetches anything: callers pass in
the technical signal, forecast, news sentiment, geopolitical read, and
fundamentals that were already computed for the page. That keeps the request
budget at zero and makes every function deterministic and unit-testable.

Two entry points:
  make_decision(...)   — per-stock verdict for the analyze page / any page
  market_decision(...) — site-wide session stance (risk-on / risk-off)

FAIL CLOSED: if price data is missing/degraded the engine returns UNRATED
rather than grading garbage. A wrong confident answer is worse than no answer.
"""

# Action thresholds on the 0-100 composite
_BUY_NOW = 68
_ACCUMULATE = 58
_HOLD_FLOOR = 42
_REDUCE_FLOOR = 32

# Component weights (sum to 1.0). Technicals carry the most weight because
# they are computed from full price history; news/geo are coarser reads.
_W_TECHNICAL = 0.34
_W_FORECAST = 0.18
_W_NEWS = 0.16
_W_MACRO = 0.16
_W_FUNDAMENTAL = 0.16

_ACTION_INSTRUCTIONS = {
    "BUY": "Enter a position today. Technicals, forecast and the news tape agree.",
    "ACCUMULATE": "Scale in on weakness. The setup is constructive but not fully confirmed.",
    "HOLD": "No edge either way. Hold existing exposure; do not add here.",
    "REDUCE": "Trim exposure into strength. The weight of evidence is deteriorating.",
    "AVOID": "Stand aside. The composite read is against this name right now.",
    "UNRATED": "Data integrity check failed — the system withholds a verdict rather than guess.",
}


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _num(v, default=None):
    """Safe float: returns default on None/NaN/garbage."""
    try:
        f = float(v)
        if f != f:  # NaN
            return default
        return f
    except (TypeError, ValueError):
        return default


def _dict(v):
    """Safe dict: anything that isn't a dict becomes {}."""
    return v if isinstance(v, dict) else {}


def _label(d, key, default=""):
    """Safe uppercase string field from a dict-like input."""
    v = _dict(d).get(key)
    return v.upper() if isinstance(v, str) else default


# ─────────────────────────────────────────────────────────────────────────────
# Component scorers — each returns (score 0-100, drivers[], risks[])
# ─────────────────────────────────────────────────────────────────────────────

def _score_technical(signal):
    """Map the multi-factor signal score (~-10..+10) onto 0-100."""
    drivers, risks = [], []
    raw = _num((signal or {}).get("score"), 0.0)
    score = _clamp(50.0 + raw * 5.0, 0.0, 100.0)
    direction = (signal or {}).get("direction") or "Hold"
    if raw >= 3:
        drivers.append(f"Technical composite {direction} (score {raw:+.0f})")
    elif raw <= -3:
        risks.append(f"Technical composite {direction} (score {raw:+.0f})")
    return score, drivers, risks


def _score_forecast(forecast):
    """Probability edge: blend 7d/30d/90d prob_up into 0-100."""
    drivers, risks = [], []
    fc = _dict(forecast).get("forecasts")
    if not isinstance(fc, list):
        fc = []
    by_days = {f.get("days"): f for f in fc if isinstance(f, dict)}
    probs, weights = [], []
    for days, w in ((7, 0.3), (30, 0.45), (90, 0.25)):
        p = _num(_dict(by_days.get(days)).get("prob_up"))
        if p is not None:
            probs.append(p * w)
            weights.append(w)
    if not weights:
        return 50.0, drivers, risks
    blended = sum(probs) / sum(weights)  # 0-100 already
    # Center at 50 and stretch the edge ×2 so a 58% prob_up reads as 66.
    score = _clamp(50.0 + (blended - 50.0) * 2.0, 0.0, 100.0)
    p30 = _num(_dict(by_days.get(30)).get("prob_up"))
    if p30 is not None and p30 >= 56:
        drivers.append(f"30-day upside probability {p30:.0f}%")
    elif p30 is not None and p30 <= 44:
        risks.append(f"30-day upside probability only {p30:.0f}%")
    return score, drivers, risks


def _score_news(news_sentiment, sector=None):
    """Stock headlines + overall market tape + the stock's own sector tone."""
    drivers, risks = [], []
    ns = _dict(news_sentiment)
    stock_s = _clamp(_num(ns.get("stock_sentiment"), 0.0) or 0.0, -1.0, 1.0)
    mkt = _dict(ns.get("market_sentiment"))
    mkt_s = _clamp(_num(mkt.get("score"), 0.0) or 0.0, -1.0, 1.0)
    sec_s = 0.0
    sec = _dict(ns.get("sector_sentiment")).get(sector) if sector else None
    if isinstance(sec, dict):
        sec_s = _clamp(_num(sec.get("score"), 0.0) or 0.0, -1.0, 1.0)
    combined = 0.55 * stock_s + 0.25 * mkt_s + 0.20 * sec_s  # -1..1
    score = _clamp(50.0 + combined * 50.0, 0.0, 100.0)
    if stock_s >= 0.25:
        drivers.append(f"Stock-specific news tone positive ({stock_s:+.2f})")
    elif stock_s <= -0.25:
        risks.append(f"Stock-specific news tone negative ({stock_s:+.2f})")
    label = mkt.get("label")
    label = label if isinstance(label, str) else ""
    if "Bearish" in label:
        risks.append(f"Market news tape {label}")
    elif "Bullish" in label and "Slightly" not in label:
        drivers.append(f"Market news tape {label}")
    return score, drivers, risks


def _score_macro_geo(geo, tariff, sector=None):
    """Geopolitical + political(tariff/policy) scan mapped to this sector."""
    drivers, risks = [], []
    score = 55.0  # quiet world = mild tailwind
    level = _label(geo, "risk_level", "LOW") or "LOW"
    if level == "CRITICAL":
        score = 25.0
        risks.append("Geopolitical risk CRITICAL — active conflict headlines")
    elif level == "ELEVATED":
        score = 40.0
        risks.append("Geopolitical risk ELEVATED")
    # Sector-specific adjustment from the geo scan (defense up, etc.)
    sec_adj = _num(_dict(_dict(geo).get("sector_adjustments")).get(sector), 0.0) or 0.0
    score = _clamp(score + sec_adj * 4.0, 0.0, 100.0)
    if sec_adj >= 1:
        drivers.append(f"Geo backdrop favors {sector} (+{sec_adj:.0f})")
    elif sec_adj <= -1:
        risks.append(f"Geo backdrop pressures {sector} ({sec_adj:.0f})")
    # Tariff / trade-policy overlay
    t_dir = _label(tariff, "tariff_direction", "QUIET") or "QUIET"
    t_adj = _num(_dict(_dict(tariff).get("sector_adjustments")).get(sector), 0.0) or 0.0
    if t_dir == "ESCALATING":
        score = _clamp(score - 8.0 + t_adj * 3.0, 0.0, 100.0)
        if t_adj < 0:
            risks.append(f"Tariff escalation headwind for {sector}")
    elif t_dir == "DE_ESCALATING":
        score = _clamp(score + 5.0 + t_adj * 3.0, 0.0, 100.0)
        drivers.append("Trade tensions de-escalating")
    return score, drivers, risks


def _score_fundamental(info):
    """Valuation + quality read from available fundamentals. Starts neutral."""
    drivers, risks = [], []
    score = 50.0
    inf = info or {}

    pe = _num(inf.get("pe_ratio"))
    if pe is not None and pe > 0:
        if pe < 15:
            score += 10
            drivers.append(f"Inexpensive at {pe:.1f}x trailing earnings")
        elif pe < 28:
            score += 4
        elif pe > 60:
            score -= 10
            risks.append(f"Rich valuation at {pe:.1f}x trailing earnings")
        elif pe > 40:
            score -= 5
            risks.append(f"Elevated valuation ({pe:.1f}x)")
    elif pe is not None and pe <= 0:
        # trailingPE of 0 from the feed means "unknown", not free — ignore.
        pass

    # Position in the 52-week range: strength is information, basing is risk.
    px = _num(inf.get("current_price"))
    hi = _num(inf.get("fifty_two_week_high"))
    lo = _num(inf.get("fifty_two_week_low"))
    if px and hi and lo and hi > lo > 0:
        pos = _clamp((px - lo) / (hi - lo), 0.0, 1.0)
        if pos >= 0.80:
            score += 8
            drivers.append(f"Trading in top {100 - pos * 100:.0f}% of 52-week range")
        elif pos <= 0.20:
            score -= 8
            risks.append(f"Near 52-week lows ({pos * 100:.0f}% of range)")

    dy = _num(inf.get("dividend_yield"), 0.0) or 0.0
    if 0 < dy < 1:  # yfinance returns fraction
        dy *= 100
    if dy >= 2.5:
        score += 4
        drivers.append(f"Pays {dy:.1f}% dividend")

    beta = _num(inf.get("beta"))
    if beta is not None and beta > 1.8:
        score -= 4
        risks.append(f"High beta {beta:.1f} — amplifies any market selloff")

    mc = _num(inf.get("market_cap"), 0.0) or 0.0
    if 0 < mc < 2e9:
        score -= 5
        risks.append("Small cap — thinner liquidity, wider swings")

    return _clamp(score, 0.0, 100.0), drivers, risks


# ─────────────────────────────────────────────────────────────────────────────
# Per-stock decision
# ─────────────────────────────────────────────────────────────────────────────

def make_decision(info=None, signal=None, forecast=None, news_sentiment=None,
                  geo=None, tariff=None, timing=None):
    """Combine every scanner into one autonomous verdict.

    All inputs optional; missing inputs score neutral (50). Returns a dict:
      action, conviction, stance, score, components{}, drivers[], risks[],
      instruction, ok
    """
    # FAIL CLOSED — no usable price means no verdict.
    px = _num((info or {}).get("current_price"))
    if px is None or px <= 0:
        return {
            "ok": False,
            "action": "UNRATED",
            "stance": "NEUTRAL",
            "conviction": 0,
            "score": None,
            "components": {},
            "drivers": [],
            "risks": ["Price data unavailable or failed integrity checks"],
            "instruction": _ACTION_INSTRUCTIONS["UNRATED"],
        }

    sector = (info or {}).get("sector")
    if sector in ("N/A", "Unknown", ""):
        sector = None

    t_score, t_dr, t_rk = _score_technical(signal)
    f_score, f_dr, f_rk = _score_forecast(forecast)
    n_score, n_dr, n_rk = _score_news(news_sentiment, sector)
    m_score, m_dr, m_rk = _score_macro_geo(geo, tariff, sector)
    u_score, u_dr, u_rk = _score_fundamental(info)

    composite = (
        t_score * _W_TECHNICAL
        + f_score * _W_FORECAST
        + n_score * _W_NEWS
        + m_score * _W_MACRO
        + u_score * _W_FUNDAMENTAL
    )

    drivers = t_dr + f_dr + n_dr + m_dr + u_dr
    risks = t_rk + f_rk + n_rk + m_rk + u_rk

    # Entry-timing veto/boost from the STB timing engine when available.
    timing_grade = None
    if isinstance(timing, dict) and timing.get("ok"):
        timing_grade = (timing.get("grade") or "").upper()

    if composite >= _BUY_NOW:
        action = "BUY"
    elif composite >= _ACCUMULATE:
        action = "ACCUMULATE"
    elif composite >= _HOLD_FLOOR:
        action = "HOLD"
    elif composite >= _REDUCE_FLOOR:
        action = "REDUCE"
    else:
        action = "AVOID"

    # Vetoes — capital preservation outranks the composite.
    level = _label(geo, "risk_level", "LOW") or "LOW"
    if level == "CRITICAL" and action == "BUY":
        action = "ACCUMULATE"
        risks.append("BUY capped to ACCUMULATE: geopolitical risk is CRITICAL")
    if timing_grade == "AVOID" and action in ("BUY", "ACCUMULATE"):
        action = "HOLD"
        risks.append("Entry timing engine reads AVOID — new entries withheld")
    elif timing_grade == "BUY_NOW" and action == "ACCUMULATE":
        action = "BUY"
        drivers.append("Entry timing engine confirms BUY NOW")

    stance = ("BULLISH" if composite >= _ACCUMULATE
              else "BEARISH" if composite < _HOLD_FLOOR
              else "NEUTRAL")
    # Conviction = distance from 50, scaled; agreement across components adds.
    comp_scores = [t_score, f_score, n_score, m_score, u_score]
    same_side = sum(1 for c in comp_scores
                    if (c - 50) * (composite - 50) > 0 and abs(c - 50) >= 3)
    conviction = _clamp(round(abs(composite - 50) * 2.2 + same_side * 6), 5, 97)

    return {
        "ok": True,
        "action": action,
        "stance": stance,
        "conviction": int(conviction),
        "score": round(composite, 1),
        "components": {
            "technical": round(t_score, 1),
            "forecast": round(f_score, 1),
            "news": round(n_score, 1),
            "macro_geo": round(m_score, 1),
            "fundamental": round(u_score, 1),
        },
        "drivers": drivers[:6],
        "risks": risks[:6],
        "instruction": _ACTION_INSTRUCTIONS[action],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Site-wide market stance
# ─────────────────────────────────────────────────────────────────────────────

def market_decision(market_sentiment=None, geo=None, tariff=None):
    """Session-level stance for the whole site: RISK_ON / NEUTRAL / RISK_OFF.

    Pure function over already-fetched reads. Every page can render this
    without a single extra request.
    """
    ms = _dict(market_sentiment)
    score = _clamp(_num(ms.get("score"), 0.0) or 0.0, -1.0, 1.0)  # -1..1
    level = _label(geo, "risk_level", "LOW") or "LOW"
    t_dir = _label(tariff, "tariff_direction", "QUIET") or "QUIET"

    pts = score * 40.0  # news tape: -40..+40
    notes = []
    if level == "CRITICAL":
        pts -= 35
        notes.append("Geopolitical risk CRITICAL")
    elif level == "ELEVATED":
        pts -= 15
        notes.append("Geopolitical risk elevated")
    if t_dir == "ESCALATING":
        pts -= 10
        notes.append("Trade tensions escalating")
    elif t_dir == "DE_ESCALATING":
        pts += 6
        notes.append("Trade tensions easing")

    label = ms.get("label")
    if isinstance(label, str) and label:
        notes.insert(0, f"News tape {label}")

    if pts >= 12:
        stance, instruction = "RISK_ON", (
            "Favor long exposure. Lean into the strongest technical setups; "
            "the tape and macro backdrop support risk.")
    elif pts <= -12:
        stance, instruction = "RISK_OFF", (
            "Defense first. No new longs into weakness; tighten stops and "
            "let the macro picture clear before adding risk.")
    else:
        stance, instruction = "NEUTRAL", (
            "Mixed tape. Trade single-name setups only — no market-direction "
            "bets until the signal firms up.")

    return {
        "stance": stance,
        "score": round(pts, 1),
        "notes": notes[:4],
        "instruction": instruction,
    }
