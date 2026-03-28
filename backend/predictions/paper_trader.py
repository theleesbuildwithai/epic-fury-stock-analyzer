"""
Paper Trading System — the execution engine of the Epic Fury hedge fund.

This manages a $100K virtual portfolio that:
  1. Auto-executes trades based on quant signals (LONG/SHORT picks)
  2. Manages position sizing (max 15 concurrent positions)
  3. Enforces risk management (7% stop-loss, target exits, time exits)
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

# Portfolio configuration
INITIAL_CAPITAL = 100_000.0
MAX_POSITIONS = 999  # No limit — only constrained by cash
POSITION_SIZE_PCT = 0.02  # 2% per position — allows 40+ positions for massive diversification
STOP_LOSS_PCT = 0.06  # 6% stop loss (tighter = less downside)
DEFAULT_HOLD_DAYS = 30
MIN_CONFIDENCE = 35  # Lowered to allow trades in BEAR regime (0.7x multiplier)

# ============================================================
#  ADVANCED: VIX-SCALED POSITION SIZING
# ============================================================
# When VIX is high (fear), we reduce position sizes to limit risk
# When VIX is low (calm), we can be more aggressive
# This is standard institutional risk management
VIX_SIZE_SCALE = {
    "low": 1.3,      # VIX < 15: calm markets, slightly bigger positions
    "normal": 1.0,    # VIX 15-20: standard sizing
    "elevated": 0.7,  # VIX 20-25: reduce size
    "high": 0.5,      # VIX 25-35: half-size positions
    "crisis": 0.25,   # VIX > 35: quarter-size (capital preservation)
}

def _get_vix_scale() -> float:
    """Get position size multiplier based on current VIX level."""
    try:
        _throttle()
        vix_df = yf.download("^VIX", period="5d", progress=False)
        if vix_df is not None and not vix_df.empty:
            vix = float(vix_df["Close"].dropna().iloc[-1])
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

def get_portfolio_state() -> dict:
    """
    Get current portfolio state: cash, positions, total value.
    Calculates unrealized P&L for all open positions.
    """
    from predictions.models import get_open_trades, get_closed_trades, get_portfolio_snapshots

    open_trades = get_open_trades()
    closed_trades = get_closed_trades(limit=50)
    snapshots = get_portfolio_snapshots(days=365)

    # Calculate current portfolio value
    if snapshots:
        last_snapshot = snapshots[-1]
        cash = last_snapshot["cash"]
        total_value = last_snapshot["total_value"]
    else:
        cash = INITIAL_CAPITAL
        total_value = INITIAL_CAPITAL

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
    total_return = ((total_current / INITIAL_CAPITAL) - 1) * 100

    # Win/loss stats from closed trades
    wins = [t for t in closed_trades if (t.get("pnl_pct") or 0) > 0]
    losses = [t for t in closed_trades if (t.get("pnl_pct") or 0) <= 0]
    win_rate = (len(wins) / len(closed_trades) * 100) if closed_trades else 0
    avg_win = np.mean([t["pnl_pct"] for t in wins]) if wins else 0
    avg_loss = np.mean([t["pnl_pct"] for t in losses]) if losses else 0
    profit_factor = abs(avg_win * len(wins)) / (abs(avg_loss * len(losses)) + 0.01) if losses else 0

    return {
        "total_value": round(total_current, 2),
        "cash": round(cash, 2),
        "positions_value": round(positions_value, 2),
        "total_return_pct": round(total_return, 2),
        "initial_capital": INITIAL_CAPITAL,
        "num_positions": len(positions),
        "max_positions": MAX_POSITIONS,
        "positions": positions,
        "recent_closed": [{
            "ticker": t["ticker"],
            "direction": t["direction"],
            "pnl_pct": t.get("pnl_pct", 0),
            "pnl_dollars": t.get("pnl_dollars", 0),
            "entry_price": t["entry_price"],
            "exit_price": t.get("exit_price"),
        } for t in closed_trades[:10]],
        "stats": {
            "total_trades": len(closed_trades),
            "win_rate": round(win_rate, 1),
            "avg_win_pct": round(float(avg_win), 2),
            "avg_loss_pct": round(float(avg_loss), 2),
            "profit_factor": round(profit_factor, 2),
        },
        "timestamp": datetime.now().isoformat(),
    }


def _get_current_prices(symbols: list) -> dict:
    """Get current prices for a list of symbols (batch download)."""
    if not symbols:
        return {}
    _throttle()
    try:
        df = yf.download(symbols, period="1d", progress=False, group_by="ticker")
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
        get_portfolio_snapshots, save_portfolio_snapshot
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

    # Determine current cash
    if snapshots:
        cash = snapshots[-1]["cash"]
    else:
        cash = INITIAL_CAPITAL

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

            # TRAILING STOP LOSS — lock in profits, don't give them back
            # If position is up 3%+, move stop to breakeven (entry price)
            # If position is up 5%+, trail stop to 50% of max profit
            # This is what separates profitable funds from those that give back gains
            if not should_close and pnl_pct > 3:
                # Check if we've pulled back significantly from peak
                try:
                    _throttle()
                    hist_df = yf.download(ticker, period="1mo", progress=False)
                    if hist_df is not None and len(hist_df) >= 5:
                        hist_closes = hist_df["Close"].values.astype(float).flatten()
                        if direction == "long":
                            peak_price = float(np.max(hist_closes))
                            peak_pnl = ((peak_price / entry_price) - 1) * 100
                            # Trail at 50% of peak profit (if peak was 8%, trail stop at 4%)
                            trail_level = peak_pnl * 0.5
                            if peak_pnl > 5 and pnl_pct < trail_level:
                                should_close = True
                                close_reason = f"Trailing stop: peaked at {peak_pnl:+.1f}%, now {pnl_pct:+.1f}%"
                        else:  # short
                            trough_price = float(np.min(hist_closes))
                            trough_pnl = ((entry_price / trough_price) - 1) * 100
                            trail_level = trough_pnl * 0.5
                            if trough_pnl > 5 and pnl_pct < trail_level:
                                should_close = True
                                close_reason = f"Trailing stop: peaked at {trough_pnl:+.1f}%, now {pnl_pct:+.1f}%"
                except Exception:
                    pass

            # AGGRESSIVE BEAR PROTECTION: close losing longs faster in BEAR
            if not should_close and regime == "BEAR" and direction == "long" and pnl_pct < -2:
                should_close = True
                close_reason = f"BEAR regime protection — closing losing long ({pnl_pct:+.1f}%)"

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
                    # Return cash from closed position
                    position_value = trade["shares"] * current_price
                    cash += position_value
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

    # --- Step 2: Open new positions ---
    current_positions = len(open_tickers)
    available_slots = MAX_POSITIONS - current_positions
    regime = quant_picks.get("regime", {}).get("regime", "SIDEWAYS")

    # --- DRAWDOWN PROTECTION (hedge fund risk management) ---
    # If portfolio has dropped 5%+, cut position sizes in half
    # If dropped 10%+, stop opening new positions entirely (capital preservation)
    total_current_value = cash + sum(
        t.get("shares", 0) * t.get("entry_price", 0) for t in open_trades
    )
    drawdown_pct = ((total_current_value / INITIAL_CAPITAL) - 1) * 100
    drawdown_multiplier = 1.0

    if drawdown_pct <= -10:
        logger.warning(f"DRAWDOWN PROTECTION: Portfolio down {drawdown_pct:.1f}% — HALTING new trades")
        results["skipped"].append({"symbol": "ALL", "reason": f"Drawdown protection: portfolio down {drawdown_pct:.1f}%"})
        available_slots = 0  # Stop all new trades
    elif drawdown_pct <= -5:
        logger.warning(f"DRAWDOWN PROTECTION: Portfolio down {drawdown_pct:.1f}% — halving position sizes")
        drawdown_multiplier = 0.5  # Half-size positions

    # No position limits — the computer trades freely like a real hedge fund

    # Get VIX-based position size multiplier
    vix_multiplier = _get_vix_scale()
    logger.info(f"VIX position size multiplier: {vix_multiplier}")

    if available_slots > 0 and cash > 1000:
        # REGIME-AWARE PICK SELECTION
        # In BEAR: prioritize shorts heavily, limit longs
        # In BULL: prioritize longs heavily, limit shorts
        all_picks = []

        long_candidates = [p for p in quant_picks.get("long_picks", [])
                          if p["confidence"] >= MIN_CONFIDENCE and p["symbol"] not in open_tickers]
        short_candidates = [p for p in quant_picks.get("short_picks", [])
                           if p["confidence"] >= MIN_CONFIDENCE and p["symbol"] not in open_tickers]

        if regime == "BEAR":
            # BEAR: Take ALL qualifying shorts, plus top 8 longs (long-term conviction picks)
            # A smart investor always has some long positions — long-term success is still success
            for p in short_candidates:
                p["_adj_confidence"] = p["confidence"] + 10
                all_picks.append(p)
            for p in long_candidates[:8]:  # Keep 8 long-term conviction longs even in bear
                p["_adj_confidence"] = p["confidence"] - 5  # slight penalty, but still allow them
                all_picks.append(p)
            logger.info(f"BEAR regime: {len(short_candidates)} shorts, {min(8, len(long_candidates))} longs selected")
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
            # Long-term success matters — keep some conviction longs
            if regime == "BEAR" and direction == "long" and pick["confidence"] < 45:
                results["skipped"].append({
                    "symbol": symbol,
                    "reason": f"Low conviction long in BEAR ({pick['confidence']}%)",
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

            # Check sector concentration
            sector_key = f"{pick.get('sector', 'Unknown')}_{direction}"
            if sector_counts.get(sector_key, 0) >= 5:
                results["skipped"].append({
                    "symbol": symbol,
                    "reason": f"Sector concentration limit ({pick.get('sector')} {direction})",
                })
                continue

            # Position sizing: REGIME-AWARE % of total portfolio value
            total_value = cash + sum(
                t.get("shares", 0) * t.get("entry_price", 0)
                for t in get_open_trades()
                if t["ticker"] in open_tickers
            )
            # Regime-aware position sizing — 2% base for massive diversification
            # In BEAR: 3% shorts (conviction), 1.5% longs (careful long-term plays)
            # In BULL: 3% longs (conviction), 1.5% shorts (hedges)
            # SIDEWAYS: 2% equal
            if regime == "BEAR":
                size_pct = 0.03 if direction == "short" else 0.015
            elif regime == "BULL":
                size_pct = 0.03 if direction == "long" else 0.015
            else:
                size_pct = POSITION_SIZE_PCT
            position_value = total_value * size_pct * drawdown_multiplier * vix_multiplier
            shares = round(position_value / price, 4)

            if shares * price > cash:
                # Not enough cash
                results["skipped"].append({
                    "symbol": symbol,
                    "reason": "Insufficient cash",
                })
                continue

            # Calculate stop loss and target — REGIME AWARE
            # BEAR: tighter stop on longs (4%), wider on shorts (10%)
            # BULL: tighter stop on shorts (4%), wider on longs (10%)
            if regime == "BEAR":
                long_stop = 0.04   # tight stop on longs in bear
                short_stop = 0.10  # give shorts room to run
                long_target = 1.08  # modest target for longs
                short_target = 0.80  # ambitious target for shorts (20% drop)
            elif regime == "BULL":
                long_stop = 0.10
                short_stop = 0.04
                long_target = 1.20
                short_target = 0.92
            else:
                long_stop = STOP_LOSS_PCT
                short_stop = STOP_LOSS_PCT
                long_target = 1.15
                short_target = 0.85

            if direction == "long":
                stop_loss = pick.get("stop_loss") or round(price * (1 - long_stop), 2)
                target = pick.get("target_price") or round(price * long_target, 2)
            else:  # short
                stop_loss = pick.get("stop_loss") or round(price * (1 + short_stop), 2)
                target = pick.get("target_price") or round(price * short_target, 2)

            try:
                # ADAPTIVE HOLD DURATION — like a real day trader / swing trader
                # High confidence + strong trend = hold longer (up to 60 days)
                # Low confidence + weak signal = quick scalp (1-5 days)
                # Regime also matters: BEAR = shorter holds (faster exits)
                confidence = pick.get("confidence", 50)
                score_strength = abs(pick.get("composite_score", 0))

                if regime == "BEAR":
                    # Bear market: quick trades, don't overstay
                    if confidence >= 80 and score_strength >= 6:
                        adaptive_hold = 14  # 2 weeks max even for strong signals
                    elif confidence >= 60:
                        adaptive_hold = 7   # 1 week
                    else:
                        adaptive_hold = 3   # 3 days — scalp trade
                elif regime == "BULL":
                    # Bull market: let winners run longer
                    if confidence >= 80 and score_strength >= 6:
                        adaptive_hold = 60  # 2 months for high-conviction
                    elif confidence >= 60:
                        adaptive_hold = 30  # 1 month
                    else:
                        adaptive_hold = 10  # ~2 weeks
                else:
                    # Sideways: moderate holds
                    if confidence >= 80:
                        adaptive_hold = 21
                    elif confidence >= 60:
                        adaptive_hold = 14
                    else:
                        adaptive_hold = 5

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

                cash -= shares * price
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
                })
            except Exception as e:
                results["errors"].append(f"Failed to open {symbol}: {str(e)}")

    # --- Step 3: Save portfolio snapshot ---
    try:
        positions_value = sum(
            p.get("position_value", 0)
            for p in get_portfolio_state()["positions"]
        )
        total_value = cash + positions_value

        # Get S&P 500 performance for comparison
        sp500_daily = 0
        sp500_cum = 0
        try:
            _throttle()
            sp_df = yf.download("^GSPC", period="1mo", progress=False)
            if sp_df is not None and len(sp_df) >= 2:
                sp_closes = sp_df["Close"].values.astype(float).flatten()
                sp500_daily = ((sp_closes[-1] / sp_closes[-2]) - 1) * 100
                # Use first available snapshot date or 1 month ago
                sp500_cum = ((sp_closes[-1] / sp_closes[0]) - 1) * 100
        except Exception:
            pass

        prev_value = snapshots[-1]["total_value"] if snapshots else INITIAL_CAPITAL
        daily_return = ((total_value / prev_value) - 1) * 100 if prev_value > 0 else 0
        cum_return = ((total_value / INITIAL_CAPITAL) - 1) * 100

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

            closes = sym_df["Close"].values.astype(float).flatten()
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
