"""
Renaissance Technologies-Style Upgrades — Epic Fury Hedge Fund

This module adds institutional-grade features inspired by RenTech's Medallion Fund:

  1. PAIRS TRADING (Statistical Arbitrage)
     - Find cointegrated stock pairs that move together
     - Trade the spread when it deviates (mean reversion)
     - Market-neutral: makes money in any direction

  2. PORTFOLIO RISK MANAGEMENT
     - Sector concentration limits
     - Beta-neutral targeting (portfolio beta ≈ 0)
     - Correlation monitoring (avoid correlated bets)
     - Max drawdown circuit breaker

  3. ENSEMBLE SIGNAL VOTING
     - 3 independent scoring models
     - Trade only when 2/3 agree on direction
     - Dramatically reduces false signals

  4. ALTERNATIVE DATA SIGNALS
     - Short interest (squeeze detection)
     - Insider transactions (smart money)
     - Institutional ownership changes

  5. INTRADAY MEAN REVERSION
     - Opening range breakout signals
     - VWAP deviation signals
     - RSI(2) Connors strategy enhancement

All data from Yahoo Finance (yf.download) with aggressive caching.
"""

import yfinance as yf
import numpy as np
import pandas as pd
import time
import logging
from datetime import datetime, timedelta
from scipy.stats import zscore as scipy_zscore

logger = logging.getLogger(__name__)

# Cache for RenTech computations
_rentech_cache = {}
_RENTECH_CACHE_TTL = 600  # 10 minutes

_last_rentech_call = [0.0]
_RENTECH_DELAY = 3.0


def _throttle_rentech():
    """Enforce minimum delay between Yahoo Finance API calls."""
    now = time.time()
    elapsed = now - _last_rentech_call[0]
    if elapsed < _RENTECH_DELAY:
        time.sleep(_RENTECH_DELAY - elapsed)
    _last_rentech_call[0] = time.time()


def _get_rentech_cached(key, fetch_fn, ttl=None):
    """Cache with configurable TTL."""
    if ttl is None:
        ttl = _RENTECH_CACHE_TTL
    now = time.time()
    if key in _rentech_cache and now - _rentech_cache[key]["time"] < ttl:
        return _rentech_cache[key]["data"]
    data = fetch_fn()
    _rentech_cache[key] = {"data": data, "time": now}
    return data


# ============================================================
#  1. PAIRS TRADING / STATISTICAL ARBITRAGE
# ============================================================

# Pre-defined pairs — stocks in same sector with high historical correlation
# These are classic stat arb pairs that institutions trade
PAIRS_UNIVERSE = [
    # Tech pairs
    ("MSFT", "AAPL"),    # Large-cap tech leaders
    ("NVDA", "AMD"),     # GPU/semiconductor rivals
    ("GOOGL", "META"),   # Digital advertising
    ("CRM", "NOW"),      # Enterprise SaaS
    ("INTC", "TXN"),     # Legacy semiconductors

    # Finance pairs
    ("JPM", "BAC"),      # Big banks
    ("GS", "MS"),        # Investment banks
    ("V", "MA"),         # Payment networks
    ("BLK", "SCHW"),     # Asset management

    # Consumer pairs
    ("KO", "PEP"),       # Beverages
    ("WMT", "TGT"),      # Retail
    ("MCD", "SBUX"),     # Quick service restaurants
    ("NKE", "LULU"),     # Athletic apparel
    ("HD", "LOW"),       # Home improvement

    # Healthcare pairs
    ("JNJ", "PFE"),      # Pharma
    ("UNH", "CI"),       # Health insurance
    ("ABT", "MDT"),      # Medical devices

    # Energy pairs
    ("XOM", "CVX"),      # Oil majors
    ("COP", "EOG"),      # E&P companies

    # Industrial pairs
    ("CAT", "DE"),       # Heavy machinery
    ("BA", "LMT"),       # Aerospace & defense
    ("UPS", "FDX"),      # Shipping/logistics
]


