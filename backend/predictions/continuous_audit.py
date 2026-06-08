"""
Continuous Self-Audit + Auto-Fix — the "robot Jackson" of the system.

Runs every 5 min during market hours. For each cycle:
  1. Runs the full trade-math reconciliation suite + cross-path checks
  2. Attempts safe auto-fixes (clear stale caches, refresh stuck values)
  3. Re-runs failed checks after each autofix
  4. If any HIGH/CRITICAL failure remains, HALTS new trade entries
  5. Auto-clears halt only after TWO consecutive clean passes
     (prevents flapping when a single noisy reading misleads us)

Halt flag lives in trading_state(audit_halt_active). Trade execution
checks it before opening any position. Manual clear available via
the /api/admin/audit-halt-clear endpoint.

Hardening:
  - Module-level lock prevents concurrent audit runs (scheduler + manual)
  - Halt-check is cached 1 sec to spare DB on hot trade-entry paths
  - Anti-flap: 2 consecutive clean passes required to auto-clear halt
  - Each check fully isolated in try/except so one bug can't sink audit
  - Autofix re-runs ONLY the affected check, not the whole suite
  - All severities/decisions are explicit constants — no magic numbers

Public API:
  run_audit_and_autofix() -> dict
  is_audit_halted() -> bool         (used by trade execution)
  get_audit_status() -> dict        (used by /api/audit/status)
  clear_audit_halt(reason) -> dict  (used by /api/admin/audit-halt-clear)
"""
import logging
import time
import json
import threading
from datetime import datetime

logger = logging.getLogger(__name__)

# ============================================================
# Persistent state keys (trading_state table)
# ============================================================
HALT_FLAG_KEY = "audit_halt_active"          # "1" when halted, "" otherwise
HALT_REASON_KEY = "audit_halt_reason"
HALT_AT_KEY = "audit_halt_at"
HALT_CLEAN_STREAK_KEY = "audit_clean_streak"  # int — consecutive clean audits
LAST_AUDIT_KEY = "audit_last_run"            # JSON of last result (truncated)
LAST_AUDIT_AT_KEY = "audit_last_run_at"

# Severity levels — only HIGH/CRITICAL trigger halt
SEVERITY_LOW = "low"          # log only
SEVERITY_MED = "medium"       # log + attempt autofix
SEVERITY_HIGH = "high"        # log + autofix + halt if autofix fails
SEVERITY_CRIT = "critical"    # log + halt immediately, no autofix attempted
_HALT_SEVERITIES = {SEVERITY_HIGH, SEVERITY_CRIT}

# Anti-flap: require N consecutive clean audits before clearing halt
CLEAN_STREAK_THRESHOLD = 2

# Halt-check cache (called from every trade entry — keep cheap)
_halt_cache = {"value": None, "ts": 0.0}
_HALT_CACHE_TTL_SEC = 1.0  # short — flag changes must propagate fast

# Concurrency: prevent overlapping audit runs
_audit_lock = threading.Lock()


# ============================================================
# State I/O — wrapped so a broken DB never crashes the audit
# ============================================================
def _read_state(key: str, default: str = "") -> str:
    try:
        from predictions.models import get_trading_state
        return get_trading_state(key, default)
    except Exception as e:
        logger.debug(f"audit: _read_state({key}) failed: {e}")
        return default


def _write_state(key: str, value: str):
    try:
        from predictions.models import set_trading_state
        set_trading_state(key, str(value))
    except Exception as e:
        logger.error(f"audit: _write_state({key}) failed: {e}")


# ============================================================
# Halt flag — hot path, cached briefly
# ============================================================
def is_audit_halted() -> bool:
    """Fast check called by execute_trades_from_signals on every cycle.
    1-second cache because the flag changes infrequently and trade
    execution can call this many times. Never raises."""
    try:
        now = time.time()
        if (_halt_cache["value"] is not None
                and (now - _halt_cache["ts"]) < _HALT_CACHE_TTL_SEC):
            return bool(_halt_cache["value"])
        v = (_read_state(HALT_FLAG_KEY, "") == "1")
        _halt_cache["value"] = v
        _halt_cache["ts"] = now
        return v
    except Exception:
        return False


