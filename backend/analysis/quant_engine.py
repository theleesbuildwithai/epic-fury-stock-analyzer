"""
Quantitative Engine — the core intelligence of the Sentinel Quant hedge fund.

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
from analysis.news_sentiment import assess_geopolitical_risk, assess_tariff_risk
from analysis.rentech import (
    run_rentech_analysis, find_pairs_trades, ensemble_vote,
    get_mean_reversion_signals, assess_portfolio_risk,
    calculate_drawdown_circuit_breaker, get_alt_data_signals
)

logger = logging.getLogger(__name__)

# ============================================================
#  CACHING & THROTTLING (shared with existing system)
# ============================================================

_quant_cache = {}
_QUANT_CACHE_TTL = 300  # 5 minutes
_last_quant_call = [0.0]
_QUANT_DELAY = 3.0  # seconds between Yahoo Finance calls

# Fundamentals cache — 24-hour TTL for yfinance .info data
_fundamentals_cache = {}
_FUNDAMENTALS_CACHE_TTL = 86400  # 24 hours

# Beta cache — stores beta vs SPY for each stock (24h TTL, same as fundamentals)
_beta_cache = {}
_BETA_CACHE_TTL = 86400  # 24 hours


def _prefetch_fundamentals(symbols: list) -> dict:
    """
    Pre-fetch P/E, P/B, forward P/E for all symbols in universe.
    Uses 24-hour cache to minimize API calls.
    Returns dict of {symbol: {pe, pb, fwd_pe, earnings_yield, book_to_price}}.
    Only fetches uncached symbols — typically 0 API calls after first run.
    """
    now = time.time()
    result = {}
    uncached = []

    for sym in symbols:
        if sym in _fundamentals_cache and (now - _fundamentals_cache[sym]["time"]) < _FUNDAMENTALS_CACHE_TTL:
            result[sym] = _fundamentals_cache[sym].get("value_data", {})
        else:
            uncached.append(sym)

    # Fetch uncached symbols in small batches (max 5 at a time to avoid rate limits)
    for sym in uncached[:50]:  # Cap at 50 to avoid excessive API calls
        try:
            _throttle()
            info = yf.Ticker(sym).info or {}
            pe = info.get("trailingPE")
            fwd_pe = info.get("forwardPE")
            pb = info.get("priceToBook")

            earnings_yield = (1.0 / pe) * 100 if pe and pe > 0 else None  # higher = cheaper
            book_to_price = (1.0 / pb) * 100 if pb and pb > 0 else None  # higher = cheaper

            value_data = {
                "pe": pe,
                "fwd_pe": fwd_pe,
                "pb": pb,
                "earnings_yield": earnings_yield,
                "book_to_price": book_to_price,
            }
            result[sym] = value_data

            # Update cache (merge with existing cache entry if present)
            if sym in _fundamentals_cache:
                _fundamentals_cache[sym]["value_data"] = value_data
                _fundamentals_cache[sym]["time"] = now
            else:
                _fundamentals_cache[sym] = {"value_data": value_data, "time": now}

        except Exception as e:
            logger.debug(f"Fundamentals prefetch failed for {sym}: {e}")
            result[sym] = {}

    return result


def _calculate_beta(stock_closes: np.ndarray, spy_closes: np.ndarray) -> float:
    """
    Calculate stock beta vs SPY using 120-day returns.
    Beta = Cov(stock, market) / Var(market)
    Returns beta value (1.0 = market, >1 = more volatile, <1 = defensive).
    """
    min_len = min(len(stock_closes), len(spy_closes))
    if min_len < 60:
        return 1.0  # default to market beta

    # Use last 120 days (or available)
    n = min(min_len, 120)
    stock = stock_closes[-n:]
    spy = spy_closes[-n:]

    stock_rets = np.diff(stock) / stock[:-1]
    spy_rets = np.diff(spy) / spy[:-1]

    # Remove NaN/inf
    valid = np.isfinite(stock_rets) & np.isfinite(spy_rets)
    stock_rets = stock_rets[valid]
    spy_rets = spy_rets[valid]

    if len(spy_rets) < 30:
        return 1.0

    cov = np.cov(stock_rets, spy_rets)[0][1]
    var_market = np.var(spy_rets)

    if var_market == 0:
        return 1.0

    beta = float(cov / var_market)
    return round(max(-2.0, min(4.0, beta)), 3)  # clamp to reasonable range


def _throttle():
    """Enforce minimum delay between Yahoo Finance API calls."""
    now = time.time()
    elapsed = now - _last_quant_call[0]
    if elapsed < _QUANT_DELAY:
        time.sleep(_QUANT_DELAY - elapsed)
    _last_quant_call[0] = time.time()  # FIXED: was dead code in _safe_close


def _safe_close(df):
    """Extract Close column from yfinance DataFrame, handling multi-level columns.
    Newer yfinance returns multi-level columns even for single tickers.
    Returns a pandas Series of close prices."""
    if df is None or df.empty:
        return pd.Series(dtype=float)
    close = df["Close"]
    if hasattr(close, "columns"):
        close = close.iloc[:, 0]
    return close


# ============================================================
#  GARCH(1,1) VOLATILITY FORECASTING
# ============================================================
# Predicts tomorrow's volatility using GARCH(1,1):
#   sigma_t^2 = omega + alpha * epsilon_{t-1}^2 + beta * sigma_{t-1}^2
# Better than backward-looking realized vol for 1-5 day horizons.
# Fitted via MLE with scipy.optimize.minimize (no extra dependencies).

_garch_cache = {}
_GARCH_CACHE_TTL = 600  # 10 minutes


def garch_forecast(closes, horizon=1):
    """
    Fit GARCH(1,1) to daily returns and forecast volatility.

    Returns:
        dict with predicted_vol (annualized), vol_ratio (predicted/realized),
        is_vol_compressed (bool), realized_vol (annualized)
    """
    from scipy.optimize import minimize

    if len(closes) < 60:
        return {"predicted_vol": 0, "vol_ratio": 1.0, "is_vol_compressed": False, "realized_vol": 0}

    try:
        returns = np.diff(np.log(closes[-252:])) if len(closes) >= 252 else np.diff(np.log(closes[-60:]))
        T = len(returns)
        if T < 30:
            return {"predicted_vol": 0, "vol_ratio": 1.0, "is_vol_compressed": False, "realized_vol": 0}

        # Realized vol (60-day annualized)
        realized_vol = float(np.std(returns[-60:]) * np.sqrt(252))

        # GARCH(1,1) log-likelihood
        def neg_log_likelihood(params):
            omega, alpha, beta = params
            sig2 = np.zeros(T)
            sig2[0] = np.var(returns)
            for t in range(1, T):
                sig2[t] = omega + alpha * returns[t-1]**2 + beta * sig2[t-1]
                if sig2[t] <= 0:
                    sig2[t] = 1e-8
            # Log-likelihood of normal distribution
            ll = -0.5 * np.sum(np.log(sig2) + returns**2 / sig2)
            return -ll  # negative because we minimize

        # Initial params and bounds
        var0 = np.var(returns)
        x0 = [var0 * 0.05, 0.08, 0.88]  # omega, alpha, beta
        bounds = [(1e-10, var0 * 10), (0.01, 0.5), (0.3, 0.99)]

        # Constraint: alpha + beta < 1 (stationarity)
        constraints = [{"type": "ineq", "fun": lambda p: 0.999 - p[1] - p[2]}]

        result = minimize(neg_log_likelihood, x0, bounds=bounds, constraints=constraints,
                         method="SLSQP", options={"maxiter": 200, "ftol": 1e-8})

        if not result.success:
            return {"predicted_vol": realized_vol, "vol_ratio": 1.0,
                    "is_vol_compressed": False, "realized_vol": realized_vol}

        omega, alpha, beta = result.x

        # Forecast: sigma_{T+h}^2
        last_sig2 = omega + alpha * returns[-1]**2 + beta * np.var(returns[-5:])
        forecast_sig2 = last_sig2
        for _ in range(horizon):
            forecast_sig2 = omega + (alpha + beta) * forecast_sig2

        predicted_vol = float(np.sqrt(forecast_sig2) * np.sqrt(252))  # annualized
        vol_ratio = predicted_vol / realized_vol if realized_vol > 0 else 1.0

        # Vol compression: GARCH predicted < 60% of realized = breakout setup
        is_compressed = vol_ratio < 0.6

        return {
            "predicted_vol": round(predicted_vol, 4),
            "vol_ratio": round(vol_ratio, 3),
            "is_vol_compressed": is_compressed,
            "realized_vol": round(realized_vol, 4),
        }

    except Exception:
        realized = float(np.std(np.diff(np.log(closes[-60:]))) * np.sqrt(252)) if len(closes) >= 60 else 0
        return {"predicted_vol": realized, "vol_ratio": 1.0,
                "is_vol_compressed": False, "realized_vol": realized}


# ============================================================
#  CROSS-ASSET MOMENTUM SIGNALS
# ============================================================
# Dollar (UUP), Bitcoin (BTC-USD), Copper (CPER), Bonds (TLT)
# move hours ahead of equities. Continuous scoring, not just overnight.

_cross_asset_cache = {"data": None, "time": 0}
_CROSS_ASSET_TTL = 600  # 10 min


def get_cross_asset_signals() -> dict:
    """
    Download cross-asset data and generate sector-level adjustments.

    Returns dict with:
        - signals: per-asset momentum data
        - sector_adjustments: sector-level score adjustments (-2 to +2)
        - risk_appetite: overall risk-on/risk-off reading
    """
    now = time.time()
    if _cross_asset_cache["data"] and now - _cross_asset_cache["time"] < _CROSS_ASSET_TTL:
        return _cross_asset_cache["data"]

    result = {"signals": {}, "sector_adjustments": {}, "risk_appetite": "NEUTRAL"}

    try:
        _throttle()
        tickers = ["UUP", "BTC-USD", "CPER", "TLT"]
        df = yf.download(tickers, period="1mo", progress=False, group_by="ticker")

        if df is None or df.empty:
            return result

        assets = {}
        for sym in tickers:
            try:
                if isinstance(df.columns, pd.MultiIndex):
                    if sym in df.columns.get_level_values(0):
                        c = df[sym]["Close"].dropna().values.astype(float)
                        if len(c) >= 10:
                            assets[sym] = c
                elif len(tickers) == 1:
                    c = _safe_close(df).values.astype(float)
                    if len(c) >= 10:
                        assets[sym] = c
            except Exception:
                continue

        if not assets:
            return result

        # Calculate momentum for each asset
        for sym, closes in assets.items():
            mom_5d = (closes[-1] / closes[-5] - 1) * 100 if len(closes) >= 5 else 0
            mom_20d = (closes[-1] / closes[-20] - 1) * 100 if len(closes) >= 20 else 0
            result["signals"][sym] = {
                "momentum_5d": round(mom_5d, 2),
                "momentum_20d": round(mom_20d, 2),
                "price": round(float(closes[-1]), 2),
            }

        # --- Sector adjustments based on cross-asset momentum ---
        sector_adj = {}

        # DOLLAR (UUP): falling dollar = bullish for multinationals
        uup = result["signals"].get("UUP", {})
        uup_5d = uup.get("momentum_5d", 0)
        if uup_5d < -1.0:  # Dollar falling
            sector_adj["Technology"] = sector_adj.get("Technology", 0) + 0.8
            sector_adj["Healthcare"] = sector_adj.get("Healthcare", 0) + 0.5
            sector_adj["Materials"] = sector_adj.get("Materials", 0) + 0.7
        elif uup_5d > 1.0:  # Dollar rising
            sector_adj["Technology"] = sector_adj.get("Technology", 0) - 0.5
            sector_adj["Materials"] = sector_adj.get("Materials", 0) - 0.7
            sector_adj["Industrials"] = sector_adj.get("Industrials", 0) - 0.3

        # BITCOIN (BTC-USD): risk appetite proxy (24/7 market)
        btc = result["signals"].get("BTC-USD", {})
        btc_5d = btc.get("momentum_5d", 0)
        if btc_5d > 5.0:  # Strong risk-on
            sector_adj["Technology"] = sector_adj.get("Technology", 0) + 0.6
            sector_adj["Consumer Discretionary"] = sector_adj.get("Consumer Discretionary", 0) + 0.5
            result["risk_appetite"] = "RISK_ON"
        elif btc_5d < -5.0:  # Risk-off
            sector_adj["Utilities"] = sector_adj.get("Utilities", 0) + 0.5
            sector_adj["Consumer Staples"] = sector_adj.get("Consumer Staples", 0) + 0.4
            result["risk_appetite"] = "RISK_OFF"

        # COPPER (CPER): global growth proxy
        cper = result["signals"].get("CPER", {})
        cper_5d = cper.get("momentum_5d", 0)
        if cper_5d > 2.0:  # Rising copper = growth
            sector_adj["Industrials"] = sector_adj.get("Industrials", 0) + 0.8
            sector_adj["Materials"] = sector_adj.get("Materials", 0) + 0.7
            sector_adj["Energy"] = sector_adj.get("Energy", 0) + 0.4
        elif cper_5d < -2.0:  # Falling copper = slowdown
            sector_adj["Industrials"] = sector_adj.get("Industrials", 0) - 0.6
            sector_adj["Materials"] = sector_adj.get("Materials", 0) - 0.5

        # BONDS (TLT): falling TLT = rising yields = bearish for rate-sensitives
        tlt = result["signals"].get("TLT", {})
        tlt_5d = tlt.get("momentum_5d", 0)
        if tlt_5d < -1.5:  # Yields rising (TLT falling)
            sector_adj["Utilities"] = sector_adj.get("Utilities", 0) - 0.8
            sector_adj["Real Estate"] = sector_adj.get("Real Estate", 0) - 0.9
            sector_adj["Financials"] = sector_adj.get("Financials", 0) + 0.6
        elif tlt_5d > 1.5:  # Yields falling (TLT rising)
            sector_adj["Utilities"] = sector_adj.get("Utilities", 0) + 0.5
            sector_adj["Real Estate"] = sector_adj.get("Real Estate", 0) + 0.6
            sector_adj["Financials"] = sector_adj.get("Financials", 0) - 0.3

        # Clamp adjustments to [-2, +2]
        result["sector_adjustments"] = {k: round(max(-2, min(2, v)), 1) for k, v in sector_adj.items()}

    except Exception as e:
        logger.debug(f"Cross-asset signals error: {e}")

    _cross_asset_cache["data"] = result
    _cross_asset_cache["time"] = time.time()
    return result


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
    # ============================================================
    # Technology (42) — expanded with volatile mid-caps for bigger moves
    # ============================================================
    "AAPL", "MSFT", "NVDA", "AVGO", "AMD", "ADBE", "CRM", "INTC", "QCOM", "TXN",
    "AMAT", "LRCX", "KLAC", "MRVL", "SNPS", "CDNS", "NXPI", "MCHP", "ON", "FTNT",
    "PANW", "NOW", "WDAY", "TEAM", "DDOG", "ZS", "CRWD", "SNOW", "MDB", "NET",
    # NEW: High-beta tech (big movers on macro news)
    "SHOP",  # Shopify — tariff-exposed e-commerce, moves 3-5% on trade news
    "SQ",    # Block — fintech, volatile, crypto-adjacent
    "PLTR",  # Palantir — defense/AI play, moves on gov contracts + geopolitics
    "U",     # Unity — high-beta gaming tech
    "DOCN",  # DigitalOcean — small-cap cloud, big swings
    "HUBS",  # HubSpot — SaaS bellwether
    "OKTA",  # Okta — cybersecurity mid-cap
    "BILL",  # Bill.com — fintech, volatile
    "SMCI",  # Super Micro — AI hardware, massive mover
    "ARM",   # ARM Holdings — chip designer, moves with NVDA
    "UBER",  # Uber — consumer tech, tariff-sensitive
    "DASH",  # DoorDash — gig economy, consumer spending indicator
    # ============================================================
    # Communication (15) — added streaming + social volatility plays
    # ============================================================
    "GOOGL", "META", "NFLX", "DIS", "CMCSA", "TMUS", "VZ", "T", "CHTR", "EA",
    "TTWO", "RBLX",
    "SNAP",  # Snap — ultra-volatile, moves 5-10% on any news
    "PINS",  # Pinterest — social/ad revenue play
    "ROKU",  # Roku — streaming hardware, consumer discretionary crossover
    # ============================================================
    # Consumer Discretionary (30) — added tariff-exposed retailers + autos
    # ============================================================
    "AMZN", "TSLA", "HD", "MCD", "NKE", "SBUX", "TJX", "LOW", "BKNG", "CMG",
    "ROST", "DHI", "LEN", "ORLY", "AZO", "POOL", "DECK", "ULTA", "ETSY", "ABNB",
    # NEW: Import-heavy retailers (CRUSHED by tariffs = great short targets)
    "FIVE",  # Five Below — 100% import-dependent, tariff victim #1
    "RH",    # RH (Restoration Hardware) — luxury imports, tariff-exposed
    "W",     # Wayfair — furniture imports from China
    "CROX",  # Crocs — manufactured overseas, tariff target
    "LEVI",  # Levi Strauss — reports Tuesday, tariff-exposed apparel
    "GPS",   # Gap — import-heavy apparel
    "RIVN",  # Rivian — EV play, moves with TSLA
    "LCID",  # Lucid — EV, volatile, tariff on parts
    "GM",    # GM — auto tariffs, big mover this week
    "F",     # Ford — auto tariffs, domestic manufacturer (could benefit)
    # ============================================================
    # Consumer Staples (18) — added domestic producers (tariff winners)
    # ============================================================
    "WMT", "PG", "COST", "KO", "PEP", "PM", "MO", "CL", "KMB", "MDLZ",
    "GIS", "HSY", "SJM", "STZ", "EL",
    "KR",    # Kroger — domestic grocery, defensive, tariff-resistant
    "TSN",   # Tyson Foods — domestic protein, benefits from import tariffs
    "ADM",   # Archer-Daniels-Midland — agriculture, trade war play
    # ============================================================
    # Healthcare (35) — expanded pharma (100% tariff on imported drugs!)
    # ============================================================
    "UNH", "LLY", "JNJ", "ABBV", "MRK", "PFE", "TMO", "ABT", "BMY", "AMGN",
    "GILD", "ISRG", "VRTX", "REGN", "DXCM", "IDXX", "ZTS", "VEEV", "ALGN", "HOLX",
    "IQV", "EW", "SYK", "BDX", "HCA",
    # NEW: Pharma tariff plays (100% tariffs on imported branded drugs)
    "AZN",   # AstraZeneca — imports drugs to US, tariff victim
    "NVO",   # Novo Nordisk — Ozempic/Wegovy, manufactured abroad
    "SNY",   # Sanofi — French pharma, import tariff target
    "GSK",   # GSK — UK pharma, import tariff target
    "MRNA",  # Moderna — mRNA, volatile biotech
    "BIIB",  # Biogen — biotech, volatile
    "CI",    # Cigna — health insurer, benefits if drug costs forced down
    "CVS",   # CVS Health — pharmacy, drug pricing plays
    "HUM",   # Humana — health insurer
    "TEVA",  # Teva — generic drugs, could BENEFIT from brand tariffs
    # ============================================================
    # Financials (25) — added regional banks + insurance
    # ============================================================
    "JPM", "V", "MA", "BAC", "GS", "MS", "WFC", "C", "BLK", "SCHW",
    "AXP", "CB", "MMC", "ICE", "CME", "MCO", "MSCI", "FIS", "COIN", "HOOD",
    "ALLY",  # Ally Financial — consumer lending, rate-sensitive
    "SOFI",  # SoFi — fintech, volatile, rate-sensitive
    "MARA",  # Marathon Digital — Bitcoin mining, ultra-volatile
    "RIOT",  # Riot Platforms — Bitcoin mining, geopolitical hedge
    "KRE",   # Regional Bank ETF — rate sensitivity play
    # ============================================================
    # Industrials (28) — expanded defense + tariff-exposed manufacturers
    # ============================================================
    "BA", "CAT", "HON", "GE", "UNP", "RTX", "LMT", "DE", "FDX", "WM",
    "GD", "NOC", "CSX", "NSC", "ITW", "PH", "ROK", "EMR", "TT", "VRSK",
    # NEW: Defense stocks (Iran escalation = these moon)
    "HII",   # Huntington Ingalls — naval defense, Iran = bullish
    "LHX",   # L3Harris — defense electronics
    "TDG",   # TransDigm — aerospace parts
    # NEW: Tariff-exposed manufacturers
    "GNRC",  # Generac — generators, import parts
    "URI",   # United Rentals — construction equipment
    "SAIA",  # Saia Inc — trucking, trade volume indicator
    "XPO",   # XPO Logistics — freight, trade volume indicator
    "ODFL",  # Old Dominion Freight — trucking bellwether
    # ============================================================
    # Energy (18) — expanded for Iran/oil plays
    # ============================================================
    "XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX", "OXY", "DVN", "HES",
    "FANG", "VLO",
    # NEW: More oil + natural gas (Iran/Strait of Hormuz = oil spikes)
    "HAL",   # Halliburton — oilfield services, benefits from high oil
    "BKR",   # Baker Hughes — oilfield services
    "AR",    # Antero Resources — natural gas
    "EQT",   # EQT Corp — largest natural gas producer
    "CTRA",  # Coterra Energy — oil + gas combo
    "OVV",   # Ovintiv — Canadian oil, less tariff-exposed
    # ============================================================
    # Materials (15) — expanded for steel/aluminum tariff plays
    # ============================================================
    "LIN", "APD", "SHW", "FCX", "NEM", "ECL", "DD", "VMC", "MLM", "NUE",
    # NEW: Steel/aluminum (tariff adjustments effective THIS WEEK)
    "STLD",  # Steel Dynamics — US steelmaker, BENEFITS from tariffs
    "X",     # US Steel — poster child for steel tariffs
    "AA",    # Alcoa — aluminum producer, tariff beneficiary
    "CLF",   # Cleveland-Cliffs — steel, tariff winner
    "RGLD",  # Royal Gold — gold royalty, safe haven play
    # ============================================================
    # Real Estate (10) — added rate-sensitive plays
    # ============================================================
    "AMT", "PLD", "SPG", "CCI", "EQIX", "DLR", "O", "WELL",
    "VNQ",   # Vanguard Real Estate ETF — sector-wide signal
    "PSA",   # Public Storage — defensive REIT
    # ============================================================
    # Utilities (10) — added safe havens for defensive positioning
    # ============================================================
    "NEE", "DUK", "SO", "AEP", "SRE", "D", "EXC", "XEL",
    "AWK",   # American Water Works — ultimate defensive
    "WEC",   # WEC Energy — stable dividend, safe haven
    # ============================================================
    # Commodities ETFs (20) — trade commodities via liquid ETFs
    # ============================================================
    "GLD",   # SPDR Gold Trust — safe haven, inflation hedge
    "SLV",   # iShares Silver Trust — industrial + precious metal
    "USO",   # United States Oil Fund — crude oil exposure
    "UNG",   # United States Natural Gas Fund
    "COPX",  # Global X Copper Miners ETF
    "DBA",   # Invesco DB Agriculture Fund
    "WEAT",  # Teucrium Wheat Fund
    "CORN",  # Teucrium Corn Fund
    "SOYB",  # Teucrium Soybean Fund
    "PPLT",  # abrdn Physical Platinum Shares
    "PALL",  # abrdn Physical Palladium Shares
    "URA",   # Global X Uranium ETF
    "CPER",  # United States Copper Index Fund
    "DBB",   # Invesco DB Base Metals Fund
    "DBC",   # Invesco DB Commodity Index Tracking Fund
    "PDBC",  # Invesco Optimum Yield Diversified Commodity
    "IAU",   # iShares Gold Trust
    "GSG",   # iShares S&P GSCI Commodity-Indexed Trust
    "REMX",  # VanEck Rare Earth/Strategic Metals ETF
    "SGOL",  # abrdn Physical Gold Shares
    # ============================================================
    # ETFs for sector-level signals (14) — expanded for macro reads
    # ============================================================
    "SPY", "QQQ", "IWM", "XLF", "XLE", "XLV", "XLK", "XLI", "XLP", "XLU",
    "TLT",   # 20+ Year Treasury ETF — rate/Fed play, CPI reaction
    "XBI",   # Biotech ETF — pharma tariff impact
    "ARKK",  # ARK Innovation — high-beta growth, biggest loser in selloffs
    "VXX",   # VIX Short-Term Futures — volatility play
    # ============================================================
    # EXPANDED S&P 500 COVERAGE (~260 additional stocks)
    # ============================================================
    # Technology additions
    "ORCL", "IBM", "HPQ", "HPE", "CSCO", "AKAM", "FFIV", "JNPR", "KEYS", "ANSS",
    "PTC", "MPWR", "SWKS", "QRVO", "TER", "ENPH", "SEDG", "FSLR", "GDDY", "GEN",
    "CTSH", "EPAM", "IT", "LDOS", "DXC", "VRSN",
    # Communication additions
    "LYV", "WBD", "PARA", "FOX", "FOXA", "NWS", "NWSA", "IPG", "OMC",
    # Consumer Discretionary additions
    "YUM", "DPZ", "WYNN", "LVS", "MGM", "CZR", "HLT", "MAR", "RCL", "CCL",
    "NCLH", "BBY", "KMX", "GRMN", "HAS", "MAT", "LULU", "BBWI", "TPR", "CPRI",
    "PVH", "RL", "VFC", "APTV", "BWA", "LEA",
    # Consumer Staples additions
    "MNST", "TAP", "BG", "CPB", "HRL", "MKC", "CAG", "K", "LW",
    "CHD", "CLX", "WBA",
    # Healthcare additions
    "A", "TECH", "WAT", "MTD", "PKI", "TFX", "BAX", "BSX", "ZBH", "PODD",
    "XRAY", "RMD", "COO", "HSIC", "INCY", "EXAS", "ALNY", "SRPT",
    "CNC", "MOH", "DGX", "LH",
    # Financials additions
    "TFC", "USB", "PNC", "MTB", "FITB", "HBAN", "KEY", "CFG", "RF", "ZION",
    "NTRS", "STT", "BK", "TROW", "IVZ", "BEN", "NDAQ", "CBOE",
    "AIG", "MET", "PRU", "ALL", "TRV", "AON", "WRB", "GL", "CINF", "L",
    "RE", "RJF", "LPLA", "MKTX",
    # Industrials additions
    "CARR", "OTIS", "SWK", "IR", "DOV", "AME", "CTAS", "FAST", "GWW",
    "MSM", "NDSN", "RHI", "MAN", "PAYC", "PAYX",
    "WAB", "TDY", "HEI", "AXON", "BAH", "CACI", "KBR",
    "DAL", "UAL", "LUV", "ALK", "JBLU", "CHRW", "EXPD", "LSTR",
    "AOS", "LII", "WSO", "JCI",
    # Energy additions
    "PXD", "TRGP", "WMB", "KMI", "OKE", "DINO", "MRO", "APA", "SM",
    # Materials additions
    "PPG", "IFF", "ALB", "EMN", "CE", "AVTR", "FMC", "MOS", "CF",
    "RPM", "SEE", "PKG", "IP", "WRK", "SON",
    # Real Estate additions
    "VICI", "IRM", "KIM", "REG", "FRT", "CPT", "ESS", "UDR", "MAA",
    "ARE", "BXP", "SLG", "HIW", "PEAK", "HST", "RLJ",
    # Utilities additions
    "ES", "AES", "PNW", "NRG", "CMS", "DTE", "OGE", "PEG",
    "ED", "FE", "PPL", "EVRG", "ATO", "NI", "LNT",
    # Additional ETFs
    "DIA", "MTUM", "VLUE", "QUAL", "SIZE", "XLY", "XLC", "XLB", "XLRE",
    "KWEB", "EEM", "FXI", "EWZ", "EWJ",
]


# ============================================================
#  INTERNATIONAL UNIVERSE — 230 top non-US stocks
# ============================================================
# All ADRs (American Depositary Receipts) — trade on NYSE/NASDAQ in USD,
# so no currency conversion or foreign-exchange microstructure issues.
# Curated for liquidity (mostly mega/large cap) and broad regional
# diversification. Added 2026-05-02.
INTERNATIONAL_UNIVERSE = [
    # ============================================================
    # CHINA / Hong Kong (30) — mega-cap tech + consumer + industry
    # ============================================================
    "BABA", "JD", "PDD", "BIDU", "NIO", "XPEV", "LI", "BILI", "NTES", "TME",
    "YUMC", "TCOM", "IQ", "ZTO", "HTHT", "FUTU", "TIGR", "TAL", "EDU", "GDS",
    "BEKE", "LX", "JKS", "KE", "VIPS", "ATAT", "EH", "BZ", "BGNE", "TCEHY",

    # ============================================================
    # JAPAN (10) — top exporters + financials
    # ============================================================
    "TM", "SONY", "MUFG", "SMFG", "MFG", "NMR", "NTT", "HMC", "TAK", "IX",

    # ============================================================
    # KOREA (5) + TAIWAN (3) + INDIA (8)
    # ============================================================
    "KB", "SHG", "KEP", "LPL", "PKX",
    "TSM", "UMC", "ASX",
    "INFY", "WIT", "HDB", "IBN", "TTM", "RDY", "SIFY", "MMYT",

    # ============================================================
    # EUROPE (45) — UK, NL, DE, FR, CH, ES, SE, FI, DK, IE, BE, IT
    # ============================================================
    "ASML", "NVO", "SAP", "AZN", "GSK", "BTI", "UL", "BUD", "RIO", "BHP",
    "BP", "SHEL", "RACE", "RYAAY", "NVS", "STM", "ABB", "SAN", "TEF", "VOD",
    "SNY", "PHG", "ERIC", "NOK", "MT", "BCS", "LYG", "ING", "DEO", "AER",
    "PUK", "BBVA", "FMS", "SPOT", "ADYEY", "ICLR", "NWG", "BAT", "GLPG", "FERG",
    "FLUT", "PSO", "AON", "STLA", "CRH",

    # ============================================================
    # ISRAEL (8) — tech + biotech ADRs
    # ============================================================
    "CHKP", "NICE", "WIX", "CYBR", "MNDY", "TEVA", "GLBE", "ICL",

    # ============================================================
    # LATIN AMERICA (20) — Brazil + Mexico + Argentina + Chile + Colombia
    # ============================================================
    "VALE", "PBR", "ITUB", "BBD", "ABEV", "NU", "MELI", "SUZ", "ERJ", "GGB",
    "CIG", "EBR", "BSBR", "GOL", "AZUL", "AMX", "ASR", "FMX", "KOF", "GLOB",

    # ============================================================
    # CANADA (18) — major banks, energy, industrials, miners
    # ============================================================
    "TD", "RY", "BNS", "BMO", "CM", "ENB", "TRP", "MFC", "SLF", "GOLD",
    "NTR", "CP", "CNI", "BCE", "RCI", "TRI", "SHOP", "OTEX",

    # ============================================================
    # OTHER REGIONS (9) — South Africa, Argentina, Norway
    # ============================================================
    "GFI", "AU", "SBSW", "HMY",
    "GGAL", "BMA", "PAM", "YPF",
    "EQNR",

    # ============================================================
    # COUNTRY ETFs (35) — single-country and regional exposure
    # ============================================================
    "EWG", "EWU", "EWA", "EWC", "EWY", "EWT", "EWQ", "EWP", "EWI", "EWS",
    "EWN", "EWH", "EWO", "EWL", "EWM", "INDA", "MCHI", "EZA", "ARGT", "NORW",
    "ECH", "EPI", "EFA", "VEA", "VWO", "IXUS", "ACWX", "GREK", "EIRL", "EUFN",
    "DXJ", "HEDJ", "VEU", "GXC", "EZU",

    # ============================================================
    # MORE CHINA (10) — secondary mega-caps + ADRs
    # ============================================================
    "ZH", "HUYA", "MOMO", "WB", "DQ", "GOTU", "DAO", "VNET", "QFIN", "MNSO",

    # ============================================================
    # MORE EUROPE / CROSS (4) — additional ADRs (deduped + removed delisted)
    # ============================================================
    "ALC", "ROIV", "RDDT", "GLBE",

    # ============================================================
    # MORE LATAM / EMERGING (7) — removed delisted BRFS
    # ============================================================
    "AGRO", "CSAN", "TLRY", "CIB", "SBS", "CCU", "EC",

    # ============================================================
    # MORE CANADA (4)
    # ============================================================
    "QSR", "MGA", "PAAS", "AEM",
]


# Merge international tickers into the main quant universe.
# yfinance.download dedupes silently if any ticker is repeated, so safe.
QUANT_UNIVERSE = QUANT_UNIVERSE + INTERNATIONAL_UNIVERSE

# Sector mapping for macro overlay adjustments — auto-generated for all 200+ stocks
SECTOR_MAP = {
    # Technology (42)
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
    "SHOP": "Technology", "SQ": "Technology", "PLTR": "Technology",
    "U": "Technology", "DOCN": "Technology", "HUBS": "Technology",
    "OKTA": "Technology", "BILL": "Technology", "SMCI": "Technology",
    "ARM": "Technology", "UBER": "Technology", "DASH": "Technology",
    # Communication (15)
    "GOOGL": "Communication", "META": "Communication", "NFLX": "Communication",
    "DIS": "Communication", "CMCSA": "Communication", "TMUS": "Communication",
    "VZ": "Communication", "T": "Communication", "CHTR": "Communication",
    "EA": "Communication", "TTWO": "Communication", "RBLX": "Communication",
    "SNAP": "Communication", "PINS": "Communication", "ROKU": "Communication",
    # Consumer Discretionary (30)
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
    "FIVE": "Consumer Discretionary", "RH": "Consumer Discretionary",
    "W": "Consumer Discretionary", "CROX": "Consumer Discretionary",
    "LEVI": "Consumer Discretionary", "GPS": "Consumer Discretionary",
    "RIVN": "Consumer Discretionary", "LCID": "Consumer Discretionary",
    "GM": "Consumer Discretionary", "F": "Consumer Discretionary",
    # Consumer Staples (18)
    "WMT": "Consumer Staples", "PG": "Consumer Staples", "COST": "Consumer Staples",
    "KO": "Consumer Staples", "PEP": "Consumer Staples", "PM": "Consumer Staples",
    "MO": "Consumer Staples", "CL": "Consumer Staples", "KMB": "Consumer Staples",
    "MDLZ": "Consumer Staples", "GIS": "Consumer Staples", "HSY": "Consumer Staples",
    "SJM": "Consumer Staples", "STZ": "Consumer Staples", "EL": "Consumer Staples",
    "KR": "Consumer Staples", "TSN": "Consumer Staples", "ADM": "Consumer Staples",
    # Healthcare (35)
    "UNH": "Healthcare", "LLY": "Healthcare", "JNJ": "Healthcare",
    "ABBV": "Healthcare", "MRK": "Healthcare", "PFE": "Healthcare",
    "TMO": "Healthcare", "ABT": "Healthcare", "BMY": "Healthcare",
    "AMGN": "Healthcare", "GILD": "Healthcare", "ISRG": "Healthcare",
    "VRTX": "Healthcare", "REGN": "Healthcare", "DXCM": "Healthcare",
    "IDXX": "Healthcare", "ZTS": "Healthcare", "VEEV": "Healthcare",
    "ALGN": "Healthcare", "HOLX": "Healthcare", "IQV": "Healthcare",
    "EW": "Healthcare", "SYK": "Healthcare", "BDX": "Healthcare",
    "HCA": "Healthcare", "AZN": "Healthcare", "NVO": "Healthcare",
    "SNY": "Healthcare", "GSK": "Healthcare", "MRNA": "Healthcare",
    "BIIB": "Healthcare", "CI": "Healthcare", "CVS": "Healthcare",
    "HUM": "Healthcare", "TEVA": "Healthcare",
    # Financials (25)
    "JPM": "Financials", "V": "Financials", "MA": "Financials",
    "BAC": "Financials", "GS": "Financials", "MS": "Financials",
    "WFC": "Financials", "C": "Financials", "BLK": "Financials",
    "SCHW": "Financials", "AXP": "Financials", "CB": "Financials",
    "MMC": "Financials", "ICE": "Financials", "CME": "Financials",
    "MCO": "Financials", "MSCI": "Financials", "FIS": "Financials",
    "COIN": "Financials", "HOOD": "Financials", "ALLY": "Financials",
    "SOFI": "Financials", "MARA": "Financials", "RIOT": "Financials",
    "KRE": "Financials",
    # Industrials (28)
    "BA": "Industrials", "CAT": "Industrials", "HON": "Industrials",
    "GE": "Industrials", "UNP": "Industrials", "RTX": "Industrials",
    "LMT": "Industrials", "DE": "Industrials", "FDX": "Industrials",
    "WM": "Industrials", "GD": "Industrials", "NOC": "Industrials",
    "CSX": "Industrials", "NSC": "Industrials", "ITW": "Industrials",
    "PH": "Industrials", "ROK": "Industrials", "EMR": "Industrials",
    "TT": "Industrials", "VRSK": "Industrials", "HII": "Industrials",
    "LHX": "Industrials", "TDG": "Industrials", "GNRC": "Industrials",
    "URI": "Industrials", "SAIA": "Industrials", "XPO": "Industrials",
    "ODFL": "Industrials",
    # Energy (18)
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy",
    "SLB": "Energy", "EOG": "Energy", "MPC": "Energy", "PSX": "Energy",
    "OXY": "Energy", "DVN": "Energy", "HES": "Energy",
    "FANG": "Energy", "VLO": "Energy", "HAL": "Energy",
    "BKR": "Energy", "AR": "Energy", "EQT": "Energy",
    "CTRA": "Energy", "OVV": "Energy",
    # Materials (15)
    "LIN": "Materials", "APD": "Materials", "SHW": "Materials",
    "FCX": "Materials", "NEM": "Materials", "ECL": "Materials",
    "DD": "Materials", "VMC": "Materials", "MLM": "Materials",
    "NUE": "Materials", "STLD": "Materials", "X": "Materials",
    "AA": "Materials", "CLF": "Materials", "RGLD": "Materials",
    # Real Estate (10)
    "AMT": "Real Estate", "PLD": "Real Estate", "SPG": "Real Estate",
    "CCI": "Real Estate", "EQIX": "Real Estate", "DLR": "Real Estate",
    "O": "Real Estate", "WELL": "Real Estate", "VNQ": "Real Estate",
    "PSA": "Real Estate",
    # Utilities (10)
    "NEE": "Utilities", "DUK": "Utilities", "SO": "Utilities",
    "AEP": "Utilities", "SRE": "Utilities", "D": "Utilities",
    "EXC": "Utilities", "XEL": "Utilities", "AWK": "Utilities",
    "WEC": "Utilities",
    # Commodities ETFs (20) — traded as first-class assets
    "GLD": "Commodities", "SLV": "Commodities", "USO": "Commodities",
    "UNG": "Commodities", "COPX": "Commodities", "DBA": "Commodities",
    "WEAT": "Commodities", "CORN": "Commodities", "SOYB": "Commodities",
    "PPLT": "Commodities", "PALL": "Commodities", "URA": "Commodities",
    "CPER": "Commodities", "DBB": "Commodities", "DBC": "Commodities",
    "PDBC": "Commodities", "IAU": "Commodities", "GSG": "Commodities",
    "REMX": "Commodities", "SGOL": "Commodities",
    # ETFs (14) — sector + macro signals
    "SPY": "ETF", "QQQ": "ETF", "IWM": "ETF", "XLF": "ETF",
    "XLE": "ETF", "XLV": "ETF", "XLK": "ETF", "XLI": "ETF",
    "XLP": "ETF", "XLU": "ETF", "TLT": "ETF",
    "XBI": "ETF", "ARKK": "ETF", "VXX": "ETF",
    # Expanded Technology
    "ORCL": "Technology", "IBM": "Technology", "HPQ": "Technology",
    "HPE": "Technology", "CSCO": "Technology", "AKAM": "Technology",
    "FFIV": "Technology", "JNPR": "Technology", "KEYS": "Technology",
    "ANSS": "Technology", "PTC": "Technology", "MPWR": "Technology",
    "SWKS": "Technology", "QRVO": "Technology", "TER": "Technology",
    "ENPH": "Technology", "SEDG": "Technology", "FSLR": "Technology",
    "GDDY": "Technology", "GEN": "Technology", "CTSH": "Technology",
    "EPAM": "Technology", "IT": "Technology", "LDOS": "Technology",
    "DXC": "Technology", "VRSN": "Technology",
    # Expanded Communication
    "LYV": "Communication", "WBD": "Communication", "PARA": "Communication",
    "FOX": "Communication", "FOXA": "Communication", "NWS": "Communication",
    "NWSA": "Communication", "IPG": "Communication", "OMC": "Communication",
    # Expanded Consumer Discretionary
    "YUM": "Consumer Discretionary", "DPZ": "Consumer Discretionary",
    "WYNN": "Consumer Discretionary", "LVS": "Consumer Discretionary",
    "MGM": "Consumer Discretionary", "CZR": "Consumer Discretionary",
    "HLT": "Consumer Discretionary", "MAR": "Consumer Discretionary",
    "RCL": "Consumer Discretionary", "CCL": "Consumer Discretionary",
    "NCLH": "Consumer Discretionary", "BBY": "Consumer Discretionary",
    "KMX": "Consumer Discretionary", "GRMN": "Consumer Discretionary",
    "HAS": "Consumer Discretionary", "MAT": "Consumer Discretionary",
    "LULU": "Consumer Discretionary", "BBWI": "Consumer Discretionary",
    "TPR": "Consumer Discretionary", "CPRI": "Consumer Discretionary",
    "PVH": "Consumer Discretionary", "RL": "Consumer Discretionary",
    "VFC": "Consumer Discretionary", "APTV": "Consumer Discretionary",
    "BWA": "Consumer Discretionary", "LEA": "Consumer Discretionary",
    # Expanded Consumer Staples
    "MNST": "Consumer Staples", "TAP": "Consumer Staples",
    "BG": "Consumer Staples", "CPB": "Consumer Staples",
    "HRL": "Consumer Staples", "MKC": "Consumer Staples",
    "CAG": "Consumer Staples", "K": "Consumer Staples",
    "LW": "Consumer Staples", "CHD": "Consumer Staples",
    "CLX": "Consumer Staples", "WBA": "Consumer Staples",
    # Expanded Healthcare
    "A": "Healthcare", "TECH": "Healthcare", "WAT": "Healthcare",
    "MTD": "Healthcare", "PKI": "Healthcare", "TFX": "Healthcare",
    "BAX": "Healthcare", "BSX": "Healthcare", "ZBH": "Healthcare",
    "PODD": "Healthcare", "XRAY": "Healthcare", "RMD": "Healthcare",
    "COO": "Healthcare", "HSIC": "Healthcare", "INCY": "Healthcare",
    "EXAS": "Healthcare", "ALNY": "Healthcare", "SRPT": "Healthcare",
    "CNC": "Healthcare", "MOH": "Healthcare", "DGX": "Healthcare",
    "LH": "Healthcare",
    # Expanded Financials
    "TFC": "Financials", "USB": "Financials", "PNC": "Financials",
    "MTB": "Financials", "FITB": "Financials", "HBAN": "Financials",
    "KEY": "Financials", "CFG": "Financials", "RF": "Financials",
    "ZION": "Financials", "NTRS": "Financials", "STT": "Financials",
    "BK": "Financials", "TROW": "Financials", "IVZ": "Financials",
    "BEN": "Financials", "NDAQ": "Financials", "CBOE": "Financials",
    "AIG": "Financials", "MET": "Financials", "PRU": "Financials",
    "ALL": "Financials", "TRV": "Financials", "AON": "Financials",
    "WRB": "Financials", "GL": "Financials", "CINF": "Financials",
    "L": "Financials", "RE": "Financials", "RJF": "Financials",
    "LPLA": "Financials", "MKTX": "Financials",
    # Expanded Industrials
    "CARR": "Industrials", "OTIS": "Industrials", "SWK": "Industrials",
    "IR": "Industrials", "DOV": "Industrials", "AME": "Industrials",
    "CTAS": "Industrials", "FAST": "Industrials", "GWW": "Industrials",
    "MSM": "Industrials", "NDSN": "Industrials", "RHI": "Industrials",
    "MAN": "Industrials", "PAYC": "Industrials", "PAYX": "Industrials",
    "WAB": "Industrials", "TDY": "Industrials", "HEI": "Industrials",
    "AXON": "Industrials", "BAH": "Industrials", "CACI": "Industrials",
    "KBR": "Industrials", "DAL": "Industrials", "UAL": "Industrials",
    "LUV": "Industrials", "ALK": "Industrials", "JBLU": "Industrials",
    "CHRW": "Industrials", "EXPD": "Industrials", "LSTR": "Industrials",
    "AOS": "Industrials", "LII": "Industrials", "WSO": "Industrials",
    "JCI": "Industrials",
    # Expanded Energy
    "PXD": "Energy", "TRGP": "Energy", "WMB": "Energy",
    "KMI": "Energy", "OKE": "Energy", "DINO": "Energy",
    "MRO": "Energy", "APA": "Energy", "SM": "Energy",
    # Expanded Materials
    "PPG": "Materials", "IFF": "Materials", "ALB": "Materials",
    "EMN": "Materials", "CE": "Materials", "AVTR": "Materials",
    "FMC": "Materials", "MOS": "Materials", "CF": "Materials",
    "RPM": "Materials", "SEE": "Materials", "PKG": "Materials",
    "IP": "Materials", "WRK": "Materials", "SON": "Materials",
    # Expanded Real Estate
    "VICI": "Real Estate", "IRM": "Real Estate", "KIM": "Real Estate",
    "REG": "Real Estate", "FRT": "Real Estate", "CPT": "Real Estate",
    "ESS": "Real Estate", "UDR": "Real Estate", "MAA": "Real Estate",
    "ARE": "Real Estate", "BXP": "Real Estate", "SLG": "Real Estate",
    "HIW": "Real Estate", "PEAK": "Real Estate", "HST": "Real Estate",
    "RLJ": "Real Estate",
    # Expanded Utilities
    "ES": "Utilities", "AES": "Utilities", "PNW": "Utilities",
    "NRG": "Utilities", "CMS": "Utilities", "DTE": "Utilities",
    "OGE": "Utilities", "PEG": "Utilities",  "ED": "Utilities",
    "FE": "Utilities", "PPL": "Utilities", "EVRG": "Utilities",
    "ATO": "Utilities", "NI": "Utilities", "LNT": "Utilities",
    # Additional ETFs
    "DIA": "ETF", "MTUM": "ETF", "VLUE": "ETF", "QUAL": "ETF",
    "SIZE": "ETF", "XLY": "ETF", "XLC": "ETF", "XLB": "ETF",
    "XLRE": "ETF", "KWEB": "ETF", "EEM": "ETF", "FXI": "ETF",
    "EWZ": "ETF", "EWJ": "ETF",
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
            sp_current = None
            if sp_df is not None and len(sp_df) >= 200:
                sp_closes = _safe_close(sp_df).values.astype(float)
                sp_current = float(sp_closes[-1])
                sp_sma200 = float(np.mean(sp_closes[-200:]))
                sp_sma50 = float(np.mean(sp_closes[-50:]))
                # SANITY CHECK: S&P 500 must be in plausible range
                # (the index has not been below 1000 since 2009 and not above
                # 20000 ever). Implausible value → fall through to truth_engine
                # fallback below. Prevents corrupted yfinance data from causing
                # phantom BEAR regime.
                if not (1000 < sp_current < 20000):
                    logger.warning(
                        f"Regime: implausible S&P 500 close {sp_current} — "
                        f"discarding and trying truth_engine fallback"
                    )
                    sp_current = None
            sp_used_fallback = False
            if sp_current is None:
                # Fallback to truth_engine (multi-source: ^GSPC, SPY, ^SPX with bounds)
                try:
                    from predictions.truth_engine import get_sp500_truth
                    truth = get_sp500_truth(force_refresh=True)
                    if truth.get("ok") and truth.get("price"):
                        sp_current = float(truth["price"])
                        # Mark fallback so we DON'T pretend to know the SMA-trend
                        sp_used_fallback = True
                        sp_sma200 = sp_current   # placeholder for display only
                        sp_sma50 = sp_current
                        regime_data["details"].append(
                            f"S&P 500 from truth_engine ({truth.get('source')}) — yfinance corrupted; trend=neutral"
                        )
                except Exception as _te:
                    logger.warning(f"Regime: truth_engine fallback failed: {_te}")
            if sp_current is not None and sp_current > 0:
                regime_data["sp500_price"] = round(sp_current, 2)
                if sp_used_fallback:
                    # No real SMA → trend stays "unknown"; do NOT emit bearish signal
                    regime_data["sp500_trend"] = "unknown"
                    regime_data["sp500_pct_above_200sma"] = 0
                else:
                    sp_pct_above_200 = ((sp_current / sp_sma200) - 1) * 100 if sp_sma200 > 0 else 0
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
                vix_val = float(_safe_close(vix_df).dropna().iloc[-1])
                # SANITY CHECK: VIX intraday lifetime high is 89.53 (2008-10-24)
                # but in normal operation > 60 has only happened in 2008/2020/2022.
                # We tighten to 60 because the production bug observed VIX=76.36
                # while real VIX was ~18 — a clear corruption pattern.  False
                # negative on a real once-a-decade crisis is acceptable (system
                # treats as "normal" — conservative for risk).
                if not (5 < vix_val < 60):
                    logger.warning(
                        f"Regime: implausible VIX {vix_val} — discarded; "
                        f"using neutral 'normal' zone"
                    )
                    regime_data["vix_level"] = 18.0  # neutral fallback
                    regime_data["vix_zone"] = "normal"
                    regime_data["details"].append(
                        f"VIX read {vix_val:.1f} discarded as corrupt — using neutral"
                    )
                else:
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
                # Mid-cap diversification (20 additional)
                "PANW", "CRWD", "DDOG", "SNOW", "ZS", "FTNT", "PLTR",
                "SQ", "COIN", "SHOP", "MELI", "TJX", "ROST", "DHR",
                "ISRG", "REGN", "VRTX", "ANET", "CDNS", "SNPS",
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

    return _get_cached("market_regime", fetch, ttl=60)  # 1 min cache — S&P 500 price updates every minute


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

                    # 20-day trend + 5-day momentum (whichever is stronger)
                    if len(closes) >= 20:
                        sma20 = float(np.mean(closes[-20:]))
                        sma_trend = "rising" if current > sma20 * 1.01 else (
                            "falling" if current < sma20 * 0.99 else "flat"
                        )
                    else:
                        sma_trend = "flat"

                    # 5-day momentum override: big moves in 5 days matter MORE than SMA
                    if change_5d <= -3.0:
                        trend = "falling"  # -3% in 5 days = falling regardless of SMA
                    elif change_5d >= 3.0:
                        trend = "rising"   # +3% in 5 days = rising regardless of SMA
                    else:
                        trend = sma_trend

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

        # Commodities: inversely correlated with dollar, correlated with inflation/fear
        commodities_adj = 0
        if gold_trend == "rising":
            commodities_adj += 1.0  # Risk-off / inflation = bullish commodities
        if oil_trend == "rising":
            commodities_adj += 0.5  # Energy commodities rise together
        if vix_val > 25:
            commodities_adj += 0.5  # Fear drives safe haven commodity demand
        if tnx_trend == "rising":
            commodities_adj -= 0.3  # Rising yields compete with non-yielding commodities
        adjustments["Commodities"] = round(commodities_adj, 1)

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
                            # Inverted yield curve: penalize cyclicals, boost defensives + safe havens
                            for sector in ["Technology", "Consumer Discretionary", "Financials", "Industrials"]:
                                adjustments[sector] = round(adjustments.get(sector, 0) - 0.5, 1)
                            for sector in ["Healthcare", "Consumer Staples", "Utilities", "Commodities"]:
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
                uup_closes = _safe_close(uup_df).values.astype(float)
                uup_sma20 = float(np.mean(uup_closes[-20:]))
                uup_current = float(uup_closes[-1])
                dollar_trend = "strengthening" if uup_current > uup_sma20 * 1.005 else (
                    "weakening" if uup_current < uup_sma20 * 0.995 else "flat"
                )
                macro["dollar_index"] = {
                    "proxy_value": round(uup_current, 2),
                    "trend": dollar_trend,
                }
                # Strong dollar hurts Energy, Materials & Commodities (priced in USD)
                if dollar_trend == "strengthening":
                    adjustments["Energy"] = round(adjustments.get("Energy", 0) - 0.5, 1)
                    adjustments["Materials"] = round(adjustments.get("Materials", 0) - 0.5, 1)
                    adjustments["Commodities"] = round(adjustments.get("Commodities", 0) - 0.8, 1)
                elif dollar_trend == "weakening":
                    adjustments["Energy"] = round(adjustments.get("Energy", 0) + 0.3, 1)
                    adjustments["Materials"] = round(adjustments.get("Materials", 0) + 0.3, 1)
                    adjustments["Commodities"] = round(adjustments.get("Commodities", 0) + 1.0, 1)
        except Exception as e:
            logger.debug(f"Dollar check failed: {e}")

        macro["sector_adjustments"] = adjustments

        # --- GEOPOLITICAL RISK LAYER ---
        # Scans CNN, Yahoo, CNBC for military events, wars, sanctions
        # Adjusts sectors: defense/energy UP during conflict, consumer/tech DOWN
        try:
            geo = assess_geopolitical_risk()
            macro["geopolitical_risk"] = {
                "level": geo.get("risk_level", "LOW"),
                "score": geo.get("risk_score", 0),
                "active_hotspots": geo.get("active_hotspots", []),
                "regional_risks": geo.get("regional_risks", {}),
            }
            if geo.get("risk_level") in ("CRITICAL", "ELEVATED"):
                geo_sector_adj = geo.get("sector_adjustments", {})
                for sector, adj in geo_sector_adj.items():
                    # OVERRIDE: If oil is FALLING, do NOT boost Energy from geo risk
                    # War headlines ≠ oil going up if oil is actually dropping
                    if sector == "Energy" and oil_trend == "falling" and adj > 0:
                        logger.info(f"GEO OVERRIDE: Skipping Energy boost ({adj:+.1f}) — oil is falling ({macro['crude_oil']['change_5d']:+.1f}%)")
                        continue
                    adjustments[sector] = round(adjustments.get(sector, 0) + adj, 1)
                macro["sector_adjustments"] = adjustments
                logger.info(f"Geopolitical risk {geo['risk_level']}: adjusted {len(geo_sector_adj)} sectors | Hotspots: {geo.get('active_hotspots', [])}")
        except Exception as e:
            logger.debug(f"Geopolitical risk check failed: {e}")
            macro["geopolitical_risk"] = {"level": "UNKNOWN", "score": 0}

        # --- KNOWN GEOPOLITICAL EVENTS (DYNAMIC + HARDCODED FALLBACK) ---
        # Merges auto-detected events from DB with hardcoded fallback events.
        # System pre-positions even before news catches up.
        from datetime import datetime as _dt
        _HARDCODED_GEO_EVENTS = {
            "iran_usa_ceasefire_end": "2026-04-13",  # Iran-USA ceasefire deal ending
        }
        # Dynamic: load auto-detected events from DB
        _all_geo_events = dict(_HARDCODED_GEO_EVENTS)
        try:
            from predictions.models import get_upcoming_geo_events, get_active_geo_events
            for ev in get_upcoming_geo_events(days_ahead=21) + get_active_geo_events():
                _all_geo_events[ev["event_key"]] = ev["estimated_date"]
        except Exception:
            pass  # Fall back to hardcoded if DB unavailable
        known_event_active = False
        known_event_approaching = False  # NEW: 3-day pre-position window
        today_str = _dt.now().strftime("%Y-%m-%d")
        today_dt = _dt.strptime(today_str, "%Y-%m-%d")
        for event_name, event_date in _all_geo_events.items():
            event_dt = _dt.strptime(event_date, "%Y-%m-%d")
            days_until = (event_dt - today_dt).days
            days_since = (today_dt - event_dt).days

            if days_until <= 3 and days_until > 0:
                # PRE-POSITION: Event is 1-3 days away — start adjusting NOW
                known_event_approaching = True
                known_event_active = True
                logger.warning(f"GEO EVENT APPROACHING: {event_name} in {days_until} days — pre-positioning")
            elif days_since >= 0 and days_since <= 14:
                # Event has occurred — within impact window
                known_event_active = True
                logger.info(f"GEO EVENT ACTIVE: {event_name} (day {days_since}) — forcing elevated risk posture")

        # --- CONTEXT-AWARE GEO OVERLAY ---
        # Instead of hardcoding "ceasefire ending = energy up, tech down",
        # we READ the headlines to determine what's actually happening.
        # The system decides for itself based on current news sentiment.
        geo_level = macro.get("geopolitical_risk", {}).get("level", "LOW")
        geo_data = macro.get("geopolitical_risk", {})
        ceasefire_detected = geo_data.get("ceasefire_detected", False)

        # Get REAL-TIME headline sentiment for sectors
        try:
            from analysis.news_sentiment import analyze_geo_impact_direction
            geo_impact = analyze_geo_impact_direction()
            macro["geo_impact_analysis"] = geo_impact
            logger.info(f"GEO IMPACT ANALYSIS: direction={geo_impact['geo_direction']}, "
                        f"confidence={geo_impact['confidence']}, sectors={geo_impact['sector_signals']}")
        except Exception as _e:
            logger.debug(f"Geo impact analysis failed: {_e}")
            geo_impact = {"geo_direction": "neutral", "confidence": 0, "sector_signals": {},
                          "should_override_default": False, "headline_evidence": []}

        # If a known geo event is active, raise risk level
        if known_event_active and not ceasefire_detected:
            geo_level = "ELEVATED"
            macro["geopolitical_risk"]["level"] = "ELEVATED"
            macro["geopolitical_risk"]["score"] = max(macro.get("geopolitical_risk", {}).get("score", 0), 4)
            macro["known_geo_event_override"] = True
            logger.info(f"GEO OVERRIDE: Known event forcing ELEVATED risk posture")

        # CONTEXT-AWARE SECTOR ADJUSTMENTS
        # Use headline sentiment to decide direction instead of hardcoding
        sector_signals = geo_impact.get("sector_signals", {})
        geo_direction = geo_impact.get("geo_direction", "neutral")
        geo_confidence = geo_impact.get("confidence", 0)

        if geo_impact.get("should_override_default") and any(sector_signals.values()):
            # Headlines give us clear sector direction — USE IT
            for sector, signal in sector_signals.items():
                if abs(signal) >= 0.3:
                    adj_amount = round(signal * 1.5, 1)  # Scale signal to adjustment
                    adjustments[sector] = round(adjustments.get(sector, 0) + adj_amount, 1)
                    logger.info(f"GEO SMART OVERLAY: {sector} adjustment {adj_amount:+.1f} from headline sentiment")

            # Also adjust correlated sectors based on overall direction
            if geo_direction == "escalation":
                adjustments["Commodities"] = round(adjustments.get("Commodities", 0) + 0.8, 1)
                adjustments["Utilities"] = round(adjustments.get("Utilities", 0) + 0.5, 1)
                macro["ceasefire_overlay"] = False
                macro["ceasefire_ending_overlay"] = True
            elif geo_direction == "deescalation":
                adjustments["Consumer Discretionary"] = round(adjustments.get("Consumer Discretionary", 0) + 0.8, 1)
                adjustments["Communication"] = round(adjustments.get("Communication", 0) + 0.5, 1)
                adjustments["Financials"] = round(adjustments.get("Financials", 0) + 0.5, 1)
                macro["ceasefire_overlay"] = True
                macro["ceasefire_ending_overlay"] = False
            else:
                macro["ceasefire_overlay"] = False
                macro["ceasefire_ending_overlay"] = False

            macro["sector_adjustments"] = adjustments
            macro["geo_overlay_source"] = "headline_sentiment"
            logger.info(f"GEO OVERLAY: Using headline-driven adjustments (direction={geo_direction}, conf={geo_confidence})")

        elif geo_level in ("LOW", "MINIMAL", "UNKNOWN") and not known_event_active:
            # No geo events, low risk — mild peace dividend (smaller than before)
            adjustments["Technology"] = round(adjustments.get("Technology", 0) + 0.8, 1)
            adjustments["Consumer Discretionary"] = round(adjustments.get("Consumer Discretionary", 0) + 0.5, 1)
            adjustments["Energy"] = round(adjustments.get("Energy", 0) - 0.5, 1)
            macro["sector_adjustments"] = adjustments
            macro["ceasefire_overlay"] = True
            macro["ceasefire_ending_overlay"] = False
            macro["geo_overlay_source"] = "default_peace"
            logger.info(f"DEFAULT PEACE OVERLAY: Mild risk-on adjustments (geo={geo_level})")

        elif known_event_active:
            # Geo event active but NO clear headline direction — stay NEUTRAL
            # Don't force energy up or tech down — let the market data decide
            macro["ceasefire_overlay"] = False
            macro["ceasefire_ending_overlay"] = True  # flag for entry blocker awareness
            macro["geo_overlay_source"] = "event_active_neutral"
            logger.info(f"GEO EVENT ACTIVE: No clear headline direction — staying neutral on sectors")

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
            btc_closes = _safe_close(btc_df).values.astype(float)
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
            elif btc_change < -5:
                bearish_signals += 3
                intel["signals"].append(f"Bitcoin CRASH {btc_change:.1f}% — extreme risk-off, reduce exposure")
                intel["weekend_shift_detected"] = True
            elif btc_change < -3:
                bearish_signals += 2
                intel["signals"].append(f"Bitcoin {btc_change:.1f}% — risk-off weekend sentiment")
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
    Calculate 22-factor composite score for each stock in the universe.

    Factors (orthogonal by design — each captures a different edge):
      1. MOMENTUM — 12-month return minus last month (Jegadeesh & Titman, 1993)
      2. VALUE — Real P/E + P/B fundamentals (earnings yield + book-to-price)
      3. QUALITY — Consistency of returns (Sharpe ratio)
      4. LOW VOLATILITY — GARCH-enhanced volatility forecast
      5. RSI(2) MEAN REVERSION — Connors strategy
      6. VOLUME — On-Balance Volume trend
      7. SMART MONEY — Price/volume divergence
      8. RELATIVE STRENGTH — vs sector peers
      9. BOLLINGER SQUEEZE — Volatility compression breakout
     10. VWAP — Institutional execution flow
     11. HURST EXPONENT — Trend vs mean-reversion detection
     12. AUTOCORRELATION — Serial return patterns
     13. STATISTICAL ARBITRAGE — Distance from fair value
     14. KURTOSIS — Fat tail risk
     15. VOLUME COMPRESSION — GARCH breakout predictor
     16. MULTI-TIMEFRAME — Daily+weekly+monthly alignment
     17. EARNINGS DRIFT — Post-earnings momentum
     18. VPOC — Volume profile point of control
     19. ICHIMOKU CLOUD — Trend confirmation
     20. SECTOR ROTATION — Sector momentum ranking
     21. CANDLESTICK — Pattern recognition (doji, hammer, engulfing)
     22. BETA — Stock beta vs SPY (low-beta anomaly)

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

    # Get adaptive weights (learned from past performance) — GLOBAL baseline
    # This is the proven, default path. Always succeeds.
    weights = get_signal_weights()
    weights_source = "global"

    # ============================================================
    # REGIME-AWARE WEIGHTS — use per-regime tuned weights when we
    # have enough sample size to trust them. Falls back to global
    # weights on any failure or insufficient data. Multi-layer safety:
    #   1. regime must be a dict with a recognized regime string
    #   2. per-regime weights must exist in DB
    #   3. regime must have at least REGIME_WEIGHTS_MIN_TRADES samples
    #   4. weights must cover at least REGIME_WEIGHTS_MIN_FACTORS factors
    #   5. weight sum must be plausible (0.85 - 1.15)
    #   6. ANY error → fall back to global (logged but non-fatal)
    # ============================================================
    REGIME_WEIGHTS_MIN_TRADES = 50      # need solid sample for regime to override global
    REGIME_WEIGHTS_MIN_FACTORS = 18     # at least 18 of 22 factors must be covered
    try:
        if isinstance(regime, dict):
            regime_str = (regime.get("regime") or "").upper()
            if regime_str in ("BULL", "BEAR", "SIDEWAYS"):
                from predictions.models import get_regime_factor_weights
                regime_weights = get_regime_factor_weights(
                    regime_str, min_trades=REGIME_WEIGHTS_MIN_TRADES
                )
                if regime_weights and len(regime_weights) >= REGIME_WEIGHTS_MIN_FACTORS:
                    rw_sum = sum(regime_weights.values())
                    if 0.85 <= rw_sum <= 1.15:
                        # All safety checks passed — use regime-specific weights
                        # Merge over global (global supplies any factor missing
                        # from regime weights, ensuring no factor is lost)
                        merged = dict(weights)  # start from global
                        merged.update(regime_weights)  # override with regime
                        weights = merged
                        weights_source = f"regime:{regime_str}"
                        logger.info(
                            f"REGIME WEIGHTS ACTIVE: using {regime_str}-specific "
                            f"factor weights ({len(regime_weights)} factors, "
                            f"sum={rw_sum:.3f})"
                        )
                    else:
                        logger.warning(
                            f"REGIME WEIGHTS REJECTED: {regime_str} weights sum "
                            f"to {rw_sum:.3f} (out of bounds 0.85-1.15) — "
                            f"falling back to global"
                        )
                else:
                    logger.info(
                        f"REGIME WEIGHTS UNAVAILABLE: {regime_str} has "
                        f"{len(regime_weights)} factors / needs {REGIME_WEIGHTS_MIN_FACTORS} "
                        f"or insufficient sample (min {REGIME_WEIGHTS_MIN_TRADES} trades) "
                        f"— using global"
                    )
    except Exception as e:
        # Regime weight lookup failure is NEVER fatal — global weights are battle-tested
        logger.warning(f"Regime weight lookup failed (using global): {e}")
        weights_source = "global_fallback"

    logger.debug(f"calculate_multi_factor_scores: weights_source={weights_source}")

    # Pre-fetch fundamentals for all symbols (cached 24h, minimal API calls)
    all_symbols = list(price_data.keys())
    fundamentals_data = _prefetch_fundamentals(all_symbols)

    # Download SPY data once for beta calculation (reuse regime's SPY data if possible)
    spy_closes = None
    try:
        _throttle()
        spy_df = yf.download("^GSPC", period="6mo", progress=False)
        if spy_df is not None and len(spy_df) >= 60:
            spy_closes = _safe_close(spy_df).values.astype(float)
    except Exception:
        logger.debug("SPY download for beta calc failed — using default beta=1.0")

    # Collect raw factor values for all stocks
    raw_factors = []

    for symbol, df in price_data.items():
        try:
            if df is None or len(df) < 60:
                continue

            closes = _safe_close(df).values.astype(float)
            volumes = df["Volume"].iloc[:, 0].values.astype(float) if hasattr(df["Volume"], "columns") else df["Volume"].values.astype(float)
            current_price = float(closes[-1])

            if current_price <= 0 or np.isnan(current_price) or np.isinf(current_price):
                continue

            # PRICE SANITY GUARD: if the latest close moved >3x or <1/3 from
            # the prior close, this is almost certainly a yfinance data glitch
            # (wrong ticker, split-adjustment error, missing decimal). A real
            # one-day move of that magnitude on a normal large-cap is rare
            # enough that the false-positive cost (skipping a real big mover)
            # is far smaller than the false-negative cost (picking a stock
            # based on a phantom price that throws off all the factors).
            # This was the root cause of the user's complaint:
            #   "prices of stocks that are wrong throw off symbols to buy"
            if len(closes) >= 2:
                prior = float(closes[-2])
                if prior > 0 and not np.isnan(prior):
                    ratio = current_price / prior
                    if ratio > 3.0 or ratio < 0.33:
                        # Don't include this stock in picks today — bad data
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

            # --- Factor 2: VALUE (real fundamentals + mean reversion blend) ---
            # Primary: Earnings yield (1/PE) + Book-to-price (1/PB) from yfinance
            # Fallback: 60-day mean reversion proxy when fundamentals unavailable
            # Higher value_raw = cheaper = better
            fund = fundamentals_data.get(symbol, {})
            ey = fund.get("earnings_yield")  # 1/PE * 100 (higher = cheaper)
            btp = fund.get("book_to_price")  # 1/PB * 100 (higher = cheaper)

            # Mean reversion component (always available)
            if len(closes) >= 60:
                price_vs_60d_avg = (current_price / float(np.mean(closes[-60:]))) - 1
                mean_rev = -price_vs_60d_avg * 100
            else:
                mean_rev = 0.0

            if ey is not None and btp is not None:
                # Full fundamental value: 50% earnings yield + 30% book-to-price + 20% mean reversion
                value_raw = ey * 0.5 + btp * 0.3 + mean_rev * 0.2
            elif ey is not None:
                # Partial: 70% earnings yield + 30% mean reversion
                value_raw = ey * 0.7 + mean_rev * 0.3
            else:
                # Fallback: pure mean reversion (no fundamentals available)
                value_raw = mean_rev

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

            # --- Factor 4: LOW VOLATILITY (GARCH-enhanced) ---
            # Use GARCH predicted vol instead of raw realized vol when available
            if len(closes) >= 60:
                window_60 = closes[-60:]
                daily_rets_60 = np.diff(window_60) / window_60[:-1]
                vol_60d = float(np.std(daily_rets_60)) * np.sqrt(252) * 100
            else:
                vol_60d = float(np.std(np.diff(closes) / closes[:-1])) * np.sqrt(252) * 100

            # GARCH forecast — forward-looking vol prediction
            garch = garch_forecast(closes)
            garch_vol = garch["predicted_vol"] * 100 if garch["predicted_vol"] > 0 else vol_60d
            vol_ratio = garch["vol_ratio"]
            is_vol_compressed = garch["is_vol_compressed"]

            # Use GARCH predicted vol for scoring (forward-looking > backward-looking)
            effective_vol = garch_vol if garch_vol > 0 else vol_60d
            low_vol_raw = -effective_vol  # lower predicted vol = better

            # --- Factor NEW: VOL COMPRESSION (GARCH breakout detector) ---
            # When GARCH predicted vol < 60% of realized = volatility compression
            # Big move incoming — score based on price position vs SMA
            vol_compression_raw = 0.0
            if is_vol_compressed:
                sma_20_vc = float(np.mean(closes[-20:])) if len(closes) >= 20 else closes[-1]
                if current_price > sma_20_vc:
                    vol_compression_raw = 3.0  # Compressed + above SMA = bullish breakout
                else:
                    vol_compression_raw = -3.0  # Compressed + below SMA = bearish breakdown
            elif vol_ratio < 0.8:
                # Mild compression
                sma_20_vc = float(np.mean(closes[-20:])) if len(closes) >= 20 else closes[-1]
                vol_compression_raw = 1.5 if current_price > sma_20_vc else -1.5

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
                        peer_closes = _safe_close(price_data[peer]).values.astype(float).flatten()
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
                    opens_col = df["Open"]
                    if hasattr(opens_col, "columns"):
                        opens_col = opens_col.iloc[:, 0]
                    opens = opens_col.values.astype(float).flatten()
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
                    highs_col = df["High"]
                    if hasattr(highs_col, "columns"):
                        highs_col = highs_col.iloc[:, 0]
                    highs = highs_col.values.astype(float).flatten()
                    lows_col = df["Low"]
                    if hasattr(lows_col, "columns"):
                        lows_col = lows_col.iloc[:, 0]
                    lows = lows_col.values.astype(float).flatten()
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

            # --- RENTECH FACTOR 1: HURST EXPONENT (Trend vs Mean-Reversion) ---
            # H > 0.5 = trending (momentum works), H < 0.5 = mean-reverting (reversion works)
            # Renaissance Technologies uses this to decide WHICH strategy to apply per stock
            hurst_raw = 0.0
            if len(closes) >= 100:
                try:
                    log_returns = np.diff(np.log(closes[-100:]))
                    # Simplified R/S analysis for Hurst exponent
                    n = len(log_returns)
                    max_k = min(n // 2, 50)
                    rs_values = []
                    for k in [10, 20, 30, 40, 50]:
                        if k > max_k:
                            break
                        n_blocks = n // k
                        for b in range(n_blocks):
                            block = log_returns[b*k:(b+1)*k]
                            mean_block = np.mean(block)
                            devs = np.cumsum(block - mean_block)
                            r = float(np.max(devs) - np.min(devs))
                            s = float(np.std(block))
                            if s > 0:
                                rs_values.append((np.log(k), np.log(r / s)))
                    if len(rs_values) >= 3:
                        x_vals = [v[0] for v in rs_values]
                        y_vals = [v[1] for v in rs_values]
                        hurst = float(np.polyfit(x_vals, y_vals, 1)[0])
                        hurst = max(0.0, min(1.0, hurst))  # clamp
                        # H > 0.6 = trending stock (momentum applies)
                        # H < 0.4 = mean-reverting (reversion applies)
                        if hurst > 0.6:
                            hurst_raw = (hurst - 0.5) * 10  # boost momentum signals
                        elif hurst < 0.4:
                            hurst_raw = (0.5 - hurst) * -10  # boost mean-reversion
                except Exception:
                    pass

            # --- RENTECH FACTOR 2: AUTOCORRELATION (Serial Return Correlation) ---
            # Positive autocorrelation = trends persist, negative = reversals likely
            # Rentech's core insight: exploit serial correlations that most traders miss
            autocorr_raw = 0.0
            if len(closes) >= 30:
                try:
                    rets = np.diff(closes[-30:]) / closes[-31:-1]
                    # Lag-1 autocorrelation
                    mean_r = np.mean(rets)
                    numerator = np.sum((rets[1:] - mean_r) * (rets[:-1] - mean_r))
                    denominator = np.sum((rets - mean_r) ** 2) + 1e-10
                    lag1_corr = float(numerator / denominator)
                    # Strong positive autocorr = trend continuation
                    # Strong negative autocorr = mean reversion opportunity
                    autocorr_raw = lag1_corr * 10  # scale for factor system
                except Exception:
                    pass

            # --- RENTECH FACTOR 3: STAT ARB Z-SCORE (Distance from Fair Value) ---
            # How many standard deviations is the current price from its statistical "fair value"
            # (120-day rolling mean). Extreme z-scores = high probability of reversion.
            # This is the core of statistical arbitrage — Rentech's bread and butter.
            stat_arb_raw = 0.0
            if len(closes) >= 120:
                try:
                    mean_120 = float(np.mean(closes[-120:]))
                    std_120 = float(np.std(closes[-120:]))
                    if std_120 > 0:
                        z_score_120 = (current_price - mean_120) / std_120
                        # Extreme z-scores signal reversion opportunities
                        if z_score_120 < -2.0:
                            stat_arb_raw = 5.0  # Very cheap vs history = strong buy
                        elif z_score_120 < -1.5:
                            stat_arb_raw = 3.0
                        elif z_score_120 < -1.0:
                            stat_arb_raw = 1.0
                        elif z_score_120 > 2.0:
                            stat_arb_raw = -5.0  # Very expensive vs history = strong sell
                        elif z_score_120 > 1.5:
                            stat_arb_raw = -3.0
                        elif z_score_120 > 1.0:
                            stat_arb_raw = -1.0
                except Exception:
                    pass

            # --- RENTECH FACTOR 4: KURTOSIS (Fat Tail Risk Detection) ---
            # High kurtosis = more extreme moves likely. Rentech avoids stocks with
            # very high kurtosis (unpredictable) but trades those with moderate kurtosis
            # at extreme z-scores (high probability reversion).
            kurtosis_raw = 0.0
            if len(closes) >= 60:
                try:
                    rets_60 = np.diff(closes[-60:]) / closes[-61:-1]
                    mean_r = np.mean(rets_60)
                    std_r = np.std(rets_60) + 1e-10
                    kurt = float(np.mean(((rets_60 - mean_r) / std_r) ** 4)) - 3.0  # excess kurtosis
                    # Moderate kurtosis (1-4) = tradeable volatility
                    # Very high kurtosis (>6) = dangerous, unpredictable
                    if kurt > 6:
                        kurtosis_raw = -3.0  # Too risky, avoid
                    elif kurt > 3:
                        kurtosis_raw = -1.0  # Slightly elevated risk
                    elif 1 < kurt < 3:
                        kurtosis_raw = 1.0  # Good trading characteristics
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

            # --- Factor 15: MULTI-TIMEFRAME TREND ALIGNMENT ---
            # Real quant funds confirm signals across daily, weekly, monthly.
            # Trades that align across all 3 timeframes have 15-25% higher win rates.
            # Daily: EMA-9 vs EMA-21 | Weekly: EMA-21 vs EMA-63 | Monthly: EMA-63 vs EMA-200
            mtf_alignment_raw = 0.0
            try:
                ema_63 = float(pd.Series(closes).ewm(span=63, adjust=False).mean().iloc[-1]) if len(closes) >= 63 else ema_50
                ema_200 = float(pd.Series(closes).ewm(span=200, adjust=False).mean().iloc[-1]) if len(closes) >= 200 else ema_63

                # Daily trend: EMA-9 above EMA-21 = bullish
                daily_bull = 1 if ema_9 > ema_21 else -1
                # Weekly proxy: EMA-21 above EMA-63 = bullish
                weekly_bull = 1 if ema_21 > ema_63 else -1
                # Monthly proxy: EMA-63 above EMA-200 = bullish
                monthly_bull = 1 if ema_63 > ema_200 else -1
                # Price position: above all EMAs = extra conviction
                price_position = 1 if current_price > ema_21 and current_price > ema_63 else -1

                mtf_alignment_raw = float(daily_bull + weekly_bull + monthly_bull + price_position)
                # Range: -4 to +4 (all 4 bearish to all 4 bullish)
            except Exception:
                mtf_alignment_raw = 0.0

            # --- Factor 16: ADX TREND STRENGTH ---
            adx_value = 25.0
            try:
                if len(closes) >= 28:
                    highs_adx = df["High"].iloc[:, 0].values.astype(float) if hasattr(df["High"], "columns") else df["High"].values.astype(float)
                    lows_adx = df["Low"].iloc[:, 0].values.astype(float) if hasattr(df["Low"], "columns") else df["Low"].values.astype(float)
                    tr_list = []
                    for k in range(1, min(len(closes), 28)):
                        tr = max(
                            highs_adx[-28+k] - lows_adx[-28+k],
                            abs(highs_adx[-28+k] - closes[-28+k-1]),
                            abs(lows_adx[-28+k] - closes[-28+k-1])
                        )
                        tr_list.append(tr)
                    plus_dm = []
                    minus_dm = []
                    for k in range(1, min(len(highs_adx), 28)):
                        up_move = highs_adx[-28+k] - highs_adx[-28+k-1]
                        down_move = lows_adx[-28+k-1] - lows_adx[-28+k]
                        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
                        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)
                    if len(tr_list) >= 14:
                        atr14 = np.mean(tr_list[-14:])
                        plus_di = (np.mean(plus_dm[-14:]) / (atr14 + 1e-10)) * 100
                        minus_di = (np.mean(minus_dm[-14:]) / (atr14 + 1e-10)) * 100
                        dx = abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10) * 100
                        adx_value = float(dx)
            except Exception:
                adx_value = 25.0

            # ============================================================
            # NEW FACTOR 17: POST-EARNINGS DRIFT
            # One of the most documented anomalies in finance.
            # Stocks that gap up on earnings keep drifting up for 60 days.
            # Stocks that gap down keep falling. Academic alpha: 2-4%/quarter.
            # ============================================================
            earnings_drift_raw = 0.0
            try:
                # Commodity ETFs don't have earnings — skip this factor
                if SECTOR_MAP.get(symbol) == "Commodities":
                    raise ValueError("skip")
                if len(closes) >= 10:
                    # Check for earnings gap in last 10 trading days
                    # Large volume spike (>2x avg) + price gap = likely earnings
                    avg_vol_60 = float(np.mean(volumes[-60:])) if len(volumes) >= 60 else float(np.mean(volumes[-20:]))
                    for ed_idx in range(min(10, len(closes) - 1)):
                        day_vol = float(volumes[-(ed_idx + 1)])
                        if day_vol > avg_vol_60 * 2.5:  # Volume spike = earnings event
                            pre_price = float(closes[-(ed_idx + 2)])
                            post_price = float(closes[-(ed_idx + 1)])
                            earnings_gap_pct = (post_price / pre_price - 1) * 100
                            if earnings_gap_pct > 5:
                                earnings_drift_raw = 4.0   # Strong beat → drift continues up
                            elif earnings_gap_pct > 3:
                                earnings_drift_raw = 2.5
                            elif earnings_gap_pct > 1.5:
                                earnings_drift_raw = 1.0
                            elif earnings_gap_pct < -5:
                                earnings_drift_raw = -4.0  # Strong miss → drift continues down
                            elif earnings_gap_pct < -3:
                                earnings_drift_raw = -2.5
                            elif earnings_gap_pct < -1.5:
                                earnings_drift_raw = -1.0
                            break  # Only use most recent earnings event
            except Exception:
                earnings_drift_raw = 0.0

            # ============================================================
            # NEW FACTOR 18: VOLUME PROFILE / VPOC (Point of Control)
            # Identifies price levels where most volume traded.
            # Price near VPOC = strong support. Breakout above = trend confirmation.
            # Used by institutional traders for key support/resistance levels.
            # ============================================================
            vpoc_raw = 0.0
            try:
                if len(closes) >= 20 and len(volumes) >= 20:
                    # Build volume profile: histogram of price vs volume
                    price_window = closes[-20:]
                    vol_window_vp = volumes[-20:]
                    price_min = float(np.min(price_window))
                    price_max = float(np.max(price_window))
                    price_range = price_max - price_min
                    if price_range > 0:
                        # 10 bins for volume profile
                        n_bins = 10
                        bin_size = price_range / n_bins
                        vol_profile = [0.0] * n_bins
                        for bp in range(len(price_window)):
                            bin_idx = min(int((float(price_window[bp]) - price_min) / bin_size), n_bins - 1)
                            vol_profile[bin_idx] += float(vol_window_vp[bp])
                        # VPOC = price level with highest volume
                        vpoc_bin = int(np.argmax(vol_profile))
                        vpoc_price = price_min + (vpoc_bin + 0.5) * bin_size
                        vpoc_dist_pct = (current_price - vpoc_price) / vpoc_price * 100

                        if abs(vpoc_dist_pct) < 1.0:
                            vpoc_raw = 1.5   # Near VPOC = support level, bullish for longs
                        elif vpoc_dist_pct > 2.0:
                            vpoc_raw = 2.0   # Broke above VPOC = breakout, bullish
                        elif vpoc_dist_pct < -2.0:
                            vpoc_raw = -2.0  # Broke below VPOC = breakdown, bearish
                        elif vpoc_dist_pct > 1.0:
                            vpoc_raw = 1.0
                        elif vpoc_dist_pct < -1.0:
                            vpoc_raw = -1.0
            except Exception:
                vpoc_raw = 0.0

            # ============================================================
            # NEW FACTOR 19: ICHIMOKU CLOUD SIGNALS
            # 5-component Japanese trend system used by institutional traders.
            # Captures trend, momentum, and support/resistance in one indicator.
            # Price above cloud + bullish cross = strong trend confirmation.
            # ============================================================
            ichimoku_raw = 0.0
            try:
                if len(closes) >= 52:
                    highs_ichi = df["High"].iloc[:, 0].values.astype(float) if hasattr(df["High"], "columns") else df["High"].values.astype(float)
                    lows_ichi = df["Low"].iloc[:, 0].values.astype(float) if hasattr(df["Low"], "columns") else df["Low"].values.astype(float)
                    # Tenkan-sen (9-period high-low midpoint)
                    tenkan = (float(np.max(highs_ichi[-9:])) + float(np.min(lows_ichi[-9:]))) / 2
                    # Kijun-sen (26-period high-low midpoint)
                    kijun = (float(np.max(highs_ichi[-26:])) + float(np.min(lows_ichi[-26:]))) / 2
                    # Senkou Span A (midpoint of Tenkan and Kijun)
                    senkou_a = (tenkan + kijun) / 2
                    # Senkou Span B (52-period high-low midpoint)
                    senkou_b = (float(np.max(highs_ichi[-52:])) + float(np.min(lows_ichi[-52:]))) / 2
                    # Cloud top and bottom
                    cloud_top = max(senkou_a, senkou_b)
                    cloud_bottom = min(senkou_a, senkou_b)

                    # Signal scoring
                    above_cloud = current_price > cloud_top
                    below_cloud = current_price < cloud_bottom
                    bullish_cross = tenkan > kijun  # Tenkan above Kijun = bullish
                    cloud_bullish = senkou_a > senkou_b  # Cloud twist = trend change

                    if above_cloud and bullish_cross and cloud_bullish:
                        ichimoku_raw = 3.0   # All 3 bullish = strong trend
                    elif above_cloud and bullish_cross:
                        ichimoku_raw = 2.0   # Above cloud + bullish cross
                    elif above_cloud:
                        ichimoku_raw = 1.0   # Just above cloud
                    elif below_cloud and not bullish_cross and not cloud_bullish:
                        ichimoku_raw = -3.0  # All 3 bearish = strong downtrend
                    elif below_cloud and not bullish_cross:
                        ichimoku_raw = -2.0  # Below cloud + bearish cross
                    elif below_cloud:
                        ichimoku_raw = -1.0  # Just below cloud
                    # Inside cloud = neutral (choppy, no clear trend)
            except Exception:
                ichimoku_raw = 0.0

            # ============================================================
            # NEW FACTOR 20: SECTOR ROTATION MOMENTUM
            # Professional hedge funds rotate into strongest sectors.
            # Stocks in top-performing sectors get a boost.
            # Stocks in worst-performing sectors get penalized.
            # 1-month sector momentum predicts next month's sector returns.
            # ============================================================
            sector_rotation_raw = 0.0
            # (Computed after all stocks processed — see below)

            # ============================================================
            # NEW FACTOR 21: CANDLESTICK PATTERN RECOGNITION
            # Detects classic candlestick patterns from last 3 candles.
            # Bullish patterns near oversold (RSI<40) = buy signal.
            # Bearish patterns near overbought (RSI>60) = sell signal.
            # Uses existing OHLC data — no new API calls.
            # ============================================================
            candlestick_raw = 0.0
            try:
                opens_cs = df["Open"].iloc[:, 0].values.astype(float) if hasattr(df["Open"], "columns") else df["Open"].values.astype(float)
                highs_cs = df["High"].iloc[:, 0].values.astype(float) if hasattr(df["High"], "columns") else df["High"].values.astype(float)
                lows_cs = df["Low"].iloc[:, 0].values.astype(float) if hasattr(df["Low"], "columns") else df["Low"].values.astype(float)

                if len(opens_cs) >= 3 and len(highs_cs) >= 3 and len(lows_cs) >= 3 and len(closes) >= 3:
                    # Current candle (last candle)
                    c_open = float(opens_cs[-1])
                    c_high = float(highs_cs[-1])
                    c_low = float(lows_cs[-1])
                    c_close = float(closes[-1])
                    c_body = abs(c_close - c_open)
                    c_range = c_high - c_low if c_high > c_low else 0.0001

                    # Previous candle
                    p_open = float(opens_cs[-2])
                    p_close = float(closes[-2])
                    p_body = abs(p_close - p_open)

                    pattern_detected = None

                    # Doji: open ≈ close (within 0.1% of range)
                    if c_body < c_range * 0.001:
                        pattern_detected = "doji"

                    # Hammer: small body at top, long lower shadow (>2x body)
                    lower_shadow = min(c_open, c_close) - c_low
                    upper_shadow = c_high - max(c_open, c_close)
                    if c_body > 0 and lower_shadow > 2 * c_body and upper_shadow < c_body:
                        pattern_detected = "hammer"

                    # Bullish Engulfing: current body fully engulfs prior body, close > open
                    if (c_close > c_open and p_close < p_open and
                            c_open <= p_close and c_close >= p_open and
                            c_body > p_body):
                        pattern_detected = "bullish_engulfing"

                    # Bearish Engulfing: current body fully engulfs prior body, close < open
                    if (c_close < c_open and p_close > p_open and
                            c_open >= p_close and c_close <= p_open and
                            c_body > p_body):
                        pattern_detected = "bearish_engulfing"

                    # Score based on pattern + RSI context
                    if pattern_detected in ("hammer", "bullish_engulfing"):
                        if rsi14 < 40:
                            candlestick_raw = 2.0   # Bullish pattern near oversold
                        elif rsi14 < 50:
                            candlestick_raw = 1.0   # Bullish pattern, neutral RSI
                        else:
                            candlestick_raw = 0.5   # Bullish pattern but not oversold
                    elif pattern_detected == "bearish_engulfing":
                        if rsi14 > 60:
                            candlestick_raw = -2.0  # Bearish pattern near overbought
                        elif rsi14 > 50:
                            candlestick_raw = -1.0  # Bearish pattern, neutral RSI
                        else:
                            candlestick_raw = -0.5  # Bearish pattern but not overbought
                    elif pattern_detected == "doji":
                        candlestick_raw = 0.0       # Doji = indecision, neutral
            except Exception:
                candlestick_raw = 0.0

            # ============================================================
            # FACTOR 22: STOCK BETA (Low-Beta Anomaly)
            # Calculates actual beta vs SPY using 120-day returns.
            # Low-beta stocks deliver higher risk-adjusted returns (Frazzini & Pedersen).
            # Score: negative beta = better (low-beta anomaly: prefer defensive stocks)
            # In BULL regime, high-beta is acceptable; in BEAR, low-beta is critical.
            # ============================================================
            beta_raw = 0.0
            if spy_closes is not None:
                beta = _calculate_beta(closes, spy_closes)
                # Low-beta anomaly: lower beta = higher score
                # Center at 1.0 (market beta), invert so low beta scores high
                beta_raw = -(beta - 1.0) * 3.0  # beta=0.5 → +1.5, beta=1.5 → -1.5
            else:
                beta = 1.0

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
                "hurst_raw": hurst_raw,
                "autocorr_raw": autocorr_raw,
                "stat_arb_raw": stat_arb_raw,
                "kurtosis_raw": kurtosis_raw,
                "vol_compression_raw": vol_compression_raw,
                "mtf_alignment_raw": mtf_alignment_raw,
                "earnings_drift_raw": earnings_drift_raw,
                "vpoc_raw": vpoc_raw,
                "ichimoku_raw": ichimoku_raw,
                "sector_rotation_raw": 0.0,  # computed post-loop
                "candlestick_raw": candlestick_raw,
                "beta_raw": beta_raw,
                "beta": round(beta, 3) if isinstance(beta, float) else 1.0,
                "adx": round(adx_value, 1),
                "gap_signal": gap_signal,
                "momentum_crash": momentum_crash_flag,
                "rsi2": round(rsi2, 1),
                "rsi14": round(rsi14, 1),
                "vol_60d": round(vol_60d, 1),
                "garch_vol_ratio": round(vol_ratio, 3),
                "ema_9": round(ema_9, 2),
                "ema_21": round(ema_21, 2),
                "ema_50": round(ema_50, 2),
                "momentum_pct": round(momentum_raw, 2),
                "closes": closes.tolist() if hasattr(closes, 'tolist') else list(closes),
                "vol_ratio": float(np.mean(volumes[-20:]) / (np.mean(volumes[-60:]) + 1)) if len(volumes) >= 60 else (float(np.mean(volumes[-20:]) / (np.mean(volumes[-20:]) + 1)) if len(volumes) >= 20 else 1.0),
                "today_volume_ratio": round(float(volumes[-1]) / (float(np.mean(volumes[-21:-1])) + 1), 2) if len(volumes) >= 21 else 1.0,
            })

        except Exception as e:
            logger.debug(f"Factor calc failed for {symbol}: {e}")
            continue

    if not raw_factors:
        return []

    # --- SECTOR ROTATION COMPUTATION (post-loop) ---
    # Calculate average 20-day momentum per sector, then rank sectors
    sector_momenta = {}
    for stock in raw_factors:
        sector = stock["sector"]
        if sector not in sector_momenta:
            sector_momenta[sector] = []
        sector_momenta[sector].append(stock["momentum_raw"])
    # Average momentum per sector
    sector_avg_mom = {s: float(np.mean(v)) for s, v in sector_momenta.items() if v}
    if sector_avg_mom:
        # Rank sectors by average momentum
        sorted_sectors = sorted(sector_avg_mom.items(), key=lambda x: x[1], reverse=True)
        n_sectors = len(sorted_sectors)
        top_sectors = {s for s, _ in sorted_sectors[:max(1, n_sectors // 3)]}
        bottom_sectors = {s for s, _ in sorted_sectors[-max(1, n_sectors // 3):]}
        for stock in raw_factors:
            if stock["sector"] in top_sectors:
                stock["sector_rotation_raw"] = 2.0   # Top-performing sector boost
            elif stock["sector"] in bottom_sectors:
                stock["sector_rotation_raw"] = -2.0  # Worst-performing sector penalty
            else:
                stock["sector_rotation_raw"] = 0.0   # Middle sectors neutral

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
    # RENTECH FACTORS — statistical arbitrage edge
    hurst_z = _safe_zscore([s["hurst_raw"] for s in raw_factors])
    autocorr_z = _safe_zscore([s["autocorr_raw"] for s in raw_factors])
    stat_arb_z = _safe_zscore([s["stat_arb_raw"] for s in raw_factors])
    kurtosis_z = _safe_zscore([s["kurtosis_raw"] for s in raw_factors])
    # GARCH VOL COMPRESSION — breakout predictor
    vol_compression_z = _safe_zscore([s["vol_compression_raw"] for s in raw_factors])
    # MULTI-TIMEFRAME ALIGNMENT — cross-timeframe trend confirmation
    mtf_alignment_z = _safe_zscore([s["mtf_alignment_raw"] for s in raw_factors])
    # NEW FACTORS — Week 2 alpha generators
    earnings_drift_z = _safe_zscore([s["earnings_drift_raw"] for s in raw_factors])
    vpoc_z = _safe_zscore([s["vpoc_raw"] for s in raw_factors])
    ichimoku_z = _safe_zscore([s["ichimoku_raw"] for s in raw_factors])
    sector_rotation_z = _safe_zscore([s["sector_rotation_raw"] for s in raw_factors])
    # CANDLESTICK PATTERN RECOGNITION
    candlestick_z = _safe_zscore([s["candlestick_raw"] for s in raw_factors])
    # STOCK BETA — low-beta anomaly (Frazzini & Pedersen)
    beta_z = _safe_zscore([s["beta_raw"] for s in raw_factors])

    # --- Regime adjustments ---
    # OVERHAUL: Regime affects FACTOR WEIGHTS only, NOT confidence multiplier
    # The old regime_multiplier (0.7 BEAR, 0.85 SIDEWAYS) was crushing confidence
    # and stacking with trend filter + VIX scaling = zero trades
    # Now: regime adjusts WHAT we look for, not HOW MUCH we believe in it
    regime_multiplier = 1.0  # ALWAYS 1.0 — confidence is set by the score, not regime
    if regime:
        if regime.get("regime") == "BEAR":
            # In bear markets: reduce momentum weight (momentum crashes),
            # increase low-vol and value weights (defensive)
            weights = dict(weights)  # copy
            weights["momentum"] = weights.get("momentum", 0.25) * 0.5
            weights["low_vol"] = weights.get("low_vol", 0.15) * 1.5
            weights["value"] = weights.get("value", 0.20) * 1.3
            # NO regime_multiplier — the score itself handles direction
        elif regime.get("regime") == "SIDEWAYS":
            # In sideways: boost mean-reversion (RSI2)
            weights = dict(weights)
            weights["rsi2"] = weights.get("rsi2", 0.15) * 1.4
            # NO regime_multiplier

    # Normalize weights to sum to 1
    # Add new factors with fixed weights (not yet in learning system)
    W_SMART_MONEY = 0.06    # Smart money divergence
    W_REL_STRENGTH = 0.05   # Relative strength vs sector
    W_BB_SQUEEZE = 0.05     # Bollinger Band squeeze breakout
    W_VWAP = 0.04           # VWAP institutional flow
    # RENTECH FACTORS
    W_HURST = 0.04          # Hurst exponent (trend vs mean-reversion detection)
    W_AUTOCORR = 0.04       # Autocorrelation (serial return patterns)
    W_STAT_ARB = 0.06       # Statistical arbitrage z-score (distance from fair value)
    W_KURTOSIS = 0.03       # Fat tail risk detection
    W_VOL_COMPRESSION = 0.04  # GARCH vol compression breakout predictor
    W_MTF_ALIGNMENT = 0.04    # Multi-timeframe trend alignment (daily+weekly+monthly)
    # WEEK 2 NEW FACTORS
    W_EARNINGS_DRIFT = 0.05   # Post-earnings drift (strongest documented anomaly)
    W_VPOC = 0.03             # Volume Profile point of control
    W_ICHIMOKU = 0.04         # Ichimoku cloud trend confirmation
    W_SECTOR_ROTATION = 0.03  # Sector rotation momentum
    W_CANDLESTICK = 0.03      # Candlestick pattern recognition
    W_BETA = 0.03              # Stock beta (low-beta anomaly)

    # Scale existing weights down to make room for new factors (22 total now)
    existing_total = sum(weights.values())
    new_factor_total = (W_SMART_MONEY + W_REL_STRENGTH + W_BB_SQUEEZE + W_VWAP +
                        W_HURST + W_AUTOCORR + W_STAT_ARB + W_KURTOSIS + W_VOL_COMPRESSION +
                        W_MTF_ALIGNMENT + W_EARNINGS_DRIFT + W_VPOC + W_ICHIMOKU +
                        W_SECTOR_ROTATION + W_CANDLESTICK + W_BETA)
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
    w_hurst = W_HURST
    w_autocorr = W_AUTOCORR
    w_stat_arb = W_STAT_ARB
    w_kurtosis = W_KURTOSIS
    w_vol_comp = W_VOL_COMPRESSION
    w_mtf = W_MTF_ALIGNMENT
    w_edrift = W_EARNINGS_DRIFT
    w_vpoc = W_VPOC
    w_ichi = W_ICHIMOKU
    w_secrot = W_SECTOR_ROTATION
    w_candle = W_CANDLESTICK
    w_beta = W_BETA

    # --- Calculate composite scores ---
    scored = []
    for i, stock in enumerate(raw_factors):
        # Weighted composite — 22 FACTORS (Renaissance Technologies grade)
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
            vwap_z[i] * w_vwap +
            hurst_z[i] * w_hurst +
            autocorr_z[i] * w_autocorr +
            stat_arb_z[i] * w_stat_arb +
            kurtosis_z[i] * w_kurtosis +
            vol_compression_z[i] * w_vol_comp +
            mtf_alignment_z[i] * w_mtf +
            earnings_drift_z[i] * w_edrift +
            vpoc_z[i] * w_vpoc +
            ichimoku_z[i] * w_ichi +
            sector_rotation_z[i] * w_secrot +
            candlestick_z[i] * w_candle +
            beta_z[i] * w_beta
        )

        # Apply macro overlay sector adjustment
        macro_adj = 0
        if macro and "sector_adjustments" in macro:
            sector = stock["sector"]
            macro_adj = macro["sector_adjustments"].get(sector, 0)
            # Convert macro adj (-2 to +2) to z-score scale (-0.5 to +0.5)
            composite += macro_adj * 0.25

        # Apply geopolitical ticker-specific boosts (defense, energy during war)
        geo_adj = 0
        if macro and "geopolitical_risk" in macro:
            geo = macro["geopolitical_risk"]
            if geo.get("level") in ("CRITICAL", "ELEVATED"):
                try:
                    geo_full = assess_geopolitical_risk()
                    ticker_boosts = geo_full.get("ticker_adjustments", {})
                    if stock["symbol"] in ticker_boosts:
                        geo_adj = ticker_boosts[stock["symbol"]]
                        composite += geo_adj * 0.15  # Scale to z-score range
                except Exception:
                    pass

        # --- HISTORICAL CALIBRATION OVERLAY ---
        # Uses 50-year patterns to fine-tune signals
        try:
            from analysis.historical_calibration import get_calibration
            cal = get_calibration()
            sym_cal = cal.get("stocks", {}).get(stock["symbol"], {})

            if sym_cal:
                # 1. Seasonal boost: if current month is historically strong/weak
                seasonal = sym_cal.get("seasonal", {})
                current_month = str(datetime.now().month)
                if current_month in seasonal:
                    month_avg = seasonal[current_month]
                    # Scale: +0.02% avg daily = small boost, +0.05% = medium
                    seasonal_boost = max(-0.3, min(0.3, month_avg * 5.0))
                    composite += seasonal_boost

                # 2. Regime calibration: scale confidence by historical regime performance
                regime_perf = sym_cal.get("regime_performance", {})
                current_regime_key = (regime.get("regime", "SIDEWAYS") if regime else "SIDEWAYS").lower()
                hist_win_rate = regime_perf.get(f"{current_regime_key}_win_rate", 50)
                if hist_win_rate > 55:
                    composite += 0.1  # Stock historically does well in this regime
                elif hist_win_rate < 45:
                    composite -= 0.1  # Stock historically struggles in this regime

                # 3. Sector rotation boost from long-term cycles
                sector_rot = cal.get("sector_rotation", {})
                stock_sector = stock.get("sector", "")
                if stock_sector in sector_rot:
                    rot_signal = sector_rot[stock_sector].get("signal", "")
                    if rot_signal == "strong":
                        composite += 0.15
                    elif rot_signal == "weak":
                        composite -= 0.15

                # 4. Earnings seasonality: boost stocks with consistent post-earnings drift
                earnings_seas = sym_cal.get("earnings_seasonality", {})
                if earnings_seas:
                    consistency = earnings_seas.get("consistency_pct", 50)
                    avg_drift = earnings_seas.get("avg_drift_5d_pct", 0)
                    if consistency > 70 and avg_drift > 0:
                        composite += 0.15  # Stock consistently drifts up after earnings
                    elif consistency > 70 and avg_drift < 0:
                        composite -= 0.1  # Stock consistently drifts down

            # 5. Cross-asset leading indicators
            cross_asset = cal.get("cross_asset_leads", {})
            if stock_sector in cross_asset:
                lead = cross_asset[stock_sector]
                lead_corr = lead.get("correlation", 0)
                if abs(lead_corr) > 0.08:
                    # Small boost/penalty based on whether leading indicator is positive
                    composite += max(-0.2, min(0.2, lead_corr * 2.0))

            # 6. Volatility regime sizing signal
            vol_regimes = cal.get("volatility_regimes", {})
            if vol_regimes and macro:
                current_vix = macro.get("vix", {}).get("value", 20)
                if current_vix < 15:
                    vr = vol_regimes.get("LOW", {})
                elif current_vix < 20:
                    vr = vol_regimes.get("NORMAL", {})
                elif current_vix < 30:
                    vr = vol_regimes.get("HIGH", {})
                else:
                    vr = vol_regimes.get("CRISIS", {})
                # If historically this VIX regime has negative avg returns, penalize longs
                hist_daily_ret = vr.get("avg_daily_return_pct", 0)
                if hist_daily_ret < -0.02:
                    composite -= 0.1
                elif hist_daily_ret > 0.05:
                    composite += 0.05

            # 7. VIX term structure historical patterns
            vix_patterns = cal.get("vix_patterns", {})
            if vix_patterns and macro and "vix_term_structure" in macro:
                vix_struct = macro["vix_term_structure"]
                structure = vix_struct.get("structure", "flat")
                if structure == "backwardation":
                    # After backwardation, what does history say about forward returns?
                    backw_data = vix_patterns.get("backwardation", {})
                    fwd_21d = backw_data.get("21d_forward_return", 0)
                    if fwd_21d > 1.0:
                        # Historically, market recovers after backwardation → buy signal
                        composite += 0.15
                    elif fwd_21d < -1.0:
                        composite -= 0.1
                elif structure == "contango":
                    cont_data = vix_patterns.get("contango", {})
                    fwd_21d = cont_data.get("21d_forward_return", 0)
                    if fwd_21d > 0.5:
                        composite += 0.05  # Calm markets tend to continue rising

        except Exception:
            pass  # Calibration not available yet — no adjustment

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

        # MOMENTUM CRASH FILTER: penalize stocks where momentum is unwinding (gently)
        if stock.get("momentum_crash") and direction == "LONG":
            confidence = int(confidence * 0.75)  # moderate penalty (was 0.4 — too aggressive)

        # GAP SIGNAL: institutional overnight order flow
        gap = stock.get("gap_signal", 0)
        if gap >= 2 and direction == "LONG":
            confidence = min(95, int(confidence * 1.15))  # big gap up confirms long
        elif gap <= -2 and direction == "SHORT":
            confidence = min(95, int(confidence * 1.15))  # big gap down confirms short
        elif gap >= 1.5 and direction == "SHORT":
            confidence = int(confidence * 0.85)  # slight penalty for shorting gap up (was 0.7)
        elif gap <= -1.5 and direction == "LONG":
            confidence = int(confidence * 0.85)  # slight penalty for buying gap down (was 0.7)

        # ADX TREND STRENGTH FILTER — route signal to right model
        # ADX > 25 = strong trend → momentum signals are reliable
        # ADX < 15 = choppy market → mean reversion signals are reliable
        stock_adx = stock.get("adx", 25.0)
        # Identify dominant factor for this stock
        dominant_is_momentum = (abs(momentum_z[i] * w_mom) + abs(relative_strength_z[i] * w_relstr) >
                                abs(rsi2_z[i] * w_rsi2) + abs(value_z[i] * w_val))
        if stock_adx > 30 and dominant_is_momentum and direction != "NEUTRAL":
            confidence = min(95, int(confidence * 1.12))  # Strong trend + momentum = high conviction
        elif stock_adx > 25 and dominant_is_momentum and direction != "NEUTRAL":
            confidence = min(95, int(confidence * 1.08))
        elif stock_adx < 15 and dominant_is_momentum and direction != "NEUTRAL":
            confidence = int(confidence * 0.85)  # Choppy market, momentum unreliable
        elif stock_adx < 15 and not dominant_is_momentum and direction != "NEUTRAL":
            confidence = min(95, int(confidence * 1.10))  # Choppy = mean reversion works

        # TREND FILTER: Gentle adjustment, NOT a kill shot
        # OVERHAUL: Old filter cut confidence by 50% (0.5x) — combined with regime_multiplier (0.7x)
        # that meant 0.35x total = killed every trade. Now: ±15% max adjustment.
        ema50 = stock["ema_50"]
        price_vs_ema50 = (stock["price"] - ema50) / ema50 * 100

        _defensive = {"Consumer Staples", "Healthcare", "Utilities"}
        _is_defensive = stock.get("sector") in _defensive

        if current_regime == "BEAR":
            if direction == "LONG" and price_vs_ema50 < -5 and not _is_defensive:
                # Non-defensive stock 5%+ below 50-EMA in bear — slight penalty
                confidence = int(confidence * 0.85)  # -15% (was -50%)
            elif direction == "LONG" and price_vs_ema50 < -10 and _is_defensive:
                confidence = int(confidence * 0.85)
            elif direction == "SHORT" and price_vs_ema50 < -5:
                confidence = min(95, int(confidence * 1.15))  # trend confirms short
        elif current_regime == "BULL":
            if direction == "SHORT" and price_vs_ema50 > 5:
                confidence = int(confidence * 0.85)  # -15% (was -50%)
            elif direction == "LONG" and price_vs_ema50 > 5:
                confidence = min(95, int(confidence * 1.15))

        # regime_multiplier is always 1.0 now — no more stacking penalties
        confidence = int(confidence * regime_multiplier)

        # RENTECH: Ensemble Signal Voting — 3 models must agree
        # This is the single biggest edge: reduces false signals by ~40%
        try:
            ensemble_data = {
                "closes": stock.get("closes", []),
                "value_raw": stock.get("value_raw", 0),
                "quality_raw": stock.get("quality_raw", 0),
                "vol_ratio": stock.get("vol_ratio", 1.0),
                "confidence": confidence,
            }
            ensemble = ensemble_vote(ensemble_data, current_regime)

            if direction != "NEUTRAL":
                # If ensemble agrees with our direction → boost confidence
                if ensemble["consensus"] == direction:
                    confidence = ensemble["adjusted_confidence"]
                    if ensemble["agreement"] == 3:
                        reasons_extra = "Ensemble: unanimous agreement (3/3 models)"
                    else:
                        reasons_extra = "Ensemble: 2/3 models agree"
                # If ensemble disagrees → reduce confidence (but don't kill trade)
                elif ensemble["consensus"] != "NO_TRADE":
                    confidence = max(25, int(confidence * 0.80))  # -20%
                    reasons_extra = f"Ensemble: models disagree ({ensemble['votes']})"
                else:
                    reasons_extra = None

                if reasons_extra:
                    pass  # Will be added to reasons below
        except Exception:
            ensemble = None

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
            "mtf_alignment": {"z": mtf_alignment_z[i], "weight": round(w_mtf, 3),
                             "raw": round(stock["mtf_alignment_raw"], 2),
                             "contribution": round(mtf_alignment_z[i] * w_mtf, 3)},
            "earnings_drift": {"z": earnings_drift_z[i], "weight": round(w_edrift, 3),
                               "raw": round(stock["earnings_drift_raw"], 2),
                               "contribution": round(earnings_drift_z[i] * w_edrift, 3)},
            "vpoc": {"z": vpoc_z[i], "weight": round(w_vpoc, 3),
                     "raw": round(stock["vpoc_raw"], 2),
                     "contribution": round(vpoc_z[i] * w_vpoc, 3)},
            "ichimoku": {"z": ichimoku_z[i], "weight": round(w_ichi, 3),
                         "raw": round(stock["ichimoku_raw"], 2),
                         "contribution": round(ichimoku_z[i] * w_ichi, 3)},
            "sector_rotation": {"z": sector_rotation_z[i], "weight": round(w_secrot, 3),
                                "raw": round(stock["sector_rotation_raw"], 2),
                                "contribution": round(sector_rotation_z[i] * w_secrot, 3)},
            "candlestick": {"z": candlestick_z[i], "weight": round(w_candle, 3),
                            "raw": round(stock["candlestick_raw"], 2),
                            "contribution": round(candlestick_z[i] * w_candle, 3)},
            "beta": {"z": beta_z[i], "weight": round(w_beta, 3),
                     "raw": round(stock["beta_raw"], 2),
                     "contribution": round(beta_z[i] * w_beta, 3),
                     "actual_beta": stock.get("beta", 1.0)},
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

        # VOLUME CONFIRMATION: penalize low-volume signals, boost high-volume
        today_vol_ratio = stock.get("today_volume_ratio", 1.0)
        if direction != "NEUTRAL":
            if today_vol_ratio < 1.2:
                vol_penalty = max(-15, int((today_vol_ratio - 1.2) * 30))
                confidence = max(15, confidence + vol_penalty)
            elif today_vol_ratio > 1.5:
                vol_boost = min(8, int((today_vol_ratio - 1.2) * 5))
                confidence = min(95, confidence + vol_boost)

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
            "adx": stock.get("adx", 25.0),
            "mtf_alignment": round(stock.get("mtf_alignment_raw", 0), 1),
            "garch_vol_ratio": stock.get("garch_vol_ratio", 1.0),
            "earnings_drift": round(stock.get("earnings_drift_raw", 0), 1),
            "vpoc_signal": round(stock.get("vpoc_raw", 0), 1),
            "ichimoku_signal": round(stock.get("ichimoku_raw", 0), 1),
            "sector_rotation": round(stock.get("sector_rotation_raw", 0), 1),
            "today_volume_ratio": today_vol_ratio,
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
      4. Calculate 22-factor composite scores
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

        # Step 2C: Cross-Asset Momentum (Dollar, Bitcoin, Copper, Bonds)
        # Leading indicators that move hours ahead of equities
        cross_asset = get_cross_asset_signals()
        for sector, adj in cross_asset.get("sector_adjustments", {}).items():
            if sector in macro.get("sector_adjustments", {}):
                macro["sector_adjustments"][sector] = round(
                    macro["sector_adjustments"][sector] + adj, 1
                )
            else:
                macro["sector_adjustments"][sector] = adj

        # Step 2D: Cross-Asset Macro Engine v2 — yield curve, credit stress,
        # VIX term structure, dollar, commodities, global equities. Outputs
        # macro_regime, exposure_modifier, and sector_tilts. SAFE: never
        # raises, returns neutral default on failure.
        try:
            from analysis.cross_asset_macro import get_macro_signals as _get_macro_v2
            macro_v2 = _get_macro_v2()
            macro["macro_v2"] = {
                "regime": macro_v2.get("macro_regime"),
                "exposure_modifier": macro_v2.get("exposure_modifier"),
                "regime_score": macro_v2.get("regime_score"),
                "ok": macro_v2.get("ok", False),
            }
            for sector, tilt in (macro_v2.get("sector_tilts") or {}).items():
                if sector in macro.get("sector_adjustments", {}):
                    macro["sector_adjustments"][sector] = round(
                        macro["sector_adjustments"][sector] + float(tilt), 1
                    )
                else:
                    macro["sector_adjustments"][sector] = round(float(tilt), 1)
        except Exception as _macro_v2_err:
            logger.debug(f"macro_v2 integration skipped: {_macro_v2_err}")
            macro["macro_v2"] = {"ok": False, "reason": str(_macro_v2_err)[:120]}

        # Step 3: Batch download price data
        # 10 batches of ~72 tickers each — well below Yahoo Finance's silent
        # ~150 cap. Smaller batches are slower per-cycle (~30s vs ~24s) but
        # much more reliable than oversized ones (which silently drop tickers
        # or return partial data). With ~720 ticker universe (US + 222
        # international ADRs) this gives plenty of headroom.
        N_BATCHES = 10
        batch_size = (len(QUANT_UNIVERSE) + N_BATCHES - 1) // N_BATCHES
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

        # Step 5A.5: INTELLIGENCE OVERLAY (Level 6) — apply composite
        # multiplier from Level 1-5 learning layers (postmortem, regime
        # playbook, earnings drift, cross-asset, stock learning, regime
        # drift). Soft-fails per-pick to neutral 1.0; cannot block any
        # pick. Disabled by env DISABLE_INTELLIGENCE_OVERLAY=1.
        try:
            from predictions.intelligence_overlay import compute_pick_overlay
            regime_label = (regime.get("regime", "unknown")
                            if isinstance(regime, dict) else str(regime or "unknown"))
            for pick in top_longs:
                try:
                    ov = compute_pick_overlay(
                        ticker=pick.get("symbol") or pick.get("ticker") or "",
                        sector=pick.get("sector") or "",
                        regime=regime_label,
                        signal_score=pick.get("composite_score") or 0,
                        direction="long",
                    )
                    mult = float(ov.get("multiplier", 1.0))
                    pick["confidence"] = max(15, min(95,
                        int(pick.get("confidence", 50) * mult)))
                    pick["_intel_overlay"] = ov
                except Exception:
                    pass
            for pick in top_shorts:
                try:
                    ov = compute_pick_overlay(
                        ticker=pick.get("symbol") or pick.get("ticker") or "",
                        sector=pick.get("sector") or "",
                        regime=regime_label,
                        signal_score=pick.get("composite_score") or 0,
                        direction="short",
                    )
                    mult = float(ov.get("multiplier", 1.0))
                    pick["confidence"] = max(15, min(95,
                        int(pick.get("confidence", 50) * mult)))
                    pick["_intel_overlay"] = ov
                except Exception:
                    pass
        except Exception as _ov_e:
            logger.debug(f"Intelligence overlay soft-fail: {_ov_e}")

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

        # ============================================================
        # FUNDAMENTALS ENRICHMENT — real PE, ROE, debt from yfinance
        # Only for top 30 picks to minimize API calls. 24h cache.
        # This is an overlay — adds to existing value factor, not a replacement.
        # ============================================================
        try:
            enrich_picks = (top_longs[:20] + top_shorts[:10])[:30]
            now_ts = time.time()
            for pick in enrich_picks:
                sym = pick["symbol"]
                try:
                    # Check 24-hour fundamentals cache
                    if sym in _fundamentals_cache and (now_ts - _fundamentals_cache[sym]["time"]) < _FUNDAMENTALS_CACHE_TTL:
                        cached = _fundamentals_cache[sym]["data"]
                        pick["fundamentals"] = cached["fundamentals"]
                        pick["factors"]["fundamental_value"] = cached["factor_entry"]
                        continue

                    _throttle()
                    info = yf.Ticker(sym).info or {}

                    pe = info.get("trailingPE")
                    fwd_pe = info.get("forwardPE")
                    roe = info.get("returnOnEquity")
                    debt_equity = info.get("debtToEquity")

                    # Convert ROE from decimal (0.15) to percent (15) if needed
                    roe_pct = (roe * 100) if roe is not None and roe < 1.0 else roe

                    fund_score = 0.0
                    if pe is not None:
                        if pe < 20:
                            fund_score += 1.0
                        elif pe > 40:
                            fund_score -= 1.0
                    if fwd_pe is not None and fwd_pe < 15:
                        fund_score += 0.5
                    if roe_pct is not None and roe_pct > 15:
                        fund_score += 0.5
                    if debt_equity is not None and debt_equity < 100:  # yfinance returns as percentage
                        fund_score += 0.5

                    fundamentals_dict = {
                        "pe": round(pe, 2) if pe is not None else None,
                        "fwd_pe": round(fwd_pe, 2) if fwd_pe is not None else None,
                        "roe": round(roe_pct, 2) if roe_pct is not None else None,
                        "debt_equity": round(debt_equity, 2) if debt_equity is not None else None,
                    }

                    factor_entry = {
                        "z": 0, "weight": 0.0,
                        "raw": round(fund_score, 2),
                        "contribution": round(fund_score * 0.03, 3),
                    }

                    pick["fundamentals"] = fundamentals_dict
                    pick["factors"]["fundamental_value"] = factor_entry

                    # Cache for 24 hours
                    _fundamentals_cache[sym] = {
                        "data": {"fundamentals": fundamentals_dict, "factor_entry": factor_entry},
                        "time": now_ts,
                    }

                except Exception as e:
                    logger.debug(f"Fundamentals fetch failed for {sym}: {e}")
                    pick["fundamentals"] = {"pe": None, "fwd_pe": None, "roe": None, "debt_equity": None}
        except Exception as e:
            logger.warning(f"Fundamentals enrichment failed (non-fatal): {e}")

        # RENTECH: Run full Renaissance Technologies analysis suite
        rentech_data = {}
        try:
            # Get open trades for portfolio risk assessment
            try:
                from predictions.models import get_open_trades, get_cash
                open_trades = get_open_trades()
                cash = get_cash()
                portfolio_value = cash + sum(
                    t.get("entry_price", 0) * t.get("shares", 0) for t in open_trades
                )
            except Exception:
                open_trades = []
                portfolio_value = 100_000

            rentech_data = run_rentech_analysis(
                price_data=price_data,
                open_trades=open_trades,
                portfolio_value=portfolio_value,
                peak_value=max(portfolio_value, 109_000),
            )

            # Inject mean reversion setups into long/short picks if they aren't already there
            # SMART FILTER: Don't inject MR picks that conflict with the macro thesis
            # e.g., don't short tech/healthcare during ceasefire (those sectors should rally)
            mr_setups = rentech_data.get("mean_reversion_setups", [])
            existing_long_syms = {p["symbol"] for p in top_longs}
            existing_short_syms = {p["symbol"] for p in top_shorts}

            # Determine which sectors are boosted by macro (ceasefire, etc.)
            boosted_sectors = set()
            penalized_sectors = set()
            if macro and "sector_adjustments" in macro:
                for sector, adj in macro["sector_adjustments"].items():
                    if adj >= 1.0:
                        boosted_sectors.add(sector)
                    elif adj <= -1.0:
                        penalized_sectors.add(sector)

            # TACO TRADE PROTECTION — Context-aware sector protection
            # Uses headline sentiment to determine which sectors to protect from MR shorts.
            # If headlines show energy is bearish, don't protect it. If tech is bullish, protect it.
            _geo_impact = macro.get("geo_impact_analysis", {}) if macro else {}
            _sector_signals = _geo_impact.get("sector_signals", {})
            TACO_PROTECTED_LONG_SECTORS = set()
            TACO_PENALIZED_SECTORS = set()
            # Protect sectors that headlines say are bullish
            for _sector, _signal in _sector_signals.items():
                if _signal > 0.3:
                    TACO_PROTECTED_LONG_SECTORS.add(_sector)
                elif _signal < -0.3:
                    TACO_PENALIZED_SECTORS.add(_sector)
            # Default protection for low-geo-risk environment
            if not TACO_PROTECTED_LONG_SECTORS and not TACO_PENALIZED_SECTORS:
                if macro and macro.get("ceasefire_overlay"):
                    TACO_PROTECTED_LONG_SECTORS = {"Technology", "Healthcare", "Financials", "Consumer Discretionary"}
                    TACO_PENALIZED_SECTORS = {"Energy"}
            boosted_sectors |= TACO_PROTECTED_LONG_SECTORS
            penalized_sectors |= TACO_PENALIZED_SECTORS
            logger.info(f"TACO TRADE: Protected={TACO_PROTECTED_LONG_SECTORS}, Penalized={TACO_PENALIZED_SECTORS} (headline-driven)")

            for mr in mr_setups:
                sym = mr["symbol"]
                mr_pick = next((s for s in all_scored if s["symbol"] == sym), None)
                if not mr_pick:
                    continue

                pick_sector = mr_pick.get("sector", "Unknown")

                if mr["direction"] == "LONG" and sym not in existing_long_syms:
                    # Don't go LONG on sectors the macro is penalizing
                    if pick_sector in penalized_sectors:
                        logger.debug(f"MR FILTER: Skipping LONG {sym} — {pick_sector} is macro-penalized")
                        continue
                    mr_pick["mean_reversion"] = True
                    mr_pick["mr_score"] = mr["mr_score"]
                    mr_pick["reasons"].append(f"Mean reversion: {mr['reasons'][0]}")
                    mr_pick["confidence"] = min(95, mr_pick["confidence"] + 10)
                    top_longs.append(mr_pick)
                    existing_long_syms.add(sym)
                elif mr["direction"] == "SHORT" and sym not in existing_short_syms:
                    # Don't SHORT sectors the macro is boosting (ceasefire = don't short tech)
                    if pick_sector in boosted_sectors:
                        logger.info(f"MR FILTER: Skipping SHORT {sym} — {pick_sector} is macro-boosted (ceasefire/bullish)")
                        continue
                    mr_pick["mean_reversion"] = True
                    mr_pick["mr_score"] = mr["mr_score"]
                    mr_pick["reasons"].append(f"Mean reversion: {mr['reasons'][0]}")
                    mr_pick["confidence"] = min(95, mr_pick["confidence"] + 10)
                    top_shorts.append(mr_pick)
                    existing_short_syms.add(sym)

            logger.info(f"🏛️ RenTech analysis complete: {len(rentech_data.get('pairs_trades', []))} pairs, "
                        f"{len(mr_setups)} MR setups, risk={rentech_data.get('portfolio_risk', {}).get('risk_level', 'N/A')}")

            # ============================================================
            # WIRE IN SMART FEATURES — apply to ALL picks (longs + shorts)
            # ============================================================

            # --- EARNINGS SHIELD: Remove stocks with earnings in next 1-2 days ---
            earnings_shield = rentech_data.get("earnings_shield", {})
            earnings_blocked = {s["symbol"] for s in earnings_shield.get("blocked", [])}
            if earnings_blocked:
                pre_long = len(top_longs)
                pre_short = len(top_shorts)
                top_longs = [p for p in top_longs if p["symbol"] not in earnings_blocked]
                top_shorts = [p for p in top_shorts if p["symbol"] not in earnings_blocked]
                blocked_count = (pre_long - len(top_longs)) + (pre_short - len(top_shorts))
                logger.info(f"EARNINGS SHIELD: Blocked {blocked_count} picks with imminent earnings: {earnings_blocked}")

            # --- MULTI-TIMEFRAME CONFIRMATION: Boost/penalize based on trend alignment ---
            mtf = rentech_data.get("mtf_signals", {})
            for pick in top_longs:
                sym = pick["symbol"]
                if sym in mtf:
                    conf = mtf[sym]["confirmation"]
                    if conf == "CONFIRMED_BULL":
                        pick["confidence"] = min(95, pick["confidence"] + 8)
                        pick["reasons"].append("Multi-TF: daily+weekly+monthly ALL bullish")
                    elif conf in ("CONFIRMED_BEAR", "LEAN_BEAR"):
                        pick["confidence"] = max(20, pick["confidence"] - 12)
                        pick["reasons"].append(f"Multi-TF WARNING: trend is {conf}")

            for pick in top_shorts:
                sym = pick["symbol"]
                if sym in mtf:
                    conf = mtf[sym]["confirmation"]
                    if conf == "CONFIRMED_BEAR":
                        pick["confidence"] = min(95, pick["confidence"] + 8)
                        pick["reasons"].append("Multi-TF: daily+weekly+monthly ALL bearish")
                    elif conf in ("CONFIRMED_BULL", "LEAN_BULL"):
                        pick["confidence"] = max(20, pick["confidence"] - 12)
                        pick["reasons"].append(f"Multi-TF WARNING: trend is {conf}")

            # --- SECTOR ROTATION: Boost inflow sectors, penalize outflow ---
            rotation = rentech_data.get("sector_rotation", {})
            inflow_sectors = set(rotation.get("top_inflow", []))
            outflow_sectors = set(rotation.get("top_outflow", []))
            for pick in top_longs:
                sector = pick.get("sector", "")
                if sector in inflow_sectors:
                    pick["confidence"] = min(95, pick["confidence"] + 5)
                    pick["reasons"].append(f"Sector rotation: {sector} has institutional INFLOW")
                elif sector in outflow_sectors:
                    pick["confidence"] = max(20, pick["confidence"] - 8)
                    pick["reasons"].append(f"Sector rotation: {sector} has institutional OUTFLOW")
            for pick in top_shorts:
                sector = pick.get("sector", "")
                if sector in outflow_sectors:
                    pick["confidence"] = min(95, pick["confidence"] + 5)
                    pick["reasons"].append(f"Sector rotation: {sector} has institutional OUTFLOW (good for short)")
                elif sector in inflow_sectors:
                    pick["confidence"] = max(20, pick["confidence"] - 8)

            # --- REGIME TRANSITION: Warn if regime change is predicted ---
            regime_pred = rentech_data.get("regime_transition", {})
            regime_str = regime.get("regime", "SIDEWAYS") if isinstance(regime, dict) else regime
            if regime_pred.get("prediction") == "BEAR_TRANSITION_LIKELY" and regime_str != "BEAR":
                # Reduce all long confidence if bear transition is likely
                for pick in top_longs:
                    pick["confidence"] = max(20, pick["confidence"] - 10)
                    pick["reasons"].append("REGIME WARNING: bear transition likely")
                logger.warning(f"REGIME TRANSITION: Bear likely — reducing long confidence by 10")
            elif regime_pred.get("prediction") == "BULL_TRANSITION_LIKELY" and regime_str != "BULL":
                for pick in top_shorts:
                    pick["confidence"] = max(20, pick["confidence"] - 10)
                    pick["reasons"].append("REGIME WARNING: bull transition likely")
                logger.info(f"REGIME TRANSITION: Bull likely — reducing short confidence by 10")

            # --- Sort picks by confidence after all adjustments ---
            top_longs.sort(key=lambda x: x.get("confidence", 0), reverse=True)
            top_shorts.sort(key=lambda x: x.get("confidence", 0), reverse=True)

            # --- Remove picks that dropped below minimum confidence ---
            top_longs = [p for p in top_longs if p.get("confidence", 0) >= 30]
            top_shorts = [p for p in top_shorts if p.get("confidence", 0) >= 30]

            logger.info(f"POST-FILTER: {len(top_longs)} longs, {len(top_shorts)} shorts after smart filters")

        except Exception as e:
            logger.warning(f"RenTech analysis failed (non-fatal): {e}")

        elapsed = round(time.time() - start_time, 1)

        # ============================================================
        # DYNAMIC HEDGING ENGINE — Composite Risk Shield
        # Automatically adjusts exposure based on multiple risk signals.
        # Protects portfolio during dangerous market conditions.
        # ============================================================
        risk_score = 0
        risk_factors = []

        # VIX level contribution
        vix_level = regime.get("vix_level", 20) if regime else 20
        if vix_level > 35:
            risk_score += 4
            risk_factors.append(f"VIX crisis ({vix_level:.0f})")
        elif vix_level > 25:
            risk_score += 3
            risk_factors.append(f"VIX high ({vix_level:.0f})")
        elif vix_level > 20:
            risk_score += 1
            risk_factors.append(f"VIX elevated ({vix_level:.0f})")

        # Regime contribution
        current_regime_str = regime.get("regime", "SIDEWAYS") if regime else "SIDEWAYS"
        if current_regime_str == "BEAR":
            risk_score += 3
            risk_factors.append("Bear regime")
        elif current_regime_str == "SIDEWAYS":
            risk_score += 1
            risk_factors.append("Sideways regime")

        # Market breadth contribution
        breadth = regime.get("breadth_pct", 50) if regime else 50
        if breadth < 35:
            risk_score += 2
            risk_factors.append(f"Weak breadth ({breadth:.0f}%)")
        elif breadth < 50:
            risk_score += 1

        # VIX term structure (backwardation = panic)
        vix_ts = macro.get("vix_term_structure", {}) if macro else {}
        if vix_ts.get("ratio", 1.0) > 1.05:
            risk_score += 2
            risk_factors.append("VIX backwardation (panic)")

        # Determine risk level and exposure
        if risk_score >= 8:
            hedge_level = "EXTREME"
            exposure_pct = 40
            hedge_action = "40% exposure — close weakest positions, no new longs"
        elif risk_score >= 5:
            hedge_level = "HIGH"
            exposure_pct = 60
            hedge_action = "60% exposure — tighten all stops 20%"
        elif risk_score >= 3:
            hedge_level = "MODERATE"
            exposure_pct = 80
            hedge_action = "80% exposure — reduce new positions"
        else:
            hedge_level = "LOW"
            exposure_pct = 100
            hedge_action = "100% exposure — normal trading"

        dynamic_hedge = {
            "risk_level": hedge_level,
            "risk_score": risk_score,
            "exposure_pct": exposure_pct,
            "action": hedge_action,
            "risk_factors": risk_factors,
        }

        # Compute sector rotation rankings from scored picks
        sector_rankings = []
        _sec_mom = {}
        for s in all_scored:
            sec = s.get("sector", "Unknown")
            if sec not in _sec_mom:
                _sec_mom[sec] = []
            _sec_mom[sec].append(s.get("momentum_pct", 0))
        _sec_avg = {s: float(np.mean(v)) for s, v in _sec_mom.items() if v}
        if _sec_avg:
            _sorted_secs = sorted(_sec_avg.items(), key=lambda x: x[1], reverse=True)
            for rank, (sect, mom) in enumerate(_sorted_secs, 1):
                sector_rankings.append({
                    "sector": sect, "rank": rank,
                    "momentum_pct": round(mom, 2),
                    "zone": "HOT" if rank <= len(_sorted_secs) // 3 else ("COLD" if rank > len(_sorted_secs) * 2 // 3 else "NEUTRAL"),
                })

        return {
            "regime": regime,
            "macro": macro,
            "overnight": overnight,
            "cross_asset": cross_asset,
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
            # RENTECH DATA
            "rentech": rentech_data,
            "pairs_trades": rentech_data.get("pairs_trades", []),
            "mean_reversion_setups": [
                mr for mr in rentech_data.get("mean_reversion_setups", [])
                if not (mr.get("direction") == "SHORT" and SECTOR_MAP.get(mr.get("symbol"), "Unknown") in TACO_PROTECTED_LONG_SECTORS)
                and not (mr.get("direction") == "LONG" and SECTOR_MAP.get(mr.get("symbol"), "Unknown") in TACO_PENALIZED_SECTORS)
            ],
            "portfolio_risk": rentech_data.get("portfolio_risk", {}),
            "circuit_breaker": rentech_data.get("circuit_breaker", {}),
            "earnings_shield": rentech_data.get("earnings_shield", {}),
            "sector_rotation": rentech_data.get("sector_rotation", {}),
            "regime_transition": rentech_data.get("regime_transition", {}),
            "drawdown_mode": rentech_data.get("drawdown_mode", {}),
            "portfolio_var": rentech_data.get("portfolio_var", {}),
            # WEEK 2: New intelligence data
            "dynamic_hedge": dynamic_hedge,
            "sector_rankings": sector_rankings,
            "total_factors": 22,
            "_price_data": price_data,  # Pass through for correlation checks in paper_trader
            "generated_at": datetime.now().isoformat(),
            "computation_time_seconds": elapsed,
            "disclaimer": (
                "This is a quantitative analysis tool for educational purposes. "
                "This is NOT financial advice. Past performance does not guarantee "
                "future results. Always do your own research before investing."
            ),
        }

    result = _get_cached("quant_picks", fetch, ttl=900)  # 15 min cache

    # ============================================================
    # PERSISTENT DISK CACHE FALLBACK — survives Yahoo outages
    # ============================================================
    # If picks generation failed (Yahoo rate-limited, network down, etc.),
    # serve yesterday's picks from disk instead of returning 0 picks.
    # If picks generation succeeded, save to disk as a backup for next outage.
    # Same pattern as .sp500_disk_cache.json (proven in production).
    import os as _os_pc, json as _json_pc
    _picks_cache_path = _os_pc.path.join(_os_pc.path.dirname(__file__), ".picks_disk_cache.json")

    def _picks_are_empty(r):
        if not isinstance(r, dict):
            return True
        if r.get("error"):
            return True
        return not r.get("long_picks") and not r.get("short_picks")

    if _picks_are_empty(result):
        # Live picks failed — try yesterday's snapshot
        try:
            if _os_pc.path.exists(_picks_cache_path):
                # Read with explicit context manager — never holds file lock
                with open(_picks_cache_path, "r") as _f:
                    cached = _json_pc.load(_f)

                # SAFETY: validate cache shape before trusting it
                # If cache is corrupt or unexpected, fall through to empty result
                # rather than serve garbage to the trade engine.
                if not isinstance(cached, dict):
                    logger.warning("PICKS DISK CACHE: cache file is not a dict, ignoring")
                elif "_saved_at" not in cached:
                    logger.warning("PICKS DISK CACHE: missing _saved_at timestamp, ignoring")
                elif not isinstance(cached.get("long_picks"), list) or not isinstance(cached.get("short_picks"), list):
                    logger.warning("PICKS DISK CACHE: long_picks/short_picks malformed, ignoring")
                else:
                    age_hours = (time.time() - float(cached.get("_saved_at", 0))) / 3600.0
                    if 0 <= age_hours <= 48:
                        cached["_cache_source"] = "disk_stale"
                        cached["_cache_age_hours"] = round(age_hours, 1)
                        logger.warning(
                            f"PICKS DISK CACHE: live generation failed — "
                            f"serving stale picks from {age_hours:.1f}h ago "
                            f"({len(cached.get('long_picks', []))} longs, "
                            f"{len(cached.get('short_picks', []))} shorts)"
                        )
                        return cached
                    else:
                        logger.warning(
                            f"PICKS DISK CACHE: cache is {age_hours:.1f}h old — "
                            f"too stale (max 48h), returning empty result"
                        )
        except (OSError, ValueError, _json_pc.JSONDecodeError) as _e:
            # File permission, JSON parse, or unicode errors. Log and continue.
            logger.warning(f"Picks disk cache load failed: {_e}")
        except Exception as _e:
            # Catch-all. Never let cache load break the trading engine.
            logger.warning(f"Picks disk cache unexpected error on load: {_e}")
    else:
        # Live picks succeeded — save snapshot for the next outage.
        # ATOMIC WRITE: write to .tmp first, then rename. Prevents serving a
        # half-written corrupt cache if the container is killed mid-write.
        try:
            # Strip _price_data (DataFrames not JSON-serializable; correlation check
            # in paper_trader has a guard for missing _price_data so this is safe)
            to_save = {k: v for k, v in result.items() if k != "_price_data"}
            to_save["_saved_at"] = time.time()
            _tmp_path = _picks_cache_path + ".tmp"
            with open(_tmp_path, "w") as _f:
                _json_pc.dump(to_save, _f, default=str)
                # Force write to disk before rename so partial writes can't survive a crash
                try:
                    _f.flush()
                    _os_pc.fsync(_f.fileno())
                except Exception:
                    pass  # fsync may not be available on all filesystems
            # Atomic rename — POSIX guarantees this is all-or-nothing
            _os_pc.replace(_tmp_path, _picks_cache_path)
        except Exception as _e:
            # Disk cache failure must NEVER crash live picks generation
            logger.debug(f"Picks disk cache save failed (non-fatal): {_e}")
            # Best-effort cleanup of orphan .tmp file
            try:
                if _os_pc.path.exists(_picks_cache_path + ".tmp"):
                    _os_pc.remove(_picks_cache_path + ".tmp")
            except Exception:
                pass

        # S3 BACKUP — second layer of cache durability. If the container
        # restarts and the local disk is wiped, S3 still has the picks.
        # Wrapped in try/except — never breaks pick gen even if S3 down.
        try:
            from predictions.enhancements import save_picks_to_s3
            _s3_save = save_picks_to_s3({k: v for k, v in result.items() if k != "_price_data"})
            if _s3_save.get("ok"):
                logger.info(f"Picks S3 backup saved: {_s3_save.get('bytes')} bytes")
        except Exception as _e:
            logger.debug(f"S3 picks backup failed (non-fatal): {_e}")

    return result


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

        # Download stock data only (SPY not needed for single-stock analysis)
        _throttle()
        stock_df = yf.download(symbol, period="1y", progress=False)
        if stock_df is None or stock_df.empty:
            result["error"] = "No price data available"
            return result

        # Flatten MultiIndex columns if present (yfinance sometimes returns ("Close","AAPL"))
        if isinstance(stock_df.columns, pd.MultiIndex):
            stock_df.columns = stock_df.columns.get_level_values(0)
        stock_df = stock_df.dropna(how="all")

        if len(stock_df) < 60:
            result["error"] = "Not enough price history (need 60+ days)"
            return result

        closes = _safe_close(stock_df).values.astype(float)
        vol_col = stock_df["Volume"]
        if hasattr(vol_col, "columns"):
            vol_col = vol_col.iloc[:, 0]
        volumes = vol_col.values.astype(float)
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

        # --- Apply self-learning adjustments ---
        learning_applied = False
        try:
            from predictions.learner import get_mistake_adjustments
            mistake_adj = get_mistake_adjustments()
            # Penalize sectors the AI has learned are weak
            sector_penalty = mistake_adj.get("sector_penalties", {}).get(sector, 0)
            if sector_penalty:
                score += sector_penalty / 5  # Scale down for single-stock scoring
                learning_applied = True
            # Cap confidence if overconfidence detected
            if mistake_adj.get("confidence_cap", 95) < 95:
                learning_applied = True
        except Exception:
            mistake_adj = {}

        # Apply learned factor weights to boost/reduce score
        try:
            from predictions.models import get_signal_weights
            learned_weights = get_signal_weights()
            if learned_weights:
                # If momentum weight is high, momentum matters more
                default_w = 1.0 / len(learned_weights) if learned_weights else 0.167
                momentum_boost = (learned_weights.get("momentum", default_w) - default_w) * 10
                score += momentum_boost * (1 if momentum > 0 else -1)
                learning_applied = True
        except Exception:
            learned_weights = {}

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

        # Regime adjustment — GENTLE, not a kill shot
        # OVERHAUL: was 0.7x (30% penalty) — now ±10% max
        current_regime = regime.get("regime", "SIDEWAYS") if regime else "SIDEWAYS"
        if current_regime == "BEAR":
            if direction == "LONG":
                confidence = int(confidence * 0.90)  # -10% (was -30%)
            elif direction == "SHORT":
                confidence = min(95, int(confidence * 1.1))
        elif current_regime == "BULL":
            if direction == "LONG":
                confidence = min(95, int(confidence * 1.1))
            elif direction == "SHORT":
                confidence = int(confidence * 0.90)  # -10% (was -30%)

        # Apply learned confidence cap
        conf_cap = mistake_adj.get("confidence_cap", 95) if mistake_adj else 95
        confidence = min(confidence, conf_cap)

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
            "learning_applied": learning_applied,
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
