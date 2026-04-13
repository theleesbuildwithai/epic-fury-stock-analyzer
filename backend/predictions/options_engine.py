"""
Options Chain Engine — autonomous strike/expiry selection and strategy decisions.

Handles:
  1. Fetching option chains from Yahoo Finance (with rate limiting)
  2. Smart strike selection (ATM vs OTM based on conviction)
  3. Smart expiration selection (theta buffer based on hold duration)
  4. Strategy decision: when to use options vs equity
  5. Basic Greeks estimation from chain data

All option chain data comes from yfinance Ticker.option_chain().
"""

import yfinance as yf
import numpy as np
import time
import logging
import math
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Rate limiting — shared with quant_engine
_last_options_call = [0.0]
_OPTIONS_DELAY = 1.5  # seconds between yfinance calls


def _throttle():
    """Enforce minimum delay between Yahoo Finance API calls."""
    now = time.time()
    elapsed = now - _last_options_call[0]
    if elapsed < _OPTIONS_DELAY:
        time.sleep(_OPTIONS_DELAY - elapsed)
    _last_options_call[0] = time.time()


# ============================================================
#  1. OPTION CHAIN FETCHING
# ============================================================

def fetch_option_chain(symbol: str, max_expiries: int = 4) -> dict:
    """
    Fetch option chain data for a symbol.
    Returns calls/puts DataFrames for the nearest relevant expiries.
    Returns empty dict if no options available.
    """
    try:
        _throttle()
        ticker = yf.Ticker(symbol)
        expiry_dates = ticker.options  # list of date strings
        if not expiry_dates:
            logger.debug(f"No options available for {symbol}")
            return {}

        # Filter to expiries between 14 and 90 DTE
        today = datetime.now().date()
        valid_expiries = []
        for exp_str in expiry_dates:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            dte = (exp_date - today).days
            if 14 <= dte <= 90:
                valid_expiries.append({"date": exp_str, "dte": dte})

        if not valid_expiries:
            # Fallback: take the nearest expiry that's at least 7 DTE
            for exp_str in expiry_dates:
                exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
                dte = (exp_date - today).days
                if dte >= 7:
                    valid_expiries.append({"date": exp_str, "dte": dte})
                    break

        if not valid_expiries:
            logger.debug(f"No valid expiries for {symbol} (all too close or too far)")
            return {}

        # Fetch chains for the closest expiries (limit API calls)
        chains = []
        for exp_info in valid_expiries[:max_expiries]:
            try:
                _throttle()
                chain = ticker.option_chain(exp_info["date"])
                chains.append({
                    "expiry": exp_info["date"],
                    "dte": exp_info["dte"],
                    "calls": chain.calls,
                    "puts": chain.puts,
                })
            except Exception as e:
                logger.debug(f"Failed to fetch chain for {symbol} {exp_info['date']}: {e}")
                continue

        if not chains:
            return {}

        return {
            "symbol": symbol,
            "expiries": [c["expiry"] for c in chains],
            "chains": chains,
        }
    except Exception as e:
        logger.debug(f"Option chain fetch failed for {symbol}: {e}")
        return {}


# ============================================================
#  2. STRIKE SELECTION
# ============================================================

