"""
Earnings Drift Learner — captures the Post-Earnings Announcement
Drift (PEAD) effect, the most-documented anomaly in academic finance.

CORE INSIGHT:
  Stocks that beat earnings significantly drift up for ~60 days after.
  Stocks that miss drift down. The market under-reacts to the surprise
  in real-time and absorbs it gradually.

PIPELINE:
  1. fetch_earnings_history(ticker) — pulls past earnings dates +
     surprise from yfinance Ticker.earnings_dates
  2. compute_drift_for_event(ticker, earnings_date, surprise_pct):
     pull price history, measure +1d / +5d / +30d / +60d drift
  3. build_ticker_drift_table(ticker) — runs above for last 8 quarters
  4. predict_drift(ticker, surprise_pct) — given a new surprise,
     forecast expected drift based on historical pattern

OUTPUT FOR PICK ENHANCEMENT:
  When a ticker recently reported earnings (last 60 days), and we have
  history on how it drifts, return a confidence boost/penalty:
    - Beat by >5% historically -> boost long
    - Missed by >5% historically -> avoid long, consider short

SAFETY:
  - Every yfinance call try/except wrapped, returns {} on fail
  - Cache results in earnings_drift_cache table (24h TTL)
  - Soft-fails to neutral signal — never blocks pick generation
"""

import logging
import sqlite3
import os
import json
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Drift measurement windows (days after earnings)
DRIFT_WINDOWS = [1, 5, 20, 60]
MIN_QUARTERS_FOR_PATTERN = 4    # Need >=4 quarters for meaningful pattern
CACHE_TTL_HOURS = 24


def _get_db():
    db_path = os.environ.get("DB_PATH", "predictions.db")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_drift_table():
    """Create the cache table on startup. Idempotent."""
    try:
        conn = _get_db()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS earnings_drift_cache (
                    ticker TEXT PRIMARY KEY,
                    drift_data TEXT NOT NULL,    -- JSON
                    n_events INTEGER,
                    avg_surprise_pct REAL,
                    avg_drift_5d REAL,
                    avg_drift_20d REAL,
                    avg_drift_60d REAL,
                    last_event_date TEXT,
                    cached_at TEXT NOT NULL
                );
            """)
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"init_drift_table soft-fail: {e}")


def _fetch_earnings_dates(ticker: str, lookback_quarters: int = 8) -> list:
    """Pull historical earnings dates + surprise from yfinance.
    Returns list of {"date": "YYYY-MM-DD", "surprise_pct": float}.
    Empty list on fail. Never raises."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        # earnings_dates returns DataFrame with index=date, cols include
        # "EPS Estimate", "Reported EPS", "Surprise(%)"
        df = t.earnings_dates
        if df is None or df.empty:
            return []
        events = []
        # Sort newest first, take recent quarters
        for idx, row in df.iterrows():
            try:
                # Index is timezone-aware datetime
                dt = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx
                date_str = dt.strftime("%Y-%m-%d")
                surprise_pct = row.get("Surprise(%)")
                if surprise_pct is None or (isinstance(surprise_pct, float)
                                             and (surprise_pct != surprise_pct)):
                    continue
                # Only past events (we need actual price reaction data)
                if dt > datetime.now(dt.tzinfo) if dt.tzinfo else dt > datetime.now():
                    continue
                events.append({
                    "date": date_str,
                    "surprise_pct": round(float(surprise_pct), 2),
                })
            except Exception:
                continue
            if len(events) >= lookback_quarters:
                break
        return events
    except Exception as e:
        logger.debug(f"_fetch_earnings_dates {ticker} soft-fail: {e}")
        return []


