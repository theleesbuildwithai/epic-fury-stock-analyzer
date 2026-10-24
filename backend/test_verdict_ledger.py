"""
Standalone verification for the verdict ledger (accuracy loop).

Uses a throwaway SQLite file via DB_PATH so the real ledger is never touched.
No pytest dependency — run directly:  python3 test_verdict_ledger.py
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_ledger.db")

from predictions.verdict_ledger import (
    record_verdict, score_pending, accuracy_summary, HORIZONS,
)

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


def mk_decision(action="BUY", conviction=60, score=70.0, ok=True):
    return {"ok": ok, "action": action, "stance": "BULLISH", "conviction": conviction,
            "score": score, "components": {"technical": 75.0}}


def mk_forecast(prob30=58.0):
    return {"forecasts": [{"days": 7, "prob_up": 55.0},
                          {"days": 30, "prob_up": prob30},
                          {"days": 90, "prob_up": 52.0}]}


print("\n[1] record_verdict")
check("1a records valid verdict",
      record_verdict("AAPL", mk_decision(), mk_forecast(), price=100.0) is True)
check("1b same ticker+day deduped",
      record_verdict("AAPL", mk_decision("AVOID"), mk_forecast(), price=100.0) is False)
check("1c not-ok decision skipped",
      record_verdict("MSFT", mk_decision(ok=False), mk_forecast(), price=100.0) is False)
check("1d UNRATED skipped",
      record_verdict("MSFT", mk_decision(action="UNRATED"), mk_forecast(), price=100.0) is False)
check("1e zero price skipped",
      record_verdict("MSFT", mk_decision(), mk_forecast(), price=0) is False)
check("1f NaN price skipped",
      record_verdict("MSFT", mk_decision(), mk_forecast(), price=float("nan")) is False)
check("1g None decision skipped",
      record_verdict("MSFT", None, mk_forecast(), price=100.0) is False)

print("\n[2] score_pending")
# Seed verdicts with explicit past dates, then score with synthetic history.
base = datetime(2026, 9, 1)
d0 = base.strftime("%Y-%m-%d")
check("2a backdated BUY recorded",
      record_verdict("TEST1", mk_decision("BUY", score=70.0), mk_forecast(60.0),
                     price=100.0, date=d0) is True)
check("2b backdated AVOID recorded",
      record_verdict("TEST2", mk_decision("AVOID", score=25.0), mk_forecast(40.0),
                     price=50.0, date=d0) is True)

# TEST1 rises: +5% by day 7, +12% by day 30. TEST2 falls: -4% by 7, -10% by 30.
chart1, chart2 = [], []
for i in range(0, 40):
    day = (base + timedelta(days=i)).strftime("%Y-%m-%d")
    chart1.append({"date": day, "close": 100.0 + (5.0 if 7 <= i < 30 else 12.0 if i >= 30 else 0.0)})
    chart2.append({"date": day, "close": 50.0 + (-2.0 if 7 <= i < 30 else -5.0 if i >= 30 else 0.0)})

check("2c scores TEST1", score_pending("TEST1", chart1) == 1)
check("2d scores TEST2", score_pending("TEST2", chart2) == 1)
check("2e rescoring is idempotent", score_pending("TEST1", chart1) == 0)
check("2f garbage chart safe", score_pending("TEST1", ["x", None, {"date": 3}]) == 0)
check("2g empty chart safe", score_pending("TEST1", []) == 0)
check("2h non-list chart safe", score_pending("TEST1", "junk") == 0)

print("\n[3] accuracy_summary")
s = accuracy_summary()
check("3a ok flag", s.get("ok") is True)
check("3b counts recorded", s.get("total_verdicts") == 3, f"got {s.get('total_verdicts')}")
check("3c two scored at 30d", s.get("scored_30d") == 2, f"got {s.get('scored_30d')}")
buy = s["by_action"].get("BUY") or {}
avoid = s["by_action"].get("AVOID") or {}
check("3d BUY avg 30d return +12%", buy.get("avg_return_30d_pct") == 12.0, f"got {buy}")
check("3e BUY hit rate 100", buy.get("hit_rate_pct") == 100.0, f"got {buy}")
check("3f AVOID hit rate 100 (fell as called)", avoid.get("hit_rate_pct") == 100.0, f"got {avoid}")
check("3g AVOID avg 30d return -10%", avoid.get("avg_return_30d_pct") == -10.0, f"got {avoid}")
cal = {c["stated_prob_up_30d"]: c for c in s.get("calibration", [])}
check("3h >=55% band has TEST1 up", cal.get(">=55%", {}).get("n") == 1
      and cal.get(">=55%", {}).get("realized_up_rate_pct") == 100.0, f"got {cal.get('>=55%')}")
check("3i <=45% band has TEST2 down", cal.get("<=45%", {}).get("n") == 1
      and cal.get("<=45%", {}).get("realized_up_rate_pct") == 0.0, f"got {cal.get('<=45%')}")
check("3j horizons constant sane", HORIZONS == (7, 30))

# Partial maturity: verdict 10 days old — 7d outcome fills, 30d stays open.
d_recent = (datetime.utcnow() - timedelta(days=10)).strftime("%Y-%m-%d")
record_verdict("TEST3", mk_decision("ACCUMULATE", score=60.0), mk_forecast(50.0),
               price=10.0, date=d_recent)
chart3 = [{"date": (datetime.utcnow() - timedelta(days=10) + timedelta(days=i)).strftime("%Y-%m-%d"),
           "close": 10.0 + (0.5 if i >= 7 else 0.0)} for i in range(0, 11)]
check("3k partial scoring updates", score_pending("TEST3", chart3) == 1)
s2 = accuracy_summary()
acc = s2["by_action"].get("ACCUMULATE") or {}
check("3l 7d filled, 30d open", acc.get("avg_return_7d_pct") == 5.0 and acc.get("scored_30d") == 0,
      f"got {acc}")

print(f"\n{'=' * 60}\nRESULT: {PASS} passed, {FAIL} failed\n{'=' * 60}")
sys.exit(1 if FAIL else 0)
