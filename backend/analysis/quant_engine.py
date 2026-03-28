"""
Quantitative Engine — the core intelligence of the Epic Fury hedge fund.

This is the brain that institutional quant funds use to find edges:
  1. Market Regime Detection — BULL / BEAR / SIDEWAYS
  2. Multi-Factor Composite Scoring — 10 orthogonal, z-scored factors
  3. Global Macro Overlay — bonds, oil, gold, VIX, yield curve, dollar → sector adjustments
  4. Event-Driven — earnings proximity risk reduction
  5. Long/Short Signal Generation — regime-adaptive thresholds
  6. VIX Term Structure Sentiment — contango vs backwardation (fear gauge)
  7. Bollinger Band Squeeze Detection — volatility compression → breakout predictor
  8. Correlation-Aware Diversification — avoid concentrated correlated bets
  9. VWAP Factor — institutional execution quality signal
  10. Trailing Stop Intelligence — lock in profits, don't give them back

Designed for accuracy when real money is on the line:
  - Z-score normalization ensures fair cross-factor comparison
  - Regime-aware position sizing and signal filtering
  - Macro overlay prevents fighting the Fed / macro trends
  - Batch yfinance downloads to minimize API calls (CRITICAL)
  - Aggressive caching to avoid Yahoo Finance rate limits
  - Self-learning: weights adjust based on historical performance

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

# Large-cap & mid-cap liquid stocks across all S&P 500 sectors
# 200+ stocks for maximum trade generation and diversification
QUANT_UNIVERSE = [
    # Technology (30)
    "AAPL", "MSFT", "NVDA", "AVGO", "AMD", "ADBE", "CRM", "INTC", "QCOM", "TXN",
    "AMAT", "LRCX", "KLAC", "MRVL", "SNPS", "CDNS", "NXPI", "MCHP", "ON", "FTNT",
    "PANW", "NOW", "WDAY", "TEAM", "DDOG", "ZS", "CRWD", "SNOW", "MDB", "NET",
    # Communication (12)
    "GOOGL", "META", "NFLX", "DIS", "CMCSA", "TMUS", "VZ", "T", "CHTR", "EA",
    "TTWO", "RBLX",
    # Consumer Discretionary (20)
    "AMZN", "TSLA", "HD", "MCD", "NKE", "SBUX", "TJX", "LOW", "BKNG", "CMG",
    "ROST", "DHI", "LEN", "ORLY", "AZO", "POOL", "DECK", "ULTA", "ETSY", "ABNB",
    # Consumer Staples (15)
    "WMT", "PG", "COST", "KO", "PEP", "PM", "MO", "CL", "KMB", "MDLZ",
    "GIS", "HSY", "SJM", "STZ", "EL",
    # Healthcare (25)
    "UNH", "LLY", "JNJ", "ABBV", "MRK", "PFE", "TMO", "ABT", "BMY", "AMGN",
    "GILD", "ISRG", "VRTX", "REGN", "DXCM", "IDXX", "ZTS", "VEEV", "ALGN", "HOLX",
    "IQV", "EW", "SYK", "BDX", "HCA",
    # Financials (20)
    "JPM", "V", "MA", "BAC", "GS", "MS", "WFC", "C", "BLK", "SCHW",
    "AXP", "CB", "MMC", "ICE", "CME", "MCO", "MSCI", "FIS", "COIN", "HOOD",
    # Industrials (20)
    "BA", "CAT", "HON", "GE", "UNP", "RTX", "LMT", "DE", "FDX", "WM",
    "GD", "NOC", "CSX", "NSC", "ITW", "PH", "ROK", "EMR", "TT", "VRSK",
    # Energy (12)
    "XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX", "OXY", "DVN", "HES",
    "FANG", "VLO",
    # Materials (10)
    "LIN", "APD", "SHW", "FCX", "NEM", "ECL", "DD", "VMC", "MLM", "NUE",
    # Real Estate (8)
    "AMT", "PLD", "SPG", "CCI", "EQIX", "DLR", "O", "WELL",
    # Utilities (8)
    "NEE", "DUK", "SO", "AEP", "SRE", "D", "EXC", "XEL",
    # ETFs for sector-level signals (10)
    "SPY", "QQQ", "IWM", "XLF", "XLE", "XLV", "XLK", "XLI", "XLP", "XLU",
]

# Sector mapping for macro overlay adjustments — auto-generated for all 200+ stocks
SECTOR_MAP = {
    # Technology (30)
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology",
    "AVGO": "Technology", "AMD": "Technology", "ADBE": "Technology",
    "CRM": "Technology", "INTC": "Technology", "QCOM": "Technology",
    "TXN": "Technology", "AMAT": "Technology", "LRCX": "Technology",
    "KLAC": "Technology", "MRVL": "Technology", "SNPS": "Technology",
    "CDNS": "Technology", "NXPI": "Technology", "MCHP": "Technology",
    "ON": "Technology", "FTNT": "Technology", "PANW": "Technology",
    "NOW": "Technology", "WDAY": "Technology", "TEAM": "Technology",
    "DDOG": "Technology", "ZS": "Technology", "CRWD": "Technology",
    "SNOW": "Technology", "MDB": "Technology", "NET": "Technology",
    # Communication (12)
    "GOOGL": "Communication", "META": "Communication", "NFLX": "Communication",
    "DIS": "Communication", "CMCSA": "Communication", "TMUS": "Communication",
    "VZ": "Communication", "T": "Communication", "CHTR": "Communication",
    "EA": "Communication", "TTWO": "Communication", "RBLX": "Communication",
    # Consumer Discretionary (20)
    "AMZN": "Consumer Discretionary", "TSLA": "Consumer Discretionary",
    "HD": "Consumer Discretionary", "MCD": "Consumer Discretionary",
    "NKE": "Consumer Discretionary", "SBUX": "Consumer Discretionary",
    "TJX": "Consumer Discretionary", "LOW": "Consumer Discretionary",
    "BKNG": "Consumer Discretionary", "CMG": "Consumer Discretionary",
    "ROST": "Consumer Discretionary", "DHI": "Consumer Discretionary",
    "LEN": "Consumer Discretionary", "ORLY": "Consumer Discretionary",
    "AZO": "Consumer Discretionary", "POOL": "Consumer Discretionary",
    "DECK": "Consumer Discretionary", "ULTA": "Consumer Discretionary",
    "ETSY": "Consumer Discretionary", "ABNB": "Consumer Discretionary",
    # Consumer Staples (15)
    "WMT": "Consumer Staples", "PG": "Consumer Staples", "COST": "Consumer Staples",
    "KO": "Consumer Staples", "PEP": "Consumer Staples", "PM": "Consumer Staples",
    "MO": "Consumer Staples", "CL": "Consumer Staples", "KMB": "Consumer Staples",
    "MDLZ": "Consumer Staples", "GIS": "Consumer Staples", "HSY": "Consumer Staples",
    "SJM": "Consumer Staples", "STZ": "Consumer Staples", "EL": "Consumer Staples",
    # Healthcare (25)
    "UNH": "Healthcare", "LLY": "Healthcare", "JNJ": "Healthcare",
    "ABBV": "Healthcare", "MRK": "Healthcare", "PFE": "Healthcare",
    "TMO": "Healthcare", "ABT": "Healthcare", "BMY": "Healthcare",
    "AMGN": "Healthcare", "GILD": "Healthcare", "ISRG": "Healthcare",
    "VRTX": "Healthcare", "REGN": "Healthcare", "DXCM": "Healthcare",
    "IDXX": "Healthcare", "ZTS": "Healthcare", "VEEV": "Healthcare",
    "ALGN": "Healthcare", "HOLX": "Healthcare", "IQV": "Healthcare",
    "EW": "Healthcare", "SYK": "Healthcare", "BDX": "Healthcare",
    "HCA": "Healthcare",
    # Financials (20)
    "JPM": "Financials", "V": "Financials", "MA": "Financials",
    "BAC": "Financials", "GS": "Financials", "MS": "Financials",
    "WFC": "Financials", "C": "Financials", "BLK": "Financials",
    "SCHW": "Financials", "AXP": "Financials", "CB": "Financials",
    "MMC": "Financials", "ICE": "Financials", "CME": "Financials",
    "MCO": "Financials", "MSCI": "Financials", "FIS": "Financials",
    "COIN": "Financials", "HOOD": "Financials",
    # Industrials (20)
    "BA": "Industrials", "CAT": "Industrials", "HON": "Industrials",
    "GE": "Industrials", "UNP": "Industrials", "RTX": "Industrials",
    "LMT": "Industrials", "DE": "Industrials", "FDX": "Industrials",
    "WM": "Industrials", "GD": "Industrials", "NOC": "Industrials",
    "CSX": "Industrials", "NSC": "Industrials", "ITW": "Industrials",
    "PH": "Industrials", "ROK": "Industrials", "EMR": "Industrials",
    "TT": "Industrials", "VRSK": "Industrials",
    # Energy (12)
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy",
    "SLB": "Energy", "EOG": "Energy", "MPC": "Energy", "PSX": "Energy",
    "OXY": "Energy", "DVN": "Energy", "HES": "Energy",
    "FANG": "Energy", "VLO": "Energy",
    # Materials (10)
    "LIN": "Materials", "APD": "Materials", "SHW": "Materials",
    "FCX": "Materials", "NEM": "Materials", "ECL": "Materials",
    "DD": "Materials", "VMC": "Materials", "MLM": "Materials",
    "NUE": "Materials",
    # Real Estate (8)
    "AMT": "Real Estate", "PLD": "Real Estate", "SPG": "Real Estate",
    "CCI": "Real Estate", "EQIX": "Real Estate", "DLR": "Real Estate",
    "O": "Real Estate", "WELL": "Real Estate",
    # Utilities (8)
    "NEE": "Utilities", "DUK": "Utilities", "SO": "Utilities",
    "AEP": "Utilities", "SRE": "Utilities", "D": "Utilities",
    "EXC": "Utilities", "XEL": "Utilities",
    # ETFs (10) — sector-level signals
    "SPY": "ETF", "QQQ": "ETF", "IWM": "ETF", "XLF": "ETF",
    "XLE": "ETF", "XLV": "ETF", "XLK": "ETF", "XLI": "ETF",
    "XLP": "ETF", "XLU": "ETF",
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

        # --- ADVANCED: Yield Curve Inversion Detection ---
        # 10Y-2Y spread: if negative = inverted = recession signal
        # This predicted every recession since 1970 with 12-18 month lead
        try:
            _throttle()
            tnx_2y_df = yf.download(["^TNX", "^IRX"], period="1mo", progress=False, group_by="ticker")
            if tnx_2y_df is not None and not tnx_2y_df.empty:
                try:
                    tnx_close = tnx_2y_df[("^TNX", "Close")].dropna().values.astype(float)
                    irx_close = tnx_2y_df[("^IRX", "Close")].dropna().values.astype(float)
                    if len(tnx_close) > 0 and len(irx_close) > 0:
                        spread = float(tnx_close[-1]) - float(irx_close[-1])
                        macro["yield_curve"] = {
                            "spread_10y_3m": round(spread, 2),
                            "inverted": spread < 0,
                            "signal": "recession_warning" if spread < 0 else (
                                "caution" if spread < 0.5 else "normal"
                            ),
                        }
                        if spread < 0:
                            # Inverted yield curve: penalize cyclicals, boost defensives
                            for sector in ["Technology", "Consumer Discretionary", "Financials", "Industrials"]:
                                adjustments[sector] = round(adjustments.get(sector, 0) - 0.5, 1)
                            for sector in ["Healthcare", "Consumer Staples", "Utilities"]:
                                adjustments[sector] = round(adjustments.get(sector, 0) + 0.5, 1)
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"Yield curve check failed: {e}")

        # --- ADVANCED: VIX Term Structure (Contango vs Backwardation) ---
        # VIX futures in contango (VIX < VIX3M) = calm markets, normal = slightly bullish
        # VIX futures in backwardation (VIX > VIX3M) = panic = strongly bearish
        # This is what the smart money watches — it predicted the 2020 crash
        try:
            _throttle()
            vix_term_df = yf.download(["^VIX", "^VIX3M"], period="5d", progress=False, group_by="ticker")
            if vix_term_df is not None and not vix_term_df.empty:
                try:
                    vix_spot = float(vix_term_df[("^VIX", "Close")].dropna().iloc[-1])
                    vix_3m = float(vix_term_df[("^VIX3M", "Close")].dropna().iloc[-1])
                    term_ratio = vix_spot / vix_3m if vix_3m > 0 else 1.0
                    macro["vix_term_structure"] = {
                        "vix_spot": round(vix_spot, 2),
                        "vix_3m": round(vix_3m, 2),
                        "ratio": round(term_ratio, 3),
                        "structure": "backwardation" if term_ratio > 1.05 else (
                            "contango" if term_ratio < 0.95 else "flat"
                        ),
                        "signal": "extreme_fear" if term_ratio > 1.15 else (
                            "fear" if term_ratio > 1.05 else (
                                "complacent" if term_ratio < 0.85 else "normal"
                            )
                        ),
                    }
                    # Backwardation = panic: penalize all risk assets
                    if term_ratio > 1.10:
                        for sector in ["Technology", "Consumer Discretionary", "Communication"]:
                            adjustments[sector] = round(adjustments.get(sector, 0) - 1.0, 1)
                        for sector in ["Utilities", "Consumer Staples", "Healthcare"]:
                            adjustments[sector] = round(adjustments.get(sector, 0) + 0.5, 1)
                    elif term_ratio > 1.05:
                        for sector in ["Technology", "Consumer Discretionary"]:
                            adjustments[sector] = round(adjustments.get(sector, 0) - 0.5, 1)
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"VIX term structure check failed: {e}")

        # --- ADVANCED: Dollar Strength (DXY proxy via UUP ETF) ---
        # Strong dollar hurts multinationals, helps domestic companies
        try:
            _throttle()
            uup_df = yf.download("UUP", period="1mo", progress=False)
            if uup_df is not None and len(uup_df) >= 20:
                uup_closes = uup_df["Close"].values.astype(float).flatten()
                uup_sma20 = float(np.mean(uup_closes[-20:]))
                uup_current = float(uup_closes[-1])
                dollar_trend = "strengthening" if uup_current > uup_sma20 * 1.005 else (
                    "weakening" if uup_current < uup_sma20 * 0.995 else "flat"
                )
                macro["dollar_index"] = {
                    "proxy_value": round(uup_current, 2),
                    "trend": dollar_trend,
                }
                # Strong dollar hurts Energy & Materials (commodity exporters)
                if dollar_trend == "strengthening":
                    adjustments["Energy"] = round(adjustments.get("Energy", 0) - 0.5, 1)
                    adjustments["Materials"] = round(adjustments.get("Materials", 0) - 0.5, 1)
                elif dollar_trend == "weakening":
                    adjustments["Energy"] = round(adjustments.get("Energy", 0) + 0.3, 1)
                    adjustments["Materials"] = round(adjustments.get("Materials", 0) + 0.3, 1)
        except Exception as e:
            logger.debug(f"Dollar check failed: {e}")

        macro["sector_adjustments"] = adjustments
        return macro

    return _get_cached("macro_overlay", fetch, ttl=600)  # 10 min cache


# ============================================================
#  2B. OVERNIGHT & PRE-MARKET INTELLIGENCE
#  Detects weekend news impact, overnight futures shifts,
#  and global market moves BEFORE the US opens.
#  This is what separates smart funds from dumb money.
# ============================================================

_overnight_cache = {}
_OVERNIGHT_CACHE_TTL = 300  # 5 min cache


def scan_overnight_intelligence() -> dict:
    """
    Pre-market intelligence scanner — runs before first trade of the day.

    Checks:
      1. S&P 500 Futures (ES=F) — overnight direction of US market
      2. Nasdaq Futures (NQ=F) — tech-heavy overnight signal
      3. European markets (EZU ETF) — already trading before US open
      4. Asian markets (EWJ Japan, FXI China) — closed by US open, shows overnight sentiment
      5. US Dollar (UUP) — overnight dollar moves affect multinationals
      6. Oil futures (CL=F) — overnight energy shifts
      7. Gold (GC=F) — safe-haven demand overnight
      8. Bitcoin (BTC-USD) — 24/7 risk sentiment proxy (trades weekends too)

    Returns adjustment scores and signals the auto-trader uses to adapt.
    """
    now = time.time()
    cache_key = "overnight_intel"
    if cache_key in _overnight_cache and now - _overnight_cache[cache_key]["time"] < _OVERNIGHT_CACHE_TTL:
        return _overnight_cache[cache_key]["data"]

    intel = {
        "futures_sentiment": "neutral",  # bullish / bearish / neutral
        "overnight_gap_pct": 0.0,        # expected gap % at open
        "global_risk_mood": "neutral",   # risk-on / risk-off / neutral
        "weekend_shift_detected": False,
        "signals": [],
        "sector_adjustments": {},        # overnight-specific sector boosts/penalties
        "confidence_modifier": 0,        # +/- applied to all trade confidence
        "position_size_modifier": 1.0,   # multiply position sizes (0.5 = half size, 1.5 = bigger)
        "timestamp": datetime.now().isoformat(),
    }

    bullish_signals = 0
    bearish_signals = 0

    # --- 1. US Futures (ES=F for S&P, NQ=F for Nasdaq) ---
    # These trade nearly 24/7 including Sunday evening — perfect for weekend shifts
    try:
        _throttle()
        futures_df = yf.download(["ES=F", "NQ=F"], period="5d", progress=False, group_by="ticker")
        if futures_df is not None and not futures_df.empty:
            for sym, label in [("ES=F", "sp500_futures"), ("NQ=F", "nasdaq_futures")]:
                try:
                    if isinstance(futures_df.columns, pd.MultiIndex):
                        closes = futures_df[(sym, "Close")].dropna().values.astype(float).flatten()
                    else:
                        continue
                    if len(closes) >= 2:
                        current = float(closes[-1])
                        prev = float(closes[-2])
                        change_pct = ((current / prev) - 1) * 100 if prev > 0 else 0

                        intel[label] = {
                            "price": round(current, 2),
                            "change_pct": round(change_pct, 2),
                        }

                        if change_pct > 0.5:
                            bullish_signals += 2
                            intel["signals"].append(f"{label}: +{change_pct:.1f}% overnight (bullish)")
                        elif change_pct > 0.2:
                            bullish_signals += 1
                            intel["signals"].append(f"{label}: +{change_pct:.1f}% overnight (mildly bullish)")
                        elif change_pct < -0.5:
                            bearish_signals += 2
                            intel["signals"].append(f"{label}: {change_pct:.1f}% overnight (bearish)")
                        elif change_pct < -0.2:
                            bearish_signals += 1
                            intel["signals"].append(f"{label}: {change_pct:.1f}% overnight (mildly bearish)")

                        if sym == "ES=F":
                            intel["overnight_gap_pct"] = round(change_pct, 2)
                except Exception:
                    continue
    except Exception as e:
        logger.debug(f"Overnight futures scan failed: {e}")

    # --- 2. Global Markets (Europe + Asia) ---
    # Shows what happened while the US was asleep
    try:
        _throttle()
        global_df = yf.download(["EZU", "EWJ", "FXI"], period="5d", progress=False, group_by="ticker")
        if global_df is not None and not global_df.empty:
            for sym, region in [("EZU", "europe"), ("EWJ", "japan"), ("FXI", "china")]:
                try:
                    if isinstance(global_df.columns, pd.MultiIndex):
                        closes = global_df[(sym, "Close")].dropna().values.astype(float).flatten()
                    else:
                        continue
                    if len(closes) >= 2:
                        current = float(closes[-1])
                        prev = float(closes[-2])
                        change_pct = ((current / prev) - 1) * 100 if prev > 0 else 0

                        intel[f"global_{region}"] = {
                            "change_pct": round(change_pct, 2),
                        }

                        if change_pct > 0.5:
                            bullish_signals += 1
                        elif change_pct < -0.5:
                            bearish_signals += 1

                        if abs(change_pct) > 1.0:
                            intel["signals"].append(f"{region}: {change_pct:+.1f}% — significant move")
                except Exception:
                    continue
    except Exception as e:
        logger.debug(f"Global markets scan failed: {e}")

    # --- 3. Bitcoin (24/7 risk sentiment — trades weekends) ---
    # If BTC crashes over the weekend, Monday will likely be rough
    try:
        _throttle()
        btc_df = yf.download("BTC-USD", period="5d", progress=False)
        if btc_df is not None and len(btc_df) >= 2:
            btc_closes = btc_df["Close"].values.astype(float).flatten()
            btc_current = float(btc_closes[-1])
            btc_prev = float(btc_closes[-2])
            btc_change = ((btc_current / btc_prev) - 1) * 100 if btc_prev > 0 else 0

            intel["bitcoin"] = {
                "price": round(btc_current, 2),
                "change_pct": round(btc_change, 2),
            }

            # BTC is a weekend risk gauge — big moves signal risk sentiment shift
            if btc_change > 3:
                bullish_signals += 1
                intel["signals"].append(f"Bitcoin +{btc_change:.1f}% — risk-on weekend sentiment")
            elif btc_change < -3:
                bearish_signals += 2
                intel["signals"].append(f"Bitcoin {btc_change:.1f}% — risk-off weekend sentiment")
                intel["weekend_shift_detected"] = True
            elif btc_change < -5:
                bearish_signals += 3
                intel["signals"].append(f"Bitcoin CRASH {btc_change:.1f}% — extreme risk-off, reduce exposure")
                intel["weekend_shift_detected"] = True
    except Exception as e:
        logger.debug(f"Bitcoin overnight scan failed: {e}")

    # --- 4. Safe Haven Check (Gold + Treasuries overnight) ---
    try:
        _throttle()
        haven_df = yf.download(["GC=F", "TLT"], period="5d", progress=False, group_by="ticker")
        if haven_df is not None and not haven_df.empty:
            for sym, label in [("GC=F", "gold_overnight"), ("TLT", "bonds_overnight")]:
                try:
                    if isinstance(haven_df.columns, pd.MultiIndex):
                        closes = haven_df[(sym, "Close")].dropna().values.astype(float).flatten()
                    else:
                        continue
                    if len(closes) >= 2:
                        current = float(closes[-1])
                        prev = float(closes[-2])
                        change_pct = ((current / prev) - 1) * 100 if prev > 0 else 0
                        intel[label] = {"change_pct": round(change_pct, 2)}

                        # Gold/bonds spiking = flight to safety = bearish for stocks
                        if change_pct > 1.0:
                            bearish_signals += 1
                            intel["signals"].append(f"{label}: +{change_pct:.1f}% — flight to safety")
                except Exception:
                    continue
    except Exception as e:
        logger.debug(f"Safe haven overnight scan failed: {e}")

    # --- Synthesize: Overall overnight sentiment ---
    net = bullish_signals - bearish_signals

    if net >= 4:
        intel["futures_sentiment"] = "strong_bullish"
        intel["confidence_modifier"] = 8
        intel["position_size_modifier"] = 1.2
        intel["signals"].append("OVERNIGHT VERDICT: Strong bullish — increase long exposure")
    elif net >= 2:
        intel["futures_sentiment"] = "bullish"
        intel["confidence_modifier"] = 4
        intel["position_size_modifier"] = 1.1
        intel["signals"].append("OVERNIGHT VERDICT: Mildly bullish — favor longs")
    elif net <= -4:
        intel["futures_sentiment"] = "strong_bearish"
        intel["confidence_modifier"] = -8
        intel["position_size_modifier"] = 0.7
        intel["signals"].append("OVERNIGHT VERDICT: Strong bearish — reduce exposure, favor shorts")
        intel["weekend_shift_detected"] = True
    elif net <= -2:
        intel["futures_sentiment"] = "bearish"
        intel["confidence_modifier"] = -4
        intel["position_size_modifier"] = 0.85
        intel["signals"].append("OVERNIGHT VERDICT: Mildly bearish — caution on longs")
    else:
        intel["futures_sentiment"] = "neutral"
        intel["confidence_modifier"] = 0
        intel["position_size_modifier"] = 1.0
        intel["signals"].append("OVERNIGHT VERDICT: Neutral — no significant overnight shifts")

    # Sector-specific overnight adjustments
    gap = intel["overnight_gap_pct"]
    sector_adj = {}
    if gap > 0.5:
        # Gap up: favor growth, reduce defensives
        sector_adj["Technology"] = 0.5
        sector_adj["Consumer Discretionary"] = 0.3
        sector_adj["Utilities"] = -0.3
        sector_adj["Consumer Staples"] = -0.2
    elif gap < -0.5:
        # Gap down: favor defensives, reduce growth
        sector_adj["Technology"] = -0.5
        sector_adj["Consumer Discretionary"] = -0.5
        sector_adj["Healthcare"] = 0.3
        sector_adj["Utilities"] = 0.3
        sector_adj["Consumer Staples"] = 0.3
    intel["sector_adjustments"] = sector_adj

    intel["bullish_signals"] = bullish_signals
    intel["bearish_signals"] = bearish_signals

    _overnight_cache[cache_key] = {"data": intel, "time": now}
    logger.warning(f"OVERNIGHT INTEL: {intel['futures_sentiment']} | gap={gap:+.2f}% | bull={bullish_signals} bear={bearish_signals}")
    return intel


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

            # --- Factor 7: SMART MONEY DIVERGENCE (Price-Volume Divergence) ---
            # When price makes new lows but volume decreases = smart money accumulating
            # When price makes new highs but volume decreases = smart money distributing
            # This is what Goldman and Citadel look for — institutional footprint
            smart_money_raw = 0.0
            if len(closes) >= 20 and len(volumes) >= 20:
                recent_closes = closes[-20:]
                recent_vols = volumes[-20:]
                first_half_price = np.mean(recent_closes[:10])
                second_half_price = np.mean(recent_closes[10:])
                first_half_vol = np.mean(recent_vols[:10])
                second_half_vol = np.mean(recent_vols[10:])

                price_direction = 1 if second_half_price > first_half_price else -1
                vol_direction = 1 if second_half_vol > first_half_vol else -1

                if price_direction == -1 and vol_direction == -1:
                    # Price falling on declining volume = accumulation (bullish divergence)
                    smart_money_raw = 2.0
                elif price_direction == 1 and vol_direction == -1:
                    # Price rising on declining volume = distribution (bearish divergence)
                    smart_money_raw = -2.0
                elif price_direction == 1 and vol_direction == 1:
                    # Price rising on rising volume = confirmed uptrend
                    smart_money_raw = 1.0
                elif price_direction == -1 and vol_direction == 1:
                    # Price falling on rising volume = confirmed downtrend (panic selling)
                    smart_money_raw = -1.0

            # --- Factor 8: RELATIVE STRENGTH vs SECTOR ---
            # Don't just buy good stocks — buy the BEST in their sector
            # A stock outperforming its sector peers has sector-relative alpha
            relative_strength_raw = 0.0
            sector = SECTOR_MAP.get(symbol, "Unknown")
            sector_peers = [s for s, sec in SECTOR_MAP.items()
                           if sec == sector and s != symbol and s in price_data]
            if len(closes) >= 60 and sector_peers:
                stock_ret_60d = (closes[-1] / closes[-60]) - 1
                peer_rets = []
                for peer in sector_peers:
                    try:
                        peer_closes = price_data[peer]["Close"].values.astype(float).flatten()
                        if len(peer_closes) >= 60:
                            peer_rets.append((peer_closes[-1] / peer_closes[-60]) - 1)
                    except Exception:
                        continue
                if peer_rets:
                    sector_avg_ret = float(np.mean(peer_rets))
                    # How much this stock outperforms its sector (in %)
                    relative_strength_raw = (stock_ret_60d - sector_avg_ret) * 100

            # --- ADVANCED: MOMENTUM CRASH FILTER ---
            # Momentum works great... until it doesn't. Momentum crashes happen when
            # high-momentum stocks suddenly reverse. We detect this by checking if
            # recent 5-day return is opposite to the 60-day trend. If a stock was
            # trending up but just had a sharp 5-day drop = momentum unwinding = danger.
            momentum_crash_flag = False
            if len(closes) >= 60:
                ret_60d = (closes[-1] / closes[-60]) - 1
                ret_5d = (closes[-1] / closes[-5]) - 1
                # Strong uptrend but sharp recent reversal
                if ret_60d > 0.10 and ret_5d < -0.05:
                    momentum_crash_flag = True  # momentum unwinding — avoid LONG
                # Strong downtrend but sharp recent bounce
                elif ret_60d < -0.10 and ret_5d > 0.05:
                    momentum_crash_flag = True  # dead cat bounce — avoid LONG

            # --- ADVANCED: GAP DETECTION ---
            # Overnight gaps reveal institutional order flow
            # Large gap up = institutions buying overnight = bullish
            # Large gap down = institutions selling overnight = bearish
            gap_signal = 0.0
            if len(closes) >= 2:
                try:
                    opens = df["Open"].values.astype(float).flatten()
                    if len(opens) >= 2:
                        # Today's gap: today's open vs yesterday's close
                        gap_pct = (opens[-1] / closes[-2] - 1) * 100
                        if gap_pct > 1.5:
                            gap_signal = 2.0   # big gap up = institutional buying
                        elif gap_pct > 0.5:
                            gap_signal = 1.0   # mild gap up
                        elif gap_pct < -1.5:
                            gap_signal = -2.0  # big gap down = institutional selling
                        elif gap_pct < -0.5:
                            gap_signal = -1.0  # mild gap down
                except Exception:
                    pass

            # --- Factor 9: BOLLINGER BAND SQUEEZE (Volatility Compression) ---
            # When Bollinger Bands narrow (squeeze), a big move is coming
            # The direction of the breakout tells us which way
            # This is John Bollinger's own recommended setup
            bb_squeeze_raw = 0.0
            if len(closes) >= 20:
                sma20_bb = float(np.mean(closes[-20:]))
                std20_bb = float(np.std(closes[-20:]))
                bb_upper = sma20_bb + 2 * std20_bb
                bb_lower = sma20_bb - 2 * std20_bb
                bb_width = (bb_upper - bb_lower) / sma20_bb * 100  # as % of price

                # Historical average BB width for comparison
                if len(closes) >= 120:
                    bb_widths_hist = []
                    for k in range(100, len(closes), 5):
                        s = float(np.mean(closes[k-20:k]))
                        st = float(np.std(closes[k-20:k]))
                        if s > 0:
                            bb_widths_hist.append(((s + 2*st) - (s - 2*st)) / s * 100)
                    if bb_widths_hist:
                        avg_bb_width = float(np.mean(bb_widths_hist))
                        # Squeeze = current width < 60% of average
                        if bb_width < avg_bb_width * 0.6:
                            # Squeeze detected! Direction based on price vs SMA
                            if current_price > sma20_bb:
                                bb_squeeze_raw = 3.0  # Squeeze + above SMA = bullish breakout
                            else:
                                bb_squeeze_raw = -3.0  # Squeeze + below SMA = bearish breakdown
                        elif bb_width < avg_bb_width * 0.8:
                            # Mild squeeze
                            if current_price > sma20_bb:
                                bb_squeeze_raw = 1.0
                            else:
                                bb_squeeze_raw = -1.0

            # --- Factor 10: VWAP PROXIMITY (Institutional Execution Quality) ---
            # VWAP = Volume Weighted Average Price — institutional benchmark
            # Stocks trading above VWAP = institutions buying at premium = bullish
            # Stocks trading below VWAP = institutions selling = bearish
            # We use a 5-day VWAP proxy since we don't have intraday data
            vwap_raw = 0.0
            if len(closes) >= 5 and len(volumes) >= 5:
                try:
                    highs = df["High"].values.astype(float).flatten()
                    lows = df["Low"].values.astype(float).flatten()
                    if len(highs) >= 5 and len(lows) >= 5:
                        # Typical price * volume / cumulative volume
                        typical_prices = (highs[-5:] + lows[-5:] + closes[-5:]) / 3
                        vwap_5d = float(np.sum(typical_prices * volumes[-5:]) /
                                       (np.sum(volumes[-5:]) + 1))
                        vwap_pct = (current_price - vwap_5d) / vwap_5d * 100
                        if vwap_pct > 1.5:
                            vwap_raw = 2.0  # Trading well above VWAP = institutional buying
                        elif vwap_pct > 0.3:
                            vwap_raw = 1.0
                        elif vwap_pct < -1.5:
                            vwap_raw = -2.0  # Trading well below VWAP = institutional selling
                        elif vwap_pct < -0.3:
                            vwap_raw = -1.0
                except Exception:
                    pass

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
                "smart_money_raw": smart_money_raw,
                "relative_strength_raw": relative_strength_raw,
                "bb_squeeze_raw": bb_squeeze_raw,
                "vwap_raw": vwap_raw,
                "gap_signal": gap_signal,
                "momentum_crash": momentum_crash_flag,
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
    smart_money_z = _safe_zscore([s["smart_money_raw"] for s in raw_factors])
    relative_strength_z = _safe_zscore([s["relative_strength_raw"] for s in raw_factors])
    bb_squeeze_z = _safe_zscore([s["bb_squeeze_raw"] for s in raw_factors])
    vwap_z = _safe_zscore([s["vwap_raw"] for s in raw_factors])

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
    # Add new factors with fixed weights (not yet in learning system)
    W_SMART_MONEY = 0.08    # Smart money divergence
    W_REL_STRENGTH = 0.06   # Relative strength vs sector
    W_BB_SQUEEZE = 0.06     # Bollinger Band squeeze breakout
    W_VWAP = 0.05           # VWAP institutional flow

    # Scale existing weights down to make room for new factors
    existing_total = sum(weights.values())
    new_factor_total = W_SMART_MONEY + W_REL_STRENGTH + W_BB_SQUEEZE + W_VWAP
    scale = (1.0 - new_factor_total)  # existing factors share this portion

    w_mom = (weights.get("momentum", 0.25) / existing_total) * scale
    w_val = (weights.get("value", 0.20) / existing_total) * scale
    w_qual = (weights.get("quality", 0.15) / existing_total) * scale
    w_lvol = (weights.get("low_vol", 0.15) / existing_total) * scale
    w_rsi2 = (weights.get("rsi2", 0.15) / existing_total) * scale
    w_vol = (weights.get("volume", 0.10) / existing_total) * scale
    w_smart = W_SMART_MONEY
    w_relstr = W_REL_STRENGTH
    w_bb = W_BB_SQUEEZE
    w_vwap = W_VWAP

    # --- Calculate composite scores ---
    scored = []
    for i, stock in enumerate(raw_factors):
        # Weighted composite — 10 FACTORS (institutional hedge fund grade)
        composite = (
            momentum_z[i] * w_mom +
            value_z[i] * w_val +
            quality_z[i] * w_qual +
            low_vol_z[i] * w_lvol +
            rsi2_z[i] * w_rsi2 +
            volume_z[i] * w_vol +
            smart_money_z[i] * w_smart +
            relative_strength_z[i] * w_relstr +
            bb_squeeze_z[i] * w_bb +
            vwap_z[i] * w_vwap
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

        # Determine direction — REGIME AWARE
        # In BEAR: raise threshold for LONG (harder to buy), lower for SHORT (easier to short)
        # In BULL: lower threshold for LONG (easier to buy), raise for SHORT (harder to short)
        current_regime = regime.get("regime", "SIDEWAYS") if regime else "SIDEWAYS"

        if current_regime == "BEAR":
            long_threshold_high, long_threshold_low = 3.0, 1.5   # still allow quality longs — long-term success matters
            short_threshold_high, short_threshold_low = -3.0, -1.0  # easier to short
        elif current_regime == "BULL":
            long_threshold_high, long_threshold_low = 3.0, 1.0   # easier to go long
            short_threshold_high, short_threshold_low = -5.5, -3.5  # harder to short
        else:  # SIDEWAYS
            long_threshold_high, long_threshold_low = 4.0, 2.0
            short_threshold_high, short_threshold_low = -4.0, -2.0

        if final_score >= long_threshold_high:
            direction = "LONG"
            confidence = min(95, 60 + int(final_score * 3))
        elif final_score >= long_threshold_low:
            direction = "LONG"
            confidence = min(85, 50 + int(final_score * 5))
        elif final_score <= short_threshold_high:
            direction = "SHORT"
            confidence = min(95, 60 + int(abs(final_score) * 3))
        elif final_score <= short_threshold_low:
            direction = "SHORT"
            confidence = min(85, 50 + int(abs(final_score) * 5))
        else:
            direction = "NEUTRAL"
            confidence = max(30, 50 - int(abs(final_score) * 5))

        # MOMENTUM CRASH FILTER: avoid stocks where momentum is unwinding
        if stock.get("momentum_crash") and direction == "LONG":
            confidence = int(confidence * 0.4)  # massive penalty — momentum crashing

        # GAP SIGNAL: institutional overnight order flow
        gap = stock.get("gap_signal", 0)
        if gap >= 2 and direction == "LONG":
            confidence = min(95, int(confidence * 1.15))  # big gap up confirms long
        elif gap <= -2 and direction == "SHORT":
            confidence = min(95, int(confidence * 1.15))  # big gap down confirms short
        elif gap >= 1.5 and direction == "SHORT":
            confidence = int(confidence * 0.7)  # don't short into gap up
        elif gap <= -1.5 and direction == "LONG":
            confidence = int(confidence * 0.7)  # don't buy into gap down

        # TREND FILTER: Don't fight the trend — this is what separates pros from amateurs
        # In BEAR: penalize longs that are below 50-EMA (trend is down, don't buy falling knives)
        # In BEAR: boost shorts that are below 50-EMA (trend confirms short thesis)
        # In BULL: penalize shorts above 50-EMA, boost longs above 50-EMA
        ema50 = stock["ema_50"]
        price_vs_ema50 = (stock["price"] - ema50) / ema50 * 100  # % above/below 50-EMA

        # Defensive sectors are safer for longs even in bear markets
        _defensive = {"Consumer Staples", "Healthcare", "Utilities"}
        _is_defensive = stock.get("sector") in _defensive

        if current_regime == "BEAR":
            if direction == "LONG" and price_vs_ema50 < -3 and not _is_defensive:
                # Non-defensive stock 3%+ below 50-EMA in bear = bad idea
                confidence = int(confidence * 0.5)  # cut confidence in half
            elif direction == "LONG" and price_vs_ema50 < -8 and _is_defensive:
                # Even defensive stocks 8%+ below EMA are risky
                confidence = int(confidence * 0.6)
            elif direction == "SHORT" and price_vs_ema50 < -5:
                # Shorting a stock already 5%+ below 50-EMA in bear = confirmed trend
                confidence = min(95, int(confidence * 1.2))  # boost confidence
        elif current_regime == "BULL":
            if direction == "SHORT" and price_vs_ema50 > 3:
                confidence = int(confidence * 0.5)
            elif direction == "LONG" and price_vs_ema50 > 5:
                confidence = min(95, int(confidence * 1.2))

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
            "smart_money": {"z": smart_money_z[i], "weight": round(w_smart, 3),
                           "raw": round(stock["smart_money_raw"], 2),
                           "contribution": round(smart_money_z[i] * w_smart, 3)},
            "relative_strength": {"z": relative_strength_z[i], "weight": round(w_relstr, 3),
                                  "raw": round(stock["relative_strength_raw"], 2),
                                  "contribution": round(relative_strength_z[i] * w_relstr, 3)},
            "bb_squeeze": {"z": bb_squeeze_z[i], "weight": round(w_bb, 3),
                          "raw": round(stock["bb_squeeze_raw"], 2),
                          "contribution": round(bb_squeeze_z[i] * w_bb, 3)},
            "vwap": {"z": vwap_z[i], "weight": round(w_vwap, 3),
                    "raw": round(stock["vwap_raw"], 2),
                    "contribution": round(vwap_z[i] * w_vwap, 3)},
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
            "ticker": stock["symbol"],  # alias for frontend compatibility
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

        # Step 2B: Overnight/pre-market intelligence
        # Detects weekend news shifts, futures gaps, global market moves
        overnight = scan_overnight_intelligence()

        # Apply overnight sector adjustments to macro overlay
        for sector, adj in overnight.get("sector_adjustments", {}).items():
            if sector in macro.get("sector_adjustments", {}):
                macro["sector_adjustments"][sector] = round(
                    macro["sector_adjustments"][sector] + adj, 1
                )
            else:
                macro["sector_adjustments"][sector] = adj

        # Step 3: Batch download price data
        # Split universe into 4 batches to avoid Yahoo Finance limits (200+ stocks)
        batch_size = len(QUANT_UNIVERSE) // 4 + 1
        batches = [QUANT_UNIVERSE[i:i+batch_size] for i in range(0, len(QUANT_UNIVERSE), batch_size)]

        price_data = {}

        for batch in batches:
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

        # Top picks — show more since we have 200+ stocks
        top_longs = long_picks[:30]
        top_shorts = short_picks[:20]

        # Step 5B: Apply overnight confidence modifier to all picks
        # If futures tanked overnight, reduce long confidence; if bullish, boost it
        overnight_mod = overnight.get("confidence_modifier", 0)
        if overnight_mod != 0:
            for pick in top_longs:
                pick["confidence"] = max(15, min(95, pick["confidence"] + overnight_mod))
                if overnight_mod > 0:
                    pick["reasons"].append(f"Overnight bullish (+{overnight_mod}% confidence)")
                else:
                    pick["reasons"].append(f"Overnight bearish ({overnight_mod}% confidence)")
            for pick in top_shorts:
                # Shorts benefit from bearish overnight, hurt by bullish
                pick["confidence"] = max(15, min(95, pick["confidence"] - overnight_mod))
                if overnight_mod < 0:
                    pick["reasons"].append(f"Overnight bearish — shorts favored (+{abs(overnight_mod)}%)")
                elif overnight_mod > 0:
                    pick["reasons"].append(f"Overnight bullish — shorts less favored (-{overnight_mod}%)")

        # Step 6: Check earnings proximity for top picks
        # (only for top picks to minimize API calls)
        for pick in (top_longs[:5] + top_shorts[:3]):
            earnings = check_earnings_proximity(pick["symbol"])
            pick["earnings"] = earnings
            if earnings["is_near_earnings"]:
                penalty = earnings["confidence_penalty"]
                pick["confidence"] = max(20, pick["confidence"] - penalty)
                pick["reasons"].append(
                    f"Earnings in {earnings['days_until_earnings']} days — confidence reduced"
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
            "overnight": overnight,
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


def analyze_watchlist_stock(symbol: str) -> dict:
    """
    Run compressed quant analysis on a single stock — same intelligence
    as the hedge fund engine, but for any ticker (not just our universe).

    Returns a compact report card:
      - Composite score, direction, confidence
      - Key factor breakdown (momentum, value, quality, RSI, volume)
      - Market regime context
      - Macro sector impact
      - Earnings proximity
      - Signal (STRONG BUY / BUY / HOLD / SELL / STRONG SELL)
    """
    cache_key = f"watchlist_analysis_{symbol}"
    cached = _quant_cache.get(cache_key)
    if cached and time.time() - cached["time"] < 600:  # 10 min cache
        return cached["data"]

    result = {
        "symbol": symbol,
        "analyzed": False,
        "error": None,
    }

    try:
        # Get regime and macro context (cached)
        regime = detect_market_regime()
        macro = get_macro_overlay()

        # Download 1 year of price data for this stock + SPY benchmark
        _throttle()
        df = yf.download([symbol, "SPY"], period="1y", progress=False, group_by="ticker")

        if df is None or df.empty:
            result["error"] = "No price data available"
            return result

        # Extract stock data
        try:
            if isinstance(df.columns, pd.MultiIndex):
                if symbol in df.columns.get_level_values(0):
                    stock_df = df[symbol].dropna(how="all")
                else:
                    result["error"] = "Ticker not found"
                    return result
            else:
                stock_df = df
        except Exception:
            result["error"] = "Could not parse price data"
            return result

        if len(stock_df) < 60:
            result["error"] = "Not enough price history (need 60+ days)"
            return result

        closes = stock_df["Close"].values.astype(float).flatten()
        volumes = stock_df["Volume"].values.astype(float).flatten()
        price = float(closes[-1])

        # --- Calculate all factors ---
        # Momentum (20d & 60d returns)
        ret_20d = (closes[-1] / closes[-20] - 1) * 100 if len(closes) >= 20 else 0
        ret_60d = (closes[-1] / closes[-60] - 1) * 100 if len(closes) >= 60 else 0
        momentum = (ret_20d * 0.6 + ret_60d * 0.4)

        # RSI-14
        deltas = np.diff(closes[-15:])
        gains = np.mean([d for d in deltas if d > 0]) if any(d > 0 for d in deltas) else 0.001
        losses = np.mean([abs(d) for d in deltas if d < 0]) if any(d < 0 for d in deltas) else 0.001
        rs = gains / losses if losses > 0 else 100
        rsi14 = 100 - (100 / (1 + rs))

        # RSI-2 (mean reversion signal)
        deltas2 = np.diff(closes[-3:])
        gains2 = np.mean([d for d in deltas2 if d > 0]) if any(d > 0 for d in deltas2) else 0.001
        losses2 = np.mean([abs(d) for d in deltas2 if d < 0]) if any(d < 0 for d in deltas2) else 0.001
        rs2 = gains2 / losses2 if losses2 > 0 else 100
        rsi2 = 100 - (100 / (1 + rs2))

        # Volatility (60d annualized)
        window_60 = closes[-60:] if len(closes) >= 60 else closes
        daily_rets = np.diff(window_60) / window_60[:-1]
        vol_60d = float(np.std(daily_rets) * np.sqrt(252) * 100)

        # Volume trend (20d avg vs 60d avg)
        vol_20d_avg = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else 0
        vol_60d_avg = float(np.mean(volumes[-60:])) if len(volumes) >= 60 else vol_20d_avg
        vol_ratio = (vol_20d_avg / vol_60d_avg) if vol_60d_avg > 0 else 1.0

        # EMAs
        def ema(data, span):
            return float(pd.Series(data).ewm(span=span, adjust=False).mean().iloc[-1])
        ema_9 = ema(closes, 9)
        ema_21 = ema(closes, 21)
        ema_50 = ema(closes, 50) if len(closes) >= 50 else ema_21
        sma_200 = float(np.mean(closes[-200:])) if len(closes) >= 200 else float(np.mean(closes))

        # Bollinger Bands
        sma_20 = float(np.mean(closes[-20:]))
        std_20 = float(np.std(closes[-20:]))
        bb_upper = sma_20 + 2 * std_20
        bb_lower = sma_20 - 2 * std_20
        bb_width = ((bb_upper - bb_lower) / sma_20) * 100 if sma_20 > 0 else 0
        bb_position = ((price - bb_lower) / (bb_upper - bb_lower)) * 100 if (bb_upper - bb_lower) > 0 else 50

        # Smart money signal
        recent_closes = closes[-20:]
        recent_vols = volumes[-20:]
        first_half_price = float(np.mean(recent_closes[:10]))
        second_half_price = float(np.mean(recent_closes[10:]))
        first_half_vol = float(np.mean(recent_vols[:10]))
        second_half_vol = float(np.mean(recent_vols[10:]))
        price_dir = "up" if second_half_price > first_half_price else "down"
        vol_dir = "up" if second_half_vol > first_half_vol else "down"

        if price_dir == "down" and vol_dir == "down":
            smart_money = "Accumulation (bullish)"
        elif price_dir == "up" and vol_dir == "down":
            smart_money = "Distribution (bearish)"
        elif price_dir == "up" and vol_dir == "up":
            smart_money = "Confirmed Uptrend"
        else:
            smart_money = "Confirmed Downtrend"

        # Composite score (simplified — can't z-score a single stock, use raw signals)
        score = 0
        if momentum > 5: score += 2
        elif momentum > 0: score += 1
        elif momentum < -5: score -= 2
        elif momentum < 0: score -= 1

        if rsi14 < 30: score += 2  # oversold
        elif rsi14 < 40: score += 1
        elif rsi14 > 70: score -= 2  # overbought
        elif rsi14 > 60: score -= 1

        if vol_ratio > 1.3: score += 1  # rising volume
        elif vol_ratio < 0.7: score -= 1

        if price > ema_50: score += 1  # above 50 EMA
        else: score -= 1

        if price > sma_200: score += 1  # above 200 SMA
        else: score -= 1

        if bb_position < 20: score += 1  # near lower band
        elif bb_position > 80: score -= 1  # near upper band

        # Macro adjustment
        sector = SECTOR_MAP.get(symbol, "Unknown")
        macro_adj = 0
        if macro and "sector_adjustments" in macro:
            macro_adj = macro["sector_adjustments"].get(sector, 0)
            score += macro_adj

        # Direction and signal
        if score >= 4:
            signal = "STRONG BUY"
            direction = "LONG"
            confidence = min(90, 60 + score * 4)
        elif score >= 2:
            signal = "BUY"
            direction = "LONG"
            confidence = min(75, 50 + score * 4)
        elif score <= -4:
            signal = "STRONG SELL"
            direction = "SHORT"
            confidence = min(90, 60 + abs(score) * 4)
        elif score <= -2:
            signal = "SELL"
            direction = "SHORT"
            confidence = min(75, 50 + abs(score) * 4)
        else:
            signal = "HOLD"
            direction = "NEUTRAL"
            confidence = 40

        # Regime adjustment
        current_regime = regime.get("regime", "SIDEWAYS") if regime else "SIDEWAYS"
        if current_regime == "BEAR":
            if direction == "LONG":
                confidence = int(confidence * 0.7)
            elif direction == "SHORT":
                confidence = min(95, int(confidence * 1.1))
        elif current_regime == "BULL":
            if direction == "LONG":
                confidence = min(95, int(confidence * 1.1))
            elif direction == "SHORT":
                confidence = int(confidence * 0.7)

        # Build compact result
        result = {
            "symbol": symbol,
            "analyzed": True,
            "price": round(price, 2),
            "sector": sector,
            "signal": signal,
            "direction": direction,
            "confidence": confidence,
            "composite_score": round(score, 1),
            "regime": current_regime,
            "regime_confidence": regime.get("confidence", 50) if regime else 50,
            "factors": {
                "momentum": {"value": round(momentum, 1), "label": f"{'+' if momentum > 0 else ''}{round(momentum, 1)}%"},
                "rsi14": {"value": round(rsi14, 1), "label": "Oversold" if rsi14 < 30 else "Overbought" if rsi14 > 70 else "Neutral"},
                "rsi2": {"value": round(rsi2, 1), "label": "Oversold" if rsi2 < 10 else "Overbought" if rsi2 > 90 else "Neutral"},
                "volatility": {"value": round(vol_60d, 1), "label": f"{round(vol_60d, 1)}% ann."},
                "volume_trend": {"value": round(vol_ratio, 2), "label": "Rising" if vol_ratio > 1.1 else "Falling" if vol_ratio < 0.9 else "Stable"},
                "smart_money": {"value": 0, "label": smart_money},
                "bb_position": {"value": round(bb_position, 0), "label": f"{round(bb_position, 0)}% (width: {round(bb_width, 1)}%)"},
            },
            "technicals": {
                "ema_9": round(ema_9, 2),
                "ema_21": round(ema_21, 2),
                "ema_50": round(ema_50, 2),
                "sma_200": round(sma_200, 2),
                "bb_upper": round(bb_upper, 2),
                "bb_lower": round(bb_lower, 2),
                "above_200sma": price > sma_200,
                "above_50ema": price > ema_50,
                "ema_trend": "Bullish" if ema_9 > ema_21 > ema_50 else "Bearish" if ema_9 < ema_21 < ema_50 else "Mixed",
            },
            "macro_impact": macro_adj,
            "returns": {
                "1m": round(ret_20d, 1),
                "3m": round(ret_60d, 1),
            },
        }

        # Cache it
        _quant_cache[cache_key] = {"data": result, "time": time.time()}
        return result

    except Exception as e:
        logger.warning(f"Watchlist analysis failed for {symbol}: {e}")
        result["error"] = str(e)
        return result


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