def select_strike(chain_data: dict, current_price: float, option_type: str,
                  conviction: float, composite_score: float) -> dict:
    """
    Select the best strike price for a call or put.

    - High conviction (score >= 4, confidence >= 70): ATM strike
    - Moderate conviction: 1-2 strikes OTM for cheaper premium
    - option_type: 'call' or 'put'

    Returns: {strike, premium, delta_est, iv, expiry, dte, moneyness}
    """
    if not chain_data or not chain_data.get("chains"):
        return {}
    if current_price <= 0:
        return {}

    abs_score = abs(composite_score)
    high_conviction = conviction >= 70 and abs_score >= 4.0

    best_pick = None
    best_score = -999

    for chain in chain_data["chains"]:
        df = chain["calls"] if option_type == "call" else chain["puts"]
        if df is None or df.empty:
            continue

        dte = chain["dte"]
        expiry = chain["expiry"]

        for _, row in df.iterrows():
            strike = row.get("strike", 0)
            bid = row.get("bid", 0)
            ask = row.get("ask", 0)
            volume = row.get("volume", 0) or 0
            open_interest = row.get("openInterest", 0) or 0
            iv = row.get("impliedVolatility", 0) or 0
            last_price = row.get("lastPrice", 0) or 0

            # Use mid price, fallback to last price
            if bid > 0 and ask > 0:
                premium = round((bid + ask) / 2, 2)
            elif last_price > 0:
                premium = last_price
            else:
                continue  # No usable price

            # Skip very illiquid options
            if premium < 0.10:
                continue
            if volume < 5 and open_interest < 50:
                continue

            # Calculate moneyness
            if option_type == "call":
                moneyness = (strike - current_price) / current_price
                # OTM calls: strike > price (moneyness > 0)
                # ATM: moneyness ≈ 0
                # ITM: moneyness < 0
            else:
                moneyness = (current_price - strike) / current_price
                # OTM puts: strike < price (moneyness > 0)
                # ATM: moneyness ≈ 0
                # ITM: moneyness < 0

            # Scoring: prefer ATM for high conviction, slightly OTM for moderate
            if high_conviction:
                # Prefer ATM (moneyness close to 0)
                target_moneyness = 0.0
                moneyness_penalty = abs(moneyness - target_moneyness) * 10
            else:
                # Prefer slightly OTM (2-5% out)
                target_moneyness = 0.03
                moneyness_penalty = abs(moneyness - target_moneyness) * 8

            # Penalty for deep ITM (expensive, less leverage)
            if moneyness < -0.05:
                moneyness_penalty += 5

            # Penalty for deep OTM (low delta, lottery ticket)
            if moneyness > 0.10:
                moneyness_penalty += 5

            # Prefer moderate DTE (30-60 days sweet spot)
            dte_score = -abs(dte - 45) / 30

            # Prefer higher liquidity
            liquidity_score = min(1.0, (volume + open_interest / 10) / 500)

            # Reasonable spread (bid-ask)
            if bid > 0 and ask > 0:
                spread_pct = (ask - bid) / ask
                if spread_pct > 0.20:
                    continue  # Too wide, skip

            total_score = -moneyness_penalty + dte_score + liquidity_score

            if total_score > best_score:
                best_score = total_score
                # Estimate delta from moneyness (rough Black-Scholes approximation)
                if option_type == "call":
                    delta_est = max(0.05, min(0.95, 0.5 - moneyness * 5))
                else:
                    delta_est = max(-0.95, min(-0.05, -0.5 + moneyness * 5))

                best_pick = {
                    "strike": strike,
                    "premium": premium,
                    "delta_est": round(delta_est, 2),
                    "iv": round(iv, 4),
                    "expiry": expiry,
                    "dte": dte,
                    "moneyness": round(moneyness, 4),
                    "moneyness_label": "ATM" if abs(moneyness) < 0.02 else ("OTM" if moneyness > 0 else "ITM"),
                    "volume": volume,
                    "open_interest": open_interest,
                }

    return best_pick or {}


# ============================================================
#  3. EXPIRATION SELECTION
# ============================================================

def select_expiration(chain_data: dict, hold_days: int, signal_strength: float) -> str:
    """
    Select the best expiration date.

    - Target: hold_days * 1.5 to 2.0 (theta buffer)
    - Stronger signals get closer expiries (cheaper, more gamma)
    - Min 14 DTE, Max 90 DTE

    Returns expiry date string or empty string.
    """
    if not chain_data or not chain_data.get("chains"):
        return ""

    # Stronger signals = closer expiry (more gamma, cheaper premium)
    if signal_strength >= 4.0:
        target_dte = max(14, int(hold_days * 1.3))
    elif signal_strength >= 3.0:
        target_dte = max(14, int(hold_days * 1.5))
    else:
        target_dte = max(14, int(hold_days * 2.0))

    target_dte = min(target_dte, 90)

    # Find closest expiry to target
    best_expiry = None
    best_diff = 999
    for chain in chain_data["chains"]:
        diff = abs(chain["dte"] - target_dte)
        if diff < best_diff:
            best_diff = diff
            best_expiry = chain["expiry"]

    return best_expiry or ""


# ============================================================
#  4. OPTIONS vs EQUITY DECISION
# ============================================================

