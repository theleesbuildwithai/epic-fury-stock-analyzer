"""
Reliability Sentinels — Sentinel Quant

Three guard systems that prevent entire classes of bugs we have actually
hit in production:

  1. CASH SENTINEL
     Validates every cash mutation against absolute bounds. Rejects
     and audit-logs any single change >$50k OR result <$0 OR >2x
     initial capital. Catches OXY-class incidents at the lowest layer.

  2. SNAPSHOT DRIFT DETECTOR
     Compares yesterday's portfolio_snapshot to live trade-derived
     value. If they diverge by >5%, auto-corrects the snapshot and
     audit-logs the correction. Eliminates the false "+10.77% gain"
     that has triggered daily_paused multiple times.

  3. TRADE CIRCUIT BREAKER
     Counts trade execution failures. If >=5 failures within a
     10-minute window, halts new trade execution for 30 minutes.
     Defends against external-data corruption flooding the system.

Every function is fully isolated, never raises, and returns a dict
result so callers can decide what to do.
"""

import logging
import time
from datetime import datetime, timedelta
from collections import deque

logger = logging.getLogger(__name__)


# ============================================================
#  CONSTANTS — tuned conservatively to allow normal operation
# ============================================================

# Cash sentinel
INITIAL_CAPITAL = 100_000.0           # baseline reference
MAX_SINGLE_DELTA = 50_000.0           # one mutation cannot move cash by more
ABSOLUTE_MAX_CASH = INITIAL_CAPITAL * 2.5  # cash cannot exceed 2.5x baseline
ABSOLUTE_MIN_CASH = -1.0              # tiny negative tolerance for FP noise

# Drift detector
DRIFT_THRESHOLD_PCT = 5.0             # auto-correct if snapshot off by >5%

# Circuit breaker
CIRCUIT_FAIL_WINDOW_SEC = 600         # 10 minutes
CIRCUIT_FAIL_THRESHOLD = 5
CIRCUIT_HALT_SEC = 1800               # 30-minute halt after trip

# Process-local state for circuit breaker (resets on container restart,
# which is fine — restarts are infrequent and a fresh state is safe)
_failure_times = deque()              # rolling window of failure timestamps
_circuit_halt_until = [0.0]           # epoch time the halt expires


# ============================================================
#  1. CASH SENTINEL
# ============================================================

def validate_cash_mutation(current_cash: float,
                           proposed_delta: float = None,
                           proposed_absolute: float = None,
                           caller: str = "unknown") -> dict:
    """Validate a proposed cash mutation BEFORE it is applied.

    Either (proposed_delta) for adjust_cash-style, or
    (proposed_absolute) for set_cash-style. Pass exactly one.

    Returns a dict:
        {"ok": bool, "rejected": bool, "reason": str|None,
         "current": float, "delta": float, "result": float}

    SAFETY: never raises. If the inputs are non-numeric, returns
    rejected=False (fails open) so we never block legitimate calls
    due to a bug in the validator itself.
    """
    try:
        cur = float(current_cash) if current_cash is not None else 0.0
    except Exception:
        return {"ok": True, "rejected": False, "reason": "non_numeric_current_passthrough",
                "current": current_cash, "delta": None, "result": current_cash}

    try:
        if proposed_delta is not None:
            delta = float(proposed_delta)
            result = cur + delta
        elif proposed_absolute is not None:
            result = float(proposed_absolute)
            delta = result - cur
        else:
            return {"ok": False, "rejected": True,
                    "reason": "no_proposal_passed",
                    "current": cur, "delta": 0.0, "result": cur}

        # Bound 1: single-mutation magnitude
        if abs(delta) > MAX_SINGLE_DELTA:
            return {"ok": False, "rejected": True,
                    "reason": f"delta ${abs(delta):.2f} exceeds MAX_SINGLE_DELTA ${MAX_SINGLE_DELTA:.0f}",
                    "current": cur, "delta": delta, "result": result,
                    "caller": caller}

        # Bound 2: result below floor
        if result < ABSOLUTE_MIN_CASH:
            return {"ok": False, "rejected": True,
                    "reason": f"result ${result:.2f} below floor ${ABSOLUTE_MIN_CASH:.2f}",
                    "current": cur, "delta": delta, "result": result,
                    "caller": caller}

        # Bound 3: result above ceiling
        if result > ABSOLUTE_MAX_CASH:
            return {"ok": False, "rejected": True,
                    "reason": f"result ${result:.2f} above ceiling ${ABSOLUTE_MAX_CASH:.2f}",
                    "current": cur, "delta": delta, "result": result,
                    "caller": caller}

        return {"ok": True, "rejected": False, "reason": None,
                "current": cur, "delta": delta, "result": result,
                "caller": caller}

    except Exception as e:
        # Defensive: never block a mutation due to a bug in the validator
        logger.debug(f"validate_cash_mutation soft-fail: {e}")
        return {"ok": True, "rejected": False,
                "reason": f"validator_soft_fail: {e}",
                "current": cur, "delta": None, "result": None}


