"""
Paper Trading System — the execution engine of the Epic Fury hedge fund.

This manages a $109K virtual portfolio that:
  1. Auto-executes trades based on quant signals (LONG/SHORT picks)
  2. Manages position sizing (uncapped — constrained by cash)
  3. Enforces risk management (6% stop-loss, target exits, time exits)
  4. Runs rapid backtesting using historical data to learn quickly
  5. Tracks portfolio performance vs S&P 500 benchmark

The goal: execute thousands of trades, track every outcome,
and feed results to the learning system so it can improve.

No real money is used. This is a paper trading simulator.
"""

import yfinance as yf
import numpy as np
import pandas as pd
import time
import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def _safe_col(df, col_name):
    """Extract a column from yfinance DataFrame, handling multi-level columns."""
    if df is None or df.empty:
        return pd.Series(dtype=float)
    c = df[col_name]
    if hasattr(c, "columns"):
        c = c.iloc[:, 0]
    return c


# Portfolio configuration
ORIGINAL_CAPITAL = 100_000.0  # What we ACTUALLY started with — used for total return calculation
INITIAL_CAPITAL = 109_000.0   # Current capital level — used for cash init if no S3 restore
MAX_POSITIONS = 999  # No limit — only constrained by cash
POSITION_SIZE_PCT = 0.06  # 6% per position — conviction sizing like real hedge funds
STOP_LOSS_PCT = 0.05  # Default fallback — overridden by per-stock ATR calculation
DEFAULT_HOLD_DAYS = 30
MIN_CONFIDENCE = 30  # Low bar — the QUANT SCORE is the real gatekeeper, not confidence


# ============================================================
#  AUTONOMOUS DECISION ENGINE — Per-Stock Stop Loss & Profit Targets
# ============================================================
# Instead of fixed 5% stop loss for every stock, the machine calculates
# the right stop loss for each stock based on:
#   1. ATR (Average True Range) — how much it ACTUALLY moves day-to-day
#   2. Signal strength — high confidence = wider stops (more room to breathe)
#   3. Regime — BEAR = tighter stops, BULL = wider stops
#   4. Stock type — volatile growth stock vs stable dividend stock
#
# This is how real quant funds do it. A 5% stop on TSLA (which moves 5%
# in a single day) is meaningless. But 5% on KO (which barely moves 1%)
# is way too wide. The machine must decide per-stock.

def _calculate_stock_atr(symbol: str, period: int = 14) -> float:
    """
    Calculate Average True Range as a percentage of price.
    ATR measures how much a stock typically moves per day.
    Returns ATR as a decimal (e.g., 0.03 = 3% daily movement).
    """
    try:
        _throttle()
        df = yf.download(symbol, period="2mo", progress=False)
        if df is None or len(df) < period + 1:
            return 0.025  # Default 2.5% if no data

        highs = _safe_col(df, "High").values.astype(float)
        lows = _safe_col(df, "Low").values.astype(float)
        closes = _safe_col(df, "Close").values.astype(float)

        # True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|)
        tr_values = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            tr_values.append(tr)

        # ATR = rolling average of True Range
        atr = float(np.mean(tr_values[-period:]))
        atr_pct = atr / closes[-1]  # Convert to percentage of current price

        return max(0.005, min(atr_pct, 0.15))  # Clamp between 0.5% and 15%
    except Exception as e:
        logger.debug(f"ATR calculation failed for {symbol}: {e}")
        return 0.025  # Default fallback


def _autonomous_stop_and_target(
    symbol: str,
    direction: str,
    price: float,
    regime: str,
    confidence: int,
    composite_score: float,
    sector: str = "",
    is_mean_reversion: bool = False,
) -> dict:
    """
    THE MACHINE DECIDES — Per-stock stop loss and profit target.

    Uses ATR (volatility), signal strength, regime, and trade type to
    calculate the optimal stop loss and take-profit for each individual stock.

    Returns dict with: stop_loss, target_price, hold_days, reasoning
    """
    atr_pct = _calculate_stock_atr(symbol)

    # --- STOP LOSS: Based on ATR ---
    # Standard: 2x ATR gives ~95% chance of NOT being stopped by noise
    # High conviction: 2.5x ATR (more room)
    # Low conviction: 1.5x ATR (tighter, less risk)
    if confidence >= 80 and abs(composite_score) >= 5:
        atr_mult_stop = 2.5  # High conviction = more breathing room
    elif confidence >= 60:
        atr_mult_stop = 2.0  # Standard
    else:
        atr_mult_stop = 1.5  # Low conviction = tight stop, less risk

    # Regime adjustment
    if regime == "BEAR":
        if direction == "long":
            atr_mult_stop *= 0.8  # Tighter stops on longs in bear
        else:
            atr_mult_stop *= 1.1  # Shorts get a bit more room in bear
    elif regime == "BULL":
        if direction == "long":
            atr_mult_stop *= 1.2  # More room for longs in bull
        else:
            atr_mult_stop *= 0.8  # Tighter on shorts in bull

    # Mean reversion trades are quick — tighter stops
    if is_mean_reversion:
        atr_mult_stop *= 0.7

    stop_pct = atr_pct * atr_mult_stop

    # Hard clamp: never risk more than 10% or less than 2%
    stop_pct = max(0.02, min(stop_pct, 0.10))

    # --- TAKE PROFIT: Risk-Reward Ratio ---
    # Target at least 2:1 reward-to-risk (RenTech standard)
    # High conviction: 3:1, Low conviction: 2:1
    if confidence >= 80 and abs(composite_score) >= 5:
        rr_ratio = 3.0  # High conviction = aim for 3x the risk
    elif confidence >= 60:
        rr_ratio = 2.5
    else:
        rr_ratio = 2.0

    # Mean reversion: quick profits, lower ratio
    if is_mean_reversion:
        rr_ratio = 1.5  # MR trades are high win-rate, lower payoff

    target_pct = stop_pct * rr_ratio

    # Hard clamp: target between 3% and 30%
    target_pct = max(0.03, min(target_pct, 0.30))

    # --- HOLD DURATION: Based on ATR and signal ---
    # High volatility stocks resolve faster (shorter hold)
    # Low volatility stocks need more time (longer hold)
    if is_mean_reversion:
        hold_days = max(1, min(5, int(5 / (atr_pct * 40 + 0.5))))  # 1-5 days
    elif regime == "BEAR":
        base_hold = 7 if confidence >= 70 else 3
        hold_days = max(2, min(14, base_hold))
    elif regime == "BULL":
        base_hold = 30 if confidence >= 70 else 14
        hold_days = max(7, min(60, base_hold))
    else:
        base_hold = 14 if confidence >= 70 else 7
        hold_days = max(5, min(30, base_hold))

    # --- CALCULATE ACTUAL PRICES ---
    if direction == "long":
        stop_loss = round(price * (1 - stop_pct), 2)
        target_price = round(price * (1 + target_pct), 2)
    else:  # short
        stop_loss = round(price * (1 + stop_pct), 2)
        target_price = round(price * (1 - target_pct), 2)

    reasoning = (
        f"ATR={atr_pct*100:.1f}%/day | "
        f"Stop={stop_pct*100:.1f}% ({atr_mult_stop:.1f}x ATR) | "
        f"Target={target_pct*100:.1f}% ({rr_ratio:.1f}:1 R:R) | "
        f"Hold={hold_days}d"
    )

    logger.info(f"AUTONOMOUS DECISION {symbol} {direction}: {reasoning}")

    return {
        "stop_loss": stop_loss,
        "target_price": target_price,
        "hold_days": hold_days,
        "stop_pct": round(stop_pct * 100, 1),
        "target_pct": round(target_pct * 100, 1),
        "atr_pct": round(atr_pct * 100, 1),
        "rr_ratio": rr_ratio,
        "reasoning": reasoning,
    }

# ============================================================
#  ADVANCED: VIX-SCALED POSITION SIZING
# ============================================================
# When VIX is high (fear), we reduce position sizes to limit risk
# When VIX is low (calm), we can be more aggressive
# This is standard institutional risk management
VIX_SIZE_SCALE = {
    "low": 1.3,      # VIX < 15: calm markets, slightly bigger positions
    "normal": 1.0,    # VIX 15-20: standard sizing
    "elevated": 0.85,  # VIX 20-25: slight reduction (was 0.7 — too timid)
    "high": 0.65,      # VIX 25-35: moderate reduction (was 0.5 — crushed trades)
    "crisis": 0.45,    # VIX > 35: reduced but still trading (was 0.25 — froze us)
}

def _get_vix_scale() -> float:
    """Get position size multiplier based on current VIX level."""
    try:
        _throttle()
        vix_df = yf.download("^VIX", period="5d", progress=False)
        if vix_df is not None and not vix_df.empty:
            vix = float(_safe_col(vix_df, "Close").dropna().iloc[-1])
            if vix < 15:
                return VIX_SIZE_SCALE["low"]
            elif vix < 20:
                return VIX_SIZE_SCALE["normal"]
            elif vix < 25:
                return VIX_SIZE_SCALE["elevated"]
            elif vix < 35:
                return VIX_SIZE_SCALE["high"]
            else:
                return VIX_SIZE_SCALE["crisis"]
    except Exception:
        pass
    return 1.0


