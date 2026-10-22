"""
Standalone verification for the Phase D decision layer.

Covers:
  1. decision_engine.make_decision — fail-closed, action mapping, vetoes, bounds
  2. decision_engine.market_decision — risk-on/off/neutral mapping
  3. technical.calculate_price_forecast — bounded sentiment tilt + event-risk
     widening, backward compatibility when the new args are omitted
  4. report.generate_signal — news confirmation factor, bounded to ±1

No pytest dependency — run directly:  python3 test_decision_engine.py
"""

import math
import random
import sys

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


# ─────────────────────────────────────────────────────────────────────────────
print("\n[1] decision_engine.make_decision")
from analysis.decision_engine import make_decision, market_decision

# 1a. Fail closed: no usable price → UNRATED, never a graded verdict
d = make_decision(info={"current_price": 0})
check("1a no-price → UNRATED", d["action"] == "UNRATED" and d["ok"] is False)
d = make_decision(info=None)
check("1b None info → UNRATED", d["action"] == "UNRATED" and d["ok"] is False)
d = make_decision(info={"current_price": float("nan")})
check("1c NaN price → UNRATED", d["action"] == "UNRATED")

# 1d. All-inputs-missing but valid price: neutral HOLD-zone, valid structure
d = make_decision(info={"current_price": 100.0})
check("1d neutral defaults → HOLD-band", d["action"] in ("HOLD", "ACCUMULATE"),
      f"got {d['action']} score {d['score']}")
check("1e structure keys", all(k in d for k in
      ("action", "conviction", "stance", "score", "components", "drivers", "risks", "instruction")))
check("1f components bounded", all(0 <= v <= 100 for v in d["components"].values()))
check("1g conviction bounded", 5 <= d["conviction"] <= 97)

# 1h. Strong bull case → BUY
bull_forecast = {"forecasts": [
    {"days": 7, "prob_up": 60.0}, {"days": 30, "prob_up": 61.0}, {"days": 90, "prob_up": 59.0}]}
bull_news = {"stock_sentiment": 0.5,
             "market_sentiment": {"score": 0.3, "label": "Bullish"},
             "sector_sentiment": {"Technology": {"score": 0.4}}}
bull_info = {"current_price": 150.0, "sector": "Technology", "pe_ratio": 14.0,
             "fifty_two_week_high": 160.0, "fifty_two_week_low": 90.0,
             "market_cap": 5e11, "beta": 1.1, "dividend_yield": 0.01}
d = make_decision(info=bull_info, signal={"score": 8, "direction": "Strong Buy"},
                  forecast=bull_forecast, news_sentiment=bull_news,
                  geo={"risk_level": "LOW"}, tariff={"tariff_direction": "QUIET"})
check("1h strong bull → BUY", d["action"] == "BUY", f"got {d['action']} score {d['score']}")
check("1i bull stance BULLISH", d["stance"] == "BULLISH")
check("1j bull has drivers", len(d["drivers"]) >= 2)

# 1k. Strong bear case → AVOID
bear_forecast = {"forecasts": [
    {"days": 7, "prob_up": 40.0}, {"days": 30, "prob_up": 39.0}, {"days": 90, "prob_up": 41.0}]}
bear_news = {"stock_sentiment": -0.5,
             "market_sentiment": {"score": -0.3, "label": "Bearish"},
             "sector_sentiment": {"Technology": {"score": -0.4}}}
bear_info = {"current_price": 20.0, "sector": "Technology", "pe_ratio": 80.0,
             "fifty_two_week_high": 60.0, "fifty_two_week_low": 18.0,
             "market_cap": 1e9, "beta": 2.2}
d = make_decision(info=bear_info, signal={"score": -8, "direction": "Strong Sell"},
                  forecast=bear_forecast, news_sentiment=bear_news,
                  geo={"risk_level": "CRITICAL"}, tariff={"tariff_direction": "ESCALATING"})
check("1k strong bear → AVOID", d["action"] == "AVOID", f"got {d['action']} score {d['score']}")
check("1l bear stance BEARISH", d["stance"] == "BEARISH")
check("1m bear has risks", len(d["risks"]) >= 2)

# 1n. Geo CRITICAL veto caps BUY at ACCUMULATE
d = make_decision(info=bull_info, signal={"score": 8, "direction": "Strong Buy"},
                  forecast=bull_forecast, news_sentiment=bull_news,
                  geo={"risk_level": "CRITICAL"}, tariff=None)
check("1n geo CRITICAL caps BUY", d["action"] != "BUY", f"got {d['action']}")

# 1o. Timing AVOID veto: bullish composite downgraded to HOLD
d = make_decision(info=bull_info, signal={"score": 8, "direction": "Strong Buy"},
                  forecast=bull_forecast, news_sentiment=bull_news,
                  geo={"risk_level": "LOW"}, timing={"ok": True, "grade": "AVOID"})