def _measure_drift(ticker: str, event_date: str) -> dict:
    """For a given earnings date, pull subsequent prices and compute
    +1d / +5d / +20d / +60d returns. Returns {} on fail."""
    try:
        import yfinance as yf
        ev_dt = datetime.strptime(event_date, "%Y-%m-%d")
        # Need 1 day before (anchor) + 65 days after
        start = (ev_dt - timedelta(days=2)).strftime("%Y-%m-%d")
        end = (ev_dt + timedelta(days=70)).strftime("%Y-%m-%d")
        df = yf.download(ticker, start=start, end=end,
                         progress=False, auto_adjust=True, threads=False)
        if df is None or df.empty or len(df) < 5:
            return {}
        closes = df["Close"].dropna().values.tolist()
        if isinstance(closes[0], list):
            closes = [float(c[0]) for c in closes]
        else:
            closes = [float(c) for c in closes]
        anchor = float(closes[0])
        if anchor <= 0:
            return {}
        out = {}
        for window in DRIFT_WINDOWS:
            if len(closes) > window:
                out[f"drift_{window}d_pct"] = round(
                    (float(closes[window]) / anchor - 1) * 100, 2)
        return out
    except Exception as e:
        logger.debug(f"_measure_drift {ticker} {event_date} soft-fail: {e}")
    # Fallback: multi-source historical adapter (~3 months to cover drift windows)
    try:
        from analytics.multi_source_adapter import get_historical_any_source
        df2 = get_historical_any_source(ticker, "3mo")
        if df2 is not None and len(df2) >= 5:
            closes2 = df2["Close"].dropna().values.tolist()
            closes2 = [float(c) for c in closes2]
            anchor = closes2[0] if closes2 else 0
            if anchor > 0:
                out = {}
                for window in DRIFT_WINDOWS:
                    if len(closes2) > window:
                        out[f"drift_{window}d_pct"] = round(
                            (float(closes2[window]) / anchor - 1) * 100, 2)
                if out:
                    return out
    except Exception:
        pass
    return {}


def build_ticker_drift_history(ticker: str,
                                lookback_quarters: int = 8,
                                use_cache: bool = True) -> dict:
    """Full pipeline for one ticker: pull events, measure drift for each,
    aggregate, cache result. Returns {ok, ticker, n_events, events,
    averages, cached_at}. Never raises."""
    try:
        ticker = ticker.upper()

        # Cache check
        if use_cache:
            try:
                conn = _get_db()
                try:
                    row = conn.execute(
                        "SELECT * FROM earnings_drift_cache WHERE ticker = ?",
                        (ticker,)
                    ).fetchone()
                finally:
                    conn.close()
                if row:
                    cached_at = datetime.fromisoformat(row["cached_at"])
                    age_hr = (datetime.utcnow() - cached_at).total_seconds() / 3600
                    if age_hr < CACHE_TTL_HOURS:
                        try:
                            data = json.loads(row["drift_data"])
                            data["_cached"] = True
                            data["_cache_age_hours"] = round(age_hr, 1)
                            return data
                        except Exception:
                            pass
            except Exception:
                pass  # cache miss is fine

        events = _fetch_earnings_dates(ticker, lookback_quarters)
        if not events:
            return {"ok": False, "ticker": ticker,
                    "reason": "no_earnings_history_available"}

        analyzed = []
        for ev in events:
            drift = _measure_drift(ticker, ev["date"])
            if drift:
                analyzed.append({**ev, **drift})

        if not analyzed:
            return {"ok": False, "ticker": ticker,
                    "reason": "no_drift_measurements_succeeded",
                    "events_attempted": len(events)}

        # Aggregate
        n = len(analyzed)
        avg_surprise = sum(e["surprise_pct"] for e in analyzed) / n
        avgs = {}
        for w in DRIFT_WINDOWS:
            key = f"drift_{w}d_pct"
            vals = [e[key] for e in analyzed if key in e]
            avgs[f"avg_drift_{w}d_pct"] = round(sum(vals) / len(vals), 2) if vals else None

        result = {
            "ok": True,
            "ticker": ticker,
            "n_events": n,
            "lookback_quarters": lookback_quarters,
            "avg_surprise_pct": round(avg_surprise, 2),
            "averages": avgs,
            "events": analyzed,
            "computed_at": datetime.utcnow().isoformat(),
            "_cached": False,
        }

        # Persist to cache
        try:
            conn = _get_db()
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO earnings_drift_cache
                       (ticker, drift_data, n_events, avg_surprise_pct,
                        avg_drift_5d, avg_drift_20d, avg_drift_60d,
                        last_event_date, cached_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ticker, json.dumps(result), n, avg_surprise,
                     avgs.get("avg_drift_5d_pct"),
                     avgs.get("avg_drift_20d_pct"),
                     avgs.get("avg_drift_60d_pct"),
                     analyzed[0]["date"] if analyzed else None,
                     datetime.utcnow().isoformat())
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as ce:
            logger.debug(f"cache write fail: {ce}")

        return result
    except Exception as e:
        logger.warning(f"build_ticker_drift_history {ticker} fail: {e}")
        return {"ok": False, "ticker": ticker, "reason": str(e)[:200]}


