"""
T-Bill Yield Simulator — Sentinel Quant

Simulates real-world treasury bill yield on idle cash. Real hedge funds
park their cash in T-bills, money-market funds, or sweep accounts, so
even unposted capital earns ~3-5% annualized. Without this, a paper
portfolio that holds 40% cash would underperform a real fund just from
the cash drag — making strategy backtests artificially negative.

Behavior:
  - Daily compound interest at T_BILL_ANNUAL_YIELD (default 3.5%)
  - Compounds on calendar days (T-bills accrue every day, including
    weekends and holidays — same as real money-market funds)
  - Idempotent — stores last_accrual_date in trading_state, can never
    double-credit on the same day
  - Catch-up — if the scheduler missed N days (deploy gap, downtime),
    accrues all N missing days in one shot
  - Safety cap — accrues at most MAX_CATCHUP_DAYS in a single run
    (defends against trading_state corruption)
  - Skips when cash <= 0 (no negative interest)
  - Tracks total_interest_earned and accrual count for transparency

API surface:
  - apply_tbill_interest() -> dict (idempotent, safe, never raises)
  - get_tbill_status() -> dict with config + state
  - set_annual_yield(yield_decimal) -> persists a new yield rate
"""

import logging
from datetime import datetime, date, timedelta

logger = logging.getLogger(__name__)


# ============================================================
#  CONFIG
# ============================================================

DEFAULT_ANNUAL_YIELD = 0.035  # 3.5% — roughly current 13-week T-bill (May 2026)
MAX_CATCHUP_DAYS = 14         # safety cap — never accrue more than 14 days at once
DAYS_PER_YEAR = 365.0         # T-bills accrue every calendar day

STATE_KEYS = {
    "last_date": "tbill_last_accrual_date",     # ISO date (YYYY-MM-DD)
    "total_earned": "tbill_total_interest_earned",
    "accrual_count": "tbill_accrual_count",
    "annual_yield": "tbill_annual_yield",       # configurable rate
}


# ============================================================
#  HELPERS
# ============================================================

def _safe_state_get(key: str, default: str = "") -> str:
    """Read a string from trading_state. Returns default on any error."""
    try:
        from predictions.models import get_trading_state
        return get_trading_state(key, default) or default
    except Exception as e:
        logger.debug(f"tbill _safe_state_get({key}) failed: {e}")
        return default


def _safe_state_set(key: str, value: str):
    """Write a string to trading_state. Silent on failure."""
    try:
        from predictions.models import set_trading_state
        set_trading_state(key, str(value))
    except Exception as e:
        logger.debug(f"tbill _safe_state_set({key}) failed: {e}")


def _get_annual_yield() -> float:
    """Return current annual yield (configurable via state)."""
    raw = _safe_state_get(STATE_KEYS["annual_yield"], "")
    if not raw:
        return DEFAULT_ANNUAL_YIELD
    try:
        v = float(raw)
        # Sanity bounds — yield should be in (-0.05, 0.20) for realistic rates
        if -0.05 < v < 0.20:
            return v
    except Exception:
        pass
    return DEFAULT_ANNUAL_YIELD


def _today_iso() -> str:
    return date.today().isoformat()


def _parse_date(s: str):
    """Parse YYYY-MM-DD safely. Returns None on failure."""
    try:
        return date.fromisoformat(s)
    except Exception:
        return None


# ============================================================
#  CORE — accrue interest
# ============================================================

