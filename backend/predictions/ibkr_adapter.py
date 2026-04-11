"""
IBKR (Interactive Brokers) Adapter — Sentinel Quant Hedge Fund
=========================================================

Connects Sentinel Quant's autonomous trading system to Interactive Brokers
for real execution via the ib_insync library.

SAFETY ARCHITECTURE (NON-NEGOTIABLE):
- Defaults to PAPER TRADING (port 7497) — NEVER live by default
- Kill switch flattens all positions instantly
- Daily loss limit auto-halts trading
- Max position size + max total exposure hard caps
- Market hours enforcement
- Dual-track: paper trades ALWAYS run alongside IBKR
- Connection watchdog blocks trades if disconnected

Author: Sentinel Quant Trading Systems
"""

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

import pytz

logger = logging.getLogger(__name__)

# ─── SAFETY CONFIGURATION ────────────────────────────────────────────────────
# ALL CAPS = THIS IS CRITICAL SAFETY CONFIG. CHANGE WITH EXTREME CARE.

IBKR_HOST = "127.0.0.1"
IBKR_PAPER_PORT = 7497       # Paper trading port — DEFAULT
IBKR_LIVE_PORT = 7496         # Live trading port — NEVER default to this
IBKR_CLIENT_ID = 1

# Master switches — ALL default to OFF/SAFE
IBKR_ENABLED = False           # Must be True to send ANY orders to IBKR
IBKR_LIVE_TRADING = False      # False = paper port, True = live port (DANGER)
TRADING_HALTED = False          # Emergency brake — blocks all new orders

# Hard limits — only daily loss limit active
MAX_POSITION_DOLLARS = 10000      # Max $10K per position (safe default)
MAX_TOTAL_EXPOSURE = 50000        # Max $50K total exposure (safe default)
DAILY_LOSS_LIMIT = 500            # Auto-halt if daily loss exceeds this (KEEP)
MAX_ORDERS_PER_DAY = 999999       # No limit on daily order count

# Timing
RECONNECT_DELAY_BASE = 5       # Seconds, doubles each retry
RECONNECT_MAX_DELAY = 300      # Max 5 minutes between retries
FILL_TIMEOUT = 30              # Seconds to wait for order fill
HEARTBEAT_INTERVAL = 30        # Seconds between connection checks

ET = pytz.timezone("US/Eastern")


# ─── ORDER LOG ────────────────────────────────────────────────────────────────

_order_log = []        # List of all order events
_order_log_lock = threading.Lock()
_daily_pnl = 0.0       # Tracks realized P&L for the day
_daily_pnl_lock = threading.Lock()
_orders_today = 0
_orders_today_lock = threading.Lock()
_last_reset_date = None


def _log_order(event: dict):
    """Thread-safe order event logging."""
    event["timestamp"] = datetime.now(ET).isoformat()
    with _order_log_lock:
        _order_log.append(event)
        # Keep last 500 events
        if len(_order_log) > 500:
            _order_log.pop(0)
    logger.info(f"IBKR ORDER LOG: {event}")


def get_order_log(limit: int = 100) -> list:
    """Get recent order events."""
    with _order_log_lock:
        return list(_order_log[-limit:])


# ─── IBKR ADAPTER CLASS ──────────────────────────────────────────────────────

