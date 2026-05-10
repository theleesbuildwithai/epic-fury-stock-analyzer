"""
Live Regime Drift Alarm — ADVISORY ONLY.

Tracks how the market regime metrics (VIX, SPY 50d MA, 20d realized
vol) shift over time. When metrics swing fast (e.g. VIX 15->25 in
3 days, or SPY breaks 50d MA), records a drift event so downstream
monitors can react.

CRITICAL SAFETY:
  This module NEVER blocks trades, NEVER pauses cycles, NEVER
  changes position sizing. It is a READ-ONLY observation layer.
  All it does is record + expose drift events via API endpoints.
  If pick generation wants to consume the output, it must opt in
  explicitly — this module doesn't push anything.

PIPELINE:
  1. snapshot_regime() pulled on schedule, stored in regime_snapshots
  2. detect_drift() compares current snapshot to N days ago
  3. get_recent_alarms() exposes recent drift events
"""

import logging
import sqlite3
import os
import time
import threading
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Drift thresholds — generous defaults, never trigger spuriously
VIX_SPIKE_THRESHOLD_PCT = 30.0     # VIX up 30% in 3d
VIX_LEVEL_HIGH = 25.0
SPY_BREAK_MA50_PCT = 1.0           # SPY < 50d MA by >1%
VOL_SHIFT_THRESHOLD = 0.05         # 20d realized vol shift by >5pp


