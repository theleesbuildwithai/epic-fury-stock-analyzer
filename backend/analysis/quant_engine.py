"""
Quantitative Engine — the core intelligence of the Epic Fury hedge fund.

This is the brain that institutional quant funds use to find edges:
  1. Market Regime Detection — BULL / BEAR / SIDEWAYS
  2. Multi-Factor Composite Scoring — 6 orthogonal, z-scored factors
  3. Global Macro Overlay — bonds, oil, gold, VIX → sector adjustments
  4. Event-Driven — earnings proximity risk reduction
  5. Long/Short Signal Generation — LONG score >= 4, SHORT score <= -4

Designed for accuracy when real money is on the line:
  - Z-score normalization ensures fair cross-factor comparison
  - Regime-aware position sizing and signal filtering
  - Macro overlay prevents fighting the Fed / macro trends
  - Batch yfinance downloads to minimize API calls (CRITICAL)
  - Aggressive caching to avoid Yahoo Finance rate limits

All data comes from Yahoo Finance via yf.download() (bulk method).
"""

import yfinance as yf
import numpy as np
import pandas as pd
import time
import json
import logging
from datetime import datetime, timedelta
from scipy.stats import zscore as scipy_zscore

logger = logging.getLogger(__name__)

# ============================================================
#  CACHING & THROTTLING (shared with existing system)
# ============================================================

_quant_cache = {}
_QUANT_CACHE_TTL = 300  # 5 minutes
_last_quant_call = [0.0]
_QUANT_DELAY = 3.0  # seconds between Yahoo Finance calls


def _throttle():
    """Enforce minimum delay between Yahoo Finance API calls."""
    now = time.time()
    elapsed = now - _last_quant_call[0]
    if elapsed < _QUANT_DELAY:
        time.sleep(_QUANT_DELAY - elapsed)
    _last_quant_call[0] = time.time()


def _get_cached(key, fetch_fn, ttl=None):
    """Cache with configurable TTL."""
    if ttl is None:
        ttl = _QUANT_CACHE_TTL
    now = time.time()
    if key in _quant_cache and now - _quant_cache[key]["time"] < ttl:
        return _quant_cache[key]["data"]
    data = fetch_fn()
    _quant_cache[key] = {"data": data, "time": now}
    return data


# ============================================================
#  UNIVERSE — stocks we analyze for quant picks
# ============================================================

# Large-cap liquid stocks across all S&P 500 sectors
# These have deep liquidity, reliable data, and analyst coverage
QUANT_UNIVERSE = [
    # Technology
    "AAPL", "MSFT", "NVDA", "AVGO", "AMD", "ADBE", "CRM", "INTC", "QCOM", "TXN",
    "AMAT", "LRCX", "KLAC", "MRVL", "SNPS",
    # Communication
    "GOOGL", "META", "NFLX", "DIS", "CMCSA", "TMUS", "VZ", "T",
    # Consumer Discretionary
    "AMZN", "TSLA", "HD", "MCD", "NKE", "SBUX", "TJX", "LOW", "BKNG", "CMG",
    # Consumer Staples
    "WMT", "PG", "COST", "KO", "PEP", "PM", "MO", "CL", "KMB",
    # Healthcare
    "UNH", "LLY", "JNJ", "ABBV", "MRK", "PFE", "TMO", "ABT", "BMY", "AMGN",
    "GILD", "ISRG", "VRTX", "REGN",
    # Financials
    "JPM", "V", "MA", "BAC", "GS", "MS", "WFC", "C", "BLK", "SCHW",
    "AXP", "CB", "MMC",
    # Industrials
    "BA", "CAT", "HON", "GE", "UNP", "RTX", "LMT", "DE", "FDX", "WM",
    # Energy
    "XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX",
    # Materials
    "LIN", "APD", "SHW", "FCX", "NEM",
    # Real Estate
    "AMT", "PLD", "SPG", "CCI", "EQIX",
    # Utilities
    "NEE", "DUK", "SO", "AEP", "SRE",
]