def predict_drift(ticker: str, current_surprise_pct: float = None,
                   cache_only: bool = False) -> dict:
    """Given a new earnings surprise (or auto-detect from latest event),
    forecast expected drift based on this ticker's historical pattern.

    cache_only=True: read DB cache only, return neutral on miss.
    USE THIS from any hot path (pick generation) to avoid blocking on
    yfinance during a trading cycle.
    """
    try:
        if cache_only:
            # Cache-only path: just check the DB, never trigger fresh fetch
            try:
                conn = _get_db()
                try:
                    row = conn.execute(
                        "SELECT * FROM earnings_drift_cache WHERE ticker = ?",
                        (ticker.upper(),)
                    ).fetchone()
                finally:
                    conn.close()
                if not row or row["n_events"] is None or row["n_events"] < MIN_QUARTERS_FOR_PATTERN:
                    return {"ok": True, "ticker": ticker.upper(),
                            "n_events": 0,
                            "confidence_signal": 1.0,
                            "direction": "neutral",
                            "reason": "no_cache",
                            "cache_only": True}
                avg_60d = float(row["avg_drift_60d"] or 0)
                if avg_60d > 3:
                    direction = "drifts_up"
                    confidence_signal = 1.10
                elif avg_60d < -3:
                    direction = "drifts_down"
                    confidence_signal = 0.90
                else:
                    direction = "neutral"
                    confidence_signal = 1.0
                return {"ok": True, "ticker": ticker.upper(),
                        "n_events": int(row["n_events"]),
                        "confidence_signal": confidence_signal,
                        "direction": direction,
                        "cache_only": True,
                        "avg_drift_60d": avg_60d}
            except Exception as ce:
                logger.debug(f"predict_drift cache_only soft-fail {ticker}: {ce}")
                return {"ok": True, "ticker": ticker, "confidence_signal": 1.0,
                        "direction": "neutral", "reason": "cache_error"}

        history = build_ticker_drift_history(ticker)
        if not history.get("ok"):
            return {"ok": False, "ticker": ticker,
                    "reason": history.get("reason"),
                    "confidence_signal": 1.0,  # neutral
                    "direction": "neutral"}
        n = history["n_events"]
        if n < MIN_QUARTERS_FOR_PATTERN:
            return {"ok": True, "ticker": ticker,
                    "n_events": n,
                    "confidence_signal": 1.0,
                    "direction": "neutral",
                    "reason": f"need >={MIN_QUARTERS_FOR_PATTERN} events, "
                              f"have {n}"}

        avg_60d = history["averages"].get("avg_drift_60d_pct") or 0
        avg_20d = history["averages"].get("avg_drift_20d_pct") or 0
        avg_5d = history["averages"].get("avg_drift_5d_pct") or 0

        # Direction: positive avg drift -> beat-pattern; negative -> miss-pattern
        if avg_60d > 3:
            direction = "drifts_up"
            confidence_signal = 1.10  # +10% boost on long picks
        elif avg_60d < -3:
            direction = "drifts_down"
            confidence_signal = 0.90  # -10% penalty on long picks
        else:
            direction = "neutral"
            confidence_signal = 1.0

        return {
            "ok": True,
            "ticker": ticker,
            "n_events": n,
            "historical_avg_drifts": {
                "5d": avg_5d, "20d": avg_20d, "60d": avg_60d,
            },
            "historical_avg_surprise_pct": history["avg_surprise_pct"],
            "direction": direction,
            "confidence_signal": confidence_signal,
            "interpretation": (
                f"Over {n} past earnings, {ticker} drifted "
                f"{avg_60d:+.1f}% in 60 days. {direction.upper()}."
            ),
        }
    except Exception as e:
        logger.debug(f"predict_drift {ticker} soft-fail: {e}")
        return {"ok": False, "ticker": ticker,
                "confidence_signal": 1.0, "direction": "neutral",
                "reason": str(e)[:200]}


def get_drift_summary(min_events: int = 4) -> dict:
    """Returns aggregated drift cache for the dashboard:
    list of tickers with strongest positive/negative drift patterns."""
    try:
        conn = _get_db()
        try:
            rows = conn.execute(
                """SELECT ticker, n_events, avg_surprise_pct,
                          avg_drift_5d, avg_drift_20d, avg_drift_60d
                   FROM earnings_drift_cache
                   WHERE n_events >= ?""",
                (int(min_events),)
            ).fetchall()
        finally:
            conn.close()

        results = [dict(r) for r in rows]
        # Sort by absolute 60d drift descending
        results.sort(key=lambda r: abs(r.get("avg_drift_60d") or 0), reverse=True)
        return {
            "ok": True,
            "min_events_filter": min_events,
            "count": len(results),
            "tickers": results[:50],
        }
    except Exception as e:
        logger.warning(f"get_drift_summary fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}