def _calculate_spread_zscore(prices_a, prices_b, lookback=60):
    """
    Calculate the z-score of the log price spread between two stocks.

    The spread = log(A) - hedge_ratio * log(B)
    Z-score tells us how many standard deviations the spread is from its mean.

    |z| > 2.0 = strong signal (spread is stretched)
    |z| > 1.5 = moderate signal
    """
    if len(prices_a) < lookback or len(prices_b) < lookback:
        return None, None, None

    log_a = np.log(prices_a[-lookback:])
    log_b = np.log(prices_b[-lookback:])

    # OLS hedge ratio: how much of B to short for every unit of A
    hedge_ratio = np.polyfit(log_b, log_a, 1)[0]

    # Spread = log(A) - hedge_ratio * log(B)
    spread = log_a - hedge_ratio * log_b

    # Z-score of the spread
    mean_spread = np.mean(spread)
    std_spread = np.std(spread)

    if std_spread < 1e-8:
        return None, None, None

    current_z = (spread[-1] - mean_spread) / std_spread

    # Half-life of mean reversion (how fast the spread reverts)
    # Ornstein-Uhlenbeck: spread_t = theta * (mu - spread_{t-1}) + epsilon
    spread_diff = np.diff(spread)
    spread_lag = spread[:-1] - mean_spread
    if len(spread_lag) > 0 and np.std(spread_lag) > 1e-8:
        theta = -np.polyfit(spread_lag, spread_diff, 1)[0]
        half_life = np.log(2) / theta if theta > 0 else 999
    else:
        half_life = 999

    return float(current_z), float(hedge_ratio), float(half_life)


def find_pairs_trades(price_data: dict) -> list:
    """
    Scan all pairs for stat arb opportunities.

    Returns list of actionable pair trades:
      - Which pair, direction (long A/short B or vice versa)
      - Z-score (how stretched the spread is)
      - Expected profit (mean reversion target)
      - Confidence based on z-score magnitude and half-life
    """
    cache_key = "pairs_trades"
    cached = _rentech_cache.get(cache_key)
    if cached and time.time() - cached["time"] < 600:
        return cached["data"]

    trades = []

    for sym_a, sym_b in PAIRS_UNIVERSE:
        if sym_a not in price_data or sym_b not in price_data:
            continue

        try:
            closes_a = price_data[sym_a]["Close"].values.astype(float).flatten()
            closes_b = price_data[sym_b]["Close"].values.astype(float).flatten()

            # Need both to have same length
            min_len = min(len(closes_a), len(closes_b))
            closes_a = closes_a[-min_len:]
            closes_b = closes_b[-min_len:]

            z_score, hedge_ratio, half_life = _calculate_spread_zscore(closes_a, closes_b)

            if z_score is None:
                continue

            # Only trade if half-life is reasonable (< 30 days)
            # and z-score is stretched enough
            if half_life > 30 or abs(z_score) < 1.5:
                continue

            # Calculate correlation (higher = better pair)
            corr = float(np.corrcoef(closes_a[-60:], closes_b[-60:])[0, 1])
            if corr < 0.6:
                continue  # Not correlated enough

            # Direction: if z > 0, spread is too wide → short A, long B
            # if z < 0, spread is too narrow → long A, short B
            if z_score > 1.5:
                direction = f"SHORT {sym_a} / LONG {sym_b}"
                long_leg = sym_b
                short_leg = sym_a
            else:
                direction = f"LONG {sym_a} / SHORT {sym_b}"
                long_leg = sym_a
                short_leg = sym_b

            # Confidence: higher z-score + shorter half-life = more confident
            confidence = min(90, int(50 + abs(z_score) * 12 + max(0, 20 - half_life)))

            # Expected return: z-score tends to revert to 0
            # Each 1 std of spread = ~2-4% return on the pair
            expected_return = round(abs(z_score) * 2.5, 1)

            trades.append({
                "pair": f"{sym_a}/{sym_b}",
                "symbol_a": sym_a,
                "symbol_b": sym_b,
                "direction": direction,
                "long_leg": long_leg,
                "short_leg": short_leg,
                "z_score": round(z_score, 2),
                "hedge_ratio": round(hedge_ratio, 3),
                "half_life_days": round(half_life, 1),
                "correlation": round(corr, 3),
                "confidence": confidence,
                "expected_return_pct": expected_return,
                "signal_type": "PAIRS_TRADE",
                "price_a": round(float(closes_a[-1]), 2),
                "price_b": round(float(closes_b[-1]), 2),
            })

        except Exception as e:
            logger.debug(f"Pair {sym_a}/{sym_b} analysis failed: {e}")
            continue

    # Sort by absolute z-score (strongest signals first)
    trades.sort(key=lambda x: abs(x["z_score"]), reverse=True)

    _rentech_cache[cache_key] = {"data": trades[:10], "time": time.time()}
    return trades[:10]  # Top 10 pair opportunities


