"""
Integrity upgrades test suite — earnings hard-gate, cross-source price
verification, and the passive source-health registry.

Standalone: python3 test_integrity_upgrades.py   (run from backend/)
Zero network: every function under test is pure or local-SQLite.
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta

# Isolated DB BEFORE any predictions import (module reads DB_PATH at call time,
# but keep the established pattern — no shared state with the real DB).
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DB_PATH"] = _tmp.name

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from predictions.earnings_cache import save_next_earnings, get_days_until_earnings
from analysis.price_verify import classify_deviation
from analytics.source_health import record, snapshot, _status
from analysis.decision_engine import make_decision

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


def future(days):
    return (datetime.utcnow().date() + timedelta(days=days)).strftime("%Y-%m-%d")


# ── 1. earnings_cache ────────────────────────────────────────────────────────
print("\n[1] earnings_cache")
check("save valid future date returns True", save_next_earnings("AAPL", future(5)) is True)
check("get returns 5 for date 5 days out", get_days_until_earnings("AAPL") == 5)
check("get is case-insensitive", get_days_until_earnings("aapl") == 5)
check("upsert overwrites", save_next_earnings("AAPL", future(12)) is True
      and get_days_until_earnings("AAPL") == 12)
check("today counts as 0 days", save_next_earnings("TSLA", future(0)) is True
      and get_days_until_earnings("TSLA") == 0)
check("past date reads as None", save_next_earnings("MSFT", "2020-01-01") is True
      and get_days_until_earnings("MSFT") is None)
check("invalid date string returns False", save_next_earnings("NVDA", "not-a-date") is False)
check("None date returns False", save_next_earnings("NVDA", None) is False)
check("empty ticker returns False", save_next_earnings("", future(3)) is False)
check("unknown ticker reads None", get_days_until_earnings("ZZZZZ") is None)

# ── 2. classify_deviation ────────────────────────────────────────────────────
print("\n[2] classify_deviation")
check("identical prices VERIFIED", classify_deviation(100.0, 100.0)["status"] == "VERIFIED")
check("2% apart VERIFIED", classify_deviation(100.0, 98.0)["status"] == "VERIFIED")
check("exactly 3% TOLERANCE", classify_deviation(100.0, 97.0)["status"] == "TOLERANCE")
check("5% apart TOLERANCE", classify_deviation(100.0, 95.0)["status"] == "TOLERANCE")
check("exactly 10% TOLERANCE (not mismatch)", classify_deviation(100.0, 90.0)["status"] == "TOLERANCE")
check("15% apart MISMATCH", classify_deviation(100.0, 85.0)["status"] == "MISMATCH")
check("deviation_pct computed", classify_deviation(100.0, 95.0)["deviation_pct"] == 5.0)
check("no reference UNVERIFIED", classify_deviation(100.0, None)["status"] == "UNVERIFIED")
check("zero reference UNVERIFIED", classify_deviation(100.0, 0)["status"] == "UNVERIFIED")
check("negative live UNVERIFIED", classify_deviation(-5, 100.0)["status"] == "UNVERIFIED")
check("NaN UNVERIFIED", classify_deviation(float("nan"), 100.0)["status"] == "UNVERIFIED")
check("garbage string UNVERIFIED", classify_deviation("abc", 100.0)["status"] == "UNVERIFIED")

# ── 3. source_health ─────────────────────────────────────────────────────────
print("\n[3] source_health")
record("test_src", ok=True)
snap = snapshot()["sources"].get("test_src", {})
check("one success -> HEALTHY", snap.get("status") == "HEALTHY" and snap.get("ok_count") == 1)
record("test_src", ok=False)
check("one failure -> DEGRADED", snapshot()["sources"]["test_src"]["status"] == "DEGRADED")
record("test_src", ok=False)
record("test_src", ok=False)
check("3 consecutive failures -> DOWN", snapshot()["sources"]["test_src"]["status"] == "DOWN")
record("test_src", ok=True)
check("recovery -> HEALTHY", snapshot()["sources"]["test_src"]["status"] == "HEALTHY")
check("counts accumulate", snapshot()["sources"]["test_src"]["fail_count"] == 3)
record("note_src", ok=False, note="x" * 500)
check("note truncated to 200", len(snapshot()["sources"]["note_src"]["last_note"]) == 200)
check("never-seen source -> UNKNOWN", _status({"ok_count": 0, "consecutive_failures": 0}) == "UNKNOWN")
check("snapshot has uptime", snapshot()["uptime_seconds"] is not None)
try:
    record(None, ok=True)
    record({"weird": 1}, ok="yes")
    check("garbage record never raises", True)
except Exception:
    check("garbage record never raises", False)

# ── 4. decision engine: earnings gate + price_check ──────────────────────────
print("\n[4] decision engine gates")
BULL = dict(
    info={"current_price": 100.0, "sector": "Technology", "pe_ratio": 12,
          "fifty_two_week_high": 120.0, "fifty_two_week_low": 60.0},
    signal={"score": 10, "direction": "Strong Buy"},
    forecast={"forecasts": [{"days": 7, "prob_up": 70}, {"days": 30, "prob_up": 70},
                            {"days": 90, "prob_up": 70}]},
    news_sentiment={"stock_sentiment": 0.8,
                    "market_sentiment": {"score": 0.5, "label": "Bullish"}},
    geo={}, tariff={},
)

base = make_decision(**BULL)
check("baseline is a bullish verdict", base["action"] in ("BUY", "ACCUMULATE"))
check("baseline no blackout", base["earnings_blackout"] is False and base["earnings_days"] is None)

d = make_decision(**BULL, earnings_days=3)
check("earnings in 3d gates to HOLD", d["action"] == "HOLD")
check("blackout flagged with days", d["earnings_blackout"] is True and d["earnings_days"] == 3)
check("blackout risk cited", any("Earnings in 3d" in r for r in d["risks"]))

d = make_decision(**BULL, earnings_days=0)
check("earnings today gates to HOLD", d["action"] == "HOLD" and d["earnings_blackout"] is True)

d = make_decision(**BULL, earnings_days=20)
check("earnings in 20d does not gate", d["action"] == base["action"]
      and d["earnings_blackout"] is False and d["earnings_days"] == 20)

d = make_decision(**BULL, earnings_days="garbage")
check("garbage earnings_days safe", d["action"] == base["action"] and d["earnings_blackout"] is False)

BEAR = dict(BULL)
BEAR["signal"] = {"score": -10, "direction": "Strong Sell"}
BEAR["forecast"] = {"forecasts": [{"days": 7, "prob_up": 30}, {"days": 30, "prob_up": 30},
                                  {"days": 90, "prob_up": 30}]}
BEAR["news_sentiment"] = {"stock_sentiment": -0.8,
                          "market_sentiment": {"score": -0.5, "label": "Bearish"}}
d_bear = make_decision(**BEAR)
check("bearish baseline not bullish", d_bear["action"] in ("HOLD", "REDUCE", "AVOID"))
d = make_decision(**BEAR, earnings_days=3)
check("earnings gate never touches bearish verdicts",
      d["action"] == d_bear["action"] and d["earnings_blackout"] is False)

d = make_decision(**BULL, price_check={"status": "MISMATCH", "deviation_pct": 14.2,
                                       "reference_price": 85.8})
check("MISMATCH withholds verdict", d["ok"] is False and d["action"] == "UNRATED")
check("MISMATCH risk cites both prices", any("mismatch" in r.lower() for r in d["risks"]))

d = make_decision(**BULL, price_check={"status": "TOLERANCE", "deviation_pct": 5.1,
                                       "reference_price": 94.9})
check("TOLERANCE keeps the verdict", d["action"] == base["action"] and d["ok"] is True)
check("TOLERANCE cuts conviction", d["conviction"] < base["conviction"])
check("TOLERANCE risk cited", any("sources differ" in r.lower() for r in d["risks"]))

d = make_decision(**BULL, price_check={"status": "VERIFIED", "deviation_pct": 0.4,
                                       "reference_price": 99.6})
check("VERIFIED changes nothing", d["action"] == base["action"]
      and d["conviction"] == base["conviction"])

d = make_decision(**BULL, price_check={"status": "UNVERIFIED"})
check("UNVERIFIED changes nothing", d["action"] == base["action"]
      and d["conviction"] == base["conviction"])

d = make_decision(**BULL, price_check="garbage")
check("garbage price_check safe", d["action"] == base["action"])

d = make_decision(info={"current_price": None}, price_check={"status": "VERIFIED"})
check("no price still UNRATED", d["ok"] is False and d["action"] == "UNRATED"
      and d["earnings_blackout"] is False)

d = make_decision(**BULL, earnings_days=2,
                  price_check={"status": "TOLERANCE", "deviation_pct": 4.0,
                               "reference_price": 96.0})
check("gate + tolerance combine", d["action"] == "HOLD" and d["earnings_blackout"] is True
      and d["conviction"] < base["conviction"])

# ── summary ──────────────────────────────────────────────────────────────────
print(f"\n{'=' * 50}\n{PASS} passed, {FAIL} failed of {PASS + FAIL} checks")
try:
    os.unlink(_tmp.name)
except OSError:
    pass
sys.exit(1 if FAIL else 0)