def _invalidate_halt_cache():
    _halt_cache["value"] = None
    _halt_cache["ts"] = 0.0


def _set_halt(reason: str):
    now = datetime.now().isoformat()
    _write_state(HALT_FLAG_KEY, "1")
    _write_state(HALT_REASON_KEY, reason[:500])
    _write_state(HALT_AT_KEY, now)
    _write_state(HALT_CLEAN_STREAK_KEY, "0")
    _invalidate_halt_cache()
    logger.error(
        f"AUDIT HALT ACTIVATED at {now} — new trades blocked. Reason: {reason}"
    )


def clear_audit_halt(reason: str = "manual") -> dict:
    """Force-clear the halt flag. Used by /api/admin/audit-halt-clear
    and internally by the audit itself after N clean passes."""
    prev_active = is_audit_halted()
    prev_reason = _read_state(HALT_REASON_KEY, "")
    prev_at = _read_state(HALT_AT_KEY, "")
    _write_state(HALT_FLAG_KEY, "")
    _write_state(HALT_REASON_KEY, "")
    _write_state(HALT_AT_KEY, "")
    _write_state(HALT_CLEAN_STREAK_KEY, "0")
    _invalidate_halt_cache()
    if prev_active:
        logger.warning(
            f"AUDIT HALT CLEARED by '{reason}'. Previous: "
            f"reason='{prev_reason}' at={prev_at}"
        )
    return {
        "ok": True,
        "was_halted": prev_active,
        "previous_reason": prev_reason,
        "previous_halt_at": prev_at,
        "cleared_by": reason,
        "cleared_at": datetime.now().isoformat(),
    }


def get_audit_status() -> dict:
    """Snapshot for the /api/audit/status endpoint."""
    halted = is_audit_halted()
    last_raw = _read_state(LAST_AUDIT_KEY, "")
    last_data = None
    try:
        if last_raw:
            last_data = json.loads(last_raw)
    except Exception:
        pass
    try:
        clean_streak = int(_read_state(HALT_CLEAN_STREAK_KEY, "0") or 0)
    except Exception:
        clean_streak = 0
    return {
        "halted": halted,
        "halt_reason": _read_state(HALT_REASON_KEY, "") if halted else None,
        "halt_at": _read_state(HALT_AT_KEY, "") if halted else None,
        "clean_streak": clean_streak,
        "clean_streak_required_to_auto_clear": CLEAN_STREAK_THRESHOLD,
        "last_audit_at": _read_state(LAST_AUDIT_AT_KEY, ""),
        "last_audit": last_data,
    }


# ============================================================
# CHECK FACTORY
# Returns dict: {name, ok, severity, detail, autofix}
# ============================================================
def _mk_result(name: str, ok: bool, severity: str, detail: str,
               autofix: str = None) -> dict:
    return {"name": name, "ok": bool(ok), "severity": severity,
            "detail": detail[:500], "autofix": autofix}


def _check_capital_aligned() -> dict:
    """INITIAL_CAPITAL must equal ORIGINAL_CAPITAL (the F17 fix)."""
    try:
        from predictions.paper_trader import INITIAL_CAPITAL, ORIGINAL_CAPITAL
        diff = abs(float(INITIAL_CAPITAL) - float(ORIGINAL_CAPITAL))
        ok = diff < 0.01
        return _mk_result(
            "capital_aligned", ok,
            SEVERITY_LOW if ok else SEVERITY_HIGH,
            f"INITIAL=${INITIAL_CAPITAL:,.0f} ORIGINAL=${ORIGINAL_CAPITAL:,.0f}",
        )
    except Exception as e:
        return _mk_result("capital_aligned", False, SEVERITY_MED,
                          f"check_error: {str(e)[:200]}")