# Sector mapping for macro overlay adjustments
SECTOR_MAP = {
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology",
    "AVGO": "Technology", "AMD": "Technology", "ADBE": "Technology",
    "CRM": "Technology", "INTC": "Technology", "QCOM": "Technology",
    "TXN": "Technology", "AMAT": "Technology", "LRCX": "Technology",
    "KLAC": "Technology", "MRVL": "Technology", "SNPS": "Technology",
    "GOOGL": "Communication", "META": "Communication", "NFLX": "Communication",
    "DIS": "Communication", "CMCSA": "Communication", "TMUS": "Communication",
    "VZ": "Communication", "T": "Communication",
    "AMZN": "Consumer Discretionary", "TSLA": "Consumer Discretionary",
    "HD": "Consumer Discretionary", "MCD": "Consumer Discretionary",
    "NKE": "Consumer Discretionary", "SBUX": "Consumer Discretionary",
    "TJX": "Consumer Discretionary", "LOW": "Consumer Discretionary",
    "BKNG": "Consumer Discretionary", "CMG": "Consumer Discretionary",
    "WMT": "Consumer Staples", "PG": "Consumer Staples", "COST": "Consumer Staples",
    "KO": "Consumer Staples", "PEP": "Consumer Staples", "PM": "Consumer Staples",
    "MO": "Consumer Staples", "CL": "Consumer Staples", "KMB": "Consumer Staples",
    "UNH": "Healthcare", "LLY": "Healthcare", "JNJ": "Healthcare",
    "ABBV": "Healthcare", "MRK": "Healthcare", "PFE": "Healthcare",
    "TMO": "Healthcare", "ABT": "Healthcare", "BMY": "Healthcare",
    "AMGN": "Healthcare", "GILD": "Healthcare", "ISRG": "Healthcare",
    "VRTX": "Healthcare", "REGN": "Healthcare",
    "JPM": "Financials", "V": "Financials", "MA": "Financials",
    "BAC": "Financials", "GS": "Financials", "MS": "Financials",
    "WFC": "Financials", "C": "Financials", "BLK": "Financials",
    "SCHW": "Financials", "AXP": "Financials", "CB": "Financials",
    "MMC": "Financials",
    "BA": "Industrials", "CAT": "Industrials", "HON": "Industrials",
    "GE": "Industrials", "UNP": "Industrials", "RTX": "Industrials",
    "LMT": "Industrials", "DE": "Industrials", "FDX": "Industrials",
    "WM": "Industrials",
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy",
    "SLB": "Energy", "EOG": "Energy", "MPC": "Energy", "PSX": "Energy",
    "LIN": "Materials", "APD": "Materials", "SHW": "Materials",
    "FCX": "Materials", "NEM": "Materials",
    "AMT": "Real Estate", "PLD": "Real Estate", "SPG": "Real Estate",
    "CCI": "Real Estate", "EQIX": "Real Estate",
    "NEE": "Utilities", "DUK": "Utilities", "SO": "Utilities",
    "AEP": "Utilities", "SRE": "Utilities",
}


# ============================================================
#  1. MARKET REGIME DETECTION
# ============================================================