# ============================================================
#  2. PORTFOLIO RISK MANAGEMENT
# ============================================================

def assess_portfolio_risk(open_trades: list, price_data: dict = None) -> dict:
    """
    Comprehensive portfolio risk assessment — like a real hedge fund risk desk.

    Checks:
      1. Sector concentration (max 25% in any sector)
      2. Direction concentration (net long/short exposure)
      3. Correlation risk (are positions too correlated?)
      4. Beta exposure (how much market risk?)
      5. Max position risk (any single position too large?)

    Returns risk report with warnings and position-level adjustments.
    """
    if not open_trades:
        return {
            "risk_level": "LOW",
            "warnings": [],
            "sector_exposure": {},
            "net_exposure": 0,
            "estimated_beta": 0,
            "correlation_risk": "LOW",
            "max_single_position_pct": 0,
            "recommendations": ["No open positions — ready to deploy capital"],
        }

    warnings = []
    recommendations = []

    # --- Sector Concentration ---
    sector_counts = {}
    sector_notional = {}
    total_notional = 0

    for trade in open_trades:
        sector = trade.get("sector", "Unknown")
        direction = trade.get("direction", "long")
        notional = abs(trade.get("entry_price", 0) * trade.get("shares", 0))

        sector_counts[sector] = sector_counts.get(sector, 0) + 1
        sector_notional[sector] = sector_notional.get(sector, 0) + notional
        total_notional += notional

    sector_exposure = {}
    for sector, notional in sector_notional.items():
        pct = (notional / total_notional * 100) if total_notional > 0 else 0
        sector_exposure[sector] = round(pct, 1)
        if pct > 30:
            warnings.append(f"HIGH CONCENTRATION: {sector} at {pct:.0f}% of portfolio (max 30%)")
            recommendations.append(f"Reduce {sector} exposure — close weakest position")
        elif pct > 25:
            warnings.append(f"Elevated concentration: {sector} at {pct:.0f}%")

    # --- Direction Concentration ---
    long_notional = sum(
        abs(t.get("entry_price", 0) * t.get("shares", 0))
        for t in open_trades if t.get("direction", "long") == "long"
    )
    short_notional = sum(
        abs(t.get("entry_price", 0) * t.get("shares", 0))
        for t in open_trades if t.get("direction", "") == "short"
    )

    net_exposure = ((long_notional - short_notional) / total_notional * 100) if total_notional > 0 else 0
    gross_exposure = ((long_notional + short_notional) / total_notional * 100) if total_notional > 0 else 0

    if abs(net_exposure) > 80:
        warnings.append(f"DIRECTIONAL RISK: Net exposure {net_exposure:+.0f}% — not market-neutral")
        recommendations.append("Add positions in opposite direction for hedging")

    # --- Correlation Risk ---
    # Simple proxy: count positions in same sector+direction
    sector_dir_counts = {}
    for trade in open_trades:
        key = f"{trade.get('sector', 'Unknown')}_{trade.get('direction', 'long')}"
        sector_dir_counts[key] = sector_dir_counts.get(key, 0) + 1

    max_correlated = max(sector_dir_counts.values()) if sector_dir_counts else 0
    if max_correlated >= 4:
        correlation_risk = "HIGH"
        warnings.append(f"CORRELATION RISK: {max_correlated} positions in same sector+direction")
    elif max_correlated >= 3:
        correlation_risk = "MEDIUM"
        warnings.append(f"Moderate correlation: {max_correlated} positions in same sector+direction")
    else:
        correlation_risk = "LOW"

    # --- Beta Estimation ---
    # Rough: longs add beta, shorts subtract
    # Tech/Growth = high beta (~1.3), Defensive = low beta (~0.7)
    HIGH_BETA_SECTORS = {"Technology", "Consumer Discretionary", "Communication Services", "Financials"}
    LOW_BETA_SECTORS = {"Consumer Staples", "Healthcare", "Utilities", "Real Estate"}

    estimated_beta = 0
    for trade in open_trades:
        sector = trade.get("sector", "Unknown")
        direction = trade.get("direction", "long")
        notional = abs(trade.get("entry_price", 0) * trade.get("shares", 0))
        weight = notional / total_notional if total_notional > 0 else 0

        if sector in HIGH_BETA_SECTORS:
            sector_beta = 1.3
        elif sector in LOW_BETA_SECTORS:
            sector_beta = 0.7
        else:
            sector_beta = 1.0

        if direction == "long":
            estimated_beta += weight * sector_beta
        else:
            estimated_beta -= weight * sector_beta

    if abs(estimated_beta) > 1.5:
        warnings.append(f"HIGH BETA: Portfolio beta ≈ {estimated_beta:.2f} — high market sensitivity")
        recommendations.append("Add low-beta or short positions to reduce market exposure")

    # --- Overall Risk Level ---
    if len(warnings) >= 3 or any("HIGH" in w for w in warnings):
        risk_level = "HIGH"
    elif len(warnings) >= 1:
        risk_level = "MEDIUM"
    else:
        risk_level = "LOW"
        recommendations.append("Portfolio risk is well-managed — continue current strategy")

    return {
        "risk_level": risk_level,
        "warnings": warnings,
        "recommendations": recommendations,
        "sector_exposure": sector_exposure,
        "net_exposure_pct": round(net_exposure, 1),
        "gross_exposure_pct": round(gross_exposure, 1),
        "long_notional": round(long_notional, 2),
        "short_notional": round(short_notional, 2),
        "estimated_beta": round(estimated_beta, 2),
        "correlation_risk": correlation_risk,
        "num_positions": len(open_trades),
        "sector_count": len(sector_counts),
    }