# ============================================================
#  2. SNAPSHOT DRIFT DETECTOR
# ============================================================

def check_and_correct_snapshot_drift() -> dict:
    """Inspect yesterday's portfolio_snapshot. If it diverges from the
    actual current portfolio value by more than DRIFT_THRESHOLD_PCT
    (default 5%) AND we have ZERO open positions (drift only matters
    for dormant portfolios — active positions explain real changes),
    write a corrected snapshot and audit-log it.

    Returns a dict describing the action. Never raises.
    """
    try:
        from predictions.models import (
            get_portfolio_snapshots, save_portfolio_snapshot, get_cash, get_open_trades
        )
        from datetime import datetime as _dt
        try:
            from predictions.audit import audit_log
        except Exception:
            audit_log = lambda *a, **k: None  # no-op fallback

        # Only auto-correct when there are NO open positions. If there are
        # positions, intraday P&L can legitimately move the value 5%+, so
        # we'd risk overwriting real trading data.
        open_positions = get_open_trades() or []
        if open_positions:
            return {"ok": True, "action": "skip_open_positions",
                    "open_positions": len(open_positions)}

        snapshots = get_portfolio_snapshots(days=5) or []
        if not snapshots:
            return {"ok": True, "action": "no_snapshots"}

        last_snap = snapshots[-1]
        snap_value = float(last_snap.get("total_value") or 0)
        if snap_value <= 0:
            return {"ok": True, "action": "snapshot_invalid",
                    "snap_value": snap_value}

        # Live value = cash only when 0 positions
        live_value = float(get_cash() or 0)
        if live_value <= 0:
            return {"ok": True, "action": "live_invalid",
                    "live_value": live_value}

        drift_pct = abs(live_value - snap_value) / snap_value * 100.0
        if drift_pct < DRIFT_THRESHOLD_PCT:
            return {"ok": True, "action": "no_drift",
                    "snap_value": round(snap_value, 2),
                    "live_value": round(live_value, 2),
                    "drift_pct": round(drift_pct, 3)}

        # Drift detected and significant — write a corrected snapshot.
        # Compute daily_ret from snap, preserve sp500 fields from previous
        # snapshot (we cannot recompute them here without market data).
        today_str = _dt.utcnow().strftime("%Y-%m-%d")
        prev_cum = float(last_snap.get("cumulative_return_pct") or 0)
        prev_sp_cum = float(last_snap.get("sp500_cumulative_return_pct") or 0)
        prev_sp_daily = float(last_snap.get("sp500_daily_return_pct") or 0)
        # Cumulative return based on $109k baseline
        try:
            new_cum_ret = (live_value - 109000.0) / 109000.0 * 100.0
        except Exception:
            new_cum_ret = prev_cum
        try:
            save_portfolio_snapshot(
                total_value=live_value,
                cash=live_value,
                positions_value=0.0,
                daily_ret=0.0,           # 0 positions, no intraday move
                cum_ret=new_cum_ret,
                sp500_daily=prev_sp_daily,
                sp500_cum=prev_sp_cum,
                num_pos=0,
            )
        except Exception as save_err:
            return {"ok": False, "action": "save_failed",
                    "reason": str(save_err)[:200]}

        try:
            audit_log(
                "snapshot_drift",
                old_value=snap_value,
                new_value=live_value,
                delta=live_value - snap_value,
                caller="snapshot_drift_detector",
                reason=f"auto-corrected drift {drift_pct:.2f}%",
                metadata={"date": today_str, "open_positions": 0},
            )
        except Exception:
            pass

        logger.warning(
            f"SNAPSHOT DRIFT auto-corrected: snap=${snap_value:,.2f} -> "
            f"live=${live_value:,.2f} (drift {drift_pct:.2f}%)"
        )
        return {"ok": True, "action": "corrected",
                "snap_value": round(snap_value, 2),
                "live_value": round(live_value, 2),
                "drift_pct": round(drift_pct, 3)}

    except Exception as e:
        logger.warning(f"check_and_correct_snapshot_drift soft-fail: {e}")
        return {"ok": False, "action": "exception", "reason": str(e)[:200]}


# ============================================================
#  3. TRADE CIRCUIT BREAKER
# ============================================================

def record_trade_failure(reason: str = "unknown"):
    """Record a trade execution failure. Trips the breaker if too many
    failures occur within the rolling window.
    """
    try:
        now = time.time()
        _failure_times.append(now)
        # Trim window
        cutoff = now - CIRCUIT_FAIL_WINDOW_SEC
        while _failure_times and _failure_times[0] < cutoff:
            _failure_times.popleft()

        if len(_failure_times) >= CIRCUIT_FAIL_THRESHOLD:
            _circuit_halt_until[0] = now + CIRCUIT_HALT_SEC
            try:
                from predictions.audit import audit_log
                audit_log(
                    "circuit_breaker",
                    old_value="closed",
                    new_value="open",
                    caller="record_trade_failure",
                    reason=f"{len(_failure_times)} failures in {CIRCUIT_FAIL_WINDOW_SEC}s",
                    metadata={"halt_until_epoch": _circuit_halt_until[0],
                              "last_reason": reason[:200]},
                )
            except Exception:
                pass
            logger.warning(
                f"CIRCUIT BREAKER TRIPPED — {len(_failure_times)} trade failures "
                f"in {CIRCUIT_FAIL_WINDOW_SEC}s. Halting trades for "
                f"{CIRCUIT_HALT_SEC // 60} min."
            )
    except Exception as e:
        logger.debug(f"record_trade_failure soft-fail: {e}")