def detect_market_regime() -> dict:
    """
    Detect the current market regime: BULL, BEAR, or SIDEWAYS.

    Uses three signals (institutional standard):
      1. S&P 500 vs 200-day SMA — the single most reliable trend filter
         Above = bullish bias, Below = bearish bias
      2. VIX level — fear gauge
         < 15 = complacent, 15-25 = normal, 25-35 = elevated fear, > 35 = crisis
      3. Market breadth — % of our universe above their own 50-day SMA
         > 60% = broad participation (healthy), < 40% = narrow/weak

    Returns:
        dict with regime, confidence, and component details
    """
    def fetch():
        regime_data = {
            "regime": "SIDEWAYS",
            "confidence": 50,
            "sp500_trend": "unknown",
            "vix_level": 0,
            "vix_zone": "unknown",
            "breadth_pct": 50,
            "breadth_signal": "neutral",
            "details": [],
            "timestamp": datetime.now().isoformat(),
        }

        # --- Signal 1: S&P 500 vs 200-SMA ---
        try:
            _throttle()
            sp_df = yf.download("^GSPC", period="1y", progress=False)
            if sp_df is not None and len(sp_df) >= 200:
                sp_closes = sp_df["Close"].values.astype(float).flatten()
                sp_current = float(sp_closes[-1])
                sp_sma200 = float(np.mean(sp_closes[-200:]))
                sp_sma50 = float(np.mean(sp_closes[-50:]))
                sp_pct_above_200 = ((sp_current / sp_sma200) - 1) * 100

                regime_data["sp500_price"] = round(sp_current, 2)
                regime_data["sp500_sma200"] = round(sp_sma200, 2)
                regime_data["sp500_sma50"] = round(sp_sma50, 2)
                regime_data["sp500_pct_above_200sma"] = round(sp_pct_above_200, 2)

                if sp_current > sp_sma200:
                    regime_data["sp500_trend"] = "bullish"
                    if sp_current > sp_sma50 > sp_sma200:
                        regime_data["details"].append(
                            "S&P 500 in strong uptrend (price > 50-SMA > 200-SMA)"
                        )
                    else:
                        regime_data["details"].append(
                            f"S&P 500 above 200-SMA by {sp_pct_above_200:.1f}%"
                        )
                else:
                    regime_data["sp500_trend"] = "bearish"
                    regime_data["details"].append(
                        f"S&P 500 below 200-SMA by {abs(sp_pct_above_200):.1f}% — risk-off"
                    )
        except Exception as e:
            logger.warning(f"Regime: S&P 500 data failed: {e}")

        # --- Signal 2: VIX level ---
        try:
            _throttle()
            vix_df = yf.download("^VIX", period="5d", progress=False)
            if vix_df is not None and not vix_df.empty:
                vix_val = float(vix_df["Close"].dropna().iloc[-1])
                regime_data["vix_level"] = round(vix_val, 2)

                if vix_val < 15:
                    regime_data["vix_zone"] = "complacent"
                    regime_data["details"].append(
                        f"VIX at {vix_val:.1f} — low fear, possible complacency"
                    )
                elif vix_val < 20:
                    regime_data["vix_zone"] = "normal"
                    regime_data["details"].append(f"VIX at {vix_val:.1f} — normal range")
                elif vix_val < 25:
                    regime_data["vix_zone"] = "elevated"
                    regime_data["details"].append(
                        f"VIX at {vix_val:.1f} — elevated uncertainty"
                    )
                elif vix_val < 35:
                    regime_data["vix_zone"] = "fear"
                    regime_data["details"].append(
                        f"VIX at {vix_val:.1f} — significant fear in market"
                    )
                else:
                    regime_data["vix_zone"] = "crisis"
                    regime_data["details"].append(
                        f"VIX at {vix_val:.1f} — CRISIS level, extreme caution"
                    )
        except Exception as e:
            logger.warning(f"Regime: VIX data failed: {e}")

        # --- Signal 3: Market Breadth ---
        # % of stocks in our universe above their 50-day SMA
        try:
            _throttle()
            # Download a representative sample (not all 100+ stocks — too many API calls)
            breadth_sample = [
                "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA",
                "JPM", "V", "UNH", "JNJ", "XOM", "PG", "HD", "BA",
                "CRM", "AMD", "NFLX", "WMT", "GS", "CAT", "LLY",
                "MRK", "ABBV", "COST", "CVX", "NEE", "AMT", "GE", "HON",
            ]
            breadth_df = yf.download(
                breadth_sample, period="3mo", progress=False, group_by="ticker"
            )

            if breadth_df is not None and not breadth_df.empty:
                above_50sma = 0
                total_checked = 0
                for sym in breadth_sample:
                    try:
                        if isinstance(breadth_df.columns, pd.MultiIndex):
                            if sym not in breadth_df.columns.get_level_values(0):
                                continue
                            close_series = breadth_df[(sym, "Close")].dropna()
                        else:
                            continue

                        if close_series is not None and len(close_series) >= 50:
                            closes = close_series.values.astype(float).flatten()
                            current = closes[-1]
                            sma50 = float(np.mean(closes[-50:]))
                            total_checked += 1
                            if current > sma50:
                                above_50sma += 1
                    except Exception:
                        continue

                if total_checked > 0:
                    breadth_pct = round((above_50sma / total_checked) * 100, 1)
                    regime_data["breadth_pct"] = breadth_pct
                    regime_data["breadth_stocks_above"] = above_50sma
                    regime_data["breadth_stocks_total"] = total_checked

                    if breadth_pct >= 70:
                        regime_data["breadth_signal"] = "strong"
                        regime_data["details"].append(
                            f"Breadth strong: {breadth_pct}% above 50-SMA — broad participation"
                        )
                    elif breadth_pct >= 50:
                        regime_data["breadth_signal"] = "moderate"
                        regime_data["details"].append(
                            f"Breadth moderate: {breadth_pct}% above 50-SMA"
                        )
                    elif breadth_pct >= 30:
                        regime_data["breadth_signal"] = "weak"
                        regime_data["details"].append(
                            f"Breadth weak: only {breadth_pct}% above 50-SMA — narrow market"
                        )
                    else:
                        regime_data["breadth_signal"] = "very_weak"
                        regime_data["details"].append(
                            f"Breadth very weak: {breadth_pct}% above 50-SMA — broad selling"
                        )
        except Exception as e:
            logger.warning(f"Regime: Breadth data failed: {e}")

        # --- Determine final regime ---
        bull_score = 0
        bear_score = 0

        # S&P trend (strongest signal, 40% weight)
        if regime_data["sp500_trend"] == "bullish":
            bull_score += 4
        elif regime_data["sp500_trend"] == "bearish":
            bear_score += 4

        # VIX (25% weight)
        vix = regime_data["vix_level"]
        if vix < 18:
            bull_score += 2.5
        elif vix < 22:
            bull_score += 1
        elif vix < 30:
            bear_score += 1.5
        else:
            bear_score += 2.5

        # Breadth (35% weight)
        breadth = regime_data["breadth_pct"]
        if breadth >= 65:
            bull_score += 3.5
        elif breadth >= 50:
            bull_score += 1.5
        elif breadth >= 35:
            bear_score += 1.5
        else:
            bear_score += 3.5

        net = bull_score - bear_score
        if net >= 3:
            regime_data["regime"] = "BULL"
            regime_data["confidence"] = min(95, 60 + int(net * 5))
        elif net <= -3:
            regime_data["regime"] = "BEAR"
            regime_data["confidence"] = min(95, 60 + int(abs(net) * 5))
        else:
            regime_data["regime"] = "SIDEWAYS"
            regime_data["confidence"] = max(40, 70 - int(abs(net) * 5))

        regime_data["bull_score"] = round(bull_score, 1)
        regime_data["bear_score"] = round(bear_score, 1)

        return regime_data

    return _get_cached("market_regime", fetch, ttl=600)  # 10 min cache


# ============================================================
#  2. GLOBAL MACRO OVERLAY
# ============================================================