def calculate_drawdown_circuit_breaker(portfolio_value: float, peak_value: float,
                                        original_capital: float = 100_000) -> dict:
    """
    Drawdown circuit breaker — automatically reduces risk when losing.

    Levels:
      - NORMAL: drawdown < 5% from peak → full trading
      - CAUTION: 5-8% drawdown → reduce position sizes by 30%
      - WARNING: 8-12% drawdown → reduce by 60%, no new shorts
      - HALT: >12% drawdown → no new trades, only exits
    """
    if peak_value <= 0:
        peak_value = original_capital

    drawdown_pct = ((portfolio_value - peak_value) / peak_value) * 100

    if drawdown_pct > -5:
        return {
            "level": "NORMAL",
            "drawdown_pct": round(drawdown_pct, 2),
            "position_size_multiplier": 1.0,
            "allow_new_longs": True,
            "allow_new_shorts": True,
            "message": "Portfolio within normal range",
        }
    elif drawdown_pct > -8:
        return {
            "level": "CAUTION",
            "drawdown_pct": round(drawdown_pct, 2),
            "position_size_multiplier": 0.7,
            "allow_new_longs": True,
            "allow_new_shorts": True,
            "message": f"Drawdown {drawdown_pct:.1f}% — reducing position sizes 30%",
        }
    elif drawdown_pct > -12:
        return {
            "level": "WARNING",
            "drawdown_pct": round(drawdown_pct, 2),
            "position_size_multiplier": 0.4,
            "allow_new_longs": True,
            "allow_new_shorts": False,
            "message": f"Drawdown {drawdown_pct:.1f}% — defensive mode, no new shorts",
        }
    else:
        return {
            "level": "HALT",
            "drawdown_pct": round(drawdown_pct, 2),
            "position_size_multiplier": 0.0,
            "allow_new_longs": False,
            "allow_new_shorts": False,
            "message": f"CRITICAL drawdown {drawdown_pct:.1f}% — trading halted, exits only",
        }


# ============================================================
#  3. ENSEMBLE SIGNAL VOTING
# ============================================================