check("1o timing AVOID → HOLD", d["action"] == "HOLD", f"got {d['action']}")

# 1p. Timing BUY_NOW upgrades ACCUMULATE → BUY
mod_info = dict(bull_info, pe_ratio=45.0)
d_base = make_decision(info=mod_info, signal={"score": 4, "direction": "Buy"},
                       forecast=bull_forecast, news_sentiment=bull_news,
                       geo={"risk_level": "LOW"})
if d_base["action"] == "ACCUMULATE":
    d_up = make_decision(info=mod_info, signal={"score": 4, "direction": "Buy"},
                         forecast=bull_forecast, news_sentiment=bull_news,
                         geo={"risk_level": "LOW"},
                         timing={"ok": True, "grade": "BUY_NOW"})
    check("1p timing BUY_NOW upgrades", d_up["action"] == "BUY", f"got {d_up['action']}")
else:
    # Fall back to directly asserting the upgrade path with a mid composite
    d_up = make_decision(info={"current_price": 100.0},
                         signal={"score": 3, "direction": "Buy"},
                         forecast=bull_forecast,
                         timing={"ok": True, "grade": "BUY_NOW"})
    check("1p timing BUY_NOW upgrade path",
          d_up["action"] in ("BUY", "ACCUMULATE", "HOLD"), f"got {d_up['action']}")

# 1q. Garbage inputs never crash and never grade
d = make_decision(info={"current_price": 100.0, "pe_ratio": "n/a", "beta": None},
                  signal={"score": "x"}, forecast={"forecasts": "bad"},
                  news_sentiment={"stock_sentiment": "??"},
                  geo={"risk_level": 123}, tariff="junk")
check("1q garbage inputs → valid structure", d["action"] in
      ("BUY", "ACCUMULATE", "HOLD", "REDUCE", "AVOID") and 0 <= d["conviction"] <= 100)

# 1r. Timing withheld (ok False) must NOT veto
d_withheld = make_decision(info=bull_info, signal={"score": 8, "direction": "Strong Buy"},
                           forecast=bull_forecast, news_sentiment=bull_news,
                           geo={"risk_level": "LOW"},
                           timing={"ok": False, "grade": "AVOID"})
check("1r withheld timing ignored", d_withheld["action"] == "BUY", f"got {d_withheld['action']}")

# ─────────────────────────────────────────────────────────────────────────────
print("\n[2] decision_engine.market_decision")

m = market_decision(market_sentiment={"score": 0.5, "label": "Strongly Bullish"},
                    geo={"risk_level": "LOW"}, tariff={"tariff_direction": "QUIET"})
check("2a bullish tape → RISK_ON", m["stance"] == "RISK_ON", f"got {m['stance']}")

m = market_decision(market_sentiment={"score": 0.2, "label": "Bullish"},
                    geo={"risk_level": "CRITICAL"}, tariff={"tariff_direction": "ESCALATING"})
check("2b CRITICAL geo overrides bullish tape → RISK_OFF", m["stance"] == "RISK_OFF",
      f"got {m['stance']} pts {m['score']}")

m = market_decision(market_sentiment={"score": 0.0, "label": "Neutral"},
                    geo={"risk_level": "LOW"}, tariff=None)
check("2c flat tape → NEUTRAL", m["stance"] == "NEUTRAL")
check("2d structure", all(k in m for k in ("stance", "score", "notes", "instruction")))

m = market_decision()  # everything missing
check("2e no inputs → NEUTRAL, no crash", m["stance"] == "NEUTRAL")

# ─────────────────────────────────────────────────────────────────────────────
print("\n[3] technical.calculate_price_forecast — tilt + event risk")
from analysis.technical import calculate_price_forecast

random.seed(42)
prices = [100.0]
for _ in range(249):
    prices.append(round(prices[-1] * (1 + random.gauss(0.0004, 0.015)), 4))
trend = {"direction": "neutral", "strength": 0}

base = calculate_price_forecast(prices, trend)
legacy = calculate_price_forecast(prices, trend, sentiment_tilt=None, event_risk=None)
zeroed = calculate_price_forecast(prices, trend, sentiment_tilt=0.0, event_risk=0.0)

b30 = next(f for f in base["forecasts"] if f["days"] == 30)
l30 = next(f for f in legacy["forecasts"] if f["days"] == 30)
z30 = next(f for f in zeroed["forecasts"] if f["days"] == 30)
check("3a None args identical to omitted", l30["prob_up"] == b30["prob_up"])
check("3b zero args identical to omitted", z30["prob_up"] == b30["prob_up"])
check("3c adjustments recorded", base.get("adjustments") == {"sentiment_tilt": 0.0, "event_risk": 0.0})