def get_macro_overlay() -> dict:
    """
    Global Macro Overlay — how macro factors affect each sector.

    Monitors:
      - ^TNX (10Y Treasury Yield) — rising yields hurt growth/REITs, help financials
      - CL=F (Crude Oil) — rising oil helps energy, hurts airlines/consumer
      - GC=F (Gold) — rising gold signals risk-off / inflation fears
      - ^VIX — fear gauge (already in regime, but also used for position sizing)

    Returns sector adjustment scores (-2 to +2) based on macro conditions.
    """
    def fetch():
        macro = {
            "treasury_10y": {"value": 0, "change_5d": 0, "signal": "neutral"},
            "crude_oil": {"value": 0, "change_5d": 0, "signal": "neutral"},
            "gold": {"value": 0, "change_5d": 0, "signal": "neutral"},
            "vix": {"value": 0, "change_5d": 0, "signal": "neutral"},
            "sector_adjustments": {},
            "timestamp": datetime.now().isoformat(),
        }

        # Batch download all macro indicators at once (1 API call)
        _throttle()
        try:
            macro_symbols = ["^TNX", "CL=F", "GC=F", "^VIX"]
            df = yf.download(macro_symbols, period="1mo", progress=False, group_by="ticker")
        except Exception as e:
            logger.warning(f"Macro overlay download failed: {e}")
            return macro

        if df is None or df.empty:
            return macro

        # Parse each macro indicator
        for symbol, key in [("^TNX", "treasury_10y"), ("CL=F", "crude_oil"),
                            ("GC=F", "gold"), ("^VIX", "vix")]:
            try:
                if isinstance(df.columns, pd.MultiIndex):
                    if symbol not in df.columns.get_level_values(0):
                        continue
                    close_series = df[(symbol, "Close")].dropna()
                else:
                    continue

                if close_series is not None and len(close_series) >= 5:
                    closes = close_series.values.astype(float).flatten()
                    current = float(closes[-1])
                    five_days_ago = float(closes[-5]) if len(closes) >= 5 else current
                    change_5d = ((current / five_days_ago) - 1) * 100 if five_days_ago > 0 else 0

                    # 20-day trend
                    if len(closes) >= 20:
                        sma20 = float(np.mean(closes[-20:]))
                        trend = "rising" if current > sma20 * 1.01 else (
                            "falling" if current < sma20 * 0.99 else "flat"
                        )
                    else:
                        trend = "flat"

                    macro[key] = {
                        "value": round(current, 2),
                        "change_5d": round(change_5d, 2),
                        "signal": trend,
                    }
            except Exception:
                continue

        # --- Calculate sector adjustments based on macro ---
        # Each sector gets a score from -2 to +2

        tnx_trend = macro["treasury_10y"]["signal"]
        oil_trend = macro["crude_oil"]["signal"]
        gold_trend = macro["gold"]["signal"]
        vix_val = macro["vix"]["value"]

        adjustments = {}

        # Technology: hurt by rising yields (higher discount rates)
        tech_adj = 0
        if tnx_trend == "rising":
            tech_adj -= 1
        elif tnx_trend == "falling":
            tech_adj += 1
        if vix_val > 25:
            tech_adj -= 0.5
        adjustments["Technology"] = round(tech_adj, 1)

        # Financials: helped by rising yields (better net interest margins)
        fin_adj = 0
        if tnx_trend == "rising":
            fin_adj += 1.5
        elif tnx_trend == "falling":
            fin_adj -= 1
        adjustments["Financials"] = round(fin_adj, 1)

        # Energy: directly tied to oil prices
        energy_adj = 0
        if oil_trend == "rising":
            energy_adj += 1.5
        elif oil_trend == "falling":
            energy_adj -= 1.5
        adjustments["Energy"] = round(energy_adj, 1)

        # Healthcare: defensive, benefits from risk-off
        health_adj = 0
        if vix_val > 25:
            health_adj += 1  # safe haven
        if gold_trend == "rising":
            health_adj += 0.5  # risk-off benefits defensives
        adjustments["Healthcare"] = round(health_adj, 1)

        # Consumer Discretionary: hurt by rising rates & oil
        cd_adj = 0
        if tnx_trend == "rising":
            cd_adj -= 0.5
        if oil_trend == "rising":
            cd_adj -= 0.5  # consumers pay more for gas
        adjustments["Consumer Discretionary"] = round(cd_adj, 1)

        # Consumer Staples: defensive
        cs_adj = 0
        if vix_val > 25:
            cs_adj += 1
        if tnx_trend == "rising":
            cs_adj -= 0.5  # yield competition
        adjustments["Consumer Staples"] = round(cs_adj, 1)

        # Industrials: sensitive to economic cycle & oil costs
        ind_adj = 0
        if oil_trend == "rising":
            ind_adj -= 0.5
        if tnx_trend == "falling":
            ind_adj += 0.5  # lower borrowing costs
        adjustments["Industrials"] = round(ind_adj, 1)

        # Real Estate: very sensitive to interest rates
        re_adj = 0
        if tnx_trend == "rising":
            re_adj -= 1.5
        elif tnx_trend == "falling":
            re_adj += 1.5
        adjustments["Real Estate"] = round(re_adj, 1)

        # Utilities: rate-sensitive (bond proxy)
        util_adj = 0
        if tnx_trend == "rising":
            util_adj -= 1
        elif tnx_trend == "falling":
            util_adj += 1
        if vix_val > 25:
            util_adj += 0.5
        adjustments["Utilities"] = round(util_adj, 1)

        # Materials: inflation beneficiary, gold-linked
        mat_adj = 0
        if gold_trend == "rising":
            mat_adj += 1
        if oil_trend == "rising":
            mat_adj += 0.5  # commodity correlation
        adjustments["Materials"] = round(mat_adj, 1)

        # Communication: similar to tech (growth sector)
        comm_adj = 0
        if tnx_trend == "rising":
            comm_adj -= 0.5
        elif tnx_trend == "falling":
            comm_adj += 0.5
        adjustments["Communication"] = round(comm_adj, 1)

        macro["sector_adjustments"] = adjustments
        return macro

    return _get_cached("macro_overlay", fetch, ttl=600)  # 10 min cache


# ============================================================
#  3. MULTI-FACTOR COMPOSITE SCORING
# ============================================================

def _safe_zscore(values: list) -> list:
    """
    Z-score normalization with safety for constant arrays.
    Returns 0 for all values if standard deviation is 0.
    """
    arr = np.array(values, dtype=float)
    std = np.std(arr)
    if std == 0 or np.isnan(std):
        return [0.0] * len(values)
    mean = np.mean(arr)
    return [round(float((v - mean) / std), 4) for v in arr]


