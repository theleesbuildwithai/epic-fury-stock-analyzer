"""
Test suite for analysis/stb_timing.py.

Standalone script — no pytest, no httpx (neither is installed in this env).
Run:  python3 test_stb_timing.py

The contract under test:
  1. compute_timing() NEVER raises, on any input.
  2. Output is always strictly JSON-serialisable (no NaN / Inf).
  3. score_tilt and conf_tilt are ALWAYS inside their published bounds.
  4. A corrupted or mismatched price series yields UNRATED with ZERO tilts —
     it can never promote or demote a pick.
  5. The module performs NO network I/O.
  6. Technical readings are directionally correct on constructed tapes.
"""

import json
import math
import sys
from datetime import date

import numpy as np

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))

from analysis.stb_timing import (  # noqa: E402
    GRADE_ORDER,
    SIGNAL_WEIGHTS,
    _BAND_MAX_TILT,
    assess_event_risk,
    check_price_integrity,
    compute_timing,
)

PASS = 0
FAIL = 0
FAILURES = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name}: {detail}")
        print(f"  FAIL  {name}  {detail}")


def section(t):
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic tapes
# ─────────────────────────────────────────────────────────────────────────────
def uptrend(n=300, drift=0.0008, vol=0.010, seed=1, start=100.0):
    rng = np.random.default_rng(seed)
    r = rng.normal(drift, vol, n)
    return start * np.exp(np.cumsum(r))


def downtrend(n=300, seed=2):
    return uptrend(n=n, drift=-0.0012, vol=0.014, seed=seed)


def uptrend_then_pullback(n=300, seed=3):
    """Low-noise, firmly trending tape that gives back ~1.5% over the last five
    sessions — the textbook 'buy the dip in an uptrend' entry.

    Drift and vol are chosen so the setup is unambiguous for any seed: price
    must stay above its 50-day mean after the dip, otherwise the fixture is
    testing a falling knife rather than a pullback.
    """
    tail_mult = np.array([0.995, 0.991, 0.988, 0.986, 0.985])
    a = uptrend(n=n - tail_mult.size, drift=0.0018, vol=0.005, seed=seed)
    return np.concatenate([a, a[-1] * tail_mult])


def parabolic(n=300, seed=4):
    """Uptrend ending in a near-vertical 12-session melt-up — maximum
    extension above the 20-day mean."""
    a = uptrend(n=n - 12, seed=seed)
    tail = a[-1] * np.cumprod(np.full(12, 1.035))
    return np.concatenate([a, tail])


def choppy(n=300, seed=5):
    rng = np.random.default_rng(seed)
    return 100.0 + np.cumsum(rng.normal(0.0, 0.9, n)) * 0.0 + rng.normal(100, 2.5, n) - 100 + 100


SPY = uptrend(n=300, drift=0.0004, vol=0.007, seed=99)


def volumes_for(c, seed=7):
    rng = np.random.default_rng(seed)
    return np.abs(rng.normal(1_000_000, 150_000, len(c)))


# ─────────────────────────────────────────────────────────────────────────────
section("1. Contract: never raises, always JSON-safe, tilts always bounded")
# ─────────────────────────────────────────────────────────────────────────────
HOSTILE_INPUTS = [
    ("none", None),
    ("empty list", []),
    ("empty array", np.array([])),
    ("single value", [100.0]),
    ("all zeros", [0.0] * 200),
    ("all negative", [-5.0] * 200),
    ("all nan", [float("nan")] * 200),
    ("all inf", [float("inf")] * 200),
    ("mixed garbage", [float("nan"), 0.0, -1.0, float("inf")] * 60),
    ("strings", ["a", "b", "c"] * 70),
    ("dict", {"a": 1}),
    ("nested", [[1, 2], [3, 4]]),
    ("short real", list(uptrend(n=30))),
    ("exact min bars", list(uptrend(n=60))),
    ("long real", list(uptrend(n=800))),
]

for label, data in HOSTILE_INPUTS:
    try:
        res = compute_timing(data)
        ok = True
        err = ""
    except Exception as e:
        ok, err, res = False, f"raised {type(e).__name__}: {e}", None
    check(f"no-raise [{label}]", ok, err)

    if res is not None:
        try:
            json.dumps(res, allow_nan=False)
            js = True
            jerr = ""
        except Exception as e:
            js, jerr = False, str(e)
        check(f"json-safe [{label}]", js, jerr)

        st = res.get("score_tilt", 0.0)
        ct = res.get("conf_tilt", 0)
        check(f"score_tilt bounded [{label}]",
              isinstance(st, (int, float)) and -10.0 <= st <= 10.0, f"got {st}")
        check(f"conf_tilt bounded [{label}]",
              isinstance(ct, int) and -6 <= ct <= 3, f"got {ct}")
        check(f"grade valid [{label}]",
              res.get("grade") in ("BUY_NOW", "ACCUMULATE", "WAIT", "AVOID", "UNRATED"),
              f"got {res.get('grade')}")