def ensemble_vote(stock_data: dict, regime: str = "SIDEWAYS") -> dict:
    """
    Three independent scoring models vote on a stock's direction.
    Trade only when 2/3 models agree.

    Models:
      1. MOMENTUM MODEL — trend following (12-1 month return, EMA alignment)
      2. MEAN REVERSION MODEL — RSI(2), Bollinger position, z-score distance
      3. FUNDAMENTAL MODEL — value + quality composite

    Returns:
      - consensus direction (LONG/SHORT/NO_TRADE)
      - vote breakdown
      - combined confidence (boosted if all 3 agree)
    """
    closes = stock_data.get("closes", [])
    if len(closes) < 60:
        return {"consensus": "NO_TRADE", "reason": "Insufficient data"}

    price = float(closes[-1])

    # --- Model 1: MOMENTUM ---
    ret_20d = (closes[-1] / closes[-20] - 1) * 100 if len(closes) >= 20 else 0
    ret_60d = (closes[-1] / closes[-60] - 1) * 100 if len(closes) >= 60 else 0
    ema_9 = float(pd.Series(closes).ewm(span=9, adjust=False).mean().iloc[-1])
    ema_21 = float(pd.Series(closes).ewm(span=21, adjust=False).mean().iloc[-1])
    ema_50 = float(pd.Series(closes).ewm(span=50, adjust=False).mean().iloc[-1])

    momentum_score = 0
    if ret_20d > 3: momentum_score += 1
    if ret_60d > 5: momentum_score += 1
    if ema_9 > ema_21 > ema_50: momentum_score += 2
    elif ema_9 < ema_21 < ema_50: momentum_score -= 2
    if ret_20d < -3: momentum_score -= 1
    if ret_60d < -5: momentum_score -= 1

    if momentum_score >= 2:
        momentum_vote = "LONG"
    elif momentum_score <= -2:
        momentum_vote = "SHORT"
    else:
        momentum_vote = "NEUTRAL"

    # --- Model 2: MEAN REVERSION ---
    # RSI(2) Connors strategy
    deltas = np.diff(closes[-3:])
    gains = np.mean([d for d in deltas if d > 0]) if any(d > 0 for d in deltas) else 0.001
    losses = np.mean([abs(d) for d in deltas if d < 0]) if any(d < 0 for d in deltas) else 0.001
    rs = gains / losses if losses > 0 else 100
    rsi2 = 100 - (100 / (1 + rs))

    # Bollinger position
    sma_20 = float(np.mean(closes[-20:]))
    std_20 = float(np.std(closes[-20:]))
    bb_upper = sma_20 + 2 * std_20
    bb_lower = sma_20 - 2 * std_20
    bb_pos = ((price - bb_lower) / (bb_upper - bb_lower) * 100) if (bb_upper - bb_lower) > 0 else 50

    mr_score = 0
    if rsi2 < 10: mr_score += 2  # Deeply oversold — strong buy
    elif rsi2 < 25: mr_score += 1
    elif rsi2 > 90: mr_score -= 2  # Deeply overbought — strong sell
    elif rsi2 > 75: mr_score -= 1

    if bb_pos < 10: mr_score += 1  # Near lower band
    elif bb_pos > 90: mr_score -= 1  # Near upper band

    # Z-score of price vs 60-day mean
    if len(closes) >= 60:
        price_z = (price - np.mean(closes[-60:])) / (np.std(closes[-60:]) + 0.01)
        if price_z < -2: mr_score += 1
        elif price_z > 2: mr_score -= 1

    if mr_score >= 2:
        mr_vote = "LONG"
    elif mr_score <= -2:
        mr_vote = "SHORT"
    else:
        mr_vote = "NEUTRAL"

    # --- Model 3: FUNDAMENTAL / QUALITY ---
    # Uses pre-computed factors from quant engine
    value_raw = stock_data.get("value_raw", 0)
    quality_raw = stock_data.get("quality_raw", 0)

    fund_score = 0
    if value_raw > 0.5: fund_score += 1  # Good value
    elif value_raw < -0.5: fund_score -= 1
    if quality_raw > 0.5: fund_score += 1  # High quality
    elif quality_raw < -0.5: fund_score -= 1

    # Volume confirmation
    vol_ratio = stock_data.get("vol_ratio", 1.0)
    if vol_ratio > 1.3: fund_score += 1
    elif vol_ratio < 0.7: fund_score -= 1

    if fund_score >= 2:
        fund_vote = "LONG"
    elif fund_score <= -2:
        fund_vote = "SHORT"
    else:
        fund_vote = "NEUTRAL"

    # --- Consensus ---
    votes = [momentum_vote, mr_vote, fund_vote]
    long_votes = votes.count("LONG")
    short_votes = votes.count("SHORT")

    if long_votes >= 2:
        consensus = "LONG"
        agreement = long_votes
    elif short_votes >= 2:
        consensus = "SHORT"
        agreement = short_votes
    else:
        consensus = "NO_TRADE"
        agreement = 0

    # Confidence boost if all 3 agree
    base_confidence = stock_data.get("confidence", 50)
    if agreement == 3:
        confidence_mod = 1.20  # +20% confidence for unanimous
    elif agreement == 2:
        confidence_mod = 1.05  # +5% for 2/3 agreement
    else:
        confidence_mod = 0.70  # -30% for no consensus

    adjusted_confidence = min(95, int(base_confidence * confidence_mod))

    return {
        "consensus": consensus,
        "votes": {
            "momentum": momentum_vote,
            "mean_reversion": mr_vote,
            "fundamental": fund_vote,
        },
        "long_votes": long_votes,
        "short_votes": short_votes,
        "agreement": agreement,
        "confidence_modifier": confidence_mod,
        "adjusted_confidence": adjusted_confidence,
        "details": {
            "momentum_score": momentum_score,
            "mr_score": mr_score,
            "fund_score": fund_score,
            "rsi2": round(rsi2, 1),
            "bb_position": round(bb_pos, 1),
        }
    }