def _get_db():
    db_path = os.environ.get("DB_PATH", "predictions.db")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_drift_table():
    """Create snapshot + alarm tables. Idempotent."""
    try:
        conn = _get_db()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS regime_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    spy_close REAL,
                    spy_ma_50 REAL,
                    spy_ma_200 REAL,
                    vix_close REAL,
                    annual_vol_20d REAL,
                    regime_label TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_drift_snap_ts
                    ON regime_snapshots(ts DESC);

                CREATE TABLE IF NOT EXISTS regime_drift_alarms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    alarm_type TEXT NOT NULL,
                    severity TEXT,           -- low / medium / high
                    description TEXT,
                    snapshot_id INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_drift_alarm_ts
                    ON regime_drift_alarms(ts DESC);
            """)
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"init_drift_table soft-fail: {e}")


def snapshot_regime() -> dict:
    """Pull latest SPY + VIX, compute MAs + realized vol, store snapshot.
    NEVER raises. Soft-fails to {ok: False}."""
    try:
        import yfinance as yf
        end = datetime.utcnow()
        start = end - timedelta(days=300)
        spy = yf.download("SPY", start=start.strftime("%Y-%m-%d"),
                          end=end.strftime("%Y-%m-%d"),
                          progress=False, auto_adjust=True, threads=False)
        vix = yf.download("^VIX", start=(end - timedelta(days=10)).strftime("%Y-%m-%d"),
                          end=end.strftime("%Y-%m-%d"),
                          progress=False, auto_adjust=True, threads=False)
        if spy is None or spy.empty or len(spy) < 200:
            return {"ok": False, "reason": "insufficient_spy_data"}

        closes = spy["Close"].dropna().values.tolist()
        if isinstance(closes[0], list):
            closes = [float(c[0]) for c in closes]
        else:
            closes = [float(c) for c in closes]
        if len(closes) < 200:
            return {"ok": False, "reason": "need_200d_for_ma200"}

        spy_close = float(closes[-1])
        ma_50 = sum(closes[-50:]) / 50
        ma_200 = sum(closes[-200:]) / 200

        # 20d realized vol (annualized)
        rets = []
        for i in range(len(closes) - 20, len(closes)):
            if closes[i-1] > 0:
                rets.append(closes[i] / closes[i-1] - 1)
        if rets:
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / len(rets)
            ann_vol = (var ** 0.5) * (252 ** 0.5)
        else:
            ann_vol = 0

        # VIX
        vix_close = None
        try:
            if vix is not None and not vix.empty:
                vix_closes = vix["Close"].dropna().values.tolist()
                if vix_closes:
                    if isinstance(vix_closes[0], list):
                        vix_close = float(vix_closes[-1][0])
                    else:
                        vix_close = float(vix_closes[-1])
        except Exception:
            pass

        # Regime label
        try:
            from predictions.backtest_pro import _classify_regime
            regime = _classify_regime(spy_close, ma_50, ma_200, ann_vol)
        except Exception:
            regime = "unknown"

        # Persist
        snap_id = None
        try:
            conn = _get_db()
            try:
                cur = conn.execute(
                    """INSERT INTO regime_snapshots
                       (ts, spy_close, spy_ma_50, spy_ma_200,
                        vix_close, annual_vol_20d, regime_label)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (datetime.utcnow().isoformat(), spy_close, ma_50, ma_200,
                     vix_close, ann_vol, regime)
                )
                snap_id = cur.lastrowid
                conn.commit()
            finally:
                conn.close()
        except Exception as we:
            logger.debug(f"snapshot persist fail: {we}")

        return {
            "ok": True, "snapshot_id": snap_id,
            "spy_close": round(spy_close, 2),
            "spy_ma_50": round(ma_50, 2),
            "spy_ma_200": round(ma_200, 2),
            "vix_close": round(vix_close, 2) if vix_close else None,
            "annual_vol_20d": round(ann_vol, 4),
            "regime_label": regime,
        }
    except Exception as e:
        logger.warning(f"snapshot_regime fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}


def detect_drift(lookback_days: int = 3) -> dict:
    """Compare current snapshot to one ~lookback_days ago. Records
    alarms for significant shifts. NEVER raises."""
    try:
        # Take fresh snapshot
        cur_snap = snapshot_regime()
        if not cur_snap.get("ok"):
            return {"ok": False, "reason": "current_snapshot_failed"}

        cutoff = (datetime.utcnow() - timedelta(days=int(lookback_days))).isoformat()
        conn = _get_db()
        try:
            old = conn.execute(
                """SELECT * FROM regime_snapshots
                   WHERE ts <= ?
                   ORDER BY ts DESC LIMIT 1""",
                (cutoff,)
            ).fetchone()
        finally:
            conn.close()

        if not old:
            return {"ok": True, "alarms": [],
                    "reason": "no_baseline_yet (need >=lookback_days history)",
                    "current": cur_snap}

        alarms = []

        # VIX spike
        if old["vix_close"] and cur_snap.get("vix_close"):
            vix_change_pct = (cur_snap["vix_close"] / old["vix_close"] - 1) * 100
            if vix_change_pct >= VIX_SPIKE_THRESHOLD_PCT:
                alarms.append({
                    "type": "vix_spike",
                    "severity": "high" if vix_change_pct >= 50 else "medium",
                    "description": (f"VIX +{vix_change_pct:.1f}% in "
                                    f"{lookback_days}d "
                                    f"({old['vix_close']:.1f} -> "
                                    f"{cur_snap['vix_close']:.1f})"),
                })

        # VIX above level
        if cur_snap.get("vix_close") and cur_snap["vix_close"] >= VIX_LEVEL_HIGH:
            alarms.append({
                "type": "vix_elevated",
                "severity": "medium",
                "description": (f"VIX at {cur_snap['vix_close']:.1f} "
                                f"(>={VIX_LEVEL_HIGH} = stress regime)"),
            })

        # SPY broke 50d MA
        if (cur_snap["spy_close"] and cur_snap["spy_ma_50"]
                and cur_snap["spy_close"] < cur_snap["spy_ma_50"]
                and old["spy_close"] >= old["spy_ma_50"]):
            break_pct = (cur_snap["spy_close"] / cur_snap["spy_ma_50"] - 1) * 100
            if abs(break_pct) >= SPY_BREAK_MA50_PCT:
                alarms.append({
                    "type": "spy_below_ma50",
                    "severity": "medium",
                    "description": (f"SPY broke 50d MA: {cur_snap['spy_close']:.2f} "
                                    f"vs MA {cur_snap['spy_ma_50']:.2f} "
                                    f"({break_pct:+.2f}%)"),
                })

        # Realized vol jump
        if old["annual_vol_20d"]:
            vol_shift = cur_snap["annual_vol_20d"] - old["annual_vol_20d"]
            if vol_shift >= VOL_SHIFT_THRESHOLD:
                alarms.append({
                    "type": "vol_regime_shift",
                    "severity": "medium",
                    "description": (f"20d realized vol {old['annual_vol_20d']:.3f} "
                                    f"-> {cur_snap['annual_vol_20d']:.3f} "
                                    f"(+{vol_shift:.3f})"),
                })

        # Regime change
        if old["regime_label"] and cur_snap["regime_label"] != old["regime_label"]:
            alarms.append({
                "type": "regime_label_change",
                "severity": "low",
                "description": (f"Regime: {old['regime_label']} -> "
                                f"{cur_snap['regime_label']}"),
            })

        # Persist alarms
        if alarms:
            try:
                conn = _get_db()
                try:
                    for a in alarms:
                        conn.execute(
                            """INSERT INTO regime_drift_alarms
                               (ts, alarm_type, severity, description, snapshot_id)
                               VALUES (?, ?, ?, ?, ?)""",
                            (datetime.utcnow().isoformat(), a["type"],
                             a["severity"], a["description"],
                             cur_snap.get("snapshot_id"))
                        )
                    conn.commit()
                finally:
                    conn.close()
            except Exception:
                pass

        return {
            "ok": True,
            "current": cur_snap,
            "baseline": dict(old),
            "lookback_days": lookback_days,
            "alarm_count": len(alarms),
            "alarms": alarms,
        }
    except Exception as e:
        logger.warning(f"detect_drift fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}


def get_recent_alarms(hours: int = 48) -> dict:
    """Return alarms from the last N hours. Never raises."""
    try:
        cutoff = (datetime.utcnow() - timedelta(hours=int(hours))).isoformat()
        conn = _get_db()
        try:
            rows = conn.execute(
                """SELECT * FROM regime_drift_alarms
                   WHERE ts >= ?
                   ORDER BY ts DESC LIMIT 200""",
                (cutoff,)
            ).fetchall()
        finally:
            conn.close()
        return {"ok": True, "hours_window": hours,
                "count": len(rows),
                "alarms": [dict(r) for r in rows]}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200], "alarms": []}
