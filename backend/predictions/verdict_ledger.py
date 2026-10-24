"""
Verdict Ledger — the accuracy loop.

Records every System Decision the engine issues and later scores it against
what the market actually did. This is how the system stops being "judgment
encoded in weights" and becomes measurable: hit rate by action, and whether
a stated 58% upside probability is actually right 58% of the time.

ZERO extra network I/O by design:
  - Recording happens inside the analyze request that produced the verdict.
  - Outcome scoring piggybacks on LATER analyze requests for the same ticker:
    their price history already contains the "future" of older verdicts.

Fail-safe everywhere: a ledger problem must never break an analyze response.
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta

# Outcomes are scored at these calendar-day horizons after the verdict date.
HORIZONS = (7, 30)

# Degraded/placeholder prices must never enter the ledger.
_MIN_PRICE = 0.01


def _conn():
    db_path = os.environ.get("DB_PATH", "predictions.db")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS verdict_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            action TEXT NOT NULL,
            stance TEXT,
            conviction INTEGER,
            composite REAL,
            components TEXT,
            prob_up_30 REAL,
            price REAL NOT NULL,
            outcome_7 REAL,
            outcome_30 REAL,
            scored_at TEXT,
            UNIQUE(ticker, date)
        )
    """)
    return conn


def _num(v):
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def record_verdict(ticker, decision, forecast=None, price=None, date=None):
    """Insert today's verdict for a ticker. One row per ticker per day —
    the FIRST verdict of the day stands (deterministic, no revisionism).

    Returns True if a row was written, False if skipped (no valid verdict,
    no valid price, or already recorded today).
    """
    if not isinstance(decision, dict) or not decision.get("ok"):
        return False
    action = decision.get("action")
    if not isinstance(action, str) or action == "UNRATED":
        return False
    px = _num(price)
    if px is None or px < _MIN_PRICE:
        return False

    day = date or datetime.utcnow().strftime("%Y-%m-%d")
    prob30 = None
    fc = (forecast or {}).get("forecasts") if isinstance(forecast, dict) else None
    if isinstance(fc, list):
        for f in fc:
            if isinstance(f, dict) and f.get("days") == 30:
                prob30 = _num(f.get("prob_up"))
                break

    try:
        conn = _conn()
        with conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO verdict_ledger
                   (ticker, date, action, stance, conviction, composite,
                    components, prob_up_30, price)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(ticker).upper(), day, action,
                    decision.get("stance"),
                    int(_num(decision.get("conviction")) or 0),
                    _num(decision.get("score")),
                    json.dumps(decision.get("components") or {}),
                    prob30, px,
                ),
            )
            return cur.rowcount > 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


def score_pending(ticker, chart_data):
    """Backfill outcomes for this ticker's matured, unscored verdicts using
    the price history the caller already fetched (list of {date, close}).

    For each horizon H, the outcome is the pct return to the first close on
    or after verdict_date + H calendar days. Returns number of rows updated.
    """
    if not isinstance(chart_data, list) or not chart_data:
        return 0
    series = []
    for p in chart_data:
        if not isinstance(p, dict):
            continue
        d = p.get("date")
        c = _num(p.get("close"))
        if isinstance(d, str) and len(d) >= 10 and c is not None and c >= _MIN_PRICE:
            series.append((d[:10], c))
    if not series:
        return 0
    series.sort()

    def close_on_or_after(day_iso):
        for d, c in series:
            if d >= day_iso:
                return c
        return None

    updated = 0
    try:
        conn = _conn()
        with conn:
            rows = conn.execute(
                """SELECT id, date, price, outcome_7, outcome_30 FROM verdict_ledger
                   WHERE ticker = ? AND (outcome_7 IS NULL OR outcome_30 IS NULL)""",
                (str(ticker).upper(),),
            ).fetchall()
            for rid, vdate, vprice, o7, o30 in rows:
                try:
                    base = datetime.strptime(vdate, "%Y-%m-%d")
                except (TypeError, ValueError):
                    continue
                vp = _num(vprice)
                if vp is None or vp < _MIN_PRICE:
                    continue
                new7, new30 = o7, o30
                if o7 is None:
                    c = close_on_or_after((base + timedelta(days=HORIZONS[0])).strftime("%Y-%m-%d"))
                    if c is not None:
                        new7 = round((c - vp) / vp * 100.0, 3)
                if o30 is None:
                    c = close_on_or_after((base + timedelta(days=HORIZONS[1])).strftime("%Y-%m-%d"))
                    if c is not None:
                        new30 = round((c - vp) / vp * 100.0, 3)
                if new7 != o7 or new30 != o30:
                    conn.execute(
                        """UPDATE verdict_ledger
                           SET outcome_7 = ?, outcome_30 = ?,
                               scored_at = CASE WHEN ? IS NOT NULL THEN ? ELSE scored_at END
                           WHERE id = ?""",
                        (new7, new30, new30,
                         datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), rid),
                    )
                    updated += 1
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return updated