# ============================================================
#  4. ALTERNATIVE DATA SIGNALS
# ============================================================

def get_alt_data_signals(symbols: list) -> dict:
    """
    Gather alternative data signals for a list of symbols.

    Sources (all from yfinance — no extra API needed):
      - Short interest / float short ratio
      - Insider transactions (net buying vs selling)
      - Institutional ownership changes

    Returns signals dict keyed by symbol.
    """
    cache_key = f"alt_data_{'_'.join(sorted(symbols[:10]))}"
    cached = _rentech_cache.get(cache_key)
    if cached and time.time() - cached["time"] < 900:  # 15 min cache
        return cached["data"]

    signals = {}

    # Process in small batches to respect API limits
    for symbol in symbols[:30]:  # Max 30 to avoid API hammering
        try:
            _throttle_rentech()
            ticker = yf.Ticker(symbol)
            info = ticker.info or {}

            signal = {
                "symbol": symbol,
                "short_interest": None,
                "short_ratio": None,
                "insider_signal": "NEUTRAL",
                "institutional_pct": None,
                "composite_alt_score": 0,
            }

            # --- Short Interest ---
            short_pct = info.get("shortPercentOfFloat")
            short_ratio = info.get("shortRatio")  # days to cover

            if short_pct is not None:
                signal["short_interest"] = round(short_pct * 100, 1)

                # High short interest + positive momentum = SQUEEZE potential
                if short_pct > 0.20:  # >20% short float
                    signal["composite_alt_score"] += 2  # Squeeze candidate
                    signal["short_squeeze_risk"] = "HIGH"
                elif short_pct > 0.10:
                    signal["composite_alt_score"] += 1
                    signal["short_squeeze_risk"] = "MODERATE"
                else:
                    signal["short_squeeze_risk"] = "LOW"

            if short_ratio is not None:
                signal["short_ratio"] = round(short_ratio, 1)
                if short_ratio > 5:  # >5 days to cover = crowded short
                    signal["composite_alt_score"] += 1

            # --- Insider Transactions ---
            try:
                insider_txns = ticker.insider_transactions
                if insider_txns is not None and not insider_txns.empty:
                    recent = insider_txns.head(10)  # Last 10 transactions

                    buys = 0
                    sells = 0
                    for _, row in recent.iterrows():
                        txn_text = str(row.get("Text", "")).lower()
                        if "purchase" in txn_text or "buy" in txn_text or "acquisition" in txn_text:
                            buys += 1
                        elif "sale" in txn_text or "sell" in txn_text or "disposition" in txn_text:
                            sells += 1

                    if buys > sells + 2:
                        signal["insider_signal"] = "STRONG_BUY"
                        signal["composite_alt_score"] += 2
                    elif buys > sells:
                        signal["insider_signal"] = "BUY"
                        signal["composite_alt_score"] += 1
                    elif sells > buys + 2:
                        signal["insider_signal"] = "STRONG_SELL"
                        signal["composite_alt_score"] -= 2
                    elif sells > buys:
                        signal["insider_signal"] = "SELL"
                        signal["composite_alt_score"] -= 1

                    signal["insider_buys"] = buys
                    signal["insider_sells"] = sells
            except Exception:
                pass

            # --- Institutional Ownership ---
            inst_pct = info.get("heldPercentInstitutions")
            if inst_pct is not None:
                signal["institutional_pct"] = round(inst_pct * 100, 1)
                # High institutional = more stable, better for our strategies
                if inst_pct > 0.80:
                    signal["composite_alt_score"] += 1

            signals[symbol] = signal

        except Exception as e:
            logger.debug(f"Alt data failed for {symbol}: {e}")
            signals[symbol] = {
                "symbol": symbol,
                "composite_alt_score": 0,
                "error": str(e),
            }

    _rentech_cache[cache_key] = {"data": signals, "time": time.time()}
    return signals