def should_use_options(pick: dict, regime: str, portfolio_state: dict,
                       open_trades: list = None) -> dict:
    """
    Decide whether to use options instead of equity for a given pick.

    Returns: {
        use_options: bool,
        strategy: 'buy_call' | 'buy_put' | 'sell_covered_call' | 'sell_cash_secured_put',
        rationale: str
    }
    """
    direction = pick.get("direction", "long")
    confidence = pick.get("confidence", 50)
    composite = pick.get("composite_score", 0)
    ticker = pick.get("ticker", "")
    abs_score = abs(composite)

    result = {"use_options": False, "strategy": None, "rationale": ""}

    # Check if options exposure is already maxed
    try:
        from predictions.models import get_options_exposure, get_cash
        opts_exp = get_options_exposure()
        total_value = portfolio_state.get("total_value", 109000)
        opts_pct = opts_exp["total_premium_deployed"] / total_value if total_value > 0 else 0
        if opts_pct >= 0.15:
            result["rationale"] = "Options exposure at limit (15%)"
            return result
    except Exception:
        pass

    # --- Strategy 1: Buy calls on high-conviction longs ---
    if direction == "long" and confidence >= 65 and abs_score >= 3.5:
        result["use_options"] = True
        result["strategy"] = "buy_call"
        result["rationale"] = f"High conviction long (score={composite:.1f}, conf={confidence}%) — buy call for leveraged upside with defined risk"
        return result

    # --- Strategy 2: Buy puts on high-conviction shorts ---
    if direction == "short" and confidence >= 65 and abs_score >= 3.5:
        result["use_options"] = True
        result["strategy"] = "buy_put"
        result["rationale"] = f"High conviction short (score={composite:.1f}, conf={confidence}%) — buy put for defined-risk downside bet (no margin)"
        return result

    # --- Strategy 3: Buy calls instead of equity in BEAR regime (defined risk) ---
    if direction == "long" and regime == "BEAR" and confidence >= 55 and abs_score >= 2.5:
        result["use_options"] = True
        result["strategy"] = "buy_call"
        result["rationale"] = f"BEAR regime long — using call for defined risk instead of equity exposure"
        return result

    # --- Strategy 4: Sell covered call on existing profitable long ---
    if open_trades and direction == "long" and abs_score < 2.0:
        # Check if we already hold this stock as a profitable long
        for trade in (open_trades or []):
            if (trade.get("ticker") == ticker and
                trade.get("direction") == "long" and
                trade.get("instrument_type", "equity") == "equity" and
                trade.get("status") == "open"):
                unrealized_pct = trade.get("unrealized_pct", 0)
                if unrealized_pct > 5:
                    result["use_options"] = True
                    result["strategy"] = "sell_covered_call"
                    result["rationale"] = f"Existing long +{unrealized_pct:.1f}% with weakening signal — sell covered call for income"
                    result["covered_trade_id"] = trade.get("trade_id") or trade.get("id")
                    return result

    # --- Strategy 5: Sell cash-secured put on mild bullish ---
    if direction == "long" and 1.0 <= abs_score <= 2.0 and confidence >= 50:
        cash = portfolio_state.get("cash", 0)
        # Need enough cash to secure the put (strike * 100)
        price = pick.get("price", 0) or pick.get("entry_price", 0)
        if price > 0 and cash >= price * 100:
            result["use_options"] = True
            result["strategy"] = "sell_cash_secured_put"
            result["rationale"] = f"Mild bullish (score={composite:.1f}) — sell cash-secured put to collect premium or own at discount"
            return result

    # Default: use equity
    result["rationale"] = f"Standard signal (score={composite:.1f}, conf={confidence}%) — equity trade"
    return result


# ============================================================
#  5. GET CURRENT OPTION PREMIUM (for portfolio valuation)
# ============================================================

