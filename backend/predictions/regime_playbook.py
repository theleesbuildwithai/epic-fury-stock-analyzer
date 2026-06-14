"""
Regime Playbook — Lessons from history.

For every major historical market event, pull what actually worked:
sector ETFs + factor ETFs over the period. Store top winners and
losers as a "playbook" so when the current market regime matches a
historical pattern, the system can recommend what worked then.

Example: when current regime classifier says "bear_highvol" matching
the COVID March 2020 profile, the playbook says:
  "Utilities + Staples + Healthcare led; Energy + Financials worst.
   Quality + Low-vol factors strong; Momentum got crushed."

SAFETY:
  - All functions try/except wrapped, return {"ok": False, ...}
  - Heavy build idempotent (uses INSERT OR REPLACE on event_label)
  - Cannot crash trading or scheduler
  - Soft-fails individual events so partial build still works
"""

import logging
import json
import sqlite3
import os
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


# 11 SPDR sector ETFs — full coverage of the S&P
SECTOR_ETFS = {
    "XLK": "Technology",
    "XLF": "Financials",
    "XLE": "Energy",
    "XLV": "Healthcare",
    "XLP": "Consumer Staples",
    "XLY": "Consumer Discretionary",
    "XLI": "Industrials",
    "XLU": "Utilities",
    "XLB": "Materials",
    "XLRE": "Real Estate",
    "XLC": "Communications",
}

# 5 factor ETFs — Morningstar/MSCI factor suite
FACTOR_ETFS = {
    "MTUM": "Momentum",
    "VLUE": "Value",
    "QUAL": "Quality",
    "USMV": "Low Volatility",
    "SIZE": "Small Size",
}


# Historical events to analyze. Tuple: (label, start, end, description, regime_label)
# regime_label uses the same vocabulary as backtest_pro._classify_regime
HISTORICAL_EVENTS = [
    ("covid_crash_2020",       "2020-02-15", "2020-04-30",
     "COVID-19 pandemic crash + emergency Fed response",
     "bear_highvol"),
    ("covid_recovery_2020",    "2020-05-01", "2020-08-31",
     "Post-COVID V-shaped recovery (Fed liquidity flood)",
     "bull_highvol"),
    ("reopening_2021",         "2021-01-01", "2021-12-31",
     "2021 reopening + meme stocks + low rates bull market",
     "bull_lowvol"),
    ("inflation_bear_2022",    "2022-01-01", "2022-10-31",
     "2022 inflation spike + aggressive Fed hikes",
     "bear_midvol"),
    ("disinflation_2023h1",    "2022-11-01", "2023-06-30",
     "2022 trough + 2023 H1 disinflation rally",
     "bull_midvol"),
    ("svb_crisis_2023",        "2023-03-01", "2023-04-15",
     "SVB collapse + regional banking crisis",
     "sideways_highvol"),
    ("ai_boom_2023h2",         "2023-04-01", "2023-12-31",
     "Mega-cap AI breakout (NVDA, MSFT, GOOGL)",
     "bull_lowvol"),
    ("aug_2024_volspike",      "2024-07-25", "2024-09-15",
     "August 2024 yen-carry unwind + vol spike",
     "sideways_highvol"),
]


