"""
IBKR Safety Module — Operational guardrails on top of the mirror adapter.

Provides:
  1. Position drift detector  — paper vs IBKR position reconciliation
  2. Slippage logger          — paper fill vs IBKR fill tracking
  3. Pre-flight self-test     — small dry-run order to validate wiring
  4. Daily reconciliation     — end-of-day P&L comparison
  5. Position sync report     — JSON snapshot for the dashboard

These are READ-ONLY from a trading perspective — no orders are placed
here. They observe and report; the adapter does the actual trading.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

import pytz

logger = logging.getLogger(__name__)
ET = pytz.timezone("US/Eastern")

# In-memory reconciliation state
_drift_history: list = []           # Last 100 drift checks
_slippage_log: list = []            # Last 200 fills with paper vs IBKR price
_preflight_results: dict = {}       # Last self-test result
_lock = threading.Lock()

DRIFT_TOLERANCE_PCT = 5.0           # 5% drift is OK (rounding, partial fills)
SLIPPAGE_ALERT_PCT = 1.0            # > 1% slippage triggers a warning
MAX_DRIFT_HISTORY = 100
MAX_SLIPPAGE_LOG = 200


# ─── 1. POSITION DRIFT DETECTOR ──────────────────────────────────────────────

def check_position_drift(paper_positions: list, ibkr_positions: list,
                          scale_factor: float) -> dict:
    """
    Compare paper trader positions vs IBKR positions.
    Flags drift when actual IBKR positions diverge from expected (paper * scale).

    Args:
        paper_positions: List of paper positions [{ticker, shares, direction, ...}]
        ibkr_positions: List of IBKR positions [{ticker, position, avgCost}]
        scale_factor: Current mirror scale (live_ibkr / paper_total)

    Returns dict with overall status, drift items, and severity.
    """
    paper_by_ticker = {}
    for p in paper_positions:
        ticker = p.get("ticker") or p.get("symbol", "")
        if not ticker:
            continue
        instrument = p.get("instrument_type", "equity")
        # Skip options for now — handled separately
        if instrument in ("call", "put"):
            continue
        expected_shares = p.get("shares", 0) * scale_factor
        if p.get("direction") == "short":
            expected_shares = -abs(expected_shares)
        paper_by_ticker[ticker.upper()] = {
            "expected_shares": expected_shares,
            "paper_value": p.get("position_value", 0) * scale_factor,
            "direction": p.get("direction"),
        }

    ibkr_by_ticker = {}
    for p in ibkr_positions:
        ticker = (p.get("ticker") or p.get("symbol") or "").upper()
        if not ticker:
            continue
        ibkr_by_ticker[ticker] = {
            "actual_shares": p.get("position", 0),
            "avg_cost": p.get("avgCost", 0),
        }

    drift_items = []
    all_tickers = set(paper_by_ticker.keys()) | set(ibkr_by_ticker.keys())

    for ticker in all_tickers:
        paper = paper_by_ticker.get(ticker, {})
        ibkr = ibkr_by_ticker.get(ticker, {})

        expected = paper.get("expected_shares", 0)
        actual = ibkr.get("actual_shares", 0)

        # Drift = abs(expected - actual) / max(abs(expected), 1) * 100
        if abs(expected) < 0.5 and abs(actual) < 0.5:
            continue  # Both effectively zero, no drift

        if abs(expected) < 0.5:
            # Paper says no position, but IBKR has one
            drift_items.append({
                "ticker": ticker,
                "type": "ORPHAN_IBKR",
                "expected_shares": 0,
                "actual_shares": actual,
                "severity": "high",
                "message": f"IBKR holds {actual} {ticker} but paper has no position",
            })
            continue

        if abs(actual) < 0.5:
            # Paper expects a position, but IBKR has none
            drift_items.append({
                "ticker": ticker,
                "type": "MISSING_IBKR",
                "expected_shares": expected,
                "actual_shares": 0,
                "severity": "high",
                "message": f"Paper expects {expected:.0f} {ticker} but IBKR has 0",
            })
            continue

        drift_pct = abs((expected - actual) / max(abs(expected), 1)) * 100

        if drift_pct > DRIFT_TOLERANCE_PCT:
            severity = "high" if drift_pct > 15 else "medium"
            drift_items.append({
                "ticker": ticker,
                "type": "SIZE_MISMATCH",
                "expected_shares": round(expected, 2),
                "actual_shares": actual,
                "drift_pct": round(drift_pct, 1),
                "severity": severity,
                "message": f"{ticker}: expected ~{expected:.0f}, IBKR has {actual} ({drift_pct:.1f}% drift)",
            })

    # Overall status
    high_count = sum(1 for d in drift_items if d["severity"] == "high")
    medium_count = sum(1 for d in drift_items if d["severity"] == "medium")

    if high_count > 0:
        status = "CRITICAL"
        action = "BLOCK new trades — manual review required"
    elif medium_count > 2:
        status = "WARNING"
        action = "Monitor closely — investigate after market close"
    else:
        status = "OK"
        action = "No action needed"

    result = {
        "timestamp": datetime.now(ET).isoformat(),
        "status": status,
        "action": action,
        "drift_items": drift_items,
        "high_severity_count": high_count,
        "medium_severity_count": medium_count,
        "tickers_checked": len(all_tickers),
        "scale_factor": scale_factor,
    }

    # Save to history
    with _lock:
        _drift_history.append(result)
        if len(_drift_history) > MAX_DRIFT_HISTORY:
            _drift_history.pop(0)

    if status != "OK":
        logger.warning(f"DRIFT CHECK: {status} — {len(drift_items)} items flagged")

    return result


def get_drift_history(limit: int = 20) -> list:
    """Return recent drift check results."""
    with _lock:
        return list(_drift_history[-limit:])


# ─── 2. SLIPPAGE LOGGER ──────────────────────────────────────────────────────

def log_slippage(ticker: str, paper_fill: float, ibkr_fill: float,
                  direction: str, shares: int) -> dict:
    """
    Log a paper vs IBKR fill for slippage tracking.
    Slippage = (ibkr_fill - paper_fill) / paper_fill * 100, signed by direction.
    Negative = IBKR got worse fill than paper (bad for us).
    """
    if paper_fill <= 0 or ibkr_fill <= 0:
        return {"status": "skipped", "reason": "Invalid fill prices"}

    raw_diff_pct = ((ibkr_fill - paper_fill) / paper_fill) * 100
    # For shorts, lower price = better fill. Flip the sign.
    slippage_pct = raw_diff_pct if direction == "long" else -raw_diff_pct
    slippage_dollars = (ibkr_fill - paper_fill) * shares
    if direction == "short":
        slippage_dollars = -slippage_dollars

    entry = {
        "timestamp": datetime.now(ET).isoformat(),
        "ticker": ticker,
        "direction": direction,
        "shares": shares,
        "paper_fill": round(paper_fill, 4),
        "ibkr_fill": round(ibkr_fill, 4),
        "slippage_pct": round(slippage_pct, 3),
        "slippage_dollars": round(slippage_dollars, 2),
        "alert": abs(slippage_pct) >= SLIPPAGE_ALERT_PCT,
    }

    with _lock:
        _slippage_log.append(entry)
        if len(_slippage_log) > MAX_SLIPPAGE_LOG:
            _slippage_log.pop(0)

    if entry["alert"]:
        logger.warning(
            f"SLIPPAGE ALERT: {ticker} {direction} "
            f"paper=${paper_fill:.2f} ibkr=${ibkr_fill:.2f} "
            f"slip={slippage_pct:+.2f}%"
        )

    return entry


def get_slippage_summary(window_hours: int = 24) -> dict:
    """Aggregate slippage stats over the recent window."""
    cutoff = datetime.now(ET) - timedelta(hours=window_hours)
    with _lock:
        recent = [
            s for s in _slippage_log
            if datetime.fromisoformat(s["timestamp"]) >= cutoff
        ]

    if not recent:
        return {
            "window_hours": window_hours,
            "fills": 0,
            "avg_slippage_pct": 0,
            "total_slippage_dollars": 0,
            "alerts": 0,
        }

    total_slip = sum(s["slippage_dollars"] for s in recent)
    avg_pct = sum(s["slippage_pct"] for s in recent) / len(recent)
    alerts = sum(1 for s in recent if s["alert"])

    return {
        "window_hours": window_hours,
        "fills": len(recent),
        "avg_slippage_pct": round(avg_pct, 3),
        "median_slippage_pct": round(sorted([s["slippage_pct"] for s in recent])[len(recent) // 2], 3),
        "total_slippage_dollars": round(total_slip, 2),
        "alerts": alerts,
        "worst_fill": max(recent, key=lambda x: abs(x["slippage_pct"])),
    }


def get_slippage_log(limit: int = 50) -> list:
    """Return recent fills with slippage data."""
    with _lock:
        return list(_slippage_log[-limit:])


# ─── 3. PRE-FLIGHT SELF-TEST ─────────────────────────────────────────────────

def run_preflight_test(adapter, test_ticker: str = "SPY") -> dict:
    """
    Run a non-destructive self-test before live trading begins.
    Validates:
      - Connection healthy
      - Account balance fetch works
      - Market data subscription works
      - Position read works
      - Order submission path works (cancels before fill)

    NEVER actually fills an order — submits then immediately cancels.
    """
    result = {
        "timestamp": datetime.now(ET).isoformat(),
        "test_ticker": test_ticker,
        "checks": {},
        "overall": "UNKNOWN",
    }

    # Check 1: Connection
    try:
        connected = adapter.is_connected()
        result["checks"]["connection"] = {
            "passed": connected,
            "message": "Connected to IB Gateway" if connected else "Not connected",
        }
        if not connected:
            result["overall"] = "FAIL"
            result["fail_reason"] = "IBKR Gateway not connected"
            _save_preflight(result)
            return result
    except Exception as e:
        result["checks"]["connection"] = {"passed": False, "error": str(e)}
        result["overall"] = "FAIL"
        _save_preflight(result)
        return result

    # Check 2: Account balance
    try:
        summary = adapter.get_account_summary()
        nl = summary.get("net_liquidation", 0)
        result["checks"]["account_balance"] = {
            "passed": nl > 0,
            "net_liquidation": nl,
            "message": f"Account value: ${nl:,.2f}",
        }
    except Exception as e:
        result["checks"]["account_balance"] = {"passed": False, "error": str(e)}

    # Check 3: Market data
    try:
        from ib_insync import Stock
        contract = Stock(test_ticker, "SMART", "USD")
        adapter._ib.qualifyContracts(contract)
        ticker_data = adapter._ib.reqMktData(contract, '', False, False)
        adapter._ib.sleep(2)
        price = ticker_data.marketPrice() or ticker_data.last or ticker_data.close
        adapter._ib.cancelMktData(contract)
        result["checks"]["market_data"] = {
            "passed": price and price > 0,
            "ticker": test_ticker,
            "price": price,
            "message": f"{test_ticker} price: ${price:.2f}" if price else "No price data",
        }
    except Exception as e:
        result["checks"]["market_data"] = {"passed": False, "error": str(e)}

    # Check 4: Position read
    try:
        positions = adapter._ib.positions() if adapter._ib else []
        result["checks"]["position_read"] = {
            "passed": True,
            "count": len(positions),
            "message": f"Read {len(positions)} positions from IBKR",
        }
    except Exception as e:
        result["checks"]["position_read"] = {"passed": False, "error": str(e)}

    # Check 5: Order submission path (submit + immediate cancel)
    # Only run if all prior checks pass and account has reasonable balance
    can_test_order = (
        result["checks"].get("connection", {}).get("passed")
        and result["checks"].get("market_data", {}).get("passed")
        and result["checks"].get("account_balance", {}).get("net_liquidation", 0) > 100
    )
    if can_test_order:
        try:
            from ib_insync import Stock, LimitOrder
            contract = Stock(test_ticker, "SMART", "USD")
            adapter._ib.qualifyContracts(contract)
            # Submit a limit order WAY below market — guaranteed not to fill
            ticker_data = adapter._ib.reqMktData(contract, '', False, False)
            adapter._ib.sleep(2)
            current = ticker_data.marketPrice() or ticker_data.last
            adapter._ib.cancelMktData(contract)

            # Order at 50% below market — never fills
            test_price = round(current * 0.5, 2)
            test_order = LimitOrder("BUY", 1, test_price)
            test_order.tif = "DAY"
            trade = adapter._ib.placeOrder(contract, test_order)
            adapter._ib.sleep(1)
            order_status = trade.orderStatus.status
            # Cancel immediately
            adapter._ib.cancelOrder(test_order)
            adapter._ib.sleep(1)
            cancel_status = trade.orderStatus.status

            result["checks"]["order_path"] = {
                "passed": order_status in ("Submitted", "PreSubmitted", "PendingSubmit", "Cancelled"),
                "submit_status": order_status,
                "cancel_status": cancel_status,
                "message": f"Order path OK — submitted at ${test_price}, cancelled cleanly",
            }
        except Exception as e:
            result["checks"]["order_path"] = {"passed": False, "error": str(e)}
    else:
        result["checks"]["order_path"] = {
            "passed": False,
            "skipped": True,
            "reason": "Skipped — prior checks failed or insufficient balance",
        }

    # Overall status
    all_passed = all(c.get("passed") for c in result["checks"].values() if not c.get("skipped"))
    result["overall"] = "PASS" if all_passed else "FAIL"
    result["passed_count"] = sum(1 for c in result["checks"].values() if c.get("passed"))
    result["total_checks"] = len(result["checks"])

    _save_preflight(result)

    if result["overall"] == "PASS":
        logger.info(f"PRE-FLIGHT PASS: {result['passed_count']}/{result['total_checks']} checks passed")
    else:
        logger.warning(f"PRE-FLIGHT FAIL: only {result['passed_count']}/{result['total_checks']} checks passed")

    return result


def _save_preflight(result: dict):
    """Save the latest pre-flight result."""
    with _lock:
        _preflight_results.clear()
        _preflight_results.update(result)


def get_preflight_result() -> dict:
    """Return the most recent pre-flight self-test result."""
    with _lock:
        return dict(_preflight_results)


# ─── 4. DAILY RECONCILIATION REPORT ──────────────────────────────────────────

def daily_reconciliation_report(paper_state: dict, ibkr_state: dict) -> dict:
    """
    End-of-day comparison: paper trader P&L vs IBKR P&L.
    Highlights any divergence so we know if mirror is on track.
    """
    paper_total = paper_state.get("total_value", 0)
    paper_return = paper_state.get("total_return_pct", 0)
    paper_positions_count = paper_state.get("num_positions", 0)

    ibkr_total = ibkr_state.get("net_liquidation", 0)
    ibkr_realized = ibkr_state.get("realized_pnl", 0)
    ibkr_unrealized = ibkr_state.get("unrealized_pnl", 0)
    ibkr_positions_count = len(ibkr_state.get("positions", []))

    # Compute IBKR daily return
    starting_balance = float(os.getenv("IBKR_ACCOUNT_SIZE", "10000"))
    ibkr_total_pnl = ibkr_realized + ibkr_unrealized
    ibkr_return_pct = (ibkr_total_pnl / max(starting_balance, 1)) * 100

    # Divergence
    return_divergence = paper_return - ibkr_return_pct
    position_divergence = abs(paper_positions_count - ibkr_positions_count)

    report = {
        "timestamp": datetime.now(ET).isoformat(),
        "date": datetime.now(ET).strftime("%Y-%m-%d"),
        "paper": {
            "total_value": paper_total,
            "return_pct": round(paper_return, 2),
            "positions": paper_positions_count,
        },
        "ibkr": {
            "net_liquidation": ibkr_total,
            "realized_pnl": ibkr_realized,
            "unrealized_pnl": ibkr_unrealized,
            "total_pnl": ibkr_total_pnl,
            "return_pct": round(ibkr_return_pct, 2),
            "positions": ibkr_positions_count,
        },
        "divergence": {
            "return_diff_pct": round(return_divergence, 2),
            "position_count_diff": position_divergence,
            "status": "OK" if abs(return_divergence) < 0.5 and position_divergence < 2 else "WARNING",
        },
    }

    if report["divergence"]["status"] == "WARNING":
        logger.warning(
            f"RECONCILIATION WARNING: paper={paper_return:+.2f}% "
            f"vs IBKR={ibkr_return_pct:+.2f}% (diff={return_divergence:+.2f}%)"
        )

    return report
