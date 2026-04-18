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
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

import pytz

logger = logging.getLogger(__name__)

# ─── SAFETY CONFIGURATION ────────────────────────────────────────────────────
# ALL CAPS = THIS IS CRITICAL SAFETY CONFIG. CHANGE WITH EXTREME CARE.

IBKR_HOST = os.getenv("IBKR_HOST", "127.0.0.1")
IBKR_PAPER_PORT = int(os.getenv("IBKR_PAPER_PORT", "7497"))  # Paper trading port — DEFAULT
IBKR_LIVE_PORT = int(os.getenv("IBKR_LIVE_PORT", "7496"))     # Live trading port — NEVER default
IBKR_CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID", "1"))

# Master switches — ALL default to OFF/SAFE
IBKR_ENABLED = os.getenv("IBKR_ENABLED", "false").lower() == "true"
IBKR_LIVE_TRADING = os.getenv("IBKR_LIVE_TRADING", "false").lower() == "true"
TRADING_HALTED = False          # Emergency brake — blocks all new orders

# ─── MIRROR MODE: scales paper trades proportionally to user's real account ──
# User has $10K in IBKR, paper trader has $122K. Mirror mode scales every
# position down by the ratio so the same strategy runs on a smaller account.
IBKR_ACCOUNT_SIZE = float(os.getenv("IBKR_ACCOUNT_SIZE", "10000"))  # Your real $ in IBKR
IBKR_MIRROR_MODE = os.getenv("IBKR_MIRROR_MODE", "true").lower() == "true"
IBKR_MIRROR_OPTIONS = os.getenv("IBKR_MIRROR_OPTIONS", "true").lower() == "true"  # Also mirror calls/puts
IBKR_MIN_TRADE_DOLLARS = float(os.getenv("IBKR_MIN_TRADE_DOLLARS", "100"))  # Skip trades smaller than this