def _check_nav_reconciliation() -> dict:
    """cash + Σ(short-aware positions) must equal reported total_value
    within $1. Mismatch = NAV math drift (phantom trade or stale price)."""
    try:
        from predictions.models import get_cash, get_open_trades
        from predictions.paper_trader import (
            _get_current_prices, _short_aware_positions_value,
            get_portfolio_state,
        )
        cash = float(get_cash() or 0)
        open_trades = get_open_trades() or []
        tickers = [t["ticker"] for t in open_trades if t.get("ticker")]
        prices = _get_current_prices(tickers) if tickers else {}
        recomputed_pos = _short_aware_positions_value(open_trades, prices)
        recomputed = cash + recomputed_pos
        state = get_portfolio_state() or {}
        reported = float(state.get("total_value") or 0)
        diff = abs(recomputed - reported)
        ok = diff <= 1.0
        # CRITICAL if mismatch > $100, HIGH otherwise
        sev = SEVERITY_LOW if ok else (SEVERITY_CRIT if diff > 100 else SEVERITY_HIGH)
        return _mk_result(
            "nav_reconciliation", ok, sev,
            f"recomputed=${recomputed:,.2f} reported=${reported:,.2f} "
            f"diff=${diff:,.2f} (cash=${cash:,.2f} pos=${recomputed_pos:,.2f} n={len(open_trades)})",
        )
    except Exception as e:
        return _mk_result("nav_reconciliation", False, SEVERITY_MED,
                          f"check_error: {str(e)[:200]}")


def _check_phantom_trades() -> dict:
    """Every open trade must have entry_date + positive entry_price.
    Every closed trade must have exit_date + non-null pnl_dollars
    (closed_flat_validator status is the explicit exception)."""
    try:
        from predictions.models import get_all_paper_trades
        all_trades = get_all_paper_trades() or []
        phantoms = []
        for t in all_trades:
            try:
                status = (t.get("status") or "").lower()
                tid = t.get("id")
                if status == "open":
                    if not t.get("entry_date") or not t.get("entry_price"):
                        phantoms.append({"id": tid, "type": "open_missing_fields"})
                    elif float(t.get("entry_price") or 0) <= 0:
                        phantoms.append({"id": tid, "type": "open_bad_price"})
                elif status == "closed":
                    if not t.get("exit_date"):
                        phantoms.append({"id": tid, "type": "closed_no_exit_date"})
                    elif t.get("pnl_dollars") is None:
                        phantoms.append({"id": tid, "type": "closed_null_pnl"})
            except Exception:
                continue
        ok = len(phantoms) == 0
        return _mk_result(
            "phantom_trades", ok,
            SEVERITY_LOW if ok else SEVERITY_CRIT,
            f"phantom_count={len(phantoms)} samples={phantoms[:3]}",
        )
    except Exception as e:
        return _mk_result("phantom_trades", False, SEVERITY_MED,
                          f"check_error: {str(e)[:200]}")


def _check_snapshot_sanity() -> dict:
    """Last 5 daily snapshots: total_value in [10k, 5x_original]
    AND |daily_return_pct| < 10%."""
    try:
        from predictions.models import get_portfolio_snapshots
        from predictions.paper_trader import ORIGINAL_CAPITAL
        snaps = get_portfolio_snapshots(days=5) or []
        bad = []
        for s in snaps:
            try:
                tv = float(s.get("total_value") or 0)
                dr = float(s.get("daily_return_pct") or 0)
                if not (10_000.0 <= tv <= ORIGINAL_CAPITAL * 5.0):
                    bad.append({"date": s.get("snapshot_date"),
                                "reason": f"total=${tv:,.0f}_out_of_bounds"})
                elif abs(dr) > 10.0:
                    bad.append({"date": s.get("snapshot_date"),
                                "reason": f"daily_return={dr:.2f}%_>10"})
            except Exception:
                continue
        ok = len(bad) == 0
        return _mk_result(
            "snapshot_sanity", ok,
            SEVERITY_LOW if ok else SEVERITY_HIGH,
            f"checked={len(snaps)} flagged={len(bad)} {bad[:3]}",
        )
    except Exception as e:
        return _mk_result("snapshot_sanity", False, SEVERITY_MED,
                          f"check_error: {str(e)[:200]}")