def apply_tbill_interest() -> dict:
    """Idempotent daily T-bill interest accrual on idle cash.

    Returns a dict describing what happened. Always returns — never raises.

    Result schema:
        {
          "ok": bool,
          "credited": bool,
          "days_accrued": int,
          "interest_credited": float,   # dollars added this run
          "starting_cash": float,
          "ending_cash": float,
          "annual_yield_pct": float,
          "today": "YYYY-MM-DD",
          "last_accrual_date": "YYYY-MM-DD" | None,
          "reason"?: str (when not credited),
        }
    """
    today = date.today()
    today_str = today.isoformat()
    annual_yield = _get_annual_yield()
    daily_factor = 1.0 + (annual_yield / DAYS_PER_YEAR)

    try:
        from predictions.models import get_cash, adjust_cash
        starting_cash = float(get_cash())
    except Exception as e:
        logger.warning(f"tbill: get_cash failed: {e}")
        return {
            "ok": False,
            "credited": False,
            "days_accrued": 0,
            "interest_credited": 0.0,
            "starting_cash": None,
            "ending_cash": None,
            "annual_yield_pct": round(annual_yield * 100, 4),
            "today": today_str,
            "reason": f"get_cash_failed: {e}",
        }

    # Don't accrue on negative or zero cash
    if starting_cash <= 0:
        # Still update last_date so we don't try again until tomorrow
        _safe_state_set(STATE_KEYS["last_date"], today_str)
        return {
            "ok": True,
            "credited": False,
            "days_accrued": 0,
            "interest_credited": 0.0,
            "starting_cash": starting_cash,
            "ending_cash": starting_cash,
            "annual_yield_pct": round(annual_yield * 100, 4),
            "today": today_str,
            "reason": "cash_non_positive",
        }

    # Determine how many days to accrue
    last_date_raw = _safe_state_get(STATE_KEYS["last_date"], "")
    last_date = _parse_date(last_date_raw)

    if last_date is None:
        # First run — set baseline, do not accrue (avoid retroactive credit)
        _safe_state_set(STATE_KEYS["last_date"], today_str)
        return {
            "ok": True,
            "credited": False,
            "days_accrued": 0,
            "interest_credited": 0.0,
            "starting_cash": starting_cash,
            "ending_cash": starting_cash,
            "annual_yield_pct": round(annual_yield * 100, 4),
            "today": today_str,
            "last_accrual_date": None,
            "reason": "first_run_baseline_set",
        }

    days_elapsed = (today - last_date).days

    if days_elapsed <= 0:
        # Already accrued today, or clock anomaly — skip
        return {
            "ok": True,
            "credited": False,
            "days_accrued": 0,
            "interest_credited": 0.0,
            "starting_cash": starting_cash,
            "ending_cash": starting_cash,
            "annual_yield_pct": round(annual_yield * 100, 4),
            "today": today_str,
            "last_accrual_date": last_date.isoformat(),
            "reason": "already_accrued_today",
        }

    # Cap catch-up to defend against state corruption
    days_to_accrue = min(days_elapsed, MAX_CATCHUP_DAYS)

    # Compound: cash * daily_factor^days_to_accrue
    new_cash = starting_cash * (daily_factor ** days_to_accrue)
    interest = round(new_cash - starting_cash, 4)

    # Apply to cash
    try:
        adjust_cash(round(interest, 2))
    except Exception as e:
        logger.error(f"tbill: adjust_cash failed: {e}")
        return {
            "ok": False,
            "credited": False,
            "days_accrued": 0,
            "interest_credited": 0.0,
            "starting_cash": starting_cash,
            "ending_cash": starting_cash,
            "annual_yield_pct": round(annual_yield * 100, 4),
            "today": today_str,
            "last_accrual_date": last_date.isoformat(),
            "reason": f"adjust_cash_failed: {e}",
        }

    # Persist new state
    _safe_state_set(STATE_KEYS["last_date"], today_str)

    # Update running totals
    try:
        prev_total = float(_safe_state_get(STATE_KEYS["total_earned"], "0") or 0)
    except Exception:
        prev_total = 0.0
    new_total = round(prev_total + interest, 4)
    _safe_state_set(STATE_KEYS["total_earned"], f"{new_total:.4f}")

    try:
        prev_count = int(_safe_state_get(STATE_KEYS["accrual_count"], "0") or 0)
    except Exception:
        prev_count = 0
    _safe_state_set(STATE_KEYS["accrual_count"], str(prev_count + 1))

    logger.warning(
        f"T-BILL ACCRUAL: +${interest:.2f} ({days_to_accrue}d at "
        f"{annual_yield*100:.2f}%/yr) | cash ${starting_cash:.2f}->${starting_cash+interest:.2f} "
        f"| total earned ${new_total:.2f}"
    )

    return {
        "ok": True,
        "credited": True,
        "days_accrued": days_to_accrue,
        "days_elapsed_actual": days_elapsed,
        "interest_credited": interest,
        "starting_cash": round(starting_cash, 2),
        "ending_cash": round(starting_cash + interest, 2),
        "annual_yield_pct": round(annual_yield * 100, 4),
        "today": today_str,
        "last_accrual_date": last_date.isoformat(),
        "total_interest_earned_to_date": new_total,
        "accrual_count": prev_count + 1,
    }


# ============================================================
#  PUBLIC STATUS / CONFIG
# ============================================================

def get_tbill_status() -> dict:
    """Lightweight status for the API endpoint. Never raises."""
    try:
        annual_yield = _get_annual_yield()
        last_date = _safe_state_get(STATE_KEYS["last_date"], "") or None
        try:
            total_earned = float(_safe_state_get(STATE_KEYS["total_earned"], "0") or 0)
        except Exception:
            total_earned = 0.0
        try:
            accrual_count = int(_safe_state_get(STATE_KEYS["accrual_count"], "0") or 0)
        except Exception:
            accrual_count = 0

        try:
            from predictions.models import get_cash
            current_cash = float(get_cash())
        except Exception:
            current_cash = None

        # Projected daily interest if accrued right now
        projected_daily = None
        if current_cash and current_cash > 0:
            projected_daily = round(current_cash * (annual_yield / DAYS_PER_YEAR), 4)

        return {
            "ok": True,
            "engine": "tbill_yield_v1",
            "annual_yield_pct": round(annual_yield * 100, 4),
            "annual_yield_decimal": annual_yield,
            "compounding": "daily_calendar",
            "last_accrual_date": last_date,
            "total_interest_earned_to_date": round(total_earned, 4),
            "accrual_count": accrual_count,
            "current_cash": round(current_cash, 2) if current_cash is not None else None,
            "projected_daily_interest": projected_daily,
            "projected_annual_interest": (
                round(current_cash * annual_yield, 2) if current_cash else None
            ),
            "max_catchup_days": MAX_CATCHUP_DAYS,
        }
    except Exception as e:
        logger.warning(f"get_tbill_status failed: {e}")
        return {"ok": False, "engine": "tbill_yield_v1", "reason": str(e)[:200]}


def set_annual_yield(new_yield: float) -> dict:
    """Persist a new T-bill yield. Validates bounds. Returns confirmation."""
    try:
        v = float(new_yield)
    except Exception:
        return {"ok": False, "reason": "yield must be a number"}
    if not (-0.05 < v < 0.20):
        return {"ok": False, "reason": "yield out of bounds (-5% to +20%)"}
    _safe_state_set(STATE_KEYS["annual_yield"], f"{v:.6f}")
    return {"ok": True, "annual_yield_pct": round(v * 100, 4)}