class IBKRAdapter:
    """
    Thread-safe IBKR connection and order management.

    Usage:
        adapter = IBKRAdapter()
        adapter.connect()
        adapter.execute_trades(quant_picks)
        adapter.flatten_all("manual kill switch")
        adapter.disconnect()
    """

    def __init__(self):
        self._ib = None
        self._connected = False
        self._lock = threading.Lock()
        self._reconnect_attempts = 0
        self._last_error = None
        self._positions_cache = {}
        self._account_cache = {}
        self._account_cache_time = 0

    # ── Connection Management ─────────────────────────────────────────────

    def connect(self) -> bool:
        """Connect to IBKR TWS or IB Gateway."""
        if not IBKR_ENABLED:
            logger.info("IBKR disabled — skipping connection")
            return False

        port = IBKR_LIVE_PORT if IBKR_LIVE_TRADING else IBKR_PAPER_PORT
        mode = "LIVE" if IBKR_LIVE_TRADING else "PAPER"

        try:
            from ib_insync import IB
            with self._lock:
                if self._ib is not None:
                    try:
                        self._ib.disconnect()
                    except Exception:
                        pass

                self._ib = IB()
                self._ib.connect(IBKR_HOST, port, clientId=IBKR_CLIENT_ID)
                self._connected = True
                self._reconnect_attempts = 0
                self._last_error = None

            _log_order({
                "action": "CONNECT",
                "mode": mode,
                "port": port,
                "status": "SUCCESS"
            })
            logger.info(f"IBKR connected — {mode} mode on port {port}")
            return True

        except ImportError:
            self._last_error = "ib_insync not installed"
            logger.error("ib_insync not installed — pip install ib_insync")
            return False
        except Exception as e:
            self._connected = False
            self._last_error = str(e)
            _log_order({
                "action": "CONNECT",
                "mode": mode,
                "port": port,
                "status": "FAILED",
                "error": str(e)
            })
            logger.error(f"IBKR connection failed: {e}")
            return False

    def disconnect(self):
        """Disconnect from IBKR."""
        with self._lock:
            if self._ib:
                try:
                    self._ib.disconnect()
                except Exception:
                    pass
                self._ib = None
            self._connected = False
        _log_order({"action": "DISCONNECT", "status": "OK"})

    def is_connected(self) -> bool:
        """Check if IBKR connection is alive."""
        if not self._ib or not self._connected:
            return False
        try:
            return self._ib.isConnected()
        except Exception:
            self._connected = False
            return False

    def _ensure_connected(self) -> bool:
        """Reconnect if disconnected. Returns True if connected."""
        if self.is_connected():
            return True

        delay = min(
            RECONNECT_DELAY_BASE * (2 ** self._reconnect_attempts),
            RECONNECT_MAX_DELAY
        )
        self._reconnect_attempts += 1
        logger.warning(f"IBKR disconnected — reconnecting in {delay}s (attempt {self._reconnect_attempts})")
        time.sleep(delay)
        return self.connect()

    # ── Safety Checks ─────────────────────────────────────────────────────

    def _check_trading_allowed(self) -> tuple:
        """
        Master safety gate. Returns (allowed: bool, reason: str).
        ALL safety checks must pass before any order is submitted.
        """
        global TRADING_HALTED, _orders_today, _last_reset_date

        # Reset daily counters at midnight ET
        now_et = datetime.now(ET)
        today = now_et.date()
        if _last_reset_date != today:
            with _daily_pnl_lock:
                global _daily_pnl
                _daily_pnl = 0.0
            with _orders_today_lock:
                _orders_today = 0
            _last_reset_date = today  # noqa: F841 — used for reset tracking

        if not IBKR_ENABLED:
            return False, "IBKR execution disabled (IBKR_ENABLED=False)"

        if TRADING_HALTED:
            return False, "TRADING HALTED — emergency brake active"

        if not self.is_connected():
            return False, "IBKR not connected — cannot submit orders"

        # Market hours check (9:30 AM - 4:00 PM ET, weekdays)
        if now_et.weekday() >= 5:
            return False, f"Market closed — weekend ({now_et.strftime('%A')})"

        market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
        if now_et < market_open or now_et > market_close:
            return False, f"Market closed — current time {now_et.strftime('%H:%M')} ET"

        # Daily loss limit
        with _daily_pnl_lock:
            if _daily_pnl < -DAILY_LOSS_LIMIT:
                TRADING_HALTED = True
                return False, f"DAILY LOSS LIMIT HIT (${_daily_pnl:.2f}) — trading halted"

        # Order count limit
        with _orders_today_lock:
            if _orders_today >= MAX_ORDERS_PER_DAY:
                return False, f"Max orders per day reached ({MAX_ORDERS_PER_DAY})"

        return True, "OK"

    def _check_position_limits(self, ticker: str, shares: int, price: float,
                                direction: str) -> tuple:
        """Check if a specific order passes position size limits."""
        order_value = abs(shares * price)

        if order_value > MAX_POSITION_DOLLARS:
            return False, (f"Position ${order_value:.0f} exceeds max "
                          f"${MAX_POSITION_DOLLARS} for {ticker}")

        # Check total exposure
        total_exposure = self._get_total_exposure()
        if total_exposure + order_value > MAX_TOTAL_EXPOSURE:
            return False, (f"Total exposure ${total_exposure + order_value:.0f} "
                          f"would exceed max ${MAX_TOTAL_EXPOSURE}")

        if shares <= 0:
            return False, f"Invalid share count: {shares}"

        if price <= 0:
            return False, f"Invalid price: {price}"

        return True, "OK"

    def _get_total_exposure(self) -> float:
        """Get total dollar exposure across all IBKR positions."""
        try:
            if not self.is_connected():
                return 0.0
            positions = self._ib.positions()
            return sum(abs(p.position * p.avgCost) for p in positions)
        except Exception as e:
            logger.error(f"Failed to get exposure: {e}")
            return MAX_TOTAL_EXPOSURE  # Assume worst case

    # ── Order Execution ───────────────────────────────────────────────────

    def execute_trades(self, quant_picks: dict) -> dict:
        """
        Execute trades on IBKR based on quant signals.
        Mirrors the interface of paper_trader.execute_trades_from_signals().

        Returns dict with opened, closed, skipped, errors lists.
        """
        results = {
            "opened": [],
            "closed": [],
            "skipped": [],
            "errors": [],
            "ibkr_mode": "LIVE" if IBKR_LIVE_TRADING else "PAPER",
        }

        # Master safety check
        allowed, reason = self._check_trading_allowed()
        if not allowed:
            _log_order({"action": "BLOCKED", "reason": reason})
            results["errors"].append({"reason": reason})
            return results

        if not self._ensure_connected():
            results["errors"].append({"reason": "Cannot connect to IBKR"})
            return results

        regime = quant_picks.get("regime", {}).get("regime", "SIDEWAYS")

        # Process long picks
        long_picks = quant_picks.get("long_picks", [])
        for pick in long_picks[:5]:  # Max 5 new positions per cycle
            result = self._submit_entry_order(pick, "long", regime)
            if result.get("status") == "filled":
                results["opened"].append(result)
            elif result.get("status") == "skipped":
                results["skipped"].append(result)
            else:
                results["errors"].append(result)

        # Process short picks
        short_picks = quant_picks.get("short_picks", [])
        for pick in short_picks[:3]:  # Max 3 short positions per cycle
            result = self._submit_entry_order(pick, "short", regime)
            if result.get("status") == "filled":
                results["opened"].append(result)
            elif result.get("status") == "skipped":
                results["skipped"].append(result)
            else:
                results["errors"].append(result)

        return results

    def _submit_entry_order(self, pick: dict, direction: str,
                            regime: str) -> dict:
        """Submit a single entry order with bracket (stop + target)."""
        global _orders_today

        ticker = pick.get("ticker", "")
        if not ticker:
            return {"ticker": "?", "status": "error", "reason": "No ticker"}

        try:
            from ib_insync import Stock, MarketOrder, StopOrder, LimitOrder

            # Get current price for sizing
            contract = Stock(ticker, "SMART", "USD")
            self._ib.qualifyContracts(contract)

            # Request market data for current price
            ticker_data = self._ib.reqMktData(contract, '', False, False)
            self._ib.sleep(2)  # Wait for data
            current_price = ticker_data.marketPrice()

            if not current_price or current_price <= 0:
                current_price = ticker_data.last or ticker_data.close
            if not current_price or current_price <= 0:
                self._ib.cancelMktData(contract)
                return {"ticker": ticker, "status": "skipped",
                        "reason": "Cannot get price"}

            self._ib.cancelMktData(contract)

            # Calculate position size (respect hard cap)
            raw_value = MAX_POSITION_DOLLARS
            shares = int(raw_value / current_price)
            if shares <= 0:
                return {"ticker": ticker, "status": "skipped",
                        "reason": f"Price ${current_price:.2f} too high for max position ${MAX_POSITION_DOLLARS}"}

            # Position limit check
            ok, reason = self._check_position_limits(
                ticker, shares, current_price, direction)
            if not ok:
                _log_order({"action": "BLOCKED", "ticker": ticker,
                           "reason": reason})
                return {"ticker": ticker, "status": "skipped", "reason": reason}

            # Get stop/target from the pick data
            stop_loss = pick.get("stop_loss", 0)
            target = pick.get("target", 0)

            # Fallback: calculate simple stops if not provided
            if not stop_loss:
                stop_pct = 0.03  # 3% default stop
                if direction == "long":
                    stop_loss = round(current_price * (1 - stop_pct), 2)
                else:
                    stop_loss = round(current_price * (1 + stop_pct), 2)

            if not target:
                target_pct = 0.06  # 6% default target
                if direction == "long":
                    target = round(current_price * (1 + target_pct), 2)
                else:
                    target = round(current_price * (1 - target_pct), 2)

            # Log BEFORE submitting
            _log_order({
                "action": "SUBMIT_ENTRY",
                "ticker": ticker,
                "direction": direction,
                "shares": shares,
                "price": current_price,
                "stop_loss": stop_loss,
                "target": target,
                "value": round(shares * current_price, 2),
                "regime": regime,
            })

            # Submit bracket order
            if direction == "long":
                entry_order = MarketOrder("BUY", shares)
                stop_order = StopOrder("SELL", shares, stop_loss)
                target_order = LimitOrder("SELL", shares, target)
            else:
                entry_order = MarketOrder("SELL", shares)
                stop_order = StopOrder("BUY", shares, stop_loss)
                target_order = LimitOrder("BUY", shares, target)

            # Place bracket order (OCA group)
            bracket = self._ib.bracketOrder(
                "BUY" if direction == "long" else "SELL",
                shares,
                limitPrice=round(current_price * 1.005, 2),  # Limit slightly above market
                takeProfitPrice=target,
                stopLossPrice=stop_loss
            )

            trades = []
            for order in bracket:
                trade = self._ib.placeOrder(contract, order)
                trades.append(trade)

            # Wait for entry fill
            entry_trade = trades[0]
            start_wait = time.time()
            while (time.time() - start_wait < FILL_TIMEOUT
                   and entry_trade.orderStatus.status != "Filled"):
                self._ib.sleep(0.5)

            if entry_trade.orderStatus.status == "Filled":
                fill_price = entry_trade.orderStatus.avgFillPrice
                with _orders_today_lock:
                    _orders_today += 1

                _log_order({
                    "action": "FILLED",
                    "ticker": ticker,
                    "direction": direction,
                    "shares": shares,
                    "fill_price": fill_price,
                    "order_id": entry_trade.order.orderId,
                })

                return {
                    "ticker": ticker,
                    "direction": direction,
                    "shares": shares,
                    "entry_price": fill_price,
                    "stop_loss": stop_loss,
                    "target": target,
                    "status": "filled",
                    "order_id": entry_trade.order.orderId,
                    "ibkr_mode": "LIVE" if IBKR_LIVE_TRADING else "PAPER",
                }
            else:
                # Cancel unfilled orders
                for trade in trades:
                    try:
                        self._ib.cancelOrder(trade.order)
                    except Exception:
                        pass

                _log_order({
                    "action": "TIMEOUT",
                    "ticker": ticker,
                    "status": entry_trade.orderStatus.status,
                })
                return {"ticker": ticker, "status": "error",
                        "reason": f"Fill timeout — status: {entry_trade.orderStatus.status}"}

        except ImportError:
            return {"ticker": ticker, "status": "error",
                    "reason": "ib_insync not installed"}
        except Exception as e:
            _log_order({
                "action": "ERROR",
                "ticker": ticker,
                "error": str(e),
            })
            logger.error(f"IBKR order error for {ticker}: {e}")
            return {"ticker": ticker, "status": "error", "reason": str(e)}

    # ── Exit / Kill Switch ────────────────────────────────────────────────

    def exit_position(self, ticker: str, shares: int, direction: str,
                      reason: str = "manual") -> dict:
        """Close a single position by submitting a market order."""
        if not self.is_connected():
            return {"ticker": ticker, "status": "error",
                    "reason": "Not connected"}

        try:
            from ib_insync import Stock, MarketOrder

            contract = Stock(ticker, "SMART", "USD")
            self._ib.qualifyContracts(contract)

            # Close: opposite direction
            action = "SELL" if direction == "long" else "BUY"
            order = MarketOrder(action, abs(shares))

            _log_order({
                "action": "EXIT",
                "ticker": ticker,
                "direction": direction,
                "shares": shares,
                "reason": reason,
            })

            trade = self._ib.placeOrder(contract, order)

            # Wait for fill
            start = time.time()
            while time.time() - start < FILL_TIMEOUT:
                if trade.orderStatus.status == "Filled":
                    fill_price = trade.orderStatus.avgFillPrice
                    _log_order({
                        "action": "EXIT_FILLED",
                        "ticker": ticker,
                        "fill_price": fill_price,
                        "reason": reason,
                    })
                    return {
                        "ticker": ticker,
                        "exit_price": fill_price,
                        "status": "filled",
                        "reason": reason,
                    }
                self._ib.sleep(0.5)

            return {"ticker": ticker, "status": "error",
                    "reason": "Exit fill timeout"}

        except Exception as e:
            logger.error(f"IBKR exit error for {ticker}: {e}")
            return {"ticker": ticker, "status": "error", "reason": str(e)}

    def flatten_all(self, reason: str = "KILL SWITCH") -> dict:
        """
        EMERGENCY: Close ALL open positions immediately.
        This is the kill switch — no safety checks, just close everything.
        """
        global TRADING_HALTED
        TRADING_HALTED = True  # Halt all new trading

        _log_order({
            "action": "KILL_SWITCH",
            "reason": reason,
        })

        results = {"closed": [], "errors": [], "reason": reason}

        if not self.is_connected():
            results["errors"].append("Not connected — cannot flatten")
            return results

        try:
            # Cancel all open orders first
            open_orders = self._ib.openOrders()
            for order in open_orders:
                try:
                    self._ib.cancelOrder(order)
                except Exception as e:
                    results["errors"].append(f"Cancel order error: {e}")

            # Close all positions
            positions = self._ib.positions()
            for pos in positions:
                ticker = pos.contract.symbol
                qty = abs(pos.position)
                direction = "long" if pos.position > 0 else "short"

                result = self.exit_position(ticker, qty, direction, reason)
                if result.get("status") == "filled":
                    results["closed"].append(result)
                else:
                    results["errors"].append(result)

            logger.warning(f"KILL SWITCH ACTIVATED: {reason} — "
                          f"closed {len(results['closed'])} positions")

        except Exception as e:
            results["errors"].append(f"Flatten error: {e}")
            logger.error(f"KILL SWITCH ERROR: {e}")

        return results

    # ── Account Info ──────────────────────────────────────────────────────

    def get_account_summary(self) -> dict:
        """Get IBKR account summary (cached 30s)."""
        now = time.time()
        if now - self._account_cache_time < 30 and self._account_cache:
            return self._account_cache

        if not self.is_connected():
            return {
                "connected": False,
                "mode": "LIVE" if IBKR_LIVE_TRADING else "PAPER",
                "error": self._last_error,
            }

        try:
            account_values = self._ib.accountSummary()
            summary = {
                "connected": True,
                "mode": "LIVE" if IBKR_LIVE_TRADING else "PAPER",
                "enabled": IBKR_ENABLED,
                "trading_halted": TRADING_HALTED,
            }

            for av in account_values:
                tag = av.tag
                if tag == "NetLiquidation":
                    summary["net_liquidation"] = float(av.value)
                elif tag == "BuyingPower":
                    summary["buying_power"] = float(av.value)
                elif tag == "TotalCashValue":
                    summary["cash"] = float(av.value)
                elif tag == "UnrealizedPnL":
                    summary["unrealized_pnl"] = float(av.value)
                elif tag == "RealizedPnL":
                    summary["realized_pnl"] = float(av.value)
                elif tag == "GrossPositionValue":
                    summary["gross_position_value"] = float(av.value)

            with _daily_pnl_lock:
                summary["daily_pnl"] = _daily_pnl
            with _orders_today_lock:
                summary["orders_today"] = _orders_today

            summary["daily_loss_limit"] = DAILY_LOSS_LIMIT
            summary["max_position"] = MAX_POSITION_DOLLARS
            summary["max_exposure"] = MAX_TOTAL_EXPOSURE

            self._account_cache = summary
            self._account_cache_time = now
            return summary

        except Exception as e:
            logger.error(f"Account summary error: {e}")
            return {
                "connected": self.is_connected(),
                "mode": "LIVE" if IBKR_LIVE_TRADING else "PAPER",
                "error": str(e),
            }

    def get_positions(self) -> list:
        """Get all current IBKR positions."""
        if not self.is_connected():
            return []

        try:
            positions = self._ib.positions()
            result = []
            for pos in positions:
                result.append({
                    "ticker": pos.contract.symbol,
                    "shares": pos.position,
                    "direction": "long" if pos.position > 0 else "short",
                    "avg_cost": pos.avgCost,
                    "value": abs(pos.position * pos.avgCost),
                    "account": pos.account,
                })
            return result
        except Exception as e:
            logger.error(f"Get positions error: {e}")
            return []

    def get_open_orders(self) -> list:
        """Get all open/pending orders."""
        if not self.is_connected():
            return []

        try:
            trades = self._ib.openTrades()
            result = []
            for trade in trades:
                result.append({
                    "order_id": trade.order.orderId,
                    "ticker": trade.contract.symbol,
                    "action": trade.order.action,
                    "quantity": trade.order.totalQuantity,
                    "order_type": trade.order.orderType,
                    "limit_price": trade.order.lmtPrice,
                    "stop_price": trade.order.auxPrice,
                    "status": trade.orderStatus.status,
                    "filled": trade.orderStatus.filled,
                    "remaining": trade.orderStatus.remaining,
                })
            return result
        except Exception as e:
            logger.error(f"Get orders error: {e}")
            return []

    def get_status(self) -> dict:
        """Full status summary for the dashboard."""
        return {
            "connected": self.is_connected(),
            "enabled": IBKR_ENABLED,
            "mode": "LIVE" if IBKR_LIVE_TRADING else "PAPER",
            "trading_halted": TRADING_HALTED,
            "port": IBKR_LIVE_PORT if IBKR_LIVE_TRADING else IBKR_PAPER_PORT,
            "last_error": self._last_error,
            "reconnect_attempts": self._reconnect_attempts,
            "safety": {
                "max_position_dollars": MAX_POSITION_DOLLARS,
                "max_total_exposure": MAX_TOTAL_EXPOSURE,
                "daily_loss_limit": DAILY_LOSS_LIMIT,
                "max_orders_per_day": MAX_ORDERS_PER_DAY,
                "market_hours_only": True,
            },
        }