def _check_vix_path_consistency() -> dict:
    """vix_guard and regime engine VIX must agree within 1.0.
    AUTOFIX: clear vix_guard cache so a fresh fetch syncs both paths."""
    try:
        from analytics.vix_guard import get_vix_safe
        from analysis.quant_engine import detect_market_regime
        guard = get_vix_safe()
        guard_val = float(guard.get("value") or 0)
        regime = detect_market_regime() or {}
        regime_val = float(regime.get("vix_level") or 0)
        if guard_val <= 0 or regime_val <= 0:
            return _mk_result(
                "vix_path_consistency", False, SEVERITY_LOW,
                f"guard={guard_val} regime={regime_val} (one missing)",
            )
        diff = abs(guard_val - regime_val)
        ok = diff <= 1.0
        return _mk_result(
            "vix_path_consistency", ok,
            SEVERITY_LOW if ok else SEVERITY_MED,
            f"guard={guard_val:.2f} regime={regime_val:.2f} diff={diff:.2f}",
            autofix="clear_vix_cache" if not ok else None,
        )
    except Exception as e:
        return _mk_result("vix_path_consistency", False, SEVERITY_LOW,
                          f"check_error: {str(e)[:200]}")


def _check_equity_curve_baseline() -> dict:
    """Each recent snapshot's implied baseline (total_value /
    (1 + cum_ret/100)) must match ORIGINAL_CAPITAL within 5%."""
    try:
        from predictions.paper_trader import ORIGINAL_CAPITAL
        from predictions.models import get_portfolio_snapshots
        snaps = get_portfolio_snapshots(days=3) or []
        bad = []
        for s in snaps:
            try:
                tv = float(s.get("total_value") or 0)
                cr = s.get("cumulative_return_pct")
                if cr is None or tv <= 0:
                    continue
                implied = tv / (1 + float(cr) / 100.0)
                drift = abs(implied - ORIGINAL_CAPITAL) / ORIGINAL_CAPITAL
                if drift > 0.05:
                    bad.append({
                        "date": s.get("snapshot_date"),
                        "implied_baseline": round(implied, 2),
                        "expected": ORIGINAL_CAPITAL,
                        "drift_pct": round(drift * 100, 1),
                    })
            except Exception:
                continue
        ok = len(bad) == 0
        return _mk_result(
            "equity_curve_baseline", ok,
            SEVERITY_LOW if ok else SEVERITY_MED,
            f"snapshots_with_baseline_drift={len(bad)} samples={bad[:2]}",
        )
    except Exception as e:
        return _mk_result("equity_curve_baseline", False, SEVERITY_LOW,
                          f"check_error: {str(e)[:200]}")


def _check_cash_floor() -> dict:
    """Cash never goes negative or above 5x ORIGINAL_CAPITAL."""
    try:
        from predictions.models import get_cash
        from predictions.paper_trader import ORIGINAL_CAPITAL
        cash = float(get_cash() or 0)
        if cash < 0:
            return _mk_result("cash_floor", False, SEVERITY_CRIT,
                              f"cash=${cash:,.2f}_negative")
        if cash > ORIGINAL_CAPITAL * 5.0:
            return _mk_result("cash_floor", False, SEVERITY_HIGH,
                              f"cash=${cash:,.2f}_exceeds_5x")
        return _mk_result("cash_floor", True, SEVERITY_LOW,
                          f"cash=${cash:,.2f}")
    except Exception as e:
        return _mk_result("cash_floor", False, SEVERITY_LOW,
                          f"check_error: {str(e)[:200]}")


