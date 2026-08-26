"""
Tests for the company-news / current-events overlay wired into BOTH quant
engines (analysis.quant_engine):

  WATCHLIST  analyze_watchlist_stock  — the EXIT signal must SEE company news so
             a held position leans toward SELL on bad news instead of holding
             blind through a catalyst.
  STB        generate_fundamental_picks — the ENTRY signal must SEE company news
             so ranking reflects current events and flags risk on the card.

These drive the REAL functions with the network sources monkeypatched, so they
exercise production code (no logic mirror that can drift). They lock in the
capital-preservation invariants of the overlay:

  * News is a SMALL, HARD-CAPPED tilt (watchlist |tilt|<=1.0; STB score_tilt in
    [-6,+6], conf_tilt in [-2,+2]) — it CONFIRMS/gently re-ranks, it can never
    single-handedly bridge a full tier gap.
  * News NEVER hard-drops an STB pick (confidence floored at 72 > the 70 cut) —
    a noisy bearish keyword must not veto a quantitatively strong long.
  * STB stays strictly LONG-ONLY.
  * Fully FAIL-SAFE: any news-source error yields a neutral 0 tilt, never a
    crash and never a withhold.

Run:  python3 test_news_overlay.py   (no pytest needed)
"""
import sys
import math
import time
import numpy as np
import pandas as pd

import analysis.quant_engine as q
import analysis.rentech as rt
import analytics.multi_source_adapter as msa
import analytics.data_shield as ds

# --- Assertions ---------------------------------------------------------------
_failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    if not cond:
        _failures.append(name)
    print(f"{status}  {name}{('  -> ' + detail) if detail else ''}")


def _all_finite(obj):
    if isinstance(obj, float):
        return math.isfinite(obj)
    if isinstance(obj, dict):
        return all(_all_finite(v) for v in obj.values())
    if isinstance(obj, list):
        return all(_all_finite(v) for v in obj)
    return True


# =============================================================================
#  PART A — WATCHLIST EXIT SIGNAL news overlay
# =============================================================================
# Neutralize the non-news dependencies so only the news label varies.
q._throttle = lambda: None
q._get_sector_with_fallback = lambda s: "Technology"
q.detect_market_regime = lambda: {"regime": "SIDEWAYS", "confidence": 50}
q.get_macro_overlay = lambda: {"sector_adjustments": {}}
msa.get_historical_any_source = lambda *a, **k: None
ds.safe_download = lambda *a, **k: None

_TRUSTED = {"px": None}
_NEWS = {"label": "NEUTRAL", "raise": False}


def _fake_quote_batch(symbols):
    px = _TRUSTED["px"]
    if px is None:
        return {}
    return {s: {"price": float(px), "change_pct": 0.0} for s in symbols}


msa.multi_source_quote_batch = _fake_quote_batch


def _fake_news(symbols):
    if _NEWS["raise"]:
        raise RuntimeError("news source down")
    lbl = _NEWS["label"]
    return {s: {"sentiment": lbl, "score": 0, "positive_signals": 0,
                "negative_signals": 3 if "NEGATIVE" in lbl else 0,
                "headlines_analyzed": 5} for s in symbols}


rt.get_stock_news_sentiment = _fake_news


def _make_df(closes):
    n = len(closes)
    idx = pd.bdate_range(end=pd.Timestamp.now().normalize() - pd.Timedelta(days=1), periods=n)
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {"Open": closes, "High": closes * 1.005, "Low": closes * 0.995,
         "Close": closes, "Volume": np.full(n, 1_000_000.0)},
        index=idx,
    )


def _run_wl(closes, trusted, label="NEUTRAL", raise_news=False):
    _TRUSTED["px"] = trusted
    _NEWS["label"] = label
    _NEWS["raise"] = raise_news
    df = _make_df(closes)
    q.yf.download = lambda *a, **k: df
    try:
        q._quant_cache.clear()
        q._quant_last_good.clear()
    except Exception:
        pass
    return q.analyze_watchlist_stock("TESTX")