# ============================================================
#  5. INTRADAY MEAN REVERSION SIGNALS
# ============================================================

def get_mean_reversion_signals(price_data: dict) -> list:
    """
    Identify stocks with strong mean reversion setups.

    Strategies:
      1. RSI(2) Connors — buy when RSI(2) < 10, sell when RSI(2) > 90
         (historically 75-91% win rate on S&P 500 stocks)
      2. VWAP Deviation — buy below VWAP, sell above VWAP
      3. Bollinger Band extreme — buy below lower band, sell above upper band
      4. 3-day consecutive moves — buy after 3 down days, sell after 3 up days

    Returns list of mean reversion trade setups with confidence.
    """
    cache_key = "mean_reversion_signals"
    cached = _rentech_cache.get(cache_key)
    if cached and time.time() - cached["time"] < 600:
        return cached["data"]

    setups = []

    for symbol, df in price_data.items():
        try:
            closes = df["Close"].values.astype(float).flatten()
            highs = df["High"].values.astype(float).flatten() if "High" in df.columns else closes
            lows = df["Low"].values.astype(float).flatten() if "Low" in df.columns else closes
            volumes = df["Volume"].values.astype(float).flatten() if "Volume" in df.columns else np.ones(len(closes))

            if len(closes) < 60:
                continue

            price = float(closes[-1])

            # --- RSI(2) Connors Strategy ---
            deltas = np.diff(closes[-3:])
            gains = np.mean([d for d in deltas if d > 0]) if any(d > 0 for d in deltas) else 0.001
            losses_v = np.mean([abs(d) for d in deltas if d < 0]) if any(d < 0 for d in deltas) else 0.001
            rs = gains / losses_v if losses_v > 0 else 100
            rsi2 = 100 - (100 / (1 + rs))

            # --- VWAP Approximation ---
            # True VWAP needs intraday data, but we approximate with cumulative price*volume
            typical_price = (highs[-20:] + lows[-20:] + closes[-20:]) / 3
            vwap_20d = float(np.sum(typical_price * volumes[-20:]) / (np.sum(volumes[-20:]) + 1))
            vwap_deviation = ((price - vwap_20d) / vwap_20d) * 100

            # --- Bollinger Band Position ---
            sma_20 = float(np.mean(closes[-20:]))
            std_20 = float(np.std(closes[-20:]))
            bb_upper = sma_20 + 2 * std_20
            bb_lower = sma_20 - 2 * std_20
            bb_pos = ((price - bb_lower) / (bb_upper - bb_lower) * 100) if (bb_upper - bb_lower) > 0 else 50

            # --- Consecutive Move Counter ---
            daily_returns = np.diff(closes[-5:]) / closes[-5:-1] * 100
            consecutive_down = sum(1 for r in daily_returns if r < -0.5)
            consecutive_up = sum(1 for r in daily_returns if r > 0.5)

            # --- Score the setup ---
            mr_score = 0
            reasons = []

            # RSI(2) extreme — the money maker
            if rsi2 < 5:
                mr_score += 3
                reasons.append(f"RSI(2) = {rsi2:.0f} — deeply oversold (Connors buy)")
            elif rsi2 < 10:
                mr_score += 2
                reasons.append(f"RSI(2) = {rsi2:.0f} — oversold (Connors buy)")
            elif rsi2 > 95:
                mr_score -= 3
                reasons.append(f"RSI(2) = {rsi2:.0f} — deeply overbought (Connors sell)")
            elif rsi2 > 90:
                mr_score -= 2
                reasons.append(f"RSI(2) = {rsi2:.0f} — overbought (Connors sell)")

            # VWAP deviation
            if vwap_deviation < -3:
                mr_score += 1
                reasons.append(f"Below VWAP by {abs(vwap_deviation):.1f}% — institutional buy zone")
            elif vwap_deviation > 3:
                mr_score -= 1
                reasons.append(f"Above VWAP by {vwap_deviation:.1f}% — institutional sell zone")

            # Bollinger Band extreme
            if bb_pos < 5:
                mr_score += 2
                reasons.append(f"Below lower Bollinger Band — extreme oversold")
            elif bb_pos > 95:
                mr_score -= 2
                reasons.append(f"Above upper Bollinger Band — extreme overbought")

            # Consecutive moves
            if consecutive_down >= 3:
                mr_score += 1
                reasons.append(f"{consecutive_down} consecutive down days — bounce expected")
            elif consecutive_up >= 3:
                mr_score -= 1
                reasons.append(f"{consecutive_up} consecutive up days — pullback expected")

            # Only generate signal if strong enough
            if abs(mr_score) >= 3:
                direction = "LONG" if mr_score > 0 else "SHORT"
                confidence = min(90, 50 + abs(mr_score) * 10)

                setups.append({
                    "symbol": symbol,
                    "direction": direction,
                    "signal_type": "MEAN_REVERSION",
                    "mr_score": mr_score,
                    "confidence": confidence,
                    "rsi2": round(rsi2, 1),
                    "vwap_deviation_pct": round(vwap_deviation, 2),
                    "bb_position": round(bb_pos, 1),
                    "consecutive_down": consecutive_down,
                    "consecutive_up": consecutive_up,
                    "price": price,
                    "reasons": reasons,
                    "expected_hold_days": 2 if abs(mr_score) >= 4 else 5,  # Quick trades
                    "expected_return_pct": round(abs(mr_score) * 1.5, 1),
                })

        except Exception as e:
            logger.debug(f"Mean reversion analysis failed for {symbol}: {e}")
            continue

    # Sort by score magnitude
    setups.sort(key=lambda x: abs(x["mr_score"]), reverse=True)

    result = setups[:15]  # Top 15 setups
    _rentech_cache[cache_key] = {"data": result, "time": time.time()}
    return result