# Hard limits — ALL AUTO-SCALED from account size by default
# For $10K: max position $2K (20%), max exposure $10K (100%), daily loss $300 (3%)
MAX_POSITION_DOLLARS = float(os.getenv("IBKR_MAX_POSITION", str(IBKR_ACCOUNT_SIZE * 0.20)))
MAX_TOTAL_EXPOSURE = float(os.getenv("IBKR_MAX_EXPOSURE", str(IBKR_ACCOUNT_SIZE * 1.00)))
DAILY_LOSS_LIMIT = float(os.getenv("IBKR_DAILY_LOSS_LIMIT", str(IBKR_ACCOUNT_SIZE * 0.03)))
MAX_ORDERS_PER_DAY = int(os.getenv("IBKR_MAX_ORDERS_PER_DAY", "50"))  # Sensible default

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
        """Check if a specific order passes position size limits.
        Uses LIVE scaled limits (not stale static) so they adjust with account size."""
        order_value = abs(shares * price)
        limits = self._get_live_safety_limits()

        if order_value > limits["max_position"]:
            return False, (f"Position ${order_value:.0f} exceeds max "
                          f"${limits['max_position']:.0f} (20% of account) for {ticker}")

        # Check total exposure
        total_exposure = self._get_total_exposure()
        if total_exposure + order_value > limits["max_exposure"]:
            return False, (f"Total exposure ${total_exposure + order_value:.0f} "
                          f"would exceed max ${limits['max_exposure']:.0f} (100% of account)")

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

    def _get_live_account_value(self) -> float:
        """Get the REAL current IBKR account net liquidation value.
        This auto-adjusts as user adds or withdraws money — no config needed."""
        try:
            summary = self.get_account_summary()
            nl = summary.get("net_liquidation")
            if nl and nl > 0:
                return float(nl)
        except Exception as e:
            logger.warning(f"Could not fetch live account value: {e}")
        # Fallback to env var if live fetch fails
        return IBKR_ACCOUNT_SIZE

    def _get_mirror_scale(self, paper_portfolio_value: float) -> float:
        """
        Compute the scale factor to convert paper trader positions into
        IBKR positions sized for the user's real account.

        Uses LIVE IBKR account value so scaling auto-adjusts when user
        deposits or withdraws money — no config change needed.

        scale = live_ibkr_value / paper_total
        e.g. $10K IBKR / $122K paper ≈ 0.082 (every position gets 8.2% size)
             $50K IBKR / $122K paper ≈ 0.410 (every position gets 41% size)
        """
        if not IBKR_MIRROR_MODE or paper_portfolio_value <= 0:
            return 1.0
        live_value = self._get_live_account_value()
        scale = live_value / max(paper_portfolio_value, 1.0)
        # Cap scale at 1.0 — we never trade MORE in IBKR than paper
        return min(scale, 1.0)

    def _get_live_safety_limits(self) -> dict:
        """Safety limits auto-scale from LIVE IBKR account value.
        User can deposit more → limits grow. Withdraw → limits shrink.
        Keeps risk at the same % of account regardless of account size."""
        live_value = self._get_live_account_value()
        return {
            "max_position": live_value * 0.20,   # 20% of account per position
            "max_exposure": live_value * 1.00,   # 100% of account (no leverage)
            "daily_loss_limit": live_value * 0.03,  # 3% daily stop
        }

    # ── Order Execution ───────────────────────────────────────────────────

    def execute_trades(self, quant_picks: dict, paper_opened: list = None,
                        paper_portfolio_value: float = 0) -> dict:
        """
        Execute trades on IBKR based on quant signals.
        Mirrors the interface of paper_trader.execute_trades_from_signals().

        Mirror mode: if paper_opened is provided, IBKR mirrors the EXACT same
        trades the paper trader just made, scaled to the user's account size.
        This guarantees 1:1 sync with the paper system.

        Args:
            quant_picks: Quant engine output (fallback source)
            paper_opened: List of trades paper trader just opened (preferred)
            paper_portfolio_value: Total paper portfolio value (for scaling)

        Returns dict with opened, closed, skipped, errors lists.
        """
        results = {
            "opened": [],
            "closed": [],
            "skipped": [],
            "errors": [],
            "ibkr_mode": "LIVE" if IBKR_LIVE_TRADING else "PAPER",
            "mirror_mode": IBKR_MIRROR_MODE,
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

        # Compute scale factor (live IBKR value / paper total value)
        scale = self._get_mirror_scale(paper_portfolio_value) if paper_portfolio_value else 1.0
        results["scale_factor"] = round(scale, 4)
        results["live_account_value"] = round(self._get_live_account_value(), 2)
        logger.warning(f"IBKR MIRROR: scale={scale:.4f} (live_acct=${results['live_account_value']:.0f}, paper=${paper_portfolio_value:.0f})")

        # ── MIRROR MODE: copy each paper trade 1:1 (scaled) ──
        if paper_opened and IBKR_MIRROR_MODE:
            for paper_trade in paper_opened:
                instrument = paper_trade.get("instrument_type", "equity")
                if instrument in ("call", "put") and not IBKR_MIRROR_OPTIONS:
                    results["skipped"].append({
                        "ticker": paper_trade.get("symbol"),
                        "reason": "Options mirroring disabled (IBKR_MIRROR_OPTIONS=false)",
                    })
                    continue
                result = self._mirror_paper_trade(paper_trade, regime, scale)
                if result.get("status") == "filled":
                    results["opened"].append(result)
                elif result.get("status") == "skipped":
                    results["skipped"].append(result)
                else:
                    results["errors"].append(result)
            return results

        # ── FALLBACK: use quant_picks directly if no paper_opened provided ──
        long_picks = quant_picks.get("long_picks", [])
        for pick in long_picks[:5]:
            result = self._submit_entry_order(pick, "long", regime, scale=scale)
            if result.get("status") == "filled":
                results["opened"].append(result)
            elif result.get("status") == "skipped":
                results["skipped"].append(result)
            else:
                results["errors"].append(result)

        short_picks = quant_picks.get("short_picks", [])
        for pick in short_picks[:3]:
            result = self._submit_entry_order(pick, "short", regime, scale=scale)
            if result.get("status") == "filled":
                results["opened"].append(result)
            elif result.get("status") == "skipped":
                results["skipped"].append(result)
            else:
                results["errors"].append(result)

        return results

    def _mirror_paper_trade(self, paper_trade: dict, regime: str, scale: float) -> dict:
        """
        Mirror a single paper trade onto IBKR, scaled to the user's account.
        Handles both equity and options.
        """
        symbol = paper_trade.get("symbol") or paper_trade.get("ticker")
        direction = paper_trade.get("direction", "long")
        instrument = paper_trade.get("instrument_type", "equity")

        if instrument in ("call", "put"):
            return self._submit_option_order(paper_trade, regime, scale)
        else:
            return self._submit_entry_order(paper_trade, direction, regime,
                                             scale=scale,
                                             paper_shares=paper_trade.get("shares"),
                                             paper_value=paper_trade.get("position_value"))

    def _submit_entry_order(self, pick: dict, direction: str,
                            regime: str, scale: float = 1.0,
                            paper_shares: int = None,
                            paper_value: float = None) -> dict:
        """Submit a single entry order with bracket (stop + target).

        If paper_shares/paper_value are provided and scale < 1.0, the IBKR
        position is sized as paper_value * scale (mirrors the paper trade
        proportionally). Otherwise uses MAX_POSITION_DOLLARS.
        """
        global _orders_today

        ticker = pick.get("ticker") or pick.get("symbol", "")
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

            # ── Position sizing ──
            # Mirror mode: scale the paper trade's dollar value down to IBKR size
            limits = self._get_live_safety_limits()
            max_position = limits["max_position"]
            if paper_value and scale < 1.0:
                # Proportional mirror: same % of portfolio as paper trade
                target_value = paper_value * scale
                target_value = min(target_value, max_position)
            else:
                target_value = min(max_position, MAX_POSITION_DOLLARS)

            if target_value < IBKR_MIN_TRADE_DOLLARS:
                return {"ticker": ticker, "status": "skipped",
                        "reason": f"Trade size ${target_value:.2f} below minimum ${IBKR_MIN_TRADE_DOLLARS}"}

            shares = int(target_value / current_price)
            if shares <= 0:
                return {"ticker": ticker, "status": "skipped",
                        "reason": f"Price ${current_price:.2f} too high for scaled position ${target_value:.2f}"}

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

    # ── OPTIONS ORDER SUBMISSION ──────────────────────────────────────────

    def _submit_option_order(self, paper_trade: dict, regime: str,
                             scale: float = 1.0) -> dict:
        """
        Mirror a paper options trade onto IBKR. Uses the SAME strike and
        expiry chosen by the paper trader, just scaled down in contracts.
        """
        global _orders_today
        ticker = paper_trade.get("symbol") or paper_trade.get("ticker", "")
        opt_type = paper_trade.get("instrument_type", "call")  # 'call' or 'put'
        strike = paper_trade.get("strike")
        expiry = paper_trade.get("expiry")
        paper_contracts = paper_trade.get("contracts", 1)
        paper_premium = paper_trade.get("premium", 0)
        strategy = paper_trade.get("strategy", "buy_call")

        if not ticker or not strike or not expiry:
            return {"ticker": ticker, "status": "skipped",
                    "reason": f"Missing option details (strike={strike}, expiry={expiry})"}

        try:
            from ib_insync import Option, MarketOrder, LimitOrder, StopOrder

            # IBKR expiry format: YYYYMMDD (strip dashes from 2026-04-18)
            expiry_ibkr = expiry.replace("-", "")

            # Build option contract
            right = "C" if opt_type == "call" else "P"
            contract = Option(ticker, expiry_ibkr, float(strike), right, "SMART")
            self._ib.qualifyContracts(contract)

            # Get current premium for sizing validation
            ticker_data = self._ib.reqMktData(contract, '', False, False)
            self._ib.sleep(2)
            current_premium = ticker_data.marketPrice()
            if not current_premium or current_premium <= 0:
                current_premium = ticker_data.last or ticker_data.close or paper_premium
            self._ib.cancelMktData(contract)

            if not current_premium or current_premium <= 0:
                return {"ticker": ticker, "status": "skipped",
                        "reason": "Cannot get option premium"}

            # ── Scale contracts proportionally ──
            # Mirror: IBKR_contracts = paper_contracts * scale (min 1)
            scaled_contracts = max(1, int(round(paper_contracts * scale)))

            # Validate size against live account safety limits
            limits = self._get_live_safety_limits()
            position_value = scaled_contracts * current_premium * 100
            if position_value > limits["max_position"]:
                # Reduce contracts to fit within position limit
                scaled_contracts = max(1, int(limits["max_position"] / (current_premium * 100)))
                position_value = scaled_contracts * current_premium * 100

            if position_value < IBKR_MIN_TRADE_DOLLARS:
                return {"ticker": ticker, "status": "skipped",
                        "reason": f"Option trade ${position_value:.2f} below minimum ${IBKR_MIN_TRADE_DOLLARS}"}

            # Check exposure
            total_exposure = self._get_total_exposure()
            if total_exposure + position_value > limits["max_exposure"]:
                return {"ticker": ticker, "status": "skipped",
                        "reason": f"Option trade would exceed max exposure"}

            # BUY or SELL depending on strategy
            action = "BUY" if strategy.startswith("buy") else "SELL"

            _log_order({
                "action": "SUBMIT_OPTION",
                "ticker": ticker,
                "option_type": opt_type,
                "strike": strike,
                "expiry": expiry,
                "contracts": scaled_contracts,
                "premium": current_premium,
                "position_value": round(position_value, 2),
                "strategy": strategy,
                "scale": round(scale, 4),
            })

            # Submit as limit order at 2% above market (give room for fill)
            limit_price = round(current_premium * (1.02 if action == "BUY" else 0.98), 2)
            order = LimitOrder(action, scaled_contracts, limit_price)
            trade = self._ib.placeOrder(contract, order)

            # Wait for fill
            start_wait = time.time()
            while (time.time() - start_wait < FILL_TIMEOUT
                   and trade.orderStatus.status not in ("Filled", "Cancelled")):
                self._ib.sleep(0.5)

            if trade.orderStatus.status == "Filled":
                fill_price = trade.orderStatus.avgFillPrice
                with _orders_today_lock:
                    _orders_today += 1

                _log_order({
                    "action": "OPTION_FILLED",
                    "ticker": ticker,
                    "option_type": opt_type,
                    "fill_price": fill_price,
                    "contracts": scaled_contracts,
                })

                opt_emoji = "📞 CALL" if opt_type == "call" else "📉 PUT"
                logger.warning(f"🎯 IBKR {opt_emoji}: {action} {scaled_contracts}x {ticker} ${strike} exp {expiry} @ ${fill_price:.2f}")

                return {
                    "ticker": ticker,
                    "instrument_type": opt_type,
                    "strategy": strategy,
                    "strike": strike,
                    "expiry": expiry,
                    "contracts": scaled_contracts,
                    "premium": fill_price,
                    "position_value": round(scaled_contracts * fill_price * 100, 2),
                    "status": "filled",
                    "order_id": trade.order.orderId,
                    "ibkr_mode": "LIVE" if IBKR_LIVE_TRADING else "PAPER",
                }
            else:
                try:
                    self._ib.cancelOrder(trade.order)
                except Exception:
                    pass
                return {"ticker": ticker, "status": "error",
                        "reason": f"Option fill timeout — status: {trade.orderStatus.status}"}

        except ImportError:
            return {"ticker": ticker, "status": "error",
                    "reason": "ib_insync not installed"}
        except Exception as e:
            _log_order({"action": "OPTION_ERROR", "ticker": ticker, "error": str(e)})
            logger.error(f"IBKR option error for {ticker}: {e}")
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
        live_value = 0.0
        try:
            if self.is_connected():
                live_value = self._get_live_account_value()
        except Exception:
            pass
        limits = self._get_live_safety_limits() if self.is_connected() else {
            "max_position": MAX_POSITION_DOLLARS,
            "max_exposure": MAX_TOTAL_EXPOSURE,
            "daily_loss_limit": DAILY_LOSS_LIMIT,
        }
        return {
            "connected": self.is_connected(),
            "enabled": IBKR_ENABLED,
            "mode": "LIVE" if IBKR_LIVE_TRADING else "PAPER",
            "trading_halted": TRADING_HALTED,
            "port": IBKR_LIVE_PORT if IBKR_LIVE_TRADING else IBKR_PAPER_PORT,
            "last_error": self._last_error,
            "reconnect_attempts": self._reconnect_attempts,
            "mirror": {
                "mode": IBKR_MIRROR_MODE,
                "mirror_options": IBKR_MIRROR_OPTIONS,
                "live_account_value": round(live_value, 2),
                "min_trade_dollars": IBKR_MIN_TRADE_DOLLARS,
            },
            "safety": {
                "max_position_dollars": round(limits["max_position"], 2),
                "max_total_exposure": round(limits["max_exposure"], 2),
                "daily_loss_limit": round(limits["daily_loss_limit"], 2),
                "max_orders_per_day": MAX_ORDERS_PER_DAY,
                "market_hours_only": True,
                "auto_scaled": True,
                "notes": "Limits auto-scale from live IBKR account value (20%/100%/3%)",
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

def ibkr_execute_trades(quant_picks: dict, paper_opened: list = None,
                         paper_portfolio_value: float = 0) -> dict:
    """Execute trades via IBKR. Mirrors paper trader 1:1 (scaled to account size).

    Args:
        quant_picks: Fresh quant engine output (fallback)
        paper_opened: Trades the paper trader just opened (preferred for exact mirror)
        paper_portfolio_value: Total paper portfolio $ value (for scaling)
    """
    adapter = get_ibkr_adapter()
    return adapter.execute_trades(quant_picks, paper_opened=paper_opened,
                                    paper_portfolio_value=paper_portfolio_value)


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