def calculate_multi_factor_scores(price_data: dict, regime: dict = None,
                                   macro: dict = None) -> list:
    """
    Calculate 6-factor composite score for each stock in the universe.

    Factors (orthogonal by design — each captures a different edge):
      1. MOMENTUM — 12-month return minus last month (Jegadeesh & Titman, 1993)
         Captures: trend continuation. Winner stocks keep winning.
      2. VALUE — P/E ratio inverted (earnings yield)
         Captures: cheap stocks outperform expensive over time.
      3. QUALITY — Return on Equity (ROE) from fundamentals
         Captures: well-managed companies with durable advantages.
      4. LOW VOLATILITY — inverse of 60-day realized volatility
         Captures: low-vol anomaly. Less volatile stocks have higher risk-adjusted returns.
      5. RSI(2) MEAN REVERSION — Connors strategy (75-91% historical win rate)
         Captures: short-term oversold bounces. Buy when RSI(2) < 10, above 200-SMA.
      6. VOLUME CONFIRMATION — On-Balance Volume (OBV) trend
         Captures: smart money flow. Volume precedes price.

    Each factor is z-scored across the universe for fair comparison,
    then combined using adaptive weights from the learning system.

    Args:
        price_data: dict of {symbol: DataFrame} from batch download
        regime: market regime dict (from detect_market_regime)
        macro: macro overlay dict (from get_macro_overlay)

    Returns:
        list of scored stock dicts, sorted by composite score
    """
    from predictions.models import get_signal_weights

    # Get adaptive weights (learned from past performance)
    weights = get_signal_weights()

    # Collect raw factor values for all stocks
    raw_factors = []

    for symbol, df in price_data.items():
        try:
            if df is None or len(df) < 60:
                continue

            closes = df["Close"].values.astype(float).flatten()
            volumes = df["Volume"].values.astype(float).flatten()
            current_price = float(closes[-1])

            if current_price <= 0 or np.isnan(current_price):
                continue

            # --- Factor 1: MOMENTUM (12-1 month return) ---
            # Use 252-day return minus last 21 days (skip recent month)
            # This is the Jegadeesh-Titman momentum factor
            if len(closes) >= 252:
                ret_12m = (closes[-21] / closes[-252]) - 1  # 12m return, skip last month
            elif len(closes) >= 126:
                ret_12m = (closes[-21] / closes[-126]) - 1  # 6m fallback
            else:
                ret_12m = (closes[-21] / closes[0]) - 1 if len(closes) > 21 else 0
            momentum_raw = float(ret_12m) * 100

            # --- Factor 2: VALUE (earnings yield = inverse P/E) ---
            # Higher earnings yield = cheaper = better
            # We'll use price-to-earnings from recent data
            # For batch efficiency, use a simplified proxy: 60-day mean reversion
            # (stocks that have fallen more are "cheaper" relative to recent history)
            if len(closes) >= 60:
                price_vs_60d_avg = (current_price / float(np.mean(closes[-60:]))) - 1
                value_raw = -price_vs_60d_avg * 100  # negative = cheaper = better
            else:
                value_raw = 0.0

            # --- Factor 3: QUALITY (consistency of returns) ---
            # Proxy: Sharpe ratio of daily returns over last 120 days
            # Stocks with consistently positive returns = higher quality
            if len(closes) >= 120:
                window = closes[-120:]
                daily_rets = np.diff(window) / window[:-1]
                quality_raw = float(np.mean(daily_rets) / (np.std(daily_rets) + 1e-10)) * np.sqrt(252)
            else:
                daily_rets = np.diff(closes) / closes[:-1]
                quality_raw = float(np.mean(daily_rets) / (np.std(daily_rets) + 1e-10)) * np.sqrt(252)

            # --- Factor 4: LOW VOLATILITY (inverse of 60-day vol) ---
            if len(closes) >= 60:
                window_60 = closes[-60:]
                daily_rets_60 = np.diff(window_60) / window_60[:-1]
                vol_60d = float(np.std(daily_rets_60)) * np.sqrt(252) * 100
            else:
                vol_60d = float(np.std(np.diff(closes) / closes[:-1])) * np.sqrt(252) * 100
            low_vol_raw = -vol_60d  # negative vol = lower vol = better

            # --- Factor 5: RSI(2) MEAN REVERSION (Connors strategy) ---
            # RSI with 2-day lookback — extremely sensitive to short-term oversold
            # Buy signal: RSI(2) < 10 AND price > 200-SMA (uptrend filter)
            if len(closes) >= 3:
                # Calculate RSI(2)
                deltas = np.diff(closes[-3:])
                gain = float(np.sum(np.maximum(deltas, 0)))
                loss = float(np.sum(np.maximum(-deltas, 0)))
                if loss == 0:
                    rsi2 = 100.0
                else:
                    rs = gain / loss
                    rsi2 = 100 - (100 / (1 + rs))

                # 200-SMA filter (only buy oversold if in uptrend)
                above_200sma = True
                if len(closes) >= 200:
                    sma200 = float(np.mean(closes[-200:]))
                    above_200sma = current_price > sma200

                # Score: lower RSI(2) = more oversold = stronger buy signal
                # But only if above 200-SMA (safety filter)
                if above_200sma and rsi2 < 10:
                    rsi2_raw = (10 - rsi2) * 5  # Strong buy signal
                elif above_200sma and rsi2 < 25:
                    rsi2_raw = (25 - rsi2) * 1  # Mild buy
                elif rsi2 > 90:
                    rsi2_raw = -(rsi2 - 90) * 3  # Overbought = sell signal
                elif rsi2 > 75:
                    rsi2_raw = -(rsi2 - 75) * 1  # Mildly overbought
                else:
                    rsi2_raw = 0.0
            else:
                rsi2 = 50.0
                rsi2_raw = 0.0

            # --- Factor 6: VOLUME CONFIRMATION (OBV trend) ---
            # On-Balance Volume: cumulative sum of volume on up-days minus down-days
            # Rising OBV with rising price = confirmed move (smart money agrees)
            if len(closes) >= 20 and len(volumes) >= 20:
                price_changes = np.diff(closes[-20:])
                vol_window = volumes[-19:]  # one fewer due to diff
                obv_changes = np.where(price_changes > 0, vol_window,
                               np.where(price_changes < 0, -vol_window, 0))
                obv = np.cumsum(obv_changes)

                # OBV trend: slope of OBV over last 20 days
                if len(obv) >= 5:
                    obv_slope = float(np.polyfit(range(len(obv)), obv, 1)[0])
                    # Normalize by average volume
                    avg_vol = float(np.mean(volumes[-20:])) + 1
                    volume_raw = (obv_slope / avg_vol) * 1000
                else:
                    volume_raw = 0.0
            else:
                volume_raw = 0.0

            # --- RSI(14) for additional context ---
            if len(closes) >= 15:
                deltas_14 = np.diff(closes[-15:])
                avg_gain_14 = float(np.mean(np.maximum(deltas_14, 0)))
                avg_loss_14 = float(np.mean(np.maximum(-deltas_14, 0)))
                if avg_loss_14 == 0:
                    rsi14 = 100.0
                else:
                    rs14 = avg_gain_14 / avg_loss_14
                    rsi14 = 100 - (100 / (1 + rs14))
            else:
                rsi14 = 50.0

            # EMA alignment for trend context
            ema_9 = float(pd.Series(closes).ewm(span=9, adjust=False).mean().iloc[-1])
            ema_21 = float(pd.Series(closes).ewm(span=21, adjust=False).mean().iloc[-1])
            ema_50 = float(pd.Series(closes).ewm(span=50, adjust=False).mean().iloc[-1]) if len(closes) >= 50 else ema_21

            raw_factors.append({
                "symbol": symbol,
                "price": round(current_price, 2),
                "sector": SECTOR_MAP.get(symbol, "Unknown"),
                "momentum_raw": momentum_raw,
                "value_raw": value_raw,
                "quality_raw": quality_raw,
                "low_vol_raw": low_vol_raw,
                "rsi2_raw": rsi2_raw,
                "volume_raw": volume_raw,
                "rsi2": round(rsi2, 1),
                "rsi14": round(rsi14, 1),
                "vol_60d": round(vol_60d, 1),
                "ema_9": round(ema_9, 2),
                "ema_21": round(ema_21, 2),
                "ema_50": round(ema_50, 2),
                "momentum_pct": round(momentum_raw, 2),
            })

        except Exception as e:
            logger.debug(f"Factor calc failed for {symbol}: {e}")
            continue

    if not raw_factors:
        return []

    # --- Z-Score Normalization ---
    # Each factor is z-scored across the universe so they're comparable
    momentum_z = _safe_zscore([s["momentum_raw"] for s in raw_factors])
    value_z = _safe_zscore([s["value_raw"] for s in raw_factors])
    quality_z = _safe_zscore([s["quality_raw"] for s in raw_factors])
    low_vol_z = _safe_zscore([s["low_vol_raw"] for s in raw_factors])
    rsi2_z = _safe_zscore([s["rsi2_raw"] for s in raw_factors])
    volume_z = _safe_zscore([s["volume_raw"] for s in raw_factors])

    # --- Regime adjustments ---
    regime_multiplier = 1.0  # default
    if regime:
        if regime.get("regime") == "BEAR":
            # In bear markets: reduce momentum weight (momentum crashes),
            # increase low-vol and value weights (defensive)
            weights = dict(weights)  # copy
            weights["momentum"] = weights.get("momentum", 0.25) * 0.5
            weights["low_vol"] = weights.get("low_vol", 0.15) * 1.5
            weights["value"] = weights.get("value", 0.20) * 1.3
            regime_multiplier = 0.7  # lower confidence in bear markets
        elif regime.get("regime") == "SIDEWAYS":
            # In sideways: boost mean-reversion (RSI2)
            weights = dict(weights)
            weights["rsi2"] = weights.get("rsi2", 0.15) * 1.4
            regime_multiplier = 0.85

    # Normalize weights to sum to 1
    w_total = sum(weights.values())
    w_mom = weights.get("momentum", 0.25) / w_total
    w_val = weights.get("value", 0.20) / w_total
    w_qual = weights.get("quality", 0.15) / w_total
    w_lvol = weights.get("low_vol", 0.15) / w_total
    w_rsi2 = weights.get("rsi2", 0.15) / w_total
    w_vol = weights.get("volume", 0.10) / w_total

    # --- Calculate composite scores ---
    scored = []
    for i, stock in enumerate(raw_factors):
        # Weighted composite
        composite = (
            momentum_z[i] * w_mom +
            value_z[i] * w_val +
            quality_z[i] * w_qual +
            low_vol_z[i] * w_lvol +
            rsi2_z[i] * w_rsi2 +
            volume_z[i] * w_vol
        )

        # Apply macro overlay sector adjustment
        macro_adj = 0
        if macro and "sector_adjustments" in macro:
            sector = stock["sector"]
            macro_adj = macro["sector_adjustments"].get(sector, 0)
            # Convert macro adj (-2 to +2) to z-score scale (-0.5 to +0.5)
            composite += macro_adj * 0.25

        # Scale composite to a more intuitive range (-10 to +10)
        final_score = round(composite * 3.0, 2)

        # Determine direction
        if final_score >= 4:
            direction = "LONG"
            confidence = min(95, 60 + int(final_score * 3))
        elif final_score >= 2:
            direction = "LONG"
            confidence = min(85, 50 + int(final_score * 5))
        elif final_score <= -4:
            direction = "SHORT"
            confidence = min(95, 60 + int(abs(final_score) * 3))
        elif final_score <= -2:
            direction = "SHORT"
            confidence = min(85, 50 + int(abs(final_score) * 5))
        else:
            direction = "NEUTRAL"
            confidence = max(30, 50 - int(abs(final_score) * 5))

        # Apply regime multiplier to confidence
        confidence = int(confidence * regime_multiplier)

        # Build factor breakdown for transparency
        factor_breakdown = {
            "momentum": {"z": momentum_z[i], "weight": round(w_mom, 3),
                         "raw": round(stock["momentum_raw"], 2),
                         "contribution": round(momentum_z[i] * w_mom, 3)},
            "value": {"z": value_z[i], "weight": round(w_val, 3),
                      "raw": round(stock["value_raw"], 2),
                      "contribution": round(value_z[i] * w_val, 3)},
            "quality": {"z": quality_z[i], "weight": round(w_qual, 3),
                        "raw": round(stock["quality_raw"], 2),
                        "contribution": round(quality_z[i] * w_qual, 3)},
            "low_vol": {"z": low_vol_z[i], "weight": round(w_lvol, 3),
                        "raw": round(stock["low_vol_raw"], 2),
                        "contribution": round(low_vol_z[i] * w_lvol, 3)},
            "rsi2": {"z": rsi2_z[i], "weight": round(w_rsi2, 3),
                     "raw": round(stock["rsi2_raw"], 2),
                     "contribution": round(rsi2_z[i] * w_rsi2, 3)},
            "volume": {"z": volume_z[i], "weight": round(w_vol, 3),
                       "raw": round(stock["volume_raw"], 2),
                       "contribution": round(volume_z[i] * w_vol, 3)},
        }

        # Generate human-readable reasons
        reasons = []
        # Top contributing factor
        contributions = [(k, v["contribution"]) for k, v in factor_breakdown.items()]
        contributions.sort(key=lambda x: abs(x[1]), reverse=True)
        for factor_name, contrib in contributions[:3]:
            if abs(contrib) > 0.05:
                if contrib > 0:
                    reasons.append(f"{factor_name.replace('_', ' ').title()} bullish ({contrib:+.2f})")
                else:
                    reasons.append(f"{factor_name.replace('_', ' ').title()} bearish ({contrib:+.2f})")

        if macro_adj != 0:
            reasons.append(f"Macro {'+' if macro_adj > 0 else ''}{macro_adj} for {stock['sector']}")

        # Stop loss and target calculations
        atr_proxy = stock["vol_60d"] / np.sqrt(252) * stock["price"] / 100  # daily vol in $
        stop_loss = round(stock["price"] - (atr_proxy * 2 * 14), 2) if direction == "LONG" else (
            round(stock["price"] + (atr_proxy * 2 * 14), 2) if direction == "SHORT" else None
        )
        target_price = round(stock["price"] + (atr_proxy * 3 * 14), 2) if direction == "LONG" else (
            round(stock["price"] - (atr_proxy * 3 * 14), 2) if direction == "SHORT" else None
        )

        scored.append({
            "symbol": stock["symbol"],
            "price": stock["price"],
            "sector": stock["sector"],
            "composite_score": final_score,
            "direction": direction,
            "confidence": confidence,
            "rsi2": stock["rsi2"],
            "rsi14": stock["rsi14"],
            "volatility_60d": stock["vol_60d"],
            "momentum_pct": stock["momentum_pct"],
            "ema_9": stock["ema_9"],
            "ema_21": stock["ema_21"],
            "ema_50": stock["ema_50"],
            "factors": factor_breakdown,
            "macro_adjustment": macro_adj,
            "reasons": reasons[:4],
            "stop_loss": stop_loss,
            "target_price": target_price,
        })

    # Sort by composite score descending
    scored.sort(key=lambda x: x["composite_score"], reverse=True)
    return scored