def _get_dynamic_winlock(regime: str = "SIDEWAYS") -> dict:
    """
    Dynamic WIN-LOCK: the system decides its own profit-lock threshold
    based on VIX level and market regime. No hardcoded values.

    High VIX / BEAR → lock early (take what you can get)
    Low VIX / BULL  → let winners run (big catalyst days need room)
    """
    try:
        _throttle()
        vix_df = yf.download("^VIX", period="5d", progress=False)
        vix = float(_safe_col(vix_df, "Close").dropna().iloc[-1]) if vix_df is not None and not vix_df.empty else 20
    except Exception:
        vix = 20

    # Base threshold from VIX
    if vix >= 35:
        # Crisis mode — grab any win, market could reverse hard
        lock_pct = 1.5
        caution_pct = 1.0
        reason = f"VIX={vix:.0f} CRISIS — lock gains early"
    elif vix >= 25:
        lock_pct = 2.5
        caution_pct = 1.5
        reason = f"VIX={vix:.0f} HIGH — moderate lock"
    elif vix >= 20:
        lock_pct = 3.5
        caution_pct = 2.5
        reason = f"VIX={vix:.0f} ELEVATED — standard lock"
    elif vix >= 15:
        lock_pct = 5.0
        caution_pct = 3.0
        reason = f"VIX={vix:.0f} NORMAL — let winners run"
    else:
        lock_pct = 6.0
        caution_pct = 4.0
        reason = f"VIX={vix:.0f} CALM — maximum room to run"

    # Regime adjustment
    if regime == "BULL":
        lock_pct *= 1.2   # Let it run more in bull
        caution_pct *= 1.2
        reason += " | BULL regime +20%"
    elif regime == "BEAR":
        lock_pct *= 0.7   # Take profits faster in bear
        caution_pct *= 0.7
        reason += " | BEAR regime -30%"

    return {
        "lock_pct": round(lock_pct, 1),
        "caution_pct": round(caution_pct, 1),
        "vix": round(vix, 1),
        "regime": regime,
        "reason": reason,
    }


# ============================================================
#  ADVANCED: CORRELATION-BASED DIVERSIFICATION
# ============================================================
def _check_correlation(new_symbol: str, open_tickers: set, price_data: dict = None) -> dict:
    """
    Check if a new position would be too correlated with existing positions.
    Returns correlation info and whether the trade should be blocked.

    Correlation > 0.80 in same direction = too similar, skip
    This prevents holding GOOGL + META + NFLX all at once (highly correlated)
    """
    result = {"correlated": False, "max_corr": 0, "correlated_with": None}

    if not open_tickers or len(open_tickers) < 2:
        return result

    try:
        check_symbols = list(open_tickers) + [new_symbol]
        _throttle()
        df = yf.download(check_symbols, period="3mo", progress=False, group_by="ticker")
        if df is None or df.empty:
            return result

        # Extract close prices for each symbol
        close_data = {}
        for sym in check_symbols:
            try:
                if isinstance(df.columns, pd.MultiIndex):
                    if sym in df.columns.get_level_values(0):
                        close_series = df[(sym, "Close")].dropna()
                        if len(close_series) >= 20:
                            close_data[sym] = close_series.pct_change().dropna().values
                elif len(check_symbols) == 1:
                    close_series = df["Close"].dropna()
                    if len(close_series) >= 20:
                        close_data[sym] = close_series.pct_change().dropna().values
            except Exception:
                continue

        if new_symbol not in close_data:
            return result

        new_returns = close_data[new_symbol]
        max_corr = 0
        corr_ticker = None

        for sym, returns in close_data.items():
            if sym == new_symbol:
                continue
            # Align lengths
            min_len = min(len(new_returns), len(returns))
            if min_len < 15:
                continue
            corr = float(np.corrcoef(new_returns[:min_len], returns[:min_len])[0, 1])
            if abs(corr) > abs(max_corr):
                max_corr = corr
                corr_ticker = sym

        result["max_corr"] = round(max_corr, 3)
        result["correlated_with"] = corr_ticker

        # Block if correlation > 0.90 with any existing position (loosened to allow more trades)
        if max_corr > 0.90:
            result["correlated"] = True

    except Exception as e:
        logger.debug(f"Correlation check failed for {new_symbol}: {e}")

    return result

# Throttle for Yahoo Finance
_last_call = [0.0]
_DELAY = 3.0


def _throttle():
    now = time.time()
    elapsed = now - _last_call[0]
    if elapsed < _DELAY:
        time.sleep(_DELAY - elapsed)
    _last_call[0] = time.time()


# ============================================================
#  PORTFOLIO STATE
# ============================================================

# Cache portfolio state for 15 seconds so both charts show identical values
_portfolio_cache = {"state": None, "timestamp": 0}
_CACHE_TTL = 15  # seconds

def get_portfolio_state() -> dict:
    """
    Get current portfolio state: cash, positions, total value.
    Cash comes from paper_cash table — updated ATOMICALLY with every trade.
    Cached for 15 seconds so all API endpoints show identical numbers.
    """
    global _portfolio_cache
    now = time.time()
    if _portfolio_cache["state"] and (now - _portfolio_cache["timestamp"]) < _CACHE_TTL:
        return _portfolio_cache["state"]

    from predictions.models import get_open_trades, get_closed_trades, get_cash

    open_trades = get_open_trades()
    closed_trades = get_closed_trades(limit=500)

    # Cash from atomic paper_cash table — ALWAYS accurate
    cash = get_cash()

    # Get current prices for open positions
    positions = []
    positions_value = 0
    if open_trades:
        symbols = list(set(t["ticker"] for t in open_trades))
        current_prices = _get_current_prices(symbols)

        for trade in open_trades:
            ticker = trade["ticker"]
            current_price = current_prices.get(ticker, trade["entry_price"])
            entry_price = trade["entry_price"]
            shares = trade["shares"]
            direction = trade["direction"]

            if direction == "long":
                unrealized_pnl = (current_price - entry_price) * shares
                unrealized_pct = ((current_price / entry_price) - 1) * 100
            else:  # short
                unrealized_pnl = (entry_price - current_price) * shares
                unrealized_pct = ((entry_price / current_price) - 1) * 100

            position_value = abs(shares * current_price)
            positions_value += position_value

            # Check days held
            try:
                entry_date = datetime.fromisoformat(trade["entry_date"])
                days_held = (datetime.now() - entry_date).days
            except Exception:
                days_held = 0

            positions.append({
                "trade_id": trade["id"],
                "ticker": ticker,
                "direction": direction,
                "entry_price": entry_price,
                "current_price": round(current_price, 2),
                "shares": shares,
                "position_value": round(position_value, 2),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "unrealized_pct": round(unrealized_pct, 2),
                "days_held": days_held,
                "stop_loss": trade.get("stop_loss_price"),
                "target": trade.get("target_price"),
                "signal_score": trade.get("signal_score"),
                "regime": trade.get("regime_at_entry"),
                "sector": trade.get("sector"),
            })

    # Calculate performance metrics
    total_current = cash + positions_value
    total_return = ((total_current / ORIGINAL_CAPITAL) - 1) * 100

    # --- Exposure calculations ---
    long_value = sum(p["position_value"] for p in positions if p["direction"] == "long")
    short_value = sum(p["position_value"] for p in positions if p["direction"] == "short")
    gross_exposure = long_value + short_value
    net_exposure = long_value - short_value
    gross_exposure_pct = round((gross_exposure / total_current) * 100, 1) if total_current > 0 else 0
    net_exposure_pct = round((net_exposure / total_current) * 100, 1) if total_current > 0 else 0
    long_pct = round((long_value / total_current) * 100, 1) if total_current > 0 else 0
    short_pct = round((short_value / total_current) * 100, 1) if total_current > 0 else 0
    cash_pct = round((cash / total_current) * 100, 1) if total_current > 0 else 0

    # Win/loss stats from closed trades
    wins = [t for t in closed_trades if (t.get("pnl_pct") or 0) > 0]
    losses = [t for t in closed_trades if (t.get("pnl_pct") or 0) <= 0]
    win_rate = (len(wins) / len(closed_trades) * 100) if closed_trades else 0
    avg_win = np.mean([t["pnl_pct"] for t in wins]) if wins else 0
    avg_loss = np.mean([t["pnl_pct"] for t in losses]) if losses else 0
    profit_factor = abs(avg_win * len(wins)) / (abs(avg_loss * len(losses)) + 0.01) if losses else 0

    # --- Trades per day ---
    trades_per_day = 0
    first_trade_date = None
    if closed_trades:
        dates_with_trades = set()
        for t in closed_trades:
            entry_d = t.get("entry_date", "")
            if entry_d:
                try:
                    d = datetime.fromisoformat(entry_d).strftime("%Y-%m-%d")
                    dates_with_trades.add(d)
                    if first_trade_date is None or d < first_trade_date:
                        first_trade_date = d
                except Exception:
                    pass
        if first_trade_date:
            try:
                start = datetime.strptime(first_trade_date, "%Y-%m-%d")
                trading_days = max(1, np.busday_count(
                    start.date(), datetime.now().date()
                ))
                trades_per_day = round(len(closed_trades) / trading_days, 1)
            except Exception:
                trades_per_day = round(len(closed_trades) / max(1, len(dates_with_trades)), 1)

    result = {
        "total_value": round(total_current, 2),
        "cash": round(cash, 2),
        "positions_value": round(positions_value, 2),
        "total_return_pct": round(total_return, 2),
        "initial_capital": ORIGINAL_CAPITAL,
        "num_positions": len(positions),
        "num_longs": sum(1 for p in positions if p["direction"] == "long"),
        "num_shorts": sum(1 for p in positions if p["direction"] == "short"),
        "max_positions": MAX_POSITIONS,
        "exposure": {
            "long_value": round(long_value, 2),
            "short_value": round(short_value, 2),
            "gross_exposure": round(gross_exposure, 2),
            "net_exposure": round(net_exposure, 2),
            "gross_exposure_pct": gross_exposure_pct,
            "net_exposure_pct": net_exposure_pct,
            "long_pct": long_pct,
            "short_pct": short_pct,
            "cash_pct": cash_pct,
        },
        "positions": positions,
        "recent_closed": [{
            "ticker": t["ticker"],
            "direction": t["direction"],
            "pnl_pct": round(t.get("pnl_pct", 0) or 0, 2),
            "pnl_dollars": round(t.get("pnl_dollars", 0) or 0, 2),
            "entry_price": round(t["entry_price"], 2),
            "exit_price": round(t.get("exit_price") or 0, 2),
        } for t in closed_trades[:10]],
        "stats": {
            "total_trades": len(closed_trades),
            "total_open": len(positions),
            "trades_per_day": trades_per_day,
            "win_rate": round(win_rate, 1),
            "avg_win_pct": round(float(avg_win), 2),
            "avg_loss_pct": round(float(avg_loss), 2),
            "profit_factor": round(profit_factor, 2),
        },
        "timestamp": datetime.now().isoformat(),
    }

    # Cache the result so all API endpoints show identical numbers
    _portfolio_cache["state"] = result
    _portfolio_cache["timestamp"] = time.time()

    return result