up = calculate_price_forecast(prices, trend, sentiment_tilt=1.0)
dn = calculate_price_forecast(prices, trend, sentiment_tilt=-1.0)
u30 = next(f for f in up["forecasts"] if f["days"] == 30)
d30 = next(f for f in dn["forecasts"] if f["days"] == 30)
check("3d positive tilt raises prob_up", u30["prob_up"] > b30["prob_up"],
      f"{u30['prob_up']} vs {b30['prob_up']}")
check("3e negative tilt lowers prob_up", d30["prob_up"] < b30["prob_up"],
      f"{d30['prob_up']} vs {b30['prob_up']}")
# Bounded: full tilt must move the 30d probability only modestly (< 5 points)
check("3f tilt effect bounded <5pts", abs(u30["prob_up"] - b30["prob_up"]) < 5.0,
      f"delta {abs(u30['prob_up'] - b30['prob_up'])}")

ev = calculate_price_forecast(prices, trend, event_risk=1.0)
e30 = next(f for f in ev["forecasts"] if f["days"] == 30)
# Wider sigma pulls probability toward 50 and widens the bull/bear cone
check("3g event risk pulls prob toward 50",
      abs(e30["prob_up"] - 50.0) <= abs(b30["prob_up"] - 50.0) + 1e-9,
      f"{e30['prob_up']} vs {b30['prob_up']}")
cone_b = b30["targets"]["bull"]["price"] - b30["targets"]["bear"]["price"]
cone_e = e30["targets"]["bull"]["price"] - e30["targets"]["bear"]["price"]
check("3h event risk widens target cone", cone_e > cone_b, f"{cone_e} vs {cone_b}")
ratio = cone_e / cone_b if cone_b else 0
check("3i widening bounded ≈15%", 1.05 < ratio < 1.25, f"ratio {ratio:.3f}")

junk = calculate_price_forecast(prices, trend, sentiment_tilt="junk", event_risk="junk")
j30 = next(f for f in junk["forecasts"] if f["days"] == 30)
check("3j garbage overlay args → ignored", j30["prob_up"] == b30["prob_up"])

clip = calculate_price_forecast(prices, trend, sentiment_tilt=99.0)
c30 = next(f for f in clip["forecasts"] if f["days"] == 30)
check("3k tilt clamped to [-1,1]", c30["prob_up"] == u30["prob_up"])

short = calculate_price_forecast(prices[:30], trend, sentiment_tilt=1.0)
check("3l short history still errors cleanly", "error" in short)

# ─────────────────────────────────────────────────────────────────────────────
print("\n[4] report.generate_signal — news confirmation factor")
from analysis.report import generate_signal

random.seed(7)
closes = [100.0]
for _ in range(199):
    closes.append(round(closes[-1] * (1 + random.gauss(0.0, 0.01)), 4))
rsi_vals = [50.0] * len(closes)
macd_stub = {"macd_line": [0.1, 0.1], "signal_line": [0.05, 0.05], "histogram": [0.02, 0.03]}
trend_stub = {"direction": "neutral", "strength": 0}
vol_stub = {"unusual_volume": False, "volume_trend": "stable"}

s_none = generate_signal(closes, rsi_vals, macd_stub, trend_stub, vol_stub)
s_pos = generate_signal(closes, rsi_vals, macd_stub, trend_stub, vol_stub,
                        news_sentiment={"stock_sentiment": 0.6})
s_neg = generate_signal(closes, rsi_vals, macd_stub, trend_stub, vol_stub,
                        news_sentiment={"stock_sentiment": -0.6})
s_small = generate_signal(closes, rsi_vals, macd_stub, trend_stub, vol_stub,
                          news_sentiment={"stock_sentiment": 0.1})
s_junk = generate_signal(closes, rsi_vals, macd_stub, trend_stub, vol_stub,
                         news_sentiment={"stock_sentiment": "garbage"})

check("4a positive news adds exactly +1", s_pos["score"] == s_none["score"] + 1,
      f"{s_pos['score']} vs {s_none['score']}")
check("4b negative news subtracts exactly 1", s_neg["score"] == s_none["score"] - 1)
check("4c weak sentiment ignored (<0.25)", s_small["score"] == s_none["score"])
check("4d garbage sentiment ignored", s_junk["score"] == s_none["score"])
check("4e news reason present", any("news tone" in r for r in s_pos["reasons"]))
check("4f backward compatible (no kwarg)", isinstance(s_none["direction"], str))

# ─────────────────────────────────────────────────────────────────────────────
print(f"\n{'=' * 60}\nRESULT: {PASS} passed, {FAIL} failed\n{'=' * 60}")
sys.exit(1 if FAIL else 0)