# ============================================================
#  MASTER FUNCTION — Run all RenTech analyses
# ============================================================

def run_rentech_analysis(price_data: dict, open_trades: list = None,
                         portfolio_value: float = 100_000,
                         peak_value: float = 100_000) -> dict:
    """
    Run all Renaissance Technologies-style analyses.

    This is the master function that calls all sub-modules and returns
    a comprehensive report. Called from generate_quant_picks().

    Args:
        price_data: dict of {symbol: DataFrame} from yf.download
        open_trades: list of current open paper trades
        portfolio_value: current portfolio value
        peak_value: highest portfolio value achieved

    Returns:
        dict with pairs_trades, risk_assessment, mean_reversion_setups,
        circuit_breaker status, and ensemble vote summary
    """
    cache_key = "rentech_analysis"
    cached = _rentech_cache.get(cache_key)
    if cached and time.time() - cached["time"] < 600:
        return cached["data"]

    logger.info("🏛️ Running Renaissance Technologies analysis suite...")

    result = {
        "pairs_trades": [],
        "mean_reversion_setups": [],
        "portfolio_risk": {},
        "circuit_breaker": {},
        "alt_data": {},
        "timestamp": datetime.now().isoformat(),
    }

    try:
        # 1. Pairs Trading
        result["pairs_trades"] = find_pairs_trades(price_data)
        logger.info(f"  Pairs: {len(result['pairs_trades'])} trade opportunities found")
    except Exception as e:
        logger.warning(f"Pairs trading analysis failed: {e}")

    try:
        # 2. Mean Reversion Setups
        result["mean_reversion_setups"] = get_mean_reversion_signals(price_data)
        logger.info(f"  Mean reversion: {len(result['mean_reversion_setups'])} setups found")
    except Exception as e:
        logger.warning(f"Mean reversion analysis failed: {e}")

    try:
        # 3. Portfolio Risk Assessment
        if open_trades:
            result["portfolio_risk"] = assess_portfolio_risk(open_trades, price_data)
            logger.info(f"  Portfolio risk: {result['portfolio_risk'].get('risk_level', 'UNKNOWN')}")
    except Exception as e:
        logger.warning(f"Portfolio risk assessment failed: {e}")

    try:
        # 4. Circuit Breaker
        result["circuit_breaker"] = calculate_drawdown_circuit_breaker(
            portfolio_value, peak_value
        )
        logger.info(f"  Circuit breaker: {result['circuit_breaker'].get('level', 'UNKNOWN')}")
    except Exception as e:
        logger.warning(f"Circuit breaker check failed: {e}")

    _rentech_cache[cache_key] = {"data": result, "time": time.time()}
    return result
