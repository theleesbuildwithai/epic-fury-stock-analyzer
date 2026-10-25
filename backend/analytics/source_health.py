"""
Source Health Registry — passive, in-process telemetry for every data source.

Each fetch layer calls record(source, ok) as a side note of work it was doing
anyway. ZERO extra network: the registry never probes anything; it only
remembers what real requests observed. /api/system-health serves a snapshot.

Status model per source:
  HEALTHY  — last attempt succeeded
  DEGRADED — 1-2 consecutive failures (fallbacks are covering)
  DOWN     — 3+ consecutive failures

Thread-safe, bounded, and fail-safe: recording must never break a request.
"""

import threading
import time

_lock = threading.Lock()
_registry = {}
_STARTED_AT = time.time()


def record(source, ok, note=None):
    """Record one observed attempt for a source. Never raises."""
    try:
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        with _lock:
            s = _registry.setdefault(str(source), {
                "ok_count": 0, "fail_count": 0, "consecutive_failures": 0,
                "last_ok": None, "last_fail": None, "last_note": None,
            })
            if ok:
                s["ok_count"] += 1
                s["consecutive_failures"] = 0
                s["last_ok"] = now
            else:
                s["fail_count"] += 1
                s["consecutive_failures"] += 1
                s["last_fail"] = now
            if note is not None:
                s["last_note"] = str(note)[:200]
    except Exception:
        pass


def _status(s):
    cf = s.get("consecutive_failures", 0)
    if cf >= 3:
        return "DOWN"
    if cf >= 1:
        return "DEGRADED"
    if s.get("ok_count", 0) > 0:
        return "HEALTHY"
    return "UNKNOWN"


def snapshot():
    """Point-in-time view of every observed source. Never raises."""
    try:
        with _lock:
            out = {}
            for name, s in _registry.items():
                d = dict(s)
                d["status"] = _status(s)
                out[name] = d
            return {
                "sources": out,
                "observed_since_utc": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.gmtime(_STARTED_AT)),
                "uptime_seconds": round(time.time() - _STARTED_AT, 1),
            }
    except Exception:
        return {"sources": {}, "observed_since_utc": None, "uptime_seconds": None}