def _check_circuit_breaker() -> dict:
    """The sentinels circuit breaker must not be open (would mean lots
    of recent failures). If open, halt new trades regardless."""
    try:
        from predictions.sentinels import get_circuit_status
        cb = get_circuit_status() or {}
        is_open = bool(cb.get("open"))
        return _mk_result(
            "circuit_breaker", not is_open,
            SEVERITY_LOW if not is_open else SEVERITY_HIGH,
            f"open={is_open} recent_failures={cb.get('recent_failures', 0)}",
        )
    except Exception as e:
        # If we can't check it, don't halt — log only
        return _mk_result("circuit_breaker", True, SEVERITY_LOW,
                          f"check_skipped: {str(e)[:200]}")


# Name → callable lookup (used for surgical re-runs after autofix)
_CHECKS_BY_NAME = {
    "capital_aligned": _check_capital_aligned,
    "nav_reconciliation": _check_nav_reconciliation,
    "phantom_trades": _check_phantom_trades,
    "snapshot_sanity": _check_snapshot_sanity,
    "vix_path_consistency": _check_vix_path_consistency,
    "equity_curve_baseline": _check_equity_curve_baseline,
    "cash_floor": _check_cash_floor,
    "circuit_breaker": _check_circuit_breaker,
}
# Order matters for log readability
_CHECK_ORDER = list(_CHECKS_BY_NAME.keys())


# ============================================================
# AUTOFIX ACTIONS
# Each returns: {ok, action, ...details}
# Only SAFE, REVERSIBLE actions allowed. No data mutation that hides
# root causes or that we can't undo.
# ============================================================
def _autofix_clear_vix_cache() -> dict:
    """Clear vix_guard persistent cache + refetch fresh."""
    try:
        from analytics.vix_guard import (
            get_vix_safe, LAST_GOOD_VIX_KEY, LAST_GOOD_VIX_TS_KEY,
        )
        _write_state(LAST_GOOD_VIX_KEY, "")
        _write_state(LAST_GOOD_VIX_TS_KEY, "")
        after = get_vix_safe()
        return {"ok": True, "action": "clear_vix_cache",
                "after_value": after.get("value"),
                "after_source": after.get("source"),
                "after_confidence": after.get("confidence")}
    except Exception as e:
        return {"ok": False, "action": "clear_vix_cache",
                "error": str(e)[:200]}


_AUTOFIX_REGISTRY = {
    "clear_vix_cache": _autofix_clear_vix_cache,
}


# ============================================================
# MAIN AUDIT ENTRYPOINT
# ============================================================
def _run_single_check(name: str) -> dict:
    """Run one named check, fully isolated."""
    fn = _CHECKS_BY_NAME.get(name)
    if not fn:
        return _mk_result(name, False, SEVERITY_LOW, f"unknown_check={name}")
    try:
        return fn()
    except Exception as e:
        return _mk_result(name, False, SEVERITY_LOW,
                          f"check_raised: {str(e)[:200]}")


def _attempt_autofix(check_name: str, action: str) -> dict:
    """Run an autofix action, fully isolated."""
    fix_fn = _AUTOFIX_REGISTRY.get(action)
    if not fix_fn:
        return {"ok": False, "action": action,
                "error": f"no_registered_autofix={action}"}
    try:
        return fix_fn()
    except Exception as e:
        return {"ok": False, "action": action, "error": str(e)[:200]}