def get_current_premium(symbol: str, strike: float, expiry: str,
                        option_type: str) -> float:
    """
    Fetch the current premium for a specific option contract.
    Returns mid price, or 0 if not found.
    """
    try:
        _throttle()
        ticker = yf.Ticker(symbol)
        chain = ticker.option_chain(expiry)
        df = chain.calls if option_type == "call" else chain.puts

        if df is None or df.empty:
            return 0.0

        # Find matching strike
        row = df[df["strike"] == strike]
        if row.empty:
            # Find closest strike
            diffs = abs(df["strike"] - strike)
            closest_idx = diffs.idxmin()
            row = df.loc[[closest_idx]]

        if not row.empty:
            r = row.iloc[0]
            bid = r.get("bid", 0) or 0
            ask = r.get("ask", 0) or 0
            if bid > 0 and ask > 0:
                return round((bid + ask) / 2, 2)
            return r.get("lastPrice", 0) or 0

        return 0.0
    except Exception as e:
        logger.debug(f"Failed to get current premium for {symbol} {strike} {expiry}: {e}")
        return 0.0


# ============================================================
#  6. OPTIONS EXIT LOGIC
# ============================================================

def check_option_exit(trade: dict, current_premium: float) -> dict:
    """
    Check if an option position should be closed.

    Returns: {should_exit: bool, reason: str, exit_price: float}
    """
    instrument_type = trade.get("instrument_type", "equity")
    if instrument_type not in ("call", "put"):
        return {"should_exit": False}

    entry_premium = trade.get("premium_per_contract") or trade.get("entry_price", 0)
    direction = trade.get("direction", "long")
    expiry_str = trade.get("expiration_date", "")

    # Calculate DTE
    dte = 999
    if expiry_str:
        try:
            exp_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
            dte = (exp_date - datetime.now().date()).days
        except Exception:
            pass
    else:
        # Missing expiration date — force exit to avoid orphaned position
        return {
            "should_exit": True,
            "reason": "MISSING EXPIRATION: no expiration_date on option — force closing",
            "exit_price": current_premium,
        }

    # --- Exit Rule 1: Auto-close at DTE <= 2 (never exercise) ---
    if dte <= 2:
        return {
            "should_exit": True,
            "reason": f"EXPIRY GUARD: {dte} DTE remaining — auto-closing to avoid expiration risk",
            "exit_price": current_premium,
        }

    # --- Exit Rule 2: Theta decay exit at DTE <= 5 ---
    if direction == "long" and dte <= 5:
        pnl_pct = ((current_premium - entry_premium) / entry_premium * 100) if entry_premium > 0 else 0
        if pnl_pct < 20:
            return {
                "should_exit": True,
                "reason": f"THETA DECAY: {dte} DTE with only {pnl_pct:.1f}% gain — closing before accelerating decay",
                "exit_price": current_premium,
            }

    # --- Exit Rule 3: Premium stop loss (50% drop for longs) ---
    if direction == "long" and entry_premium > 0:
        pnl_pct = ((current_premium - entry_premium) / entry_premium * 100)
        if pnl_pct <= -50:
            return {
                "should_exit": True,
                "reason": f"OPTIONS STOP: Premium down {pnl_pct:.1f}% — cutting loss at 50% threshold",
                "exit_price": current_premium,
            }

    # --- Exit Rule 4: Profit lock (100%+ gain for longs) ---
    if direction == "long" and entry_premium > 0:
        pnl_pct = ((current_premium - entry_premium) / entry_premium * 100)
        if pnl_pct >= 100:
            return {
                "should_exit": True,
                "reason": f"OPTIONS PROFIT LOCK: Premium up {pnl_pct:.1f}% — locking in 100%+ gain",
                "exit_price": current_premium,
            }

    # --- Exit Rule 5: Sold option profit lock (premium dropped 70%+) ---
    if direction == "short" and entry_premium > 0:
        pnl_pct = ((entry_premium - current_premium) / entry_premium * 100)
        if pnl_pct >= 70:
            return {
                "should_exit": True,
                "reason": f"SOLD OPTION WIN: Premium decayed {pnl_pct:.1f}% — closing to lock in income",
                "exit_price": current_premium,
            }

    # --- Exit Rule 6: Sold option stop loss (premium doubled against us) ---
    if direction == "short" and entry_premium > 0:
        pnl_pct = ((entry_premium - current_premium) / entry_premium * 100)
        if pnl_pct <= -100:
            return {
                "should_exit": True,
                "reason": f"SOLD OPTION STOP: Premium moved against us {pnl_pct:.1f}% — cutting loss",
                "exit_price": current_premium,
            }

    return {"should_exit": False}