def part_a():
    # A steady uptrend that scores a clean LONG; quote == last close so anchoring
    # is a no-op and the ONLY variable across runs is the news label.
    up = list(np.linspace(300, 400, 120))
    last = float(up[-1])

    base = _run_wl(up, last, "NEUTRAL")
    check("WL neutral news scores + has news field",
          base.get("analyzed") is True and isinstance(base.get("news"), dict)
          and base["news"].get("sentiment") == "NEUTRAL"
          and base["news"].get("tilt") == 0.0,
          f"news={base.get('news')}")
    base_score = base.get("composite_score")

    neg = _run_wl(up, last, "NEGATIVE")
    check("WL negative news lowers composite score",
          neg.get("composite_score") is not None and base_score is not None
          and neg["composite_score"] < base_score
          and abs((base_score - neg["composite_score"]) - 0.4) < 0.06,
          f"base={base_score} neg={neg.get('composite_score')}")

    pos = _run_wl(up, last, "POSITIVE")
    check("WL positive news raises composite score",
          pos.get("composite_score") is not None and base_score is not None
          and pos["composite_score"] > base_score
          and abs((pos["composite_score"] - base_score) - 0.4) < 0.06,
          f"base={base_score} pos={pos.get('composite_score')}")

    vneg = _run_wl(up, last, "VERY_NEGATIVE")
    check("WL VERY_NEGATIVE tilt is exactly -0.75 (hard-capped)",
          vneg["news"].get("sentiment") == "VERY_NEGATIVE"
          and vneg["news"].get("tilt") == -0.75 and abs(vneg["news"]["tilt"]) <= 1.0,
          f"tilt={vneg['news'].get('tilt')}")

    vpos = _run_wl(up, last, "VERY_POSITIVE")
    check("WL VERY_POSITIVE tilt is exactly +0.75 (hard-capped, symmetric)",
          vpos["news"].get("sentiment") == "VERY_POSITIVE"
          and vpos["news"].get("tilt") == 0.75 and abs(vpos["news"]["tilt"]) <= 1.0,
          f"tilt={vpos['news'].get('tilt')}")

    # No-flip invariant: |tilt| <= 0.75 < the 2-point gap between HOLD and BUY/SELL,
    # so news alone can never bridge a full tier. Prove the bound directly.
    check("WL news tilt magnitude never exceeds 1.0 across labels",
          all(abs(r["news"]["tilt"]) <= 1.0 for r in (base, neg, pos, vneg, vpos)))

    # Fail-safe: a news-source exception must NOT crash or withhold — neutral tilt,
    # still a normal scored result identical to the neutral-news baseline.
    fail = _run_wl(up, last, "VERY_NEGATIVE", raise_news=True)
    check("WL news-source failure is fail-safe (neutral, still scores)",
          fail.get("analyzed") is True
          and fail["news"].get("sentiment") == "NEUTRAL"
          and fail["news"].get("tilt") == 0.0
          and fail.get("composite_score") == base_score,
          f"news={fail.get('news')} score={fail.get('composite_score')}")

    check("WL result has no NaN/inf with news applied", _all_finite(vneg))


# =============================================================================
#  PART B — STB ENTRY news overlay
# =============================================================================
_STB_NEWS = {}  # {ticker: label}


def _fake_news_stb(symbols):
    out = {}
    for s in symbols:
        lbl = _STB_NEWS.get(s, "NEUTRAL")
        out[s] = {"sentiment": lbl, "score": 0, "positive_signals": 0,
                  "negative_signals": 3 if "NEGATIVE" in lbl else 0,
                  "headlines_analyzed": 5}
    return out


_SECTORS = ["Technology", "Healthcare", "Energy", "Financial Services",
            "Industrials", "Utilities", "Materials", "Consumer Cyclical"]