def accuracy_summary():
    """Aggregate the scored ledger into measured accuracy.

    - by_action: n, avg 7d/30d return, hit rate (bullish verdicts hit when
      the 30d return is positive, bearish when negative; HOLD has no "hit").
    - calibration: stated 30d upside probability bands vs realized up-rate.
    Honest by construction: returns counts so small samples read as small.
    """
    out = {
        "ok": True,
        "total_verdicts": 0,
        "scored_30d": 0,
        "by_action": {},
        "calibration": [],
        "note": ("Hit rate = 30-day outcome agreed with the verdict's direction. "
                 "Small samples mean wide error bars — counts are shown for a reason."),
    }
    try:
        conn = _conn()
        rows = conn.execute(
            """SELECT action, prob_up_30, outcome_7, outcome_30
               FROM verdict_ledger"""
        ).fetchall()
    except Exception as e:
        return {"ok": False, "error": f"ledger read failed: {e}"}
    finally:
        try:
            conn.close()
        except Exception:
            pass

    out["total_verdicts"] = len(rows)
    by = {}
    bands = [
        {"band": "<=45%", "lo": None, "hi": 45.0, "n": 0, "up": 0},
        {"band": "45-55%", "lo": 45.0, "hi": 55.0, "n": 0, "up": 0},
        {"band": ">=55%", "lo": 55.0, "hi": None, "n": 0, "up": 0},
    ]
    for action, prob30, o7, o30 in rows:
        a = by.setdefault(action, {"n": 0, "scored": 0, "sum7": 0.0, "n7": 0,
                                   "sum30": 0.0, "hits": 0, "hit_eligible": 0})
        a["n"] += 1
        if o7 is not None:
            a["sum7"] += o7
            a["n7"] += 1
        if o30 is None:
            continue
        a["scored"] += 1
        a["sum30"] += o30
        out["scored_30d"] += 1
        if action in ("BUY", "ACCUMULATE"):
            a["hit_eligible"] += 1
            if o30 > 0:
                a["hits"] += 1
        elif action in ("REDUCE", "AVOID"):
            a["hit_eligible"] += 1
            if o30 < 0:
                a["hits"] += 1
        p = _num(prob30)
        if p is not None:
            for b in bands:
                if (b["lo"] is None or p > b["lo"]) and (b["hi"] is None or p <= b["hi"]):
                    b["n"] += 1
                    if o30 > 0:
                        b["up"] += 1
                    break

    for action, a in by.items():
        out["by_action"][action] = {
            "verdicts": a["n"],
            "scored_30d": a["scored"],
            "avg_return_7d_pct": round(a["sum7"] / a["n7"], 2) if a["n7"] else None,
            "avg_return_30d_pct": round(a["sum30"] / a["scored"], 2) if a["scored"] else None,
            "hit_rate_pct": (round(a["hits"] / a["hit_eligible"] * 100.0, 1)
                             if a["hit_eligible"] else None),
            "hit_sample": a["hit_eligible"],
        }
    out["calibration"] = [
        {"stated_prob_up_30d": b["band"], "n": b["n"],
         "realized_up_rate_pct": round(b["up"] / b["n"] * 100.0, 1) if b["n"] else None}
        for b in bands
    ]
    return out