def run_audit_and_autofix() -> dict:
    """Full audit + autofix pass. Returns structured result; never raises.

    Halt logic:
      - Any HIGH/CRITICAL failure remaining after autofix → SET halt
      - All clean for CLEAN_STREAK_THRESHOLD consecutive runs → CLEAR halt
      - Single clean run when halted: increment streak, keep halt
    """
    if not _audit_lock.acquire(blocking=False):
        # Another audit is already running — return a marker without forcing
        # the caller to wait (callers are the scheduler + manual trigger)
        return {
            "ok": False,
            "skipped": True,
            "reason": "another_audit_in_progress",
            "started_at": datetime.now().isoformat(),
        }
    try:
        started_at = datetime.now().isoformat()
        t0 = time.time()

        # Pass 1: run all checks
        results = [_run_single_check(name) for name in _CHECK_ORDER]
        results_by_name = {r["name"]: r for r in results}

        # Pass 2: attempt autofix for each failing check with an action
        autofix_attempts = []
        for r in list(results):  # copy to allow re-mutation
            if r["ok"] or not r.get("autofix"):
                continue
            action = r["autofix"]
            fix_outcome = _attempt_autofix(r["name"], action)
            autofix_attempts.append({
                "check": r["name"],
                "action": action,
                "result": fix_outcome,
            })
            logger.warning(
                f"AUDIT AUTOFIX: {r['name']} → {action} → "
                f"ok={fix_outcome.get('ok', False)}"
            )
            # Pass 3: re-run THIS check to see if autofix worked
            new_r = _run_single_check(r["name"])
            new_r["recheck_after_autofix"] = True
            results_by_name[r["name"]] = new_r

        # Rebuild ordered results from the (possibly updated) map
        results = [results_by_name[n] for n in _CHECK_ORDER]

        # Pass 4: verdict
        crit_high = [r for r in results
                     if not r["ok"] and r["severity"] in _HALT_SEVERITIES]
        meds = [r for r in results
                if not r["ok"] and r["severity"] == SEVERITY_MED]
        all_clean = (len(crit_high) == 0 and len(meds) == 0)

        # Halt / unhalt decision with anti-flap
        was_halted = is_audit_halted()
        halt_action = None  # "set" | "cleared" | "streak_progress" | None

        if len(crit_high) > 0:
            reasons = " | ".join(
                f"{r['name']}: {r['detail']}" for r in crit_high[:2]
            )
            if not was_halted:
                _set_halt(reasons)
                halt_action = "set"
            else:
                # Already halted; update reason if it's a new failure
                _write_state(HALT_REASON_KEY, reasons[:500])
                _write_state(HALT_CLEAN_STREAK_KEY, "0")
        else:
            # No critical/high failures right now
            if was_halted:
                try:
                    streak = int(_read_state(HALT_CLEAN_STREAK_KEY, "0") or 0)
                except Exception:
                    streak = 0
                streak += 1
                _write_state(HALT_CLEAN_STREAK_KEY, str(streak))
                if streak >= CLEAN_STREAK_THRESHOLD:
                    clear_audit_halt(
                        reason=f"auto_clear_clean_streak={streak}"
                    )
                    halt_action = "cleared"
                else:
                    halt_action = "streak_progress"
                    logger.info(
                        f"AUDIT clean streak {streak}/"
                        f"{CLEAN_STREAK_THRESHOLD} (halt held)"
                    )

        elapsed = round(time.time() - t0, 3)
        audit_result = {
            "ok": all_clean,
            "started_at": started_at,
            "elapsed_seconds": elapsed,
            "checks": results,
            "autofix_attempts": autofix_attempts,
            "critical_or_high_failures": len(crit_high),
            "medium_failures": len(meds),
            "halt_action": halt_action,
            "halted_after": is_audit_halted(),
        }

        # Persist (truncated) for the status endpoint
        try:
            blob = json.dumps(audit_result, default=str)
            if len(blob) > 8000:
                blob = blob[:8000] + '..."}]'
            _write_state(LAST_AUDIT_KEY, blob)
            _write_state(LAST_AUDIT_AT_KEY, started_at)
        except Exception as e:
            logger.debug(f"audit: result persist failed: {e}")

        # One-line summary log
        if not all_clean:
            logger.warning(
                f"AUDIT FAIL ({elapsed}s): crit/high={len(crit_high)} "
                f"med={len(meds)} halt={halt_action or '(unchanged)'}"
            )
        else:
            logger.info(
                f"AUDIT OK ({elapsed}s, {len(results)} checks) "
                f"halt={halt_action or '(unchanged)'}"
            )
        return audit_result
    finally:
        _audit_lock.release()