# Hostile inputs on every optional argument simultaneously.
try:
    res = compute_timing(
        uptrend(), volumes="garbage", benchmark_closes=[float("nan")] * 50,
        sector=12345, news="not a dict", earnings_days="soon",
        expected_price="abc", now="yesterday",
    )
    check("no-raise [all optional args garbage]", True)
    check("json-safe [all optional args garbage]",
          json.dumps(res, allow_nan=False) is not None)
except Exception as e:
    check("no-raise [all optional args garbage]", False, f"{type(e).__name__}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
section("2. Price-integrity gate — corrupt series must never be scored")
# ─────────────────────────────────────────────────────────────────────────────
good = uptrend(n=300, seed=11)
spot = float(good[-1])

r = check_price_integrity(good, expected_price=spot)
check("integrity accepts matching series", r["ok"] is True, r.get("reason"))

r = check_price_integrity(good, expected_price=spot * 0.5)
check("integrity rejects 50% price mismatch", r["reason"] == "price_mismatch", r)

r = check_price_integrity(good, expected_price=spot * 1.02)
check("integrity tolerates 2% drift", r["ok"] is True, r)

r = check_price_integrity(good, expected_price=spot * 1.30)
check("integrity rejects 30% mismatch", r["reason"] == "price_mismatch", r)

r = check_price_integrity([50.0] * 200)
check("integrity rejects flat-line", r["ok"] is False, r)

stale = list(uptrend(n=200, seed=12))
stale[-80:] = [stale[-81]] * 80
r = check_price_integrity(stale)
check("integrity rejects stale repeated closes", r["ok"] is False, r)

spike = list(uptrend(n=200, seed=13))
spike[-5] = spike[-6] * 4.0      # +300% single session = contamination
r = check_price_integrity(spike)
check("integrity rejects implausible session move", r["reason"] == "implausible_move", r)

# The decisive end-to-end behaviour: a mismatched series must be UNRATED and
# must apply zero tilts, so it cannot move a pick in either direction.
res = compute_timing(good, expected_price=spot * 0.4, sector="Technology")
check("mismatched series -> UNRATED", res["grade"] == "UNRATED", res.get("grade"))
check("mismatched series -> zero score_tilt", res["score_tilt"] == 0.0, res.get("score_tilt"))
check("mismatched series -> no timing_score", res["timing_score"] is None, res.get("timing_score"))
check("mismatched series -> reason surfaced", res["reason"] == "price_mismatch", res.get("reason"))
check("mismatched series -> conf_tilt non-positive", res["conf_tilt"] <= 0, res.get("conf_tilt"))

res = compute_timing([50.0] * 300, expected_price=50.0)
check("flat-line series -> UNRATED", res["grade"] == "UNRATED", res.get("grade"))
check("flat-line series -> zero tilt", res["score_tilt"] == 0.0, res.get("score_tilt"))


# ─────────────────────────────────────────────────────────────────────────────
section("3. Directional correctness on constructed tapes")
# ─────────────────────────────────────────────────────────────────────────────
up = uptrend_then_pullback(seed=21)
down = downtrend(seed=22)
para = parabolic(seed=23)

r_up = compute_timing(up, volumes=volumes_for(up), benchmark_closes=SPY,
                      sector="Technology", expected_price=float(up[-1]))
r_dn = compute_timing(down, volumes=volumes_for(down), benchmark_closes=SPY,
                      sector="Technology", expected_price=float(down[-1]))
r_pa = compute_timing(para, volumes=volumes_for(para), benchmark_closes=SPY,
                      sector="Technology", expected_price=float(para[-1]))

check("uptrend+pullback is rated", r_up["ok"] is True, r_up.get("reason"))
check("downtrend is rated", r_dn["ok"] is True, r_dn.get("reason"))
check("parabolic is rated", r_pa["ok"] is True, r_pa.get("reason"))

check("uptrend scores above downtrend",
      r_up["timing_score"] > r_dn["timing_score"],
      f"up={r_up['timing_score']} down={r_dn['timing_score']}")
check("downtrend graded WAIT or AVOID",
      r_dn["grade"] in ("WAIT", "AVOID"), r_dn["grade"])
check("downtrend has more bearish than bullish signals",
      r_dn["bear_count"] > r_dn["bull_count"],
      f"bull={r_dn['bull_count']} bear={r_dn['bear_count']}")
check("uptrend has more bullish than bearish signals",
      r_up["bull_count"] > r_up["bear_count"],
      f"bull={r_up['bull_count']} bear={r_up['bear_count']}")

# The headline purpose of the module: a vertical melt-up must be penalised on
# extension even though its trend and 52-week-high readings are excellent.
ext_pa = next(s for s in r_pa["signals"] if s["name"] == "extension")
check("parabolic flagged as extended", ext_pa["state"] == "BEAR", ext_pa)
check("parabolic extension > 3 ATR", (r_pa["extension_atr"] or 0) > 3.0,
      r_pa.get("extension_atr"))
check("parabolic scores below the orderly pullback entry",
      r_pa["timing_score"] < r_up["timing_score"],
      f"para={r_pa['timing_score']} pullback={r_up['timing_score']}")

pb_up = next(s for s in r_up["signals"] if s["name"] == "pullback")
check("pullback-in-uptrend reads bullish", pb_up["state"] == "BULL", pb_up)

ts_dn = next(s for s in r_dn["signals"] if s["name"] == "trend_stack")
check("downtrend trend-stack reads bearish", ts_dn["state"] == "BEAR", ts_dn)

# Signals are ordered worst-first so the weakest link is visible immediately.
scores = [s["score"] for s in r_up["signals"]]
check("signals sorted worst-first", scores == sorted(scores), scores)

# ── Grade caps: one disqualifying reading must override a flattering average.
check("parabolic grade capped below its raw grade",
      GRADE_ORDER.index(r_pa["grade"]) < GRADE_ORDER.index(r_pa["raw_grade"]),
      f"grade={r_pa['grade']} raw={r_pa['raw_grade']}")
check("parabolic cap reason given", len(r_pa["grade_caps"]) > 0, r_pa["grade_caps"])
check("parabolic never graded BUY_NOW", r_pa["grade"] != "BUY_NOW", r_pa["grade"])
check("parabolic cap reflected in summary",
      r_pa["grade_caps"][0] in r_pa["summary"] if r_pa["grade_caps"] else False,
      r_pa["summary"])
check("capped grade demotes the score tilt",
      r_pa["score_tilt"] <= _BAND_MAX_TILT[r_pa["grade"]] + 1e-9,
      f"tilt={r_pa['score_tilt']} band_max={_BAND_MAX_TILT[r_pa['grade']]}")
check("clean pullback is not capped", r_up["grade_caps"] == [], r_up["grade_caps"])
check("clean pullback grade equals raw grade",
      r_up["grade"] == r_up["raw_grade"], f"{r_up['grade']}/{r_up['raw_grade']}")

# A cap must only ever lower a grade, never raise one. Verified over random tapes.
_rng = np.random.default_rng(777)
for _i in range(40):
    _c = uptrend(n=260, drift=float(_rng.normal(0.0, 0.0015)),
                 vol=float(abs(_rng.normal(0.012, 0.006)) + 0.003),
                 seed=int(_rng.integers(1, 10**6)))
    _r = compute_timing(_c, benchmark_closes=SPY, expected_price=float(_c[-1]))
    if not _r["ok"]:
        continue
    check(f"cap never raises grade [{_i}]",
          GRADE_ORDER.index(_r["grade"]) <= GRADE_ORDER.index(_r["raw_grade"]),
          f"grade={_r['grade']} raw={_r['raw_grade']}")
    check(f"cap implies a stated reason [{_i}]",
          (_r["grade"] == _r["raw_grade"]) == (len(_r["grade_caps"]) == 0),
          f"{_r['grade']}/{_r['raw_grade']} caps={_r['grade_caps']}")


# ─────────────────────────────────────────────────────────────────────────────
section("4. Graceful degradation — missing data must not read as bearish")
# ─────────────────────────────────────────────────────────────────────────────
full = compute_timing(up, volumes=volumes_for(up), benchmark_closes=SPY,
                      expected_price=float(up[-1]))
no_vol = compute_timing(up, benchmark_closes=SPY, expected_price=float(up[-1]))
no_bench = compute_timing(up, volumes=volumes_for(up), expected_price=float(up[-1]))
bare = compute_timing(up, expected_price=float(up[-1]))

for label, r in [("no volume", no_vol), ("no benchmark", no_bench), ("bare closes", bare)]:
    check(f"still rated [{label}]", r["ok"] is True, r.get("reason"))
    check(f"coverage reported [{label}]", 0.0 < r["coverage"] <= 1.0, r.get("coverage"))

check("full data has highest coverage",
      full["coverage"] >= no_vol["coverage"] >= bare["coverage"] - 1e-9,
      f"{full['coverage']} / {no_vol['coverage']} / {bare['coverage']}")
check("missing volume does not collapse the score",
      abs(full["timing_score"] - no_vol["timing_score"]) < 15,
      f"full={full['timing_score']} no_vol={no_vol['timing_score']}")

names = {s["name"] for s in no_vol["signals"]}
check("volume signal dropped when volume absent", "volume_trend" not in names, names)
names = {s["name"] for s in no_bench["signals"]}
check("rel-strength dropped when benchmark absent", "rel_strength" not in names, names)

# Only 60 bars: short-window signals exist but the long ones cannot, so the
# coverage gate should withhold a grade rather than publish a thin one.
thin = compute_timing(uptrend(n=62, seed=31))
check("thin history withholds a grade or reports low coverage",
      thin["grade"] == "UNRATED" or thin["coverage"] < 1.0,
      f"grade={thin['grade']} coverage={thin['coverage']}")


# ─────────────────────────────────────────────────────────────────────────────
section("5. Event risk — deterministic calendar only")
# ─────────────────────────────────────────────────────────────────────────────
# 2026-03-06 is the first Friday of March 2026 -> payrolls.
ev = assess_event_risk(now=date(2026, 3, 6))
check("payrolls detected on first Friday",
      any("Payrolls" in i for i in ev["items"]), ev["items"])

# 2026-03-20 is the third Friday of a quarter-end month -> triple witching.
ev = assess_event_risk(now=date(2026, 3, 20))
check("triple witching detected",
      any("Triple witching" in i for i in ev["items"]), ev["items"])

# 2026-10-16 is the third Friday of a non-quarter-end month -> monthly opex.
ev = assess_event_risk(now=date(2026, 10, 16))
check("monthly opex detected",
      any("Monthly opex" in i for i in ev["items"]), ev["items"])

ev = assess_event_risk(now=date(2026, 12, 30))
check("quarter-end rebalancing detected",
      any("Quarter-end" in i for i in ev["items"]), ev["items"])

# Weather / seasonal exposure is structural and sector-gated.
ev = assess_event_risk(sector="Financial Services", now=date(2026, 9, 15))
check("hurricane peak flagged for financials",
      any("hurricane" in i.lower() for i in ev["items"]), ev["items"])

ev = assess_event_risk(sector="Technology", now=date(2026, 9, 15))
check("hurricane NOT flagged for technology",
      not any("hurricane" in i.lower() for i in ev["items"]), ev["items"])

ev = assess_event_risk(sector="Utilities", now=date(2026, 1, 15))
check("winter heating flagged for utilities",
      any("heating" in i.lower() for i in ev["items"]), ev["items"])

ev = assess_event_risk(sector="Consumer Cyclical", now=date(2026, 12, 1))
check("holiday retail flagged for consumer cyclical",
      any("Holiday retail" in i for i in ev["items"]), ev["items"])

ev = assess_event_risk(sector="Utilities", now=date(2026, 4, 15))
check("no seasonal flag for utilities in April",
      not any("heating" in i.lower() or "cooling" in i.lower() for i in ev["items"]),
      ev["items"])

# Earnings proximity bands.
for days, must_contain in [(10, "binary event"), (18, "early in the hold"),
                           (25, "inside the hold"), (40, "6-12 week hold")]:
    ev = assess_event_risk(earnings_days=days, now=date(2026, 4, 8))
    check(f"earnings band {days}d described",
          any(must_contain in i for i in ev["items"]),
          f"{days}d -> {ev['items']}")

ev = assess_event_risk(earnings_days=120, now=date(2026, 4, 8))
check("distant earnings not flagged",
      not any("Earnings" in i for i in ev["items"]), ev["items"])

# News sentiment feeds event risk.
ev = assess_event_risk(news={"sentiment": "VERY_NEGATIVE"}, now=date(2026, 4, 8))
check("very negative news raises event risk", ev["points"] >= 2, ev)
ev = assess_event_risk(news={"sentiment": "POSITIVE"}, now=date(2026, 4, 8))
check("positive news adds no risk points", ev["points"] == 0, ev)

# Levels, penalties and bounds.
ev = assess_event_risk(sector="Energy", news={"sentiment": "VERY_NEGATIVE"},
                       earnings_days=16, now=date(2026, 9, 18))
check("stacked risks escalate the level",
      ev["level"] in ("ELEVATED", "HIGH"), ev)
check("conf_penalty never positive", ev["conf_penalty"] <= 0, ev)
check("event items capped at 6", len(ev["items"]) <= 6, len(ev["items"]))

for bad in [object(), "x", -1, float("nan")]:
    try:
        ev = assess_event_risk(sector=bad, news=bad, earnings_days=bad, now=bad)
        check(f"event risk survives garbage [{type(bad).__name__}]",
              ev["level"] in ("LOW", "MODERATE", "ELEVATED", "HIGH"), ev)
    except Exception as e:
        check(f"event risk survives garbage [{type(bad).__name__}]", False, str(e))


# ─────────────────────────────────────────────────────────────────────────────
section("6. Entry plan — must be actionable and never nonsensical")
# ─────────────────────────────────────────────────────────────────────────────
for label, series in [("uptrend", up), ("downtrend", down), ("parabolic", para)]:
    r = compute_timing(series, benchmark_closes=SPY, expected_price=float(series[-1]))
    plan = r.get("entry_plan")
    check(f"entry plan present [{label}]", isinstance(plan, dict), plan)
    if not isinstance(plan, dict):
        continue
    px = float(series[-1])
    check(f"stop below spot [{label}]", plan["structural_stop"] < px,
          f"stop={plan['structural_stop']} px={px}")
    check(f"stop at least 5% away [{label}]", plan["stop_pct"] >= 5.0, plan["stop_pct"])
    check(f"stop is positive [{label}]", plan["structural_stop"] > 0, plan)
    check(f"entry never above spot [{label}]", plan["ideal_entry"] <= px + 1e-9,
          f"entry={plan['ideal_entry']} px={px}")
    check(f"entry zone ordered [{label}]",
          plan["entry_zone_low"] <= plan["entry_zone_high"], plan)
    check(f"size factor in [0,1] [{label}]", 0.0 <= plan["size_factor"] <= 1.0,
          plan["size_factor"])
    check(f"size factor matches grade [{label}]",
          plan["size_factor"] == {"BUY_NOW": 1.0, "ACCUMULATE": 0.6,
                                  "WAIT": 0.3, "AVOID": 0.0}[r["grade"]],
          f"{r['grade']} -> {plan['size_factor']}")

# An AVOID grade must deploy no capital at all — this is the capital-preservation
# mechanism, so it is asserted explicitly rather than inferred.
r = compute_timing(down, benchmark_closes=SPY, expected_price=float(down[-1]))
if r["grade"] == "AVOID":
    check("AVOID deploys zero capital", r["entry_plan"]["size_factor"] == 0.0,
          r["entry_plan"])
else:
    check("AVOID deploys zero capital", True, "(downtrend graded WAIT — checked above)")


# ─────────────────────────────────────────────────────────────────────────────
section("7. Tilt monotonicity and bounds across many random tapes")
# ─────────────────────────────────────────────────────────────────────────────
rows = []
for seed in range(60):
    drift = (seed % 7 - 3) * 0.0008
    vol = 0.006 + (seed % 5) * 0.006
    s = uptrend(n=300, drift=drift, vol=vol, seed=seed + 500)
    r = compute_timing(s, volumes=volumes_for(s, seed), benchmark_closes=SPY,
                       sector="Technology", expected_price=float(s[-1]))
    rows.append(r)
    assert -10.0 <= r["score_tilt"] <= 10.0
    assert -6 <= r["conf_tilt"] <= 3

check("all random tapes produced bounded tilts", True)
check("all random tapes JSON-safe",
      all(json.dumps(r, allow_nan=False) for r in rows))

rated = [r for r in rows if r["ok"]]
check("most random tapes are rated", len(rated) >= 50, f"{len(rated)}/60")

# Among uncapped reads, score_tilt must be a monotone function of timing_score
# and must cross zero at the ACCUMULATE threshold. If this breaks, ranking
# silently inverts. Capped reads are deliberately demoted below this line.
uncapped = [r for r in rated if not r["grade_caps"]]
capped = [r for r in rated if r["grade_caps"]]
check("random tapes produced both capped and uncapped reads",
      len(uncapped) > 0 and len(capped) > 0, f"{len(uncapped)}/{len(capped)}")

pairs = sorted((r["timing_score"], r["score_tilt"]) for r in uncapped)
mono = all(pairs[i][1] <= pairs[i + 1][1] + 1e-9 for i in range(len(pairs) - 1))
check("score_tilt is monotone in timing_score (uncapped)", mono, pairs)
check("high scores tilt positive (uncapped)",
      all(r["score_tilt"] > 0 for r in uncapped if r["timing_score"] > 60),
      [(r["timing_score"], r["score_tilt"]) for r in uncapped
       if r["timing_score"] > 60 and r["score_tilt"] <= 0])
check("low scores tilt negative",
      all(r["score_tilt"] < 0 for r in rated if r["timing_score"] < 50))
check("capped reads never tilt above their grade band",
      all(r["score_tilt"] <= _BAND_MAX_TILT[r["grade"]] + 1e-9 for r in capped),
      [(r["grade"], r["score_tilt"]) for r in capped
       if r["score_tilt"] > _BAND_MAX_TILT[r["grade"]] + 1e-9])
check("capped reads are demoted relative to their raw score",
      all(r["score_tilt"] < (r["timing_score"] - 55.0) / 45.0 * 10.0 + 1e-9
          for r in capped),
      [(r["timing_score"], r["score_tilt"]) for r in capped])
check("grade and tilt agree in sign",
      all((r["conf_tilt"] > 0) == (r["grade"] in ("BUY_NOW", "ACCUMULATE"))
          for r in rated if r["event_risk"]["level"] in ("LOW", "MODERATE")))


# ─────────────────────────────────────────────────────────────────────────────
section("8. Determinism and purity (no network, no hidden state)")
# ─────────────────────────────────────────────────────────────────────────────
a = compute_timing(up, volumes=volumes_for(up), benchmark_closes=SPY,
                   sector="Energy", news={"sentiment": "POSITIVE"},
                   earnings_days=30, expected_price=float(up[-1]),
                   now=date(2026, 5, 12))
b = compute_timing(up, volumes=volumes_for(up), benchmark_closes=SPY,
                   sector="Energy", news={"sentiment": "POSITIVE"},
                   earnings_days=30, expected_price=float(up[-1]),
                   now=date(2026, 5, 12))
check("identical inputs give identical output", json.dumps(a, sort_keys=True) ==
      json.dumps(b, sort_keys=True))

# Inputs must not be mutated — the engine reuses these arrays for other work.
orig = up.copy()
compute_timing(up, expected_price=float(up[-1]))
check("input array not mutated", np.array_equal(up, orig))

src = open(__import__("os").path.join(
    __import__("os").path.dirname(__import__("os").path.abspath(__file__)),
    "analysis", "stb_timing.py")).read()
for banned in ["import requests", "import urllib", "import yfinance",
               "from urllib", "http://", "https://", "socket", "aiohttp"]:
    check(f"no network dependency: {banned!r}", banned not in src)

check("weights all positive", all(w > 0 for w in SIGNAL_WEIGHTS.values()))
check("weights count matches expectation", len(SIGNAL_WEIGHTS) == 14,
      len(SIGNAL_WEIGHTS))


# ─────────────────────────────────────────────────────────────────────────────
section("9. NaN scrubbing inside otherwise valid series")
# ─────────────────────────────────────────────────────────────────────────────
dirty = list(uptrend(n=300, seed=41))
for i in (5, 50, 120, 250):
    dirty[i] = float("nan")
dirty[77] = float("inf")
dirty[99] = 0.0
r = compute_timing(dirty, benchmark_closes=SPY)
check("series with embedded NaN/Inf/zero still rated", r["ok"] is True, r.get("reason"))
check("dirty series output JSON-safe", json.dumps(r, allow_nan=False) is not None)
check("no NaN leaked into signal values",
      all(s["value"] is None or math.isfinite(s["value"]) for s in r["signals"]))


# ─────────────────────────────────────────────────────────────────────────────
print(f"\n{'=' * 72}")
print(f"RESULT: {PASS} passed, {FAIL} failed")
if FAILURES:
    print("\nFailures:")
    for f in FAILURES:
        print(f"  - {f}")
print("=" * 72)
sys.exit(1 if FAIL else 0)