def _get_current_prices(symbols: list) -> dict:
    """Get current prices for a list of symbols (batch download)."""
    if not symbols:
        return {}
    _throttle()
    try:
        df = yf.download(symbols, period="5d", progress=False, group_by="ticker")
        prices = {}
        if df is not None and not df.empty:
            for sym in symbols:
                try:
                    if isinstance(df.columns, pd.MultiIndex):
                        if sym in df.columns.get_level_values(0):
                            close = df[(sym, "Close")].dropna()
                            if len(close) > 0:
                                prices[sym] = float(close.iloc[-1])
                    elif len(symbols) == 1:
                        close = df["Close"].dropna()
                        if len(close) > 0:
                            prices[sym] = float(close.iloc[-1])
                except Exception:
                    continue
        return prices
    except Exception:
        return {}


# ============================================================
#  TRADE EXECUTION
# ============================================================

def execute_trades_from_signals(quant_picks: dict) -> dict:
    """
    Execute paper trades based on quant signal picks.

    Logic:
      1. Close positions that hit stop-loss, target, or hold duration
      2. Open new LONG positions for top long picks
      3. Open new SHORT positions for top short picks
      4. Respect max position limit and minimum confidence

    Args:
        quant_picks: output from generate_quant_picks()

    Returns:
        dict with trades executed, positions closed, portfolio state
    """
    from predictions.models import (
        get_open_trades, close_paper_trade, save_paper_trade,
        get_portfolio_snapshots, save_portfolio_snapshot, get_cash
    )

    results = {
        "opened": [],
        "closed": [],
        "skipped": [],
        "errors": [],
    }

    open_trades = get_open_trades()
    open_tickers = set(t["ticker"] for t in open_trades)
    snapshots = get_portfolio_snapshots(days=5)

    # Cash from atomic paper_cash table — ALWAYS accurate
    cash = get_cash()

    regime = quant_picks.get("regime", {}).get("regime", "SIDEWAYS")

    # --- Step 1: Check exits for open positions ---
    if open_trades:
        exit_symbols = list(set(t["ticker"] for t in open_trades))
        current_prices = _get_current_prices(exit_symbols)

        for trade in open_trades:
            ticker = trade["ticker"]
            current_price = current_prices.get(ticker)
            if current_price is None:
                continue

            entry_price = trade["entry_price"]
            direction = trade["direction"]
            should_close = False
            close_reason = ""

            # Calculate current P&L
            if direction == "long":
                pnl_pct = ((current_price / entry_price) - 1) * 100
            else:
                pnl_pct = ((entry_price / current_price) - 1) * 100

            # Check stop loss
            stop_loss = trade.get("stop_loss_price", 0)
            if stop_loss and direction == "long" and current_price <= stop_loss:
                should_close = True
                close_reason = f"Stop loss hit (${stop_loss})"
            elif stop_loss and direction == "short" and current_price >= stop_loss:
                should_close = True
                close_reason = f"Stop loss hit (${stop_loss})"

            # Check target
            target = trade.get("target_price", 0)
            if target and direction == "long" and current_price >= target:
                should_close = True
                close_reason = f"Target hit (${target})"
            elif target and direction == "short" and current_price <= target:
                should_close = True
                close_reason = f"Target hit (${target})"

            # Check hold duration
            try:
                entry_date = datetime.fromisoformat(trade["entry_date"])
                days_held = (datetime.now() - entry_date).days
                max_hold = trade.get("hold_duration_days", DEFAULT_HOLD_DAYS)
                if days_held >= max_hold:
                    should_close = True
                    close_reason = f"Hold duration expired ({days_held} days)"
            except Exception:
                pass

            # AUTONOMOUS TRAILING PROFIT PROTECTION
            # The machine decides when to lock profits based on each stock's volatility.
            # A volatile stock (ATR 5%) needs to be up 10%+ before trailing makes sense.
            # A stable stock (ATR 1%) can start trailing at 3%.
            # Trail width = 1x ATR below peak — gives exactly one day of noise room.
            stock_atr = _calculate_stock_atr(ticker)
            trail_start_pct = stock_atr * 100 * 2  # Start trailing at 2x daily ATR
            trail_start_pct = max(3.0, min(trail_start_pct, 12.0))  # Clamp 3-12%

            if not should_close and pnl_pct > trail_start_pct:
                try:
                    _throttle()
                    hist_df = yf.download(ticker, period="1mo", progress=False)
                    if hist_df is not None and len(hist_df) >= 3:
                        hist_closes = _safe_col(hist_df, "Close").values.astype(float)

                        # Get peak P&L since entry
                        if direction == "long":
                            peak_price = float(np.max(hist_closes))
                            peak_pnl = ((peak_price / entry_price) - 1) * 100
                        else:
                            trough_price = float(np.min(hist_closes))
                            peak_pnl = ((entry_price / trough_price) - 1) * 100

                        # Smart trail: keep more of big winners, protect small ones
                        if peak_pnl >= 20:
                            trail_pct = 0.65  # Keep 65% of huge gains
                        elif peak_pnl >= 12:
                            trail_pct = 0.55  # Keep 55%
                        else:
                            trail_pct = 0.45  # Keep 45% of smaller gains

                        trail_level = peak_pnl * trail_pct

                        if peak_pnl >= trail_start_pct and pnl_pct < trail_level:
                            should_close = True
                            close_reason = (
                                f"SMART TRAIL: ATR={stock_atr*100:.1f}%, peaked at {peak_pnl:+.1f}%, "
                                f"trail at {trail_level:+.1f}%, now {pnl_pct:+.1f}%"
                            )
                except Exception:
                    pass

            # AUTONOMOUS TAKE PROFIT — based on stock's target (set at entry)
            # The target was already calculated by _autonomous_stop_and_target()
            # at entry time and stored in trade["target_price"]. We check it above.
            # But if somehow the stock is up 20%+ and target wasn't hit, lock it.
            if not should_close and pnl_pct >= 20:
                should_close = True
                close_reason = f"AUTO PROFIT LOCK: up {pnl_pct:+.1f}% — exceptional gain secured"

            # REMOVED: "SELL AT HIGHS" intraday trigger
            # This was selling at +3% on tiny intraday dips — killed our best trades
            # The trailing stop above handles profit protection properly

            # BEAR PROTECTION: close losing longs at stop level, not at -2%
            # Was -2% = too tight, gets stopped out on normal volatility
            if not should_close and regime == "BEAR" and direction == "long" and pnl_pct < -4:
                should_close = True
                close_reason = f"BEAR regime protection — closing losing long ({pnl_pct:+.1f}%)"

            # QUICK-CUT RULE: Shorts losing >3% in first 2 days = bad trade, cut immediately
            # Shorts can gap up violently — don't wait for stop-loss
            if not should_close and direction == "short" and pnl_pct < -3:
                try:
                    entry_date = datetime.fromisoformat(trade["entry_date"])
                    days_held = (datetime.now() - entry_date).days
                    if days_held <= 2:
                        should_close = True
                        close_reason = f"QUICK-CUT: short losing {pnl_pct:+.1f}% in {days_held} days — bad entry"
                except Exception:
                    pass

            # SHORTS MAX LOSS: Never let a short lose more than 5%
            if not should_close and direction == "short" and pnl_pct < -5:
                should_close = True
                close_reason = f"SHORT MAX LOSS: down {pnl_pct:+.1f}% — hard cap reached"

            # Check if signal has reversed (optional aggressive exit)
            if not should_close and pnl_pct < -5:
                # If losing more than 5% and we have new signals,
                # check if the stock is now in opposite direction
                for pick in quant_picks.get("short_picks", []) if direction == "long" else quant_picks.get("long_picks", []):
                    if pick["symbol"] == ticker:
                        should_close = True
                        close_reason = f"Signal reversed to {pick['direction']}"
                        break

            if should_close:
                try:
                    close_paper_trade(trade["id"], current_price)
                    # Cash is updated atomically in close_paper_trade()
                    open_tickers.discard(ticker)
                    results["closed"].append({
                        "ticker": ticker,
                        "direction": direction,
                        "entry_price": entry_price,
                        "exit_price": round(current_price, 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "reason": close_reason,
                    })
                except Exception as e:
                    results["errors"].append(f"Failed to close {ticker}: {str(e)}")

    # Refresh cash after closes (atomic paper_cash is always current)
    cash = get_cash()

    # --- Step 2: Open new positions ---
    current_positions = len(open_tickers)
    available_slots = MAX_POSITIONS - current_positions
    regime = quant_picks.get("regime", {}).get("regime", "SIDEWAYS")

    # --- DYNAMIC WIN-LOCK SYSTEM ---
    # The system decides its own lock threshold based on VIX + regime.
    # No hardcoded values — adapts to market conditions in real time.
    current_prices_for_winlock = _get_current_prices(list(open_tickers)) if open_tickers else {}
    positions_value_now = sum(
        current_prices_for_winlock.get(t["ticker"], t["entry_price"]) * t["shares"]
        for t in get_open_trades()
    )
    total_current_value = cash + positions_value_now
    total_return_now = ((total_current_value / ORIGINAL_CAPITAL) - 1) * 100

    # Compare to yesterday's snapshot to get TODAY's gain
    daily_gain = 0
    if snapshots:
        yesterday_val = snapshots[-1].get("total_value", INITIAL_CAPITAL)
        daily_gain = ((total_current_value / yesterday_val) - 1) * 100

    # Dynamic WIN-LOCK: system decides based on VIX + regime
    winlock = _get_dynamic_winlock(regime)
    winlock_threshold = winlock["lock_pct"]
    winlock_caution = winlock["caution_pct"]

    if daily_gain >= winlock_threshold:
        logger.warning(f"WIN-LOCK: Up {daily_gain:+.1f}% today (threshold: {winlock_threshold}%) — SELLING ALL | {winlock['reason']}")
        for trade in get_open_trades():
            t_ticker = trade["ticker"]
            t_price = current_prices_for_winlock.get(t_ticker)
            if t_price:
                try:
                    close_paper_trade(trade["id"], t_price)
                    t_dir = trade["direction"]
                    t_entry = trade["entry_price"]
                    if t_dir == "long":
                        t_pnl = ((t_price / t_entry) - 1) * 100
                    else:
                        t_pnl = ((t_entry / t_price) - 1) * 100
                    results["closed"].append({
                        "ticker": t_ticker, "direction": t_dir,
                        "entry_price": t_entry, "exit_price": round(t_price, 2),
                        "pnl_pct": round(t_pnl, 2),
                        "reason": f"WIN-LOCK: daily gain {daily_gain:+.1f}% ≥ {winlock_threshold}% | {winlock['reason']}",
                    })
                    open_tickers.discard(t_ticker)
                except Exception as e:
                    results["errors"].append(f"WIN-LOCK close {t_ticker}: {e}")
        cash = get_cash()
        results["skipped"].append({"symbol": "ALL", "reason": f"WIN-LOCK: up {daily_gain:+.1f}% — sold all (auto-threshold: {winlock_threshold}% | {winlock['reason']})"})
        available_slots = 0
    elif daily_gain >= winlock_caution:
        logger.warning(f"WIN-LOCK CAUTION: Up {daily_gain:+.1f}% (caution at {winlock_caution}%) — reducing new trade size by 50%")

    # --- DRAWDOWN PROTECTION (hedge fund risk management) ---
    drawdown_pct = total_return_now
    drawdown_multiplier = 1.0

    if drawdown_pct <= -10:
        logger.warning(f"DRAWDOWN PROTECTION: Portfolio down {drawdown_pct:.1f}% — HALTING new trades")
        results["skipped"].append({"symbol": "ALL", "reason": f"Drawdown protection: portfolio down {drawdown_pct:.1f}%"})
        available_slots = 0
    elif drawdown_pct <= -5:
        logger.warning(f"DRAWDOWN PROTECTION: Portfolio down {drawdown_pct:.1f}% — halving position sizes")
        drawdown_multiplier = 0.5

    # No position limits — the computer trades freely like a real hedge fund

    # Get VIX-based position size multiplier
    vix_multiplier = _get_vix_scale()
    logger.info(f"VIX position size multiplier: {vix_multiplier}")

    # --- LEARN FROM MISTAKES: Apply corrections from past losing trades ---
    try:
        from predictions.learner import get_mistake_adjustments
        mistake_adj = get_mistake_adjustments()
        logger.info(f"Mistake adjustments loaded: {len(mistake_adj.get('sector_penalties', {}))} sector penalties, "
                     f"{len(mistake_adj.get('blocked_combos', []))} blocked combos")
    except Exception:
        mistake_adj = {"sector_penalties": {}, "blocked_combos": [], "confidence_cap": 95, "tighten_stops": False}

    # --- OVERNIGHT INTELLIGENCE: Apply pre-market position sizing ---
    overnight = quant_picks.get("overnight", {})
    overnight_size_mod = overnight.get("position_size_modifier", 1.0) if overnight else 1.0

    if available_slots > 0 and cash > 1000:
        # REGIME-AWARE PICK SELECTION
        # In BEAR: prioritize shorts heavily, limit longs
        # In BULL: prioritize longs heavily, limit shorts
        all_picks = []

        long_candidates = [p for p in quant_picks.get("long_picks", [])
                          if p["confidence"] >= MIN_CONFIDENCE and p["symbol"] not in open_tickers]
        short_candidates = [p for p in quant_picks.get("short_picks", [])
                           if p["confidence"] >= MIN_CONFIDENCE and p["symbol"] not in open_tickers]

        # Defensive sectors — safe for long positions even in bear markets
        # These are stable, dividend-paying, recession-resistant sectors
        DEFENSIVE_SECTORS = {"Consumer Staples", "Healthcare", "Utilities", "ETF"}

        if regime == "BEAR":
            # BEAR: Take ALL qualifying shorts, plus safe defensive longs
            # Smart investing = shorts for profit + defensive longs for stability
            for p in short_candidates:
                p["_adj_confidence"] = p["confidence"] + 10
                all_picks.append(p)

            # FORCED SAFE LONGS: Only take longs in defensive sectors (low risk)
            # Defensive stocks (WMT, JNJ, PG, KO) hold up in bear markets
            safe_longs = [p for p in long_candidates
                          if p.get("sector") in DEFENSIVE_SECTORS]
            # Also allow any long with very high confidence (65%+) regardless of sector
            high_conviction_longs = [p for p in long_candidates
                                     if p["confidence"] >= 65 and p.get("sector") not in DEFENSIVE_SECTORS]
            bear_longs = (safe_longs + high_conviction_longs)[:10]

            for p in bear_longs:
                p["_adj_confidence"] = p["confidence"]  # no penalty for safe longs
                all_picks.append(p)
            logger.info(f"BEAR regime: {len(short_candidates)} shorts, {len(bear_longs)} safe longs "
                        f"({len(safe_longs)} defensive + {len(high_conviction_longs)} high-conviction)")
        elif regime == "BULL":
            # BULL: Take ALL qualifying longs first, then only top 2 shorts
            for p in long_candidates:
                p["_adj_confidence"] = p["confidence"] + 15
                all_picks.append(p)
            for p in short_candidates[:2]:  # max 2 shorts in bull
                p["_adj_confidence"] = p["confidence"] - 10
                all_picks.append(p)
            logger.info(f"BULL regime: {len(long_candidates)} longs, {min(2, len(short_candidates))} shorts selected")
        else:
            # SIDEWAYS: balanced
            for p in long_candidates:
                p["_adj_confidence"] = p["confidence"]
                all_picks.append(p)
            for p in short_candidates:
                p["_adj_confidence"] = p["confidence"]
                all_picks.append(p)

        # Sort by adjusted confidence (highest first)
        all_picks.sort(key=lambda x: x.get("_adj_confidence", x["confidence"]), reverse=True)

        # SECTOR CONCENTRATION LIMIT: max 3 positions per sector per direction
        # Diversification is what separates hedge funds from retail gamblers
        sector_counts = {}
        for t in get_open_trades():
            if t["ticker"] in open_tickers:
                key = f"{t.get('sector', 'Unknown')}_{t['direction']}"
                sector_counts[key] = sector_counts.get(key, 0) + 1

        for pick in all_picks[:available_slots]:
            symbol = pick["symbol"]
            price = pick["price"]
            direction = "long" if pick["direction"] == "LONG" else "short"

            # GROSS EXPOSURE LIMIT: Never invest more than 85% of portfolio
            # Hedge funds always keep a cash buffer for opportunities and margin calls
            gross_exposure = sum(
                t.get("shares", 0) * t.get("entry_price", 0)
                for t in get_open_trades() if t["ticker"] in open_tickers
            )
            max_exposure = total_current_value * 0.92  # 92% max gross exposure — deploy more capital
            if gross_exposure >= max_exposure:
                results["skipped"].append({
                    "symbol": symbol,
                    "reason": f"Gross exposure limit (85% of portfolio)",
                })
                break  # Stop opening more positions

            # CONFIDENCE GATE: In BEAR only take decent-conviction longs
            # Defensive sector longs get a lower gate (35%) — they're safe by nature
            # Other sector longs need higher conviction (55%)
            if regime == "BEAR" and direction == "long":
                is_defensive = pick.get("sector") in DEFENSIVE_SECTORS
                min_conf = 35 if is_defensive else 55
                if pick["confidence"] < min_conf:
                    results["skipped"].append({
                        "symbol": symbol,
                        "reason": f"Low conviction long in BEAR ({pick['confidence']}%, need {min_conf}%)",
                    })
                    continue

            # CORRELATION CHECK: Don't hold highly correlated positions
            # This is what separates hedge funds from retail — true diversification
            if len(open_tickers) >= 3:
                corr_check = _check_correlation(symbol, open_tickers)
                if corr_check["correlated"]:
                    results["skipped"].append({
                        "symbol": symbol,
                        "reason": f"Too correlated with {corr_check['correlated_with']} (r={corr_check['max_corr']:.2f})",
                    })
                    continue

            # MISTAKE LEARNING: Penalize sectors we've lost money in
            pick_sector = pick.get("sector", "Unknown")
            sector_penalty = mistake_adj.get("sector_penalties", {}).get(pick_sector, 0)
            if sector_penalty != 0:
                pick["confidence"] = max(15, pick["confidence"] + sector_penalty)
                if pick["confidence"] < MIN_CONFIDENCE:
                    results["skipped"].append({
                        "symbol": symbol,
                        "reason": f"Learned mistake: {pick_sector} sector has high loss rate",
                    })
                    continue

            # MISTAKE LEARNING: Block bad regime/direction combos
            combo_key = f"{regime}_{direction.upper()}"
            if combo_key in mistake_adj.get("blocked_combos", []):
                # Don't fully block — just heavily penalize confidence
                pick["confidence"] = max(15, pick["confidence"] - 15)
                if pick["confidence"] < MIN_CONFIDENCE:
                    results["skipped"].append({
                        "symbol": symbol,
                        "reason": f"Learned mistake: {direction} in {regime} regime has high loss rate",
                    })
                    continue

            # MISTAKE LEARNING: Cap max confidence if system has been overconfident
            conf_cap = mistake_adj.get("confidence_cap", 95)
            pick["confidence"] = min(pick["confidence"], conf_cap)

            # Check sector concentration — raised from 5 to 8 to allow more trades
            sector_key = f"{pick.get('sector', 'Unknown')}_{direction}"
            if sector_counts.get(sector_key, 0) >= 8:
                results["skipped"].append({
                    "symbol": symbol,
                    "reason": f"Sector concentration limit ({pick.get('sector')} {direction})",
                })
                continue

            # Position sizing: CONVICTION-BASED like a real hedge fund
            # Renaissance uses 5-10% per position, not 2%
            cash = get_cash()  # Always fresh
            total_value = cash + positions_value_now

            # RENTECH: Apply circuit breaker from portfolio risk module
            circuit_breaker = quant_picks.get("circuit_breaker", {})
            cb_multiplier = circuit_breaker.get("position_size_multiplier", 1.0)
            if not circuit_breaker.get("allow_new_longs", True) and direction == "long":
                results["skipped"].append({
                    "symbol": symbol,
                    "reason": f"Circuit breaker HALT: {circuit_breaker.get('message', 'drawdown too large')}",
                })
                continue
            if not circuit_breaker.get("allow_new_shorts", True) and direction == "short":
                results["skipped"].append({
                    "symbol": symbol,
                    "reason": f"Circuit breaker: no new shorts — {circuit_breaker.get('message', '')}",
                })
                continue

            # DRAWDOWN RECOVERY MODE — reduce size when portfolio is losing
            dd_mode = quant_picks.get("drawdown_mode", {})
            dd_multiplier = dd_mode.get("size_multiplier", 1.0)
            dd_allowed = dd_mode.get("allowed_sectors")
            if dd_multiplier == 0:
                results["skipped"].append({
                    "symbol": symbol,
                    "reason": f"DRAWDOWN HALT: {dd_mode.get('message', 'trading suspended')}",
                })
                continue
            if dd_allowed is not None and pick.get("sector", "") not in dd_allowed:
                results["skipped"].append({
                    "symbol": symbol,
                    "reason": f"Drawdown mode: only {dd_allowed} sectors allowed",
                })
                continue

            # EARNINGS SHIELD — Block stocks with earnings in next 1-2 days
            earnings_shield = quant_picks.get("earnings_shield", {})
            earnings_blocked_syms = {s["symbol"] for s in earnings_shield.get("blocked", [])}
            if symbol in earnings_blocked_syms:
                results["skipped"].append({
                    "symbol": symbol,
                    "reason": "EARNINGS SHIELD: earnings imminent, trade blocked",
                })
                continue

            # Regime-aware sizing — 6% base, conviction direction gets 8%
            if regime == "BEAR":
                size_pct = 0.08 if direction == "short" else 0.05
            elif regime == "BULL":
                size_pct = 0.08 if direction == "long" else 0.04
            else:
                size_pct = POSITION_SIZE_PCT  # 6%
            position_value = total_value * size_pct * drawdown_multiplier * vix_multiplier * overnight_size_mod * cb_multiplier * dd_multiplier
            shares = round(position_value / price, 4)

            if shares * price > cash:
                # Not enough cash
                results["skipped"].append({
                    "symbol": symbol,
                    "reason": "Insufficient cash",
                })
                continue

            # AUTONOMOUS DECISION ENGINE — Machine decides per-stock
            # No more fixed percentages. Each stock gets its own stop loss
            # and profit target based on its actual volatility (ATR),
            # signal strength, regime, and trade type.
            is_mr = pick.get("mean_reversion", False) or pick.get("signal_type") == "MEAN_REVERSION"
            auto_decision = _autonomous_stop_and_target(
                symbol=symbol,
                direction=direction,
                price=price,
                regime=regime,
                confidence=pick.get("confidence", 50),
                composite_score=pick.get("composite_score", 0),
                sector=pick.get("sector", ""),
                is_mean_reversion=is_mr,
            )
            stop_loss = auto_decision["stop_loss"]
            target = auto_decision["target_price"]

            # MISTAKE LEARNING: Tighten stops if we've been holding losers too long
            if mistake_adj.get("tighten_stops"):
                stop_pct_adj = auto_decision["stop_pct"] / 100 * 0.7  # 30% tighter
                if direction == "long":
                    stop_loss = round(price * (1 - stop_pct_adj), 2)
                else:
                    stop_loss = round(price * (1 + stop_pct_adj), 2)

            try:
                # AUTONOMOUS HOLD DURATION — decided by the machine per-stock
                adaptive_hold = auto_decision["hold_days"]

                trade_id = save_paper_trade(
                    ticker=symbol,
                    direction=direction,
                    entry_price=price,
                    shares=shares,
                    signal_score=pick.get("composite_score", 0),
                    regime=regime,
                    factors=pick.get("factors", {}),
                    stop_loss=stop_loss,
                    target_price=target,
                    hold_days=adaptive_hold,
                    sector=pick.get("sector", ""),
                )

                # Cash deducted atomically inside save_paper_trade()
                cash = get_cash()  # Refresh from DB
                open_tickers.add(symbol)
                current_positions += 1
                sector_counts[sector_key] = sector_counts.get(sector_key, 0) + 1

                results["opened"].append({
                    "trade_id": trade_id,
                    "symbol": symbol,
                    "direction": direction,
                    "price": price,
                    "shares": round(shares, 4),
                    "position_value": round(shares * price, 2),
                    "stop_loss": stop_loss,
                    "target": target,
                    "confidence": pick["confidence"],
                    "score": pick["composite_score"],
                    "sector": pick.get("sector"),
                    "auto_decision": auto_decision["reasoning"],
                })
            except Exception as e:
                results["errors"].append(f"Failed to open {symbol}: {str(e)}")

    # --- Step 3: Save portfolio snapshot ---
    try:
        # Use atomic cash — always accurate
        cash = get_cash()
        state = get_portfolio_state()
        positions_value = state["positions_value"]
        total_value = state["total_value"]  # Already uses atomic cash

        # Get S&P 500 performance for comparison
        sp500_daily = 0
        sp500_cum = 0
        try:
            _throttle()
            sp_df = yf.download("^GSPC", period="1mo", progress=False)
            if sp_df is not None and len(sp_df) >= 2:
                sp_closes = _safe_col(sp_df, "Close").values.astype(float)
                sp500_daily = ((sp_closes[-1] / sp_closes[-2]) - 1) * 100
                # Use first available snapshot date or 1 month ago
                sp500_cum = ((sp_closes[-1] / sp_closes[0]) - 1) * 100
        except Exception:
            pass

        prev_value = snapshots[-1]["total_value"] if snapshots else INITIAL_CAPITAL
        daily_return = ((total_value / prev_value) - 1) * 100 if prev_value > 0 else 0
        cum_return = ((total_value / ORIGINAL_CAPITAL) - 1) * 100

        save_portfolio_snapshot(
            total_value=round(total_value, 2),
            cash=round(cash, 2),
            positions_value=round(positions_value, 2),
            daily_ret=round(daily_return, 2),
            cum_ret=round(cum_return, 2),
            sp500_daily=round(sp500_daily, 2),
            sp500_cum=round(sp500_cum, 2),
            num_pos=current_positions,
        )
    except Exception as e:
        results["errors"].append(f"Snapshot save failed: {str(e)}")

    results["portfolio_after"] = {
        "cash": round(cash, 2),
        "num_positions": current_positions,
        "regime": regime,
    }

    return results


# ============================================================
#  RAPID BACKTESTING — Historical Simulated Trades
# ============================================================

def run_backtest(days_back: int = 180, num_trades_target: int = 500) -> dict:
    """
    Rapid backtesting — simulate hundreds of trades using historical data
    to immediately populate the learning system with win/loss history.

    Strategy:
      For each trading day in the lookback period:
        1. Calculate RSI(2) for each stock
        2. If RSI(2) < 10 and price > 200-SMA → BUY (Connors strategy)
        3. Exit when RSI(2) > 70 or after 5 trading days (whichever first)
        4. Record outcome as win/loss

    This is the Connors RSI(2) mean-reversion strategy which has
    a documented 75-91% win rate historically.

    We also test momentum and value factors for comparison.

    Args:
        days_back: how many calendar days of history to test
        num_trades_target: approximate number of trades to generate

    Returns:
        dict with trade results, factor performance, overall stats
    """
    from predictions.models import save_paper_trade, close_paper_trade

    logger.info(f"Starting backtest: {days_back} days, target {num_trades_target} trades")
    start_time = time.time()

    # Use a subset of liquid stocks for backtesting
    backtest_symbols = [
        "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA",
        "JPM", "V", "UNH", "JNJ", "XOM", "HD", "PG", "BA",
        "CRM", "AMD", "NFLX", "WMT", "GS", "CAT", "LLY",
        "MRK", "COST", "CVX", "MA", "ABBV",
    ]

    # Batch download historical data
    _throttle()
    try:
        period = "2y" if days_back > 365 else "1y"
        df = yf.download(
            backtest_symbols, period=period, progress=False, group_by="ticker"
        )
    except Exception as e:
        return {"error": f"Download failed: {e}", "trades": []}

    if df is None or df.empty:
        return {"error": "No data available", "trades": []}

    all_trades = []
    factor_stats = {
        "rsi2_mean_reversion": {"wins": 0, "losses": 0, "returns": []},
        "momentum": {"wins": 0, "losses": 0, "returns": []},
        "mean_reversion_value": {"wins": 0, "losses": 0, "returns": []},
    }

    for symbol in backtest_symbols:
        try:
            if isinstance(df.columns, pd.MultiIndex):
                if symbol not in df.columns.get_level_values(0):
                    continue
                sym_df = df[symbol].dropna(how="all")
            else:
                continue

            if len(sym_df) < 250:
                continue

            closes = _safe_col(sym_df, "Close").values.astype(float)
            dates = sym_df.index

            # Scan through history looking for trade setups
            for i in range(210, len(closes) - 10):
                # Only backtest within the requested window
                try:
                    trade_date = dates[i]
                    if hasattr(trade_date, 'date'):
                        td = trade_date.date()
                    else:
                        td = pd.Timestamp(trade_date).date()
                    cutoff = datetime.now().date() - timedelta(days=days_back)
                    if td < cutoff:
                        continue
                except Exception:
                    continue

                # --- Strategy 1: RSI(2) Mean Reversion ---
                if i >= 3:
                    deltas = closes[i-2:i+1] - closes[i-3:i]
                    gain = float(np.sum(np.maximum(deltas, 0)))
                    loss = float(np.sum(np.maximum(-deltas, 0)))
                    rsi2 = 100 - (100 / (1 + (gain / (loss + 1e-10))))

                    # 200-SMA filter
                    sma200 = float(np.mean(closes[i-200:i]))
                    current = closes[i]

                    if rsi2 < 10 and current > sma200:
                        # Entry signal! Find exit
                        entry_price = closes[i + 1] if i + 1 < len(closes) else current  # buy next day open approx
                        exit_price = entry_price
                        exit_day = i + 1
                        hold_days = 0

                        # Exit when RSI(2) > 70 or after 5 days
                        for j in range(i + 2, min(i + 7, len(closes))):
                            d = closes[j-2:j+1] - closes[j-3:j]
                            g = float(np.sum(np.maximum(d, 0)))
                            l = float(np.sum(np.maximum(-d, 0)))
                            exit_rsi = 100 - (100 / (1 + (g / (l + 1e-10))))
                            hold_days = j - i - 1
                            if exit_rsi > 70 or hold_days >= 5:
                                exit_price = closes[j]
                                exit_day = j
                                break
                        else:
                            if i + 6 < len(closes):
                                exit_price = closes[i + 6]
                                hold_days = 5

                        ret_pct = ((exit_price / entry_price) - 1) * 100
                        is_win = ret_pct > 0

                        factor_stats["rsi2_mean_reversion"]["returns"].append(ret_pct)
                        if is_win:
                            factor_stats["rsi2_mean_reversion"]["wins"] += 1
                        else:
                            factor_stats["rsi2_mean_reversion"]["losses"] += 1

                        all_trades.append({
                            "symbol": symbol,
                            "strategy": "rsi2_mean_reversion",
                            "direction": "long",
                            "entry_price": round(float(entry_price), 2),
                            "exit_price": round(float(exit_price), 2),
                            "return_pct": round(float(ret_pct), 2),
                            "hold_days": hold_days,
                            "is_win": is_win,
                            "entry_date": str(td),
                            "rsi2_at_entry": round(rsi2, 1),
                        })

                # --- Strategy 2: Momentum (buy winners) ---
                # Every 20 trading days, check 60-day momentum
                if i % 20 == 0 and i >= 60:
                    mom_60d = ((closes[i] / closes[i - 60]) - 1) * 100
                    if mom_60d > 10:  # Strong momentum
                        entry_price = closes[i + 1] if i + 1 < len(closes) else closes[i]
                        # Hold for 20 trading days
                        exit_idx = min(i + 21, len(closes) - 1)
                        exit_price = closes[exit_idx]
                        ret_pct = ((exit_price / entry_price) - 1) * 100

                        is_win = ret_pct > 0
                        factor_stats["momentum"]["returns"].append(ret_pct)
                        if is_win:
                            factor_stats["momentum"]["wins"] += 1
                        else:
                            factor_stats["momentum"]["losses"] += 1

                        all_trades.append({
                            "symbol": symbol,
                            "strategy": "momentum",
                            "direction": "long",
                            "entry_price": round(float(entry_price), 2),
                            "exit_price": round(float(exit_price), 2),
                            "return_pct": round(float(ret_pct), 2),
                            "hold_days": min(20, exit_idx - i),
                            "is_win": is_win,
                            "entry_date": str(td),
                            "momentum_60d": round(mom_60d, 1),
                        })

                # --- Strategy 3: Mean Reversion / Value ---
                # Buy stocks that have dropped > 10% from 60-day high
                if i % 10 == 5 and i >= 60:
                    high_60d = float(np.max(closes[i-60:i]))
                    drawdown = ((closes[i] / high_60d) - 1) * 100

                    if drawdown < -10 and closes[i] > float(np.mean(closes[i-200:i])):
                        # Dropped 10%+ but still above 200-SMA (not in freefall)
                        entry_price = closes[i + 1] if i + 1 < len(closes) else closes[i]
                        exit_idx = min(i + 16, len(closes) - 1)
                        exit_price = closes[exit_idx]
                        ret_pct = ((exit_price / entry_price) - 1) * 100

                        is_win = ret_pct > 0
                        factor_stats["mean_reversion_value"]["returns"].append(ret_pct)
                        if is_win:
                            factor_stats["mean_reversion_value"]["wins"] += 1
                        else:
                            factor_stats["mean_reversion_value"]["losses"] += 1

                        all_trades.append({
                            "symbol": symbol,
                            "strategy": "mean_reversion_value",
                            "direction": "long",
                            "entry_price": round(float(entry_price), 2),
                            "exit_price": round(float(exit_price), 2),
                            "return_pct": round(float(ret_pct), 2),
                            "hold_days": min(15, exit_idx - i),
                            "is_win": is_win,
                            "entry_date": str(td),
                            "drawdown_pct": round(drawdown, 1),
                        })

                # Early exit if we have enough trades
                if len(all_trades) >= num_trades_target:
                    break

        except Exception as e:
            logger.debug(f"Backtest failed for {symbol}: {e}")
            continue

        if len(all_trades) >= num_trades_target:
            break

    # --- Calculate overall statistics ---
    if all_trades:
        returns = [t["return_pct"] for t in all_trades]
        wins = [r for r in returns if r > 0]
        losses = [r for r in returns if r <= 0]
        win_rate = len(wins) / len(returns) * 100
        avg_return = float(np.mean(returns))
        avg_win = float(np.mean(wins)) if wins else 0
        avg_loss = float(np.mean(losses)) if losses else 0

        # Sharpe ratio (annualized, assuming ~252 trading days / avg hold period)
        avg_hold = float(np.mean([t["hold_days"] for t in all_trades]))
        trades_per_year = 252 / max(1, avg_hold)
        if np.std(returns) > 0:
            sharpe = (avg_return / float(np.std(returns))) * np.sqrt(trades_per_year)
        else:
            sharpe = 0

        # Max drawdown
        cumulative = np.cumsum(returns)
        running_max = np.maximum.accumulate(cumulative)
        drawdowns = cumulative - running_max
        max_drawdown = float(np.min(drawdowns)) if len(drawdowns) > 0 else 0

        # Profit factor
        total_gains = sum(wins) if wins else 0
        total_losses = abs(sum(losses)) if losses else 0.01
        profit_factor = total_gains / total_losses
    else:
        win_rate = 0
        avg_return = 0
        avg_win = 0
        avg_loss = 0
        sharpe = 0
        max_drawdown = 0
        profit_factor = 0

    # Factor-level statistics
    factor_results = {}
    for factor_name, stats in factor_stats.items():
        total = stats["wins"] + stats["losses"]
        if total > 0:
            rets = stats["returns"]
            factor_results[factor_name] = {
                "total_trades": total,
                "win_rate": round(stats["wins"] / total * 100, 1),
                "avg_return": round(float(np.mean(rets)), 2),
                "best_trade": round(float(max(rets)), 2) if rets else 0,
                "worst_trade": round(float(min(rets)), 2) if rets else 0,
                "sharpe": round(
                    float(np.mean(rets) / (np.std(rets) + 1e-10)) * np.sqrt(52), 2
                ),
            }

    elapsed = round(time.time() - start_time, 1)

    return {
        "total_trades": len(all_trades),
        "win_rate": round(win_rate, 1),
        "avg_return_pct": round(avg_return, 2),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "sharpe_ratio": round(sharpe, 2),
        "max_drawdown_pct": round(max_drawdown, 2),
        "profit_factor": round(profit_factor, 2),
        "factor_results": factor_results,
        "trades_sample": all_trades[:50],  # First 50 for display
        "computation_time_seconds": elapsed,
        "period_days": days_back,
        "symbols_tested": len(backtest_symbols),
    }


# ============================================================
#  PERFORMANCE ANALYTICS
# ============================================================

def get_performance_analytics() -> dict:
    """
    Comprehensive performance analytics for the paper trading portfolio.

    Calculates:
      - Sharpe ratio (annualized)
      - Max drawdown
      - Win rate by sector, by regime, by direction
      - Equity curve data for charting
      - Comparison vs S&P 500
    """
    from predictions.models import get_closed_trades, get_portfolio_snapshots

    closed = get_closed_trades(limit=500)
    snapshots = get_portfolio_snapshots(days=365)

    if not closed and not snapshots:
        return {
            "message": "No trading history yet. Run a backtest or wait for live trades.",
            "has_data": False,
        }

    result = {"has_data": True}

    # --- Overall stats ---
    if closed:
        returns = [t.get("pnl_pct", 0) or 0 for t in closed]
        wins = [r for r in returns if r > 0]
        losses = [r for r in returns if r <= 0]

        result["overall"] = {
            "total_trades": len(closed),
            "win_rate": round(len(wins) / len(returns) * 100, 1) if returns else 0,
            "avg_return": round(float(np.mean(returns)), 2),
            "avg_win": round(float(np.mean(wins)), 2) if wins else 0,
            "avg_loss": round(float(np.mean(losses)), 2) if losses else 0,
            "best_trade": round(float(max(returns)), 2) if returns else 0,
            "worst_trade": round(float(min(returns)), 2) if returns else 0,
            "total_pnl": round(sum(t.get("pnl_dollars", 0) or 0 for t in closed), 2),
        }

        # Sharpe ratio
        if len(returns) >= 5 and np.std(returns) > 0:
            avg_hold = 15  # approximate
            trades_per_year = 252 / max(1, avg_hold)
            sharpe = (np.mean(returns) / np.std(returns)) * np.sqrt(trades_per_year)
            result["overall"]["sharpe_ratio"] = round(float(sharpe), 2)
        else:
            result["overall"]["sharpe_ratio"] = 0

        # Sortino ratio — only penalizes downside volatility
        if len(returns) >= 5:
            downside_returns = [r for r in returns if r < 0]
            if downside_returns and np.std(downside_returns) > 0:
                avg_hold = 15
                trades_per_year = 252 / max(1, avg_hold)
                sortino = (np.mean(returns) / np.std(downside_returns)) * np.sqrt(trades_per_year)
                result["overall"]["sortino_ratio"] = round(float(sortino), 2)
            else:
                # No losing trades = infinite sortino, cap at 99
                result["overall"]["sortino_ratio"] = 99.0 if np.mean(returns) > 0 else 0
        else:
            result["overall"]["sortino_ratio"] = 0

        # Trades per day
        first_date = None
        for t in closed:
            ed = t.get("entry_date", "")
            if ed:
                try:
                    d = datetime.fromisoformat(ed).strftime("%Y-%m-%d")
                    if first_date is None or d < first_date:
                        first_date = d
                except Exception:
                    pass
        if first_date:
            try:
                start = datetime.strptime(first_date, "%Y-%m-%d")
                trading_days = max(1, int(np.busday_count(start.date(), datetime.now().date())))
                result["overall"]["trades_per_day"] = round(len(closed) / trading_days, 1)
                result["overall"]["trading_days_active"] = trading_days
            except Exception:
                result["overall"]["trades_per_day"] = 0
                result["overall"]["trading_days_active"] = 0
        else:
            result["overall"]["trades_per_day"] = 0
            result["overall"]["trading_days_active"] = 0

        # Profit factor
        total_gains = sum(wins) if wins else 0
        total_losses_abs = abs(sum(losses)) if losses else 0.01
        result["overall"]["profit_factor"] = round(total_gains / total_losses_abs, 2)

        # --- Win rate by sector ---
        sector_stats = {}
        for trade in closed:
            sector = trade.get("sector") or "Unknown"
            if sector not in sector_stats:
                sector_stats[sector] = {"wins": 0, "total": 0, "returns": []}
            sector_stats[sector]["total"] += 1
            pnl = trade.get("pnl_pct", 0) or 0
            sector_stats[sector]["returns"].append(pnl)
            if pnl > 0:
                sector_stats[sector]["wins"] += 1

        result["by_sector"] = {
            sector: {
                "win_rate": round(s["wins"] / s["total"] * 100, 1),
                "total_trades": s["total"],
                "avg_return": round(float(np.mean(s["returns"])), 2),
            }
            for sector, s in sector_stats.items()
            if s["total"] >= 3  # minimum sample size
        }

        # --- Win rate by regime ---
        regime_stats = {}
        for trade in closed:
            regime = trade.get("regime_at_entry") or "Unknown"
            if regime not in regime_stats:
                regime_stats[regime] = {"wins": 0, "total": 0, "returns": []}
            regime_stats[regime]["total"] += 1
            pnl = trade.get("pnl_pct", 0) or 0
            regime_stats[regime]["returns"].append(pnl)
            if pnl > 0:
                regime_stats[regime]["wins"] += 1

        result["by_regime"] = {
            regime: {
                "win_rate": round(s["wins"] / s["total"] * 100, 1),
                "total_trades": s["total"],
                "avg_return": round(float(np.mean(s["returns"])), 2),
            }
            for regime, s in regime_stats.items()
            if s["total"] >= 3
        }

        # --- Win rate by direction ---
        long_trades = [t for t in closed if t.get("direction") == "long"]
        short_trades = [t for t in closed if t.get("direction") == "short"]

        if long_trades:
            long_rets = [(t.get("pnl_pct", 0) or 0) for t in long_trades]
            result["long_stats"] = {
                "total": len(long_trades),
                "win_rate": round(sum(1 for r in long_rets if r > 0) / len(long_rets) * 100, 1),
                "avg_return": round(float(np.mean(long_rets)), 2),
            }

        if short_trades:
            short_rets = [(t.get("pnl_pct", 0) or 0) for t in short_trades]
            result["short_stats"] = {
                "total": len(short_trades),
                "win_rate": round(sum(1 for r in short_rets if r > 0) / len(short_rets) * 100, 1),
                "avg_return": round(float(np.mean(short_rets)), 2),
            }

    # --- Equity curve ---
    if snapshots:
        result["equity_curve"] = [{
            "date": s["snapshot_date"],
            "portfolio_value": s["total_value"],
            "portfolio_return": s.get("cumulative_return_pct", 0),
            "sp500_return": s.get("sp500_cumulative_return_pct", 0),
            "num_positions": s.get("num_positions", 0),
        } for s in snapshots]

        # Max drawdown from equity curve
        values = [s["total_value"] for s in snapshots]
        if len(values) >= 2:
            peak = values[0]
            max_dd = 0
            for v in values:
                peak = max(peak, v)
                dd = ((v / peak) - 1) * 100
                max_dd = min(max_dd, dd)
            result["max_drawdown_pct"] = round(max_dd, 2)

    result["timestamp"] = datetime.now().isoformat()
    return result


# ============================================================
#  STANDALONE EXIT CHECKER
#  Runs independently of entry logic — checks stops/targets
#  every cycle even when no new trades are planned.
# ============================================================

def check_and_exit_positions(regime: str = "SIDEWAYS") -> dict:
    """
    Check all open positions for exit conditions WITHOUT generating new picks.
    This decouples exit management from entry decisions so stop-losses always fire.
    Also handles dynamic WIN-LOCK: system decides its own profit-lock threshold based on VIX + regime.
    Returns dict with list of closed positions.
    """
    from predictions.models import get_open_trades, close_paper_trade, get_portfolio_snapshots, save_portfolio_snapshot, get_cash

    open_trades = get_open_trades()
    if not open_trades:
        return {"closed": [], "checked": 0}

    exit_symbols = list(set(t["ticker"] for t in open_trades))
    current_prices = _get_current_prices(exit_symbols)

    if not current_prices:
        return {"closed": [], "checked": len(open_trades), "error": "Could not fetch prices"}

    closed = []
    snapshots = get_portfolio_snapshots(days=5)
    cash = get_cash()  # Atomic cash — always accurate

    # --- DYNAMIC WIN-LOCK CHECK: system decides threshold based on VIX + regime ---
    positions_val = sum(
        current_prices.get(t["ticker"], t["entry_price"]) * t["shares"]
        for t in open_trades
    )
    total_now = cash + positions_val
    yesterday_val = snapshots[-1]["total_value"] if snapshots else INITIAL_CAPITAL
    daily_gain = ((total_now / yesterday_val) - 1) * 100

    # Let the system decide its own WIN-LOCK threshold
    winlock = _get_dynamic_winlock()
    winlock_threshold = winlock["lock_pct"]

    if daily_gain >= winlock_threshold:
        logger.warning(f"EXIT CHECKER WIN-LOCK: Up {daily_gain:+.1f}% (threshold: {winlock_threshold}%) — SELLING ALL! | {winlock['reason']}")
        for trade in open_trades:
            ticker = trade["ticker"]
            price = current_prices.get(ticker)
            if price:
                try:
                    close_paper_trade(trade["id"], price)
                    direction = trade["direction"]
                    entry_price = trade["entry_price"]
                    if direction == "long":
                        pnl_pct = ((price / entry_price) - 1) * 100
                    else:
                        pnl_pct = ((entry_price / price) - 1) * 100
                    closed.append({
                        "ticker": ticker, "direction": direction,
                        "entry_price": entry_price, "exit_price": round(price, 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "reason": f"WIN-LOCK: daily gain {daily_gain:+.1f}% ≥ {winlock_threshold}% | {winlock['reason']}",
                    })
                except Exception as e:
                    logger.error(f"WIN-LOCK close {ticker}: {e}")
        cash = get_cash()
        total_value = cash
        cum_ret = ((total_value / ORIGINAL_CAPITAL) - 1) * 100
        save_portfolio_snapshot(total_value, cash, 0, daily_gain, cum_ret, 0, 0, 0)
        return {"closed": closed, "checked": len(open_trades), "win_lock": True, "winlock_info": winlock}

    for trade in open_trades:
        ticker = trade["ticker"]
        current_price = current_prices.get(ticker)
        if current_price is None:
            continue

        entry_price = trade["entry_price"]
        direction = trade["direction"]
        should_close = False
        close_reason = ""

        # Calculate current P&L
        if direction == "long":
            pnl_pct = ((current_price / entry_price) - 1) * 100
        else:
            pnl_pct = ((entry_price / current_price) - 1) * 100

        # Check stop loss
        stop_loss = trade.get("stop_loss_price", 0)
        if stop_loss and direction == "long" and current_price <= stop_loss:
            should_close = True
            close_reason = f"Stop loss hit (${stop_loss:.2f})"
        elif stop_loss and direction == "short" and current_price >= stop_loss:
            should_close = True
            close_reason = f"Stop loss hit (${stop_loss:.2f})"

        # Check target
        target = trade.get("target_price", 0)
        if target and direction == "long" and current_price >= target:
            should_close = True
            close_reason = f"Target hit (${target:.2f})"
        elif target and direction == "short" and current_price <= target:
            should_close = True
            close_reason = f"Target hit (${target:.2f})"

        # Check hold duration
        try:
            entry_date = datetime.fromisoformat(trade["entry_date"])
            days_held = (datetime.now() - entry_date).days
            max_hold = trade.get("hold_duration_days", DEFAULT_HOLD_DAYS)
            if days_held >= max_hold:
                should_close = True
                close_reason = f"Hold duration expired ({days_held}/{max_hold} days)"
        except Exception:
            pass

        # EARNINGS SHIELD: Close positions if earnings are imminent (next 1 day)
        if not should_close:
            try:
                t = yf.Ticker(ticker)
                cal = t.calendar
                if cal is not None:
                    next_earn = None
                    if isinstance(cal, dict) and cal.get("Earnings Date"):
                        ed = cal["Earnings Date"]
                        next_earn = pd.Timestamp(ed[0] if isinstance(ed, list) else ed)
                    elif isinstance(cal, pd.DataFrame) and not cal.empty:
                        next_earn = pd.Timestamp(cal.iloc[0, 0]) if len(cal.columns) > 0 else None
                    if next_earn:
                        if next_earn.tzinfo is not None:
                            next_earn = next_earn.tz_localize(None)
                        days_to_earn = (next_earn - pd.Timestamp.now()).days
                        if 0 <= days_to_earn <= 1:
                            should_close = True
                            close_reason = f"EARNINGS SHIELD: earnings in {days_to_earn} day(s) — closing to avoid gap risk"
            except Exception:
                pass

        # BEAR regime protection: close losing longs at -4% (was -2% — too tight)
        if not should_close and regime == "BEAR" and direction == "long" and pnl_pct < -4:
            should_close = True
            close_reason = f"BEAR regime protection — losing long ({pnl_pct:+.1f}%)"

        # QUICK-CUT RULE: Shorts losing >3% in first 2 days = bad trade
        if not should_close and direction == "short" and pnl_pct < -3:
            try:
                entry_date = datetime.fromisoformat(trade["entry_date"])
                days_held = (datetime.now() - entry_date).days
                if days_held <= 2:
                    should_close = True
                    close_reason = f"QUICK-CUT: short losing {pnl_pct:+.1f}% in {days_held} days"
            except Exception:
                pass

        # SHORTS MAX LOSS: Never let a short lose more than 5%
        if not should_close and direction == "short" and pnl_pct < -5:
            should_close = True
            close_reason = f"SHORT MAX LOSS: down {pnl_pct:+.1f}% — hard cap"

        # AUTONOMOUS TRAILING PROFIT PROTECTION (ATR-based, per-stock)
        stock_atr = _calculate_stock_atr(ticker)
        trail_start_pct = stock_atr * 100 * 2  # Start trailing at 2x daily ATR
        trail_start_pct = max(3.0, min(trail_start_pct, 12.0))  # Clamp 3-12%

        if not should_close and pnl_pct > trail_start_pct:
            try:
                _throttle()
                hist_df = yf.download(ticker, period="1mo", progress=False)
                if hist_df is not None and len(hist_df) >= 3:
                    hist_closes = _safe_col(hist_df, "Close").values.astype(float)
                    if direction == "long":
                        peak_price = float(np.max(hist_closes))
                        peak_pnl = ((peak_price / entry_price) - 1) * 100
                    else:
                        trough_price = float(np.min(hist_closes))
                        peak_pnl = ((entry_price / trough_price) - 1) * 100

                    if peak_pnl >= 20:
                        trail_pct = 0.65
                    elif peak_pnl >= 12:
                        trail_pct = 0.55
                    else:
                        trail_pct = 0.45
                    trail_level = peak_pnl * trail_pct
                    if peak_pnl >= trail_start_pct and pnl_pct < trail_level:
                        should_close = True
                        close_reason = (
                            f"SMART TRAIL: ATR={stock_atr*100:.1f}%, peaked at {peak_pnl:+.1f}%, "
                            f"trail at {trail_level:+.1f}%, now {pnl_pct:+.1f}%"
                        )
            except Exception:
                pass

        # AUTONOMOUS TAKE PROFIT — 20%+ is exceptional, lock it
        if not should_close and pnl_pct >= 20:
            should_close = True
            close_reason = f"AUTO PROFIT LOCK: up {pnl_pct:+.1f}% — exceptional gain secured"

        # REMOVED: "SELL AT HIGHS" intraday trigger — killed best trades at +3%

        if should_close:
            try:
                close_paper_trade(trade["id"], current_price)
                # Cash updated atomically in close_paper_trade()
                closed.append({
                    "ticker": ticker,
                    "direction": direction,
                    "entry_price": entry_price,
                    "exit_price": round(current_price, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "reason": close_reason,
                })
                logger.warning(f"EXIT: {ticker} {direction} | {pnl_pct:+.1f}% | {close_reason}")
            except Exception as e:
                logger.error(f"Failed to close {ticker}: {e}")

    # Save snapshot if we closed anything
    if closed:
        try:
            cash = get_cash()  # Refresh atomic cash after all closes
            positions_value = sum(
                current_prices.get(t["ticker"], t["entry_price"]) * t["shares"]
                for t in get_open_trades()
            )
            total_value = cash + positions_value
            cum_ret = ((total_value / ORIGINAL_CAPITAL) - 1) * 100
            save_portfolio_snapshot(total_value, cash, positions_value, 0, cum_ret, 0, 0, len(get_open_trades()))
        except Exception:
            pass

    return {"closed": closed, "checked": len(open_trades)}