def _seed_stb():
    """Seed the engine with 8 distinct-sector quant longs (no price arrays, so the
    correlation filter is skipped) and stub the whole network surface so
    generate_fundamental_picks runs fully offline and deterministically."""
    tickers = [f"TSTB{i:02d}" for i in range(8)]
    longs = []
    for i, t in enumerate(tickers):
        longs.append({
            "ticker": t, "symbol": t,
            "price": 50.0 + i * 7.0,            # distinct prices → no dedup drop
            "momentum_pct": 30.0 - i,           # distinct, all strong → all rank in top-20
            "volatility_60d": 20.0,
            "composite_score": 2.0,
            "confidence": 80,
            "sector": _SECTORS[i],              # distinct sectors → no sector-cap drop
            "reasons": [],
        })
    q._quant_cache.clear()
    q._quant_cache["quant_picks"] = {"data": {"long_picks": longs}, "time": time.time()}
    q._PRICE_DATA_LASTGOOD.clear()
    q._STB_UNIVERSE = list(tickers)             # cold-start download loop finds nothing to fetch
    q._SCAN_RUNNING = False
    # Stub the network surface.
    q._prefetch_fundamentals = lambda syms: {}
    q.generate_quant_picks = lambda *a, **k: None
    q.get_cross_asset_signals = lambda: {"risk_appetite": "NEUTRAL"}
    q.assess_geopolitical_risk = lambda: {"risk_level": "LOW"}
    msa.multi_source_quote_batch = lambda syms: {}  # skip live refresh (keep seeded prices)
    rt.get_stock_news_sentiment = _fake_news_stb
    return tickers


def part_b():
    tickers = _seed_stb()
    t_pos, t_neg, t_neu = tickers[0], tickers[1], tickers[2]
    _STB_NEWS.clear()
    _STB_NEWS[t_pos] = "VERY_POSITIVE"
    _STB_NEWS[t_neg] = "VERY_NEGATIVE"
    # rest default NEUTRAL

    res = q.generate_fundamental_picks(force=True)
    picks = {p["ticker"]: p for p in (res.get("long_picks") or [])}

    check("STB produced picks with news wired", len(picks) >= 3 and all("news" in p for p in picks.values()),
          f"n={len(picks)}")
    check("STB stays LONG-ONLY (no short_picks; every pick LONG)",
          res.get("short_picks") == []
          and all(p.get("direction") == "LONG" for p in picks.values()))

    pp = picks.get(t_pos, {}).get("news", {})
    check("STB VERY_POSITIVE pick tilts +6 score / +2 conf (hard-capped)",
          pp.get("sentiment") == "VERY_POSITIVE" and pp.get("score_tilt") == 6.0
          and pp.get("conf_tilt") == 2,
          f"news={pp}")

    pn = picks.get(t_neg, {})
    pnn = pn.get("news", {})
    check("STB VERY_NEGATIVE pick tilts -6 score / -2 conf but is NOT hard-dropped",
          t_neg in picks and pnn.get("sentiment") == "VERY_NEGATIVE"
          and pnn.get("score_tilt") == -6.0 and pnn.get("conf_tilt") == -2
          and pn.get("confidence", 0) >= 72,
          f"present={t_neg in picks} conf={pn.get('confidence')} news={pnn}")

    pu = picks.get(t_neu, {}).get("news", {})
    check("STB NEUTRAL pick has zero news tilt",
          pu.get("sentiment") == "NEUTRAL" and pu.get("score_tilt") == 0.0
          and pu.get("conf_tilt") == 0,
          f"news={pu}")

    # A strong bearish headline visibly flags the card (surfaced in reasons).
    check("STB surfaces bad-news warning in reasons",
          any("News" in r and "Negative" in r for r in (pn.get("reasons") or [])),
          f"reasons={pn.get('reasons')}")

    check("STB result has no NaN/inf with news applied", _all_finite(res.get("long_picks")))

    # Fail-safe: a news-source exception must NOT crash the STB build — picks still
    # produced, every pick carries a neutral news field. Re-seed (the prior call
    # consumed the cache) then make the news reader raise.
    _seed_stb()
    rt.get_stock_news_sentiment = lambda syms: (_ for _ in ()).throw(RuntimeError("news down"))
    res2 = q.generate_fundamental_picks(force=True)
    picks2 = res2.get("long_picks") or []
    check("STB news-source failure is fail-safe (build survives, neutral news)",
          len(picks2) >= 3
          and all(p.get("news", {}).get("sentiment") == "NEUTRAL" for p in picks2)
          and all(p.get("news", {}).get("score_tilt") == 0.0 for p in picks2),
          f"n={len(picks2)}")


def main():
    part_a()
    part_b()
    print("\n" + ("ALL TESTS PASSED" if not _failures else f"FAILURES: {_failures}"))
    return 0 if not _failures else 1


if __name__ == "__main__":
    sys.exit(main())