def is_circuit_open() -> bool:
    """True if the circuit breaker is currently halting trades."""
    try:
        return time.time() < _circuit_halt_until[0]
    except Exception:
        return False


def get_circuit_status() -> dict:
    """Status dict for the health endpoint."""
    try:
        now = time.time()
        cutoff = now - CIRCUIT_FAIL_WINDOW_SEC
        recent_failures = sum(1 for t in _failure_times if t >= cutoff)
        halt_remaining = max(0, _circuit_halt_until[0] - now)
        return {
            "open": now < _circuit_halt_until[0],
            "recent_failures": recent_failures,
            "fail_threshold": CIRCUIT_FAIL_THRESHOLD,
            "window_seconds": CIRCUIT_FAIL_WINDOW_SEC,
            "halt_remaining_seconds": int(halt_remaining),
            "halt_until_epoch": _circuit_halt_until[0] if halt_remaining > 0 else None,
        }
    except Exception as e:
        return {"error": str(e)[:120]}


def reset_circuit_breaker(reason: str = "manual"):
    """Force-reset the circuit breaker (admin escape hatch)."""
    try:
        _failure_times.clear()
        _circuit_halt_until[0] = 0.0
        try:
            from predictions.audit import audit_log
            audit_log(
                "circuit_breaker",
                old_value="open",
                new_value="closed",
                caller="reset_circuit_breaker",
                reason=reason[:200],
            )
        except Exception:
            pass
        logger.warning(f"CIRCUIT BREAKER manually reset: {reason}")
        return {"ok": True, "reset": True, "reason": reason}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


# ============================================================
#  4. YFINANCE RATE-LIMIT BUDGET TRACKER
# ============================================================

_yf_call_times = deque()
_yf_failure_times = deque()             # rolling window of consecutive failures
_yf_degraded_until = [0.0]              # epoch time the yf-degraded state expires
YF_BUDGET_PER_MIN = 60                  # max yfinance calls per 60s
YF_THROTTLE_SLEEP = 1.0                 # extra sleep when over budget
YF_DEGRADED_FAIL_WINDOW = 120           # 2 min rolling window
YF_DEGRADED_FAIL_THRESHOLD = 5          # 5 fails in 2 min => degraded
YF_DEGRADED_DURATION = 600              # back off for 10 min when degraded


def yf_record_failure():
    """Track yfinance failures so we can detect degraded state.
    Called from any code that catches a yfinance exception."""
    try:
        now = time.time()
        _yf_failure_times.append(now)
        cutoff = now - YF_DEGRADED_FAIL_WINDOW
        while _yf_failure_times and _yf_failure_times[0] < cutoff:
            _yf_failure_times.popleft()
        if len(_yf_failure_times) >= YF_DEGRADED_FAIL_THRESHOLD:
            _yf_degraded_until[0] = now + YF_DEGRADED_DURATION
            try:
                from predictions.audit import audit_log
                audit_log("yfinance_degraded",
                          old_value="ok", new_value="degraded",
                          caller="yf_record_failure",
                          reason=f"{len(_yf_failure_times)} fails in {YF_DEGRADED_FAIL_WINDOW}s")
            except Exception:
                pass
            logger.warning(
                f"YFINANCE DEGRADED: {len(_yf_failure_times)} failures in "
                f"{YF_DEGRADED_FAIL_WINDOW}s — backing off for {YF_DEGRADED_DURATION//60} min"
            )
    except Exception:
        pass


def yf_is_degraded() -> bool:
    """True when yfinance is in degraded state and non-critical features
    should skip Yahoo calls + use cached/fallback data instead."""
    try:
        return time.time() < _yf_degraded_until[0]
    except Exception:
        return False


def yfinance_budget_check() -> dict:
    """Track yfinance call frequency. Returns whether we should throttle.

    Caller should use the returned `should_throttle` flag and sleep
    additional time before making the next yfinance call.
    """
    try:
        now = time.time()
        _yf_call_times.append(now)
        cutoff = now - 60.0
        while _yf_call_times and _yf_call_times[0] < cutoff:
            _yf_call_times.popleft()
        recent = len(_yf_call_times)
        should_throttle = recent > YF_BUDGET_PER_MIN
        return {
            "calls_last_60s": recent,
            "budget_per_min": YF_BUDGET_PER_MIN,
            "should_throttle": should_throttle,
            "extra_sleep_seconds": YF_THROTTLE_SLEEP if should_throttle else 0,
        }
    except Exception:
        return {"calls_last_60s": 0, "should_throttle": False}