def _get_db():
    db_path = os.environ.get("DB_PATH", "predictions.db")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_playbook_table():
    """Create the playbook table on startup. Idempotent."""
    try:
        conn = _get_db()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS regime_playbooks (
                    event_label TEXT PRIMARY KEY,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    description TEXT,
                    regime_label TEXT,
                    spy_return_pct REAL,
                    top_sectors TEXT,        -- JSON list of {ticker, name, return_pct}
                    bottom_sectors TEXT,     -- JSON list
                    top_factor TEXT,         -- factor ticker
                    bottom_factor TEXT,
                    factors_detail TEXT,     -- JSON: all factors with returns
                    insights TEXT,
                    computed_at TEXT NOT NULL
                );
            """)
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"init_playbook_table soft-fail: {e}")


def auto_rebuild_if_empty():
    """Called from init_db on startup. If the regime_playbooks table
    is empty (e.g. fresh container, ephemeral storage reset), spawns
    a BACKGROUND daemon thread to rebuild all 8 playbooks via yfinance.

    NEVER blocks startup. NEVER raises. The build itself is heavy
    (~10-90s of yfinance calls) but runs out-of-band so it cannot
    affect trade firing or scheduler jobs."""
    try:
        # Quick check — must be cheap (single SELECT COUNT)
        conn = _get_db()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM regime_playbooks"
            ).fetchone()
            n = int(row["n"]) if row else 0
        finally:
            conn.close()

        if n > 0:
            return {"ok": True, "skipped": True, "rows": n,
                    "reason": "playbook already populated"}

        # Empty — spawn background rebuild
        import threading
        def _rebuild():
            try:
                logger.warning("AUTO-REBUILD playbooks: starting background fetch")
                result = build_all_playbooks()
                if result.get("ok"):
                    logger.warning(
                        f"AUTO-REBUILD playbooks: complete — "
                        f"{result.get('succeeded')}/{result.get('events_total')} events"
                    )
                else:
                    logger.warning(
                        f"AUTO-REBUILD playbooks: failed — {result.get('reason')}"
                    )
            except Exception as _e:
                logger.warning(f"AUTO-REBUILD playbooks: exception {_e}")
        try:
            threading.Thread(target=_rebuild, daemon=True,
                             name="regime-playbook-auto-rebuild").start()
        except Exception as _te:
            logger.warning(f"AUTO-REBUILD spawn fail: {_te}")
        return {"ok": True, "rebuild_spawned": True, "rows": 0}
    except Exception as e:
        logger.warning(f"auto_rebuild_if_empty soft-fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}


def _safe_period_returns(tickers: list, start: str, end: str) -> dict:
    """Pull total returns for each ticker over [start, end].
    Returns {ticker: return_pct}. Skips tickers that fail. Never raises."""
    out = {}
    try:
        import yfinance as yf
        import pandas as pd
        df = yf.download(tickers, start=start, end=end, progress=False,
                         auto_adjust=True, threads=True, group_by="ticker")
        if df is None or df.empty:
            return out

        for sym in tickers:
            try:
                if isinstance(df.columns, pd.MultiIndex):
                    if sym in df.columns.get_level_values(0):
                        s = df[(sym, "Close")].dropna()
                        if len(s) >= 2:
                            ret = (float(s.iloc[-1]) / float(s.iloc[0]) - 1) * 100
                            out[sym] = round(ret, 2)
                elif len(tickers) == 1:
                    s = df["Close"].dropna()
                    if len(s) >= 2:
                        ret = (float(s.iloc[-1]) / float(s.iloc[0]) - 1) * 100
                        out[sym] = round(ret, 2)
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"_safe_period_returns soft-fail: {e}")
    return out


def analyze_historical_event(event_label: str, start: str, end: str,
                              description: str = "",
                              regime_label: str = "") -> dict:
    """Pure function: pulls sector + factor returns for a single event,
    computes top/bottom + insights. Does NOT write to DB."""
    try:
        # SPY benchmark
        spy_ret = _safe_period_returns(["SPY"], start, end).get("SPY", 0.0)

        # Sectors
        sector_rets = _safe_period_returns(list(SECTOR_ETFS.keys()), start, end)
        if not sector_rets:
            return {"ok": False, "reason": "no_sector_data",
                    "event_label": event_label}

        sectors_sorted = sorted(sector_rets.items(), key=lambda x: x[1],
                                 reverse=True)
        top_sectors = [{"ticker": t, "name": SECTOR_ETFS.get(t, t),
                        "return_pct": r,
                        "alpha_vs_spy_pct": round(r - spy_ret, 2)}
                       for t, r in sectors_sorted[:3]]
        bottom_sectors = [{"ticker": t, "name": SECTOR_ETFS.get(t, t),
                           "return_pct": r,
                           "alpha_vs_spy_pct": round(r - spy_ret, 2)}
                          for t, r in sectors_sorted[-3:]]

        # Factors (may have less coverage on older periods)
        factor_rets = _safe_period_returns(list(FACTOR_ETFS.keys()), start, end)
        factors_detail = []
        if factor_rets:
            factors_sorted = sorted(factor_rets.items(),
                                     key=lambda x: x[1], reverse=True)
            factors_detail = [{"ticker": t, "name": FACTOR_ETFS.get(t, t),
                               "return_pct": r,
                               "alpha_vs_spy_pct": round(r - spy_ret, 2)}
                              for t, r in factors_sorted]
            top_factor = factors_sorted[0][0]
            bottom_factor = factors_sorted[-1][0]
        else:
            top_factor = None
            bottom_factor = None

        # Auto-generated insight string
        top_names = ", ".join(s["name"] for s in top_sectors)
        bot_names = ", ".join(s["name"] for s in bottom_sectors)
        insight = (f"Top: {top_names}. Bottom: {bot_names}. "
                   f"SPY {spy_ret:+.1f}%.")
        if top_factor:
            insight += (f" Best factor: {FACTOR_ETFS.get(top_factor, top_factor)}; "
                        f"worst: {FACTOR_ETFS.get(bottom_factor, bottom_factor)}.")

        return {
            "ok": True,
            "event_label": event_label,
            "period": [start, end],
            "description": description,
            "regime_label": regime_label,
            "spy_return_pct": spy_ret,
            "top_sectors": top_sectors,
            "bottom_sectors": bottom_sectors,
            "top_factor": top_factor,
            "bottom_factor": bottom_factor,
            "factors_detail": factors_detail,
            "insight": insight,
        }
    except Exception as e:
        logger.warning(f"analyze_historical_event {event_label} fail: {e}")
        return {"ok": False, "reason": str(e)[:200],
                "event_label": event_label}


def build_all_playbooks() -> dict:
    """Heavy: analyze all HISTORICAL_EVENTS, store each in DB.
    Idempotent (uses INSERT OR REPLACE on event_label).
    Each event's failure is independent — partial build still works."""
    try:
        init_playbook_table()
        results = []
        succeeded = 0
        failed = 0
        for label, start, end, desc, regime in HISTORICAL_EVENTS:
            try:
                # POST-DEPLOY FIX: yfinance occasionally fails transiently.
                # Retry once after a brief pause before giving up — observed
                # in production where 2 of 8 events failed first try then
                # succeeded second try. Retry is cheap (single API call).
                analysis = analyze_historical_event(label, start, end,
                                                     desc, regime)
                if not analysis.get("ok"):
                    try:
                        time.sleep(2)
                    except Exception:
                        pass
                    analysis = analyze_historical_event(label, start, end,
                                                         desc, regime)
                if not analysis.get("ok"):
                    results.append({"event": label, "ok": False,
                                    "reason": analysis.get("reason")})
                    failed += 1
                    continue

                # Persist
                conn = _get_db()
                try:
                    conn.execute(
                        """INSERT OR REPLACE INTO regime_playbooks
                           (event_label, start_date, end_date, description,
                            regime_label, spy_return_pct, top_sectors,
                            bottom_sectors, top_factor, bottom_factor,
                            factors_detail, insights, computed_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (label, start, end, desc, regime,
                         float(analysis["spy_return_pct"]),
                         json.dumps(analysis["top_sectors"]),
                         json.dumps(analysis["bottom_sectors"]),
                         analysis["top_factor"], analysis["bottom_factor"],
                         json.dumps(analysis["factors_detail"]),
                         analysis["insight"],
                         datetime.utcnow().isoformat())
                    )
                    conn.commit()
                finally:
                    conn.close()

                results.append({"event": label, "ok": True,
                                "spy_return_pct": analysis["spy_return_pct"],
                                "top": [s["ticker"] for s in analysis["top_sectors"]],
                                "bottom": [s["ticker"] for s in analysis["bottom_sectors"]]})
                succeeded += 1
            except Exception as _ee:
                logger.debug(f"build_all_playbooks event {label} fail: {_ee}")
                results.append({"event": label, "ok": False,
                                "reason": str(_ee)[:200]})
                failed += 1

        return {
            "ok": True,
            "events_total": len(HISTORICAL_EVENTS),
            "succeeded": succeeded,
            "failed": failed,
            "results": results,
            "computed_at": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        logger.warning(f"build_all_playbooks fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}


def _row_to_dict(r) -> dict:
    """Decode DB row JSON columns back to lists."""
    d = dict(r)
    for k in ("top_sectors", "bottom_sectors", "factors_detail"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                d[k] = []
    return d


def get_playbook(event_label: str) -> dict:
    """Returns one playbook by label. Soft-fails."""
    try:
        conn = _get_db()
        try:
            row = conn.execute(
                "SELECT * FROM regime_playbooks WHERE event_label = ?",
                (event_label,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return {"ok": False, "reason": "not_found",
                    "available_events": [e[0] for e in HISTORICAL_EVENTS]}
        return {"ok": True, "playbook": _row_to_dict(row)}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


def get_all_playbooks() -> dict:
    """Returns all stored playbooks. Soft-fails to empty list."""
    try:
        conn = _get_db()
        try:
            rows = conn.execute(
                "SELECT * FROM regime_playbooks ORDER BY start_date DESC"
            ).fetchall()
        finally:
            conn.close()
        return {
            "ok": True,
            "count": len(rows),
            "playbooks": [_row_to_dict(r) for r in rows],
        }
    except Exception as e:
        logger.warning(f"get_all_playbooks fail: {e}")
        return {"ok": False, "reason": str(e)[:200], "playbooks": []}


def _classify_current_regime() -> dict:
    """Use the existing backtest_pro classifier on the most recent SPY
    data. Returns {regime_label, classification reason}. Never raises."""
    try:
        from predictions.backtest_pro import _classify_regime
        spy = _safe_period_returns(["SPY"], None, None)  # may not work without dates
        # Better: pull recent SPY history directly
        import yfinance as yf
        import threading as _rp_thr
        end_dt = datetime.utcnow()
        start_dt = end_dt - timedelta(days=300)
        _rp_r = [None]
        _rp_t = _rp_thr.Thread(
            target=lambda r=_rp_r, s=start_dt, e=end_dt: r.__setitem__(
                0, yf.download("SPY", start=s.strftime("%Y-%m-%d"),
                               end=e.strftime("%Y-%m-%d"),
                               progress=False, auto_adjust=True)
            ), daemon=True)
        _rp_t.start(); _rp_t.join(timeout=15)
        df = _rp_r[0]
        if df is None or df.empty or len(df) < 200:
            try:
                from analytics.multi_source_adapter import get_historical_any_source
                df = get_historical_any_source("SPY", "1y")
            except Exception:
                pass
        if df is None or df.empty or len(df) < 200:
            return {"ok": False, "reason": "insufficient_spy_data"}
        closes_raw = df["Close"].dropna().values.tolist()
        # FIX: yfinance occasionally returns nested lists for single-ticker
        # downloads. Flatten + force float so sum() doesn't fail with
        # "unsupported operand type(s) for +: 'int' and 'list'".
        if closes_raw and isinstance(closes_raw[0], list):
            closes = [float(c[0]) for c in closes_raw]
        else:
            closes = [float(c) for c in closes_raw]
        if len(closes) < 200:
            return {"ok": False, "reason": "need_200_days_for_ma200"}
        ma_50 = sum(closes[-50:]) / 50
        ma_200 = sum(closes[-200:]) / 200
        # 20d realized vol
        rets = []
        for i in range(len(closes) - 20, len(closes)):
            if closes[i-1] > 0:
                rets.append(closes[i] / closes[i-1] - 1)
        if not rets:
            return {"ok": False, "reason": "no_returns"}
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        annual_vol = (var ** 0.5) * (252 ** 0.5)
        regime = _classify_regime(closes[-1], ma_50, ma_200, annual_vol)
        return {"ok": True, "regime_label": regime,
                "ma_50": round(ma_50, 2), "ma_200": round(ma_200, 2),
                "annual_vol_20d": round(annual_vol, 4),
                "spy_close": round(closes[-1], 2)}
    except Exception as e:
        logger.warning(f"classify_current_regime fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}


def get_current_match() -> dict:
    """Classify current regime, find matching historical playbook(s),
    return what worked then.

    Match logic: exact regime_label match preferred; if none, return
    closest by trend-component (bull/bear/sideways)."""
    try:
        cur = _classify_current_regime()
        if not cur.get("ok"):
            return {"ok": False, "phase": "classify",
                    "reason": cur.get("reason")}

        regime = cur["regime_label"]

        # Find exact matches first
        all_pb = get_all_playbooks()
        if not all_pb.get("ok"):
            return {"ok": False, "phase": "load_playbooks",
                    "reason": all_pb.get("reason")}
        playbooks = all_pb.get("playbooks", [])
        if not playbooks:
            return {"ok": False, "reason": "no_playbooks_built_yet",
                    "current_regime": regime,
                    "hint": "Call POST /api/regime-playbook/build first"}

        exact = [p for p in playbooks if p.get("regime_label") == regime]

        # Fallback: trend-only match (bull/bear/sideways)
        trend = regime.split("_")[0] if "_" in regime else regime
        trend_match = [p for p in playbooks
                       if p.get("regime_label", "").split("_")[0] == trend]

        matches = exact if exact else trend_match
        match_quality = "exact" if exact else ("trend_only" if trend_match else "none")

        # Aggregate sector signal: which sectors won most often across matches?
        from collections import Counter
        winner_count = Counter()
        loser_count = Counter()
        for p in matches:
            for s in (p.get("top_sectors") or []):
                winner_count[s.get("ticker")] += 1
            for s in (p.get("bottom_sectors") or []):
                loser_count[s.get("ticker")] += 1

        top_recommended = [{"ticker": t, "name": SECTOR_ETFS.get(t, t),
                            "winner_count": c}
                           for t, c in winner_count.most_common(3)]
        avoid_list = [{"ticker": t, "name": SECTOR_ETFS.get(t, t),
                       "loser_count": c}
                      for t, c in loser_count.most_common(3)]

        return {
            "ok": True,
            "current": cur,
            "match_quality": match_quality,
            "matched_events": [{"event": p["event_label"],
                                "period": [p["start_date"], p["end_date"]],
                                "description": p.get("description"),
                                "spy_return_pct": p.get("spy_return_pct"),
                                "top_sectors": p.get("top_sectors"),
                                "bottom_sectors": p.get("bottom_sectors"),
                                "top_factor": p.get("top_factor"),
                                "insight": p.get("insights")}
                               for p in matches],
            "recommendations": {
                "buy_sectors": top_recommended,
                "avoid_sectors": avoid_list,
                "rationale": (f"Current regime '{regime}' matches "
                              f"{len(matches)} historical event(s). "
                              f"Match quality: {match_quality}."),
            },
        }
    except Exception as e:
        logger.warning(f"get_current_match fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}