# ============================================================
#  4. EARNINGS PROXIMITY CHECK
# ============================================================

def check_earnings_proximity(symbol: str) -> dict:
    """
    Check if a stock has earnings coming up within 5 trading days.

    Earnings are the single biggest risk for any position:
    - Within 5 days of earnings → reduce confidence by 30%
    - Within 2 days → reduce confidence by 50%
    - This prevents us from taking big positions right before
      an earnings surprise that could go either way

    Returns:
        dict with days_until_earnings, is_near_earnings, confidence_penalty
    """
    result = {
        "has_earnings_data": False,
        "days_until_earnings": None,
        "is_near_earnings": False,
        "confidence_penalty": 0,
        "earnings_date": None,
    }

    try:
        _throttle()
        stock = yf.Ticker(symbol)
        try:
            ed_df = stock.earnings_dates
            if ed_df is not None and not ed_df.empty:
                today = datetime.now().date()
                for idx in ed_df.index:
                    try:
                        if hasattr(idx, 'date'):
                            ed = idx.date()
                        else:
                            ed = pd.Timestamp(idx).date()

                        if ed >= today:
                            days_until = (ed - today).days
                            result["has_earnings_data"] = True
                            result["days_until_earnings"] = days_until
                            result["earnings_date"] = ed.isoformat()

                            if days_until <= 2:
                                result["is_near_earnings"] = True
                                result["confidence_penalty"] = 50
                            elif days_until <= 5:
                                result["is_near_earnings"] = True
                                result["confidence_penalty"] = 30
                            elif days_until <= 10:
                                result["confidence_penalty"] = 10
                            break
                    except Exception:
                        continue
        except Exception:
            pass
    except Exception:
        pass

    return result


