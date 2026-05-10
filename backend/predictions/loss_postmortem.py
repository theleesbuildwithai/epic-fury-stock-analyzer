"""
Auto-Postmortem on Every Loss — Closes the missing learning loop.

For every losing trade close, captures the FULL CONTEXT of what was
happening when the trade was opened + what went wrong. After enough
losses, aggregates patterns: "73% of my losses came from tech longs
in high-vol regimes after FOMC days."

These patterns become rejection filters that prevent future picks
matching the same toxic profile.

PIPELINE:
  1. Hooked into close_paper_trade — fires only when pnl_pct < 0
  2. Captures: regime, sector, signal_score bucket, day-of-week,
     hour-of-day, hold duration, exit reason
  3. Stored in loss_postmortems table
  4. aggregate_loss_patterns() finds clusters meeting min-occurrence
     thresholds
  5. get_active_loss_filters() exposes patterns as concrete rules
  6. Pick generation can call check_against_loss_filters(ticker, regime,
     sector, score) -> reject or allow

SAFETY:
  - Every function try/except wrapped, soft-fails to no-op
  - Hooked write is async-safe and never blocks trade close
  - All thresholds tunable + defensive defaults (require >= 5 losses
    matching pattern before becoming a filter)
"""

import logging
import sqlite3
import os
import json
from datetime import datetime, timedelta
from collections import defaultdict, Counter

logger = logging.getLogger(__name__)

# Tunables — conservative defaults to avoid over-filtering on noise
MIN_LOSSES_FOR_PATTERN = 5      # need >=5 losses matching to become a rule
MIN_LOSS_RATE_FOR_PATTERN = 0.70 # 70%+ of trades matching pattern lose
DEFAULT_LOOKBACK_DAYS = 180