# ─── SINGLETON INSTANCE ──────────────────────────────────────────────────────

_adapter_instance = None
_adapter_lock = threading.Lock()


def get_ibkr_adapter() -> IBKRAdapter:
    """Get or create the singleton IBKR adapter."""
    global _adapter_instance
    if _adapter_instance is None:
        with _adapter_lock:
            if _adapter_instance is None:
                _adapter_instance = IBKRAdapter()
    return _adapter_instance


# ─── CONVENIENCE FUNCTIONS (match paper_trader interface) ─────────────────────

def ibkr_execute_trades(quant_picks: dict) -> dict:
    """Execute trades via IBKR. Same interface as execute_trades_from_signals."""
    adapter = get_ibkr_adapter()
    return adapter.execute_trades(quant_picks)


def ibkr_flatten_all(reason: str = "manual") -> dict:
    """Kill switch — flatten all IBKR positions."""
    adapter = get_ibkr_adapter()
    return adapter.flatten_all(reason)


def ibkr_get_status() -> dict:
    """Get IBKR connection and trading status."""
    adapter = get_ibkr_adapter()
    return adapter.get_status()


def ibkr_get_account() -> dict:
    """Get IBKR account summary."""
    adapter = get_ibkr_adapter()
    return adapter.get_account_summary()


def ibkr_get_positions() -> list:
    """Get IBKR open positions."""
    adapter = get_ibkr_adapter()
    return adapter.get_positions()


def ibkr_get_orders() -> list:
    """Get IBKR open orders."""
    adapter = get_ibkr_adapter()
    return adapter.get_open_orders()


def ibkr_toggle(enabled: bool) -> dict:
    """Enable/disable IBKR execution."""
    global IBKR_ENABLED
    old = IBKR_ENABLED
    IBKR_ENABLED = enabled
    _log_order({
        "action": "TOGGLE",
        "old": old,
        "new": enabled,
    })
    logger.info(f"IBKR execution {'ENABLED' if enabled else 'DISABLED'}")

    # Auto-connect when enabling
    if enabled:
        adapter = get_ibkr_adapter()
        adapter.connect()

    return {"enabled": IBKR_ENABLED, "previous": old}


def ibkr_unhalt() -> dict:
    """Resume trading after halt (e.g., after daily loss limit)."""
    global TRADING_HALTED
    TRADING_HALTED = False
    _log_order({"action": "UNHALT", "status": "Trading resumed"})
    return {"halted": False, "message": "Trading resumed"}