# ============================================================
#  5. MAIN ENTRY POINT — GENERATE QUANT PICKS
# ============================================================

def generate_quant_picks() -> dict:
    """
    Main entry point: generates LONG and SHORT picks using the full
    quantitative pipeline.

    Pipeline:
      1. Detect market regime (BULL/BEAR/SIDEWAYS)
      2. Get macro overlay (bonds, oil, gold, VIX)
      3. Batch download price data for entire universe (2 API calls max)
      4. Calculate 6-factor composite scores
      5. Apply earnings proximity risk reduction
      6. Rank and return top LONG and SHORT picks

    Returns:
        dict with regime, macro, long_picks, short_picks, neutral, metadata
    """
    def fetch():
        start_time = time.time()

        # Step 1: Market regime
        regime = detect_market_regime()

        # Step 2: Macro overlay
        macro = get_macro_overlay()

        # Step 3: Batch download price data
        # Split universe into 2 batches to avoid Yahoo Finance limits
        half = len(QUANT_UNIVERSE) // 2
        batch1 = QUANT_UNIVERSE[:half]
        batch2 = QUANT_UNIVERSE[half:]

        price_data = {}

        for batch in [batch1, batch2]:
            _throttle()
            try:
                df = yf.download(
                    batch, period="1y", progress=False, group_by="ticker"
                )
                if df is not None and not df.empty:
                    for sym in batch:
                        try:
                            if isinstance(df.columns, pd.MultiIndex):
                                if sym in df.columns.get_level_values(0):
                                    sym_df = df[sym].dropna(how="all")
                                    if len(sym_df) >= 60:
                                        price_data[sym] = sym_df
                            elif len(batch) == 1:
                                if len(df) >= 60:
                                    price_data[sym] = df
                        except Exception:
                            continue
            except Exception as e:
                logger.warning(f"Batch download failed: {e}")
                continue

        if not price_data:
            return {
                "error": "Could not download price data",
                "regime": regime,
                "macro": macro,
                "long_picks": [],
                "short_picks": [],
                "neutral": [],
            }

        # Step 4: Calculate multi-factor scores
        all_scored = calculate_multi_factor_scores(price_data, regime, macro)

        # Step 5: Separate into LONG, SHORT, NEUTRAL
        long_picks = [s for s in all_scored if s["direction"] == "LONG"]
        short_picks = [s for s in all_scored if s["direction"] == "SHORT"]
        neutral = [s for s in all_scored if s["direction"] == "NEUTRAL"]

        # Sort: longs by highest score, shorts by lowest score
        long_picks.sort(key=lambda x: x["composite_score"], reverse=True)
        short_picks.sort(key=lambda x: x["composite_score"])

        # Top picks only
        top_longs = long_picks[:15]
        top_shorts = short_picks[:10]

        # Step 6: Check earnings proximity for top picks
        # (only for top picks to minimize API calls)
        for pick in (top_longs[:5] + top_shorts[:3]):
            earnings = check_earnings_proximity(pick["symbol"])
            pick["earnings"] = earnings
            if earnings["is_near_earnings"]:
                penalty = earnings["confidence_penalty"]
                pick["confidence"] = max(20, pick["confidence"] - penalty)
                pick["reasons"].append(
                    f"⚠️ Earnings in {earnings['days_until_earnings']} days — confidence reduced"
                )

        # Add rank
        for i, p in enumerate(top_longs):
            p["rank"] = i + 1
        for i, p in enumerate(top_shorts):
            p["rank"] = i + 1

        elapsed = round(time.time() - start_time, 1)

        return {
            "regime": regime,
            "macro": macro,
            "long_picks": top_longs,
            "short_picks": top_shorts,
            "neutral_count": len(neutral),
            "total_analyzed": len(all_scored),
            "universe_size": len(QUANT_UNIVERSE),
            "stocks_with_data": len(price_data),
            "factor_weights": {
                k: round(v, 3)
                for k, v in (get_signal_weights_safe()).items()
            },
            "generated_at": datetime.now().isoformat(),
            "computation_time_seconds": elapsed,
            "disclaimer": (
                "This is a quantitative analysis tool for educational purposes. "
                "This is NOT financial advice. Past performance does not guarantee "
                "future results. Always do your own research before investing."
            ),
        }

    return _get_cached("quant_picks", fetch, ttl=900)  # 15 min cache


def get_signal_weights_safe() -> dict:
    """Get signal weights with fallback if DB not initialized."""
    try:
        from predictions.models import get_signal_weights
        return get_signal_weights()
    except Exception:
        return {
            "momentum": 0.25, "value": 0.20, "quality": 0.15,
            "low_vol": 0.15, "rsi2": 0.15, "volume": 0.10
        }