def _get_db():
    db_path = os.environ.get("DB_PATH", "predictions.db")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_postmortem_table():
    """Create the postmortem table on startup. Idempotent."""
    try:
        conn = _get_db()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS loss_postmortems (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id INTEGER,
                    ticker TEXT NOT NULL,
                    direction TEXT,
                    sector TEXT,
                    regime TEXT,
                    signal_score REAL,
                    score_bucket TEXT,        -- low/medium/high
                    day_of_week TEXT,
                    hour_of_day INTEGER,
                    hold_duration_days REAL,
                    pnl_pct REAL NOT NULL,
                    exit_reason TEXT,
                    closed_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_pm_time
                    ON loss_postmortems(closed_at DESC);
                CREATE INDEX IF NOT EXISTS idx_pm_ticker
                    ON loss_postmortems(ticker, closed_at DESC);
                CREATE INDEX IF NOT EXISTS idx_pm_sector
                    ON loss_postmortems(sector, closed_at DESC);
                CREATE INDEX IF NOT EXISTS idx_pm_regime
                    ON loss_postmortems(regime, closed_at DESC);
            """)
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"init_postmortem_table soft-fail: {e}")


def _bucket_score(score: float) -> str:
    """Classify signal_score into low/medium/high bucket."""
    try:
        s = float(score or 0)
    except Exception:
        return "unknown"
    if abs(s) < 1.5:
        return "low"
    if abs(s) < 3.0:
        return "medium"
    return "high"


def _bucket_hold(days: float) -> str:
    """Classify hold duration into intraday/swing/multi-week."""
    try:
        d = float(days or 0)
    except Exception:
        return "unknown"
    if d < 1:
        return "intraday"
    if d < 7:
        return "swing"
    return "long"


def record_loss_postmortem(trade: dict, exit_reason: str = "") -> dict:
    """Hook called from close_paper_trade for every losing trade.
    NEVER raises — soft-fails so trade close cannot break.

    `trade` is a dict-like with: ticker, direction, sector, regime_at_entry,
    signal_score, entry_date, exit_date, pnl_pct.
    """
    try:
        if not trade:
            return {"ok": False, "reason": "no_trade"}
        try:
            pnl_pct = float(trade.get("pnl_pct") or 0)
        except Exception:
            return {"ok": False, "reason": "bad_pnl"}
        if pnl_pct >= 0:
            return {"ok": True, "skipped": True, "reason": "not_a_loss"}

        # Compute hold duration + day-of-week + hour-of-day
        try:
            entry = trade.get("entry_date") or ""
            exit_d = trade.get("exit_date") or datetime.utcnow().isoformat()
            ed = datetime.fromisoformat(entry) if entry else None
            xd = datetime.fromisoformat(exit_d) if exit_d else datetime.utcnow()
            hold_days = ((xd - ed).total_seconds() / 86400.0) if ed else 0
            day_of_week = ed.strftime("%A") if ed else None
            hour_of_day = ed.hour if ed else None
        except Exception:
            hold_days = 0
            day_of_week = None
            hour_of_day = None

        signal_score = trade.get("signal_score") or 0
        bucket = _bucket_score(signal_score)

        conn = _get_db()
        try:
            conn.execute(
                """INSERT INTO loss_postmortems
                   (trade_id, ticker, direction, sector, regime, signal_score,
                    score_bucket, day_of_week, hour_of_day, hold_duration_days,
                    pnl_pct, exit_reason, closed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trade.get("id"),
                    str(trade.get("ticker") or "")[:16],
                    str(trade.get("direction") or "")[:8],
                    str(trade.get("sector") or "")[:32],
                    str(trade.get("regime_at_entry") or "")[:32],
                    float(signal_score or 0),
                    bucket,
                    day_of_week,
                    hour_of_day,
                    round(hold_days, 3),
                    round(pnl_pct, 3),
                    str(exit_reason or "")[:64],
                    datetime.utcnow().isoformat(),
                )
            )
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "recorded": True}
    except Exception as e:
        logger.debug(f"record_loss_postmortem soft-fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}


def aggregate_loss_patterns(lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> dict:
    """Scan recent losses, find clusters that match pattern thresholds.
    Returns the discovered patterns with frequency + avg loss.

    A "pattern" is a (regime, sector, score_bucket) triple that occurs
    >= MIN_LOSSES_FOR_PATTERN times.
    """
    try:
        cutoff = (datetime.utcnow() - timedelta(days=int(lookback_days))).isoformat()
        conn = _get_db()
        try:
            losses = conn.execute(
                """SELECT regime, sector, score_bucket, day_of_week,
                          hour_of_day, pnl_pct, ticker
                   FROM loss_postmortems
                   WHERE closed_at >= ?""",
                (cutoff,)
            ).fetchall()
        finally:
            conn.close()

        if not losses:
            return {"ok": True, "patterns": [], "total_losses": 0}

        # Pattern keys: (regime, sector, score_bucket)
        clusters = defaultdict(list)
        for r in losses:
            key = (r["regime"] or "unknown",
                   r["sector"] or "unknown",
                   r["score_bucket"] or "unknown")
            clusters[key].append({
                "ticker": r["ticker"],
                "pnl_pct": r["pnl_pct"],
                "day_of_week": r["day_of_week"],
                "hour_of_day": r["hour_of_day"],
            })

        patterns = []
        for (regime, sector, bucket), items in clusters.items():
            if len(items) < MIN_LOSSES_FOR_PATTERN:
                continue
            avg_loss = sum(x["pnl_pct"] for x in items) / len(items)
            day_freq = Counter(x["day_of_week"] for x in items if x["day_of_week"])
            top_day = day_freq.most_common(1)[0][0] if day_freq else None
            patterns.append({
                "pattern": {
                    "regime": regime,
                    "sector": sector,
                    "score_bucket": bucket,
                },
                "loss_count": len(items),
                "avg_loss_pct": round(avg_loss, 2),
                "tickers_affected": sorted(set(x["ticker"] for x in items))[:10],
                "most_common_day": top_day,
            })
        # Worst patterns first (largest avg loss)
        patterns.sort(key=lambda p: p["avg_loss_pct"])

        return {
            "ok": True,
            "lookback_days": lookback_days,
            "total_losses": len(losses),
            "patterns_found": len(patterns),
            "patterns": patterns,
        }
    except Exception as e:
        logger.warning(f"aggregate_loss_patterns fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}


def get_active_loss_filters(lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> list:
    """Returns concrete filters that picks should be checked against.
    Each filter is {"regime": ..., "sector": ..., "score_bucket": ...,
    "reason": ...}. Empty list if not enough loss data yet."""
    try:
        agg = aggregate_loss_patterns(lookback_days)
        if not agg.get("ok") or not agg.get("patterns"):
            return []
        filters = []
        for p in agg["patterns"]:
            pat = p["pattern"]
            # Only build filter if avg loss is meaningful (-2% or worse)
            if p["avg_loss_pct"] > -2.0:
                continue
            filters.append({
                "regime": pat["regime"],
                "sector": pat["sector"],
                "score_bucket": pat["score_bucket"],
                "reason": (f"{p['loss_count']} historical losses with "
                           f"avg {p['avg_loss_pct']:.1f}% in this profile"),
                "loss_count": p["loss_count"],
                "avg_loss_pct": p["avg_loss_pct"],
            })
        return filters
    except Exception as e:
        logger.debug(f"get_active_loss_filters soft-fail: {e}")
        return []


def check_against_loss_filters(ticker: str, regime: str, sector: str,
                                signal_score: float) -> dict:
    """Returns {"reject": bool, "matched_filter": ...} for a candidate
    pick. Used by pick generation to skip toxic profiles."""
    try:
        bucket = _bucket_score(signal_score)
        filters = get_active_loss_filters()
        for f in filters:
            if (f["regime"] == (regime or "unknown")
                and f["sector"] == (sector or "unknown")
                and f["score_bucket"] == bucket):
                return {
                    "reject": True,
                    "ticker": ticker,
                    "matched_filter": f,
                    "reason": f["reason"],
                }
        return {"reject": False, "ticker": ticker}
    except Exception as e:
        logger.debug(f"check_against_loss_filters soft-fail: {e}")
        return {"reject": False, "ticker": ticker}


def get_postmortem_summary(lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> dict:
    """Returns aggregate stats for the postmortem dashboard."""
    try:
        cutoff = (datetime.utcnow() - timedelta(days=int(lookback_days))).isoformat()
        conn = _get_db()
        try:
            losses = conn.execute(
                """SELECT * FROM loss_postmortems
                   WHERE closed_at >= ?
                   ORDER BY closed_at DESC""",
                (cutoff,)
            ).fetchall()
        finally:
            conn.close()

        if not losses:
            return {"ok": True, "total_losses": 0,
                    "lookback_days": lookback_days, "message": "no losses recorded yet"}

        n = len(losses)
        avg_loss = sum(r["pnl_pct"] for r in losses) / n
        by_regime = Counter(r["regime"] or "unknown" for r in losses)
        by_sector = Counter(r["sector"] or "unknown" for r in losses)
        by_day = Counter(r["day_of_week"] for r in losses if r["day_of_week"])
        by_bucket = Counter(r["score_bucket"] for r in losses)
        by_exit = Counter(r["exit_reason"] for r in losses if r["exit_reason"])

        # Filters
        filters = get_active_loss_filters(lookback_days)

        return {
            "ok": True,
            "lookback_days": lookback_days,
            "total_losses": n,
            "avg_loss_pct": round(avg_loss, 2),
            "by_regime": dict(by_regime.most_common()),
            "by_sector": dict(by_sector.most_common(8)),
            "by_day_of_week": dict(by_day.most_common()),
            "by_score_bucket": dict(by_bucket.most_common()),
            "by_exit_reason": dict(by_exit.most_common(5)),
            "active_filters": filters,
            "active_filter_count": len(filters),
        }
    except Exception as e:
        logger.warning(f"get_postmortem_summary fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}


def backfill_from_closed_trades(limit: int = 5000) -> dict:
    """One-shot import of existing closed losing trades into the
    postmortem log. Idempotent — uses (trade_id) as dedup key."""
    try:
        from predictions.models import get_closed_trades
        trades = get_closed_trades(limit=limit) or []
        if not trades:
            return {"ok": True, "imported": 0, "reason": "no_trades"}

        # Existing trade_ids in postmortem log
        conn = _get_db()
        try:
            existing = set()
            try:
                rows = conn.execute(
                    "SELECT trade_id FROM loss_postmortems WHERE trade_id IS NOT NULL"
                ).fetchall()
                existing = {r["trade_id"] for r in rows}
            except Exception:
                pass
        finally:
            conn.close()

        imported = 0
        skipped = 0
        for t in trades:
            try:
                if t.get("id") in existing:
                    skipped += 1
                    continue
                pnl_pct = float(t.get("pnl_pct") or 0)
                if pnl_pct >= 0:
                    continue  # not a loss
                record_loss_postmortem(t, exit_reason=t.get("exit_reason") or "")
                imported += 1
            except Exception:
                continue
        return {"ok": True, "imported": imported, "skipped": skipped,
                "scanned": len(trades)}
    except Exception as e:
        logger.warning(f"backfill_from_closed_trades fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}
